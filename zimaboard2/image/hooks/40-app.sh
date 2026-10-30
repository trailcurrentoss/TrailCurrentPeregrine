#!/usr/bin/env bash
# Hook 40: application code, llama.cpp, LLM bridge, config files, tools
set -euo pipefail
R="$1"; S="$PEREGRINE_STAGING"
echo "[hook 40] application"

H="$R/home/trailcurrent"
# Shared src/ — the same files the Q6A runs. genie_server.py is Q6A-only.
for f in assistant.py tts.py stt.py web_chat.py; do
    install -m 644 "$S/src/$f" "$H/$f"
done
install -d -m 755 "$H/.cache/peregrine-tts"

# Site config: the Q6A template, never overwritten once on the board.
install -m 600 "$S/q6a-files/env/assistant.env.example" "$H/assistant.env"

# llama.cpp, relocatable tree with a "current" symlink (plan §2.3)
install -d "$R/opt/llama.cpp"
cp -a "$S/llama.cpp" "$R/opt/llama.cpp/$LLAMA_TAG"
chown -R root:root "$R/opt/llama.cpp/$LLAMA_TAG"
ln -sfn "$LLAMA_TAG" "$R/opt/llama.cpp/current"
chroot "$R" /opt/llama.cpp/current/bin/llama-server --version

# Genie-compatible bridge — ZimaBoard only, outside /home so the locked
# peregrine-llm user can run it with ProtectHome=yes.
install -d "$R/opt/peregrine"
install -m 644 "$S/zb2-src/llm_bridge.py" "$R/opt/peregrine/llm_bridge.py"

# /etc/peregrine: llm.env (active model) and power.env (tunables)
install -d -m 755 "$R/etc/peregrine"
sed "s|^LLM_MODEL=.*|LLM_MODEL=/var/lib/peregrine/llm/models/$LLM_MODEL_FILE|" \
    "$S/config/llm.env.example" > "$R/etc/peregrine/llm.env"
chmod 644 "$R/etc/peregrine/llm.env"
install -m 644 "$S/config/llm.env.example" "$R/etc/peregrine/llm.env.example"
cat > "$R/etc/peregrine/power.env" <<'EOF'
# TrailCurrent Peregrine (ZimaBoard 2) — power tunables (peregrine-power.service)
# CPU energy/performance preference: balance_performance (default) or
# balance_power. Plan step 7 keeps balance_power only if fast-path p95 holds.
CPU_EPP=balance_performance
# iGPU max frequency in MHz. Empty = uncapped. Plan step 3 picks the value.
GPU_MAX_FREQ_MHZ=
EOF

# Licence sidecar for the baked-in model, read by `peregrine-model list`
echo "$LLM_MODEL_LICENSE" > "$R/var/lib/peregrine/llm/models/${LLM_MODEL_FILE%.gguf}.license"

install -m 755 "$S/tools/peregrine-model" "$R/usr/local/bin/peregrine-model"
