"""Cloud/local ownership and recovery, entirely on synthetic loopback peers."""
import asyncio
from contextlib import asynccontextmanager
import socket

import pytest

from custom_components.alphaess_relay._relay.relay import Relay, RelayConfig
from test_standalone import LIVE, RECORDED_ACK, SERIAL, frame, settled


@asynccontextmanager
async def peer_server(port=0):
    peers, writers = asyncio.Queue(), []

    def accept(reader, writer):
        writers.append(writer)
        peers.put_nowait((reader, writer))

    server = await asyncio.start_server(accept, "127.0.0.1", port)
    try:
        yield server.sockets[0].getsockname()[1], peers
    finally:
        server.close()
        for writer in writers:
            writer.close()
        for writer in writers:
            await writer.wait_closed()
        await server.wait_closed()


@asynccontextmanager
async def connection(relay):
    reader, writer = await asyncio.open_connection(*relay.address)
    try:
        yield reader, writer
    finally:
        writer.close()
        await writer.wait_closed()


def config(port, **kwargs):
    return RelayConfig("127.0.0.1", port, SERIAL, listen_port=0,
                       fallback_enabled=True, eof_timeout=0.03, **kwargs)


async def test_cold_failure_keeps_live_reporting_and_withholds_history_log_acks():
    with socket.socket() as refused:
        refused.bind(("127.0.0.1", 0))
        async with Relay(config(refused.getsockname()[1])) as relay:
            async with connection(relay) as (reader, writer):
                writer.write(frame(0x10, LIVE))
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(29), 2) == RECORDED_ACK
                writer.write(frame(0x0c, {"history": "not delivered"})
                             + frame(9, {"log": "not delivered"}) + frame(0x10, {**LIVE, "SOC": 51}))
                await writer.drain()
                # Only the new live report gets an ACK, in the correct position.
                assert await asyncio.wait_for(reader.readexactly(29), 2) == RECORDED_ACK
                await relay.observer.queue.join()
                state = relay.snapshot()
                assert state["mode"] == "local" and state["local_connected"]
                assert not state["upstream_connected"] and state["available"]
                assert state["telemetry"]["soc_pct"] == 51
                assert state["counters"]["withheld_history_log_acks"] == 2
                assert state["counters"]["local_replies"] == 2


@pytest.mark.parametrize("failure", ["eof", "silent"])
async def test_warm_failure_retires_cloud_before_local_replies(failure):
    async with peer_server() as (port, peers), Relay(config(port, cloud_response_timeout=0.05)) as relay:
        async with connection(relay) as (reader, writer):
            cloud_reader, cloud_writer = await peers.get()
            raw = frame(0x10, LIVE)
            writer.write(raw)
            await writer.drain()
            assert await cloud_reader.readexactly(len(raw)) == raw
            if failure == "eof":
                cloud_writer.close()
                await cloud_writer.wait_closed()
            # Never fabricate an ACK on a session with an ambiguous cloud result.
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await settled(relay)
            if failure == "silent":
                assert relay.last_close_reason == "cloud_response_timeout"
                assert await asyncio.wait_for(cloud_reader.read(), 2) == b""
        async with connection(relay) as (reader, writer):
            writer.write(frame(0x10, {**LIVE, "SOC": 52}))
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(29), 2) == RECORDED_ACK
            await relay.observer.queue.join()
            assert relay.snapshot()["mode"] == "local"
            assert peers.empty()  # The old cloud cannot race with local replies.
            assert relay.snapshot()["telemetry"]["soc_pct"] == 52


async def test_local_recovery_deadline_reconnects_to_cloud_without_replay():
    with socket.socket() as refused:
        refused.bind(("127.0.0.1", 0))
        port = refused.getsockname()[1]
        relay = await Relay(config(port, recovery_interval=0.1)).start()
        try:
            async with connection(relay) as (reader, writer):
                writer.write(frame(0x10, LIVE))
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(29), 2) == RECORDED_ACK
                assert await asyncio.wait_for(reader.read(), 2) == b""
            await settled(relay)
            assert relay.last_close_reason == "cloud_recovery_due"
            refused.close()
            async with peer_server(port) as (_, peers), connection(relay) as (reader, writer):
                cloud_reader, cloud_writer = await peers.get()
                assert not relay.snapshot()["available"]  # No old session values.
                raw = frame(0x10, {**LIVE, "SOC": 53})
                writer.write(raw)
                await writer.drain()
                assert await cloud_reader.readexactly(len(raw)) == raw
                cloud_writer.write(RECORDED_ACK)
                await cloud_writer.drain()
                assert await asyncio.wait_for(reader.readexactly(29), 2) == RECORDED_ACK
                assert relay.snapshot()["mode"] == "cloud"
                assert relay.counters["local_replies"] == 1
                assert relay.counters["upstream_bytes"] == len(raw)
        finally:
            await relay.close()


async def test_healthy_cloud_remains_opaque_with_fallback_enabled():
    async with peer_server() as (port, peers), Relay(config(port)) as relay:
        async with connection(relay) as (reader, writer):
            cloud_reader, cloud_writer = await peers.get()
            raw = b"unknown and invalid protocol bytes" + frame(8, {"Unknown": True})
            writer.write(raw)
            cloud_writer.write(b"unsolicited cloud message")
            await asyncio.gather(writer.drain(), cloud_writer.drain())
            assert await cloud_reader.readexactly(len(raw)) == raw
            assert await reader.readexactly(25) == b"unsolicited cloud message"
            assert relay.snapshot()["mode"] == "cloud"
            assert relay.counters["local_replies"] == 0
