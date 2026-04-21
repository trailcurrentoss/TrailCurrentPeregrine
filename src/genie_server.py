#!/usr/bin/env python3
"""Thin HTTP wrapper around Qualcomm Genie for NPU LLM inference.

Exposes an Ollama-compatible /api/generate endpoint so assistant.py can use
the Hexagon NPU without code changes — just point OLLAMA_URL at this server.

Primary path: persistent ctypes binding to libGenie.so. The GenieDialog
handle is created once at service start and kept resident across queries,
eliminating the ~15s model reload that the subprocess path pays per query.

Fallback path: spawn genie-t2t-run as a subprocess. Kicks in automatically
if libGenie.so fails to load (e.g. missing .so, NPU access error).

Runs as a systemd service on the Radxa Dragon Q6A (QCS6490).
"""

import ctypes
import glob
import json
import os
import queue
import select
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- Config ---
GENIE_DIR = os.getenv("GENIE_DIR", os.path.expanduser("~/Llama3.2-1B-1024-v68"))
GENIE_BIN = os.path.join(GENIE_DIR, "genie-t2t-run")
GENIE_LIB = os.path.join(GENIE_DIR, "libGenie.so")


def _find_genie_config():
    """Locate the htp-model-config JSON in GENIE_DIR.

    Upstream bundles ship exactly one ``htp-model-config-*.json`` per model,
    but the filename embeds the model size (e.g. llama32-1b-gqa, llama32-3b).
    Auto-detecting lets us swap bundles without also updating this file.
    GENIE_CONFIG env var overrides if set.
    """
    override = os.getenv("GENIE_CONFIG")
    if override:
        return override
    matches = sorted(glob.glob(os.path.join(GENIE_DIR, "htp-model-config-*.json")))
    if not matches:
        # Fall back to the 1B default name so the error message from CDLL
        # points at the expected path rather than silently succeeding.
        return os.path.join(GENIE_DIR, "htp-model-config-llama32-1b-gqa.json")
    return matches[0]


GENIE_CONFIG = _find_genie_config()
HOST = os.getenv("GENIE_HOST", "127.0.0.1")
PORT = int(os.getenv("GENIE_PORT", "11434"))

# Set to "0" in the environment to force the subprocess fallback path.
USE_PERSISTENT_NPU = os.getenv("GENIE_PERSISTENT", "1") not in ("0", "false", "no")

# --- Genie C API constants (from GenieCommon.h / GenieDialog.h) ---
GENIE_STATUS_SUCCESS = 0
# GenieDialog_SentenceCode_t — response position indicator passed to callback
_SENTENCE_COMPLETE = 0
_SENTENCE_BEGIN = 1
_SENTENCE_CONTINUE = 2
_SENTENCE_END = 3
_SENTENCE_ABORT = 4
_SENTENCE_FINAL = {_SENTENCE_COMPLETE, _SENTENCE_END, _SENTENCE_ABORT}

# --- Subprocess-fallback constants ---
# Tokens that mark the end of a useful response. Genie emits one of these
# before `[END]` when the model produces a stop token.
_STOP_MARKERS = (b"<|eot_id|>", b"<|end_of_text|>", b"<|start_header_id|>", b"[END]")
# Hold back the trailing N bytes of the running buffer so a stop marker that
# straddles two reads can still be detected before partial text is emitted.
_WITHHOLD_BYTES = 24
# Wait at most this long for the NPU to start producing output.
_GENIE_TIMEOUT = 60


def _ensure_ld_library_path():
    """Re-exec with LD_LIBRARY_PATH=GENIE_DIR if not already set.

    libGenie.so has transitive deps (libQnnHtp.so, libQnnHtpV68Stub.so, etc.)
    that sit next to it in $GENIE_DIR. dlopen resolves them via the
    LD_LIBRARY_PATH captured at process start — it cannot be patched in later
    from Python. If the caller forgot to set it (e.g. manual invocation),
    re-exec so the child process inherits the correct env.
    """
    current = os.environ.get("LD_LIBRARY_PATH", "")
    paths = [p for p in current.split(":") if p]
    if GENIE_DIR in paths:
        return
    new_env = os.environ.copy()
    new_env["LD_LIBRARY_PATH"] = GENIE_DIR + (":" + current if current else "")
    os.execve(sys.executable, [sys.executable] + sys.argv, new_env)


