"""Explicit combined profile; synthetic loopback observations only."""
import pytest
from test_receiver_lifecycle import receiver, PATHS  # noqa: F401
from test_sensor_sources import reconfigure


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
async def test_combined_fixed_owners(receiver, order):
    hass, entry, client, writes = receiver
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    await reconfigure(receiver, sensor_source="combined", anonymous_station_address="127.0.0.1", legacy_anonymous_binding=True)
    paths = [PATHS[0] + "&humidity=40&rainin=1&dailyrainin=2&indoortempf=104&UV=9&solarradiation=999",
             PATHS[1].replace("/temp/200", "/temp/350") + "/hum/99/tempin/0/humin/0/heat/250/uvi/0/solarrad/10500/rainrate/254/rain/0/thw/999"]
    for index in order * 3:
        assert (await client.get(paths[index])).status == 200
    entities = entry.runtime_data.known_sensors
    expected = {"temperature": 20, "humidity": 40, "rain_1h": 25.4, "dailyrain": 50.8,
                "indoortemperature": 0, "indoorhumidity": 0, "heatindex": 25, "uv": 0,
                "irradiance": 1050, "rain_intensity": 25.4}
    assert set(entities) == {f"-{key}" for key in expected}
    for key, value in expected.items():
        assert entities[f"-{key}"].native_value == pytest.approx(value)
    assert entities["-rain_intensity"].native_unit_of_measurement == "mm/h"
    assert entities["-rain_1h"].native_unit_of_measurement == "mm"


async def test_combined_loaded_proof_and_names(receiver):
    import json
    from custom_components.cloudweatherproxy.diagnostics import async_get_config_entry_diagnostics
    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source="combined", anonymous_station_address="127.0.0.1")
    await client.get(PATHS[0])
    await client.get(PATHS[1] + "/rainrate/0/solarrad/0")
    entities = list(entry.runtime_data.known_sensors.values())
    assert {entity.name for entity in entities} >= {"Rain Rate", "Solar Radiation"}
    proof = (await async_get_config_entry_diagnostics(hass, entry))["sensor_source"]
    assert proof["ownership_implementation"] == "combined.1"
    assert proof["quantity_owners"]["rain_intensity"] == "weathercloud"
    assert proof["protocol_accepted_fields"] == {"wunderground": 1, "weathercloud": 2}
    assert "127.0.0.1" not in json.dumps(proof)


async def test_combined_no_fallback_expiry_invalid_and_wrong_origin(receiver):
    import asyncio
    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source="combined", anonymous_station_address="192.0.2.20", trusted_sensor_proxies="127.0.0.1", sensor_timeout=1)
    headers = {"X-Forwarded-For": "192.0.2.20"}
    await client.get(PATHS[0] + "&humidity=40", headers=headers)
    await client.get(PATHS[1] + "/tempin/200/uvi/0", headers=headers)
    entities = {e.sensor.name: e for e in entry.runtime_data.known_sensors.values()}
    timers = {key: e._expiry for key, e in entities.items()}
    await client.get(PATHS[1] + "/hum/99/tempin/nan/uvi/invalid", headers=headers)
    await client.get(PATHS[0] + "&indoortempf=100&UV=9", headers={"X-Forwarded-For": "192.0.2.21"})
    await client.get(PATHS[0].replace("ID=SYNTHETIC", "ID=collision"), headers=headers)
    assert all(e._expiry is timers[key] for key, e in entities.items())
    await asyncio.sleep(1.1)
    assert all(not e.available for e in entities.values())
    await client.get(PATHS[1] + "/hum/99", headers=headers)
    assert not entities["temperature"].available
    assert entities["humidity"].native_value == 40


async def test_combined_rate_never_reuses_legacy_rain(receiver):
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from custom_components.cloudweatherproxy.const import DOMAIN
    hass, entry, client, writes = receiver
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    await reconfigure(receiver, sensor_source="combined", anonymous_station_address="127.0.0.1", legacy_anonymous_binding=True)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "")})
    er.async_get(hass).async_get_or_create("sensor", DOMAIN, "-rain", config_entry=entry, device_id=device.id, original_device_class="precipitation_intensity", unit_of_measurement="mm/h")
    await client.get(PATHS[1] + "/rainrate/0")
    assert "-rain_intensity" in entry.runtime_data.known_sensors
    assert "-rain" not in entry.runtime_data.known_sensors


def test_combined_configuration_requires_binding():
    import voluptuous as vol
    from custom_components.cloudweatherproxy.config_flow import validate_configuration
    with pytest.raises(vol.Invalid):
        validate_configuration({"sensor_source": "combined", "weatherunderground_proxy": False, "weathercloud_proxy": False})
    result = validate_configuration({"sensor_source": "combined", "anonymous_station_address": "192.0.2.20", "weatherunderground_proxy": False, "weathercloud_proxy": False})
    assert result["sensor_source"] == "combined"
