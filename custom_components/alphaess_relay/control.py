"""Confirmed dispatch actions, persisted inputs and bounded local deadlines."""
import asyncio
from datetime import datetime, timezone
import math
import time
from zoneinfo import ZoneInfo

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from ._relay.control import ControlError, Dispatch, integer
from .const import DOMAIN

DEFAULTS = {f"{direction}_{key}": value for direction in ("charge", "discharge")
            for key, value in (("power_w", 500), ("duration_minutes", 5),
                               ("target_soc", 100 if direction == "charge" else 5))}


class DispatchController:
    def __init__(self, coordinator, hass, entry):
        self.coordinator, self.hass, self.entry = coordinator, hass, entry
        self.settings = dict(DEFAULTS)
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.controls")
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.timer = None
        self.owned = None
        self.last_error = None
        self.closed = False
        self.cloud_revision = 0
        self.saved_nominal = None
        self.confirmation_timeout = 25

    @property
    def relay(self):
        return self.coordinator.relay

    async def async_load(self):
        stored = await self.store.async_load() or {}
        for key, value in stored.get("settings", {}).items():
            if key in self.settings:
                try:
                    self.settings[key] = self.validate_setting(key, value)
                except (ControlError, ValueError, TypeError):
                    pass
        nominal = stored.get("nominal_power_w") or self.entry.options.get("inverter_rating_w")
        if nominal is not None:
            try:
                self.relay.control_state.nominal_power_w = integer(nominal, 1, 30000)
            except ControlError:
                pass
        self.saved_nominal = self.relay.control_state.nominal_power_w

    def stored_data(self):
        return {"settings": dict(self.settings),
                "nominal_power_w": self.relay.control_state.nominal_power_w}

    def validate_setting(self, key, value):
        if isinstance(value, bool):
            raise ControlError("Invalid setting")
        value = float(value)
        if not math.isfinite(value):
            raise ControlError("Invalid setting")
        if key.endswith("power_w"):
            return integer(value, 100, 30000)
        if key.endswith("target_soc"):
            return integer(value, 5, 100)
        if not 1.5 <= value <= 180:
            raise ControlError("Duration must be between 90 seconds and 3 hours")
        return value

    async def set_setting(self, key, value):
        try:
            value = self.validate_setting(key, value)
            if key.endswith("power_w"):
                maximum = self.relay.control_state.nominal_power_w
                if maximum is None or value > maximum:
                    raise ControlError("Requested power exceeds the current device limit")
            self.settings[key] = value
            await self.store.async_save(self.stored_data())
            self.coordinator._handle_update()
        except (ControlError, ValueError, TypeError) as error:
            raise HomeAssistantError(str(error)) from None

    def state_updated(self):
        self.changed.set()
        state = self.relay.control_state
        if state.cloud_revision != self.cloud_revision:
            self.cloud_revision = state.cloud_revision
            if self.owned:
                self.owned = None
                self._cancel_timer()
                self.last_error = "Superseded by a cloud control command"
        if state.nominal_power_w != self.saved_nominal:
            self.saved_nominal = state.nominal_power_w
            self.store.async_delay_save(self.stored_data, 1)

    def _cancel_timer(self):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None

    def available(self, direction=None):
        state = self.relay.control_state
        return bool(not self.closed and self.relay.control_channel
                    and self.relay.control_channel.ready and state.fresh()
                    and self.relay.observer.snapshot()["available"]
                    and (direction is None or (state.maximum_power(direction) or 0) >= 100))

    def active_direction(self):
        state = self.relay.control_state
        value = state.dispatch if state.fresh() else None
        if not value or not value["enabled"] or value["mode"] != 2:
            return None
        return "discharge" if value["power_w"] > 0 else "charge" if value["power_w"] < 0 else None

    async def _issue(self, command):
        channel = self.relay.control_channel
        if not channel or not channel.ready:
            raise ControlError("Device control connection unavailable")
        state = self.relay.control_state
        revision, cloud_revision = state.revision, state.cloud_revision
        await channel.request(command)
        # Success ACK alone does not prove state changed. Require a newer
        # device report with the requested mode, signed watts and SoC encoding.
        async with asyncio.timeout(self.confirmation_timeout):
            while True:
                self.changed.clear()
                if state.cloud_revision != cloud_revision:
                    raise ControlError("Superseded by a cloud control command")
                if self.relay.control_channel is not channel:
                    raise ControlError("Device disconnected before confirmation")
                value = state.dispatch if state.fresh() else None
                if value and state.revision > revision:
                    if not command.enabled and not value["enabled"]:
                        return
                    sign = 1 if command.direction == "discharge" else -1
                    if (command.enabled and value["enabled"] and value["mode"] == 2
                            and value["power_w"] == sign * command.power_w
                            and value["target_soc"] == command.reported_soc):
                        return
                await self.changed.wait()

    async def start(self, direction, *, power_w=None, target_soc=None,
                    duration_minutes=None, end_time=None):
        async with self.lock:
            try:
                if not self.available(direction):
                    raise ControlError("Waiting for fresh device status and a verified inverter rating")
                if duration_minutes is not None and end_time is not None:
                    raise ControlError("Choose a duration or an end time")
                power = self.validate_setting(f"{direction}_power_w", power_w if power_w is not None
                                              else self.settings[f"{direction}_power_w"])
                target = self.validate_setting(f"{direction}_target_soc", target_soc if target_soc is not None
                                               else self.settings[f"{direction}_target_soc"])
                if power > self.relay.control_state.maximum_power(direction):
                    raise ControlError("Requested power exceeds the current device limit")
                if end_time is not None:
                    end = datetime.fromisoformat(end_time) if isinstance(end_time, str) else end_time
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=ZoneInfo(self.hass.config.time_zone))
                    seconds = math.floor((end - datetime.now(timezone.utc)).total_seconds())
                else:
                    minutes = self.validate_setting(f"{direction}_duration_minutes", duration_minutes
                        if duration_minutes is not None else self.settings[f"{direction}_duration_minutes"])
                    seconds = math.floor(minutes * 60)
                command = Dispatch(direction, power, target, seconds)
                command.parameters()
                soc = self.relay.observer.reading.soc_pct
                effective_target = int(command.parameters()["Parameter2"].split(",")[2]) * 0.4
                if (direction == "discharge" and effective_target >= soc) or (direction == "charge" and effective_target <= soc):
                    raise ControlError("Target SoC must be below current SoC for discharge, above it for charge")
                current = self.relay.control_state.dispatch
                if current["enabled"] and current["mode"] != 2:
                    raise ControlError("Another dispatch mode is active; it cannot be cancelled by this control")
                if self.active_direction() not in (None, direction):
                    await self._issue(Dispatch(direction, 1, 5, 90, enabled=False))
                self._cancel_timer()
                self.owned = {"direction": direction, "power_w": power, "target_soc": target,
                              "end_time": datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()}
                self.timer = self.entry.async_create_background_task(
                    self.hass, self._expire(seconds), f"{DOMAIN}:dispatch-expiry")
                self.last_error = None
                await self._issue(command)
            except (ControlError, TimeoutError, ValueError, TypeError) as error:
                self.last_error = str(error) if not isinstance(error, TimeoutError) else "Device readback not confirmed; do not retry blindly"
                raise HomeAssistantError(self.last_error) from None
            finally:
                self.coordinator._handle_update()

    async def stop(self, direction=None):
        async with self.lock:
            try:
                if not self.available():
                    raise ControlError("Waiting for fresh device status to confirm stop")
                active = self.active_direction()
                current = self.relay.control_state.dispatch
                if direction and active and active != direction:
                    return  # Turning off an inactive switch must not stop the other direction.
                if current["enabled"] and current["mode"] != 2:
                    raise ControlError("Another dispatch mode is active; no universal stop is assumed")
                if current["enabled"]:
                    await self._issue(Dispatch(active or direction or "discharge", 1, 5, 90, enabled=False))
                self.owned = None
                self._cancel_timer()
                self.last_error = None
            except (ControlError, TimeoutError) as error:
                self.last_error = str(error) if not isinstance(error, TimeoutError) else "Stop readback not confirmed"
                raise HomeAssistantError(self.last_error) from None
            finally:
                self.coordinator._handle_update()

    async def _expire(self, seconds):
        await asyncio.sleep(seconds)
        if not self.owned:
            return
        try:
            await self.stop(self.owned["direction"])
        except HomeAssistantError:
            # The device also received the finite native duration. Never retry
            # an ambiguous physical command automatically.
            pass

    async def async_close(self):
        if self.owned and self.available() and self.active_direction() == self.owned["direction"]:
            try:
                await asyncio.wait_for(self.stop(self.owned["direction"]), 3)
            except (HomeAssistantError, TimeoutError):
                pass
        self.closed = True
        task = self.timer
        self._cancel_timer()
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
