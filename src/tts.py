"""Fast Piper TTS pipeline for the Peregrine voice assistant.

Three optimizations over spawning `piper --output-raw` per utterance:

1. **Persistent in-process Piper** — PiperVoice.load() once at startup, reuse
   the ONNX session for every utterance. Eliminates ~1-2 s of model reload
   per call on the Q6A at 600 MHz.

2. **Streaming synthesis → aplay stdin** — audio chunks are piped directly
   into an aplay subprocess as each sentence is synthesized, so playback
   starts while later sentences are still being generated.

3. **On-disk WAV cache** — each utterance is persisted to
   ``~/.cache/peregrine-tts/<sha1>.wav`` on first render. Repeated phrases
   (every "Turning on ..." confirmation, every "I don't have X right now"
   fallback, every module description) replay in ~50-150 ms with no
   synthesis work at all.

The CLI path in assistant.speak() remains as a fallback if PiperVoice.load()
fails for any reason.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Iterable, Optional

try:
    from piper import PiperVoice
    _HAS_PIPER = True
except Exception:
    PiperVoice = None
    _HAS_PIPER = False


def _cache_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


class TTSEngine:
    """Persistent Piper voice with streaming playback and on-disk caching."""

    def __init__(
        self,
        model_path: str,
        cache_dir: Optional[str] = None,
        playback_device: Optional[str] = None,
    ):
        self.model_path = model_path
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self.playback_device = playback_device
        self._voice = None
        self._sample_rate = 22050
        self._sample_width = 2
        self._lock = threading.Lock()
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"  [tts] cache dir unavailable ({e}); caching disabled")
                self.cache_dir = None

    @property
    def available(self) -> bool:
        return self._voice is not None

    def load(self) -> bool:
        """Load the voice model. Returns True on success."""
        if self._voice is not None:
            return True
        if not _HAS_PIPER:
            return False
        try:
            t0 = time.monotonic()
            self._voice = PiperVoice.load(self.model_path)
            try:
                self._sample_rate = int(self._voice.config.sample_rate)
            except AttributeError:
                pass
            print(
                f"  [tts] loaded {os.path.basename(self.model_path)} in "
                f"{time.monotonic() - t0:.2f}s (sr={self._sample_rate})"
            )
            return True
        except Exception as e:
            print(f"  [tts] PiperVoice.load failed: {e}")
            self._voice = None
            return False

    # ---- cache ----

    def _cache_path(self, text: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (_cache_key(text) + ".wav")

    def is_cached(self, text: str) -> bool:
        p = self._cache_path(text)
        return p is not None and p.exists() and p.stat().st_size > 44

    def render_to_cache(self, text: str) -> Optional[Path]:
        """Synthesize `text` and persist it as a WAV in the cache.

        Returns the cached path, or None if disabled/failed. Safe to call on
        an already-cached phrase (returns the existing path).
        """
        path = self._cache_path(text)
        if path is None:
            return None
        if self.is_cached(text):
            return path
        if not self.load():
            return None
        tmp = path.with_suffix(".wav.tmp")
        try:
            with wave.open(str(tmp), "wb") as wf:
                self._voice.synthesize_wav(text, wf)
            tmp.replace(path)
            return path
        except Exception as e:
            print(f"  [tts] render failed for {text!r:.80}: {e}")
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
            return None

    def warm_cache(self, phrases: Iterable[str], background: bool = True) -> None:
        """Pre-render each phrase into the cache.

        When ``background=True`` (default) this runs on a daemon thread so it
        does not delay startup. Phrases are rendered one at a time; missing
        entries become cache-hits on subsequent boots.
        """
        if self.cache_dir is None:
            return
        missing = [p for p in phrases if p and not self.is_cached(p)]
        if not missing:
            return

        def _run() -> None:
            t0 = time.monotonic()
            rendered = 0
            for phrase in missing:
                if self.render_to_cache(phrase) is not None:
                    rendered += 1
            print(
                f"  [tts] warmed {rendered}/{len(missing)} cached phrases "
                f"in {time.monotonic() - t0:.1f}s"
            )

        if background:
            threading.Thread(target=_run, daemon=True, name="tts-warm").start()
        else:
            _run()

    # ---- playback ----

    def _aplay_raw_cmd(self) -> list:
        cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
               "-r", str(self._sample_rate)]
        if self.playback_device:
            cmd[1:1] = ["-D", self.playback_device]
        return cmd

    def _aplay_wav_cmd(self, path: str) -> list:
        cmd = ["aplay", "-q"]
        if self.playback_device:
            cmd += ["-D", self.playback_device]
        cmd.append(path)
        return cmd

    def _play_wav_file(self, path: str) -> None:
        try:
            subprocess.run(self._aplay_wav_cmd(path),
                           capture_output=True, timeout=30)
        except Exception as e:
            print(f"  [tts] aplay failed: {e}")

    def speak(self, text: str) -> float:
        """Synthesize (or replay cached) and play. Returns wall-clock seconds.

        Raises RuntimeError if PiperVoice is unavailable and no cached file
        matches — the caller can then fall back to the piper CLI.
        """
        text = text.strip()
        if not text:
            return 0.0
        t0 = time.monotonic()
        with self._lock:
            # Cache hit: just aplay the WAV — no synthesis.
            path = self._cache_path(text)
            if path is not None and path.exists() and path.stat().st_size > 44:
                self._play_wav_file(str(path))
                return time.monotonic() - t0
            # Stream synthesis into aplay stdin, collect bytes for the cache.
            if not self.load():
                raise RuntimeError("PiperVoice unavailable")
            self._stream_and_cache(text, path)
        return time.monotonic() - t0

    def _stream_and_cache(self, text: str, cache_path: Optional[Path]) -> None:
        proc = subprocess.Popen(
            self._aplay_raw_cmd(),
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        collected = bytearray() if cache_path is not None else None
        first_sr = None
        try:
            for chunk in self._voice.synthesize(text):
                pcm = chunk.audio_int16_bytes
                if not pcm:
                    continue
                if first_sr is None:
                    first_sr = chunk.sample_rate
                try:
                    proc.stdin.write(pcm)
                except (BrokenPipeError, OSError):
                    break
                if collected is not None:
                    collected.extend(pcm)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if cache_path is not None and collected and first_sr:
            tmp = cache_path.with_suffix(".wav.tmp")
            try:
                with wave.open(str(tmp), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(self._sample_width)
                    wf.setframerate(first_sr)
                    wf.writeframes(bytes(collected))
                tmp.replace(cache_path)
            except Exception as e:
                print(f"  [tts] cache write failed: {e}")
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
