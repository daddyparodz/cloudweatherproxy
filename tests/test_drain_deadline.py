"""Bounded shutdown on isolated HA HTTP routes and owned request tasks."""
# ruff: noqa: F811
import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from custom_components import cloudweatherproxy as component
from custom_components.cloudweatherproxy import web as receiver_web
from custom_components.cloudweatherproxy.const import DOMAIN
from custom_components.cloudweatherproxy.diagnostics import async_get_config_entry_diagnostics
from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink
from homeassistant.config_entries import ConfigEntryState
from test_receiver_lifecycle import PATHS, receiver  # noqa: F401
from test_real_entry_lifecycle import integration_for


async def test_shared_deadline_cancels_only_accepted_requests(monkeypatch):
    """Concurrent drains share one deadline and one cancellation settlement."""
    monkeypatch.setattr(receiver_web, "DRAIN_TIMEOUT", 0.05, raising=False)
    monkeypatch.setattr(receiver_web, "CANCEL_SETTLE_TIMEOUT", 0.05, raising=False)
    manager = receiver_web.ReceiverManager()
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = 0

    class Listener:
        async def handler(self, request):
            nonlocal cancelled
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled += 1
                await release.wait()
            return web.Response()

    manager.listener = Listener()
    accepted = asyncio.create_task(manager.handler(make_mocked_request("GET", PATHS[0])))
    unrelated = asyncio.create_task(release.wait())
    await entered.wait()
    try:
        first = asyncio.create_task(manager.drain())
        await asyncio.sleep(0.02)
        second = asyncio.create_task(manager.drain())
        async with asyncio.timeout(0.5):
            await asyncio.gather(first, second)
        assert manager.in_flight == 1
        assert cancelled == 1
        assert not unrelated.cancelled()
        deadline = manager.drain_deadline
        start = time.monotonic()
        await manager.drain()
        assert time.monotonic() - start < 0.03
        assert manager.drain_deadline == deadline
        assert cancelled == 1
        assert (await manager.handler(make_mocked_request("GET", PATHS[0]))).status == 503
    finally:
        release.set()
        await asyncio.gather(accepted, unrelated)


@pytest.mark.parametrize("path", PATHS)
async def test_permanent_request_is_cancelled_before_cleanup(receiver, monkeypatch, path):
    """Both real HTTP protocols settle cancellation before listener retirement."""
    monkeypatch.setattr(receiver_web, "DRAIN_TIMEOUT", 0.05, raising=False)
    hass, entry, client, writes = receiver
    listener = entry.runtime_data.listener
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(data):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            assert listener.new_dataset_cb
            cancelled.set()
            raise

    method = "process_wunderground" if path == PATHS[0] else "process_weathercloud"
    monkeypatch.setattr(listener, method, blocked)
    request = asyncio.create_task(client.get(path))
    await entered.wait()
    # Keep RED bounded even when the old unload implementation consumes timeout.
    fallback = asyncio.get_running_loop().call_later(0.4, release.set)
    try:
        async with asyncio.timeout(0.5):
            assert await component.async_unload_entry(hass, entry)
        assert cancelled.is_set()
        assert hass.data[DOMAIN].receiver.in_flight == 0
    finally:
        fallback.cancel()
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def real_loaded(receiver):
    """Enter LOADED using HA's actual setup wrapper."""
    hass, entry, client, writes = receiver
    integration = await integration_for(entry)
    assert await component.async_unload_entry(hass, entry)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    return hass, entry, integration


@pytest.mark.parametrize("raises", [False, True])
async def test_stop_preserves_deadline_after_platform_refusal(receiver, monkeypatch, raises):
    """A concurrent shutdown must keep its first deadline through refusal/errors."""
    hass, entry, integration = await real_loaded(receiver)
    domain = hass.data[DOMAIN]
    entered, release = asyncio.Event(), asyncio.Event()

    async def refuse(*args):
        entered.set()
        await release.wait()
        if raises:
            raise RuntimeError("synthetic platform refusal")
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", refuse)

    async def unload():
        async with entry.setup_lock:
            return await entry.async_unload(hass, integration=integration)

    unloading = asyncio.create_task(unload())
    await entered.wait()
    deadline = domain.receiver.drain_deadline
    stopping = asyncio.create_task(hass.async_stop(force=True))
    try:
        async with asyncio.timeout(1):
            while not domain.stopping:
                await asyncio.sleep(0)
        release.set()
        assert not await unloading
        await stopping
        assert domain.receiver.drain_deadline == deadline
        assert not domain.receiver.accepting
        assert domain.entry_id is None
    finally:
        release.set()
        await asyncio.gather(unloading, stopping, return_exceptions=True)


