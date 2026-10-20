"""Local instantaneous power, SoC and last-received diagnostic."""
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.helpers.entity import EntityCategory

from .entity import RelayEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    powers = [
        ("battery_io", "battery_power_raw_w"),
        ("generation", "generation_w"),
        ("home_load", "home_load_w"),
        ("grid_io_total", "grid_io_w"),
    ]
    async_add_entities([SoCSensor(coordinator),
                        *(PowerSensor(coordinator, key, field) for key, field in powers),
                        LastReceivedSensor(coordinator)])


class SoCSensor(RelayEntity, SensorEntity):
    _attr_translation_key = "state_of_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator):
        super().__init__(coordinator, "soc")

    @property
    def available(self):
        return self.coordinator.running and bool(self.coordinator.data.get("available"))

    @property
    def native_value(self):
        data = self.coordinator.data.get("telemetry")
        return data["soc_pct"] if data else None


class PowerSensor(RelayEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, key, field):
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self.field = field

    @property
    def available(self):
        data = self.coordinator.data.get("telemetry")
        return (self.coordinator.running and bool(self.coordinator.data.get("available"))
                and data is not None and self.field in data)

    @property
    def native_value(self):
        data = self.coordinator.data.get("telemetry")
        return data.get(self.field) if data else None


class LastReceivedSensor(RelayEntity, SensorEntity):
    _attr_translation_key = "last_received"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "last_received")

    @property
    def available(self):
        return self.coordinator.running

    @property
    def native_value(self):
        data = self.coordinator.data.get("telemetry")
        return datetime.fromisoformat(data["received_at"]) if data else None
