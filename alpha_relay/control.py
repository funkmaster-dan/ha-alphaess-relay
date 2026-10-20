"""Bounded mode-2 dispatch, reported capabilities and command correlation."""
import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
import secrets
import struct
import time

from tools.alpha_payload import decode_payload, encode_payload
from .observer import crc16


class ControlError(ValueError):
    """Fixed, payload-free control failure."""


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ControlError("invalid_number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ControlError("invalid_number") from None
    if not number.is_finite():
        raise ControlError("invalid_number")
    return number


def integer(value, minimum, maximum):
    number = finite_number(value)
    if number != int(number) or not minimum <= number <= maximum:
        raise ControlError("invalid_range")
    return int(number)


@dataclass(frozen=True)
class Dispatch:
    direction: str
    power_w: int
    target_soc: float
    duration_seconds: int
    enabled: bool = True

    def parameters(self):
        if self.direction not in ("charge", "discharge") or not isinstance(self.enabled, bool):
            raise ControlError("invalid_dispatch")
        power = integer(self.power_w, 1, 30000)
        duration = integer(self.duration_seconds, 90, 10800)
        soc = finite_number(self.target_soc)
        if not 5 <= soc <= 100:
            raise ControlError("invalid_soc")
        # 0.4% protocol steps. Never discharge below, or charge above, the
        # requested target. The native report truncates the resulting percent.
        rounding = ROUND_CEILING if self.direction == "discharge" else ROUND_FLOOR
        raw_soc = int((soc / Decimal("0.4")).to_integral_value(rounding=rounding))
        signed_power = power if self.direction == "discharge" else -power
        if not self.enabled:
            signed_power, raw_soc = 0, 0
        return {"Parameter1": "1" if self.enabled else "0",
                "Parameter2": f"{32000 + signed_power},0,{raw_soc},2,0,0,0,0",
                "Parameter3": str(duration), "Parameter4": "", "Parameter5": ""}

    @property
    def reported_soc(self):
        raw = int(self.parameters()["Parameter2"].split(",")[2])
        return raw * 4 // 10


def dispatch_frame(serial, index, dispatch):
    if not isinstance(index, str) or len(index) != 16 or not all(c in "0123456789abcdef" for c in index):
        raise ControlError("invalid_command_index")
    value = {"Command": "Dispatch", "CmdIndex": index, "SysSn": serial,
             "Description": "Home Assistant local dispatch", **dispatch.parameters()}
    body = encode_payload(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())
    raw = struct.pack(">BBBI", 1, 1, 8, len(body)) + body
    return raw + crc16(raw).to_bytes(2, "big")


class WireStream:
    """Strict bounded tap. Invalid streams still forward, but cannot inject."""
    def __init__(self):
        self.buffer = bytearray()
        self.valid = True

    def feed(self, data):
        if not self.valid:
            return [], data
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 7:
            version, kind, _, length = struct.unpack_from(">BBBI", self.buffer)
            if version != 1 or kind not in (1, 2) or length > 65536:
                self.valid = False
                break
            if len(self.buffer) < length + 9:
                break
            raw = bytes(self.buffer[:length + 9])
            if crc16(raw[:-2]) != int.from_bytes(raw[-2:], "big"):
                self.valid = False
                break
            frames.append(raw)
            del self.buffer[:length + 9]
        remainder = b""
        if not self.valid:
            remainder = bytes(self.buffer)
            self.buffer.clear()
        return frames, remainder


class ControlState:
    def __init__(self, serial, notify):
        self.serial, self.notify = serial, notify
        self.nominal_power_w = None
        self.limits = {}
        self.limits_received = None
        self.dispatch = None
        self.received = None
        self.revision = 0
        self.cloud_revision = 0
        self.counters = Counter()

    def observe(self, raw, *, cloud=False):
        if raw[2] not in ((8,) if cloud else (4, 5, 0x10)) or raw[1] != 1:
            return
        if cloud:
            # The native parser accepts prefixes and is not a JSON parser.
            # Any server command can also reuse shared CmdIndex state. Do not
            # decide ownership from a Python interpretation of the verb.
            self.cloud_revision += 1
            self.notify()
            return
        try:
            payload = decode_payload(raw[7:-2])
            data = payload.data
            if payload.format != "json" or not isinstance(data, dict):
                return
            if data.get("SN") != self.serial:
                return
            if raw[2] == 4 and "Poinv" in data:
                watts = finite_number(data["Poinv"]) * 1000
                self.nominal_power_w = integer(watts, 1, 30000)
            if raw[2] == 5:
                self.limits = {key: integer(data[field], 0, 1000000) for key, field in
                               (("charge", "Pcharge"), ("discharge", "Pdischarge"))}
                self.limits_received = time.monotonic()
            if raw[2] in (5, 0x10):
                self.dispatch = {
                    "enabled": integer(data["DispatchSwitch"], 0, 1),
                    "mode": integer(data["DispatchMode"], 0, 255),
                    "power_w": integer(data["Pdispatch"], -32000, 33535),
                    "target_soc": integer(data["DispatchSoc"], 0, 100),
                }
                self.received = time.monotonic()
                self.revision += 1
            self.notify()
        except (ControlError, ValueError, TypeError, KeyError, RecursionError):
            if raw[2] in (5, 0x10):
                self.dispatch, self.received = None, None
            if raw[2] == 5:
                self.limits, self.limits_received = {}, None
            self.notify()

    def maximum_power(self, direction):
        if self.nominal_power_w is None:
            return None
        if self.limits_received is None or time.monotonic() - self.limits_received > 180:
            return None
        limit = self.limits.get(direction)
        return min(self.nominal_power_w, limit) if limit is not None else None

    def fresh(self):
        return self.received is not None and time.monotonic() - self.received <= 45

    def disconnected(self):
        self.dispatch, self.received = None, None
        self.limits, self.limits_received = {}, None

    def snapshot(self):
        return {"nominal_power_w": self.nominal_power_w,
                "max_charge_power_w": self.maximum_power("charge"),
                "max_discharge_power_w": self.maximum_power("discharge"),
                "dispatch": self.dispatch if self.fresh() else None,
                "counters": dict(self.counters)}


class ControlChannel:
    """One session, one pending command, local ACKs consumed before cloud.

    Before the first local command, forwarding remains opaque and immediate.
    Afterwards device frames are assembled to remove only owned command ACKs.
    Unknown/bad bytes fail open for forwarding and fail closed for new controls.
    """
    def __init__(self, state, writer, timeout=15):
        self.state, self.writer, self.timeout = state, writer, timeout
        self.up, self.down = WireStream(), WireStream()
        self.write_lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.closed = False
        self.filter_enabled = False
        self.owned = deque(maxlen=64)
        self.pending = None

    @property
    def ready(self):
        return not self.closed and self.up.valid and self.down.valid

    def observe_up(self, data):
        was_valid = self.up.valid
        frames, remainder = self.up.feed(data)
        forwarded = []
        for raw in frames:
            self.state.observe(raw)
            consumed = False
            if raw[1:3] == b"\x02\x08":
                try:
                    payload = decode_payload(raw[7:-2]).data
                    if isinstance(payload, dict) and payload.get("Command") == "Dispatch" and payload.get("CmdIndex") in self.owned:
                        consumed = True
                        self.state.counters["filtered_local_acks"] += 1
                        if self.pending and payload.get("CmdIndex") == self.pending[0]:
                            future = self.pending[1]
                            if not future.done():
                                if payload.get("Status") == "Success":
                                    future.set_result(True)
                                else:
                                    future.set_exception(ControlError("command_rejected"))
                except (ValueError, TypeError, RecursionError):
                    pass
            if not consumed:
                forwarded.append(raw)
        self.changed.set()
        if was_valid and not self.up.valid:
            self.state.notify()
        return b"".join(forwarded) + remainder if self.filter_enabled else data

    def observe_down(self, data):
        was_valid = self.down.valid
        frames, _ = self.down.feed(data)
        for raw in frames:
            self.state.observe(raw, cloud=True)
        self.changed.set()
        if was_valid and not self.down.valid:
            self.state.notify()

    async def request(self, dispatch):
        if self.pending:
            raise ControlError("command_in_progress")
        future = asyncio.get_running_loop().create_future()
        index = secrets.token_hex(8)
        raw = dispatch_frame(self.state.serial, index, dispatch)
        self.pending = index, future
        cloud_revision = self.state.cloud_revision
        try:
            # Never inject into a partially forwarded frame, in either direction.
            async with asyncio.timeout(2):
                while True:
                    self.changed.clear()
                    async with self.write_lock:
                        if not self.ready or self.writer.is_closing():
                            raise ControlError("control_channel_unavailable")
                        if not self.up.buffer and not self.down.buffer:
                            if cloud_revision != self.state.cloud_revision:
                                raise ControlError("cloud_control_conflict")
                            self.owned.append(index)
                            self.filter_enabled = True
                            self.writer.write(raw)
                            await self.writer.drain()
                            self.state.counters["sent_commands"] += 1
                            break
                    await self.changed.wait()
            async with asyncio.timeout(self.timeout):
                await future
            if cloud_revision != self.state.cloud_revision:
                raise ControlError("cloud_control_conflict")
            self.state.counters["acknowledged_commands"] += 1
        except TimeoutError:
            self.state.counters["unknown_outcomes"] += 1
            raise ControlError("command_outcome_unknown") from None
        finally:
            if not future.done():
                future.cancel()
            self.pending = None

    def close(self):
        self.closed = True
        self.changed.set()
        if self.pending and not self.pending[1].done():
            self.pending[1].set_exception(ControlError("device_disconnected"))
