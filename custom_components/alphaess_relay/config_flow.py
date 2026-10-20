"""Local configuration only: no device, cloud, credentials or router probes."""
import ipaddress
import re

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section

from .const import (CONF_FALLBACK_ENABLED, CONF_LISTEN_HOST, CONF_LISTEN_PORT, CONF_SERIAL,
                    DEFAULT_FALLBACK_ENABLED, DEFAULT_LISTEN_HOST, DEFAULT_LISTEN_PORT, DOMAIN)


def normalize_serial(value):
    if not isinstance(value, str):
        raise ValueError("Invalid serial")
    serial = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,63}", serial):
        raise ValueError("Invalid serial")
    return serial


def listener_schema(values):
    schema = {
        vol.Required(CONF_LISTEN_PORT, default=values.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT)):
            vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
        vol.Required(CONF_LISTEN_HOST, default=values.get(CONF_LISTEN_HOST, DEFAULT_LISTEN_HOST)): str,
        vol.Required(CONF_FALLBACK_ENABLED, default=values.get(
            CONF_FALLBACK_ENABLED, DEFAULT_FALLBACK_ENABLED)): bool,
    }
    key = vol.Optional("inverter_rating_w", **({"default": values["inverter_rating_w"]}
                                              if "inverter_rating_w" in values else {}))
    schema[key] = vol.All(vol.Coerce(int), vol.Range(min=100, max=30000))
    return schema


def validate_listener(values):
    address = ipaddress.IPv4Address(values.get(CONF_LISTEN_HOST, DEFAULT_LISTEN_HOST))
    if address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError("Invalid bind address")
    port = int(values.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT))
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid bind port")
    fallback = values.get(CONF_FALLBACK_ENABLED, DEFAULT_FALLBACK_ENABLED)
    if not isinstance(fallback, bool):
        raise ValueError("Invalid fallback setting")
    result = {CONF_LISTEN_HOST: str(address), CONF_LISTEN_PORT: port,
              CONF_FALLBACK_ENABLED: fallback}
    if "inverter_rating_w" in values:
        value = values["inverter_rating_w"]
        if isinstance(value, bool) or not 100 <= int(value) <= 30000 or float(value) != int(value):
            raise ValueError("Invalid inverter rating")
        result["inverter_rating_w"] = int(value)
    return result


def listener_conflicts(hass, values, *, exclude=None):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude:
            continue
        existing = {**entry.data, **entry.options}
        if existing.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT) != values[CONF_LISTEN_PORT]:
            continue
        old_host = existing.get(CONF_LISTEN_HOST, DEFAULT_LISTEN_HOST)
        if old_host == values[CONF_LISTEN_HOST] or "0.0.0.0" in (old_host, values[CONF_LISTEN_HOST]):
            return True
    return False


class AlphaESSRelayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                serial = normalize_serial(user_input[CONF_SERIAL])
            except (ValueError, TypeError, KeyError):
                errors[CONF_SERIAL] = "invalid_serial"
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                try:
                    listener = validate_listener(user_input.get("listener", {}))
                except (ValueError, TypeError):
                    errors["base"] = "invalid_listener"
                else:
                    if listener_conflicts(self.hass, listener):
                        errors["base"] = "listener_in_use"
                    else:
                        return self.async_create_entry(
                            title=f"AlphaESS {serial}", data={CONF_SERIAL: serial}, options=listener
                        )
        listener_defaults = {
            CONF_LISTEN_HOST: DEFAULT_LISTEN_HOST,
            CONF_LISTEN_PORT: DEFAULT_LISTEN_PORT,
            CONF_FALLBACK_ENABLED: DEFAULT_FALLBACK_ENABLED,
            **(user_input or {}).get("listener", {}),
        }
        schema = {
            vol.Required(CONF_SERIAL): str,
            vol.Optional("listener", default=listener_defaults): section(
                vol.Schema(listener_schema(listener_defaults)),
                {"collapsed": True},
            ),
        }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return RelayOptionsFlow()


class RelayOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                listener = validate_listener(user_input)
            except (ValueError, TypeError):
                errors["base"] = "invalid_listener"
            else:
                if listener_conflicts(self.hass, listener, exclude=self.config_entry.entry_id):
                    errors["base"] = "listener_in_use"
                else:
                    return self.async_create_entry(title="", data=listener)
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(listener_schema(self.config_entry.options)), errors=errors
        )
