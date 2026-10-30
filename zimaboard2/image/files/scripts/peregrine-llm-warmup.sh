#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — llama-server warm-up
#
# ExecStartPost of peregrine-llm.service. Waits for the model to load, then
# sends one tiny completion so the weights are resident and the Vulkan
# pipelines are compiled before voice-assistant starts. Fails the unit if the
# server never becomes healthy, so a broken model shows up as a failed start.
# ============================================================================

set -u

URL=http://127.0.0.1:8080
DEADLINE=$((SECONDS + 840))

until curl -sf -m 2 "$URL/health" >/dev/null; do
    if (( SECONDS > DEADLINE )); then
        echo "llama-server did not become healthy" >&2
        exit 1
    fi
    sleep 1
done

curl -sf -m 300 "$URL/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
    >/dev/null || echo "warm-up request failed (non-fatal)" >&2
exit 0
