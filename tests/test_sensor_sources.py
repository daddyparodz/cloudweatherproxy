"""Sensor-source regression tests; all observations are synthetic."""
# ruff: noqa: F811  # Imported pytest fixture names are intentionally injected.
import pytest
from test_receiver_lifecycle import receiver, PATHS  # noqa: F401
from custom_components.cloudweatherproxy import async_setup_entry, async_unload_entry
from custom_components.cloudweatherproxy.const import DOMAIN
from custom_components.cloudweatherproxy.aiocloudweather.server import CloudWeatherListener


async def reconfigure(receiver, **data):
    """Reload the real entry with explicit synthetic configuration."""
    hass, entry, client, writes = receiver
    assert await async_unload_entry(hass, entry)
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        hass.config_entries._entries[entry.entry_id] = entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, **data})
    assert await async_setup_entry(hass, entry)
    writes.clear()
    return entry


async def test_blank_id_requires_explicit_physical_binding(receiver):
    """Verify blank id requires explicit physical binding."""
    hass, entry, client, writes = receiver
    path = "/wunderground/weatherstation/updateweatherstation.php?ID=&PASSWORD=&tempf=68"
    assert (await client.get(path)).status == 200
    assert not writes
    assert not entry.runtime_data.known_sensors
    await reconfigure(receiver, anonymous_station_address="127.0.0.1", legacy_anonymous_binding=True)
    assert (await client.get(path)).status == 200
    entity = next(iter(entry.runtime_data.known_sensors.values()))
    assert entity.unique_id == "-temperature"
    assert entity.device_info["identifiers"] == {("cloudweatherproxy", "")}


async def test_forwarded_identity_requires_explicit_single_hop_trust(receiver):
    """Verify forwarded identity requires explicit single hop trust."""
    hass, entry, client, writes = receiver
    path = "/wunderground/weatherstation/updateweatherstation.php?ID=&PASSWORD=&tempf=68"
    await reconfigure(receiver, anonymous_station_address="192.0.2.20", legacy_anonymous_binding=True)
    assert (await client.get(path, headers={"X-Forwarded-For": "192.0.2.20"})).status == 200
    assert not writes
    await reconfigure(receiver, trusted_sensor_proxies="127.0.0.1")
    for header in ("192.0.2.20, 192.0.2.21", "not-an-address", "192.0.2.21"):
        await client.get(path, headers={"X-Forwarded-For": header})
        assert not writes
    await client.get(path, headers={"X-Forwarded-For": "192.0.2.20"})
    assert writes



@pytest.mark.parametrize("source", ["wunderground", "weathercloud"])
@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
async def test_selected_source_is_not_first_packet_wins(receiver, order, source):
    """Verify selected source is not first packet wins."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source=source)
    for index in order * 3:
        writes.clear()
        assert (await client.get(PATHS[index])).status == 200
        assert bool(writes) is (index == (0 if source == "wunderground" else 1))
    assert len(entry.runtime_data.known_sensors) == 1


async def test_bound_source_change_preserves_only_compatible_quantities(receiver):
    """Verify bound source change preserves only compatible quantities."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, anonymous_station_address="127.0.0.1", legacy_anonymous_binding=True)
    wu = PATHS[0] + "&rainin=1&dailyrainin=2&solarradiation=1050"
    await client.get(wu)
    old = {entity.unique_id: entity for entity in entry.runtime_data.known_sensors.values()}
    assert set(old) == {"-temperature", "-rain_1h", "-wunderground_dailyrain", "-irradiance"}
    assert old["-rain_1h"].native_value == 25.4
    assert old["-rain_1h"].state_class == "measurement"
    await reconfigure(receiver, sensor_source="weathercloud")
    await client.get(PATHS[1] + "/rainrate/200/rain/350/solarrad/10500")
    current = {entity.unique_id: entity for entity in entry.runtime_data.known_sensors.values()}
    assert set(current) == {"-temperature", "-rain_intensity", "-weathercloud_dailyrain", "-irradiance"}
    assert current["-temperature"].native_value == old["-temperature"].native_value == 20
    assert current["-temperature"].device_info == old["-temperature"].device_info
    assert current["-rain_intensity"].native_value == 20
    assert current["-rain_intensity"].device_class == "precipitation_intensity"
    assert current["-rain_intensity"].native_unit_of_measurement == "mm/h"
    assert current["-weathercloud_dailyrain"].native_value == 35
    assert current["-irradiance"].native_value == 1050
    assert current["-irradiance"].native_unit_of_measurement == "W/m²"
    assert current["-irradiance"].device_class == "irradiance"
    assert "-rain" not in current and "-solarradiation" not in current