def build_prompt(system: str, user_prompt: str) -> str:
    """Format system + user text into Llama 3.2 chat template."""
    parts = ["<|begin_of_text|>"]
    if system:
        parts.append(
            f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
        )
    parts.append(
        f"<|start_header_id|>user<|end_header_id|>\n\n{user_prompt}<|eot_id|>"
    )
    parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
    return "".join(parts)


# ============================================================================
# Persistent path: ctypes wrapper around libGenie.so
# ============================================================================

class _GenieLib:
    """Loads libGenie.so once and keeps a GenieDialog handle resident.

    The dialog handle represents a fully-loaded model on the NPU. Creating it
    is slow (~15s for 1B) because the weights get bound into the Hexagon DSP.
    Keeping it alive across queries avoids paying that cost per request.
    """

    # void (*)(const char* response, SentenceCode_t sentenceCode, const void* userData)
    _QueryCallback = ctypes.CFUNCTYPE(
        None, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p
    )

    def __init__(self):
        self._lib = ctypes.CDLL(GENIE_LIB, mode=ctypes.RTLD_GLOBAL)
        self._bind_signatures()
        self._config_handle = ctypes.c_void_p()
        self._dialog_handle = ctypes.c_void_p()
        self._lock = threading.Lock()
        self._load_dialog()

    def _bind_signatures(self):
        L = self._lib
        # Genie_Status_t GenieDialogConfig_createFromJson(const char*, handle*)
        L.GenieDialogConfig_createFromJson.restype = ctypes.c_int
        L.GenieDialogConfig_createFromJson.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        L.GenieDialogConfig_free.restype = ctypes.c_int
        L.GenieDialogConfig_free.argtypes = [ctypes.c_void_p]
        L.GenieDialog_create.restype = ctypes.c_int
        L.GenieDialog_create.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        L.GenieDialog_free.restype = ctypes.c_int
        L.GenieDialog_free.argtypes = [ctypes.c_void_p]
        L.GenieDialog_reset.restype = ctypes.c_int
        L.GenieDialog_reset.argtypes = [ctypes.c_void_p]
        # Genie_Status_t GenieDialog_query(
        #     handle, const char* query, SentenceCode_t, QueryCallback_t, void* userData)
        L.GenieDialog_query.restype = ctypes.c_int
        L.GenieDialog_query.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            self._QueryCallback,
            ctypes.c_void_p,
        ]
        L.Genie_getApiMajorVersion.restype = ctypes.c_uint32
        L.Genie_getApiMajorVersion.argtypes = []
        L.Genie_getApiMinorVersion.restype = ctypes.c_uint32
        L.Genie_getApiMinorVersion.argtypes = []

    def api_version(self):
        return (
            self._lib.Genie_getApiMajorVersion(),
            self._lib.Genie_getApiMinorVersion(),
        )

    def _load_dialog(self):
        with open(GENIE_CONFIG, "r") as f:
            cfg_json = f.read()
        # The config JSON references model .bin files by relative path.
        # Run createFromJson from GENIE_DIR so those paths resolve.
        prev_cwd = os.getcwd()
        os.chdir(GENIE_DIR)
        try:
            status = self._lib.GenieDialogConfig_createFromJson(
                cfg_json.encode("utf-8"),
                ctypes.byref(self._config_handle),
            )
            if status != GENIE_STATUS_SUCCESS:
                raise RuntimeError(
                    f"GenieDialogConfig_createFromJson failed: status={status}"
                )
            status = self._lib.GenieDialog_create(
                self._config_handle,
                ctypes.byref(self._dialog_handle),
            )
            if status != GENIE_STATUS_SUCCESS:
                raise RuntimeError(f"GenieDialog_create failed: status={status}")
        finally:
            os.chdir(prev_cwd)

    def stream(self, prompt: str):
        """Generator yielding (text_chunk, is_done) tuples for a query.

        GenieDialog_query is blocking and fires its callback synchronously
        from inside that call. To expose the callback as a generator we run
        the query on a worker thread and bridge via a queue.
        """
        q = queue.Queue()
        exception_ref = [None]

        def py_callback(response_bytes, sentence_code, _user_data):
            try:
                text = ""
                if response_bytes is not None:
                    text = response_bytes.decode("utf-8", errors="replace")
                is_final = sentence_code in _SENTENCE_FINAL
                q.put((text, is_final))
            except Exception as e:
                exception_ref[0] = e
                q.put(("", True))

        c_cb = self._QueryCallback(py_callback)

        def run_query():
            try:
                with self._lock:
                    reset_status = self._lib.GenieDialog_reset(self._dialog_handle)
                    if reset_status != GENIE_STATUS_SUCCESS:
                        print(f"[genie-server] warning: reset status={reset_status}")
                    status = self._lib.GenieDialog_query(
                        self._dialog_handle,
                        prompt.encode("utf-8"),
                        _SENTENCE_COMPLETE,
                        c_cb,
                        None,
                    )
                    if status != GENIE_STATUS_SUCCESS:
                        exception_ref[0] = RuntimeError(
                            f"GenieDialog_query failed: status={status}"
                        )
            except Exception as e:
                exception_ref[0] = e
            finally:
                q.put(("", True))  # guarantee termination of the consumer loop

        worker = threading.Thread(target=run_query, daemon=True)
        worker.start()

        done = False
        while not done:
            text, is_final = q.get()
            if text:
                yield (text, False)
            if is_final:
                yield ("", True)
                done = True
        worker.join(timeout=5)
        if exception_ref[0] is not None:
            raise exception_ref[0]

    def close(self):
        if self._dialog_handle:
            self._lib.GenieDialog_free(self._dialog_handle)
            self._dialog_handle = ctypes.c_void_p()
        if self._config_handle:
            self._lib.GenieDialogConfig_free(self._config_handle)
            self._config_handle = ctypes.c_void_p()


