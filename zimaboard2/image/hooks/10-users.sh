#!/usr/bin/env bash
# Hook 10: trailcurrent (interactive, sudo; same model as the Q6A) and the
# locked peregrine-llm system user that runs llama-server and the bridge.
set -euo pipefail
R="$1"
echo "[hook 10] users"

GROUPS_TC=audio,video,render,sudo,plugdev,systemd-journal,adm
if ! chroot "$R" id trailcurrent >/dev/null 2>&1; then
    chroot "$R" useradd -m -s /bin/bash -G "$GROUPS_TC" trailcurrent
else
    chroot "$R" usermod -aG "$GROUPS_TC" trailcurrent
fi
echo "trailcurrent:trailcurrent" | chroot "$R" chpasswd
# Password last-change = today so PAM does not force a change at login; the
# first-login wizard forces it instead.
chroot "$R" chage -d "$(date +%Y-%m-%d)" trailcurrent
chroot "$R" passwd -l root

if ! chroot "$R" id peregrine-llm >/dev/null 2>&1; then
    chroot "$R" useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --user-group --groups render,video peregrine-llm
fi
