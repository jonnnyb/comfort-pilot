"""Comfort Pilot: solar-aware, occupancy-aware control for split systems."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, NAME, PLATFORMS
from .coordinator import ComfortPilotCoordinator

type ComfortPilotConfigEntry = ConfigEntry[ComfortPilotCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ComfortPilotConfigEntry) -> bool:
    coordinator = ComfortPilotCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Create the house device first so room devices can hang off it.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_house")},
        name=NAME,
        manufacturer=NAME,
        model="House",
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ComfortPilotConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ComfortPilotConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded
