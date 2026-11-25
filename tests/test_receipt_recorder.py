"""Receipt attribute filtering through real HA state and Recorder serializers."""
import asyncio
from datetime import timedelta
import logging
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from homeassistant.config_entries import ConfigEntries
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.components.recorder.core import DATA_RECORDER, Recorder
from homeassistant.components.recorder.db_schema import Base, StateAttributes, States
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.entityfilter import generate_filter

from custom_components.cloudweatherproxy import sensor
from custom_components.cloudweatherproxy.aiocloudweather.server import CloudWeatherListener
from custom_components.cloudweatherproxy.const import DOMAIN
from test_receiver_lifecycle import make_entry


async def test_identical_receipts_do_not_record_rows_but_numeric_changes_do(tmp_path):
    """Keep per-field receipts in memory, not in changing HA state attributes."""
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    entry = make_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    listener = CloudWeatherListener()
    entry.runtime_data = SimpleNamespace(listener=listener, known_sensors={})
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN, platform=sensor, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    pending = []
    await sensor.async_setup_entry(hass, entry, pending.extend)
    await platform.async_add_entities(pending)
    pending.clear()
    timestamp_id = entry.runtime_data.external_data_timestamp.entity_id
    entity_filter = generate_filter([], [], [], [timestamp_id])
    hass.data[DATA_RECORDER] = SimpleNamespace(db_connected=hass.loop.create_future())
    recorder = Recorder(hass, False, False, 10, 5, "sqlite://", 1, 1, entity_filter, set())
    recorder._dialect_name = "sqlite"
    recorder.async_initialize()
    station = await listener.process_wunderground({"ID": "synthetic", "rainin": "0"})
    station.station_client_ip = "127.0.0.1"
    events = []
    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, events.append)
    try:
        await listener._new_dataset_cb(station)
        await platform.async_add_entities(pending)
        await hass.async_block_till_done()
        measurement = next(iter(entry.runtime_data.known_sensors.values()))
        first = hass.states.get(measurement.entity_id)
        first_receipt = getattr(measurement, "last_valid_received_at", None)
        await asyncio.sleep(0)
        await listener._new_dataset_cb(station)
        await hass.async_block_till_done()
        second = hass.states.get(measurement.entity_id)
        assert first.state == second.state == "0.0"
        relevant = [event for event in events if event.data["entity_id"] == measurement.entity_id]
        assert len(relevant) == 1
        assert first_receipt is not None
        assert measurement.last_valid_received_at > first_receipt
        assert "last_valid_received_at" not in second.attributes
        assert first.last_changed == second.last_changed
        assert first.last_updated == second.last_updated
        assert second.last_reported > first.last_updated
        changed = await listener.process_wunderground({"ID": "synthetic", "rainin": "1"})
        changed.station_client_ip = "127.0.0.1"
        await listener._new_dataset_cb(changed)
        await hass.async_block_till_done()
        relevant = [event for event in events if event.data["entity_id"] == measurement.entity_id]
        assert len(relevant) == 2
        assert hass.states.get(measurement.entity_id).state == "25.4"
        attributes = [StateAttributes.shared_attrs_bytes_from_event(event, "sqlite") for event in relevant]
        assert attributes[0] == attributes[1]
        assert b"last_valid_received_at" not in attributes[0]

        # Exercise Recorder's real session-ingestion path with an isolated DB,
        # not a replacement serializer or production Home Assistant database.
        queued_states = []
        while not recorder._queue.empty():
            event = recorder._queue.get_nowait()
            if event.event_type == EVENT_STATE_CHANGED:
                queued_states.append(event)
        assert len(queued_states) == 2
        assert all(event.data["entity_id"] == measurement.entity_id for event in queued_states)
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as session:
                recorder.event_session = session
                for event in queued_states:
                    recorder._process_one_event(event)
                session.flush()
                assert len(session.scalars(select(States)).all()) == 2
                stored = session.scalars(select(StateAttributes)).all()
                assert len(stored) == 1
                assert "last_valid_received_at" not in stored[0].shared_attrs
        finally:
            engine.dispose()

        assert not entity_filter(timestamp_id)
        assert entity_filter(measurement.entity_id)
        assert hass.states.get(timestamp_id).state not in ("unknown", "unavailable")
    finally:
        unsubscribe()
        recorder._async_stop_listeners()
        await platform.async_reset()
        await entry._async_process_on_unload(hass)
        await listener.stop()
