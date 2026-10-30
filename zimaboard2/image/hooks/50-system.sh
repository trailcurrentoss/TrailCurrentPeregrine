#!/usr/bin/env bash
# Hook 50: systemd units, scripts, system config, service enable/mask
set -euo pipefail
R="$1"; S="$PEREGRINE_STAGING"
Q="$S/q6a-files"
echo "[hook 50] system"

U="$R/etc/systemd/system"
for unit in peregrine-llm.service peregrine-llm-bridge.service \
            voice-assistant.service peregrine-chat.service \
            peregrine-power.service peregrine-llm-gate.socket \
            peregrine-llm-gate@.service; do
    install -m 644 "$S/config/$unit" "$U/$unit"
done
# First boot (machine-id, grow root, per-board web CA) — same unit as the Q6A
install -m 644 "$Q/systemd/peregrine-firstboot.service" "$U/peregrine-firstboot.service"
install -d "$R/etc/systemd/system.conf.d" "$R/etc/systemd/timesyncd.conf.d"
install -m 644 "$Q/systemd/system.conf.d/timeout.conf" "$R/etc/systemd/system.conf.d/timeout.conf"
install -m 644 "$Q/systemd/timesyncd.conf.d/10-trailcurrent.conf" "$R/etc/systemd/timesyncd.conf.d/10-trailcurrent.conf"

# Scripts
install -m 755 "$S/files/scripts/peregrine-firstboot.sh"  "$R/usr/local/sbin/peregrine-firstboot.sh"
install -m 755 "$S/files/scripts/peregrine-first-login.sh" "$R/usr/local/bin/peregrine-first-login.sh"
install -m 755 "$S/files/scripts/peregrine-self-test.sh"  "$R/usr/local/bin/peregrine-self-test.sh"
install -m 755 "$S/files/scripts/peregrine-power.sh"      "$R/usr/local/sbin/peregrine-power"
install -m 755 "$S/files/scripts/peregrine-llm-gate.sh"   "$R/usr/local/sbin/peregrine-llm-gate"
install -d "$R/usr/local/libexec"
install -m 755 "$S/files/scripts/peregrine-llm-warmup.sh" "$R/usr/local/libexec/peregrine-llm-warmup"
install -m 755 "$S/scripts/generate-certs.sh"             "$R/usr/local/sbin/peregrine-gen-certs.sh"

# System config
install -m 644 "$S/config/alsa/asound.conf" "$R/etc/asound.conf"
install -m 644 "$S/config/modprobe/blacklist-audio.conf" "$R/etc/modprobe.d/peregrine-blacklist-audio.conf"
install -m 644 "$Q/sysctl/90-peregrine.conf" "$R/etc/sysctl.d/90-peregrine.conf"
install -d "$R/etc/ssh/sshd_config.d"
install -m 644 "$Q/ssh/sshd_config.d/10-trailcurrent.conf" "$R/etc/ssh/sshd_config.d/10-trailcurrent.conf"
install -d "$R/etc/default/grub.d"
install -m 644 "$S/files/grub/90-peregrine.cfg" "$R/etc/default/grub.d/90-peregrine.cfg"

# PulseAudio/PipeWire never run (ALSA only), as on the Q6A
install -d "$R/etc/pulse" "$R/home/trailcurrent/.config/pulse"
echo "autospawn = no" > "$R/etc/pulse/client.conf"
echo "autospawn = no" > "$R/home/trailcurrent/.config/pulse/client.conf"

# ssh.service owns port 22; socket activation off (see the Q6A hook 15 for why)
chroot "$R" systemctl disable ssh.socket 2>/dev/null || true
chroot "$R" systemctl mask ssh.socket
rm -f "$U/ssh.service.requires/ssh.socket"

chroot "$R" systemctl enable \
    peregrine-llm.service \
    peregrine-llm-bridge.service \
    voice-assistant.service \
    peregrine-chat.service \
    peregrine-power.service \
    peregrine-llm-gate.socket \
    peregrine-firstboot.service \
    avahi-daemon.service \
    systemd-timesyncd.service \
    ssh.service
chroot "$R" systemctl set-default multi-user.target

for svc in snapd snapd.socket snapd.seeded.service \
           cups cups-browsed bluetooth ModemManager fwupd packagekit \
           accounts-daemon colord switcheroo-control power-profiles-daemon \
           udisks2 NetworkManager-wait-online unattended-upgrades \
           systemd-networkd-wait-online \
           apt-daily.timer apt-daily-upgrade.timer motd-news.timer \
           man-db.timer e2scrub_all.timer \
           systemd-sysupdate.timer systemd-sysupdate-reboot.timer \
           whoopsie apport wpa_supplicant; do
    chroot "$R" systemctl mask "$svc" 2>/dev/null || true
done
