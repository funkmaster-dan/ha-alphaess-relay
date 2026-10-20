"""Push local telemetry into HA; timers only expire cached data."""
import asyncio
import logging

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (CONF_FALLBACK_ENABLED, CONF_LISTEN_HOST, CONF_LISTEN_PORT, CONF_SERIAL,
                    DEFAULT_FALLBACK_ENABLED, DEFAULT_LISTEN_HOST, DEFAULT_LISTEN_PORT,
                    DOMAIN, FRESHNESS_SECONDS, UPSTREAM_HOST, UPSTREAM_PORT)
from ._relay.observer import Observer
from ._relay.relay import Relay, RelayConfig

LOGGER = logging.getLogger(__name__)


class RelayCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN, always_update=False)
        self.serial = entry.data[CONF_SERIAL]
        options = {**entry.data, **entry.options}

        def task_factory(coro, name):
            return entry.async_create_background_task(hass, coro, f"{DOMAIN}:{name}")

        observer = Observer(self.serial, freshness_seconds=FRESHNESS_SECONDS,
                            task_factory=task_factory, run_decoder=hass.async_add_executor_job)
        self.relay = Relay(RelayConfig(
            UPSTREAM_HOST, UPSTREAM_PORT, self.serial,
            listen_host=options.get(CONF_LISTEN_HOST, DEFAULT_LISTEN_HOST),
            listen_port=options.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT),
            fallback_enabled=options.get(CONF_FALLBACK_ENABLED, DEFAULT_FALLBACK_ENABLED),
        ), observer=observer, task_factory=task_factory)
        self._unsubscribe = observer.subscribe(self._handle_update)
        self._expire_cancel = None
        self._close_lock = asyncio.Lock()
        self.closed = False
        self.running = False

    async def async_start(self):
        await self.relay.start()
        self.running = True
        self._handle_update()

    @callback
    def _handle_update(self):
        if self.closed:
            return
        if self._expire_cancel:
            self._expire_cancel()
            self._expire_cancel = None
        data = self.relay.snapshot()
        self.async_set_updated_data(data)
        if data["available"]:
            remaining = max(0.01, self.relay.observer.freshness_seconds - data["age_seconds"] + 0.01)
            self._expire_cancel = async_call_later(self.hass, remaining, self._expire)

    @callback
    def _expire(self, _now):
        self._expire_cancel = None
        self._handle_update()

    async def _async_update_data(self):
        """HA's manual refresh reads cached state; it sends no network request."""
        return self.relay.snapshot()

    async def async_close(self):
        async with self._close_lock:
            if self.closed:
                return
            self.closed = True
            self.running = False
            self._unsubscribe()
            if self._expire_cancel:
                self._expire_cancel()
                self._expire_cancel = None
            await self.relay.close()
            await self.async_shutdown()
