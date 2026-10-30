#!/usr/bin/env bash
# Hook 99: final checkpoint — every artefact present, every unit enabled
set -uo pipefail
R="$1"
echo "[hook 99] CHECKPOINT — final artifact verification"

FAIL=0
check()   { if [[ -e "$R$1" ]]; then echo "  ✓ $1"; else echo "  ✗ MISSING: $1"; FAIL=$((FAIL+1)); fi; }
check_x() { if [[ -x "$R$1" ]]; then echo "  ✓ $1 (x)"; else echo "  ✗ NOT EXECUTABLE: $1"; FAIL=$((FAIL+1)); fi; }

for f in assistant.py tts.py stt.py web_chat.py assistant.env models/hey_peregrine.onnx \
         "piper-voices/$PIPER_VOICE.onnx" "piper-voices/$PIPER_VOICE.onnx.json"; do
    check "/home/trailcurrent/$f"
done
check_x /home/trailcurrent/assistant-env/bin/python3
check   /home/trailcurrent/.cache/huggingface/hub/models--${WHISPER_REPO/\//--}/refs/main
check_x /opt/llama.cpp/current/bin/llama-server
check_x /opt/llama.cpp/current/bin/llama-bench
check   /opt/peregrine/llm_bridge.py
check   /etc/peregrine/llm.env
check   /etc/peregrine/power.env
check_x /usr/local/bin/peregrine-model
check_x /usr/local/bin/peregrine-self-test.sh
check_x /usr/local/bin/peregrine-first-login.sh
check_x /usr/local/sbin/peregrine-firstboot.sh
check_x /usr/local/sbin/peregrine-gen-certs.sh
check_x /usr/local/sbin/peregrine-power
check_x /usr/local/sbin/peregrine-llm-gate
check_x /usr/local/libexec/peregrine-llm-warmup
check   /etc/asound.conf
check   /etc/netplan/01-peregrine.yaml
check   /etc/peregrine-release
check   /etc/profile.d/first-login-hook.sh
check   /usr/share/plymouth/themes/trailcurrent/trailcurrent.plymouth
check   /usr/lib/shim/shimx64.efi.signed.latest
check   /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed
ls "$R"/boot/vmlinuz-* >/dev/null 2>&1 && echo "  ✓ kernel" || { echo "  ✗ no kernel"; FAIL=$((FAIL+1)); }
ls "$R"/boot/initrd.img-* >/dev/null 2>&1 && echo "  ✓ initrd" || { echo "  ✗ no initrd"; FAIL=$((FAIL+1)); }

for svc in peregrine-llm peregrine-llm-bridge voice-assistant peregrine-chat \
           peregrine-power peregrine-llm-gate.socket peregrine-firstboot ssh \
           avahi-daemon systemd-networkd; do
    if chroot "$R" systemctl is-enabled "$svc" >/dev/null 2>&1; then
        echo "  ✓ $svc enabled"
    else
        echo "  ✗ NOT ENABLED: $svc"; FAIL=$((FAIL+1))
    fi
done
for unit in genie-server cpu-performance power-save-hw; do
    if [[ -e "$R/etc/systemd/system/$unit.service" ]]; then
        echo "  ✗ Q6A-only unit present: $unit"; FAIL=$((FAIL+1))
    fi
done
if [[ "$(readlink "$R/etc/systemd/system/ssh.socket")" == /dev/null ]]; then
    echo "  ✓ ssh.socket masked"
else
    echo "  ✗ ssh.socket not masked"; FAIL=$((FAIL+1))
fi
chroot "$R" visudo -c >/dev/null || { echo "  ✗ sudoers invalid"; FAIL=$((FAIL+1)); }
chroot "$R" python3 -m py_compile /opt/peregrine/llm_bridge.py || FAIL=$((FAIL+1))

if (( FAIL > 0 )); then
    echo "  ✗✗✗ Final checkpoint FAILED ($FAIL)"
    exit 1
fi
echo "  ✓ All artifacts present — rootfs is ready ($(du -sh --exclude=proc --exclude=sys --exclude=dev "$R" 2>/dev/null | cut -f1))"
