"""Exercise HA's real entry state machine, holding its required setup lock."""
# ruff: noqa: F811  # Imported pytest fixture names are intentionally injected.
import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from custom_components import cloudweatherproxy as component
from custom_components.cloudweatherproxy.const import DOMAIN
from custom_components.cloudweatherproxy.aiocloudweather.proxy import DataSink
from test_receiver_lifecycle import receiver, make_entry  # noqa: F401


async def integration_for(entry):
    """Supply only loader metadata, not a replacement state machine."""
    integration = AsyncMock()
    integration.domain = DOMAIN
    integration.async_get_component.return_value = component
    entry.supports_unload = True
    entry.supports_remove_device = False
    entry.async_migrate = AsyncMock(return_value=True)
    return integration


@pytest.mark.parametrize("failure", ["stop", "cancel", "cancel_stop"])
async def test_real_unload_recovers_through_setup(receiver, monkeypatch, failure):
    """HA finishes unloading; setup retries owned cleanup before replacing it."""
    hass, entry, client, writes = receiver
    integration = await integration_for(entry)
    # Initial fixture uses direct setup; obtain LOADED via real setup next.
    assert await component.async_unload_entry(hass, entry)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    assert entry.state is ConfigEntryState.LOADED
    listener = entry.runtime_data.listener
    await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
    session = listener.proxy.session
    original = listener.stop
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop():
        entered.set()
        if failure == "stop":
            raise RuntimeError("synthetic stop failure")
        await release.wait()
        if failure == "cancel_stop":
            raise RuntimeError("synthetic stop failure after cancellation")
        await original()

    monkeypatch.setattr(listener, "stop", stop)

    async def unload():
        async with entry.setup_lock:
            return await entry.async_unload(hass, integration=integration)

    task = asyncio.create_task(unload())
    await entered.wait()
    if failure != "stop":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
    try:
        assert await task
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert not hasattr(entry, "runtime_data")
        if failure != "cancel":
            assert not session.closed
            assert hass.data[DOMAIN].cleanup_pending
            assert hass.data[DOMAIN].pending_listener is listener
            assert hass.data[DOMAIN].entry_id == entry.entry_id
            assert hass.data[DOMAIN].receiver.listener is None
            # A retry which still fails must not construct or bind replacements.
            async with entry.setup_lock:
                await entry.async_setup(hass, integration=integration)
            assert entry.state is ConfigEntryState.SETUP_ERROR
            assert hass.data[DOMAIN].pending_listener is listener
            assert not session.closed
            monkeypatch.setattr(listener, "stop", original)
        async with entry.setup_lock:
            await entry.async_setup(hass, integration=integration)
        assert entry.state is ConfigEntryState.LOADED
        assert session.closed
        assert entry.runtime_data.listener is not listener
        assert hass.data[DOMAIN].receiver.listener is entry.runtime_data.listener
    finally:
        await original()


@pytest.mark.parametrize("cancel_setup", [False, True])
async def test_real_failed_setup_cleanup_retried_by_setup(receiver, monkeypatch, cancel_setup):
    """SETUP_ERROR recovery cannot depend on an unload hook HA never invokes."""
    hass, old_entry, client, writes = receiver
    assert await component.async_unload_entry(hass, old_entry)
    entry = make_entry()
    integration = await integration_for(entry)
    forward = hass.config_entries.async_forward_entry_setups
    captured = []

    async def fail(*args):
        listener = entry.runtime_data.listener
        await listener.update_config(proxy_sinks=[DataSink.WEATHERCLOUD])
        captured.append((listener, listener.proxy.session, listener.stop))
        monkeypatch.setattr(listener, "stop", AsyncMock(side_effect=RuntimeError("stop failed")))
        if cancel_setup:
            raise asyncio.CancelledError
        raise RuntimeError("setup failed")

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", fail)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    listener, session, stop = captured[0]
    monkeypatch.setattr(listener, "stop", stop)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    try:
        async with entry.setup_lock:
            await entry.async_setup(hass, integration=integration)
        assert entry.state is ConfigEntryState.LOADED
        assert session.closed
        assert entry.runtime_data.listener is not listener
    finally:
        await stop()
        if entry.state is ConfigEntryState.LOADED:
            async with entry.setup_lock:
                await entry.async_unload(hass, integration=integration)
