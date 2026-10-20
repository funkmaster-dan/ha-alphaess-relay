# Changelog

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
