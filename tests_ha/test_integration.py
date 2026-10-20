"""Exercise the installed component against real HA registries and config flows."""
import asyncio
from contextlib import asynccontextmanager
import json
import socket
import struct
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alphaess_relay.const import DOMAIN, UPSTREAM_HOST, UPSTREAM_PORT
from custom_components.alphaess_relay._relay.observer import crc16
from custom_components.alphaess_relay._relay.payload import encode_payload

SERIAL = "OFFLINE-TEST-EMS"
POWER_FIELDS = {"Ppv1": "1000", "Ppv2": "200", "Ppv3": "300", "Ppv4": "400",
                "PmeterDC": "500", "PmeterL1": "-1200", "PmeterL2": "100", "PmeterL3": "-50"}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def assert_port_free(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def wire(*, serial=SERIAL, soc="50.4", power="-321", layers=1, command=0x10, **extra):
    data = {"SN": serial, "Time": "2026/10/20 12:00:00", "SOC": soc, **extra}
    if power is not None:
        data["Pbat"] = power
    payload = json.dumps(data, separators=(",", ":")).encode()
    for _ in range(layers):
        payload = encode_payload(payload)
    result = struct.pack(">BBBI", 1, 1, command, len(payload)) + payload
    return result + crc16(result).to_bytes(2, "big")


async def eventually(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.005)


@asynccontextmanager
async def cloud():
    connections, writers = asyncio.Queue(), set()

    def accept(reader, writer):
        writers.add(writer)
        connections.put_nowait((reader, writer))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], connections
    finally:
        server.close()
        for writer in writers:
            writer.close()
        for writer in writers:
            try:
                await writer.wait_closed()
            except OSError:
                pass
        await server.wait_closed()


@asynccontextmanager
async def loaded(hass):
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        yield entry
    finally:
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@asynccontextmanager
async def device(entry):
    reader, writer = await asyncio.open_connection(*entry.runtime_data.relay.address)
    try:
        yield reader, writer
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def state(hass, key, domain="sensor"):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{SERIAL}_{key}")
    assert entity_id is not None
    return hass.states.get(entity_id)


@pytest.fixture
def dns(hass, monkeypatch):
    """Keep the real product hostname/port, resolve it to synthetic loopback."""
    target = {"port": None, "calls": []}

    async def resolve(host, port, **kwargs):
        assert (host, port) == (UPSTREAM_HOST, UPSTREAM_PORT)
        target["calls"].append((host, port))
        if target["port"] is None:
            raise OSError("synthetic DNS failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", target["port"]))]

    monkeypatch.setattr(hass.loop, "getaddrinfo", resolve)
    return target


def make_entry(hass, port=None, serial=SERIAL):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=serial, title=f"AlphaESS {serial}",
                            data={"serial": serial}, options={"listen_host": "127.0.0.1",
                                                             "listen_port": port or free_port(),
                                                             "fallback_enabled": False})
    entry.add_to_hass(hass)
    return entry


async def test_setup_screen_only_serial(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    assert {str(k) for k in result["data_schema"].schema} == {"serial", "listener"}
    fields = {str(k): v for k, v in result["data_schema"].schema.items()}
    assert fields["listener"].options["collapsed"]
    assert {str(k) for k in fields["listener"].schema.schema} == {"listen_host", "listen_port", "fallback_enabled", "inverter_rating_w"}
    serialized = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    # The frontend initializes an expandable section from its parent default.
    # An empty parent default hides the nested defaults when expanded.
    listener_field = next(field for field in serialized if field["name"] == "listener")
    assert listener_field["default"] == {"listen_host": "0.0.0.0", "listen_port": 17778, "fallback_enabled": True}
    assert "upstream" not in json.dumps(serialized)
    assert "password" not in json.dumps(serialized)


async def test_setup_is_local_and_unload_releases_listener(hass):
    entry = make_entry(hass)
    with patch("custom_components.alphaess_relay._relay.relay.asyncio.open_connection", new_callable=AsyncMock) as connect:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        connect.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.relay.address == ("127.0.0.1", entry.options["listen_port"])
    states = hass.states.async_all()
    assert len([s for s in states if s.entity_id.startswith("sensor.alphaess_")]) == 6
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator.closed
    assert not coordinator.relay.tasks
    assert coordinator.relay.observer.task is None
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", entry.options["listen_port"]))


async def test_flow_normalizes_serial_and_applies_defaults_without_probe(hass):
    with patch("custom_components.alphaess_relay.async_setup_entry", return_value=True), patch(
        "custom_components.alphaess_relay._relay.relay.asyncio.open_connection", new_callable=AsyncMock
    ) as connect:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"},
                                                          data={"serial": " offline-test-ems "})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {"serial": SERIAL}
        assert result["options"] == {"listen_host": "0.0.0.0", "listen_port": 17778, "fallback_enabled": True}
        await hass.async_block_till_done()
        connect.assert_not_called()


