#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — LLM yield gate
#
# Socket-activated (peregrine-llm-gate.socket, one instance per connection).
# Reads one verb from the socket:
#   freeze  pause llama-server (cgroup freezer) and arm a 5 s auto-thaw
#   thaw    cancel the auto-thaw and resume llama-server
# Anything else is ignored. Replies "ok" so the caller can wait on freeze.
# In-flight HTTP requests just stall while frozen; nothing is lost.
# ============================================================================

set -u

UNIT=peregrine-llm.service
TIMER=peregrine-llm-autothaw

read -r -t 2 verb || exit 0

case "$verb" in
    freeze)
        systemctl stop "$TIMER.timer" 2>/dev/null
        systemctl freeze "$UNIT" 2>/dev/null
        # Watchdog: a crashed voice loop must never leave the LLM frozen.
        # Timers default to AccuracySec=1min, so pin it or "5 s" can be 65.
        systemd-run --quiet --collect --unit="$TIMER" --on-active=5 \
            --timer-property=AccuracySec=100ms \
            /bin/systemctl thaw "$UNIT" 2>/dev/null
        ;;
    thaw)
        systemctl stop "$TIMER.timer" 2>/dev/null
        systemctl thaw "$UNIT" 2>/dev/null
        ;;
    *)
        exit 0
        ;;
esac
# A thaw is fire-and-forget: the caller has usually hung up already.
echo ok 2>/dev/null || true
