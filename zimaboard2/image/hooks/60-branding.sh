#!/usr/bin/env bash
# Hook 60: Plymouth theme, MOTD, console issue, shell prompt — shared
# TrailCurrent branding, with the ZimaBoard 2 board name.
set -euo pipefail
R="$1"; S="$PEREGRINE_STAGING"
Q="$S/q6a-files"
echo "[hook 60] branding"

T="$R/usr/share/plymouth/themes/trailcurrent"
install -d "$T"
install -m 644 "$Q/plymouth/trailcurrent.plymouth" "$Q/plymouth/trailcurrent.script" \
    "$Q/plymouth/logo.png" "$Q/plymouth/background.png" "$T/"
chroot "$R" update-alternatives --install /usr/share/plymouth/themes/default.plymouth \
    default.plymouth /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth 200
chroot "$R" update-alternatives --set default.plymouth \
    /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth

rm -f "$R"/etc/update-motd.d/*
install -m 755 "$S/files/motd/10-trailcurrent" "$R/etc/update-motd.d/10-trailcurrent"
install -m 644 "$S/files/motd/issue-trailcurrent" "$R/etc/issue"
install -m 644 "$S/files/motd/issue-trailcurrent" "$R/etc/issue.net"

install -m 644 "$S/files/profile/trailcurrent-prompt.sh" "$R/etc/profile.d/trailcurrent-prompt.sh"
install -m 644 "$Q/profile/first-login-hook.bash" "$R/etc/profile.d/first-login-hook.sh"

# initramfs carries the theme; MODULES=most (the default) so it boots on any
# x86 storage controller, eMMC and SATA included.
chroot "$R" update-initramfs -u -k all
