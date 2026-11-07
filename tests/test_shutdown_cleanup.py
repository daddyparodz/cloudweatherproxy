"""Quarantined resources must have a cleanup path without a future setup."""
# ruff: noqa: F811
import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION

from custom_components import cloudweatherproxy as component
from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink
from custom_components.cloudweatherproxy.const import DOMAIN
from test_real_entry_lifecycle import integration_for
from test_receiver_lifecycle import PATHS, make_entry, receiver  # noqa: F401


async def test_shutdown_retries_failed_unload_without_later_setup(receiver, monkeypatch):
    """Normal HA shutdown retries resources retained after FAILED_UNLOAD."""
    hass, entry, client, writes = receiver
    integration = await integration_for(entry)
    assert await component.async_unload_entry(hass, entry)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    stop = listener.stop
    failing_stop = AsyncMock(side_effect=RuntimeError("synthetic stop failure"))
    monkeypatch.setattr(listener, "stop", failing_stop)
    try:
        async with entry.setup_lock:
            assert not await entry.async_unload(hass, integration=integration)
        assert entry.state is ConfigEntryState.FAILED_UNLOAD
        assert hasattr(entry, "runtime_data")
        assert not session.closed
        assert hass.data[DOMAIN].pending_listener is listener
        # A still-failing setup must preserve the same quarantined owner.
        async with entry.setup_lock:
            await entry.async_setup(hass, integration=integration)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert failing_stop.await_count == 2
        assert not session.closed
        monkeypatch.setattr(listener, "stop", stop)
        await hass.async_stop(force=True)
        assert session.closed
        assert hass.data[DOMAIN].pending_listener is None
        assert not hass.data[DOMAIN].cleanup_pending
        assert hass.data[DOMAIN].entry_id is None
    finally:
        monkeypatch.setattr(listener, "stop", stop)
        await stop()


@pytest.mark.parametrize("fails_again", [False, True])
async def test_shutdown_serializes_with_in_progress_unload_cleanup(
    receiver, monkeypatch, fails_again
):
    """Shutdown waits for the current owner, then retries once if it failed."""
    hass, entry, client, writes = receiver
    integration = await integration_for(entry)
    assert await component.async_unload_entry(hass, entry)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    original = listener.stop
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    active = 0

    async def stop():
        nonlocal calls, active
        calls += 1
        active += 1
        assert active == 1, "two owners attempted cleanup concurrently"
        try:
            if calls == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("synthetic first failure")
            if fails_again:
                raise RuntimeError("synthetic repeated failure")
            await original()
        finally:
            active -= 1

    monkeypatch.setattr(listener, "stop", stop)

    async def unload():
        async with entry.setup_lock:
            return await entry.async_unload(hass, integration=integration)

    unloading = asyncio.create_task(unload())
    await asyncio.wait_for(entered.wait(), 5)
    stopping = asyncio.create_task(hass.async_stop(force=True))
    try:
        await asyncio.sleep(0)
        assert not session.closed
        release.set()
        assert not await asyncio.wait_for(unloading, 5)
        await asyncio.wait_for(stopping, 5)
        assert calls == 2
        assert entry.state is ConfigEntryState.FAILED_UNLOAD
        assert hasattr(entry, "runtime_data")
        assert session.closed is not fails_again
        domain_data = hass.data[DOMAIN]
        assert not domain_data.receiver.accepting
        if fails_again:
            assert domain_data.cleanup_pending
            assert domain_data.pending_listener is listener
            assert domain_data.entry_id == entry.entry_id
        else:
            assert not domain_data.cleanup_pending
            assert domain_data.pending_listener is None
        assert not await component.async_setup_entry(hass, entry)
    finally:
        release.set()
        await asyncio.gather(unloading, stopping, return_exceptions=True)
        monkeypatch.setattr(listener, "stop", original)
        await original()


