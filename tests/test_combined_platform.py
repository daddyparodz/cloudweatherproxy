"""Actual HA platform registration, synthetic data, no live HA access."""
from datetime import timedelta
import logging
from types import SimpleNamespace
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from test_receiver_lifecycle import make_entry
from custom_components.cloudweatherproxy import sensor
from custom_components.cloudweatherproxy.aiocloudweather.server import CloudWeatherListener
from custom_components.cloudweatherproxy.const import DOMAIN


@pytest.mark.parametrize("imperial", [False, True])
async def test_combined_real_platform_renamed_ids_conversions_and_reload(tmp_path, imperial):
    hass = HomeAssistant(str(tmp_path))
    hass.config.units = US_CUSTOMARY_SYSTEM if imperial else METRIC_SYSTEM
    hass.config_entries = ConfigEntries(hass, {})
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    registry = er.async_get(hass)
    entry = make_entry("combined")
    hass.config_entries._entries[entry.entry_id] = entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, "anonymous_station_address": "127.0.0.1", "legacy_anonymous_binding": True})
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "")})
    old = registry.async_get_or_create("sensor", DOMAIN, "-rain", config_entry=entry, device_id=device.id, original_device_class="precipitation", unit_of_measurement="mm", suggested_object_id="weatherstation_rain_last_hour")
    daily = registry.async_get_or_create("sensor", DOMAIN, "-dailyrain", config_entry=entry, device_id=device.id, suggested_object_id="weatherstation_daily_rain")
    listener = CloudWeatherListener()
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN, platform=sensor, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    wu = await listener.process_wunderground({"ID": "synthetic", "rainin": "1", "dailyrainin": "2", "tempf": "68"})
    wc = await listener.process_weathercloud(["wid", "synthetic", "rainrate", "254", "rain", "0", "tempin", "200", "uvi", "0", "solarrad", "1000"])
    for station in (wu, wc):
        station.station_client_ip = "127.0.0.1"
    try:
        for source in ("combined", "wunderground", "combined"):
            hass.config_entries.async_update_entry(entry, data={**entry.data, "sensor_source": source})
            entry.runtime_data = SimpleNamespace(listener=listener, known_sensors={})
            pending = []
            await sensor.async_setup_entry(hass, entry, pending.extend)
            for station in (wc, wu, wc, wu):
                await listener._new_dataset_cb(station)
                await platform.async_add_entities(pending)
                pending.clear()
            assert platform.entities[old.entity_id].unique_id == "-rain"
            assert platform.entities[daily.entity_id].native_value == pytest.approx(50.8)
            assert registry.async_get_entity_id("sensor", DOMAIN, "-rain_1h") is None
            if source == "combined":
                rate = entry.runtime_data.known_sensors["-rain_intensity"]
                assert rate.name == "Rain Rate"
                assert rate.native_unit_of_measurement == "mm/h"
                assert rate.native_value == pytest.approx(25.4)
                assert rate.state == pytest.approx(1 if imperial else 25.4)
                assert rate.unit_of_measurement == ("in/h" if imperial else "mm/h")
                assert platform.entities[old.entity_id].native_unit_of_measurement == "mm"
            await platform.async_reset()
            await entry._async_process_on_unload(hass)
    finally:
        await platform.async_reset()
        await entry._async_process_on_unload(hass)
        await listener.stop()
