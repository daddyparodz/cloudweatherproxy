"""Real HA config flow/schema tests, independent forwarding flags."""
# ruff: noqa: F811  # Imported pytest fixture names are intentionally injected.
from unittest.mock import AsyncMock, Mock
import pytest
from homeassistant.config_entries import ConfigEntry  # noqa: F401
import voluptuous as vol
from test_receiver_lifecycle import receiver  # noqa: F401
from custom_components.cloudweatherproxy.config_flow import CloudWeatherProxyConfigFlow


async def test_source_form_exposes_independent_choice_and_safe_defaults(receiver):
    """Verify source form exposes independent choice and safe defaults."""
    hass, entry, client, writes = receiver
    flow = CloudWeatherProxyConfigFlow()
    flow.hass = hass
    form = await flow.async_step_user()
    import probatio
    from homeassistant.helpers import config_validation as cv

    # Exercise HA's actual HTTP form serialization, not just the Python schema.
    serialized = probatio.to_field_list(form["data_schema"], custom_serializer=cv.custom_serializer)
    assert {item["name"] for item in serialized} >= {"sensor_source", "sensor_timeout", "trusted_sensor_proxies", "anonymous_station_address"}
    source_field = next(item for item in serialized if item["name"] == "sensor_source")
    assert source_field["options"] == [("wunderground", "Wunderground"), ("weathercloud", "Weathercloud"), ("combined", "Wunderground + Weathercloud")]
    data = form["data_schema"]({"weatherunderground_proxy": False, "weathercloud_proxy": True})
    assert data["sensor_source"] == "wunderground"
    assert data["sensor_timeout"] >= 900
    assert data["trusted_sensor_proxies"] == ""
    assert data["anonymous_station_address"] == ""
    assert data["legacy_anonymous_binding"] is False
    assert data["weathercloud_proxy"] is True
    assert data["weatherunderground_proxy"] is False


@pytest.mark.parametrize("invalid", [
    {"sensor_source": "automatic"},
    {"sensor_timeout": 0},
    {"sensor_timeout": float("inf")},
    {"trusted_sensor_proxies": "proxy.example"},
    {"trusted_sensor_proxies": "127.0.0.1,garbage"},
    {"anonymous_station_address": "station.example"},
    {"legacy_anonymous_binding": True},
])
async def test_flow_rejects_invalid_source_or_trust_binding(receiver, invalid):
    """Verify flow rejects invalid source or trust binding."""
    hass, entry, client, writes = receiver
    flow = CloudWeatherProxyConfigFlow()
    flow.hass = hass
    form = await flow.async_step_user()
    base = {"weatherunderground_proxy": False, "weathercloud_proxy": False}
    if "sensor_source" in invalid or "sensor_timeout" in invalid:
        with pytest.raises(vol.Invalid):
            form["data_schema"]({**base, **invalid})
    result = await flow.async_step_user({**base, **invalid})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_sensor_config"}


@pytest.mark.parametrize("source", ["wunderground", "weathercloud"])
async def test_valid_initial_flow_retains_source_binding_and_proxy_flags(receiver, source):
    """Exercise successful initial flow creation with validated scalar inputs."""
    hass, entry, client, writes = receiver
    hass.config.internal_url = "http://127.0.0.1:8123"
    flow = CloudWeatherProxyConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user({
        "weatherunderground_proxy": True, "weathercloud_proxy": False,
        "sensor_source": source, "trusted_sensor_proxies": "192.0.2.10",
        "anonymous_station_address": "192.0.2.20", "legacy_anonymous_binding": True,
    })
    assert result["type"] == "create_entry"
    assert result["data"]["sensor_source"] == source
    assert result["data"]["weatherunderground_proxy"] is True
    assert result["data"]["weathercloud_proxy"] is False
    assert result["data"]["sensor_timeout"] == 900
    assert result["data"]["trusted_sensor_proxies"] == "192.0.2.10"
    assert result["data"]["anonymous_station_address"] == "192.0.2.20"
    assert result["data"]["legacy_anonymous_binding"] is True


async def test_reconfigure_persists_source_without_live_proxy_reconfiguration(receiver, monkeypatch):
    """Verify reconfigure persists source without live proxy reconfiguration."""
    hass, entry, client, writes = receiver
    hass.config_entries._entries[entry.entry_id] = entry
    flow = CloudWeatherProxyConfigFlow()
    flow.hass = hass
    flow.context = {"entry_id": entry.entry_id, "source": "reconfigure"}
    monkeypatch.setattr(flow, "_abort_if_unique_id_mismatch", lambda: None)
    update = AsyncMock(wraps=entry.runtime_data.listener.update_config)
    monkeypatch.setattr(entry.runtime_data.listener, "update_config", update)
    # Persist through HA's real entry-update helper; replace only reload scheduling.
    schedule = Mock()
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", schedule)
    form = await flow.async_step_reconfigure()
    valid = form["data_schema"]({"weatherunderground_proxy": True, "weathercloud_proxy": False, "sensor_source": "weathercloud"})
    result = await flow.async_step_reconfigure(valid)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["sensor_source"] == "weathercloud"
    assert entry.data["weatherunderground_proxy"] is True
    assert entry.data["weathercloud_proxy"] is False
    schedule.assert_called_once_with(entry.entry_id)
    update.assert_not_called()
