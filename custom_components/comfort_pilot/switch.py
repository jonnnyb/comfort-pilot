"""Fresh air mode switch (evap fan / windows open: splits pause)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ComfortPilotConfigEntry
from .entity import HouseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ComfortPilotConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([FreshAirSwitch(entry.runtime_data)])


class FreshAirSwitch(HouseEntity, SwitchEntity):
    _attr_translation_key = "fresh_air"
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "fresh_air")

    @property
    def is_on(self) -> bool:
        return self.coordinator.fresh_air

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_fresh_air(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_fresh_air(False)
