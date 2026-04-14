#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine — First-login wizard
# Runs on the first interactive SSH/console login as the trailcurrent user.
# Forces a password change then optionally configures MQTT.
# Idempotent — safe to re-run if the user deletes
# the completion flag.
# ============================================================================

set -uo pipefail

PEREGRINE_HOME="/home/trailcurrent"
ENV_FILE="${PEREGRINE_HOME}/assistant.env"
DONE_FLAG="${PEREGRINE_HOME}/.peregrine-setup-complete"

GREEN='\033[38;5;70m'
TEAL='\033[38;5;30m'
GRAY='\033[38;5;245m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

banner() {
    echo ""
    echo -e "${BOLD}${GREEN}============================================${RESET}"
    echo -e "${BOLD}  ${GREEN}Trail${TEAL}Current${RESET} ${BOLD}Peregrine${RESET} — ${1}"
    echo -e "${BOLD}${GREEN}============================================${RESET}"
    echo ""
}

section() {
    echo ""
    echo -e "${BOLD}${TEAL}── ${1} ──${RESET}"
}

ok()   { echo -e "  ${GREEN}\xE2\x9C\x93${RESET} $*"; }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; }
err()  { echo -e "  ${RED}\xE2\x9C\x97${RESET} $*"; }

# ── Detect first-run vs re-run ──────────────────────────────────────────────
if [[ -f "$DONE_FLAG" ]]; then
    banner "Setup (re-run)"
    echo "  Setup has already completed once on this board."
    echo "  This wizard is safe to re-run; existing config will be preserved."
    read -rp "  Continue? [y/N]: " yn
    [[ "${yn,,}" == "y" || "${yn,,}" == "yes" ]] || exit 0
else
    banner "First-time setup"
fi

# ── Step 1: Force password change ──────────────────────────────────────────
section "Change password"

if [[ ! -f "$DONE_FLAG" ]]; then
    echo "  This board ships with the default password 'trailcurrent'."
    echo "  You must change it before continuing."
    echo ""
    while true; do
        if passwd; then
            ok "Password changed"
            break
        else
            warn "Password change failed — try again"
        fi
    done
else
    read -rp "  Change password now? [y/N]: " yn
    if [[ "${yn,,}" == "y" || "${yn,,}" == "yes" ]]; then
        passwd && ok "Password changed"
    fi
fi

# ── Step 2: MQTT configuration ─────────────────────────────────────────────
section "MQTT broker (optional)"

read -rp "  Configure MQTT now? [Y/n]: " yn
if [[ "${yn,,}" != "n" && "${yn,,}" != "no" ]]; then
    read -rp "  Broker hostname or IP: " MQTT_BROKER
    if [[ -n "$MQTT_BROKER" ]]; then
        read -rp "  Port [8883]: " MQTT_PORT
        MQTT_PORT="${MQTT_PORT:-8883}"

        read -rp "  Use TLS? [Y/n]: " MQTT_TLS_YN
        MQTT_TLS_YN="${MQTT_TLS_YN:-y}"

        read -rp "  Username (blank to skip): " MQTT_USER
        read -rsp "  Password (blank to skip): " MQTT_PASS
        echo ""

        {
            echo "# Voice assistant environment config"
            echo "# Edit with: nano ~/assistant.env"
            echo "# Then restart: sudo systemctl restart voice-assistant"
            echo ""
            echo "MQTT_BROKER=${MQTT_BROKER}"
            echo "MQTT_PORT=${MQTT_PORT}"
            if [[ "${MQTT_TLS_YN,,}" == "y" || "${MQTT_TLS_YN,,}" == "yes" ]]; then
                echo "MQTT_USE_TLS=true"
                echo "MQTT_CA_CERT=${PEREGRINE_HOME}/ca.pem"
            fi
            [[ -n "$MQTT_USER" ]] && echo "MQTT_USERNAME=${MQTT_USER}"
            [[ -n "$MQTT_PASS" ]] && echo "MQTT_PASSWORD=${MQTT_PASS}"
            echo ""
            echo "# Audio tuning (optional)"
            echo "#WAKE_THRESHOLD=0.5"
            echo "#SILENCE_THRESHOLD=500"
            echo "#SILENCE_DURATION=1.5"
        } > "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        ok "Wrote ${ENV_FILE}"

        if [[ "${MQTT_TLS_YN,,}" == "y" || "${MQTT_TLS_YN,,}" == "yes" ]]; then
            echo ""
            echo "  TLS is enabled. Copy your CA certificate to the board with:"
            echo "    scp ca.pem trailcurrent@$(hostname):${PEREGRINE_HOME}/ca.pem"
        fi
    else
        warn "Skipped MQTT (no broker specified)"
    fi
else
    ok "Skipped MQTT"
fi

# ── Step 3: Restart services and wrap up ───────────────────────────────────
section "Starting voice assistant"

sudo systemctl restart genie-server voice-assistant 2>/dev/null || \
    warn "Could not restart services (try: sudo systemctl restart voice-assistant)"
ok "Services restarted"

touch "$DONE_FLAG"

banner "Setup complete"
echo "  Quick reference:"
echo "    Logs:      sudo journalctl -u voice-assistant -f"
echo "    Restart:   sudo systemctl restart voice-assistant"
echo "    Edit cfg:  nano ~/assistant.env"
echo "    Self-test: peregrine-self-test"
echo ""
echo -e "  Say ${BOLD}\"hey peregrine\"${RESET} to wake the assistant."
echo ""
