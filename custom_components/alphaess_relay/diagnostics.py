"""Diagnostics contain counters and status, not protocol data or serials."""


async def async_get_config_entry_diagnostics(hass, entry):
    data = entry.runtime_data.relay.snapshot()
    # Deliberately omit all telemetry, entry data, addresses, and timestamps.
    return {key: data[key] for key in (
        "relay_running", "session_active", "upstream_connected", "available",
        "last_close_reason", "counters", "observer", "mode", "local_connected", "fallback_enabled",
        "controls", "control_channel_ready"
    )}
