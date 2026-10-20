# Generated from alpha_relay/relay.py; edit the canonical source.
"""Full-duplex TCP forwarding with bounded buffers and explicit lifetimes."""
import asyncio
from collections import Counter
from dataclasses import dataclass
import ipaddress
import logging
import math
import re
import time

from .observer import Observer

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelayConfig:
    upstream_host: str
    upstream_port: int
    expected_serial: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 17778
    connect_timeout: float = 5
    write_timeout: float = 10
    idle_timeout: float = 120
    eof_timeout: float = 2
    buffer_bytes: int = 65536
    chunk_bytes: int = 16384
    fallback_enabled: bool = False
    cloud_response_timeout: float = 30
    recovery_interval: float = 300

    def __post_init__(self):
        # An explicit hostname resolves anew for each device connection. No
        # DNS or upstream connection occurs until a device actually connects.
        try:
            ipaddress.ip_address(self.upstream_host)
        except ValueError:
            if not isinstance(self.upstream_host, str) or len(self.upstream_host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in self.upstream_host.rstrip(".").split(".")
            ):
                raise ValueError("Invalid upstream hostname") from None
        ipaddress.ip_address(self.listen_host)
        if not 1 <= self.upstream_port <= 65535 or not 0 <= self.listen_port <= 65535:
            raise ValueError("Invalid relay port")
        if not isinstance(self.expected_serial, str) or not 1 <= len(self.expected_serial) <= 64:
            raise ValueError("An expected serial is required")
        if not isinstance(self.fallback_enabled, bool):
            raise ValueError("Invalid fallback setting")
        for value in (self.connect_timeout, self.write_timeout, self.idle_timeout, self.eof_timeout,
                      self.cloud_response_timeout, self.recovery_interval):
            if not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError("Invalid relay timeout")
        if not 1024 <= self.buffer_bytes <= 1048576 or not 1 <= self.chunk_bytes <= self.buffer_bytes:
            raise ValueError("Invalid relay buffer limits")


