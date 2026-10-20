"""One action form supports watts, target SoC and duration or absolute end."""
import probatio as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN


def register_services(hass):
    common = {vol.Optional("entry_id"): cv.string}
    start = {**common, vol.Optional("power_w"): vol.Coerce(float),
             vol.Optional("target_soc"): vol.Coerce(float),
             vol.Optional("duration_minutes"): vol.Coerce(float),
             vol.Optional("end_time"): cv.datetime}

    async def handle(call):
        entries = [entry for entry in hass.config_entries.async_entries(DOMAIN)
                   if getattr(entry, "runtime_data", None) is not None
                   and entry.runtime_data.running]
        if call.data.get("entry_id"):
            entries = [entry for entry in entries if entry.entry_id == call.data["entry_id"]]
        if len(entries) != 1:
            raise HomeAssistantError("Select one running AlphaESS Relay integration")
        controller = entries[0].runtime_data.control
        if call.service == "stop":
            await controller.stop()
        else:
            await controller.start(call.service.removeprefix("start_"), **{
                key: value for key, value in call.data.items() if key != "entry_id"})

    for service in ("start_charge", "start_discharge", "stop"):
        hass.services.async_register(DOMAIN, service, handle,
                                     schema=vol.Schema(common if service == "stop" else start))
