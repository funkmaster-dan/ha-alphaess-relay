"""Inputs for the next explicit charge/discharge action; never start a command."""
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.entity import EntityCategory

from .entity import RelayEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DispatchNumber(entry.runtime_data, direction, key)
                        for direction in ("charge", "discharge")
                        for key in ("power_w", "target_soc", "duration_minutes")])


class DispatchNumber(RelayEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, direction, key):
        self.direction, self.key = direction, f"{direction}_{key}"
        super().__init__(coordinator, self.key)
        self._attr_translation_key = self.key
        self._attr_native_min_value = {"power_w": 100, "target_soc": 5, "duration_minutes": 1.5}[key]
        self._maximum = {"power_w": None, "target_soc": 100, "duration_minutes": 180}[key]
        self._attr_native_step = 0.5 if key == "duration_minutes" else 1
        self._attr_native_unit_of_measurement = {"power_w": "W", "target_soc": "%", "duration_minutes": "min"}[key]

    @property
    def available(self):
        return self.coordinator.running and (self._maximum is not None or
            self.coordinator.relay.control_state.nominal_power_w is not None)

    @property
    def native_max_value(self):
        return self._maximum or self.coordinator.relay.control_state.nominal_power_w or 100

    @property
    def native_value(self):
        return self.coordinator.control.settings[self.key]

    async def async_set_native_value(self, value):
        await self.coordinator.control.set_setting(self.key, value)
