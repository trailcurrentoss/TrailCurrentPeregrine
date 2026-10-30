#!/usr/bin/env bash
# Hook 30: Python venv with the same pins as the Q6A image (hook 8 there),
# openWakeWord resource models, and the Whisper weights in the HF cache so
# HF_HUB_OFFLINE=1 works on first boot. Ends with a smoke test.
set -euo pipefail
R="$1"
echo "[hook 30] venv"

V=/home/trailcurrent/assistant-env
chroot "$R" python3 -m venv "$V"
chroot "$R" "$V/bin/pip" install -q --no-cache-dir \
    "pip==26.0.1" "wheel==0.46.3" "setuptools==82.0.1"
chroot "$R" "$V/bin/pip" install -q --no-cache-dir \
    "faster-whisper==1.2.1" \
    "piper-tts==1.4.2" \
    "paho-mqtt==2.1.0" \
    "numpy==2.4.4" \
    "scipy==1.17.1" \
    "pathvalidate==3.3.1" \
    "requests==2.33.1" \
    "timezonefinder==8.2.2" \
    "av==18.1.0"
# av is pinned because faster-whisper 1.2.1 asks only for av>=11 and calls
# av.open(..., metadata_errors=...), which av 19 removed: with av 19 every
# transcription raises TypeError and the voice loop never answers.
# openwakeword with --no-deps, as on the Q6A: only ONNX inference is used,
# and its tflite-runtime dependency is not needed. Its other runtime deps are
# pinned explicitly: openwakeword/__init__.py imports scikit-learn at load
# time, so the package cannot even be imported without it.
chroot "$R" "$V/bin/pip" install -q --no-cache-dir "scikit-learn==1.9.1" "tqdm==4.70.1"
chroot "$R" "$V/bin/pip" install -q --no-cache-dir --force-reinstall --no-deps "openwakeword==0.6.0"

echo "  openWakeWord resource models"
chroot "$R" "$V/bin/python3" -c 'import openwakeword; openwakeword.utils.download_models()'

echo "  Whisper $WHISPER_REPO @ $WHISPER_REVISION"
chroot "$R" env HF_HOME=/home/trailcurrent/.cache/huggingface \
    WHISPER_REPO="$WHISPER_REPO" WHISPER_REVISION="$WHISPER_REVISION" \
    "$V/bin/python3" - <<'EOF'
import os
from huggingface_hub import snapshot_download
repo, rev = os.environ["WHISPER_REPO"], os.environ["WHISPER_REVISION"]
path = snapshot_download(repo, revision=rev, allow_patterns=[
    "config.json", "preprocessor_config.json", "model.bin",
    "tokenizer.json", "vocabulary.*"])
# faster-whisper asks for revision "main"; offline mode resolves it through
# refs/main, which a download by commit hash does not write.
hub = os.path.join(os.environ["HF_HOME"], "hub", "models--" + repo.replace("/", "--"))
os.makedirs(os.path.join(hub, "refs"), exist_ok=True)
with open(os.path.join(hub, "refs", "main"), "w") as f:
    f.write(rev)
print("  ->", path)
EOF

echo "  smoke test"
chroot "$R" "$V/bin/python3" -c 'from openwakeword.model import Model; m = Model(); print("  openwakeword OK")'
chroot "$R" env HOME=/home/trailcurrent HF_HUB_OFFLINE=1 "$V/bin/python3" -c '
from faster_whisper import WhisperModel
WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=2)
print("  faster-whisper base.en loads offline")'
chroot "$R" "$V/bin/python3" -c 'import paho.mqtt.client, numpy, scipy, timezonefinder, piper; print("  all dependencies importable")'