async def test_field_timer_expires_without_packets_and_ignores_other_source(receiver):
    """Verify field timer expires without packets and ignores other source."""
    import asyncio

    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_timeout=1)
    await client.get(PATHS[0] + "&humidity=40")
    entities = {entity.sensor.name: entity for entity in entry.runtime_data.known_sensors.values()}
    temp, humidity = entities["temperature"], entities["humidity"]
    assert temp.available and humidity.available
    await asyncio.sleep(0.6)
    await client.get(PATHS[0].replace("tempf=68", "tempf=70"))
    await client.get(PATHS[1] + "/hum/90")
    await asyncio.sleep(0.5)
    assert temp.available
    assert not humidity.available
    assert humidity.native_value == 40
    await asyncio.sleep(0.6)
    assert not temp.available
    await client.get(PATHS[0])
    assert temp.available
    assert not humidity.available
    assert await async_unload_entry(hass, entry)
    writes.clear()
    await asyncio.sleep(1.1)
    assert not writes


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "invalid"])
async def test_invalid_field_does_not_create_zero_or_renew(receiver, bad):
    """Verify invalid field does not create zero or renew."""
    hass, entry, client, writes = receiver
    await client.get(PATHS[0].replace("tempf=68", f"tempf={bad}"))
    assert not entry.runtime_data.known_sensors
    await client.get(PATHS[0])
    entity = next(iter(entry.runtime_data.known_sensors.values()))
    writes.clear()
    await client.get(PATHS[0].replace("tempf=68", f"tempf={bad}"))
    assert not writes
    assert entity.native_value == 20


