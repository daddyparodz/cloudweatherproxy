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

DRAIN_TIMEOUT = 30.0
CANCEL_SETTLE_TIMEOUT = 1.0


class ReceiverManager:
    """Permanent per-HA dispatch target; never retain a config entry."""

    def __init__(self) -> None:
        """Initialize the request lease counter."""
        self.listener: CloudWeatherListener | None = None
        self.accepting = True
        self._requests = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._request_tasks: set[asyncio.Task] = set()
        self.drain_deadline: float | None = None
        self.cancel_deadline: float | None = None

    def begin_drain(self) -> None:
        """Close admission and start the shared, monotonic shutdown deadline."""
        self.accepting = False
        if self.drain_deadline is None:
            self.drain_deadline = asyncio.get_running_loop().time() + DRAIN_TIMEOUT

    def reset_drain(self) -> None:
        """Start a new lifecycle after successful cleanup or refused teardown."""
        self.drain_deadline = None
        self.cancel_deadline = None

    @property
    def in_flight(self) -> int:
        """Return the current number of listener leases for diagnostics."""
        return self._requests

    async def drain(self) -> None:
        """Drain naturally, then cancel accepted tasks once and bound settlement."""
        self.begin_drain()
        loop = asyncio.get_running_loop()
        assert self.drain_deadline is not None
        if self._requests and loop.time() < self.drain_deadline:
            try:
                async with asyncio.timeout_at(self.drain_deadline):
                    await self._drained.wait()
            except TimeoutError:
                pass
        if not self._requests:
            return
        if self.cancel_deadline is None:
            self.cancel_deadline = loop.time() + CANCEL_SETTLE_TIMEOUT
            for task in self._request_tasks:
                task.cancel()
        remaining = self.cancel_deadline - loop.time()
        if remaining > 0:
            await asyncio.wait(self._request_tasks, timeout=remaining)

    async def handler(self, request: web.Request) -> web.Response:
        """Dispatch to the currently loaded listener."""
        listener = self.listener
        if listener is None or not self.accepting:
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        # No await between resolving the target and acquiring its lease.
        self._requests += 1
        task = asyncio.current_task()
        assert task is not None
        self._request_tasks.add(task)
        self._drained.clear()
        try:
            return await listener.handler(request)
        finally:
            self._requests -= 1
            self._request_tasks.discard(task)
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
