"""Per-room "Resume auto" button to cancel a manual hold."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ComfortPilotConfigEntry
from .entity import RoomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ComfortPilotConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([ResumeButton(c, room) for room in c.rooms])


class ResumeButton(RoomEntity, ButtonEntity):
    _attr_translation_key = "resume"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator, room: str) -> None:
        super().__init__(coordinator, room, "resume")

    async def async_press(self) -> None:
        await self.coordinator.async_resume(self.room)