async def test_named_stations_isolated_and_duplicate_origin_rejected(receiver):
    """Verify named stations isolated and duplicate origin rejected."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, trusted_sensor_proxies="127.0.0.1")
    await client.get(PATHS[0], headers={"X-Forwarded-For": "192.0.2.10"})
    first = list(entry.runtime_data.known_sensors.values())
    assert len(first) == 1
    writes.clear()
    await client.get(PATHS[0], headers={"X-Forwarded-For": "192.0.2.11"})
    assert not writes
    assert len(entry.runtime_data.known_sensors) == 1
    await client.get(PATHS[0].replace("ID=SYNTHETIC", "ID=other"), headers={"X-Forwarded-For": "192.0.2.11"})
    assert len(entry.runtime_data.known_sensors) == 2
    # Same literal ID in a different protocol does not imply a physical match.
    old_id = first[0].unique_id
    await reconfigure(receiver, sensor_source="weathercloud")
    await client.get(PATHS[1], headers={"X-Forwarded-For": "192.0.2.10"})
    assert next(iter(entry.runtime_data.known_sensors.values())).unique_id != old_id


async def test_protocol_and_origin_caches_are_not_shared(receiver):
    """Verify protocol and origin caches are not shared."""
    hass, entry, client, writes = receiver
    await client.get(PATHS[0] + "&humidity=45")
    await client.get(PATHS[1])
    values = list(entry.runtime_data.listener.last_values.values())
    assert len(values) == 2
    assert {value.vendor.name for value in values} == {"WUNDERGROUND", "WEATHERCLOUD"}
    assert [value for value in values if value.vendor.name == "WEATHERCLOUD"][0].humidity is None


@pytest.mark.parametrize("imperial", [False, True])
async def test_real_sensorentity_conversions_and_dimension_guards(receiver, imperial):
    """Verify real sensorentity conversions and dimension guards."""
    from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
    from homeassistant.helpers import entity_registry as er
    from custom_components.cloudweatherproxy.entity import CloudWeatherEntity

    hass, entry, client, writes = receiver
    hass.config.units = US_CUSTOMARY_SYSTEM if imperial else METRIC_SYSTEM
    listener = CloudWeatherListener()
    wu = await listener.process_wunderground({"ID": "example", "PASSWORD": "", "tempf": "68", "baromin": "29.92", "windspeedmph": "2.2369362920544"})
    wc = await listener.process_weathercloud(["wid", "example", "key", "", "temp", "200", "bar", "10132", "wspd", "10", "rainrate", "254", "solarrad", "10500"])
    from homeassistant.helpers import device_registry as dr

    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    registry = er.async_get(hass)
    for index, station in enumerate((wu, wc)):
        for field in ("temperature", "barometer", "windspeed"):
            entity = CloudWeatherEntity(getattr(station, field), station, field)
            entity.hass = hass
            entity.registry_entry = registry.async_get_or_create("sensor", DOMAIN, f"conversion_{index}_{field}", get_initial_options=entity.get_initial_entity_options)
            entity._async_read_entity_options()
            expected = {
                "temperature": (68, "°F") if imperial else (20, "°C"),
                "barometer": (14.69538, "psi") if imperial else (1013.2, "hPa"),
                "windspeed": (2.2369362920544, "mph") if imperial else (3.6, "km/h"),
            }[field]
            assert entity.state == pytest.approx(expected[0], abs=0.02)
            assert entity.unit_of_measurement == expected[1]
    for field, forbidden in (("rain", "mm"), ("solarradiation", "lx")):
        entity = CloudWeatherEntity(getattr(wc, field), wc, field)
        entity.hass = hass
        entity.registry_entry = registry.async_get_or_create("sensor", DOMAIN, f"guard_{field}", get_initial_options=lambda: {"sensor": {"unit_of_measurement": forbidden}})
        entity._async_read_entity_options()
        assert entity.unit_of_measurement == entity.native_unit_of_measurement
        if field == "rain":
            entity.registry_entry = registry.async_update_entity_options(entity.registry_entry.entity_id, "sensor", {"unit_of_measurement": "in/h"})
            entity._async_read_entity_options()
            assert entity.state == pytest.approx(1)
            assert entity.native_value == 25.4


async def test_quantity_metadata_rejects_incompatible_native_units(receiver):
    """Verify quantity metadata rejects incompatible native units."""
    from custom_components.cloudweatherproxy.aiocloudweather import Sensor

    hass, entry, client, writes = receiver
    await client.get(PATHS[0] + "&solarradiation=100")
    station = await entry.runtime_data.listener.process_wunderground({"ID": "SYNTHETIC", "PASSWORD": ""})
    station.station_client_ip = "127.0.0.1"
    station.solarradiation = Sensor("solarradiation", 5000, "lx")
    writes.clear()
    await entry.runtime_data.listener._new_dataset_cb(station)
    assert not writes
    irradiance = [entity for entity in entry.runtime_data.known_sensors.values() if entity.sensor.name == "solarradiation"][0]
    assert irradiance.native_value == 100
    assert irradiance.native_unit_of_measurement == "W/m²"


@pytest.mark.parametrize("source", ["wunderground", "weathercloud"])
@pytest.mark.parametrize("flags", [(False, False), (True, False), (False, True), (True, True)])
async def test_forwarding_is_independent_real_loopback_io(receiver, monkeypatch, aiohttp_server, source, flags, caplog):
    """Verify forwarding is independent real loopback io."""
    import logging
    from aiohttp import web
    from yarl import URL

    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source=source, weatherunderground_proxy=flags[0], weathercloud_proxy=flags[1])
    seen = []
    async def upstream(request):
        seen.append(request.query["source"])
        return web.Response(text="synthetic")
    app = web.Application()
    app.router.add_get("/upstream", upstream)
    server = await aiohttp_server(app)
    proxy = entry.runtime_data.listener.proxy
    if proxy:
        original_get = proxy.session.get
        def local_get(url, **kwargs):
            requested = URL(url)
            wire_source = "wunderground" if requested.host == "rtupdate.wunderground.com" else "weathercloud"
            return original_get(server.make_url("/upstream").with_query(source=wire_source), **kwargs)
        monkeypatch.setattr(proxy.session, "get", local_get)
    caplog.set_level(logging.DEBUG, logger="custom_components.cloudweatherproxy")
    for index, path in enumerate(PATHS):
        writes.clear()
        assert (await client.get(path)).status == 200
        assert bool(writes) is (index == (0 if source == "wunderground" else 1))
    assert seen == [name for flag, name in zip(flags, ("wunderground", "weathercloud")) if flag]
    messages = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("custom_components.cloudweatherproxy"))
    assert "PASSWORD=" not in messages and "/key/" not in messages


async def test_diagnostics_loaded_source_marker_and_no_identifiers(receiver, caplog):
    """Verify diagnostics loaded source marker and no identifiers."""
    import json
    from custom_components.cloudweatherproxy.diagnostics import async_get_config_entry_diagnostics

    hass, entry, client, writes = receiver
    await client.get(PATHS[0].replace("SYNTHETIC", "private_station_token").replace("tempf=68", "tempf=private_key_token"))
    await client.get(PATHS[1])
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["sensor_source"]["implementation"] == "sensorsource.1"
    assert result["sensor_source"]["selected"] == "wunderground"
    assert result["sensor_source"]["selected_packets"] == 1
    assert result["sensor_source"]["unselected_packets"] == 1
    assert "private_station_token" not in json.dumps(result)
    assert "private_key_token" not in json.dumps(result)
    assert "private_key_token" not in caplog.text


async def test_explicit_physical_binding_cannot_merge_two_protocol_ids(receiver):
    """Verify explicit physical binding cannot merge two protocol ids."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, anonymous_station_address="127.0.0.1", legacy_anonymous_binding=True)
    await client.get(PATHS[0])
    writes.clear()
    await client.get(PATHS[0].replace("ID=SYNTHETIC", "ID=second-station").replace("tempf=68", "tempf=104"))
    assert not writes
    assert next(iter(entry.runtime_data.known_sensors.values())).native_value == 20


