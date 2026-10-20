"""Bounded, lossy observation of copies; never decides what gets forwarded."""
import asyncio
from collections import Counter
from dataclasses import dataclass
import datetime
import logging
import re
import time

from tools.alpha_payload import decode_payload, telemetry_reading

LOG = logging.getLogger(__name__)


def crc16(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


@dataclass(frozen=True)
class Reading:
    serial: str
    device_time: str
    soc_pct: float
    battery_power_raw_w: float | None
    encoding_layers: int
    generation_w: float | None = None
    home_load_w: float | None = None
    grid_io_w: float | None = None


class TelemetryDecoder:
    """Resynchronizing observer with limits independent of forwarding limits.

    A plausible incomplete header must not hide a subsequent complete valid
    frame. CRC and expected serial gate publication, not the forwarding path.
    CPU work runs in a worker thread; candidate and buffer limits bound abuse.
    """
    max_payload = 65536
    max_candidates = 256
    prefix = re.compile(b"\x01[\x01\x02]")

    def __init__(self, expected_serial):
        self.expected_serial = expected_serial
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        counts = Counter()
        limit = self.max_payload + 9
        if len(self.buffer) > limit:
            counts["buffer_overflows"] += 1
            del self.buffer[:-limit]
        readings = []
        offset, incomplete, candidates = 0, None, 0
        while offset + 7 <= len(self.buffer):
            match = self.prefix.search(self.buffer, offset)
            if match is None:
                break
            start = match.start()
            if start + 7 > len(self.buffer):
                break
            candidates += 1
            if candidates > self.max_candidates:
                counts["candidate_overflows"] += 1
                incomplete = None
                offset = len(self.buffer) - 6
                break
            length = int.from_bytes(self.buffer[start+3:start+7], "big")
            offset = start + 1
            if length > self.max_payload:
                counts["oversized_headers"] += 1
                continue
            end = start + length + 9
            if end > len(self.buffer):
                if incomplete is None:
                    incomplete = start
                continue
            raw = bytes(self.buffer[start:end])
            if crc16(raw[:-2]) != int.from_bytes(raw[-2:], "big"):
                counts["bad_crc"] += 1
                continue
            counts["valid_frames"] += 1
            incomplete = None
            offset = end
            if raw[1:3] != b"\x01\x10":
                continue
            try:
                payload = decode_payload(raw[7:-2])
                if payload.format != "json":
                    raise ValueError("Not telemetry JSON")
                if isinstance(payload.data, dict) and isinstance(payload.data.get("SOC"), bool):
                    raise ValueError("A boolean is not a SoC measurement")
                value = telemetry_reading(payload.data)
                if value["serial"] != self.expected_serial:
                    counts["serial_mismatches"] += 1
                    continue
                readings.append(Reading(value["serial"], value["device_time"], value["soc_pct"],
                                        value.get("battery_power_w"), payload.layers,
                                        value.get("generation_w"), value.get("home_load_w"),
                                        value.get("grid_io_w")))
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                counts["invalid_telemetry"] += 1
        # Retain only a partial candidate or the possible start of a header.
        retain = incomplete if incomplete is not None else max(offset, len(self.buffer) - 6)
        del self.buffer[:retain]
        return readings, counts


class Observer:
    """One decoder worker across sessions; one in-flight thread job maximum."""
    def __init__(self, expected_serial, *, queue_size=16, freshness_seconds=45,
                 decoder_factory=TelemetryDecoder, task_factory=None, run_decoder=None):
        if not 1 <= queue_size <= 1024 or not 0 < freshness_seconds <= 3600:
            raise ValueError("Invalid observer limits")
        self.expected_serial = expected_serial
        self.queue = asyncio.Queue(queue_size)
        self.freshness_seconds = freshness_seconds
        self.decoder_factory = decoder_factory
        self.counters = Counter()
        self.generation = 0
        self.latest_session = 0
        self.connected = False
        self.reading = None
        self.reading_session = 0
        self.received_at = None
        self.received_monotonic = None
        self.task = None
        self.task_factory = task_factory or (lambda coro, name: asyncio.create_task(coro, name=name))
        self.run_decoder = run_decoder or asyncio.to_thread
        self.listeners = set()

    def subscribe(self, listener):
        """Subscribe an event-loop callback; slow consumers belong outside here."""
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def notify(self):
        for listener in tuple(self.listeners):
            try:
                listener()
            except Exception:
                self.counters["callback_errors"] += 1
                LOG.error("observer_callback_failed")

    def start(self):
        self.task = self.task_factory(self._run(), "alpha-relay-observer")

    def offer(self, session, data):
        if self.queue.full():
            self.counters["queue_overflows"] += 1
            self.generation += 1
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
                self.counters["dropped_chunks"] += 1
        self.queue.put_nowait((session, self.generation, data, time.monotonic(),
                              datetime.datetime.now(datetime.timezone.utc).isoformat()))

    async def _run(self):
        key, decoder = None, None
        while True:
            session, generation, data, timestamp, wall = await self.queue.get()
            try:
                if session != self.latest_session:
                    self.counters["superseded_chunks"] += 1
                    continue
                if key != (session, generation):
                    decoder = self.decoder_factory(self.expected_serial)
                    key = (session, generation)
                try:
                    readings, counts = await self.run_decoder(decoder.feed, data)
                except Exception:
                    # Discard parser state. Never expose payloads or exception text.
                    self.counters["decoder_errors"] += 1
                    key = None
                    continue
                self.counters.update(counts)
                # A slow old job must not publish over a newer connection/gap.
                if session != self.latest_session or generation != self.generation:
                    continue
                for reading in readings:
                    self.reading = reading
                    self.reading_session = session
                    self.received_at, self.received_monotonic = wall, timestamp
                    self.counters["telemetry_updates"] += 1
                if readings:
                    self.notify()
            finally:
                self.queue.task_done()

    def snapshot(self):
        age = None if self.received_monotonic is None else max(0, time.monotonic() - self.received_monotonic)
        available = (self.connected and self.reading_session == self.latest_session
                     and age is not None and age <= self.freshness_seconds)
        state = None
        if self.reading is not None:
            r = self.reading
            state = {"serial": r.serial, "device_time": r.device_time, "soc_pct": r.soc_pct,
                     "received_at": self.received_at, "source_command": "0x10",
                     "quality": {"crc_valid": True, "encoding_layers": r.encoding_layers}}
            if r.battery_power_raw_w is not None:
                state["battery_power_raw_w"] = r.battery_power_raw_w
            for key in ("generation_w", "home_load_w", "grid_io_w"):
                value = getattr(r, key)
                if value is not None:
                    state[key] = value
        return {"available": bool(available), "age_seconds": age, "telemetry": state,
                "observer": {"queue_depth": self.queue.qsize(), "queue_limit": self.queue.maxsize,
                             "counters": dict(self.counters)}}

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
