"""Charge/discharge switches reflect device readback, never optimistic ACKs."""
from homeassistant.components.switch import SwitchEntity

from .entity import RelayEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DispatchSwitch(entry.runtime_data, direction)
                        for direction in ("charge", "discharge")])


class DispatchSwitch(RelayEntity, SwitchEntity):
    def __init__(self, coordinator, direction):
        super().__init__(coordinator, f"force_{direction}")
        self.direction = direction
        self._attr_translation_key = f"force_{direction}"
        self._attr_icon = "mdi:battery-arrow-up" if direction == "charge" else "mdi:battery-arrow-down"

    @property
    def available(self):
        control = self.coordinator.control
        return control.available() and (control.active_direction() == self.direction
                                        or control.available(self.direction))

    @property
    def is_on(self):
        return self.coordinator.control.active_direction() == self.direction

    @property
    def extra_state_attributes(self):
        control = self.coordinator.control
        return {"requested_power_w": control.settings[f"{self.direction}_power_w"],
                "target_soc": control.settings[f"{self.direction}_target_soc"],
                "duration_minutes": control.settings[f"{self.direction}_duration_minutes"],
                "available_power_w": control.relay.control_state.maximum_power(self.direction),
                "active_request": control.owned if control.owned and control.owned["direction"] == self.direction else None,
                "last_control_error": control.last_error}

    async def async_turn_on(self, **kwargs):
        await self.coordinator.control.start(self.direction)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.control.stop(self.direction)