@pytest.mark.parametrize("path", [
    "/wunderground/weatherstation/updateweatherstation.php?humidity=invalid&tempf=nan",
    "/weathercloud/v01/set/temp/nan/hum/invalid",
])
async def test_missing_ids_and_invalid_fields_are_acknowledged_without_entities(receiver, path):
    """Verify missing ids and invalid fields are acknowledged without entities."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source="weathercloud" if "weathercloud/" in path else "wunderground")
    assert (await client.get(path)).status == 200
    assert not writes
    assert not entry.runtime_data.known_sensors


@pytest.mark.parametrize("headers", [
    [("X-Forwarded-For", "192.0.2.20"), ("X-Forwarded-For", "192.0.2.20")],
    {"X-Real-IP": "192.0.2.20"},
    {"Forwarded": "for=192.0.2.20"},
    {"X-Forwarded-For": "192.0.2.20:1234"},
])
async def test_ambiguous_or_alternative_forwarding_headers_never_bind(receiver, headers):
    """Only the explicitly supported single XFF hop can establish provenance."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, trusted_sensor_proxies="127.0.0.1", anonymous_station_address="192.0.2.20", legacy_anonymous_binding=True)
    await client.get(PATHS[0].replace("ID=SYNTHETIC", "ID="), headers=headers)
    assert not writes
    assert entry.runtime_data.listener.source_counters["identity_rejected"] == 1


