# AlphaESS Relay for Home Assistant

One custom integration provides the TCP relay, local telemetry decoding and
Home Assistant sensors. It runs inside Home Assistant Core. It needs a single
static router redirect; there is no separate relay container, MQTT broker,
monitoring API or router watchdog to maintain.

This is a community prototype for the researched AlphaESS version-1 Wi-Fi
protocol. It is not an official AlphaESS integration. Its first release exposes
monitoring only and preserves the normal AlphaESS cloud connection. Explicit charge/discharge actions can send bounded local mode-2 Dispatch commands.
No firmware updates, calibration or installer/safety-setting changes are generated.
Cloud outage fallback is enabled by default and sends minimal local monitoring
ACKs only while the cloud path is unavailable.

## Status and compatibility

- Version 0.5.1; tested with Home Assistant 2026.10.2 and Python 3.14.4.
- Runtime files are self-contained under `custom_components/alphaess_relay/`.
- No runtime pip dependencies beyond Home Assistant itself.
- Offline tests, isolated synthetic peers on live HA, and a short physical
  battery pass-through have passed. An extended soak is still needed; see
  [validation evidence and limits](https://github.com/funkmaster-dan/ha-alphaess-relay/blob/main/VALIDATION.md).
- Repository: [funkmaster-dan/ha-alphaess-relay](https://github.com/funkmaster-dan/ha-alphaess-relay).
- Install through HACS as a custom repository of type **Integration**, or use
  the ZIP from GitHub Releases for a manual installation.

## Install the local package

1. Extract the ZIP. Copy its `custom_components/alphaess_relay` directory into
   your Home Assistant configuration directory's `custom_components` folder.
2. Restart Home Assistant so it discovers the custom integration.
3. Open **Settings → Devices & services → Add integration → AlphaESS Relay**.
4. Enter the **battery system serial**. Use the system serial shown in the
   AlphaESS app or on the battery, rather than the separate Wi-Fi stick serial.

No portal login, API credentials or upstream address is requested. The known
cloud endpoint `cloudiot.alphaess.com:17777` is handled internally. DNS is
resolved again for each new device connection; setup does not resolve it or
connect to the battery/cloud.

The collapsed **Advanced relay settings** section defaults to IPv4
`0.0.0.0`, TCP port `17778`. These settings can also be changed through the
integration's options. Applying changes reloads the relay and interrupts its
current TCP session. The same section includes **Use local reporting when the cloud is unavailable**,
enabled by default, including for existing entries after upgrade. You can disable
it in the integration options to retain forwarding-only behavior.

With no router redirect, the sensors initially show
unavailable and **Relay connected** is off; this is expected.

For HACS installation, add `https://github.com/funkmaster-dan/ha-alphaess-relay`
as a custom repository of type **Integration**, download it, then restart HA
and add the integration. HACS does not configure the router.

## Sensors

| Entity | Behavior |
| --- | --- |
| State of charge | Percentage from CRC-valid live command 10, matching the configured system serial |
| Instantaneous battery I/O | W; positive discharge, negative charging; replaces the prototype raw-power entity with a new entity ID |
| Instantaneous generation | W; total of all four PV strings plus the external solar CT/meter (`PmeterDC`) |
| Instantaneous home load | W; derived from generation + signed grid I/O + signed battery I/O in the same report |
| Instantaneous grid I/O total | W; sum of all three grid phases, positive import / negative export |
| Last received | Diagnostic timestamp of the last accepted live report; remains visible when telemetry becomes stale |
| Relay connected | Diagnostic binary sensor: an active device-to-cloud relay TCP session exists |
| Local fallback active | Diagnostic binary sensor: local monitoring replies are active without a cloud session |

SoC and power become unavailable on disconnect, after 45 seconds without a
valid live report, or until a new session supplies its own live telemetry.
Missing or invalid source fields make only dependent power sensors unavailable. Generation requires all four string values and the external meter value; grid requires all three phases. Missing fields are not assumed to be zero. Old readings are never replaced with invented
zeroes, and history is not mixed into live state. Freshness uses monotonic
receive time rather than the battery's clock.

Entities update from local push callbacks. A freshness timer only expires
cached state; it never polls the battery or cloud. HA's manual entity refresh
also reads only cached data. Downloadable diagnostics contain connection
status and counters, excluding serials and protocol payloads.

## The one-time router configuration

The battery opens an outbound main TCP connection; it does not offer a local
telemetry listener. A static router rule must redirect that connection to the
Home Assistant host and the integration's listening port.

The network configuration needs these values:

| Setting | Value |
| --- | --- |
| Source | Only the battery's reserved IPv4 address, optionally also its MAC |
| Traffic | Outbound TCP destination port 17777 |
| Redirect target | Home Assistant host IPv4 address and integration listening port, normally 17778 |
| Same-subnet return path | Source NAT to the router's LAN address when battery and HA share a subnet |
| E-Linter side channel | Leave TCP 51100 outside the redirect |

Matching this one device and port can avoid depending on a fixed cloud IP.
HA's own upstream connection must not match the battery-source rule. The
listening port belongs on the local network and should accept the intended
router/device path. The system serial filters published telemetry; it is not
TCP authentication. HA Container users must also make the listening port
reachable through their container network configuration.

**Validate the static redirect with a synthetic LAN client before directing
battery traffic through it.** No router configuration is changed by installing,
setting up, reloading or removing this integration.

## Home Assistant downtime and removal

While HA or this integration is stopped, its relay listener is unavailable and
the redirected battery cloud connection is interrupted. When HA returns, the
battery can reconnect. There is no automatic router bypass in this design.

Before removing the integration permanently, disable the static redirect and
restore the direct cloud path. Existing NAT mappings can outlive a rule change,
so the affected TCP session must also reconnect. This does not require a
battery application command. The complete rollback procedure still needs the
synthetic LAN routing validation mentioned above.

## Charge and discharge

Two **Force charge / Force discharge** switches use device readback for their
state. Each has power, target SoC and duration inputs on the device page. Changing
an input saves settings for the next explicit start; it sends no battery command.
Turning off returns supported mode-2 dispatch to normal self-consumption, so the
battery may still supply the home normally. The two forced directions are
mutually exclusive; switching directions confirms stop before starting the other.

For one action form, use **AlphaESS Relay: Start charge / Start discharge** in HA
Actions or an automation. Supply watts, target SoC and either duration or an
absolute end time. Omitted inputs use that direction's saved number values.
Target SoC is 5–100%, below current SoC for discharge and above it for charge.
Duration is 90 seconds to three hours; a shorter end-time request is rejected
rather than extended. Times without an offset use HA's configured time zone.

The wattage ceiling is learned from the device's `Poinv` rating (kW), cached
locally, and reduced at start by current `Pcharge` / `Pdischarge` battery limits
(W). There is no universal 5,000 W constant. Until a rating report arrives, a
verified rating can be entered in Advanced relay settings; starts remain
unavailable without a verified inverter rating and fresh status. If the sparse
battery-limit report is not current, the inverter rating bounds the request;
this does not bypass device-side battery protections. The initial requested power
is a conservative 500 W, configurable independently for each direction.

See [control behavior, safeguards and limitations](https://github.com/funkmaster-dan/ha-alphaess-relay/blob/main/CONTROL.md).

## Cloud outages and recovery

Normal cloud traffic passes unchanged. If connecting to the cloud fails, the
integration handles the device session locally. If an established cloud
connection fails or no cloud bytes arrive for 30 seconds after device data,
the relay closes that session and uses local mode on the next device connection.
Closing the old sockets first prevents late cloud ACKs from competing with
local replies. Reporting can pause during device reconnection.

Local mode acknowledges supported login, startup queries, heartbeats and
monitoring reports, using the same decoder and sensor freshness rules. Fallback itself does
not initiate battery controls or invent configuration, licensing or time values.
Explicit user charge/discharge actions remain available in local mode.
History and log ACKs are withheld, allowing live reports to continue without
retiring undelivered history. Live reports acknowledged locally are not uploaded
or backfilled into the vendor cloud.

Every five minutes of local operation the integration closes the device
connection so its next connection tries the cloud again. Recovery depends on
the device's own reconnect timing; it is not an uninterrupted stream. If the
cloud is still unavailable, local reporting resumes. No router changes or
separate service are needed. See [standalone behavior and validation limits](https://github.com/funkmaster-dan/ha-alphaess-relay/blob/main/STANDALONE.md).

## Limits

This version has no arbitrary message-send service, durable history store,
energy-counter normalization or automatic router management. Only bounded mode-2
charge/discharge is exposed; other dispatch families are not cancelled blindly. Native firmware and isolated loopback tests validate the fallback
paths. Sustained physical WAN loss, full cold boot, licensing expiry and loss of
the separate E-Linter channel remain unverified. This is an experimental outage
responder, not demonstrated complete vendor-cloud replacement.

## Build and test from the research workspace

The component's `_relay` directory is generated from the single canonical relay
core and verified codec, with a local import adjustment for packaging. It is
bundled code, not an additional installed application. Edit canonical sources
and rebuild instead of changing generated files by hand:

```bash
uv venv --python 3.14 .venv-ha
uv pip install --python .venv-ha/bin/python -r requirements-ha-test.txt
.venv-ha/bin/python tools/build_ha_package.py --check
.venv-ha/bin/python -m pytest -c tests_ha/pytest.ini tests_ha -v
.venv-ha/bin/python tools/build_ha_package.py
```

The tests use an isolated HA instance and block all TCP destinations except
literal loopback. The cloud hostname is mapped to synthetic loopback peers
inside tests; no real DNS query or AlphaESS connection is made. Tests cover
configuration, byte forwarding, sensor state, expiry, reconnect, DNS rotation,
failure recovery, reload, shutdown and diagnostics.
