"""Config flow for Cloud Weather Proxy."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.network import get_url
import voluptuous as vol
from yarl import URL

from .const import CONF_WUNDERGROUND_PROXY, CONF_WEATHERCLOUD_PROXY, CONF_DNS_SERVERS, DOMAIN
from .source import numeric_address


def _address(value: str) -> str:
    if not isinstance(value, str):
        raise vol.Invalid("Numeric address required")
    try:
        return numeric_address(value) if value else ""
    except ValueError as err:
        raise vol.Invalid("Numeric address required") from err


def _proxies(value: str) -> str:
    if not isinstance(value, str):
        raise vol.Invalid("Numeric addresses required")
    if not value:
        return ""
    try:
        return ",".join(numeric_address(part.strip()) for part in value.split(","))
    except ValueError as err:
        raise vol.Invalid("Numeric addresses required") from err


def configuration_schema(current: dict | None = None) -> vol.Schema:
    """One schema for creation/reconfiguration; forwarding is independent."""
    current = current or {}
    return vol.Schema({
        vol.Required(CONF_WUNDERGROUND_PROXY, default=current.get(CONF_WUNDERGROUND_PROXY, False)): bool,
        vol.Required(CONF_WEATHERCLOUD_PROXY, default=current.get(CONF_WEATHERCLOUD_PROXY, False)): bool,
        vol.Optional(CONF_DNS_SERVERS, default=current.get(CONF_DNS_SERVERS, "9.9.9.9")): str,
        vol.Required("sensor_source", default=current.get("sensor_source", "wunderground")): vol.In({"wunderground": "Wunderground", "weathercloud": "Weathercloud"}),
        vol.Required("sensor_timeout", default=current.get("sensor_timeout", 900)): vol.All(int, vol.Range(min=1, max=86400)),
        vol.Optional("trusted_sensor_proxies", default=current.get("trusted_sensor_proxies", "")): str,
        vol.Optional("anonymous_station_address", default=current.get("anonymous_station_address", "")): str,
        vol.Optional("legacy_anonymous_binding", default=current.get("legacy_anonymous_binding", False)): bool,
    })


def validate_configuration(data: dict) -> dict:
    """Reject inconsistent physical associations, never infer one from traffic."""
    result = configuration_schema()(data)
    result["trusted_sensor_proxies"] = _proxies(result["trusted_sensor_proxies"])
    result["anonymous_station_address"] = _address(result["anonymous_station_address"])
    if result["legacy_anonymous_binding"] and not result["anonymous_station_address"]:
        raise vol.Invalid("An explicit station binding is required")
    return result


class CloudWeatherProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """The existing Reconfigure UI controls source, identity and forwarding."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create an entry with an explicit sensor-source selection."""
        errors = {}
        if user_input is not None:
            try:
                data = validate_configuration(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_sensor_config"
            else:
                if self._async_current_entries():
                    return self.async_abort(reason="single_instance")
                base_url = URL(get_url(self.hass))
                return self.async_create_entry(
                    title="Cloud Weather Proxy", data=data,
                    description_placeholders={"address": base_url.host or "", "port": str(base_url.port)},
                )
        return self.async_show_form(step_id="user", data_schema=configuration_schema(), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Apply validated source/identity changes through the atomic entry reload."""
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                data = validate_configuration(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_sensor_config"
            else:
                self._abort_if_unique_id_mismatch()
                # The atomic entry reload owns resource changes. Never mutate a
                # live listener/session before its in-flight requests drain.
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure", data_schema=configuration_schema(dict(entry.data)), errors=errors
        )
