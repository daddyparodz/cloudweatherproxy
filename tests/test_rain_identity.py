"""Conservative upstream identities, using synthetic observations only."""
from datetime import timedelta
import logging
from types import SimpleNamespace

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from test_receiver_lifecycle import make_entry
from custom_components.cloudweatherproxy import sensor
from custom_components.cloudweatherproxy.aiocloudweather.server import CloudWeatherListener
from custom_components.cloudweatherproxy.const import DOMAIN


@pytest.mark.parametrize("source", ["wunderground", "weathercloud"])
@pytest.mark.parametrize("legacy_unit,legacy_class,legacy_state_class", [
    ("mm", "precipitation", "total_increasing"),
    ("mm/h", "precipitation_intensity", "measurement"),
    ("in", "precipitation", "measurement"),
    ("mm/h", "precipitation", "measurement"),
    ("mm", "precipitation_intensity", "measurement"),
    (None, None, None),
])
async def test_real_platform_reattaches_upstream_counter_and_preserves_custom_name(
    tmp_path, source, legacy_unit, legacy_class, legacy_state_class
):
    """Real HA registration must reuse the legacy record, not just write states."""
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    registry = er.async_get(hass)
    entry = make_entry(source)
    hass.config_entries._entries[entry.entry_id] = entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, "anonymous_station_address": "127.0.0.1", "legacy_anonymous_binding": True})
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "")}, name="Custom station")
    old = {}
    suffixes = ("dailyrain", "weeklyrain", "monthlyrain") if source == "wunderground" else ("dailyrain",)
    legacy_rain = registry.async_get_or_create(
        "sensor", DOMAIN, "-rain", config_entry=entry, device_id=device.id,
        original_device_class=legacy_class, unit_of_measurement=legacy_unit,
        capabilities={"state_class": legacy_state_class},
    )
    legacy_rain = registry.async_update_entity(legacy_rain.entity_id, name="My Rain")
    for suffix in suffixes:
        record = registry.async_get_or_create("sensor", DOMAIN, f"-{suffix}", config_entry=entry, device_id=device.id, suggested_object_id=f"kept_{suffix}")
        custom_name = "Daily Rain" if suffix == "dailyrain" else f"My {suffix}"
        old[suffix] = registry.async_update_entity(record.entity_id, name=custom_name)
    listener = CloudWeatherListener()
    entry.runtime_data = SimpleNamespace(listener=listener, known_sensors={})
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN, platform=sensor, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    pending = []
    await sensor.async_setup_entry(hass, entry, pending.extend)
    if source == "wunderground":
        station = await listener.process_wunderground({"ID": "example", "PASSWORD": "", "rainin": "0.5", "dailyrainin": "1", "weeklyrainin": "2", "monthlyrainin": "3"})
    else:
        station = await listener.process_weathercloud(["wid", "example", "key", "", "rainrate", "125", "rain", "250"])
    station.station_client_ip = "127.0.0.1"
    try:
        await listener._new_dataset_cb(station)
        await platform.async_add_entities(pending)
        for suffix, record in old.items():
            assert registry.async_get_entity_id("sensor", DOMAIN, f"-{suffix}") == record.entity_id
            entity = platform.entities[record.entity_id]
            assert entity.unique_id == f"-{suffix}"
            assert entity.registry_entry.name == record.name
            assert entity.registry_entry.device_id == device.id
            state = hass.states.get(record.entity_id)
            assert state is not None
            assert state.attributes["device_class"] == "precipitation"
            assert state.attributes["state_class"] == "total_increasing"
            assert entity.native_unit_of_measurement == "mm"
            if suffix == "dailyrain":
                assert entity.name == "Daily Rain"
        assert len(platform.entities) == len(suffixes) + 1
        compatible = (
            source == "wunderground" and legacy_unit in ("mm", "in") and legacy_class == "precipitation"
            or source == "weathercloud" and legacy_unit == "mm/h" and legacy_class == "precipitation_intensity"
        )
        quantity = "rain_1h" if source == "wunderground" else "rain_intensity"
        suffix = "rain" if compatible else quantity
        rain = entry.runtime_data.known_sensors[f"-{suffix}"]
        if compatible:
            assert rain.entity_id == legacy_rain.entity_id
            assert rain.registry_entry.name == "My Rain"
            assert rain.registry_entry.device_id == device.id
            assert registry.async_get_entity_id("sensor", DOMAIN, f"-{quantity}") is None
        else:
            assert registry.async_get(legacy_rain.entity_id) is legacy_rain
            assert legacy_rain.entity_id not in platform.entities
        assert rain.state_class == "measurement"
        assert rain.native_unit_of_measurement == ("mm" if source == "wunderground" else "mm/h")
        assert rain.device_class == ("precipitation" if source == "wunderground" else "precipitation_intensity")
        if compatible:
            # Removing obsolete parallel records must be durable, not disabling them.
            for obsolete_suffix in (quantity, f"{source}_dailyrain"):
                obsolete = registry.async_get_or_create(
                    "sensor", DOMAIN, f"-{obsolete_suffix}", config_entry=entry, device_id=device.id
                )
                registry.async_remove(obsolete.entity_id)
            for _ in range(3):
                await platform.async_reset()
                await entry._async_process_on_unload(hass)
                entry.runtime_data = SimpleNamespace(listener=listener, known_sensors={})
                pending.clear()
                await sensor.async_setup_entry(hass, entry, pending.extend)
                await listener._new_dataset_cb(station)
                await platform.async_add_entities(pending)
                pending.clear()
                await listener._new_dataset_cb(station)
                assert not pending  # Natural updates never add a parallel entity.
                assert platform.entities[legacy_rain.entity_id].unique_id == "-rain"
                assert registry.async_get_entity_id("sensor", DOMAIN, f"-{quantity}") is None
                assert registry.async_get_entity_id("sensor", DOMAIN, f"-{source}_dailyrain") is None
                assert len(platform.entities) == len(suffixes) + 1
    finally:
        await platform.async_reset()
        await entry._async_process_on_unload(hass)
        await listener.stop()
