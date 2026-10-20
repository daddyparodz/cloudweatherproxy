"""Receiver regression tests using HA views, entries, listener and aiohttp routing.

Only the platform orchestration seam is substituted: sensor setup is real, while
entity state writes (which require a fully booted entity platform) are recorded.
No external weather service or credentials are used.
"""

import asyncio
from types import MappingProxyType
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant.components.http import HomeAssistantHTTP
from homeassistant.helpers.http import KEY_AUTHENTICATED

from custom_components.cloudweatherproxy import async_setup_entry, async_unload_entry
from custom_components.cloudweatherproxy import sensor
from custom_components.cloudweatherproxy.const import DOMAIN

PATHS = [
    "/wunderground/weatherstation/updateweatherstation.php?ID=SYNTHETIC&PASSWORD=synthetic&tempf=68",
    "/weathercloud/v01/set/wid/SYNTHETIC/key/synthetic/temp/200",
]


def make_entry():
    """Create a real config entry containing only synthetic local-test data."""
    return ConfigEntry(
        domain=DOMAIN,
        title="Synthetic",
        data={
            "weatherunderground_proxy": False,
            "weathercloud_proxy": False,
            "dns_servers": "192.0.2.1",
        },
        options={},
        source="user",
        version=1,
        minor_version=1,
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )


@pytest_asyncio.fixture
async def receiver(tmp_path, monkeypatch, request):
    """Run real HA views and sensor callbacks on an actual aiohttp server."""
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    hass.http = HomeAssistantHTTP(
        hass, None, None, None, ["127.0.0.1"], 0, [], "modern"
    )
    writes = []
    monkeypatch.setattr(
        sensor.CloudWeatherEntity,
        "async_write_ha_state",
        lambda self: writes.append(self),
    )

    async def forward(entry, platforms):
        await sensor.async_setup_entry(hass, entry, lambda entities: None)

    async def unload(entry, platforms):
        await entry._async_process_on_unload(hass)
        return True

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)

    @web.middleware
    async def auth(request, handler):
        request[KEY_AUTHENTICATED] = request.headers.get("X-Test-Auth", "yes") == "yes"
        return await handler(request)

    hass.http.app.middlewares.append(auth)
    entry = make_entry()
    assert await async_setup_entry(hass, entry)
    client = TestClient(TestServer(hass.http.app))
    if getattr(request, "param", True):
        await client.start_server()
    yield hass, entry, client, writes
    await client.close()
    listener = entry.runtime_data.listener
    await listener.stop()
    if DOMAIN in hass.data:
        import logging

        logging.getLogger("custom_components.cloudweatherproxy").removeHandler(
            hass.data[DOMAIN].log_handler
        )


@pytest.mark.parametrize("path", PATHS)
async def test_reload_dispatches_only_current_sensor_callback(receiver, path):
    """A reload must deliver the next dataset to the replacement sensor callback."""
    hass, entry, client, writes = receiver
    assert (await client.get(path)).status == 200
    old = entry.runtime_data
    assert old.known_sensors
    assert await async_unload_entry(hass, entry)
    assert await async_setup_entry(hass, entry)
    writes.clear()
    assert (await client.get(path)).status == 200
    assert entry.runtime_data.known_sensors
    assert writes
    assert not old.listener.new_dataset_cb


@pytest.mark.parametrize("path", PATHS)
async def test_unload_drains_before_removing_callbacks_or_closing_session(
    receiver, monkeypatch, path
):
    """An accepted request retains callbacks until its listener lease completes."""
    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    entered, release = asyncio.Event(), asyncio.Event()
    original = (
        listener.process_wunderground
        if path == PATHS[0]
        else listener.process_weathercloud
    )

    async def blocked(data):
        entered.set()
        await release.wait()
        assert listener.new_dataset_cb, "callback removed while request was in flight"
        return await original(data)

    method = "process_wunderground" if path == PATHS[0] else "process_weathercloud"
    monkeypatch.setattr(listener, method, blocked)
    stop = AsyncMock(wraps=listener.stop)
    monkeypatch.setattr(listener, "stop", stop)
    request = asyncio.create_task(client.get(path))
    await asyncio.wait_for(entered.wait(), 5)
    unloading = asyncio.create_task(async_unload_entry(hass, entry))
    try:
        await asyncio.sleep(0.05)
        assert not unloading.done(), "unload must wait for accepted requests"
        stop.assert_not_called()
        assert listener.new_dataset_cb
        assert (await client.get(path)).status == 503
    finally:
        release.set()
        response = await asyncio.wait_for(request, 5)
        await asyncio.wait_for(unloading, 5)
    assert response.status == 200
    assert writes
    stop.assert_awaited_once()