@pytest.mark.parametrize("serial", ["", "xx", "invalid serial!", "x"*65, None, 42])
async def test_flow_rejects_invalid_serial(hass, serial):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"}, data={"serial": serial})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["serial"] == "invalid_serial"


async def test_flow_rejects_duplicate_serial(hass):
    make_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"}, data={"serial": SERIAL.lower()})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("listener", [{"listen_host": "not-an-ip"}, {"listen_port": 80},
                                      {"listen_host": "224.1.1.1"}, {"listen_port": 65536}])
async def test_flow_rejects_invalid_listener(hass, listener):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"},
                                                      data={"serial": SERIAL, "listener": listener})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_listener"


async def test_flow_rejects_conflicting_listener(hass):
    entry = make_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"},
        data={"serial": "ANOTHER-EMS", "listener": {"listen_host": "0.0.0.0", "listen_port": entry.options["listen_port"]}})
    assert result["errors"]["base"] == "listener_in_use"


async def test_sensors_initially_unavailable_and_dns_not_queried(hass, dns):
    async with loaded(hass):
        assert state(hass, "soc").state == STATE_UNAVAILABLE
        assert state(hass, "battery_io").state == STATE_UNAVAILABLE
        assert state(hass, "last_received").state == "unknown"
        assert state(hass, "relay_connected", "binary_sensor").state == "off"
        assert dns["calls"] == []


async def test_telemetry_first_push_updates_entities_and_forwards_unchanged(hass, dns):
    async with cloud() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        async with device(entry) as (reader, writer):
            cloud_reader, cloud_writer = await asyncio.wait_for(peers.get(), 2)
            cloud_writer.write(b"unsolicited-synthetic-cloud-bytes")
            await cloud_writer.drain()
            assert await asyncio.wait_for(reader.readexactly(33), 2) == b"unsolicited-synthetic-cloud-bytes"
            data = wire(layers=2, **POWER_FIELDS)
            for offset in range(0, len(data), 3):
                writer.write(data[offset:offset+3])
                await writer.drain()
                await asyncio.sleep(0)
            assert await asyncio.wait_for(cloud_reader.readexactly(len(data)), 2) == data
            await eventually(lambda: state(hass, "soc").state == "50.4")
            assert state(hass, "soc").attributes["unit_of_measurement"] == "%"
            assert state(hass, "soc").attributes["state_class"] == "measurement"
            assert state(hass, "battery_io").state == "-321.0"
            for key, value in (("generation", "2400.0"), ("home_load", "929.0"),
                               ("grid_io_total", "-1150.0"), ("battery_io", "-321.0")):
                entity = state(hass, key)
                assert entity.state == value
                assert entity.attributes["unit_of_measurement"] == "W"
                assert entity.attributes["device_class"] == "power"
                assert entity.attributes["state_class"] == "measurement"
            assert state(hass, "battery_io").attributes["friendly_name"].endswith("Instantaneous battery I/O")
            assert state(hass, "relay_connected", "binary_sensor").state == "on"
            assert state(hass, "last_received").state != "unknown"
            assert dns["calls"] == [(UPSTREAM_HOST, UPSTREAM_PORT)]


