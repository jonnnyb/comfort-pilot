"""Base entities for Comfort Pilot."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import ComfortPilotCoordinator


class HouseEntity(CoordinatorEntity[ComfortPilotCoordinator]):
    """An entity on the house-level Comfort Pilot device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComfortPilotCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_house_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_house")},
            name=NAME,
            manufacturer=NAME,
            model="House",
        )


class RoomEntity(CoordinatorEntity[ComfortPilotCoordinator]):
    """An entity on a per-room Comfort Pilot device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComfortPilotCoordinator, room: str, key: str) -> None:
        super().__init__(coordinator)
        self.room = room
        entry_id = coordinator.config_entry.entry_id
        cfg = coordinator.rooms[room]
        self._attr_unique_id = f"{entry_id}_{room}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{room}")},
            name=f"{NAME} {cfg.name}",
            manufacturer=NAME,
            model="Room",
            via_device=(DOMAIN, f"{entry_id}_house"),
        )
