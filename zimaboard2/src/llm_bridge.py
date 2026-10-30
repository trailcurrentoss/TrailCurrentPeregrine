#!/usr/bin/env python3
"""Genie-compatible HTTP bridge in front of llama-server (ZimaBoard 2 only).

assistant.py and web_chat.py talk to an Ollama-*shaped* API on
127.0.0.1:11434 — the one genie_server.py provides on the Q6A. This bridge
serves that exact API on the same port and forwards each request to
llama.cpp's llama-server (OpenAI-compatible /v1/chat/completions), so neither
client needs to know which board it runs on.

Forked from genie_server.py: the HTTP layer, the sentence-level dedup and the
"too long" apology are unchanged. The Genie/ctypes backend is replaced by a
streaming HTTP call. llama-server applies each GGUF's own chat template
(--jinja), so this file holds no model-specific template.

Stdlib only — runs under /usr/bin/python3 as the locked peregrine-llm user.
"""

import http.client
import json
import os
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# --- Config ---
HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.getenv("BRIDGE_PORT", "11434"))
LLAMA_URL = os.getenv("LLAMA_URL", "http://127.0.0.1:8080")
# Clients that send no num_predict (web_chat.py) get this many tokens.
DEFAULT_MAX_TOKENS = int(os.getenv("LLM_DEFAULT_MAX_TOKENS", "512"))
# Reported in the "model" field of every reply. Clients ignore it.
MODEL_LABEL = os.path.basename(os.getenv("LLM_MODEL", "llama-server")).removesuffix(".gguf")
# How long to wait for llama-server to produce the first byte. Generous,
# because a voice command may hold the server frozen for a second or two.
UPSTREAM_TIMEOUT = float(os.getenv("LLAMA_TIMEOUT", "180"))

_TOO_LONG = ("Sorry, that question was too long for me to answer "
             "on-device. Try something shorter.")
_NO_RESPONSE = "Sorry, I didn't get a response."

_upstream = urllib.parse.urlsplit(LLAMA_URL)


class ContextOverflow(RuntimeError):
    """llama-server rejected the prompt because it exceeds the slot context."""


def build_messages(system: str, messages: list) -> list:
    """Turn Genie's request shape into OpenAI chat messages.

    A top-level ``system`` string takes precedence over any system role in
    the list (same rule as genie_server.build_chat_prompt).
    """
    sys_text = system
    if not sys_text:
        for m in messages:
            if m.get("role") == "system" and m.get("content"):
                sys_text = m["content"]
                break
    out = []
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content})
    return out


# ============================================================================
# Backend: streaming call to llama-server
# ============================================================================

def _connect():
    return http.client.HTTPConnection(
        _upstream.hostname, _upstream.port or 80, timeout=UPSTREAM_TIMEOUT)


def _run_llama_stream(messages: list, max_tokens: int):
    """POST /v1/chat/completions with stream=true and yield (text, is_done).

    The final yielded item is always ``("", True)``. Raises ContextOverflow
    when the prompt does not fit the slot, RuntimeError on any other upstream
    failure.
    """
    body = json.dumps({
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "cache_prompt": True,
    }).encode("utf-8")
    conn = _connect()
    try:
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            detail = resp.read().decode("utf-8", errors="replace")
            if resp.status == 400 and ("context" in detail.lower()
                                       or "exceed" in detail.lower()):
                raise ContextOverflow(detail)
            raise RuntimeError(f"llama-server HTTP {resp.status}: {detail[:200]}")
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            if "error" in evt:
                msg = json.dumps(evt["error"])
                if "context" in msg.lower() or "exceed" in msg.lower():
                    raise ContextOverflow(msg)
                raise RuntimeError(f"llama-server error: {msg[:200]}")
            for choice in evt.get("choices") or []:
                text = (choice.get("delta") or {}).get("content") or ""
                if text:
                    yield (text, False)
        yield ("", True)
    finally:
        conn.close()


def upstream_healthy() -> bool:
    try:
        conn = _connect()
        conn.timeout = 2
        conn.request("GET", "/health")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except OSError:
        return False


# ============================================================================
# Sentence-level dedup — unchanged from genie_server.py
# ============================================================================
#
# Kept so both boards behave the same to clients. Larger models loop far less
# than Llama 3.2 1B on Genie did, but the filter is cheap and JSON control
# commands ({"action":...}) have no sentence boundaries, so they flow through
# untouched into the final flush.

import re as _re
_SENT_BOUNDARY = _re.compile(r"([.!?])(\s+|$)")


def _normalize_sentence(s: str) -> str:
    """Lowercase, strip punctuation + collapse whitespace for dedup comparison."""
    s = _re.sub(r"[^\w\s]", "", s).lower()
    s = _re.sub(r"\s+", " ", s).strip()
    return s


