#!/usr/bin/env bash
# Hook 90: release file, ownership, golden-image cleanup
set -euo pipefail
R="$1"
echo "[hook 90] cleanup"

{
    echo "PEREGRINE_VERSION=\"$PEREGRINE_VERSION\""
    echo "PEREGRINE_BOARD=\"zimaboard2\""
    echo "PEREGRINE_BUILD_DATE=\"$(date -R)\""
    echo "PEREGRINE_BUILD_HOST=\"$PEREGRINE_BUILD_HOST\""
    echo "PEREGRINE_LLAMA_TAG=\"$LLAMA_TAG\""
    echo "PEREGRINE_LLM_MODEL=\"$LLM_MODEL_NAME\""
} > "$R/etc/peregrine-release"

chroot "$R" chown -R trailcurrent:trailcurrent /home/trailcurrent

# Per-board identity is minted later: machine-id on first boot, SSH host keys
# by the installer.
: > "$R/etc/machine-id"
rm -f "$R/var/lib/dbus/machine-id"
echo peregrine > "$R/etc/hostname"
ln -sf ../run/systemd/resolve/stub-resolv.conf "$R/etc/resolv.conf"

chroot "$R" apt-get clean
rm -rf "$R"/var/lib/apt/lists/* "$R"/root/.cache "$R"/home/trailcurrent/.cache/pip
find "$R/var/log" -type f \( -name '*.log' -o -name '*.gz' \) -delete 2>/dev/null || true
: > "$R/root/.bash_history"