async def test_stale_timer_marks_data_unavailable_and_new_frame_recovers(hass, dns):
    async with cloud() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        coordinator = entry.runtime_data
        coordinator.relay.observer.freshness_seconds = 0.08
        async with device(entry) as (_, writer):
            await peers.get()
            writer.write(wire(**POWER_FIELDS))
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "50.4")
            received = state(hass, "last_received").state
            await eventually(lambda: state(hass, "soc").state == STATE_UNAVAILABLE)
            for key in ("generation", "home_load", "grid_io_total", "battery_io"):
                assert state(hass, key).state == STATE_UNAVAILABLE
            assert state(hass, "last_received").state == received
            assert state(hass, "relay_connected", "binary_sensor").state == "on"
            writer.write(wire(soc="52.0"))
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "52.0")
            # A newer incomplete report must not retain old power measurements.
            for key in ("generation", "home_load", "grid_io_total"):
                assert state(hass, key).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(("field", "value", "unavailable"), [
    ("PmeterL2", None, ("grid_io_total", "home_load")),
    ("Ppv4", "bad", ("generation", "home_load")),
    ("PmeterDC", True, ("generation", "home_load")),
    ("Pbat", "nan", ("battery_io", "home_load")),
])
async def test_invalid_optional_power_only_affects_dependent_sensors(hass, dns, field, value, unavailable):
    async with cloud() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        async with device(entry) as (_, writer):
            cloud_reader, _ = await peers.get()
            values = {**POWER_FIELDS, "Pbat": "-321", field: value}
            data = wire(power=values.pop("Pbat"), **values)
            writer.write(data)
            await writer.drain()
            assert await cloud_reader.readexactly(len(data)) == data
            await eventually(lambda: state(hass, "soc").state == "50.4")
            for key in ("generation", "home_load", "grid_io_total", "battery_io"):
                assert (state(hass, key).state == STATE_UNAVAILABLE) == (key in unavailable)


@pytest.mark.parametrize(("battery", "grid", "expected_load"), [
    ("321", ["-1200", "100", "-50"], 1571.0),
    ("-321", ["500", "200", "300"], 3079.0),
    ("0", ["0", "0", "0"], 2400.0),
])
def test_power_balance_preserves_import_export_and_charge_discharge(battery, grid, expected_load):
    from custom_components.alphaess_relay._relay.payload import telemetry_reading
    data = {"SN": SERIAL, "Time": "2026/10/20 12:00:00", "SOC": "50", **POWER_FIELDS,
            "Pbat": battery, **{f"PmeterL{i}": value for i, value in enumerate(grid, 1)}}
    result = telemetry_reading(data)
    assert result["generation_w"] == 2400
    assert result["grid_io_w"] == sum(map(float, grid))
    assert result["battery_power_w"] == float(battery)
    assert result["home_load_w"] == expected_load


async def test_battery_rename_replaces_obsolete_entity(hass):
    entry = make_entry(hass)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create("sensor", DOMAIN, f"{SERIAL}_battery_power_raw",
        config_entry=entry, suggested_object_id="existing_battery")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    renamed = state(hass, "battery_io")
    assert renamed.entity_id != existing.entity_id
    assert registry.async_get(existing.entity_id) is None
    assert renamed.entity_id.endswith("_instantaneous_battery_i_o")
    assert renamed.attributes["friendly_name"].endswith("Instantaneous battery I/O")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_bad_identity_crc_and_history_do_not_change_sensors(hass, dns):
    async with cloud() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        async with device(entry) as (_, writer):
            cloud_reader, _ = await peers.get()
            bad_crc = bytearray(wire()); bad_crc[-1] ^= 1
            data = bytes(bad_crc) + wire(serial="OTHER-EMS") + wire(soc="nan") + wire(command=0x0C)
            writer.write(data)
            await writer.drain()
            assert await asyncio.wait_for(cloud_reader.readexactly(len(data)), 2) == data
            await asyncio.wait_for(entry.runtime_data.relay.observer.queue.join(), 2)
            assert state(hass, "soc").state == STATE_UNAVAILABLE
            writer.write(wire(power=None))
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "50.4")
            assert state(hass, "battery_io").state == STATE_UNAVAILABLE


async def test_reconnect_resolves_dns_again_and_does_not_restore_stale_state(hass, dns):
    async with cloud() as (port1, peers1), cloud() as (port2, peers2), loaded(hass) as entry:
        dns["port"] = port1
        async with device(entry) as (_, writer):
            _, upstream = await peers1.get()
            writer.write(wire())
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "50.4")
            upstream.close()
        await eventually(lambda: not entry.runtime_data.relay.active)
        assert state(hass, "soc").state == STATE_UNAVAILABLE
        dns["port"] = port2
        async with device(entry) as (_, writer):
            cloud_reader, _ = await peers2.get()
            assert state(hass, "soc").state == STATE_UNAVAILABLE
            data = wire(soc="53")
            writer.write(data)
            await writer.drain()
            assert await cloud_reader.readexactly(len(data)) == data
            await eventually(lambda: state(hass, "soc").state == "53.0")
        assert len(dns["calls"]) == 2


