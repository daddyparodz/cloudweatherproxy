"""Exercise the listener through its mounted public HTTP endpoints."""

import pytest  # type: ignore[import-not-found]
from pathlib import Path
from aiohttp import web
from cloudweatherproxy.aiocloudweather.server import (
    CloudWeatherListener,
)


@pytest.fixture
async def client(aiohttp_client):
    """Start only the ephemeral aiohttp test server, not a second fixed-port server."""
    app = web.Application()
    listener = CloudWeatherListener()
    app.router.add_get(
        "/wunderground/weatherstation/updateweatherstation.php", listener.handler)
    app.router.add_get("/weathercloud/v01/set/{path:.*}", listener.handler)

    client = await aiohttp_client(app)
    try:
        yield client
    finally:
        await listener.stop()
        await client.close()


@pytest.mark.asyncio
async def test_handler(client):
    """Resolve fixtures relative to this file and apply the application's mounts."""
    data_dir = Path(__file__).parent / "data"
    data_files = [data_dir / "weathercloud", data_dir / "wunderground"]

    async def test_request(client, request_url):
        response = await client.get(request_url)
        assert response.status == 200
        assert await response.text() == "OK"

    c = client
    for file_path in data_files:
        with open(file_path) as file:
            for line in file:
                request_url = f"/{file_path.name}{line.strip()}"
                await test_request(c, request_url)
