"""Accepted receipt times through the real local HTTP ingestion boundary."""
# ruff: noqa: F811
import asyncio
from datetime import datetime

import pytest

from test_receiver_lifecycle import receiver, PATHS  # noqa: F401
from test_sensor_sources import reconfigure


async def test_external_timestamp_is_unknown_until_valid_external_field(receiver):
    """The visible timestamp excludes indoor and invalid packets, including zero."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source="weathercloud", sensor_timeout=0.03)
    timestamp = entry.runtime_data.external_data_timestamp
    assert timestamp.entity_description.name == "Last external data update"
    assert timestamp.device_class == "timestamp"
    assert timestamp.available and timestamp.native_value is None
    await client.get("/weathercloud/v01/set/wid/SYNTHETIC/key/synthetic/tempin/200/humin/40/chillin/200")
    assert timestamp.native_value is None
    await client.get("/weathercloud/v01/set/wid/SYNTHETIC/key/synthetic/rainrate/nan")
    assert timestamp.native_value is None
    await client.get("/weathercloud/v01/set/wid/SYNTHETIC/key/synthetic/rainrate/0")
    first = timestamp.native_value
    assert isinstance(first, datetime) and first.utcoffset().total_seconds() == 0
    await asyncio.sleep(0.001)
    await client.get("/weathercloud/v01/set/wid/SYNTHETIC/key/synthetic/rainrate/0")
    second = timestamp.native_value
    assert second > first
    await asyncio.sleep(0.05)
    assert timestamp.available and timestamp.native_value == second
    await reconfigure(receiver)
    assert entry.runtime_data.external_data_timestamp.native_value is None


async def test_external_timestamp_rejects_non_owner_identity_and_unit(receiver):
    """Only a validated field from its selected owner may renew the summary."""
    from custom_components.cloudweatherproxy.aiocloudweather import Sensor

    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_source="combined", anonymous_station_address="127.0.0.1", legacy_anonymous_binding=True)
    timestamp = entry.runtime_data.external_data_timestamp
    await client.get(PATHS[0])
    first = timestamp.native_value
    assert first is not None
    # WC temperature is not the combined profile's owner.
    await client.get(PATHS[1])
    assert timestamp.native_value == first
    station = await entry.runtime_data.listener.process_weathercloud(["wid", "SYNTHETIC", "solarrad", "100"])
    station.station_client_ip = "127.0.0.1"
    station.solarradiation = Sensor("solarradiation", 100, "lx")
    await entry.runtime_data.listener._new_dataset_cb(station)
    assert timestamp.native_value == first
    station.solarradiation = Sensor("solarradiation", 100, "W/m²")
    station.station_client_ip = "192.0.2.3"
    await entry.runtime_data.listener._new_dataset_cb(station)
    assert timestamp.native_value == first
    station.station_client_ip = "127.0.0.1"
    await entry.runtime_data.listener._new_dataset_cb(station)
    assert timestamp.native_value > first


async def test_identical_zero_renews_receipt_and_expiry_retains_it(receiver):
    """A zero is data, not missing; expiry is not an accepted receipt."""
    hass, entry, client, writes = receiver
    await reconfigure(receiver, sensor_timeout=0.03)
    path = PATHS[0].replace("tempf=68", "rainin=0")
    await client.get(path)
    entity = next(iter(entry.runtime_data.known_sensors.values()))
    first = entity.extra_state_attributes["last_valid_received_at"]
    assert datetime.fromisoformat(first).utcoffset().total_seconds() == 0
    await asyncio.sleep(0.001)
    await client.get(path)
    second = entity.extra_state_attributes["last_valid_received_at"]
    assert second > first
    assert entity.native_value == 0
    await asyncio.sleep(0.05)
    assert not entity.available
    assert entity.extra_state_attributes["last_valid_received_at"] == second


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "invalid"])
async def test_invalid_fields_do_not_change_receipt(receiver, bad):
    """Invalid fields cannot renew an earlier valid timestamp."""
    hass, entry, client, writes = receiver
    await client.get(PATHS[0])
    entity = next(iter(entry.runtime_data.known_sensors.values()))
    first = dict(entity.extra_state_attributes)
    await client.get(PATHS[0].replace("tempf=68", f"tempf={bad}"))
    assert entity.extra_state_attributes == first
