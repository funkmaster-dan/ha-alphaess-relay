"""AlphaESS relay and sensors hosted entirely inside Home Assistant."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the local listener without contacting the battery or cloud."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
    from homeassistant.exceptions import ConfigEntryNotReady

    from .coordinator import RelayCoordinator

    coordinator = RelayCoordinator(hass, entry)
    try:
        await coordinator.async_start()
    except OSError as err:
        await coordinator.async_close()
        raise ConfigEntryNotReady(
            translation_domain="alphaess_relay", translation_key="cannot_listen"
        ) from err
    entry.runtime_data = coordinator
    try:
        # Replace the prototype's raw-power entity with a new battery I/O ID.
        from homeassistant.helpers import entity_registry as er
        from .const import CONF_SERIAL, DOMAIN

        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.platform == DOMAIN and entity.unique_id == f"{entry.data[CONF_SERIAL]}_battery_power_raw":
                registry.async_remove(entity.entity_id)
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform.SENSOR, Platform.BINARY_SENSOR]
        )
    except BaseException:
        await coordinator.async_close()
        raise

    async def stop(_event):
        await coordinator.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.const import Platform

    if await hass.config_entries.async_unload_platforms(
        entry, [Platform.SENSOR, Platform.BINARY_SENSOR]
    ):
        await entry.runtime_data.async_close()
        return True
    return False
