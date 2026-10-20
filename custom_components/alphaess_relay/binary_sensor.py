"""Connection diagnostic independent of live telemetry freshness."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import RelayEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RelayConnected(entry.runtime_data)])


class RelayConnected(RelayEntity, BinarySensorEntity):
    _attr_translation_key = "relay_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "relay_connected")

    @property
    def available(self):
        return self.coordinator.running

    @property
    def is_on(self):
        return bool(self.coordinator.data.get("upstream_connected"))
