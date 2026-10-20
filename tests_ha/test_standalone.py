"""Standalone development uses synthetic loopback peers, never battery/cloud."""
import asyncio
import json
import struct
from unittest.mock import patch

import pytest

from custom_components.alphaess_relay._relay.observer import crc16
from custom_components.alphaess_relay._relay.payload import encode_payload
from custom_components.alphaess_relay._relay.standalone import (
    MAX_PAYLOAD, StandaloneRejected, StandaloneRelay, StandaloneSession,
)

SERIAL = "STANDALONE-TEST"
LIVE = {"SN": SERIAL, "Time": "2026/10/20 12:00:00", "SOC": 50, "Pbat": -321}
LOGIN = {"UserName": SERIAL, "CompanyName": "AlphaESS", "Password": "synthetic"}
RECORDED_ACK = bytes.fromhex(
    "01021000000014ddc9489da5fcd30d9dd4c94a8b54555e95e9f6df7597")


def frame(command, data=None, *, layers=1, kind=1):
    payload = b"" if data is None else json.dumps(data, separators=(",", ":")).encode()
    for _ in range(layers):
        payload = encode_payload(payload)
    raw = struct.pack(">BBBI", 1, kind, command, len(payload)) + payload
    return raw + crc16(raw).to_bytes(2, "big")


async def settled(relay):
    async with asyncio.timeout(3):
        while relay.active:
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("order", [(3, 0x14, 0x60), (3, 0x60, 0x14)])
def test_login_and_queries_do_not_return_configuration(order):
    session = StandaloneSession(SERIAL)
    requests = [(2, LOGIN), *((command, {"SN": SERIAL}) for command in order), (0, None)]
    for command, data in requests:
        assert session.response(frame(command, data)) == frame(
            command, {"Status": "Success"}, kind=2)


@pytest.mark.parametrize("layers", [0, 1, 2])
def test_telemetry_first_and_independent_captured_ack_vector(layers):
    session = StandaloneSession(SERIAL)
    assert session.response(frame(0x10, LIVE, layers=layers)) == RECORDED_ACK
    assert session.response(frame(0)) == frame(0, {"Status": "Success"}, kind=2)


@pytest.mark.parametrize("command", [8, 0x0d, 0x7f, 0xa0])
def test_controls_history_logs_unknown_never_receive_success(command):
    session = StandaloneSession(SERIAL)
    session.response(frame(0x10, LIVE))
    with pytest.raises(StandaloneRejected, match="unsupported_command"):
        session.response(frame(command, {"SN": SERIAL}))


@pytest.mark.parametrize("raw", [
    frame(0), frame(3, {"SN": SERIAL}), frame(2, {**LOGIN, "UserName": "OTHER"}),
    frame(0x10, {**LIVE, "SN": "OTHER"}), frame(0x10, {**LIVE, "SOC": True}),
    frame(0x10, {**LIVE, "SOC": "nan"}), frame(0x10, []),
    frame(0x10, LIVE, kind=2), frame(0x10, LIVE)[:-1],
    frame(0x10, LIVE)[:-1] + b"\x00",
])
def test_rejected_requests_do_not_establish_identity(raw):
    session = StandaloneSession(SERIAL)
    with pytest.raises(StandaloneRejected):
        session.response(raw)
    assert not session.identified


async def test_fragmented_and_coalesced_reports_publish_without_dns_or_cloud():
    relay = StandaloneRelay(SERIAL)
    await relay.start()
    writer = None
    try:
        # A failure here proves an attempted DNS lookup/upstream connection.
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            reader, writer = await asyncio.open_connection(*relay.address)
            with patch("asyncio.open_connection", side_effect=AssertionError("Upstream forbidden")):
                raw = frame(0x10, LIVE, layers=2)
                for byte in raw:
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(0)
                assert await asyncio.wait_for(reader.readexactly(29), 2) == RECORDED_ACK
                writer.write(frame(0) + frame(0x10, {**LIVE, "SOC": 51}))
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(58), 2) == (
                    frame(0, {"Status": "Success"}, kind=2) + RECORDED_ACK)
                await relay.observer.queue.join()
                state = relay.snapshot()
                assert state["available"] and state["standalone_connected"]
                assert not state["upstream_connected"]
                assert state["telemetry"]["soc_pct"] == 51
                assert state["telemetry"]["battery_power_raw_w"] == -321
                assert state["counters"]["local_replies"] == 3
                assert "Password" not in repr(state)
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        await relay.close()
    assert not relay.tasks and not relay.writers
    assert not relay.snapshot()["available"]


@pytest.mark.parametrize(("raw", "reason"), [
    (frame(8, {"SN": SERIAL}), "unsupported_command"),
    (b"\x01\x01\x10" + (MAX_PAYLOAD + 1).to_bytes(4, "big"), "invalid_frame"),
    (frame(0x10, LIVE)[:7], "truncated_frame"),
    (frame(0x10, LIVE)[:3], "truncated_frame"),
])
async def test_bad_stream_closes_without_reply_and_next_session_is_fresh(raw, reason):
    async with StandaloneRelay(SERIAL) as relay:
        reader, writer = await asyncio.open_connection(*relay.address)
        writer.write(raw)
        await writer.drain()
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        await settled(relay)
        assert relay.last_close_reason == reason
        # A new session must not inherit identity or old telemetry availability.
        reader, writer = await asyncio.open_connection(*relay.address)
        writer.write(frame(0))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        await settled(relay)
        assert relay.last_close_reason == "unexpected_heartbeat"


async def test_partial_frame_timeout_and_active_shutdown():
    async with StandaloneRelay(SERIAL, idle_timeout=0.1) as relay:
        reader, writer = await asyncio.open_connection(*relay.address)
        writer.write(frame(0x10, LIVE)[:8])
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        await settled(relay)
        assert relay.last_close_reason == "frame_timeout"
        reader, writer = await asyncio.open_connection(*relay.address)
        writer.write(frame(0x10, LIVE))
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(29), 2) == RECORDED_ACK
        await asyncio.wait_for(relay.close(), 2)
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
        assert not relay.writers and not relay.tasks
