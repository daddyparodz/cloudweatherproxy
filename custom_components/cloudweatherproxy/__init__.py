"""The Wunderground Receiver integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .aiocloudweather import CloudWeatherListener
from .aiocloudweather.proxy import DataSink
from .aiocloudweather.utils import LimitedSizeQueue, DiagnosticsLogHandler

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_DNS_SERVERS, CONF_WEATHERCLOUD_PROXY, CONF_WUNDERGROUND_PROXY, DOMAIN
from .web import CANCEL_SETTLE_TIMEOUT, ReceiverManager, WeathercloudReceiver, WundergroundReceiver
from .entity import CloudWeatherEntity

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

CLEANUP_TIMEOUT = 5.0


@dataclass
class RuntimeData:
    """Runtime data for Cloud Weather Proxy."""

    listener: CloudWeatherListener
    known_sensors: dict[str, CloudWeatherEntity] = field(default_factory=dict)


CloudWeatherProxyConfigEntry = ConfigEntry[RuntimeData]


@dataclass
class DomainData:
    """Domain-wide data for Cloud Weather Proxy."""

    receiver: ReceiverManager
    log_queue: LimitedSizeQueue
    log_handler: DiagnosticsLogHandler
    entry_id: str | None = None
    cleanup_pending: bool = False
    pending_listener: CloudWeatherListener | None = None
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stopping: bool = False
    setup_complete: asyncio.Future[None] | None = None
    unload_task: asyncio.Task[bool] | None = None
    cleanup_task: asyncio.Task[None] | None = None
    cleanup_deadline: float | None = None
    cleanup_cancel_deadline: float | None = None


async def _async_cleanup_listener(
    domain_data: DomainData,
    listener: CloudWeatherListener,
    cancellation: asyncio.CancelledError | None = None,
    *,
    quarantine_failure: bool = False,
) -> bool:
    """Serialize cleanup owners, including shutdown and a concurrent unload."""
    async with domain_data.cleanup_lock:
        if domain_data.pending_listener is not listener:
            return True
        return await _async_cleanup_listener_owned(
            domain_data, listener, cancellation, quarantine_failure=quarantine_failure
        )


async def _async_cleanup_listener_owned(
    domain_data: DomainData,
    listener: CloudWeatherListener,
    cancellation: asyncio.CancelledError | None = None,
    *,
    quarantine_failure: bool = False,
) -> bool:
    """Bound cleanup and cancellation; keep unfinished tasks owned for retries."""
    loop = asyncio.get_running_loop()
    task = domain_data.cleanup_task
    if task is None:
        task = domain_data.cleanup_task = asyncio.create_task(listener.stop())
        domain_data.cleanup_deadline = loop.time() + CLEANUP_TIMEOUT
        domain_data.cleanup_cancel_deadline = None
    assert domain_data.cleanup_deadline is not None
    while not task.done():
        deadline = domain_data.cleanup_cancel_deadline or domain_data.cleanup_deadline
        remaining = deadline - loop.time()
        if remaining <= 0:
            if domain_data.cleanup_cancel_deadline is not None:
                _LOGGER.error("Listener cleanup did not settle; resources remain quarantined")
                if quarantine_failure:
                    return False
                raise RuntimeError("Listener cleanup remains quarantined")
            domain_data.cleanup_cancel_deadline = loop.time() + CANCEL_SETTLE_TIMEOUT
            task.cancel()
            continue
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError as err:
            cancellation = err
    domain_data.cleanup_task = None
    try:
        task.result()
    except (Exception, asyncio.CancelledError) as err:
        if quarantine_failure:
            _LOGGER.exception("Listener cleanup failed; resources remain quarantined")
            return False
        if cancellation is not None:
            _LOGGER.exception("Listener cleanup failed during cancellation; setup can retry")
            raise cancellation
        if task.cancelled():
            raise RuntimeError("Listener cleanup was cancelled; resources remain quarantined") from err
        raise
    domain_data.cleanup_pending = False
    domain_data.pending_listener = None
    domain_data.entry_id = None
    domain_data.cleanup_deadline = None
    domain_data.cleanup_cancel_deadline = None
    if cancellation is not None and not quarantine_failure:
        raise cancellation
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry) -> bool:
    """Set up Cloud Weather Proxy from a config entry."""

    if len(hass.config_entries.async_entries(DOMAIN)) > 1:
        _LOGGER.error("Multiple Cloud Weather Proxy entries configured; remove duplicates")
        return False
    domain_data: DomainData | None = hass.data.get(DOMAIN)
    if domain_data is not None and domain_data.stopping:
        return False
    if domain_data is not None and domain_data.cleanup_pending:
        if domain_data.unload_task is not None or domain_data.cleanup_lock.locked():
            return False
        domain_data.receiver.accepting = False
        try:
            assert domain_data.pending_listener is not None
            await _async_cleanup_listener(domain_data, domain_data.pending_listener)
        finally:
            domain_data.receiver.accepting = not domain_data.stopping and not domain_data.cleanup_pending
        if domain_data.stopping:
            return False
    if DOMAIN in hass.data and hass.data[DOMAIN].entry_id is not None:
        _LOGGER.error("Only one Cloud Weather Proxy entry can be loaded")
        return False

    proxies = [
        DataSink.WUNDERGROUND] if entry.data[CONF_WUNDERGROUND_PROXY] else []
    proxies += [DataSink.WEATHERCLOUD] if entry.data[CONF_WEATHERCLOUD_PROXY] else []

    dns_servers: list[str] = entry.data[CONF_DNS_SERVERS].split(",")

    _LOGGER.debug("Setting up Cloud Weather Proxy with %s and %s",
                  proxies, dns_servers)
    cloudweather = CloudWeatherListener(
        proxy_sinks=proxies, dns_servers=dns_servers
    )

    # Store per-entry runtime data
    entry.runtime_data = RuntimeData(listener=cloudweather)

    # Initialize domain-wide data on first entry setup
    if DOMAIN not in hass.data:
        # Per-HASS small queue to hold most recent integration logs for diagnostics
        per_hass_queue = LimitedSizeQueue(maxsize=100)

        # Attach integration-scoped diagnostics handler so logs are captured
        # and can be included in the HA diagnostics download.
        handler = DiagnosticsLogHandler(queue=per_hass_queue)
        # Attach to the package logger (custom_components.cloudweatherproxy)
        integration_logger = logging.getLogger(__package__)
        integration_logger.addHandler(handler)

        hass.data[DOMAIN] = DomainData(
            receiver=ReceiverManager(), log_queue=per_hass_queue, log_handler=handler)
        hass.http.register_view(WundergroundReceiver(hass.data[DOMAIN].receiver))
        hass.http.register_view(WeathercloudReceiver(hass.data[DOMAIN].receiver))

        async def shutdown_cleanup(_event) -> None:
            """Join transitions before retiring active or quarantined resources."""
            domain_data = hass.data[DOMAIN]
            domain_data.stopping = True
            receiver = domain_data.receiver
            receiver.begin_drain()
            for task in (domain_data.setup_complete, domain_data.unload_task):
                if task is not None:
                    # The integration call still owns this task. Observe its
                    # completion without cancelling it at HA's stop boundary.
                    await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            listener = domain_data.pending_listener
            if listener is None and receiver.listener is not None:
                await receiver.drain()
                if receiver.in_flight:
                    _LOGGER.error("Requests did not settle; listener remains quarantined")
                    return
                listener = receiver.listener
                receiver.listener = None
                domain_data.pending_listener = listener
                domain_data.cleanup_pending = True
            if listener is not None:
                await _async_cleanup_listener(
                    domain_data, listener, quarantine_failure=True
                )

        # Domain ownership outlives successful entry unload and failed setup.
        # An entry unload callback would disappear before this retry is needed.
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown_cleanup)

    domain_data = hass.data[DOMAIN]
    domain_data.entry_id = entry.entry_id
    complete = hass.loop.create_future()
    domain_data.setup_complete = complete
    try:
        return await _async_finish_setup(hass, entry, domain_data, cloudweather)
    finally:
        complete.set_result(None)
        domain_data.setup_complete = None


async def _async_finish_setup(
    hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry,
    domain_data: DomainData, cloudweather: CloudWeatherListener,
) -> bool:
    """Keep setup resource ownership joinable until binding or rollback ends."""
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException as err:
        # HA handles platform setup rollback; do not retain an unusable listener.
        domain_data = hass.data[DOMAIN]
        domain_data.cleanup_pending = True
        domain_data.pending_listener = cloudweather
        domain_data.receiver.accepting = False
        try:
            await _async_cleanup_listener(
                domain_data, cloudweather,
                err if isinstance(err, asyncio.CancelledError) else None,
            )
        finally:
            domain_data.receiver.accepting = not domain_data.stopping and not domain_data.cleanup_pending
        raise

    domain_data.receiver.listener = cloudweather
    if not domain_data.stopping:
        domain_data.receiver.reset_drain()
    domain_data.receiver.accepting = not domain_data.stopping

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry) -> bool:
    """Unload a config entry."""

    domain_data: DomainData | None = hass.data.get(DOMAIN)
    if domain_data is None or domain_data.entry_id != entry.entry_id:
        return False
    receiver = domain_data.receiver
    if domain_data.unload_task is not None:
        return False
    if domain_data.setup_complete is not None:
        return False
    receiver.begin_drain()
    # HA does not catch CancelledError in ConfigEntry.async_unload. Join the
    # complete transaction and return its outcome so HA can finish its state
    # transition. Unsettled cleanup stays explicitly owned by DomainData.
    task = asyncio.create_task(_async_unload_owned(hass, entry, domain_data))
    domain_data.unload_task = task
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()
    finally:
        domain_data.unload_task = None


async def _async_unload_owned(
    hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry, domain_data: DomainData
) -> bool:
    """Complete an owned unload transaction without caller cancellation."""
    receiver = domain_data.receiver
    try:
        if not domain_data.cleanup_pending:
            await receiver.drain()
            if receiver.in_flight:
                _LOGGER.error("Requests did not settle; listener remains quarantined")
                return False
            if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
                if not domain_data.stopping:
                    receiver.reset_drain()
                receiver.accepting = not domain_data.stopping
                return False
            receiver.listener = None
            domain_data.cleanup_pending = True
            domain_data.pending_listener = entry.runtime_data.listener
        receiver.accepting = False
        # Report cleanup failure honestly: HA retains runtime_data in FAILED_UNLOAD.
        # Domain ownership and the stop callback also survive platform teardown.
        assert domain_data.pending_listener is not None
        return await _async_cleanup_listener(
            domain_data, domain_data.pending_listener, quarantine_failure=True
        )
    except Exception:
        if not domain_data.cleanup_pending:
            if not domain_data.stopping:
                receiver.reset_drain()
            receiver.accepting = not domain_data.stopping
        raise
    finally:
        if domain_data.entry_id is None:
            receiver.accepting = not domain_data.stopping
