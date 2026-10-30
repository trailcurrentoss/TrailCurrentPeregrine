#!/usr/bin/env bash
# Hook 20: Piper voice + hey_peregrine wake-word model. (The GGUF ships next
# to the rootfs on the ISO; the installer copies it.)
set -euo pipefail
R="$1"; S="$PEREGRINE_STAGING"
echo "[hook 20] voice + wake-word models"

H="$R/home/trailcurrent"
install -d "$H/piper-voices" "$H/models"
install -m 644 "$S/piper-voice/$PIPER_VOICE.onnx" "$S/piper-voice/$PIPER_VOICE.onnx.json" "$H/piper-voices/"
install -m 644 "$S/models/hey_peregrine.onnx" "$H/models/"
[[ -f "$S/models/hey_peregrine.onnx.data" ]] && install -m 644 "$S/models/hey_peregrine.onnx.data" "$H/models/"

# LLM model directory; the installer fills it.
install -d -m 755 "$R/var/lib/peregrine/llm/models"