async def test_second_entry_is_rejected_without_touching_active_listener(receiver):
    """A competing entry cannot steal the active receiver."""
    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    second = make_entry()
    assert not await async_setup_entry(hass, second)
    assert entry.runtime_data.listener is listener
    assert (await client.get(PATHS[0])).status == 200
    assert writes


async def test_unloading_non_owner_cannot_disable_the_active_receiver(receiver):
    """An entry without receiver ownership cannot disturb the active listener."""
    hass, entry, client, writes = receiver
    second = make_entry()
    assert not await async_unload_entry(hass, second)
    assert (await client.get(PATHS[0])).status == 200
    assert writes


async def test_ambiguous_configured_entries_are_not_arbitrarily_selected(
    receiver, monkeypatch
):
    """Reject a broken multi-entry configuration rather than picking a winner."""
    hass, entry, client, writes = receiver
    assert await async_unload_entry(hass, entry)
    second = make_entry()
    monkeypatch.setattr(
        hass.config_entries, "async_entries", lambda domain: [entry, second]
    )
    assert not await async_setup_entry(hass, entry)
    assert not await async_setup_entry(hass, second)
    assert (await client.get(PATHS[0])).status == 503


async def test_failed_setup_releases_resources_and_allows_retry(receiver, monkeypatch):
    """A platform setup exception must not permanently reserve the receiver."""
    hass, entry, client, writes = receiver
    assert await async_unload_entry(hass, entry)
    forward = hass.config_entries.async_forward_entry_setups
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(side_effect=RuntimeError("synthetic setup failure")),
    )
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        await async_setup_entry(hass, entry)
    assert (await client.get(PATHS[0])).status == 503
    assert not entry.runtime_data.listener.new_dataset_cb
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    assert await async_setup_entry(hass, entry)
    assert (await client.get(PATHS[0])).status == 200


async def test_loaded_code_diagnostics_report_stable_receiver_status(receiver):
    """Diagnostics identify loaded code and count actual public-router routes."""
    from custom_components.cloudweatherproxy.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    hass, entry, client, writes = receiver
    data = await async_get_config_entry_diagnostics(hass, entry)
    status = data["receiver_lifecycle"]
    assert status == {
        "implementation": "reloadfix.1",
        "api_version": 1,
        "registered_views": 2,
        "active": True,
        "accepting": True,
        "in_flight": 0,
    }
    assert await async_unload_entry(hass, entry)
    status = (await async_get_config_entry_diagnostics(hass, entry))[
        "receiver_lifecycle"
    ]
    assert not status["active"]
    assert status["registered_views"] == 2


@pytest.mark.parametrize("path", PATHS)
async def test_unloaded_is_controlled_503_and_auth_remains_required(receiver, path):
    """Unloaded routes return 503 without weakening HA authentication."""
    hass, entry, client, writes = receiver
    assert (await client.get(path, headers={"X-Test-Auth": "no"})).status == 401
    assert not writes
    assert await async_unload_entry(hass, entry)
    assert (await client.get(path)).status == 503
    assert (await client.get(path, headers={"X-Test-Auth": "no"})).status == 401
    assert not writes