async def test_legacy_registry_records_and_names_untouched(receiver, monkeypatch):
    """Old incompatible series remain intact; no registry repair is performed."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    hass, entry, client, writes = receiver
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    registry = er.async_get(hass)
    old = [registry.async_get_or_create("sensor", DOMAIN, key) for key in ("-temperature", "-rain", "-solarradiation", "-dailyrain")]
    old = [registry.async_update_entity(item.entity_id, name=f"custom_name_{index}") for index, item in enumerate(old)]
    for method in ("async_remove", "async_update_entity", "async_update_entity_options"):
        def forbidden(*args, **kwargs):
            pytest.fail("Integration attempted automatic registry migration")
        monkeypatch.setattr(registry, method, forbidden)
    await reconfigure(receiver, anonymous_station_address="127.0.0.1", legacy_anonymous_binding=True)
    await client.get(PATHS[0] + "&rainin=1&dailyrainin=2&solarradiation=100")
    for item in old:
        assert registry.async_get(item.entity_id) is item
    ids = set(entry.runtime_data.known_sensors)
    assert "-temperature" in ids
    assert ids.isdisjoint({"-rain", "-solarradiation", "-dailyrain"})
    metadata = {key: (entity.device_class, entity.native_unit_of_measurement, entity.state_class) for key, entity in entry.runtime_data.known_sensors.items()}
    await reconfigure(receiver)
    await client.get(PATHS[0] + "&rainin=0.5&dailyrainin=3&solarradiation=90")
    assert metadata == {key: (entity.device_class, entity.native_unit_of_measurement, entity.state_class) for key, entity in entry.runtime_data.known_sensors.items()}
    for item in old:
        assert registry.async_get(item.entity_id) is item


async def test_raw_nonempty_station_tokens_are_not_normalized(receiver):
    """Whitespace detection for empty IDs must not rewrite valid user tokens."""
    hass, entry, client, writes = receiver
    await client.get(PATHS[0].replace("ID=SYNTHETIC", "ID=%20example%20"))
    await client.get(PATHS[0].replace("ID=SYNTHETIC", "ID=example"))
    assert len(entry.runtime_data.known_sensors) == 2
    assert {entity.station.station_id for entity in entry.runtime_data.known_sensors.values()} == {" example ", "example"}
    writes.clear()
    await client.get(PATHS[0].replace("ID=SYNTHETIC", "ID=%20%20"))
    assert not writes


async def test_undocumented_weathercloud_visibility_has_no_guessed_unit(receiver):
    """Do not promote the legacy unverified vis scaling to a native HA quantity."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source="weathercloud")
    await client.get("/weathercloud/v01/set/wid/example/key/example/vis/100")
    assert not writes
    assert not entry.runtime_data.known_sensors


async def test_fractional_uv_index_is_not_dropped_or_scaled_by_source():
    """UV is a dimensionless index; WC encodes tenths, WU accepts decimals."""
    listener = CloudWeatherListener()
    wu = await listener.process_wunderground({"ID": "example", "PASSWORD": "", "UV": "2.5"})
    wc = await listener.process_weathercloud(["wid", "example", "key", "", "uvi", "25"])
    assert wu.uv is not None
    assert wu.uv.value == wc.uv.value == 2.5


async def test_diagnostics_report_loaded_settings_not_pending_saved_values(receiver):
    """Saved configuration is not proof that an existing listener loaded it."""
    from custom_components.cloudweatherproxy.diagnostics import async_get_config_entry_diagnostics

    hass, entry, client, writes = receiver
    hass.config_entries._entries[entry.entry_id] = entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, "sensor_source": "weathercloud", "sensor_timeout": 1800, "anonymous_station_address": "192.0.2.20", "legacy_anonymous_binding": True})
    diagnostic = (await async_get_config_entry_diagnostics(hass, entry))["sensor_source"]
    assert diagnostic["selected"] == "wunderground"
    assert diagnostic["timeout_seconds"] == 900
    assert not diagnostic["physical_binding_configured"]
    assert not diagnostic["legacy_binding_enabled"]


async def test_only_documented_wu_solar_is_accepted():
    """Verify only documented wu solar is accepted."""
    listener = CloudWeatherListener()
    unknown = await listener.process_wunderground({"ID": "example", "PASSWORD": "", "solarRadiation": "42.5"})
    assert unknown.solarradiation is None
    documented = await listener.process_wunderground({"ID": "example", "PASSWORD": "", "solarradiation": "1050"})
    assert documented.solarradiation.value == 1050
    assert documented.solarradiation.unit == "W/m²"