async def test_dns_failure_closes_session_without_ack_then_can_recover(hass, dns):
    async with cloud() as (port, peers), loaded(hass) as entry:
        async with device(entry) as (reader, _):
            assert await asyncio.wait_for(reader.read(), 2) == b""
        await eventually(lambda: not entry.runtime_data.relay.active)
        assert state(hass, "soc").state == STATE_UNAVAILABLE
        assert entry.runtime_data.relay.snapshot()["counters"].get("downstream_bytes", 0) == 0
        dns["port"] = port
        async with device(entry) as (_, writer):
            await peers.get()
            writer.write(wire())
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "50.4")


async def test_existing_entry_defaults_to_fallback_and_publishes_ha_sensors(hass, dns):
    entry = make_entry(hass)
    options = dict(entry.options)
    options.pop("fallback_enabled")
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        assert entry.runtime_data.relay.config.fallback_enabled
        async with device(entry) as (reader, writer):
            writer.write(wire())
            await writer.drain()
            assert len(await asyncio.wait_for(reader.readexactly(29), 2)) == 29
            await eventually(lambda: state(hass, "soc").state == "50.4")
            assert state(hass, "relay_connected", "binary_sensor").state == "off"
            assert state(hass, "local_fallback_active", "binary_sensor").state == "on"
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_busy_listening_port_retries_without_leaking_tasks(hass):
    async with cloud() as (port, _):
        entry = make_entry(hass, port=port)
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        entry.async_cancel_retry_setup()
        assert not entry._background_tasks


async def test_options_reload_preserves_entity_ids_and_releases_old_port(hass):
    async with loaded(hass) as entry:
        before = entry.runtime_data
        old_port = entry.options["listen_port"]
        registry = er.async_get(hass)
        ids = {e.entity_id for e in registry.entities.values() if e.config_entry_id == entry.entry_id}
        options = await hass.config_entries.options.async_init(entry.entry_id)
        assert options["type"] is FlowResultType.FORM
        new_port = free_port()
        result = await hass.config_entries.options.async_configure(options["flow_id"],
            user_input={"listen_host": "127.0.0.1", "listen_port": new_port})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.runtime_data is not before
        assert before.closed
        assert entry.runtime_data.relay.address[1] == new_port
        assert_port_free(old_port)
        assert ids == {e.entity_id for e in registry.entities.values() if e.config_entry_id == entry.entry_id}


async def test_diagnostics_exclude_payloads_and_serial(hass, dns):
    from custom_components.alphaess_relay.diagnostics import async_get_config_entry_diagnostics

    async with cloud() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        async with device(entry) as (_, writer):
            await peers.get()
            writer.write(wire(Password="DONT-EXPOSE", WifiPassword="DONT-EXPOSE"))
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "50.4")
            diagnostic = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
            assert "DONT-EXPOSE" not in diagnostic
            assert SERIAL not in diagnostic
            assert "Time" not in diagnostic


async def test_ha_stop_closes_active_connection_and_timers(hass, dns):
    async with cloud() as (port, peers):
        entry = make_entry(hass)
        dns["port"] = port
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        async with device(entry) as (reader, writer):
            await peers.get()
            writer.write(wire())
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "50.4")
            await hass.async_stop()
            assert coordinator.closed
            assert coordinator._expire_cancel is None
            assert not coordinator.relay.tasks
            assert coordinator.relay.observer.task is None
            assert await asyncio.wait_for(reader.read(), 2) == b""
        assert_port_free(entry.options["listen_port"])


async def test_reload_with_live_traffic_closes_old_session_and_waits_for_new_data(hass, dns):
    async with cloud() as (port, peers), loaded(hass) as entry:
        dns["port"] = port
        previous = entry.runtime_data
        async with device(entry) as (reader, writer):
            await peers.get()
            writer.write(wire())
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "50.4")
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert previous.closed
            assert not previous.relay.tasks
            assert await asyncio.wait_for(reader.read(), 2) == b""
        assert entry.runtime_data is not previous
        assert state(hass, "soc").state == STATE_UNAVAILABLE
        async with device(entry) as (_, writer):
            await peers.get()
            writer.write(wire(soc="54"))
            await writer.drain()
            await eventually(lambda: state(hass, "soc").state == "54.0")