@pytest.mark.parametrize("stop_during_reload", [False, True])
async def test_real_reload_joins_drain_and_cannot_reopen_during_stop(
    receiver, monkeypatch, stop_during_reload
):
    """Run ConfigEntries.async_reload with real wrappers and concurrent stop."""
    monkeypatch.setattr(receiver_web, "DRAIN_TIMEOUT", 0.08)
    hass, entry, integration = await real_loaded(receiver)
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    entry._integration_for_domain = integration
    hass.config.components.add(DOMAIN)
    monkeypatch.setattr(hass.config_entries, "async_get_known_entry", lambda entry_id: entry)
    entered, release = asyncio.Event(), asyncio.Event()
    process = listener.process_wunderground

    async def blocked(data):
        entered.set()
        await release.wait()
        assert listener.new_dataset_cb
        assert not session.closed
        return await process(data)

    monkeypatch.setattr(listener, "process_wunderground", blocked)
    request = asyncio.create_task(receiver[2].get(PATHS[0]))
    await entered.wait()
    reloading = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
    domain = hass.data[DOMAIN]
    stopping = None
    fallback = asyncio.get_running_loop().call_later(1, release.set)
    try:
        async with asyncio.timeout(0.5):
            while domain.receiver.accepting:
                await asyncio.sleep(0)
        deadline = domain.receiver.drain_deadline
        assert deadline is not None
        status = (await async_get_config_entry_diagnostics(hass, entry))["receiver_lifecycle"]
        assert status["drain_deadline"] == deadline
        assert status["drain_timeout_seconds"] == 30.0
        assert status["owns_entry"]
        assert status["in_flight"] == 1
        assert not status["accepting"]
        assert (await receiver[2].get(PATHS[0])).status == 503
        if stop_during_reload:
            stopping = asyncio.create_task(hass.async_stop(force=True))
            while not domain.stopping:
                await asyncio.sleep(0)
            assert domain.receiver.drain_deadline == deadline
        else:
            release.set()
            assert (await request).status == 200
        assert await reloading is (not stop_during_reload)
        if stopping is not None:
            await stopping
            assert entry.state is ConfigEntryState.SETUP_ERROR
            assert not domain.receiver.accepting
            assert domain.receiver.listener is None
        else:
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data.listener is not listener
            assert (await receiver[2].get(PATHS[0])).status == 200
        assert session.closed
    finally:
        fallback.cancel()
        release.set()
        request.cancel()
        await asyncio.gather(request, reloading, *([stopping] if stopping else []), return_exceptions=True)


async def test_real_noncooperative_request_retains_callback_and_session(receiver, monkeypatch):
    """An expired retry neither cancels again nor retires an occupied listener."""
    monkeypatch.setattr(receiver_web, "DRAIN_TIMEOUT", 0.05, raising=False)
    monkeypatch.setattr(receiver_web, "CANCEL_SETTLE_TIMEOUT", 0.05, raising=False)
    hass, entry, integration = await real_loaded(receiver)
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    entered, release = asyncio.Event(), asyncio.Event()
    cancellations = 0
    process = listener.process_wunderground

    async def blocked(data):
        nonlocal cancellations
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellations += 1
            await release.wait()
        assert listener.new_dataset_cb
        assert not session.closed
        return await process(data)

    monkeypatch.setattr(listener, "process_wunderground", blocked)
    accepted = asyncio.create_task(hass.data[DOMAIN].receiver.handler(
        make_mocked_request("GET", PATHS[0])
    ))
    await entered.wait()
    fallback = asyncio.get_running_loop().call_later(1, release.set)
    try:
        async with entry.setup_lock:
            assert not await entry.async_unload(hass, integration=integration)
        assert entry.state is ConfigEntryState.FAILED_UNLOAD
        domain = hass.data[DOMAIN]
        deadline = domain.receiver.drain_deadline
        start = time.monotonic()
        assert not await component.async_unload_entry(hass, entry)
        assert time.monotonic() - start < 0.03
        assert domain.receiver.drain_deadline == deadline
        assert cancellations == 1
        assert listener.new_dataset_cb
        assert not session.closed
        assert domain.receiver.listener is listener
        assert domain.entry_id == entry.entry_id
        release.set()
        await accepted
        await hass.async_stop(force=True)
        assert session.closed
        assert domain.entry_id is None
    finally:
        fallback.cancel()
        release.set()
        await asyncio.gather(accepted, return_exceptions=True)


