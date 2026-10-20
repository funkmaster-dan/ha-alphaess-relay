# Changelog

## 0.5.1

- Use the verified/cached inverter rating when the sparse detailed battery-limit report is not current. Current reported limits can reduce the request ceiling; zero blocks that direction. Fresh live state is always required.
- Correct commissioning availability without changing battery protections or assuming a universal 5000 W limit.

## 0.5.0

- Add device-confirmed Force charge and Force discharge switches, with separate wattage, target SoC and duration inputs.
- Add Start charge / Start discharge action forms supporting duration or absolute end time, plus a confirmed mode-2 stop action.
- Learn/cache inverter rating from Poinv and validate starts against fresh battery charge/discharge limits. No fixed 5,000 W ceiling.
- Validate 5–100% targets, round 0.4% protocol steps conservatively, and enforce native-compatible finite durations of 90 seconds to three hours.
- Correlate bounded command indices and require fresh state readback after the ACK. Serialize at complete frame boundaries, keep owned ACKs off the cloud stream, and do not retry uncertain controls automatically.
- Native tests verify production encoding, both directions, stop and expiry. Loopback/HA tests cover framing, ACK ownership, cloud conflict, settings, interlocks, deadlines and unavailable/invalid inputs.

## 0.4.0

- Enable local monitoring fallback by default when the cloud path fails; an advanced option can disable it.
- Retire failed cloud sessions before local replies; detect an unresponsive cloud and retry through fresh device connections every five minutes in local mode.
- Support observed login-first and telemetry-first paths with minimal monitoring ACKs. Withhold history/log ACKs; no controls, fabricated settings or cloud backfill.
- Add Local fallback active diagnostics and reuse existing measurement freshness and identity rules.
- Add isolated transport, ownership, recovery and HA tests plus private native EMS receiver validation. Prolonged physical WAN independence remains unverified.

## 0.3.0

- Add Instantaneous generation, Instantaneous home load and Instantaneous grid I/O total, all in W with HA power/measurement metadata.
- Generation includes the solar inverter's CT/meter reading and all four PV strings. Grid sums the three signed phases; home load uses generation + grid + battery power from one live report.
- Replace Battery power (raw) with Instantaneous battery I/O and a new entity ID; remove the obsolete raw-power entity from the registry.
- Missing or invalid inputs make only dependent power sensors unavailable, without fabricated zeroes or values retained from older reports.
- 34 HA integration tests cover power totals, signs, units, availability and entity replacement. The existing 63-test research/relay regression suite passes.

## 0.2.1

- Populate the advanced setup section with its listening address and port defaults when expanded in the live HA frontend.
- Verified on live Home Assistant 2026.10.2 through Playwright; regression coverage checks the section data sent to the frontend.
- Later live validation confirmed byte-exact physical pass-through and 21 real cloud telemetry acknowledgements; see `VALIDATION.md` for scope and remaining soak tests.

## 0.2.0

- One Home Assistant integration embeds TCP forwarding, telemetry decoding and sensors.
- Minimal serial-number setup with optional listening settings; cloud hostname handled internally.
- State of charge, raw signed battery power, last received and connection diagnostics.
- Monotonic freshness, DNS resolution on reconnect, clean unload/reload/shutdown.
- Read-only diagnostics; no command generation, local ACKs or outage responder.
- Tested on Home Assistant 2026.10.2 / Python 3.14: 26 integration tests, including the extracted release ZIP.
- Live router/battery pass-through validation is not yet complete. Read the installation and removal notes before changing routing.
