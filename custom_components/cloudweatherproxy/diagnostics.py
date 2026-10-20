"""Simple diagnostics option. To be expanded later."""

from typing import Any
from homeassistant.core import HomeAssistant

from . import CloudWeatherProxyConfigEntry
from .entity import quantity_key
from .const import DOMAIN, CONF_WUNDERGROUND_PROXY, CONF_WEATHERCLOUD_PROXY
from .web import WeathercloudReceiver, WundergroundReceiver


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: CloudWeatherProxyConfigEntry) -> dict[str, Any]:
    """Return config entry diagnostics."""

    runtime_data = entry.runtime_data
    listener = runtime_data.listener
    formatted_sensors = {
        f"sensor_{index}": {
            "quantity": quantity_key(entity.sensor, entity.station),
            "unit": entity.native_unit_of_measurement,
            "available": entity.available,
        }
        for index, entity in enumerate(runtime_data.known_sensors.values(), 1)
        if entity.sensor is not None
    }
    formatted_entry_data = {
        "proxy_wunderground": entry.data.get(CONF_WUNDERGROUND_PROXY, False),
        "proxy_weathercloud": entry.data.get(CONF_WEATHERCLOUD_PROXY, False),
    }

    return {
        "receiver_lifecycle": {
            "implementation": "reloadfix.1",
            "api_version": 1,
            "registered_views": sum(
                route.method == "GET" and route.resource.canonical in (
                    WundergroundReceiver.url, WeathercloudReceiver.url.replace(":.*", "")
                )
                for route in hass.http.app.router.routes()
            ),
            "active": hass.data[DOMAIN].receiver.listener is not None,
            "accepting": hass.data[DOMAIN].receiver.accepting,
            "in_flight": hass.data[DOMAIN].receiver.in_flight,
        },
        "sensor_source": {
            "implementation": "sensorsource.1",
            "selected": listener.sensor_source,
            **listener.sensor_settings,
            "trusted_proxy_count": len(listener.trusted_sensor_proxies),
            **listener.source_counters,
        },
        "known_sensors": formatted_sensors,
        "entry_data": formatted_entry_data,
        "logs": {
            # Historical exception text can contain arbitrary credential formats.
            "recent": [],
            "omitted_for_privacy": True,
        },
    }
