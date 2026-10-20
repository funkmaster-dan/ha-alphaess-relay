# Local force-charge and force-discharge

The integration exposes two force switches, three saved inputs per direction
(watts, target SoC, minutes), and action forms `alphaess_relay.start_charge`,
`start_discharge` and `stop`. Actions also accept an absolute `end_time` instead
of `duration_minutes`. An optional `entry_id` selects an integration when multiple
batteries are configured. These use the embedded relay in healthy-cloud and local
fallback modes; there is no portal credential or separate service dependency.

## Power and timing

- Learn nominal watts from the matching device's report-04 `Poinv` in kW and
  cache it in HA storage. An optional advanced inverter-rating input permits
  commissioning from verified device/nameplate information before that report.
- Read report-05 `Pcharge` and `Pdischarge` limits in verified integer W units.
  For 15 minutes after a valid detailed report, constrain the nominal rating by
  the relevant reported limit; a reported zero blocks that direction. Without
  a current detailed report, use the verified inverter rating as the request
  ceiling. Fast status must still be fresh. This is a command ceiling, not
  a substitute for device-side BMS/inverter protections.
- Saved power inputs are bounded by nominal rating; available reported limits are rechecked
  when starting. A battery limit is not a promise that the requested physical
  power will be achieved under all operating conditions.
- Target SoC is 5–100%. Discharge rounds up to a 0.4% protocol step (5% becomes
  5.2%); charge rounds down. Reject a target already reached after that rounding.
  Native reported SoC truncates the fractional percent; requested and reported
  values can therefore differ by one percentage point.
- Native duration has a 90-second floor. Accept 90 seconds through three hours;
  reject shorter requests and end times. A finite duration is always sent to the
  EMS, and a host timer also attempts same-family stop at the deadline while HA
  is running. Uncertain commands are never blindly retried.

## Protocol and arbitration

The only generated control verb is exact `Dispatch`, mode 2. Its compact JSON
uses quoted parameters, a unique 16-hex-character `CmdIndex`, signed power biased
by 32000, target/0.4, and seconds. Full production bytes are checked against actual
pinned native framing, parsing, reporting, stop and expiry instructions.

The manufacturer [Modbus/Server dispatch draft](https://projects.hillviewlodge.ie/_alphaess/AlphaESS-ModbusDispatchFunction.pdf)
(page 4) documents this mode and offset/SoC meanings. It is a 2022 draft, not a
claim of compatibility with every installed firmware or inverter.

Only one local command is outstanding. Injection waits for complete device and
cloud frame boundaries and shares the downstream write lock. Owned ACKs are
matched by exact verb/index and consumed locally, including duplicates/late ACKs;
other ACKs continue to the cloud. After local injection, device frames are
assembled before forwarding so a fragmented owned ACK cannot leak into the
vendor session. Invalid streams remain forwardable but disable further injection.

A Success ACK does not establish actuation. Starting requires a newer matching
report (enable, mode, signed setpoint and reported target); stopping requires a
newer disabled readback. Switches reflect actual reported dispatch, including
externally initiated mode-2 activity, rather than optimistic requests.

Any server command-08 frame observed during local work supersedes local ownership and
cancels its host timer; an overlapping local operation reports a conflict. No
transaction/priority guarantee is possible for commands a different controller
sends concurrently. Avoid parallel portal/VPP automations for the same operation.
Turning off an inactive direction does not cancel the opposite direction.
Turning off forced operation restores normal behavior, not a battery shutdown.

## Scope and recovery

No generic raw-command service, SmartDispatch write, safety/installer setting,
firmware update, calibration, relay/MOS command or universal stop is exposed.
Same-family mode-2 cancellation does not claim to clear unrelated active modes.
Reconnections do not replay start commands. Settings persist locally; active
commands are finite in the device and are not recreated from HA restored state.
While shutting down, HA attempts a bounded stop only for its own active mode-2
operation. Power flow and hardware/BMS protections remain authoritative.

Isolated tests cover healthy-cloud and local-mode control, partial frames,
correlation, late/duplicate ACKs, cloud conflicts, default/saved inputs, mutually
exclusive switches, duration/end-time validation, finite expiry and readback
failure. A native tick-counter wrap can extend device-side expiry; the HA deadline does not
use that device clock. Native receive tests validate encoding/state transitions, not full
physical board behavior, licensing, all operating modes or arbitrary firmware.
See `VALIDATION.md` for the evidence available at release time.
