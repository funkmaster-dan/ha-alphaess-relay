# Standalone cloud-outage reporting

Version 0.4.0 enables cloud-outage fallback by default for new and existing HA
entries. The Advanced relay settings / integration options allow disabling it.
The architecture remains one HA integration and the existing static router route.

## Ownership and recovery

- Healthy cloud: opaque forwarding, including unknown and malformed bytes.
  No local application replies are generated.
- Cloud connection failure: use the accepted device session locally after the
  connection attempt fails (five-second connect timeout).
- Established cloud failure: retire both sockets and use local mode on the next
  device connection. A cloud EOF/reset/write failure or 30 seconds of silence
  after device data triggers recovery. Silence means no downstream bytes;
  it is not an assertion that individual application ACKs have been matched.
- Local mode: one responder, no upstream connection. Its lifetime is bounded
  to five minutes; the device then reconnects and the relay tries the cloud.
  Native reconnect delays can add reporting gaps. There is no midstream splice,
  retry of ambiguous in-flight messages, or replay of locally acknowledged data.

The cloud diagnostic remains off in local mode. A separate **Local fallback
active** binary sensor turns on. SoC and powers use the existing observer,
serial gate and monotonic 45-second freshness. Disconnects clear availability;
old session values cannot become fresh merely because a new socket connects.
Diagnostics expose mode and counters without serials, credentials or payloads.

## Local reply policy

Login-first and telemetry-first reconnects are supported. A matching login
(`UserName`, `CompanyName=AlphaESS`) or a valid matching live 10 report establishes
identity for empty heartbeats. Queries 03/14/60 can arrive in either observed
order. The report allowlist is 04/05/10/15/16/55/61.

Replies contain only compact `{"Status":"Success"}`, encoded once with the
observed feedback cipher and CRC. No configuration, clock or licensing values
are invented or replayed. Passwords are neither retained nor verified: serial
matching is not authentication. Restrict the listener to the intended LAN path.

History 0c and event/debug logs 09 receive **no ACK**, and interleaved live
reports can continue. A 0c Success can retire history the vendor has not received.
No durability, eventual delivery or vendor-app backfill is promised. Other
unsupported commands, controls and OTA are rejected without a reply.

Local framing is strict and bounded to 5,111 payload bytes, from the native EMS
receive buffer. CRC failures, oversized headers, truncated frames, invalid kinds
and identity mismatches terminate the local session. A deadline covers the whole
frame, including slow trickles; writes have a separate timeout. The forwarding
path retains its existing opaque byte preservation and larger observer bound.

## Development and evidence

`alpha_relay/standalone.py` contains the reply policy and bounded local session
handler. `Relay` owns transitions and socket retirement. A separate
`StandaloneRelay` development harness binds only to 127.0.0.1 and never opens an
upstream socket or DNS lookup; HA uses the integrated fallback path.

Run the HA suite and generated-source drift check with the project test venv:

```bash
python tools/build_ha_package.py --check
python -m pytest -q -c tests_ha/pytest.ini tests_ha
```

Tests cover independent captured ACK bytes, startup paths, fragmentation,
coalescing, identity rejection, withheld history/log ACKs, cloud refusal, EOF,
silent upstream, fresh-session recovery, opaque healthy forwarding, HA default
behavior, sensor states, timeouts and active shutdown. Private native validation
uses pinned EMS instructions to generate requests and consume local replies,
completing seven exchanges without configuration writes. Firmware and private
captures are not distributed.

These tests do not demonstrate autonomous scheduling, full physical cold boot,
licensing/time expiry, installed-dongle compatibility during prolonged WAN loss,
or independence from TCP/51100. Physical outage/recovery validation remains
necessary. Version 0.5.0 adds explicit charge/discharge actions through the same
local session; see [control behavior](CONTROL.md). Fallback never initiates a
charge/discharge action by itself.
