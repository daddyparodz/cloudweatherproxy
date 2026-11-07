"""aioCloudWeather API server."""

from __future__ import annotations

from math import isfinite
import logging
import time
from typing import Any, get_type_hints
from collections.abc import Callable, Coroutine
from copy import deepcopy
from dataclasses import fields

from aiohttp import web, ClientResponse

from ..source import request_address, station_identity
from .proxy import CloudWeatherProxy, DataSink
from .utils import resolve_caster
from .station import (
    WundergroundRawSensor,
    WeathercloudRawSensor,
    WeatherStation,
)

_LOGGER = logging.getLogger(__name__)
_CLOUDWEATHER_LISTEN_PORT = 49199


class CloudWeatherListener:
    """CloudWeather Server API server."""

    def __init__(
        self,
        port: int = _CLOUDWEATHER_LISTEN_PORT,
        proxy_sinks: list[DataSink] | None = None,
        dns_servers: list[str] | None = None,
    ):
        """Initialize CloudWeather Server."""
        # API Constants
        self.port: int = port

        # Proxy functionality
        self.proxy: None | CloudWeatherProxy = None
        self.dns_servers: list[str] = dns_servers or ["9.9.9.9"]
        self.proxy_sinks: list[DataSink] = proxy_sinks or []
        self.proxy_enabled: bool = bool(self.proxy_sinks)
        if self.proxy_enabled:
            self.proxy = CloudWeatherProxy(self.proxy_sinks, self.dns_servers)

        # webserver
        self.server: None | web.Server = None
        self.runner: None | web.ServerRunner = None
        self.site: None | web.TCPSite = None

        # internal data
        self.last_values: dict[str, WeatherStation] = {}
        self.last_updates: dict[str, float] = {}
        self.new_dataset_cb: list[
            Callable[[WeatherStation], Coroutine[Any, Any, Any]]
        ] = []

        self.sensor_source = "wunderground"
        self.protocol_accepted_fields: dict[str, int] = {"wunderground": 0, "weathercloud": 0}
        self.sensor_settings = {
            "timeout_seconds": 900,
            "physical_binding_configured": False,
            "legacy_binding_enabled": False,
        }
        self.trusted_sensor_proxies: set[str] = set()
        self.source_counters = {
            "selected_packets": 0, "unselected_packets": 0,
            "identity_rejected": 0, "collision_rejected": 0,
            "accepted_fields": 0,
        }
        # storage
        self.stations: list[str] = []

    async def update_config(
        self,
        proxy_sinks: list[DataSink] | None = None,
        dns_servers: list[str] | None = None,
    ) -> None:
        """Update the proxy configuration."""
        self.proxy_sinks = proxy_sinks or []
        self.dns_servers = dns_servers or self.dns_servers or ["9.9.9.9"]
        self.proxy_enabled = bool(self.proxy_sinks)

        if self.proxy:
            await self.proxy.close()
            self.proxy = None

        if self.proxy_enabled:
            self.proxy = CloudWeatherProxy(self.proxy_sinks, self.dns_servers)

    def get_active_proxies(self) -> list[DataSink]:
        """Get the active proxies."""
        return self.proxy_sinks or []

    def get_dns_servers(self) -> list[str]:
        """Get the DNS servers."""
        return self.dns_servers

    async def _new_dataset_cb(self, dataset: WeatherStation) -> None:
        """Call new dataset callbacks."""
        for callback in self.new_dataset_cb:
            await callback(dataset)

    async def process_wunderground(
        self, data: dict[str, str | float]
    ) -> WeatherStation:
        """Process Wunderground data."""

        dfields = {
            f.metadata["arg"]: f
            for f in fields(WundergroundRawSensor)
            if "arg" in f.metadata
        }
        type_hints = get_type_hints(WundergroundRawSensor)
        instance_data: dict[str, Any] = {"station_id": "", "station_key": ""}

        for arg, field in dfields.items():
            if arg in data:
                value = data[arg]
                try:
                    parsed = resolve_caster(type_hints[field.name])(value)
                    if isinstance(parsed, (int, float)) and not isfinite(parsed):
                        continue
                    instance_data[field.name] = parsed
                except (ValueError, TypeError, OverflowError):
                    _LOGGER.warning("Invalid sensor field: %s", field.name)

        return WeatherStation.from_wunderground(WundergroundRawSensor(**instance_data))

    async def process_weathercloud(self, segments: list[str]) -> WeatherStation:
        """Process WeatherCloud data."""

        data = dict(zip(segments[::2], segments[1::2]))
        dfields = {
            f.metadata["arg"]: f
            for f in fields(WeathercloudRawSensor)
            if "arg" in f.metadata
        }
        type_hints = get_type_hints(WeathercloudRawSensor)
        instance_data: dict[str, Any] = {"station_id": "", "station_key": ""}

        for arg, field in dfields.items():
            if arg in data:
                value = data[arg]
                try:
                    parsed = resolve_caster(type_hints[field.name])(value)
                    if isinstance(parsed, (int, float)) and not isfinite(parsed):
                        continue
                    instance_data[field.name] = parsed
                except (ValueError, TypeError, OverflowError):
                    _LOGGER.warning("Invalid sensor field: %s", field.name)

        return WeatherStation.from_weathercloud(WeathercloudRawSensor(**instance_data))

    async def handler(self, request: web.BaseRequest) -> web.Response:
        """AIOHTTP handler for the API."""

        if not isinstance(request, web.Request):
            raise web.HTTPBadRequest()

        if request.method != "GET" or request.path is None:
            raise web.HTTPBadRequest()

        station_id: str | None = None
        dataset: WeatherStation | None = None
        sink: DataSink | None = None
        if request.path.endswith("/weatherstation/updateweatherstation.php"):
            dataset = await self.process_wunderground(dict(request.query))
            station_id = dataset.station_id
            sink = DataSink.WUNDERGROUND
        elif "/v01/set" in request.path:
            dataset_path = request.path.split("/v01/set/", 1)[1]
            path_segments = dataset_path.split("/")
            dataset = await self.process_weathercloud(path_segments)
            station_id = dataset.station_id
            sink = DataSink.WEATHERCLOUD
        else:
            return web.Response(status=404, text="Not Found")

        assert dataset is not None
        assert station_id is not None

        dataset.update_time = time.monotonic()

        # The User-Agent is the only recognizable information we have aside from the IP
        # In case of the station at hand it just shows lwIP/2.1.2 of their IP stack
        user_agent = request.headers.get("User-Agent")
        if user_agent:
            dataset.station_sw_version = user_agent

        dataset.station_client_ip = request_address(request, self.trusted_sensor_proxies)

        try:
            await self._new_dataset_cb(dataset)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("CloudWeather new dataset callback error: %s", type(err).__name__)

        if self.proxy and sink is not None:
            if sink not in self.proxy.proxied_sinks:
                _LOGGER.debug(
                    "Skipping proxy for sink %s because it is not enabled", sink
                )
            elif self.proxy.session.closed:
                _LOGGER.warning(
                    "CloudWeather proxy session closed for %s; skipping",
                    sink,
                )
            else:
                try:
                    response: ClientResponse = await self.proxy.forward(sink, request)
                    await response.read()
                    _LOGGER.debug("CloudWeather proxy response[%d]", response.status)

                    if response.status >= 400:
                        raise RuntimeError(
                            f"Upstream returned {response.status} for {sink}"
                        )
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.warning(
                        "CloudWeather proxy error for %s: %s",
                        sink,
                        type(err).__name__,
                    )

        # A protocol/address namespace is mandatory even for empty credentials.
        # Never use the physical cross-protocol association for wire caches.
        cache_key = station_identity(dataset, None, False)
        if cache_key is not None:
            self.last_values[cache_key] = deepcopy(dataset)
            self.last_updates[cache_key] = dataset.update_time
            if cache_key not in self.stations:
                self.stations.append(cache_key)
        return web.Response(text="OK")

    async def start(self) -> None:
        """Listen and process."""

        self.server = web.Server(self.handler)
        self.runner = web.ServerRunner(self.server)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, port=self.port)
        await self.site.start()

    async def stop(self) -> None:
        """Stop listening."""
        if self.site:
            await self.site.stop()
        if self.proxy:
            await self.proxy.close()
