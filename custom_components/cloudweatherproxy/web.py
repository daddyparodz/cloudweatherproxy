"""The web server for the CloudWeatherProxy integration."""

from http import HTTPStatus
import asyncio
import logging

from .aiocloudweather import CloudWeatherListener
from aiohttp import web
from aiohttp.web_exceptions import HTTPClientError

from homeassistant.helpers.http import HomeAssistantView
from homeassistant.helpers.http import KEY_AUTHENTICATED

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class ReceiverManager:
    """Permanent per-HA dispatch target; never retain a config entry."""

    def __init__(self) -> None:
        """Initialize the request lease counter."""
        self.listener: CloudWeatherListener | None = None
        self.accepting = True
        self._requests = 0
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def in_flight(self) -> int:
        """Return the current number of listener leases for diagnostics."""
        return self._requests

    async def drain(self) -> None:
        """Reject new requests and wait for existing listener users."""
        self.accepting = False
        await self._drained.wait()

    async def handler(self, request: web.Request) -> web.Response:
        """Dispatch to the currently loaded listener."""
        listener = self.listener
        if listener is None or not self.accepting:
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        # No await between resolving the target and acquiring its lease.
        self._requests += 1
        self._drained.clear()
        try:
            return await listener.handler(request)
        finally:
            self._requests -= 1
            if not self._requests:
                self._drained.set()


class WundergroundReceiver(HomeAssistantView):
    """Wunderground receiver."""

    name = f"api:{DOMAIN}:wunderground"
    url = "/wunderground/weatherstation/updateweatherstation.php"

    def __init__(self, listener: ReceiverManager) -> None:
        """Initialize the Wunderground receiver."""
        self.listener = listener

    async def get(
        self,
        request: web.Request,
    ) -> web.Response:
        """Handle Wunderground request."""

        if not request[KEY_AUTHENTICATED]:
            return web.Response(status=HTTPStatus.UNAUTHORIZED)

        try:
            return await self.listener.handler(request)
        except HTTPClientError as e:
            _LOGGER.error(e)
            return web.Response(status=HTTPStatus.BAD_REQUEST)


class WeathercloudReceiver(HomeAssistantView):
    """Weathercloud receiver."""

    name = f"api:{DOMAIN}:weathercloud"
    url = "/weathercloud/v01/set/{values:.*}"

    def __init__(self, listener: ReceiverManager) -> None:
        """Initialize the Weathercloud receiver."""
        self.listener = listener

    async def get(
        self,
        request: web.Request,
        values: str,
    ) -> web.Response:
        """Handle Weathercloud request."""

        if not request[KEY_AUTHENTICATED]:
            return web.Response(status=HTTPStatus.UNAUTHORIZED)

        try:
            return await self.listener.handler(request)
        except HTTPClientError as e:
            _LOGGER.error(e)
            return web.Response(status=HTTPStatus.BAD_REQUEST)
