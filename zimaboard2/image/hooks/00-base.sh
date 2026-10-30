#!/usr/bin/env bash
# Hook 00: hostname, locale, timezone, network
set -euo pipefail
R="$1"; S="$PEREGRINE_STAGING"
echo "[hook 00] base system"

echo peregrine > "$R/etc/hostname"
cat > "$R/etc/hosts" <<'EOF'
127.0.0.1   localhost
127.0.1.1   peregrine
::1         localhost ip6-localhost ip6-loopback
ff02::1     ip6-allnodes
ff02::2     ip6-allrouters
EOF

echo 'LANG=C.UTF-8' > "$R/etc/default/locale"
ln -sf /usr/share/zoneinfo/Etc/UTC "$R/etc/localtime"
echo Etc/UTC > "$R/etc/timezone"

install -d "$R/etc/netplan"
install -m 600 "$S/config/netplan/01-peregrine.yaml" "$R/etc/netplan/01-peregrine.yaml"
chroot "$R" systemctl enable systemd-networkd.service systemd-resolved.service
# /etc/resolv.conf stays the build host's copy until the end so later hooks
# can reach PyPI; hook 90 points it at systemd-resolved.