async def test_shutdown_retries_failed_setup_cleanup(receiver, monkeypatch):
    """A SETUP_ERROR entry is not unloaded by HA, but quarantine is still retried."""
    hass, entry, client, writes = receiver
    assert await component.async_unload_entry(hass, entry)
    integration = await integration_for(entry)
    captured = []

    async def fail_setup(*args):
        listener = entry.runtime_data.listener
        await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
        captured.append((listener, listener.proxy.session, listener.stop))
        monkeypatch.setattr(listener, "stop", AsyncMock(side_effect=RuntimeError("stop failed")))
        raise RuntimeError("setup failed")

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", fail_setup)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    listener, session, stop = captured[0]
    assert not session.closed
    monkeypatch.setattr(listener, "stop", stop)
    try:
        await hass.async_stop(force=True)
        assert session.closed
        assert hass.data[DOMAIN].pending_listener is None
        assert not hass.data[DOMAIN].cleanup_pending
    finally:
        await stop()


@pytest.mark.parametrize("unload_first", [False, True])
async def test_shutdown_drains_active_or_unloading_listener(
    receiver, monkeypatch, unload_first
):
    """A stop during request draining must not miss later quarantined cleanup."""
    hass, entry, client, writes = receiver
    integration = await integration_for(entry)
    assert await component.async_unload_entry(hass, entry)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    process = listener.process_wunderground
    original = listener.stop
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def blocked(data):
        entered.set()
        await release.wait()
        assert listener.new_dataset_cb
        assert not session.closed
        return await process(data)

    async def stop():
        nonlocal calls
        calls += 1
        if unload_first and calls == 1:
            raise RuntimeError("synthetic failure after drain")
        await original()

    monkeypatch.setattr(listener, "process_wunderground", blocked)
    monkeypatch.setattr(listener, "stop", stop)
    request = asyncio.create_task(client.get(PATHS[0]))
    await asyncio.wait_for(entered.wait(), 5)

    async def unload():
        async with entry.setup_lock:
            return await entry.async_unload(hass, integration=integration)

    unloading = asyncio.create_task(unload()) if unload_first else None
    if unloading is not None:
        async with asyncio.timeout(5):
            while hass.data[DOMAIN].receiver.accepting:
                await asyncio.sleep(0)
    stopping = asyncio.create_task(hass.async_stop(force=True))
    try:
        async with asyncio.timeout(5):
            while not hass.data[DOMAIN].stopping:
                await asyncio.sleep(0)
        assert not session.closed
        assert (await client.get(PATHS[0])).status == 503
        release.set()
        assert (await asyncio.wait_for(request, 5)).status == 200
        if unloading is not None:
            assert not await asyncio.wait_for(unloading, 5)
            assert entry.state is ConfigEntryState.FAILED_UNLOAD
            assert hasattr(entry, "runtime_data")
        await asyncio.wait_for(stopping, 5)
        assert session.closed
        assert calls == (2 if unload_first else 1)
        assert hass.data[DOMAIN].pending_listener is None
        assert hass.data[DOMAIN].receiver.listener is None
        assert not hass.data[DOMAIN].receiver.accepting
    finally:
        release.set()
        await asyncio.gather(request, stopping, *([unloading] if unloading else []), return_exceptions=True)
        monkeypatch.setattr(listener, "stop", original)
        await original()


async def test_shutdown_joins_setup_without_reopening_receiver(receiver, monkeypatch):
    """Completing platform setup during shutdown cannot re-enable ingestion."""
    hass, entry, client, writes = receiver
    assert await component.async_unload_entry(hass, entry)
    integration = await integration_for(entry)
    entered, release = asyncio.Event(), asyncio.Event()
    forward = hass.config_entries.async_forward_entry_setups
    captured = []

    async def blocked(*args):
        listener = entry.runtime_data.listener
        await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
        captured.append(listener)
        entered.set()
        await release.wait()
        await forward(*args)

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", blocked)

    async def setup():
        async with entry.setup_lock:
            await entry.async_setup(hass, integration=integration)

    setting_up = asyncio.create_task(setup())
    await asyncio.wait_for(entered.wait(), 5)
    stopping = asyncio.create_task(hass.async_stop(force=True))
    try:
        async with asyncio.timeout(5):
            while not hass.data[DOMAIN].stopping:
                await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(setting_up, 5)
        await asyncio.wait_for(stopping, 5)
        assert captured[0].proxy.session.closed
        assert hass.data[DOMAIN].receiver.listener is None
        assert not hass.data[DOMAIN].receiver.accepting
        assert (await client.get(PATHS[0])).status == 503
    finally:
        release.set()
        await asyncio.gather(setting_up, stopping, return_exceptions=True)
        for listener in captured:
            await listener.stop()