_genie_lib = None


def _init_genie_lib():
    """Attempt to load libGenie.so. Sets _genie_lib on success, None on failure."""
    global _genie_lib
    if not USE_PERSISTENT_NPU:
        print("[genie-server] GENIE_PERSISTENT=0 — using subprocess fallback")
        return
    if not os.path.isfile(GENIE_LIB):
        print(f"[genie-server] {GENIE_LIB} not found — using subprocess fallback")
        return
    try:
        t0 = time.monotonic()
        lib = _GenieLib()
        major, minor = lib.api_version()
        print(
            f"[genie-server] Persistent NPU ready (Genie API v{major}.{minor}, "
            f"load {time.monotonic() - t0:.1f}s)"
        )
        _genie_lib = lib
    except Exception as e:
        print(f"[genie-server] libGenie init failed: {e!r} — using subprocess fallback")
        _genie_lib = None


# ============================================================================
# Fallback path: subprocess-per-query
# ============================================================================

def _run_genie_stream_subprocess(prompt: str, num_predict: int = 200):
    """Spawn genie-t2t-run and yield (text_chunk, is_done) as tokens arrive.

    Reads stdout incrementally via a pipe (with ``stdbuf -o0`` to defeat any
    block-buffering the CLI may apply when stdout is not a terminal). Locates
    the ``[BEGIN]:`` marker, then yields each new slice of text as it arrives,
    withholding the last ``_WITHHOLD_BYTES`` bytes so a stop marker that
    straddles a read boundary can still be detected.

    The final yielded item is always ``("", True)``, signalling end of stream.
    """
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = GENIE_DIR

    proc = subprocess.Popen(
        ["stdbuf", "-o0", GENIE_BIN, "-c", GENIE_CONFIG, "-p", prompt],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=GENIE_DIR, env=env, bufsize=0,
    )

    buffer = b""
    started = False
    emitted_to = 0
    deadline = time.monotonic() + _GENIE_TIMEOUT

    try:
        while True:
            if time.monotonic() > deadline:
                break
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = proc.stdout.read1(256)
            if not chunk:
                break
            buffer += chunk

            if not started:
                idx = buffer.find(b"[BEGIN]:")
                if idx == -1:
                    continue
                start_pos = idx + len(b"[BEGIN]:")
                while (start_pos < len(buffer)
                       and buffer[start_pos:start_pos + 1] in (b" ", b"\t", b"\r", b"\n")):
                    start_pos += 1
                buffer = buffer[start_pos:]
                started = True
                emitted_to = 0

            stop_pos = -1
            for marker in _STOP_MARKERS:
                p = buffer.find(marker, emitted_to)
                if p != -1 and (stop_pos == -1 or p < stop_pos):
                    stop_pos = p

            if stop_pos != -1:
                if stop_pos > emitted_to:
                    final = buffer[emitted_to:stop_pos].decode("utf-8", errors="replace")
                    if final:
                        yield (final, False)
                yield ("", True)
                return

            safe_emit_to = max(emitted_to, len(buffer) - _WITHHOLD_BYTES)
            if safe_emit_to > emitted_to:
                text = buffer[emitted_to:safe_emit_to].decode("utf-8", errors="replace")
                if text:
                    yield (text, False)
                emitted_to = safe_emit_to

        if started and len(buffer) > emitted_to:
            remaining = buffer[emitted_to:].decode("utf-8", errors="replace")
            if remaining:
                yield (remaining, False)
        yield ("", True)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ============================================================================