@pytest.mark.parametrize("failure", [False, RuntimeError("synthetic unload failure")])
@pytest.mark.parametrize("path", PATHS)
async def test_failed_unload_preserves_active_listener_and_callback(
    receiver, monkeypatch, failure, path
):
    """Platform unload refusal or exceptions leave the current callback usable."""
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    # Enable only the other sink: construct a real session without external IO.
    sink = DataSink.WEATHERCLOUD if path == PATHS[0] else DataSink.WUNDERGROUND
    await listener.update_config(proxy_sinks=[sink], dns_servers=["192.0.2.1"])
    session = listener.proxy.session
    callback = listener.new_dataset_cb[0]
    stop = AsyncMock(wraps=listener.stop)
    monkeypatch.setattr(listener, "stop", stop)
    unload = (
        AsyncMock(return_value=False)
        if failure is False
        else AsyncMock(side_effect=failure)
    )
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    if failure is False:
        assert not await async_unload_entry(hass, entry)
    else:
        with pytest.raises(RuntimeError, match="synthetic unload failure"):
            await async_unload_entry(hass, entry)
    assert listener.new_dataset_cb == [callback]
    assert not session.closed
    stop.assert_not_called()
    assert (await client.get(path)).status == 200
    assert writes


async def test_repeated_reload_and_remove_recreate_do_not_accumulate_routes_or_callbacks(
    receiver,
):
    """Stable views must survive replacement entries without retaining old ones."""
    import logging

    hass, entry, client, writes = receiver
    routes = tuple(hass.http.app.router.routes())
    registry = hass.data[DOMAIN]
    log = logging.getLogger("custom_components.cloudweatherproxy")
    handlers = tuple(log.handlers)
    old_listeners = []
    for _ in range(5):
        old_listeners.append(entry.runtime_data.listener)
        assert await async_unload_entry(hass, entry)
        assert (await client.get(PATHS[0])).status == 503
        assert await async_setup_entry(hass, entry)
        for path in PATHS:
            assert (await client.get(path)).status == 200
        assert len(entry.runtime_data.listener.new_dataset_cb) == 1
        assert tuple(hass.http.app.router.routes()) == routes
        assert hass.data[DOMAIN] is registry
        assert tuple(log.handlers) == handlers
    assert await async_unload_entry(hass, entry)
    replacement = make_entry()
    assert replacement.entry_id != entry.entry_id
    assert await async_setup_entry(hass, replacement)
    try:
        for path in PATHS:
            assert (await client.get(path)).status == 200
        assert replacement.runtime_data.known_sensors
        assert not entry.runtime_data.listener.new_dataset_cb
        assert all(not listener.new_dataset_cb for listener in old_listeners)
        assert tuple(hass.http.app.router.routes()) == routes
    finally:
        assert await async_unload_entry(hass, replacement)


@pytest.mark.parametrize("receiver", [False], indirect=True)
@pytest.mark.parametrize("path", PATHS)
async def test_pre_start_reload_cannot_leave_old_route_first(receiver, path):
    """Exercise old-first routing even when aiohttp allows route registration."""
    hass, entry, client, writes = receiver
    old = entry.runtime_data.listener
    assert await async_unload_entry(hass, entry)
    assert await async_setup_entry(hass, entry)
    await client.start_server()
    assert (await client.get(path)).status == 200
    assert entry.runtime_data.known_sensors
    assert writes
    assert not old.last_values


@pytest.mark.parametrize("path", PATHS)
async def test_in_flight_local_proxy_session_stays_open_until_drain(
    receiver, monkeypatch, aiohttp_server, path
):
    """Real local proxy IO must finish before unloading closes its session."""
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        entered.set()
        await release.wait()
        return web.Response(text="synthetic upstream")

    app = web.Application()
    app.router.add_get("/synthetic", upstream)
    server = await aiohttp_server(app)
    await listener.update_config(proxy_sinks=list(DataSink), dns_servers=["192.0.2.1"])
    proxy = listener.proxy
    session = proxy.session

    async def local_forward(sink, request):
        return await session.get(server.make_url("/synthetic"))

    monkeypatch.setattr(proxy, "forward", local_forward)
    request = asyncio.create_task(client.get(path))
    await asyncio.wait_for(entered.wait(), 5)
    unloading = asyncio.create_task(async_unload_entry(hass, entry))
    try:
        await asyncio.sleep(0.05)
        assert not session.closed
        assert not unloading.done()
        assert (await client.get(path)).status == 503
    finally:
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert await asyncio.wait_for(unloading, 5)
    assert response.status == 200
    assert session.closed
    assert writes


