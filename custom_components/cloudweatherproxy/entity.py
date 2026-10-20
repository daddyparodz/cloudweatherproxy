"""Cloud Weather Proxy Entity definitions."""

from __future__ import annotations

import asyncio
import logging

from .aiocloudweather import Sensor, WeatherStation

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, QUANTITY_DESCRIPTIONS

_LOGGER = logging.getLogger(__name__)


def quantity_key(sensor: Sensor, station: WeatherStation) -> str:
    """Describe source semantics independently of compatibility identities."""
    source = "wunderground" if station.vendor.name == "WUNDERGROUND" else "weathercloud"
    if sensor.name == "rain":
        return "rain_1h" if source == "wunderground" else "rain_intensity"
    if sensor.name in ("dailyrain", "weeklyrain", "monthlyrain"):
        return f"{source}_{sensor.name}"
    if sensor.name == "solarradiation":
        return "irradiance"
    return sensor.name


def identity_suffix(sensor: Sensor, station: WeatherStation) -> str:
    """Preserve upstream counter IDs without letting IDs select metadata.

    A legacy ``rain`` record is not evidence of a single physical quantity:
    historical versions used it for both depth and intensity. Keep the safe
    semantic namespace unless independent provenance can establish its type.
    """
    if sensor.name in ("dailyrain", "weeklyrain", "monthlyrain"):
        # Physical identity is resolved before this point. Shared depth units
        # do not imply equal counter values/reset times when source changes;
        # never synthesize offsets or attempt history repair here.
        return sensor.name
    return quantity_key(sensor, station)


class CloudWeatherBaseEntity(Entity):
    """The Cloud Weather Proxy Entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    sensor: Sensor | None
    station: WeatherStation

    def __init__(self, sensor: Sensor, station: WeatherStation) -> None:
        """Construct the entity."""
        self.sensor = sensor
        self.station = station

        identity = station.entity_station_id if station.entity_station_id is not None else station.station_id
        suffix = identity_suffix(sensor, station)
        self._attr_unique_id = f"{identity}-{suffix}"
        self._attr_device_info = DeviceInfo(
            name="Weatherstation",
            identifiers={(DOMAIN, identity)},
            sw_version=station.station_sw_version,
        )
        self._attr_available = True
        self._expiry: asyncio.TimerHandle | None = None


class CloudWeatherEntity(CloudWeatherBaseEntity, SensorEntity):
    """The Cloud Weather Proxy Sensor Entity."""

    def __init__(
        self,
        sensor: Sensor,
        station: WeatherStation,
        name: str,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(sensor, station)
        self._attr_name = name
        self._attr_native_value = sensor.value if sensor else None
        self.entity_description = QUANTITY_DESCRIPTIONS[quantity_key(sensor, station)]
        semantic_names = {
            "rain_1h": "Rain past 60 minutes",
            "rain_intensity": "Rain intensity",
            "irradiance": "Solar irradiance",
        }
        self._attr_name = semantic_names.get(quantity_key(sensor, station), name)
        if sensor.name == "dailyrain":
            self._attr_name = "Daily Rain"

    def cancel_expiry(self) -> None:
        """Release the per-field timer on unload/removal."""
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None

    def refresh_expiry(self, timeout: float) -> None:
        """Only a fresh selected-source field may renew availability."""
        self.cancel_expiry()
        self._attr_available = True

        def expire() -> None:
            self._expiry = None
            self._attr_available = False
            self.async_write_ha_state()

        self._expiry = asyncio.get_running_loop().call_later(timeout, expire)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the field timer when HA removes this entity."""
        self.cancel_expiry()
        await super().async_will_remove_from_hass()

    async def update_sensor(self, station: WeatherStation) -> None:
        """Update the entity."""
        self.station = station
        old_name = self.sensor.name if self.sensor is not None else None
        if old_name is None:
            return
        new_sensor = getattr(station, old_name, None)
        self.sensor = new_sensor

        self._attr_native_value = self.sensor.value if self.sensor else None

        _LOGGER.debug("Updating %s [%s] with update time %s",
                      self.unique_id, self.sensor, self.station.update_time)
        self.async_write_ha_state()