@pytest.mark.parametrize("path", PATHS)
async def test_forwarding_deadline_and_concurrent_stop(receiver, monkeypatch, path):
    """A permanent forward is owned through concurrent real unload and HA stop."""
    monkeypatch.setattr(receiver_web, "DRAIN_TIMEOUT", 0.05, raising=False)
    hass, entry, integration = await real_loaded(receiver)
    client = receiver[2]
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=list(DataSink))
    session = listener.proxy.session
    entered, release = asyncio.Event(), asyncio.Event()
    cancellations = 0

    async def forward(*args):
        nonlocal cancellations
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellations += 1
            assert not session.closed
            assert listener.new_dataset_cb
            raise

    monkeypatch.setattr(listener.proxy, "forward", forward)
    request = asyncio.create_task(client.get(path))
    await entered.wait()

    async def unload():
        async with entry.setup_lock:
            return await entry.async_unload(hass, integration=integration)

    fallback = asyncio.get_running_loop().call_later(1, release.set)
    unloading = asyncio.create_task(unload())
    await asyncio.sleep(0)
    stopping = asyncio.create_task(hass.async_stop(force=True))
    try:
        async with asyncio.timeout(0.8):
            assert await unloading
            await stopping
        assert cancellations == 1
        assert session.closed
        assert not hass.data[DOMAIN].receiver.accepting
        assert not await component.async_setup_entry(hass, entry)
    finally:
        fallback.cancel()
        release.set()
        request.cancel()
        await asyncio.gather(request, unloading, stopping, return_exceptions=True)


@pytest.mark.parametrize("failure", ["exception", "blocked", "noncooperative"])
async def test_real_cleanup_failure_quarantines_without_false_success(
    receiver, monkeypatch, failure
):
    """HA keeps FAILED_UNLOAD; stop can still recover the domain-owned session."""
    monkeypatch.setattr(component, "CLEANUP_TIMEOUT", 0.05, raising=False)
    monkeypatch.setattr(component, "CANCEL_SETTLE_TIMEOUT", 0.05, raising=False)
    hass, entry, integration = await real_loaded(receiver)
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session, original = listener.proxy.session, listener.stop
    release = asyncio.Event()
    calls = 0

    async def stop():
        nonlocal calls
        calls += 1
        if failure == "exception":
            raise RuntimeError("synthetic cleanup failure")
        try:
            await release.wait()
        except asyncio.CancelledError:
            if failure == "blocked":
                raise
            await release.wait()
        await original()

    monkeypatch.setattr(listener, "stop", stop)
    fallback = asyncio.get_running_loop().call_later(1, release.set)
    try:
        start = time.monotonic()
        async with entry.setup_lock:
            assert not await entry.async_unload(hass, integration=integration)
        assert time.monotonic() - start < 0.5
        assert entry.state is ConfigEntryState.FAILED_UNLOAD
        assert hasattr(entry, "runtime_data")
        domain = hass.data[DOMAIN]
        assert domain.entry_id == entry.entry_id
        assert domain.pending_listener is listener
        assert domain.cleanup_pending
        assert not domain.receiver.accepting
        assert not session.closed
        # HA refuses a second unload/reload of a nonrecoverable entry.
        async with entry.setup_lock:
            assert not await entry.async_unload(hass, integration=integration)
        if failure == "noncooperative":
            cleanup_task = domain.cleanup_task
            deadlines = (domain.cleanup_deadline, domain.cleanup_cancel_deadline)
            assert cleanup_task is not None
            assert not await component.async_unload_entry(hass, entry)
            assert calls == 1
            with pytest.raises(RuntimeError, match="quarantined"):
                await component.async_setup_entry(hass, entry)
            assert domain.cleanup_task is cleanup_task
            assert cleanup_task.cancelling() == 1
            assert (domain.cleanup_deadline, domain.cleanup_cancel_deadline) == deadlines
            assert not domain.receiver.accepting
        release.set()
        await asyncio.sleep(0)
        if domain.cleanup_task is not None:
            await asyncio.shield(domain.cleanup_task)
        monkeypatch.setattr(listener, "stop", original)
        await hass.async_stop(force=True)
        assert session.closed
        assert domain.pending_listener is None
        assert domain.entry_id is None
        assert not await component.async_setup_entry(hass, entry)
    finally:
        fallback.cancel()
        release.set()
        monkeypatch.setattr(listener, "stop", original)
        await original()


async def test_real_thirty_second_http_drain(receiver, monkeypatch):
    """Use the production deadline and real clock, with synthetic local HTTP only."""
    hass, entry, integration = await real_loaded(receiver)
    client = receiver[2]
    listener = entry.runtime_data.listener
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started = time.monotonic()

    async def blocked(data):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            assert listener.new_dataset_cb
            raise

    monkeypatch.setattr(listener, "process_wunderground", blocked)
    request = asyncio.create_task(client.get(PATHS[0]))
    await entered.wait()
    start = time.monotonic()
    fallback = asyncio.get_running_loop().call_later(35, release.set)
    try:
        async with entry.setup_lock:
            assert await entry.async_unload(hass, integration=integration)
        elapsed = time.monotonic() - start
        print(f"real HTTP drain elapsed={elapsed:.3f}s total={time.monotonic()-started:.3f}s")  # noqa: T201 -- saved clock proof
        assert 29.9 <= elapsed < 34
        assert cancelled.is_set()
        assert entry.state is ConfigEntryState.NOT_LOADED
    finally:
        fallback.cancel()
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