async def test_overlapping_unload_cannot_reopen_a_draining_receiver(
    receiver, monkeypatch
):
    """A second unload must not restore admission while the first is pending."""
    hass, entry, client, writes = receiver
    entered, release = asyncio.Event(), asyncio.Event()
    unload = hass.config_entries.async_unload_platforms

    async def blocked(*args):
        entered.set()
        await release.wait()
        return await unload(*args)

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", blocked)
    first = asyncio.create_task(async_unload_entry(hass, entry))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(async_unload_entry(hass, entry))
    try:
        await asyncio.sleep(0.05)
        assert second.done(), (
            "competing unload must fail without entering platform teardown"
        )
        assert not await second
        assert (await client.get(PATHS[0])).status == 503
    finally:
        release.set()
        await asyncio.wait_for(first, 5)
        await asyncio.wait_for(second, 5)


async def test_repeated_reload_closes_every_real_proxy_session(receiver):
    """Every retired listener must close its real ClientSession."""
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    sessions = []
    for _ in range(5):
        listener = entry.runtime_data.listener
        await listener.update_config(
            proxy_sinks=[DataSink.WEATHERCLOUD], dns_servers=["192.0.2.1"]
        )
        session = listener.proxy.session
        sessions.append(session)
        assert not session.closed
        # Wunderground is not an enabled sink: no external network request.
        assert (await client.get(PATHS[0])).status == 200
        assert await async_unload_entry(hass, entry)
        assert all(s.closed for s in sessions)
        assert await async_setup_entry(hass, entry)

async def test_stop_failure_keeps_cleanup_retryable(receiver, monkeypatch):
    """A detached listener remains owned until its real session is closed."""
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    original = listener.stop
    unload = AsyncMock(wraps=hass.config_entries.async_unload_platforms)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    stop = AsyncMock(side_effect=RuntimeError("synthetic stop failure"))
    monkeypatch.setattr(listener, "stop", stop)
    assert await async_unload_entry(hass, entry)
    assert not listener.new_dataset_cb
    assert (await client.get(PATHS[0])).status == 503
    assert not session.closed
    with pytest.raises(RuntimeError, match="synthetic stop failure"):
        await async_setup_entry(hass, entry)
    monkeypatch.setattr(listener, "stop", original)
    assert await async_unload_entry(hass, entry)
    unload.assert_awaited_once()
    assert session.closed
    assert hass.data[DOMAIN].entry_id is None
    assert await async_setup_entry(hass, entry)


@pytest.mark.parametrize("stop_error", [False, True])
async def test_cancelled_stop_finishes_cleanup_before_releasing_owner(receiver, monkeypatch, caplog, stop_error):
    """Cancellation is consumed so HA can finish its unload state transition."""
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    entered, release = asyncio.Event(), asyncio.Event()
    original = listener.stop

    async def blocked():
        entered.set()
        await release.wait()
        if stop_error:
            raise RuntimeError("cleanup failed while cancelled")
        await original()

    monkeypatch.setattr(listener, "stop", blocked)
    task = asyncio.create_task(async_unload_entry(hass, entry))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cleanup must survive caller cancellation"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "repeated cancellation must not orphan cleanup"
        assert hass.data[DOMAIN].entry_id == entry.entry_id
        assert not listener.new_dataset_cb
    finally:
        release.set()
        assert await task
    if stop_error:
        assert "cleanup failed while cancelled" in caplog.text
        assert not session.closed
        assert hass.data[DOMAIN].entry_id == entry.entry_id
        monkeypatch.setattr(listener, "stop", original)
        assert await async_unload_entry(hass, entry)
    assert session.closed
    assert hass.data[DOMAIN].entry_id is None
    assert await async_setup_entry(hass, entry)

