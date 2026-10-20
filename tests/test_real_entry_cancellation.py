"""Cancellation at the HA unload boundary must not strand its state machine."""
# ruff: noqa: F811  # Imported pytest fixture names are intentionally injected.
import asyncio

from homeassistant.config_entries import ConfigEntryState
from custom_components import cloudweatherproxy as component
from custom_components.cloudweatherproxy.const import DOMAIN
from test_receiver_lifecycle import receiver  # noqa: F401
from test_real_entry_lifecycle import integration_for


async def test_real_cancelled_drain_completes_unload(receiver, monkeypatch):
    """Cancellation is deferred across the entire atomic HA unload operation."""
    hass, entry, client, writes = receiver
    integration = await integration_for(entry)
    assert await component.async_unload_entry(hass, entry)
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    entered, release = asyncio.Event(), asyncio.Event()
    manager = hass.data[DOMAIN].receiver
    drain = manager.drain

    async def blocked():
        entered.set()
        await release.wait()
        await drain()

    monkeypatch.setattr(manager, "drain", blocked)

    async def unload():
        async with entry.setup_lock:
            return await entry.async_unload(hass, integration=integration)

    task = asyncio.create_task(unload())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    assert await task
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(entry, "runtime_data")
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=integration)
    assert entry.state is ConfigEntryState.LOADED
