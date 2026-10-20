"""Minimal local replies for cloud outages, plus a loopback development server."""
import asyncio
import struct

from tools.alpha_payload import decode_payload, encode_payload, telemetry_reading

from .observer import crc16
from .relay import Relay, RelayConfig

# Native EMS receive buffer: 5120 bytes including the nine-byte envelope.
MAX_PAYLOAD = 5111
SUCCESS = b'{"Status":"Success"}'
# Existing native simulator coverage only. In particular, never ACK 0x0c:
# Success can retire history that the real cloud has not received.
REPORT_COMMANDS = frozenset((4, 5, 0x10, 0x15, 0x16, 0x55, 0x61))
QUERY_COMMANDS = frozenset((3, 0x14, 0x60))


class StandaloneRejected(ValueError):
    """Fixed, payload-free rejection suitable for diagnostics."""


class StandaloneSession:
    """One identity-bound session; no remembered credentials or configuration.

    Login-first and telemetry-first reconnects are both observed on the wire.
    Only a valid login or live report establishes identity for empty heartbeats.
    The serial check is a publication/reply gate, not network authentication.
    """

    def __init__(self, expected_serial):
        self.expected_serial = expected_serial
        self.identified = False

    def response(self, raw):
        if len(raw) < 9:
            raise StandaloneRejected("invalid_frame")
        version, kind, command, length = struct.unpack_from(">BBBI", raw)
        if (version != 1 or kind != 1 or length > MAX_PAYLOAD
                or len(raw) != length + 9
                or crc16(raw[:-2]) != int.from_bytes(raw[-2:], "big")):
            raise StandaloneRejected("invalid_frame")
        if command in (9, 0x0c):
            if not self.identified:
                raise StandaloneRejected("unidentified_history_or_log")
            # Withhold ACKs while allowing interleaved live reports to continue.
            # No success, error, durable-storage or cloud-delivery claim.
            return None
        if command not in REPORT_COMMANDS | QUERY_COMMANDS | {0, 2}:
            raise StandaloneRejected("unsupported_command")
        if command == 0:
            if length or not self.identified:
                raise StandaloneRejected("unexpected_heartbeat")
        else:
            try:
                value = decode_payload(raw[7:-2])
                if value.format != "json" or not isinstance(value.data, dict):
                    raise ValueError
                data = value.data
                if command == 2:
                    if (data.get("UserName") != self.expected_serial
                            or data.get("CompanyName") != "AlphaESS"):
                        raise ValueError
                elif data.get("SN") != self.expected_serial:
                    raise ValueError
                elif command == 0x10:
                    if isinstance(data.get("SOC"), bool):
                        raise ValueError
                    telemetry_reading(data)
                elif not self.identified:
                    raise ValueError
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                raise StandaloneRejected("invalid_identity_or_payload") from None
            if command in (2, 0x10):
                self.identified = True
        # Compact Status is intentional: the native query parser depends on it.
        # No time, licensing or configuration fields are invented or replayed.
        payload = encode_payload(SUCCESS)
        body = struct.pack(">BBBI", 1, 2, command, len(payload)) + payload
        return body + crc16(body).to_bytes(2, "big")


class StandaloneRelay(Relay):
    """Loopback-only transport using the relay's observer and shutdown lifecycle.

    No upstream socket, DNS lookup, history replay or midstream cloud splice.
    Keeping this separate prevents accidental enablement on the live HA route.
    """

    def __init__(self, expected_serial, *, listen_port=0, idle_timeout=120,
                 write_timeout=10, observer=None):
        # Upstream fields satisfy the shared configuration validator only; the
        # overridden session handler never opens an upstream connection.
        config = RelayConfig("127.0.0.1", 1, expected_serial,
                             listen_host="127.0.0.1", listen_port=listen_port,
                             idle_timeout=idle_timeout, write_timeout=write_timeout)
        super().__init__(config, observer=observer)

    async def _session(self, reader, writer, session):
        self.counters["accepted_connections"] += 1
        self.writers.add(writer)
        reason = "shutdown"
        try:
            if writer.get_extra_info("peername")[0] != "127.0.0.1":
                raise StandaloneRejected("non_loopback_peer")
            self.observer.connected = True
            self.observer.notify()
            reason = await serve_standalone(reader, writer, session, self.config,
                                            self.observer, self.counters)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                writer.transport.abort()
            self.writers.discard(writer)
            self.observer.connected = False
            self.active = False
            self.last_close_reason = reason
            self.counters["closed_connections"] += 1
            self.observer.notify()

    def snapshot(self):
        return {**super().snapshot(), "mode": "standalone_loopback",
                "upstream_connected": False,
                "standalone_connected": self.observer.connected}


async def serve_standalone(reader, writer, session, config, observer, counters):
    """One bounded local session; caller owns connectivity and socket cleanup."""
    policy = StandaloneSession(config.expected_serial)
    frame_started = False
    writer.transport.set_write_buffer_limits(high=config.buffer_bytes,
                                             low=config.buffer_bytes // 4)
    try:
        while True:
            frame_started = False
            # One deadline covers the whole frame, including slow trickles.
            async with asyncio.timeout(config.idle_timeout):
                header = await reader.readexactly(7)
                frame_started = True
                version, kind, _, length = struct.unpack(">BBBI", header)
                if version != 1 or kind != 1 or length > MAX_PAYLOAD:
                    raise StandaloneRejected("invalid_frame")
                raw = header + await reader.readexactly(length + 2)
            response = policy.response(raw)
            if response is None:
                counters["withheld_history_log_acks"] += 1
                continue
            observer.offer(session, raw)
            writer.write(response)
            try:
                await asyncio.wait_for(writer.drain(), config.write_timeout)
            except TimeoutError:
                return "write_timeout"
            counters["local_replies"] += 1
    except asyncio.IncompleteReadError as error:
        return "truncated_frame" if error.partial or frame_started else "eof"
    except StandaloneRejected as error:
        counters["rejected_frames"] += 1
        return str(error)
    except TimeoutError:
        return "frame_timeout"
    except (ConnectionError, OSError):
        return "connection_error"