async def test_setup_cancellation_before_first_child_turn_releases_resources(
    receiver, monkeypatch
):
    """Queued cancellation cannot skip ownership rollback before setup starts."""
    hass, old_entry, client, writes = receiver
    assert await component.async_unload_entry(hass, old_entry)
    entry = make_entry(weathercloud_proxy=True)
    integration = await integration_for(entry)
    factory = component.CloudWeatherListener
    captured = []
    forward = hass.config_entries.async_forward_entry_setups

    async def yield_then_forward(*args):
        # Ensure cancellation is delivered at setup's first suspension even
        # when the real sensor setup happens to finish without suspending.
        await asyncio.sleep(0)
        await forward(*args)

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", yield_then_forward)

    def listener_factory(*args, **kwargs):
        listener = factory(*args, **kwargs)
        captured.append(listener)
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        return listener

    monkeypatch.setattr(component, "CloudWeatherListener", listener_factory)
    try:
        # HA 2025.12 consumes setup cancellation; HA 2026.10 re-raises it.
        cancellation = nullcontext() if HA_VERSION.startswith("2025.12.") else pytest.raises(asyncio.CancelledError)
        with cancellation:
            async with entry.setup_lock:
                await entry.async_setup(hass, integration=integration)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert captured[0].proxy.session.closed
        assert hass.data[DOMAIN].pending_listener is None
        assert hass.data[DOMAIN].entry_id is None
        assert hass.data[DOMAIN].setup_complete is None
        assert hass.data[DOMAIN].unload_task is None
        monkeypatch.setattr(component, "CloudWeatherListener", factory)
        async with entry.setup_lock:
            await entry.async_setup(hass, integration=integration)
        assert entry.state is ConfigEntryState.LOADED
    finally:
        monkeypatch.setattr(component, "CloudWeatherListener", factory)
        for listener in captured:
            await listener.stop()
        if entry.state is ConfigEntryState.LOADED:
            async with entry.setup_lock:
                await entry.async_unload(hass, integration=integration)


@pytest.mark.parametrize("path", PATHS)
async def test_forward_timeout_releases_shutdown_drain(
    receiver, monkeypatch, aiohttp_server, path
):
    """A real loopback read timeout releases the lease before session closure."""
    from aiohttp import ClientTimeout, web

    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(_request):
        entered.set()
        await release.wait()
        return web.Response(text="synthetic upstream")

    app = web.Application()
    app.router.add_get("/synthetic", upstream)
    server = await aiohttp_server(app)
    await listener.update_config(proxy_sinks=list(DataSink))
    proxy = listener.proxy
    session = proxy.session
    timed_out = []

    async def local_forward(_sink, _request):
        try:
            return await session.get(
                server.make_url("/synthetic"), timeout=ClientTimeout(sock_read=0.1)
            )
        except TimeoutError as err:
            timed_out.append(err)
            raise

    monkeypatch.setattr(proxy, "forward", local_forward)
    request = asyncio.create_task(client.get(path))
    await asyncio.wait_for(entered.wait(), 5)
    stopping = asyncio.create_task(hass.async_stop(force=True))
    try:
        assert (await asyncio.wait_for(request, 5)).status == 200
        await asyncio.wait_for(stopping, 5)
        assert len(timed_out) == 1
        assert session.closed
        assert writes
        assert hass.data[DOMAIN].receiver.in_flight == 0
        assert hass.data[DOMAIN].receiver.listener is None
        assert not hass.data[DOMAIN].receiver.accepting
    finally:
        release.set()
        await asyncio.gather(request, stopping, return_exceptions=True)
