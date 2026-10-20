"""Registering Cloud Weather Proxy Weather Stations."""

from __future__ import annotations

from dataclasses import fields
import logging
from math import isfinite

from .aiocloudweather.utils import resolve_caster

from .aiocloudweather import CloudWeatherListener, Sensor, WeatherStation

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from homeassistant.const import UnitOfPrecipitationDepth, UnitOfVolumetricFlux

from . import CloudWeatherProxyConfigEntry
from .entity import CloudWeatherEntity, identity_suffix, quantity_key
from .const import DOMAIN, QUANTITY_DESCRIPTIONS
from .source import numeric_address, station_identity
from .aiocloudweather.station import WeatherstationVendor

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Register new weather stations."""
    runtime_data = entry.runtime_data
    cloudweather: CloudWeatherListener = runtime_data.listener

    selected_source = entry.data.get("sensor_source", "wunderground")
    cloudweather.sensor_source = selected_source
    counters = cloudweather.source_counters

    binding = entry.data.get("anonymous_station_address") or None
    if binding:
        binding = numeric_address(binding)
    cloudweather.trusted_sensor_proxies = {
        numeric_address(address.strip())
        for address in entry.data.get("trusted_sensor_proxies", "").split(",")
        if address.strip()
    }
    legacy = entry.data.get("legacy_anonymous_binding", False)
    timeout = entry.data.get("sensor_timeout", 900)
    cloudweather.sensor_settings = {
        "timeout_seconds": timeout,
        "physical_binding_configured": bool(binding),
        "legacy_binding_enabled": legacy,
    }
    claims: dict[tuple[str, str], str | None] = {}
    physical_claims: dict[tuple[str, str], str] = {}

    def cancel_timers():
        for entity in runtime_data.known_sensors.values():
            entity.cancel_expiry()

    entry.async_on_unload(cancel_timers)

    async def _new_dataset(station: WeatherStation) -> None:
        source = "wunderground" if station.vendor == WeatherstationVendor.WUNDERGROUND else "weathercloud"
        if source != selected_source:
            counters["unselected_packets"] += 1
            return
        counters["selected_packets"] += 1
        identity = station_identity(station, binding, legacy)
        if identity is None:
            counters["identity_rejected"] += 1
            return
        claim = (source, station.station_id)
        origin = station.station_client_ip
        physical_claim = (source, identity)
        if (
            claim in claims and claims[claim] != origin
            or physical_claim in physical_claims and physical_claims[physical_claim] != station.station_id
        ):
            counters["collision_rejected"] += 1
            _LOGGER.warning("Sensor identity collision rejected")
            return
        claims[claim] = origin
        physical_claims[physical_claim] = station.station_id
        station.entity_station_id = identity
        known_sensors: dict[str,
                            CloudWeatherEntity] = runtime_data.known_sensors
        new_sensors: list[CloudWeatherEntity] = []

        for field in fields(station):
            field_type = field.type
            caster = resolve_caster(field_type)
            if caster is not Sensor and caster is not None:
                if getattr(caster, "__name__", "") != getattr(Sensor, "__name__", ""):
                    continue
            elif caster is None:
                continue
            sensor: Sensor | None = getattr(station, field.name)
            if sensor is None or not isfinite(sensor.value):
                continue
            quantity = quantity_key(sensor, station)
            description = QUANTITY_DESCRIPTIONS.get(quantity)
            expected_unit = "UV index" if quantity == "uv" else description.native_unit_of_measurement if description else None
            if description is None or sensor.unit != expected_unit:
                continue
            counters["accepted_fields"] += 1
            unique_id = f"{identity}-{identity_suffix(sensor, station)}"
            if sensor.name == "rain" and binding and legacy and "sensor_source" in entry.data:
                # The explicit physical binding and source establish future provenance.
                # Original class (not a user override) and unit must agree in dimension.
                # This does not certify or repair historical samples or statistics.
                registry = er.async_get(hass)
                old_id = registry.async_get_entity_id("sensor", DOMAIN, f"{identity}-rain")
                old = registry.async_get(old_id) if old_id else None
                device = dr.async_get(hass).async_get(old.device_id) if old and old.device_id else None
                units = UnitOfPrecipitationDepth if quantity == "rain_1h" else UnitOfVolumetricFlux
                if (
                    old is not None
                    and old.config_entry_id == entry.entry_id
                    and device is not None
                    and (DOMAIN, identity) in device.identifiers
                    and old.original_device_class == description.device_class
                    and old.unit_of_measurement in units
                ):
                    unique_id = f"{identity}-rain"

            if unique_id in known_sensors:
                known_sensors[unique_id].refresh_expiry(timeout)
                await known_sensors[unique_id].update_sensor(station)
                continue

            meta_name = field.metadata.get("name") or sensor.name
            new_sensor = CloudWeatherEntity(sensor, station, str(meta_name), unique_id=unique_id)
            new_sensor.refresh_expiry(timeout)
            known_sensors[unique_id] = new_sensor
            new_sensors.append(new_sensor)

        if len(new_sensors) > 0:
            _LOGGER.debug("Adding %d sensors", len(new_sensors))
            async_add_entities(new_sensors)

    cloudweather.new_dataset_cb.append(_new_dataset)
    entry.async_on_unload(
        lambda: cloudweather.new_dataset_cb.remove(_new_dataset))
