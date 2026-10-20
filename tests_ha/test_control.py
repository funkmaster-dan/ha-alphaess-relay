"""Synthetic electrical control tests; all sockets are literal loopback."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import socket
import struct

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.alphaess_relay._relay.control import (
    ControlError, ControlState, Dispatch, dispatch_frame,
)
from custom_components.alphaess_relay._relay.payload import decode_payload
from custom_components.alphaess_relay._relay.relay import Relay, RelayConfig
from test_fallback import connection, peer_server
from test_integration import device, dns, eventually, loaded, state
from test_standalone import LIVE, SERIAL, frame


def report(*, enabled=0, power=0, target=0, soc=50, serial=SERIAL, command=0x10, **extra):
    return frame(command, {**LIVE, "SN": serial, "SOC": soc,
        "DispatchSwitch": enabled, "DispatchMode": 2 if enabled else 0,
        "Pdispatch": power, "DispatchSoc": target, **extra})


def initial(serial=SERIAL):
    return (report(serial=serial) + frame(4, {"SN": serial, "Poinv": "1.000"})
            + report(serial=serial, command=5, Pcharge=700, Pdischarge=600))


async def read_frame(reader):
    async with asyncio.timeout(3):
        header = await reader.readexactly(7)
        return header + await reader.readexactly(int.from_bytes(header[3:], "big") + 2)


def response(request, **extra):
    data = decode_payload(request[7:-2]).data
    return frame(8, {"Command": "Dispatch", "CmdIndex": data["CmdIndex"], "Status": "Success", **extra}, kind=2)


@pytest.mark.parametrize("direction,power,raw_power,target,raw_soc,reported", [
    ("discharge", 5000, 37000, 5, 13, 5),
    ("charge", 5000, 27000, 100, 250, 100),
    ("discharge", 250, 32250, 51, 128, 51),
    ("charge", 250, 31750, 99, 247, 98),
])
def test_documented_offset_and_conservative_soc_steps(direction, power, raw_power, target, raw_soc, reported):
    command = Dispatch(direction, power, target, 90)
    raw = dispatch_frame(SERIAL, "0123456789abcdef", command)
    data = decode_payload(raw[7:-2]).data
    assert data["Parameter2"] == f"{raw_power},0,{raw_soc},2,0,0,0,0"
    assert data["Parameter3"] == "90" and data["Parameter1"] == "1"
    assert command.reported_soc == reported
    assert all(isinstance(data[f"Parameter{i}"], str) for i in range(1, 6))


@pytest.mark.parametrize("kwargs", [
    {"power_w": 0}, {"power_w": True}, {"power_w": float("nan")},
    {"power_w": 30001}, {"target_soc": 4.9}, {"target_soc": 101},
    {"target_soc": float("inf")}, {"duration_seconds": 89},
    {"duration_seconds": -1}, {"duration_seconds": 10801}, {"direction": "restart"},
])
def test_invalid_commands_are_rejected_before_encoding(kwargs):
    values = {"direction": "discharge", "power_w": 250, "target_soc": 5, "duration_seconds": 90, **kwargs}
    with pytest.raises(ControlError):
        Dispatch(**values).parameters()


def test_reported_rating_and_live_battery_limits_are_separate():
    control = ControlState(SERIAL, lambda: None)
    control.observe(frame(4, {"SN": "OTHER", "Poinv": "99"}))
    assert control.nominal_power_w is None
    control.observe(frame(4, {"SN": SERIAL, "Poinv": "5.000"}))
    assert control.nominal_power_w == 5000
    assert control.maximum_power("charge") == 5000
    assert not control.limits_current()
    control.observe(report(command=5, Pcharge=15435, Pdischarge=4000))
    assert control.maximum_power("charge") == 5000
    assert control.maximum_power("discharge") == 4000
    assert control.limits_current()
    control.observe(report(command=5, Pcharge=0, Pdischarge=0))
    assert control.maximum_power("charge") == 0
    control.observe(report(command=5, Pcharge="nan", Pdischarge=999999))
    assert control.maximum_power("discharge") == 5000
    assert not control.limits_current()
    control.disconnected()
    assert control.nominal_power_w == 5000 and not control.fresh()


async def test_local_ack_filtered_cloud_stream_preserved_and_late_ack_not_retried():
    async with peer_server() as (port, peers), Relay(RelayConfig(
        "127.0.0.1", port, SERIAL, listen_port=0, controls_enabled=True)) as relay:
        async with connection(relay) as (reader, writer):
            cloud_reader, _ = await peers.get()
            data = initial()
            writer.write(data)
            await writer.drain()
            assert await cloud_reader.readexactly(len(data)) == data
            command = Dispatch("discharge", 250, 5, 90)
            pending = asyncio.create_task(relay.control_channel.request(command))
            request = await read_frame(reader)
            ack = response(request)
            # An unrelated ACK must still reach the vendor without completing ours.
            other = response(request, CmdIndex="ffffffffffffffff")
            writer.write(other)
            await writer.drain()
            assert await cloud_reader.readexactly(len(other)) == other
            assert not pending.done()
            for byte in ack:
                writer.write(bytes([byte]))
                await writer.drain()
                await asyncio.sleep(0)
            await asyncio.wait_for(pending, 2)
            live = report(enabled=1, power=250, target=5)
            writer.write(ack + live)  # Duplicate owned ACK is consumed as well.
            await writer.drain()
            assert await cloud_reader.readexactly(len(live)) == live
            relay.control_channel.timeout = 0.03
            pending = asyncio.create_task(relay.control_channel.request(command))
            late_request = await read_frame(reader)
            with pytest.raises(ControlError, match="outcome_unknown"):
                await pending
            writer.write(response(late_request) + live)
            await writer.drain()
            assert await cloud_reader.readexactly(len(live)) == live
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.read(1), 0.03)  # No blind retry.


async def test_injection_waits_for_cloud_frame_boundary():
    async with peer_server() as (port, peers), Relay(RelayConfig(
        "127.0.0.1", port, SERIAL, listen_port=0, controls_enabled=True)) as relay:
        async with connection(relay) as (reader, writer):
            cloud_reader, cloud_writer = await peers.get()
            data = initial()
            writer.write(data)
            await writer.drain()
            await cloud_reader.readexactly(len(data))
            cloud_frame = frame(0x10, {"Status": "Success"}, kind=2)
            cloud_writer.write(cloud_frame[:9])
            await cloud_writer.drain()
            assert await reader.readexactly(9) == cloud_frame[:9]
            pending = asyncio.create_task(relay.control_channel.request(Dispatch("charge", 250, 100, 90)))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.read(1), 0.03)
            cloud_writer.write(cloud_frame[9:])
            await cloud_writer.drain()
            assert await reader.readexactly(len(cloud_frame) - 9) == cloud_frame[9:]
            request = await read_frame(reader)
            assert decode_payload(request[7:-2]).data["Command"] == "Dispatch"
            writer.write(response(request))
            await writer.drain()
            await asyncio.wait_for(pending, 2)


async def test_control_works_in_local_fallback_without_response_to_command_ack():
    with socket.socket() as refused:
        refused.bind(("127.0.0.1", 0))
        async with Relay(RelayConfig("127.0.0.1", refused.getsockname()[1], SERIAL,
                                    listen_port=0, controls_enabled=True, fallback_enabled=True)) as relay:
            async with connection(relay) as (reader, writer):
                writer.write(initial())
                await writer.drain()
                for _ in range(3):
                    assert (await read_frame(reader))[1] == 2
                pending = asyncio.create_task(relay.control_channel.request(Dispatch("charge", 250, 100, 90)))
                request = await read_frame(reader)
                writer.write(response(request) + report(enabled=1, power=-250, target=100))
                await writer.drain()
                await asyncio.wait_for(pending, 2)
                live_ack = await read_frame(reader)
                assert live_ack[1:3] == b"\x02\x10"
                assert relay.control_state.dispatch["power_w"] == -250
                assert relay.snapshot()["mode"] == "local"


async def test_cloud_control_conflict_is_not_reported_as_local_success():
    async with peer_server() as (port, peers), Relay(RelayConfig(
        "127.0.0.1", port, SERIAL, listen_port=0, controls_enabled=True)) as relay:
        async with connection(relay) as (reader, writer):
            cloud_reader, cloud_writer = await peers.get()
            data = initial()
            writer.write(data)
            await writer.drain()
            await cloud_reader.readexactly(len(data))
            pending = asyncio.create_task(relay.control_channel.request(Dispatch("charge", 250, 100, 90)))
            request = await read_frame(reader)
            external = frame(8, {"Command": "DispatchExtra", "CmdIndex": "CLOUD-COMMAND", "Parameter1": "1"})
            cloud_writer.write(external)
            await cloud_writer.drain()
            assert await read_frame(reader) == external
            writer.write(response(request))
            await writer.drain()
            with pytest.raises(ControlError, match="cloud_control_conflict"):
                await pending
            assert relay.control_state.cloud_revision == 1
            assert relay.control_state.counters["sent_commands"] == 1


async def emulated_device(reader, writer, serial, commands, *, confirm=True):
    while True:
        try:
            request = await read_frame(reader)
        except asyncio.IncompleteReadError:
            return
        data = decode_payload(request[7:-2]).data
        commands.append(data)
        writer.write(response(request))
        if confirm:
            parts = [int(value) for value in data["Parameter2"].split(",")]
            writer.write(report(serial=serial, enabled=int(data["Parameter1"]),
                power=parts[0] - 32000, target=parts[2] * 4 // 10))
        await writer.drain()


async def test_ha_switches_settings_actions_and_mutual_exclusion(hass, dns):
    async with peer_server() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        serial = entry.data["serial"]
        async with device(entry) as (reader, writer):
            cloud_reader, _ = await peers.get()
            data = initial(serial)
            writer.write(data)
            await writer.drain()
            await cloud_reader.readexactly(len(data))
            await eventually(lambda: entry.runtime_data.control.available("discharge"))
            commands = []
            worker = asyncio.create_task(emulated_device(reader, writer, serial, commands))
            try:
                power = state(hass, "discharge_power_w", "number")
                assert power.attributes["max"] == 1000
                await hass.services.async_call("number", "set_value", {"entity_id": power.entity_id, "value": 250}, blocking=True)
                discharge = state(hass, "force_discharge", "switch")
                charge = state(hass, "force_charge", "switch")
                await hass.services.async_call("switch", "turn_on", {"entity_id": discharge.entity_id}, blocking=True)
                assert state(hass, "force_discharge", "switch").state == "on"
                assert state(hass, "force_charge", "switch").state == "off"
                assert commands[-1]["Parameter2"].startswith("32250,0,13,2,")
                await hass.services.async_call("switch", "turn_on", {"entity_id": charge.entity_id}, blocking=True)
                assert [item["Parameter1"] for item in commands] == ["1", "0", "1"]
                assert state(hass, "force_charge", "switch").state == "on"
                assert state(hass, "force_discharge", "switch").state == "off"
                await hass.services.async_call("switch", "turn_off", {"entity_id": discharge.entity_id}, blocking=True)
                assert len(commands) == 3
                await hass.services.async_call("switch", "turn_off", {"entity_id": charge.entity_id}, blocking=True)
                assert commands[-1]["Parameter1"] == "0"
                end = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
                await hass.services.async_call("alphaess_relay", "start_discharge", {
                    "power_w": 200, "target_soc": 6, "end_time": end}, blocking=True)
                assert 110 <= int(commands[-1]["Parameter3"]) <= 120
                await entry.runtime_data.control._expire(0)
                assert commands[-1]["Parameter1"] == "0"
                assert state(hass, "force_discharge", "switch").state == "off"
                count = len(commands)
                for kwargs in ({"target_soc": 4}, {"power_w": 601},
                               {"target_soc": 80}, {"duration_minutes": 1},
                               {"end_time": end, "duration_minutes": 2}):
                    with pytest.raises(HomeAssistantError):
                        await entry.runtime_data.control.start("discharge", **kwargs)
                assert len(commands) == count
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)


async def test_success_ack_without_readback_does_not_turn_ha_switch_on(hass, dns):
    async with peer_server() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        serial = entry.data["serial"]
        async with device(entry) as (reader, writer):
            cloud_reader, _ = await peers.get()
            data = initial(serial)
            writer.write(data)
            await writer.drain()
            await cloud_reader.readexactly(len(data))
            await eventually(lambda: entry.runtime_data.control.available("discharge"))
            entry.runtime_data.control.confirmation_timeout = 0.03
            commands = []
            worker = asyncio.create_task(emulated_device(reader, writer, serial, commands, confirm=False))
            try:
                with pytest.raises(HomeAssistantError, match="readback not confirmed"):
                    await entry.runtime_data.control.start("discharge", power_w=250, duration_minutes=1.5)
                assert state(hass, "force_discharge", "switch").state == "off"
                assert len(commands) == 1
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
