# Validation — 20 October 2026

## 0.5.0 charge/discharge controls

87 HA/core cases pass locally. A new private native regression runs the actual
production bytes through pinned EMS instructions for charge/discharge at 250 W
and 5 kW, verifies the 16-character correlation index and status fields, confirms
same-family disable, and exercises the native 90-second expiry. No hardware
commands were used for those tests. Physical trial results will be recorded
separately after development-build validation.

The mode-2 encoding matches the manufacturer draft and the earlier independent
native Modbus/Wi-Fi state equivalence. An ACK alone is insufficient: switches
require fresh matching device state. Dynamic battery limits and a learned/cached
inverter rating gate starts. See [control scope](CONTROL.md).

## 0.4.0 standalone fallback

Fallback is enabled by default for new and existing entries. Healthy cloud
sessions forward unchanged. Failed/silent cloud sessions are retired before
local replies, and local sessions periodically reconnect to check recovery.
History/log ACKs and physical controls are excluded. See
[behavior and limitations](STANDALONE.md).

**65 HA/core tests pass from source and the extracted release ZIP**.
**64 private research/relay tests pass**, including
pinned native EMS instructions generating requests and consuming this responder's
replies. Seven native exchanges complete without configuration writes. Tests
cover refusal, established disconnect, silent cloud, local telemetry, withheld
history/log ACKs, fresh-session recovery and normal opaque forwarding.

These results do not establish autonomous firmware scheduling, physical cold
boot, long outages, licensing/time expiry or independence from TCP/51100.

## 0.3.0 power sensors

The added generation, home-load and total grid I/O sensors use watts, power
device class and measurement state class, matching the
[AlphaESS integration's sensor definitions](https://github.com/CharlesGillanders/homeassistant-alphaESS/blob/main/custom_components/alphaess/sensorlist.py).
Its [power parser](https://github.com/CharlesGillanders/homeassistant-alphaESS/blob/main/custom_components/alphaess/coordinator.py)
reads the cloud's `ppv`, `pload`, `pgrid` and `pbat` fields.

Passive wire reports and the owner's existing cloud integration gave an exact
comparison during battery discharge. The generation value includes the
external solar inverter's CT/meter (`PmeterDC`). Local totals are:

- Generation: `Ppv1 + Ppv2 + Ppv3 + Ppv4 + PmeterDC`.
- Grid I/O: `PmeterL1 + PmeterL2 + PmeterL3`, import positive, export negative.
- Home load: generation + grid I/O + `Pbat`, discharge positive, charge negative.

Each total requires all its inputs in the same CRC-valid live report. An
absent, invalid or stale input makes its dependent sensors unavailable. No
phase, string or meter is silently treated as zero. Native firmware deadbands
remain unchanged. Synthetic tests cover all nonzero strings/phases, import,
export, charge/discharge, missing/invalid data, units, freshness and replacing
the prototype battery entity with a new entity ID.

**34 tests pass** in HA 2026.10.2; the existing **63 research/relay regression
tests pass**. This extends the earlier pass-through evidence below; it does
not establish compatibility with other hardware/firmware or prolonged uptime.

## 0.2.1 initial live deployment

Version 0.2.1 has been installed through HACS and exercised on live Home
Assistant 2026.10.2 using Python Playwright and Chrome. The monitoring relay
remains enabled at the owner's request, using a static OpenWrt DNAT/SNAT include.
There is no ongoing router watchdog or separate relay service.

| Check | Result |
| --- | --- |
| Actual HA configuration flows, registries and entity lifecycle | 26 tests pass |
| Same tests importing the extracted release ZIP | 26 tests pass |
| GitHub Actions, v0.2.1 runtime commit | Pass |
| Live setup and options screens, including populated advanced defaults | Pass |
| Isolated synthetic LAN DNAT/SNAT path through live HA | Pass |
| Opaque bidirectional bytes, invalid CRC and wrong-serial gating | Pass |
| Valid telemetry push and 45-second stale expiry | Pass |
| Reload, reconnect, half-close and upstream failure cleanup | Pass |
| Physical battery → HA → real cloud byte preservation | Pass in short capture |
| Independent TCP/51100 session | Continued without SYN/reset in capture |

The five-minute physical capture contained 592 packets with zero kernel drops.
The relay's incoming and outgoing TCP streams matched byte for byte: **20,623
bytes upstream and 970 bytes downstream**, including **21 live reports and
their real cloud acknowledgements**. HA measurements matched the existing
AlphaESS integration. Raw captures and owner configuration remain private.
The relay generated no battery application messages or acknowledgements.

The original test timer removed the redirect before the owner's request to
keep it enabled reached the operator. The path was restored and telemetry
resumed. Permanent NAT chains then replaced the temporary test table without
resetting the active session. OpenWrt's `fw4 check` accepts the persisted include;
a router reboot was not performed. A complete removal/direct-path recovery is
not claimed as a completed validation.

Next is an extended unattended soak, followed by deliberate HA downtime and
WAN recovery tests. Long-duration stability, different firmware/hardware,
dynamic charging/discharging behavior and full operation without the real
cloud remain unverified. The existing design interrupts the redirected cloud
connection while HA is unavailable; it does not provide automatic bypass.

Published v0.2.1 ZIP SHA-256:
`8294370378465776865cb6cca8a43593edebad7385c1a939dab2dbb9a2a965f5`.
Its bundled README predates these live validation results; this page records
the later evidence without replacing the released artifact.
