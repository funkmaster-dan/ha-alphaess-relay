"""Verified SoC, raw signed power and last-received diagnostic."""
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.helpers.entity import EntityCategory

from .entity import RelayEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([SoCSensor(coordinator), PowerSensor(coordinator), LastReceivedSensor(coordinator)])


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
    _attr_translation_key = "battery_power_raw"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator):
        super().__init__(coordinator, "battery_power_raw")

    @property
    def available(self):
        data = self.coordinator.data.get("telemetry")
        return (self.coordinator.running and bool(self.coordinator.data.get("available"))
                and data is not None and "battery_power_raw_w" in data)

    @property
    def native_value(self):
        data = self.coordinator.data.get("telemetry")
        return data.get("battery_power_raw_w") if data else None


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
