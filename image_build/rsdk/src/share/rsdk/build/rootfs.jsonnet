// ============================================================================
// TrailCurrent Peregrine — Radxa Dragon Q6A rootfs.jsonnet
//
// Builds a minimal Ubuntu Noble (24.04) image with the Peregrine voice
// assistant fully installed: NPU LLM model, Piper TTS, openWakeWord, ALSA,
// systemd services, branding, and the trailcurrent default user.
//
// All Peregrine-specific files are staged into $PEREGRINE_STAGING by
// build.sh before rsdk-build runs. The customize-hooks below copy them
// from there into the chroot rootfs.
//
// Each customize-hook is a separate entry so a failure logs the exact
// hook that failed — critical for the long build/flash cycle.
// ============================================================================

local distro = import "mod/distro.libjsonnet";
local additional_repos = import "mod/additional_repos.libjsonnet";
local packages = import "mod/packages.libjsonnet";
local cleanup = import "mod/cleanup.libjsonnet";

function(
    architecture = "arm64",
    mode = "root",
    rootfs = "rootfs.tar",
    variant = "apt",

    temp_dir,
    output_dir,
    rsdk_rev = "",

    distro_mirror = "",
    snapshot_timestamp = "",

    radxa_mirror = "",
    radxa_repo_suffix = "",

    product,
    suite,
    edition,
    build_date,

    vendor_packages = true,
    linux_override = "",
    firmware_override = "",
    install_vscodium = false,
    use_pkgs_json = true,
) distro(suite, distro_mirror, architecture, snapshot_timestamp)
+ additional_repos(suite, radxa_mirror, radxa_repo_suffix, product, temp_dir, install_vscodium, use_pkgs_json)
+ packages(suite, edition, product, temp_dir, vendor_packages, linux_override, firmware_override)
+ cleanup()
+ {
    mmdebstrap+: {
        architectures: [
            architecture
        ],
        keyrings: [
            "%(temp_dir)s/keyrings/" % { temp_dir: temp_dir },
        ],
        mode: mode,
        target: rootfs,
        variant: variant,
        hostname: "peregrine",
        packages+:
        [
            // ── Core system tooling ──
            "ca-certificates",
            "curl",
            "wget",
            "gnupg",
            "lsb-release",
            "apt-transport-https",
            "sudo",
            "openssh-server",
            "avahi-daemon",
            "avahi-utils",
            "libnss-mdns",
            "rfkill",
            "cloud-guest-utils",
            "parted",
            "nvme-cli",
            "htop",
            "nano",
            "less",

            // ── Python runtime ──
            "python3",
            "python3-venv",
            "python3-pip",
            "python3-dev",
            "build-essential",

            // ── Audio (ALSA only — PulseAudio is masked) ──
            "alsa-utils",
            "ffmpeg",
            "libsndfile1",
            "libasound2-dev",

            // ── Boot splash ──
            "plymouth",
            "plymouth-themes",
            "initramfs-tools",
        ],
        "customize-hooks"+:
        [
            // ════════════════════════════════════════════════════════════════
            // Hook 0: rsdk standard hooks (hostname, fingerprint, initramfs)
            // ════════════════════════════════════════════════════════════════
            'echo "127.0.1.1\tperegrine" >> "$1/etc/hosts"',
            'cp "%(output_dir)s/config.yaml" "$1/etc/rsdk/"' % { output_dir: output_dir },
            'echo "FINGERPRINT_VERSION=\'2\'" > "$1/etc/radxa_image_fingerprint"',
            'echo "RSDK_BUILD_DATE=\'$(date -R)\'" >> "$1/etc/radxa_image_fingerprint"',
            'echo "RSDK_REVISION=\'%(rsdk_rev)s\'" >> "$1/etc/radxa_image_fingerprint"' % { rsdk_rev: rsdk_rev },
            'echo "RSDK_CONFIG=\'/etc/rsdk/config.yaml\'" >> "$1/etc/radxa_image_fingerprint"',
            'chroot "$1" sh -c "SYSTEMD_RELAX_ESP_CHECKS=1 update-initramfs -c -k all"',
            'chroot "$1" sh -c "u-boot-update"',
            |||
                cp -aR "$1/boot/efi" "$1/boot/efi2"
                chmod 0755 "$1/boot/efi2"
                umount "$1/boot/efi"
                rmdir "$1/boot/efi"
                mv "$1/boot/efi2" "$1/boot/efi"
            |||,
            |||
                mkdir -p "%(output_dir)s/seed"
                cp "$1/etc/radxa_image_fingerprint" "%(output_dir)s/seed"
                cp "$1/etc/rsdk/"* "%(output_dir)s/seed"
                tar Jvcf "%(output_dir)s/seed.tar.xz" -C "%(output_dir)s/seed" .
                rm -rf "%(output_dir)s/seed"
            ||| % { output_dir: output_dir },

            // ════════════════════════════════════════════════════════════════
            // Hook 1: Set hostname
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 1] hostname"
                echo "peregrine" > "$1/etc/hostname"
                grep -q "127.0.1.1.*peregrine" "$1/etc/hosts" || \
                    echo "127.0.1.1   peregrine" >> "$1/etc/hosts"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 2: Add Radxa QCS6490 apt repo (for NPU packages)
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 2] adding Radxa QCS6490 apt repo"
                chroot "$1" sh -c "curl -s https://radxa-repo.github.io/qcs6490-noble/install.sh | sh"
                chroot "$1" apt-get update
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 3: Install NPU packages (FastRPC + libcdsprpc)
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 3] installing NPU packages"
                chroot "$1" apt-get install -y --no-install-recommends \
                    fastrpc libcdsprpc1
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 3b: Remove rsetup-config-first-boot
            //
            // This Radxa package (pulled in by core.libjsonnet) installs
            // /config/before.txt and runs rsetup.service on first boot.
            // Its before.txt calls disable_service ssh, then only re-enables
            // SSH if the board appears headless (DRM has no connected display).
            // On the Q6A, DRM connectors may report "connected" even with no
            // physical display — so SSH would never be re-enabled.
            //
            // We manage everything rsetup-config-first-boot does ourselves:
            // SSH setup (hook 15), host key regen (hook 31), partition
            // expansion (peregrine-firstboot.sh). Remove it before it runs.
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 3b] removing rsetup-config-first-boot (Peregrine manages first-boot itself)"
                chroot "$1" apt-get remove -y --purge rsetup-config-first-boot 2>/dev/null || true
                echo "  rsetup-config-first-boot removed — rsetup.service will not override SSH"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 4: Create trailcurrent user (default password: trailcurrent)
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 4] creating trailcurrent user"
                if ! chroot "$1" id trailcurrent >/dev/null 2>&1; then
                    chroot "$1" useradd -m -s /bin/bash -G audio,render,sudo,plugdev,systemd-journal,adm trailcurrent
                else
                    chroot "$1" usermod -aG audio,render,sudo,plugdev,systemd-journal,adm trailcurrent
                fi
                echo "trailcurrent:trailcurrent" | chroot "$1" chpasswd
                # Set password last-change to today so PAM does not force a change on first login
                chroot "$1" chage -d "$(date +%Y-%m-%d)" trailcurrent
                # Disable root login entirely (sudo only via trailcurrent)
                chroot "$1" passwd -l root || true
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 5: Stage NPU LLM model from build host cache
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 5] staging NPU LLM model"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                NPU_SRC="$STAGING/cache/npu-model"
                if [ ! -d "$NPU_SRC" ]; then
                    echo "ERROR: NPU model cache missing at $NPU_SRC"
                    echo "Run: ./image_build/preflight.sh --download-cache"
                    exit 1
                fi
                mkdir -p "$1/home/trailcurrent/Llama3.2-1B-1024-v68"
                cp -a "$NPU_SRC"/. "$1/home/trailcurrent/Llama3.2-1B-1024-v68/"
                chmod +x "$1/home/trailcurrent/Llama3.2-1B-1024-v68/genie-t2t-run" 2>/dev/null || true
                echo "  copied $(du -sh "$1/home/trailcurrent/Llama3.2-1B-1024-v68" | cut -f1)"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 6: Stage Piper TTS voice from build host cache
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 6] staging Piper TTS voice"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                PIPER_SRC="$STAGING/cache/piper-voice"
                if [ ! -d "$PIPER_SRC" ]; then
                    echo "ERROR: Piper voice cache missing at $PIPER_SRC"
                    exit 1
                fi
                mkdir -p "$1/home/trailcurrent/piper-voices"
                cp -a "$PIPER_SRC"/. "$1/home/trailcurrent/piper-voices/"
                ls "$1/home/trailcurrent/piper-voices/"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 7: Stage hey_peregrine wake-word model
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 7] staging wake-word model"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                MODELS_SRC="$STAGING/models"
                if [ ! -f "$MODELS_SRC/hey_peregrine.onnx" ]; then
                    echo "ERROR: hey_peregrine.onnx missing at $MODELS_SRC"
                    exit 1
                fi
                mkdir -p "$1/home/trailcurrent/models"
                cp "$MODELS_SRC/hey_peregrine.onnx" "$1/home/trailcurrent/models/"
                cp "$MODELS_SRC/hey_peregrine.onnx.data" "$1/home/trailcurrent/models/" 2>/dev/null || true
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 8: Create venv + install Python packages
            //   (most likely failure point — slow but well-isolated)
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 8] creating venv and installing Python packages"
                chroot "$1" python3 -m venv /home/trailcurrent/assistant-env
                chroot "$1" /home/trailcurrent/assistant-env/bin/pip install \
                    "pip==26.0.1" "wheel==0.46.3" "setuptools==82.0.1"
                chroot "$1" /home/trailcurrent/assistant-env/bin/pip install \
                    "faster-whisper==1.2.1" \
                    "piper-tts==1.4.2" \
                    "paho-mqtt==2.1.0" \
                    "numpy==2.4.4" \
                    "scipy==1.17.1" \
                    "pathvalidate==3.3.1" \
                    "requests==2.33.1" \
                    "timezonefinder==8.2.2"
                # openwakeword installed separately with --no-deps
                # (tflite-runtime has no aarch64 wheel; we only use ONNX inference)
                chroot "$1" /home/trailcurrent/assistant-env/bin/pip install \
                    --force-reinstall --no-deps "openwakeword==0.6.0"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 9: Download openWakeWord resource ONNX models
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 9] downloading openWakeWord resource models"
                chroot "$1" /home/trailcurrent/assistant-env/bin/python3 -c 'import openwakeword; openwakeword.utils.download_models(); print("  resource models downloaded")'
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 10: VENV SMOKE TEST (fail-fast checkpoint #1)
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 10] CHECKPOINT — venv smoke test"
                chroot "$1" /home/trailcurrent/assistant-env/bin/python3 -c 'from openwakeword.model import Model; m = Model(); del m; print("  openwakeword OK")'
                chroot "$1" /home/trailcurrent/assistant-env/bin/python3 -c 'from faster_whisper import WhisperModel; print("  faster_whisper import OK")'
                chroot "$1" /home/trailcurrent/assistant-env/bin/python3 -c 'import paho.mqtt.client, numpy, scipy, timezonefinder; print("  all dependencies importable")'
                echo "  venv smoke test passed"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 11: Stage application code
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 11] staging application code"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 644 "$STAGING/src/assistant.py" "$1/home/trailcurrent/assistant.py"
                install -m 644 "$STAGING/src/tts.py"       "$1/home/trailcurrent/tts.py"
                install -m 644 "$STAGING/src/genie_server.py" "$1/home/trailcurrent/genie_server.py"
                # On-disk Piper TTS cache dir (populated on first run)
                install -d -o trailcurrent -g trailcurrent -m 755 \
                    "$1/home/trailcurrent/.cache/peregrine-tts"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 12: Stage default assistant.env
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 12] staging default assistant.env"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 600 "$STAGING/files/env/assistant.env.example" \
                    "$1/home/trailcurrent/assistant.env"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 13: Install systemd unit files
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 13] installing systemd unit files"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 644 "$STAGING/config/voice-assistant.service" \
                    "$1/etc/systemd/system/voice-assistant.service"
                install -m 644 "$STAGING/config/genie-server.service" \
                    "$1/etc/systemd/system/genie-server.service"
                install -m 644 "$STAGING/files/systemd/cpu-performance.service" \
                    "$1/etc/systemd/system/cpu-performance.service"
                install -m 644 "$STAGING/files/systemd/power-save-hw.service" \
                    "$1/etc/systemd/system/power-save-hw.service"
                install -m 644 "$STAGING/files/systemd/peregrine-firstboot.service" \
                    "$1/etc/systemd/system/peregrine-firstboot.service"
                mkdir -p "$1/etc/systemd/system.conf.d"
                install -m 644 "$STAGING/files/systemd/system.conf.d/timeout.conf" \
                    "$1/etc/systemd/system.conf.d/timeout.conf"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 14: Install scripts to /usr/local/{bin,sbin}
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 14] installing scripts"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 755 "$STAGING/files/scripts/peregrine-firstboot.sh" \
                    "$1/usr/local/sbin/peregrine-firstboot.sh"
                install -m 755 "$STAGING/files/scripts/peregrine-first-login.sh" \
                    "$1/usr/local/bin/peregrine-first-login.sh"
                install -m 755 "$STAGING/files/scripts/peregrine-self-test.sh" \
                    "$1/usr/local/bin/peregrine-self-test.sh"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 15: Enable services
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 15] enabling services"
                # Fix Ubuntu 24.04 socket activation so ssh.service owns port 22.
                #
                # Problem: openssh-server postinst runs `deb-systemd-helper enable
                # ssh.socket`, which creates:
                #   /etc/systemd/system/ssh.service.requires/ssh.socket
                # (because ssh.socket's [Install] has RequiredBy=ssh.service)
                #
                # `systemctl mask` creates /etc/systemd/system/ssh.socket -> /dev/null
                # but does NOT remove the .requires/ symlink. At boot, systemd sees
                # ssh.service Requires=ssh.socket (via .requires/), finds ssh.socket
                # masked, and refuses to start ssh.service entirely.
                #
                # Fix: disable (removes .wants/.requires symlinks) → mask (blocks
                # re-enable) → rm belt-and-suspenders in case disable missed it.
                chroot "$1" systemctl disable ssh.socket 2>/dev/null || true
                chroot "$1" systemctl mask ssh.socket 2>/dev/null || true
                rm -f "$1/etc/systemd/system/ssh.service.requires/ssh.socket"
                chroot "$1" systemctl enable \
                    genie-server.service \
                    voice-assistant.service \
                    cpu-performance.service \
                    power-save-hw.service \
                    peregrine-firstboot.service \
                    avahi-daemon.service \
                    ssh.service
                chroot "$1" systemctl set-default multi-user.target
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 16: Install Plymouth theme
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 16] installing Plymouth theme"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                THEME_DIR="$1/usr/share/plymouth/themes/trailcurrent"
                mkdir -p "$THEME_DIR"
                cp "$STAGING/files/plymouth/trailcurrent.plymouth" "$THEME_DIR/"
                cp "$STAGING/files/plymouth/trailcurrent.script" "$THEME_DIR/"
                cp "$STAGING/files/plymouth/logo.png" "$THEME_DIR/"
                cp "$STAGING/files/plymouth/background.png" "$THEME_DIR/"
                # plymouth-set-default-theme was removed in Ubuntu Noble.
                # Use update-alternatives to register and select the theme.
                chroot "$1" update-alternatives --install \
                    /usr/share/plymouth/themes/default.plymouth \
                    default.plymouth \
                    /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth 200
                chroot "$1" update-alternatives --set default.plymouth \
                    /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth
                chroot "$1" update-initramfs -u -k all 2>/dev/null || \
                    echo "  WARNING: update-initramfs failed (non-fatal under qemu)"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 17: Install MOTD + console issue
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 17] installing MOTD and console issue"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                rm -f "$1"/etc/update-motd.d/*
                install -m 755 "$STAGING/files/motd/10-trailcurrent" \
                    "$1/etc/update-motd.d/10-trailcurrent"
                install -m 644 "$STAGING/files/motd/issue-trailcurrent" "$1/etc/issue"
                install -m 644 "$STAGING/files/motd/issue-trailcurrent" "$1/etc/issue.net"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 18: Install profile.d shell branding
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 18] installing branded shell prompt"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 644 "$STAGING/files/profile/trailcurrent-prompt.sh" \
                    "$1/etc/profile.d/trailcurrent-prompt.sh"
                install -m 644 "$STAGING/files/profile/first-login-hook.bash" \
                    "$1/etc/profile.d/first-login-hook.sh"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 19: Install sysctl tuning
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 19] installing sysctl tuning"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 644 "$STAGING/files/sysctl/90-peregrine.conf" \
                    "$1/etc/sysctl.d/90-peregrine.conf"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 20: Disable WiFi (kernel module blacklist)
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 20] disabling WiFi"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 644 "$STAGING/files/modprobe/disable-wifi.conf" \
                    "$1/etc/modprobe.d/disable-wifi.conf"
                chroot "$1" systemctl mask wpa_supplicant.service 2>/dev/null || true
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 21: Disable PulseAudio/PipeWire
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 21] disabling PulseAudio/PipeWire"
                mkdir -p "$1/etc/pulse"
                echo "autospawn = no" > "$1/etc/pulse/client.conf"
                mkdir -p "$1/home/trailcurrent/.config/pulse"
                echo "autospawn = no" > "$1/home/trailcurrent/.config/pulse/client.conf"
                for svc in pulseaudio pipewire pipewire-pulse wireplumber; do
                    chroot "$1" systemctl --global disable "$svc.service" 2>/dev/null || true
                    chroot "$1" systemctl --global disable "$svc.socket" 2>/dev/null || true
                done
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 22: Mask unnecessary services
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 22] masking unnecessary services"
                MASK="snapd snapd.socket snapd.seeded.service \
                      cups cups-browsed bluetooth ModemManager fwupd packagekit \
                      accounts-daemon colord switcheroo-control power-profiles-daemon \
                      udisks2 NetworkManager-wait-online unattended-upgrades \
                      systemd-networkd-wait-online \
                      samba-ad-dc \
                      apt-daily.timer apt-daily-upgrade.timer motd-news.timer \
                      man-db.timer e2scrub_all.timer fstrim.timer \
                      systemd-sysupdate.timer systemd-sysupdate-reboot.timer \
                      whoopsie apport \
                      gdm3 gdm lightdm sddm"
                for svc in $MASK; do
                    chroot "$1" systemctl mask "$svc" 2>/dev/null || true
                done
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 23: Install SSH config drop-in
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 23] installing SSH config"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                mkdir -p "$1/etc/ssh/sshd_config.d"
                install -m 644 "$STAGING/files/ssh/sshd_config.d/10-trailcurrent.conf" \
                    "$1/etc/ssh/sshd_config.d/10-trailcurrent.conf"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 24: First-login wizard auto-runs on first interactive SSH session
            // Installed via /etc/profile.d/first-login-hook.sh (hook 18).
            // Guards: interactive terminal check ([ -t 0 ]) and
            // ~/.peregrine-setup-complete sentinel — runs exactly once.
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 24] first-login wizard installed via /etc/profile.d/first-login-hook.sh"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 25: Validate sudoers
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 25] validating /etc/sudoers"
                chroot "$1" visudo -c
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 26: Write /etc/peregrine-release
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 26] writing /etc/peregrine-release"
                {
                    echo "PEREGRINE_VERSION=\"${PEREGRINE_VERSION:-dev}\""
                    echo "PEREGRINE_BUILD_DATE=\"$(date -R)\""
                    echo "PEREGRINE_BUILD_HOST=\"$(hostname)\""
                } > "$1/etc/peregrine-release"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 27: Fix ownership of /home/trailcurrent
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 27] fixing ownership of /home/trailcurrent"
                chroot "$1" chown -R trailcurrent:trailcurrent /home/trailcurrent
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 28: Install ALSA default device config
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 28] installing ALSA default device config"
                STAGING="${PEREGRINE_STAGING:-/tmp/peregrine-staging}"
                install -m 644 "$STAGING/files/alsa/asound.conf" \
                    "$1/etc/asound.conf"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 29: Add usbcore.autosuspend=-1 to kernel cmdline
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 29] adding usbcore.autosuspend=-1 to ALL boot configs"
                # /etc/kernel/cmdline — source of truth for kernel-install
                CMDLINE="$1/etc/kernel/cmdline"
                if [ -f "$CMDLINE" ] && ! grep -q "usbcore.autosuspend" "$CMDLINE"; then
                    sed -i 's/$/ usbcore.autosuspend=-1/' "$CMDLINE"
                    echo "  patched /etc/kernel/cmdline"
                fi
                # extlinux.conf — U-Boot fallback
                EXTLINUX="$1/boot/extlinux/extlinux.conf"
                if [ -f "$EXTLINUX" ] && ! grep -q "usbcore.autosuspend" "$EXTLINUX"; then
                    sed -i '/^[[:space:]]*append/ s/$/ usbcore.autosuspend=-1/' "$EXTLINUX"
                    echo "  patched extlinux.conf"
                fi
                # systemd-boot entries — THIS is what the Q6A actually boots from
                # (EDK2 UEFI -> systemd-boot -> loader entry -> kernel)
                for entry in "$1"/boot/efi/loader/entries/*.conf; do
                    [ -f "$entry" ] || continue
                    if ! grep -q "usbcore.autosuspend" "$entry"; then
                        sed -i '/^options / s/$/ usbcore.autosuspend=-1/' "$entry"
                        echo "  patched $(basename "$entry")"
                    fi
                done
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 30: Golden image cleanup
            //
            // NOTE: do NOT wipe /tmp — bdebstrap needs /tmp/bdebstrap-output/.
            // NOTE: do NOT delete SSH host keys — sshd won't start without them
            //   and the firstboot reboot cycle is fragile. Pre-generated keys in
            //   the image are unique per-build (generated inside the chroot).
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 30] golden image cleanup"
                : > "$1/etc/machine-id"
                rm -f "$1/var/lib/dbus/machine-id"
                # SSH host keys: KEEP them. They were generated during package
                # install inside THIS chroot — unique per build. Deleting them
                # means sshd won't start until firstboot regenerates + reboots,
                # and that reboot cycle proved fragile. Pre-generated keys are
                # safe: each reflash gets a fresh build's keys.
                chroot "$1" apt-get clean
                rm -rf "$1"/var/lib/apt/lists/*
                rm -rf "$1"/root/.cache/pip
                rm -rf "$1"/home/trailcurrent/.cache/pip
                find "$1"/var/log -type f -name '*.log' -delete 2>/dev/null || true
                : > "$1"/root/.bash_history 2>/dev/null || true
                : > "$1"/home/trailcurrent/.bash_history 2>/dev/null || true
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 31: Verify SSH will actually start
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 31] verifying SSH readiness"
                # Check host keys exist
                KEY_COUNT=$(ls "$1"/etc/ssh/ssh_host_*_key 2>/dev/null | wc -l)
                if [ "$KEY_COUNT" -ge 3 ]; then
                    echo "  $KEY_COUNT SSH host key(s) present"
                else
                    echo "  WARNING: only $KEY_COUNT host keys — generating fresh ones"
                    for type in rsa ecdsa ed25519; do
                        chroot "$1" ssh-keygen -t "$type" \
                            -f "/etc/ssh/ssh_host_${type}_key" -N "" -q
                    done
                fi
                # Check sshd can parse its config
                chroot "$1" sshd -t 2>&1 || {
                    echo "  WARNING: sshd -t failed — check sshd_config"
                    echo "  Attempting to fix by removing drop-ins..."
                    rm -f "$1"/etc/ssh/sshd_config.d/*.conf
                    chroot "$1" sshd -t 2>&1 || echo "  sshd -t still failing"
                }
                # Verify ssh.service is enabled
                if chroot "$1" systemctl is-enabled ssh.service >/dev/null 2>&1; then
                    echo "  ssh.service enabled"
                else
                    echo "  WARNING: ssh.service not enabled — enabling now"
                    chroot "$1" systemctl enable ssh.service
                fi
                echo "  SSH readiness verified"
            |||,

            // ════════════════════════════════════════════════════════════════
            // Hook 32: FINAL CHECKPOINT (fail-fast checkpoint #2)
            // ════════════════════════════════════════════════════════════════
            |||
                set -e
                echo "[hook 32] CHECKPOINT — final artifact verification"
                FAIL=0
                check() {
                    if [ -e "$1$2" ]; then
                        echo "  ✓ $2"
                    else
                        echo "  ✗ MISSING: $2"
                        FAIL=$((FAIL+1))
                    fi
                }
                check_x() {
                    if [ -x "$1$2" ]; then
                        echo "  ✓ $2 (executable)"
                    else
                        echo "  ✗ NOT EXECUTABLE OR MISSING: $2"
                        FAIL=$((FAIL+1))
                    fi
                }
                check "$1" /home/trailcurrent/assistant.py
                check "$1" /home/trailcurrent/tts.py
                check "$1" /home/trailcurrent/genie_server.py
                check "$1" /home/trailcurrent/assistant.env
                check "$1" /home/trailcurrent/models/hey_peregrine.onnx
                check_x "$1" /home/trailcurrent/Llama3.2-1B-1024-v68/genie-t2t-run
                check "$1" /home/trailcurrent/piper-voices/en_US-libritts_r-medium.onnx
                check_x "$1" /home/trailcurrent/assistant-env/bin/python3
                check "$1" /etc/systemd/system/voice-assistant.service
                check "$1" /etc/systemd/system/genie-server.service
                check "$1" /etc/systemd/system/peregrine-firstboot.service
                check_x "$1" /usr/local/sbin/peregrine-firstboot.sh
                check_x "$1" /usr/local/bin/peregrine-first-login.sh
                check_x "$1" /usr/local/bin/peregrine-self-test.sh
                check "$1" /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth
                check "$1" /etc/peregrine-release
                check "$1" /etc/profile.d/first-login-hook.sh
                check "$1" /etc/modprobe.d/disable-wifi.conf
                check "$1" /etc/sysctl.d/90-peregrine.conf
                check "$1" /etc/asound.conf
                check "$1" /etc/ssh/ssh_host_ed25519_key
                # Verify ssh.socket is properly neutralized
                if [ -L "$1/etc/systemd/system/ssh.socket" ] && \
                   [ "$(readlink "$1/etc/systemd/system/ssh.socket")" = "/dev/null" ]; then
                    echo "  ✓ ssh.socket masked → /dev/null"
                else
                    echo "  ✗ ssh.socket NOT masked (SSH will fail at boot)"
                    FAIL=$((FAIL+1))
                fi
                if [ ! -e "$1/etc/systemd/system/ssh.service.requires/ssh.socket" ]; then
                    echo "  ✓ ssh.service.requires/ssh.socket absent"
                else
                    echo "  ✗ ssh.service.requires/ssh.socket EXISTS (SSH will fail at boot)"
                    FAIL=$((FAIL+1))
                fi
                # Verify rsetup-config-first-boot is gone
                if ! chroot "$1" dpkg -l rsetup-config-first-boot >/dev/null 2>&1; then
                    echo "  ✓ rsetup-config-first-boot not installed"
                else
                    echo "  ✗ rsetup-config-first-boot still installed (will override SSH on first boot)"
                    FAIL=$((FAIL+1))
                fi
                for svc in genie-server voice-assistant cpu-performance power-save-hw peregrine-firstboot ssh; do
                    if chroot "$1" systemctl is-enabled "$svc" >/dev/null 2>&1; then
                        echo "  ✓ $svc enabled"
                    else
                        echo "  ✗ NOT ENABLED: $svc"
                        FAIL=$((FAIL+1))
                    fi
                done
                if [ "$FAIL" -gt 0 ]; then
                    echo ""
                    echo "  ✗✗✗ Final checkpoint FAILED with $FAIL missing artifacts"
                    exit 1
                fi
                echo "  ✓ All artifacts present — image is ready"
            |||,
        ]
    },
    metadata: {
        architecture: architecture,
        mode: mode,
        rootfs: rootfs,
        variant: variant,

        temp_dir: temp_dir,
        output_dir: output_dir,
        rsdk_rev: rsdk_rev,

        distro_mirror: distro_mirror,

        radxa_mirror: radxa_mirror,
        radxa_repo_suffix: radxa_repo_suffix,

        product: product,
        suite: suite,
        edition: edition,
        build_date: build_date,

        vendor_packages: vendor_packages,
        linux_override: linux_override,
        firmware_override: firmware_override,
        install_vscodium: install_vscodium,
        use_pkgs_json: use_pkgs_json,
        sdboot: std.extVar("sdboot"),
    },
}
