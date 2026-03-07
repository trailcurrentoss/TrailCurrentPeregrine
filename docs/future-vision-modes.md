# Future Vision & Operating Modes

## Overview

Peregrine can extend beyond voice assistant duties by leveraging the Radxa Dragon Q6A's compute capacity across different operating modes. Paired with an ESP32-CAM (~$5), the system can handle security monitoring, safety chain inspection, and proximity awareness — all without upgrading hardware.

## Hardware Evaluation Summary

We evaluated several boards for this project:

| Board | CPU | RAM | Price | Power | Verdict |
|---|---|---|---|---|---|
| **Radxa Dragon Q6A (RK3588S)** | 4x A76 + 4x A55 | 8 GB | $122 | 3–8W | **Winner** — best price/performance/power |
| Raspberry Pi 5 | 4x A76 | Up to 16 GB | $304+ | 5–12W | Fewer cores, higher price, NPU requires HAT |
| Luckfox Omni3576 (RK3576) | 4x A72 + 4x A53 | 4/8 GB | $170 | ~5–8W | Older/slower cores, costs more |
| NVIDIA Jetson Orin Nano Super | 6x A78AE | 8 GB | $360–587 | 7–25W | Excellent AI performance but too expensive and power-hungry for single-duty use |

The Radxa at $122 with 3–8W draw is the clear winner for a battery-conscious trailer application. The Jetson would only make sense as a multi-role AI compute hub, but the mode-based architecture below achieves the same goals on cheaper hardware.

## Operating Modes

### Storage Mode (trailer unoccupied, no tow vehicle)

**Goal**: Minimize power draw while detecting intruders.

- Radxa in deep sleep or powered off entirely
- ESP32-CAM running standalone with PIR sensor as trigger (~0.5W total)
- Voice assistant is OFF (ambient noise would cause false wake word triggers)
- On motion detect: ESP32 wakes Radxa via GPIO → run classification model → record → alert owner via cellular/Wi-Fi
- **Average power draw: under 1W**

### In Transit Mode (trailer being towed)

**Goal**: Monitor trailer safety and surroundings. Power is plentiful from tow vehicle alternator.

- Voice assistant is OFF (wind/road noise makes speech unusable)
- Human presence detection is OFF
- Radxa fully dedicated to vision tasks with full CPU/RAM available:
  - **Safety chain monitoring**: camera on hitch area, detect chain presence, proper cross pattern, dragging
  - **Proximity/lane awareness**: ultrasonic sonar sensors for surrounding vehicles and lane position
- Alert driver via TrailCurrent app if issues detected (dragging chain, unsafe lane change near trailer)
- **Power: not a concern** — tow vehicle supplies continuous 12V

### Parked/Occupied Mode (normal use)

**Goal**: Full voice assistant experience.

- Voice assistant fully active (wake word → STT → LLM → TTS)
- Presence detection OFF (occupant is already present)
- ESP32-CAM could optionally serve as interior security camera
- All Radxa resources dedicated to assistant responsiveness
- **Power: shore power or battery, ~3–8W**

## Tiered Detection Architecture (Storage Mode)

Inspired by Tesla Sentry Mode / Rivian Guard Mode. Auto manufacturers use tiered detection to minimize power — they do NOT run neural networks continuously.

1. **Tier 1 — Hardware motion detection** (~milliwatts): PIR sensor or ESP32-CAM's built-in frame differencing. No AI involved. Runs essentially for free.
2. **Tier 2 — Lightweight classification** (low power): When motion is detected, wake Radxa and run a small model to determine: person? animal? branch? False alarm?
3. **Tier 3 — Full recording + alerting** (burst power): Only when a real threat is classified — start recording video, run object tracking, push alert to owner's phone.

Average power stays near idle because tier 2/3 only activate on real events.

## Implementation Notes

- Mode switching is systemd service management — stop one set of services, start another
- Mode selection could be automatic (detect tow vehicle connection via 12V sense, detect shore power) or manual via TrailCurrent app
- Each mode gets full Radxa resources instead of splitting them across concurrent tasks
- ESP32-CAM communicates with Radxa via GPIO wake signal + UART or Wi-Fi for image transfer
- Total BOM for compute + vision: ~$130 (Radxa $122 + ESP32-CAM ~$5 + PIR ~$2)

## Bill of Materials (Vision Add-on)

| Component | Est. Cost | Purpose |
|---|---|---|
| ESP32-CAM | ~$5 | Low-power camera with Wi-Fi, deep sleep capable |
| PIR sensor (HC-SR501 or similar) | ~$2 | Hardware motion trigger, microamp standby |
| Ultrasonic sonar (HC-SR04 x2–4) | ~$4–8 | Proximity/lane awareness in transit |
| Camera module (hitch-facing) | ~$10–15 | Safety chain monitoring input |
| **Total add-on cost** | **~$20–30** | |
