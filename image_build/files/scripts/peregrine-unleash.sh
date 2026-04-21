#!/usr/bin/env bash
# ============================================================================
# peregrine-unleash.sh — Q6A "show me the headroom" diagnostic + boost
#
# The shipping image runs Peregrine inside a ≤5 W budget on the Radxa
# Dragon Q6A. That budget is enforced thermally and by whatever
# power-management policy the kernel/soc-shim applies at boot. This
# script:
#
#   1. Dumps the CURRENT governor, scaling freq ranges, measured freqs,
#      throttle counters, and thermal zone temps so you can see exactly
#      what is limiting compute.
#   2. Sets every cpufreq policy to 'performance' + raises scaling_max_freq
#      to cpuinfo_max_freq so nothing is artificially capped.
#   3. (Optional, with --thermal) raises the software trip points so the
#      kernel stops preemptively throttling. DO NOT run this in a closed
#      enclosure without airflow — at full A78 clocks Q6A will happily
#      climb well past the ≤5 W target.
#
# Intended for bench measurement only. Reboot restores normal policy.
# ============================================================================

set -euo pipefail

do_thermal=0
dry_run=0
while [ $# -gt 0 ]; do
    case "$1" in
        --thermal|-t) do_thermal=1 ;;
        --dry-run|-n) dry_run=1 ;;
        --help|-h)
            cat <<USAGE
Usage: $(basename "$0") [--thermal] [--dry-run]

  --thermal   Raise thermal-zone trip points so the kernel stops throttling.
              Bench-only; needs active cooling.
  --dry-run   Print current state but apply no changes.
USAGE
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2 ; exit 2 ;;
    esac
    shift
done

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "This script must run as root (sudo $0 ...)"
        exit 1
    fi
}

header() {
    printf '\n=== %s ===\n' "$*"
}

read_or_na() {
    local p="$1"
    if [ -r "$p" ]; then cat "$p"; else echo "(not readable)"; fi
}

dump_state() {
    header "CPU topology"
    lscpu | grep -E "Model name|Vendor|Architecture|CPU\(s\)|CPU max MHz|CPU min MHz|NUMA" || true

    header "Governors & scaling ranges (per-CPU)"
    for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
        n="$(basename "$cpu")"
        cf="$cpu/cpufreq"
        [ -d "$cf" ] || continue
        gov="$(read_or_na "$cf/scaling_governor")"
        cur="$(read_or_na "$cf/scaling_cur_freq")"
        smax="$(read_or_na "$cf/scaling_max_freq")"
        smin="$(read_or_na "$cf/scaling_min_freq")"
        hwmax="$(read_or_na "$cf/cpuinfo_max_freq")"
        hwmin="$(read_or_na "$cf/cpuinfo_min_freq")"
        printf '  %-8s gov=%-11s  cur=%8s  scaling=[%s..%s]  hw=[%s..%s]\n' \
            "$n" "$gov" "$cur" "$smin" "$smax" "$hwmin" "$hwmax"
    done

    header "Thermal zones"
    for tz in /sys/class/thermal/thermal_zone*; do
        [ -d "$tz" ] || continue
        t_mC="$(read_or_na "$tz/temp")"
        ttype="$(read_or_na "$tz/type")"
        policy="$(read_or_na "$tz/policy")"
        if [ "$t_mC" != "(not readable)" ]; then
            printf '  %-30s type=%-20s policy=%-10s %0.1f C\n' \
                "$(basename "$tz")" "$ttype" "$policy" "$(awk -v v="$t_mC" 'BEGIN{print v/1000}')"
        else
            printf '  %-30s (unreadable)\n' "$(basename "$tz")"
        fi
    done

    header "Throttle / boost counters"
    for f in /sys/devices/system/cpu/cpufreq/boost \
             /sys/devices/system/cpu/intel_pstate/no_turbo ; do
        [ -e "$f" ] && printf '  %s = %s\n' "$f" "$(cat "$f")"
    done
    # ARM big.LITTLE sometimes exposes 'throttle' counters via perf — skip.

    header "CPU frequency snapshot (1 s wall-clock, measured)"
    for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
        n="$(basename "$cpu")"
        f1="$cpu/cpufreq/scaling_cur_freq"
        [ -r "$f1" ] || continue
        a="$(cat "$f1")"
        sleep 0.25
        b="$(cat "$f1")"
        printf '  %-8s %7s kHz  (sample %s)\n' "$n" "$b" "$a"
    done
}

apply_boost() {
    header "Applying: governor=performance, scaling_max_freq=cpuinfo_max_freq"
    for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
        cf="$cpu/cpufreq"
        [ -d "$cf" ] || continue
        [ -w "$cf/scaling_governor" ] && echo performance > "$cf/scaling_governor" 2>/dev/null || true
        if [ -r "$cf/cpuinfo_max_freq" ] && [ -w "$cf/scaling_max_freq" ]; then
            cat "$cf/cpuinfo_max_freq" > "$cf/scaling_max_freq" 2>/dev/null || true
        fi
    done
    # cpufreq boost, if the kernel exposes it
    if [ -w /sys/devices/system/cpu/cpufreq/boost ]; then
        echo 1 > /sys/devices/system/cpu/cpufreq/boost 2>/dev/null || true
    fi
    echo "  Done."
}

disable_thermal_throttle() {
    header "Raising thermal trip points (BENCH ONLY)"
    echo "  WARNING: this disables preemptive thermal throttling."
    echo "  Ensure active cooling or an open enclosure before proceeding."
    for tz in /sys/class/thermal/thermal_zone*; do
        [ -d "$tz" ] || continue
        # Switch policy to user_space where supported (kernel stops acting)
        if [ -w "$tz/policy" ]; then
            echo user_space > "$tz/policy" 2>/dev/null || true
        fi
        # Raise every available trip point to 120 C
        for trip in "$tz"/trip_point_*_temp; do
            [ -w "$trip" ] || continue
            echo 120000 > "$trip" 2>/dev/null || true
        done
    done
    echo "  Done. Hardware thermal shutdown (PMIC / junction) still intact."
}

show_next_steps() {
    cat <<EOF

Next steps:

  # Benchmark Piper TTS latency with the persistent engine (ms per utterance)
  /home/trailcurrent/assistant-env/bin/python3 - <<'PY'
  import time, os
  from piper import PiperVoice
  v = PiperVoice.load(os.path.expanduser("~/piper-voices/en_US-libritts_r-medium.onnx"))
  for _ in range(2):
      t0 = time.monotonic()
      n = 0
      for c in v.synthesize("Turning on the kitchen lights."):
          n += len(c.audio_int16_bytes)
      print(f"  synthesis: {(time.monotonic()-t0)*1000:.0f} ms, {n} bytes")
  PY

  # Watch live frequency while the assistant speaks
  watch -n 0.2 'grep MHz /proc/cpuinfo | nl'

  # Tail the assistant log (look for "Spoken (cached) in X.XX s" lines)
  journalctl -u voice-assistant -f

Reboot to restore normal policy.
EOF
}

# ----------------------------------------------------------------------------

dump_state

if [ "$dry_run" -eq 1 ]; then
    echo ""
    echo "Dry run — no changes applied."
    exit 0
fi

need_root
apply_boost

if [ "$do_thermal" -eq 1 ]; then
    disable_thermal_throttle
fi

header "Post-apply state"
dump_state
show_next_steps
