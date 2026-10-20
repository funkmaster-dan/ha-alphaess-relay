# AlphaESS Relay for Home Assistant

Local battery monitoring and timed charge/discharge controls in Home Assistant.
The integration keeps the normal AlphaESS cloud connection and offers automatic
local fallback when the cloud is unavailable.

It runs inside Home Assistant: no separate server, broker or portal credentials.
**A one-time router redirect is required.**

## What you get

- Live state of charge, solar generation, home load, grid power and battery power.
- **Force charge** and **Force discharge** switches, each with power, target SoC
  and duration settings.
- Actions for starting a charge/discharge until a chosen end time.
- Connection, local-fallback and last-received diagnostics.

Power readings use watts. Positive grid power means import; negative means
export. Positive battery power means discharge; negative means charging.
Readings become unavailable on disconnect or after 45 seconds without fresh data.

## Before you start

You need Home Assistant, the battery's **system serial number**, and a router
that can redirect the battery's outbound TCP connection to Home Assistant.
Reserve the battery and Home Assistant IP addresses so the redirect stays valid.

This community integration has been tested with a **SMILE-G3-B5-INV** and Home
Assistant **2026.10.2**. Compatibility with other systems is not established.
It is not an official AlphaESS product.

## Install and configure

1. In **HACS → Custom repositories**, add
   `https://github.com/funkmaster-dan/ha-alphaess-relay` as an **Integration**.
2. Download **AlphaESS Relay** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → AlphaESS Relay**.
4. Enter the **battery system serial**, not the Wi-Fi dongle serial.
5. Configure the router redirect below. Readings appear when the battery connects.

For manual installation, download the [latest release ZIP](https://github.com/funkmaster-dan/ha-alphaess-relay/releases/latest),
copy its `custom_components/alphaess_relay` folder into Home Assistant's
`custom_components` directory, then restart and continue from step 3.

### Router redirect

The battery initiates the connection; entering its serial alone does not route
traffic to Home Assistant. Configure your router with these settings:

| Setting | Value |
| --- | --- |
| Match | Only the battery's reserved IPv4 address; optionally also its MAC |
| Traffic | Outbound TCP destination port **17777** |
| Redirect to | Home Assistant's LAN address, TCP **17778** |
| Same-subnet replies | Apply source NAT to the router's LAN address when battery and HA share a subnet |
| Leave unchanged | TCP **51100**, and Home Assistant's own outbound cloud connection |

These are LAN routing rules, **not Internet port forwarding**. Keep the listener
accessible only to the intended local network. For HA Container, also make the
listening port reachable through the container network. The integration does
not configure your router.

### Integration options

Open **AlphaESS Relay → Configure** to change these settings:

| Option | Default / purpose |
| --- | --- |
| Listening address and port | `0.0.0.0:17778`; must match the router redirect |
| Local reporting during cloud outages | Enabled |
| Inverter rating | Normally learned and cached from the battery; enter a verified rating only if it has not been received |

Listening options are also available under **Advanced relay settings** during
setup. Applying options restarts the connection and can briefly interrupt readings.

## Charge and discharge

Open the **AlphaESS device page** in Home Assistant.

1. Under **Configuration**, set the direction's **power**, **target SoC** and
   **duration**. Editing these values saves them without starting the battery.
2. Under **Controls**, turn on **Force charge** or **Force discharge**.
3. Turn the switch off to return to normal operation. The battery may still
   charge from solar or supply the home normally.

Only one forced direction can run at a time. Switch states follow confirmed
device reports; a command acknowledgement alone does not turn a switch on.

| Input | Allowed values |
| --- | --- |
| Power | At least 100 W, up to the verified inverter rating; current reported battery limits can reduce it |
| Target SoC | **5–100%**; above current SoC for charging, below it for discharging |
| Duration | **90 seconds to 3 hours** |

The power ceiling is device-derived, not fixed at 5,000 W. Until a current
battery-limit report arrives, the verified inverter rating bounds requests.
Device protections remain unchanged, and actual battery power can differ from
the requested setpoint.

For an absolute end time, use **AlphaESS Relay: Start charge / Start discharge**
in **Developer tools → Actions** or an automation. Supply power, target SoC and
**either duration or end time**. Omitted inputs use the saved settings. End times
without a time-zone offset use Home Assistant's configured time zone.

## Cloud outages and Home Assistant downtime

Local fallback is enabled by default. During a cloud failure, reporting and
explicit charge/discharge actions can run locally. The relay retries the cloud
through a new battery connection every five minutes; reconnection can cause gaps.
Locally acknowledged live reports are **not backfilled to the AlphaESS cloud**.
Prolonged physical WAN independence remains unverified.

**Home Assistant must stay running.** While it is stopped, the redirected battery
cloud connection is interrupted; there is no automatic router bypass. Before
removing the integration, disable the router redirect and allow the battery to
reconnect directly to the cloud. Existing connections may retain their old route
until they reconnect.

## Troubleshooting and further details

- **No readings:** check the serial, router redirect, listening port and container
  networking. Adding the integration does not discover or reroute the battery.
- **Controls unavailable:** wait for fresh status and check that an inverter
  rating has been learned or configured.
- **A control failed:** inspect its error and device state before trying again;
  uncertain commands are not automatically retried.

See [control behavior](https://github.com/funkmaster-dan/ha-alphaess-relay/blob/main/CONTROL.md),
[local fallback](https://github.com/funkmaster-dan/ha-alphaess-relay/blob/main/STANDALONE.md)
and [validation and compatibility limits](https://github.com/funkmaster-dan/ha-alphaess-relay/blob/main/VALIDATION.md)
for technical details and test evidence.