def _dedup_sentences(source):
    """Wrap a (text, is_done) generator, dropping repeated sentences."""
    seen = set()
    buf = ""
    for text, done in source:
        if text:
            buf += text
        while True:
            m = _SENT_BOUNDARY.search(buf)
            if not m:
                break
            end = m.end()
            sentence = buf[:end]
            buf = buf[end:]
            norm = _normalize_sentence(sentence)
            if len(norm) < 12 or norm not in seen:
                if len(norm) >= 12:
                    seen.add(norm)
                yield (sentence, False)
        if done:
            if buf:
                norm = _normalize_sentence(buf)
                if len(norm) < 12 or norm not in seen:
                    yield (buf, False)
                buf = ""
            yield ("", True)
            return


def run_stream(messages: list, max_tokens: int):
    yield from _dedup_sentences(_run_llama_stream(messages, max_tokens))


def run_unary(messages: list, max_tokens: int) -> tuple[str, float]:
    start = time.monotonic()
    parts = []
    for text, _done in run_stream(messages, max_tokens):
        if text:
            parts.append(text)
    return "".join(parts).strip(), time.monotonic() - start


# ============================================================================
# HTTP server — same request/response shapes as genie_server.py
# ============================================================================

class BridgeHandler(BaseHTTPRequestHandler):
    """Handle Ollama-compatible /api/generate and /api/chat requests."""

    def do_POST(self):
        if self.path == "/api/generate":
            self._handle_generate()
        elif self.path == "/api/chat":
            self._handle_chat()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == "/":
            healthy = upstream_healthy()
            payload = json.dumps({
                "status": "ok" if healthy else "starting",
                "backend": "llama-server",
            }).encode()
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    @staticmethod
    def _max_tokens(body):
        # num_thread and keep_alive are Ollama/Genie knobs with no meaning here.
        opts = body.get("options") or {}
        try:
            return int(opts.get("num_predict") or DEFAULT_MAX_TOKENS)
        except (TypeError, ValueError):
            return DEFAULT_MAX_TOKENS

    def _handle_generate(self):
        body = self._read_body()
        user_prompt = body.get("prompt", "")
        if not user_prompt:
            self.send_error(400, "missing prompt")
            return
        messages = build_messages(body.get("system", ""),
                                  [{"role": "user", "content": user_prompt}])
        self._respond(messages, body)

    def _handle_chat(self):
        body = self._read_body()
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            self.send_error(400, "missing messages")
            return
        self._respond(build_messages(body.get("system", ""), messages), body)

    def _respond(self, messages, body):
        if body.get("stream"):
            self._stream_generate(messages, self._max_tokens(body))
        else:
            self._unary_generate(messages, self._max_tokens(body))

    def _unary_generate(self, messages, max_tokens):
        try:
            response_text, duration = run_unary(messages, max_tokens)
        except ContextOverflow as e:
            print(f"[llm-bridge] context overflow: {e}")
            response_text, duration = _TOO_LONG, 0.0
        except (RuntimeError, OSError) as e:
            print(f"[llm-bridge] generate failed: {e}")
            response_text, duration = "", 0.0
        if not response_text:
            response_text = _NO_RESPONSE

        reply = {
            "model": MODEL_LABEL,
            "response": response_text,
            # web_chat.py's voice path reads message.content first
            "message": {"role": "assistant", "content": response_text},
            "done": True,
            "total_duration": int(duration * 1e9),
        }
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_line(self, obj):
        self.wfile.write((json.dumps(obj) + "\n").encode())
        self.wfile.flush()

    def _stream_generate(self, messages, max_tokens):
        """Stream tokens as NDJSON, one JSON object per line (Ollama-compatible)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        start = time.monotonic()
        try:
            try:
                for text, done in run_stream(messages, max_tokens):
                    if done:
                        break
                    if text:
                        self._write_line({"model": MODEL_LABEL,
                                          "response": text, "done": False})
            except (ContextOverflow, RuntimeError, OSError) as e:
                # Emit a spoken-friendly line so the client gets a structured
                # signal instead of a silent socket close.
                print(f"[llm-bridge] stream failed: {e}")
                msg = _TOO_LONG if isinstance(e, ContextOverflow) else _NO_RESPONSE
                self._write_line({"model": MODEL_LABEL, "response": msg,
                                  "done": False})
            self._write_line({
                "model": MODEL_LABEL,
                "response": "",
                "done": True,
                "total_duration": int((time.monotonic() - start) * 1e9),
            })
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-stream — drop the stream.
            return

    def log_message(self, format, *args):
        print(f"[llm-bridge] {args[0]}")


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ReusableHTTPServer((HOST, PORT), BridgeHandler)
    print(f"LLM bridge listening on {HOST}:{PORT} -> {LLAMA_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    print("Bridge stopped.")


if __name__ == "__main__":
    main()