@pytest.mark.parametrize("cancel_setup", [False, True])
async def test_failed_setup_stop_can_be_retried_without_platform_teardown(receiver, monkeypatch, caplog, cancel_setup):
    """Failed setup cleanup retains resources for an explicit unload retry."""
    from custom_components.cloudweatherproxy.aiocloudweather import CloudWeatherListener
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    assert await async_unload_entry(hass, entry)
    session = None

    async def fail_setup(*args):
        nonlocal session
        await entry.runtime_data.listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
        session = entry.runtime_data.listener.proxy.session
        if cancel_setup:
            raise asyncio.CancelledError
        raise RuntimeError("synthetic setup failure")

    original = CloudWeatherListener.stop
    with monkeypatch.context() as scoped:
        scoped.setattr(hass.config_entries, "async_forward_entry_setups", fail_setup)
        scoped.setattr(CloudWeatherListener, "stop", AsyncMock(side_effect=RuntimeError("stop failed")))
        expected = asyncio.CancelledError if cancel_setup else RuntimeError
        with pytest.raises(expected):
            await async_setup_entry(hass, entry)
    if cancel_setup:
        assert "stop failed" in caplog.text
    unload = AsyncMock(return_value=False)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    try:
        assert await async_unload_entry(hass, entry)
        unload.assert_not_called()
        assert session is not None
        assert session.closed
        assert hass.data[DOMAIN].entry_id is None
    finally:
        await original(entry.runtime_data.listener)


@pytest.mark.parametrize("cancel_unload", [True, False])
async def test_cancellation_before_teardown_preserves_active_listener(receiver, monkeypatch, cancel_unload):
    """Cancelled request leases release drain; cancelled unload completes atomically."""
    from aiohttp.test_utils import make_mocked_request
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    callback = listener.new_dataset_cb[0]
    manager = hass.data[DOMAIN].receiver
    entered, release = asyncio.Event(), asyncio.Event()
    original = listener.process_wunderground

    async def blocked(data):
        entered.set()
        await release.wait()
        return await original(data)

    monkeypatch.setattr(listener, "process_wunderground", blocked)
    request = asyncio.create_task(manager.handler(make_mocked_request("GET", PATHS[0])))
    await asyncio.wait_for(entered.wait(), 5)
    unload = AsyncMock(wraps=hass.config_entries.async_unload_platforms)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    task = asyncio.create_task(async_unload_entry(hass, entry)) if cancel_unload else None
    try:
        await asyncio.sleep(0)
        if task is not None:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            unload.assert_not_called()
            assert not manager.accepting
            assert manager.listener is listener
            assert listener.new_dataset_cb == [callback]
            assert not session.closed
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert manager.in_flight == 0
        if task is not None:
            assert await task
            assert session.closed
            assert manager.listener is None
            assert manager.accepting
            assert await async_setup_entry(hass, entry)
            return
        assert manager.listener is listener
        assert listener.new_dataset_cb == [callback]
        assert not session.closed
        release.set()
        assert (await client.get(PATHS[0])).status == 200
        assert writes
        assert await async_unload_entry(hass, entry)
        assert session.closed
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

async def test_cancelled_setup_joins_cleanup_without_competing_unload(receiver, monkeypatch):
    """Setup rollback owns its cleanup until the real session is closed."""
    from custom_components.cloudweatherproxy.aiocloudweather import CloudWeatherListener
    from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink

    hass, entry, client, writes = receiver
    assert await async_unload_entry(hass, entry)
    entered, release = asyncio.Event(), asyncio.Event()
    original = CloudWeatherListener.stop

    async def fail_setup(*args):
        await entry.runtime_data.listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
        raise asyncio.CancelledError

    async def blocked(listener):
        entered.set()
        await release.wait()
        await original(listener)

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", fail_setup)
    monkeypatch.setattr(CloudWeatherListener, "stop", blocked)
    task = asyncio.create_task(async_setup_entry(hass, entry))
    await asyncio.wait_for(entered.wait(), 5)
    session = entry.runtime_data.listener.proxy.session
    try:
        assert not hass.data[DOMAIN].receiver.accepting
        assert not await async_unload_entry(hass, entry)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert session.closed
    assert hass.data[DOMAIN].entry_id is None
    assert hass.data[DOMAIN].receiver.accepting
