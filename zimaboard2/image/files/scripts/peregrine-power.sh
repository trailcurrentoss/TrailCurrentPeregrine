#!/usr/bin/env bash
# ============================================================================
# TrailCurrent Peregrine (ZimaBoard 2) — power policy, run every boot
#
# Plan §4.1. Every setting is best-effort: a missing sysfs knob is skipped,
# never fatal. Tunables live in /etc/peregrine/power.env.
# ============================================================================

set -u

EPP="${CPU_EPP:-balance_performance}"
GPU_MAX="${GPU_MAX_FREQ_MHZ:-}"

log() { echo "[peregrine-power] $*"; }

# CPU: intel_pstate active + powersave (the only dynamic HWP governor). EPP
# sets how fast cores ramp when the wake word fires. Never "performance".
for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
    echo powersave > "$c/scaling_governor" 2>/dev/null
    echo "$EPP" > "$c/energy_performance_preference" 2>/dev/null
done
log "governor=powersave epp=$EPP"

# USB: autosuspend everything except audio (01) and HID (03) interfaces —
# the Jabra streams continuously and must never be suspended.
for dev in /sys/bus/usb/devices/*/power/control; do
    dp=$(dirname "$(dirname "$dev")")
    skip=false
    for ifc in "$dp"/*:*/bInterfaceClass; do
        [ -f "$ifc" ] || continue
        case "$(tr -d '[:space:]' < "$ifc")" in 01|03) skip=true; break;; esac
    done
    if $skip; then echo on > "$dev"; else echo auto > "$dev"; fi
done 2>/dev/null
log "usb autosuspend applied (audio/HID kept on)"

# SATA link power management.
for p in /sys/class/scsi_host/host*/link_power_management_policy; do
    echo med_power_with_dipm > "$p" 2>/dev/null
done

# iGPU frequency cap (step 3 of the plan picks the value). Empty = uncapped.
if [[ -n "$GPU_MAX" ]]; then
    for f in /sys/class/drm/card*/gt/gt0/rps_max_freq_mhz /sys/class/drm/card*/gt_max_freq_mhz; do
        [ -w "$f" ] && echo "$GPU_MAX" > "$f" 2>/dev/null && log "iGPU max $GPU_MAX MHz ($f)"
    done
fi

# Radios: the board is Ethernet-only.
rfkill block wifi 2>/dev/null
rfkill block bluetooth 2>/dev/null
exit 0
