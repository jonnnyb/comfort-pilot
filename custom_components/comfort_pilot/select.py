"""House mode and per-room mode selectors."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ComfortPilotConfigEntry
from .engine import HOUSE_MODES, ROOM_AUTO, ROOM_MODES
from .entity import HouseEntity, RoomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ComfortPilotConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([HouseModeSelect(c)] + [RoomModeSelect(c, room) for room in c.rooms])


class HouseModeSelect(HouseEntity, SelectEntity):
    _attr_translation_key = "house_mode"
    _attr_options = HOUSE_MODES
    _attr_icon = "mdi:home-automation"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "house_mode")

    @property
    def current_option(self) -> str:
        return self.coordinator.house_mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_house_mode(option)


class RoomModeSelect(RoomEntity, SelectEntity):
    _attr_translation_key = "room_mode"
    _attr_options = ROOM_MODES
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator, room: str) -> None:
        super().__init__(coordinator, room, "mode")

    @property
    def current_option(self) -> str:
        return self.coordinator.room_modes.get(self.room, ROOM_AUTO)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_room_mode(self.room, option)