class Relay:
    """One configured battery, one upstream, optional local outage responder.

    Telemetry, unknown frames, malformed traffic and server-originated messages
    all use exactly the same opaque forwarding path. Interpretation is optional.
    """
    def __init__(self, config, *, observer=None, task_factory=None):
        self.config = config
        self.task_factory = task_factory or (lambda coro, name: asyncio.create_task(coro, name=name))
        self.observer = observer or Observer(config.expected_serial, task_factory=self.task_factory)
        self.counters = Counter()
        self.server = None
        self.tasks = set()
        self.writers = set()
        self.active = False
        self.closing = False
        self.session_number = 0
        self.last_close_reason = None
        self.mode = "disconnected"
        self._fallback_next = False

    @property
    def address(self):
        if self.server is None:
            raise RuntimeError("Relay is not running")
        return self.server.sockets[0].getsockname()[:2]

    async def start(self):
        if self.server is not None or self.closing:
            raise RuntimeError("Relay cannot be started twice")
        self.observer.start()
        try:
            self.server = await asyncio.start_server(
                self._accept, self.config.listen_host, self.config.listen_port,
                limit=self.config.buffer_bytes, backlog=8)
        except BaseException:
            await self.observer.close()
            raise
        return self

    def _accept(self, reader, writer):
        # Reserve the slot synchronously, including the upstream-connect phase.
        if self.active or self.closing:
            self.counters["rejected_connections"] += 1
            writer.close()
            return
        self.active = True
        self.mode = "connecting"
        self.session_number += 1
        self.observer.latest_session = self.session_number
        self.observer.notify()
        task = self.task_factory(self._session(reader, writer, self.session_number), "alpha-relay-session")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _pump(self, reader, writer, direction, session, activity, pending):
        while True:
            data = await reader.read(self.config.chunk_bytes)
            if not data:
                if writer.can_write_eof():
                    writer.write_eof()
                    await asyncio.wait_for(writer.drain(), self.config.write_timeout)
                return "eof"
            activity[0] = time.monotonic()
            if direction == "upstream":
                if pending[0] is None:
                    pending[0] = time.monotonic()
                # Timestamp/queue receipt before downstream backpressure can
                # delay it. Publication never waits for cloud acknowledgement.
                self.observer.offer(session, data)
            else:
                pending[0] = None
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self.config.write_timeout)
            self.counters[direction + "_bytes"] += len(data)

    async def _idle(self, activity):
        while True:
            await asyncio.sleep(min(1, self.config.idle_timeout / 2))
            if time.monotonic() - activity[0] >= self.config.idle_timeout:
                return "idle_timeout"

    async def _cloud_silence(self, pending):
        while True:
            await asyncio.sleep(min(1, self.config.cloud_response_timeout / 2))
            if pending[0] is not None and time.monotonic() - pending[0] >= self.config.cloud_response_timeout:
                return "cloud_response_timeout"

    async def _local_session(self, reader, writer, session):
        from .standalone import serve_standalone

        self.mode = "local"
        self.observer.connected = True
        self.counters["fallback_sessions"] += 1
        self.observer.notify()
        try:
            async with asyncio.timeout(self.config.recovery_interval):
                return await serve_standalone(reader, writer, session, self.config,
                                              self.observer, self.counters)
        except TimeoutError:
            # Device reconnects through the same static route. Try real cloud
            # first on that new session; never splice streams or replay traffic.
            return "cloud_recovery_due"

    async def _session(self, device_reader, device_writer, session):
        upstream_writer = None
        children = set()
        reason = "shutdown"
        self.counters["accepted_connections"] += 1
        self.writers.add(device_writer)
        try:
            if self.config.fallback_enabled and self._fallback_next:
                self._fallback_next = False
                reason = await self._local_session(device_reader, device_writer, session)
                return
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(self.config.upstream_host, self.config.upstream_port,
                                            limit=self.config.buffer_bytes),
                    self.config.connect_timeout)
            except (OSError, TimeoutError):
                reason = "upstream_unavailable"
                self.counters["upstream_connect_failures"] += 1
                if self.config.fallback_enabled:
                    reason = await self._local_session(device_reader, device_writer, session)
                return
            self.writers.add(upstream_writer)
            for writer in (device_writer, upstream_writer):
                writer.transport.set_write_buffer_limits(high=self.config.buffer_bytes,
                                                         low=self.config.buffer_bytes // 4)
            self.observer.connected = True
            self.mode = "cloud"
            self.observer.notify()
            activity = [time.monotonic()]
            pending = [None]
            # Child errors are expected transport results handled below. Keep
            # them supervised by this session rather than registering each as
            # an independently logged Home Assistant background job.
            up = asyncio.create_task(self._pump(device_reader, upstream_writer, "upstream", session, activity, pending), name="alpha-relay-upstream")
            down = asyncio.create_task(self._pump(upstream_reader, device_writer, "downstream", session, activity, pending), name="alpha-relay-downstream")
            idle = asyncio.create_task(self._idle(activity), name="alpha-relay-idle")
            children = {up, down, idle}
            silence = None
            if self.config.fallback_enabled:
                silence = asyncio.create_task(self._cloud_silence(pending), name="alpha-relay-cloud-silence")
                children.add(silence)
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            # Retrieve every completed task exception, even simultaneous failures.
            errors = [t.exception() for t in done if not t.cancelled() and t.exception() is not None]
            if errors:
                raise errors[0]
            if down in done and up not in done:
                self._fallback_next = self.config.fallback_enabled
            if silence in done:
                reason = "cloud_response_timeout"
                self._fallback_next = True
            elif idle in done:
                reason = "idle_timeout"
            else:
                remaining = [t for t in (up, down) if t not in done]
                reason = "eof"
                if remaining:
                    try:
                        await asyncio.wait_for(asyncio.gather(*remaining), self.config.eof_timeout)
                    except TimeoutError:
                        reason = "eof_timeout"
        except TimeoutError:
            reason = "write_timeout"
        except (ConnectionError, OSError):
            reason = "connection_error"
        except asyncio.CancelledError:
            reason = "shutdown"
            raise
        except Exception:
            reason = "internal_error"
            self.counters["internal_errors"] += 1
        finally:
            if self.config.fallback_enabled and self.mode == "cloud" and reason in (
                    "write_timeout", "connection_error", "eof_timeout"):
                self._fallback_next = True
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            for writer in (device_writer, upstream_writer):
                if writer is not None:
                    writer.close()
            for writer in (device_writer, upstream_writer):
                if writer is not None:
                    try:
                        await asyncio.wait_for(writer.wait_closed(), 1)
                    except (OSError, TimeoutError):
                        writer.transport.abort()
                    self.writers.discard(writer)
            self.observer.connected = False
            self.mode = "disconnected"
            self.active = False
            self.last_close_reason = reason
            self.counters["closed_connections"] += 1
            self.observer.notify()
            # Fixed enum only. Raw traffic/credentials/exceptions are never logged.
            LOG.info("session_closed reason=%s", reason)

    def snapshot(self):
        return {"relay_running": self.server is not None and not self.closing,
                "session_active": self.active,
                "upstream_connected": self.observer.connected and self.mode == "cloud",
                "local_connected": self.observer.connected and self.mode == "local",
                "mode": self.mode, "fallback_enabled": self.config.fallback_enabled,
                "last_close_reason": self.last_close_reason,
                "counters": dict(self.counters), **self.observer.snapshot()}

    async def close(self):
        self.closing = True
        server, self.server = self.server, None
        if server:
            server.close()
        # Python 3.13 Server.wait_closed() also waits for active transports.
        # Close sessions before awaiting the server, otherwise an idle live
        # device/cloud pair can deadlock graceful shutdown indefinitely.
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            await server.wait_closed()
        await self.observer.close()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.close()
