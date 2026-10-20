"""The Wunderground Receiver integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .aiocloudweather import CloudWeatherListener
from .aiocloudweather.proxy import DataSink
from .aiocloudweather.utils import LimitedSizeQueue, DiagnosticsLogHandler

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_DNS_SERVERS, CONF_WEATHERCLOUD_PROXY, CONF_WUNDERGROUND_PROXY, DOMAIN
from .web import ReceiverManager, WeathercloudReceiver, WundergroundReceiver
from .entity import CloudWeatherEntity

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


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


async def _async_cleanup_listener(
    domain_data: DomainData,
    listener: CloudWeatherListener,
    cancellation: asyncio.CancelledError | None = None,
    *,
    quarantine_failure: bool = False,
) -> None:
    """Join resource cleanup even if our caller is cancelled; retain failed ownership."""
    task = asyncio.create_task(listener.stop())
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as err:
            cancellation = err
        except Exception:
            # Retrieve the failure below, including when cancellation raced it.
            break
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        if quarantine_failure:
            _LOGGER.exception("Listener cleanup failed; resources quarantined until next setup")
            return
        if cancellation is not None:
            _LOGGER.exception("Listener cleanup failed during cancellation; setup can retry")
            raise cancellation
        raise
    domain_data.cleanup_pending = False
    domain_data.pending_listener = None
    domain_data.entry_id = None
    if cancellation is not None and not quarantine_failure:
        raise cancellation


async def async_setup_entry(hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry) -> bool:
    """Set up Cloud Weather Proxy from a config entry."""

    if len(hass.config_entries.async_entries(DOMAIN)) > 1:
        _LOGGER.error("Multiple Cloud Weather Proxy entries configured; remove duplicates")
        return False
    domain_data: DomainData | None = hass.data.get(DOMAIN)
    if domain_data is not None and domain_data.cleanup_pending:
        if not domain_data.receiver.accepting:
            return False
        domain_data.receiver.accepting = False
        try:
            assert domain_data.pending_listener is not None
            await _async_cleanup_listener(domain_data, domain_data.pending_listener)
        finally:
            domain_data.receiver.accepting = True
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

    hass.data[DOMAIN].entry_id = entry.entry_id
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
            domain_data.receiver.accepting = True
        raise

    hass.data[DOMAIN].receiver.listener = cloudweather
    hass.data[DOMAIN].receiver.accepting = True

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry) -> bool:
    """Unload a config entry."""

    domain_data: DomainData | None = hass.data.get(DOMAIN)
    if domain_data is None or domain_data.entry_id != entry.entry_id:
        return False
    receiver = domain_data.receiver
    if not receiver.accepting:
        return False
    receiver.accepting = False
    # HA does not catch CancelledError in ConfigEntry.async_unload. Join the
    # complete transaction and return its outcome so HA can finish its state
    # transition. No detached task may outlive this hook.
    task = asyncio.create_task(_async_unload_owned(hass, entry, domain_data))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _async_unload_owned(
    hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry, domain_data: DomainData
) -> bool:
    """Complete an owned unload transaction without caller cancellation."""
    receiver = domain_data.receiver
    try:
        if not domain_data.cleanup_pending:
            await receiver.drain()
            if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
                return False
            receiver.listener = None
            domain_data.cleanup_pending = True
            domain_data.pending_listener = entry.runtime_data.listener
        receiver.accepting = False
        # Platform teardown has committed. HA must receive True, not an exception
        # (FAILED_UNLOAD) or cancellation (UNLOAD_IN_PROGRESS). Failed resources
        # stay owned by DomainData, not runtime_data which HA deletes on success.
        assert domain_data.pending_listener is not None
        await _async_cleanup_listener(
            domain_data, domain_data.pending_listener, quarantine_failure=True
        )
        return True
    finally:
        receiver.accepting = True