# Unified entry points — pick persistent or subprocess at runtime
# ============================================================================

def run_genie_stream(prompt: str, num_predict: int = 200):
    """Yield (text_chunk, is_done) tuples. Uses persistent NPU when available."""
    if _genie_lib is not None:
        yield from _genie_lib.stream(prompt)
    else:
        yield from _run_genie_stream_subprocess(prompt, num_predict)


def run_genie(prompt: str, num_predict: int = 200) -> tuple[str, float]:
    """Run a query and return (response_text, duration_seconds).

    Backwards-compatible wrapper for callers (warmup, non-streaming HTTP path)
    that want the full response in one shot.
    """
    start = time.monotonic()
    parts = []
    for text, _done in run_genie_stream(prompt, num_predict):
        if text:
            parts.append(text)
    return "".join(parts).strip(), time.monotonic() - start


# ============================================================================
# HTTP server
# ============================================================================

class GenieHandler(BaseHTTPRequestHandler):
    """Handle Ollama-compatible /api/generate requests."""

    def do_POST(self):
        if self.path == "/api/generate":
            self._handle_generate()
        else:
            self.send_error(404)

    def do_GET(self):
        # Health check — Ollama returns 200 on GET /
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = json.dumps({
                "status": "ok",
                "backend": "persistent" if _genie_lib is not None else "subprocess",
            })
            self.wfile.write(payload.encode())
        else:
            self.send_error(404)

    def _handle_generate(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        system = body.get("system", "")
        user_prompt = body.get("prompt", "")
        if not user_prompt:
            self.send_error(400, "missing prompt")
            return

        prompt = build_prompt(system, user_prompt)
        if body.get("stream"):
            self._stream_generate(prompt)
        else:
            self._unary_generate(prompt)

    def _unary_generate(self, prompt):
        response_text, duration = run_genie(prompt)

        reply = {
            "model": "llama3.2:1b-npu",
            "response": response_text or "Sorry, I didn't get a response.",
            "done": True,
            "total_duration": int(duration * 1e9),
        }
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _stream_generate(self, prompt):
        """Stream tokens as NDJSON, one JSON object per line (Ollama-compatible)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        start = time.monotonic()
        try:
            for text, done in run_genie_stream(prompt):
                if done:
                    final = {
                        "model": "llama3.2:1b-npu",
                        "response": "",
                        "done": True,
                        "total_duration": int((time.monotonic() - start) * 1e9),
                    }
                    self.wfile.write((json.dumps(final) + "\n").encode())
                    self.wfile.flush()
                    return
                if not text:
                    continue
                chunk = {
                    "model": "llama3.2:1b-npu",
                    "response": text,
                    "done": False,
                }
                self.wfile.write((json.dumps(chunk) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-stream — drop the stream.
            return

    def log_message(self, format, *args):
        print(f"[genie-server] {args[0]}")


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def main():
    _ensure_ld_library_path()

    if not os.path.isfile(GENIE_BIN):
        print(f"ERROR: genie-t2t-run not found at {GENIE_BIN}")
        raise SystemExit(1)

    # Start server first so it's reachable during warmup
    server = ReusableHTTPServer((HOST, PORT), GenieHandler)
    print(f"Genie NPU server listening on {HOST}:{PORT}")

    # Initialize persistent backend; falls back silently to subprocess on failure
    _init_genie_lib()

    # Warm up with a tiny query — with the persistent path this confirms the
    # dialog is usable; with the fallback it primes the OS page cache.
    print(f"Warming up NPU model from {GENIE_DIR} ...")
    try:
        text, dur = run_genie(build_prompt("Reply with one word.", "hi"))
        print(f"  Warmup done in {dur:.1f}s: {text!r}")
    except Exception as e:
        print(f"  Warmup failed (will load on first query): {e}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    if _genie_lib is not None:
        _genie_lib.close()
    print("Server stopped.")


if __name__ == "__main__":
    main()
