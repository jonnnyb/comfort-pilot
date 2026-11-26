"""Sensors: the per-room "why" status and house-level readings."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ComfortPilotConfigEntry
from .engine import SEASON_COOLING, SEASON_HEATING, SEASON_NEUTRAL
from .entity import HouseEntity, RoomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ComfortPilotConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities: list[SensorEntity] = [
        HouseStatusSensor(c),
        SolarBudgetSensor(c),
        SeasonSensor(c),
        IndoorAverageSensor(c),
        OverrideCountSensor(c),
    ]
    entities += [RoomStatusSensor(c, room) for room in c.rooms]
    async_add_entities(entities)


class RoomStatusSensor(RoomEntity, SensorEntity):
    """Plain-English explanation of what Comfort Pilot is doing in a room."""

    _attr_translation_key = "room_status"
    _attr_icon = "mdi:thermostat-auto"

    def __init__(self, coordinator, room: str) -> None:
        super().__init__(coordinator, room, "status")

    @property
    def _decision(self):
        data = self.coordinator.data
        return data.result.decisions.get(self.room) if data else None

    @property
    def native_value(self) -> str | None:
        d = self._decision
        if d is None:
            return None
        if d.target is not None and d.hvac_mode not in (None, "off"):
            text = f"{d.hvac_mode.title()} {d.target:g}° – {d.reason}"
        else:
            text = d.reason
        return text[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._decision
        data = self.coordinator.data
        st = data.room_states.get(self.room) if data else None
        hold = self.coordinator.holds.get(self.room)
        rules = [r.describe() for r in self.coordinator.rules if self.room in r.rooms]
        return {
            "level": d.level if d else None,
            "hvac_mode": d.hvac_mode if d else None,
            "target": d.target if d else None,
            "reason": d.reason if d else None,
            "room_temperature": st.current_temp if st else None,
            "occupied": st.occupied_now if st else None,
            "room_mode": self.coordinator.room_modes.get(self.room, "auto"),
            "hold_until": hold.isoformat() if hold else None,
            "rules": rules,
        }


class HouseStatusSensor(HouseEntity, SensorEntity):
    _attr_translation_key = "house_status"
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        counts = Counter(d.level for d in data.result.decisions.values())
        parts = [f"{n} {level}" for level, n in sorted(counts.items())]
        return ", ".join(parts) or "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        c = self.coordinator
        return {
            "house_mode": c.house_mode,
            "fresh_air": c.fresh_air,
            "season": c.season,
            "outside_temperature": data.outside if data else None,
            "indoor_average": data.inside if data else None,
            "forecast_high": data.forecast_high if data else None,
            "forecast_low": data.forecast_low if data else None,
            "grid_power_avg": data.grid_power if data else None,
            "anyone_home": data.anyone_home if data else None,
            "fresh_air_advice": data.advice if data else None,
            "rules": [r.describe() for r in c.rules],
        }


class SolarBudgetSensor(HouseEntity, SensorEntity):
    """Power available for comfort: export plus what the splits are already using."""

    _attr_translation_key = "solar_budget"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "solar_budget")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.result.solar_budget if data else None


class SeasonSensor(HouseEntity, SensorEntity):
    _attr_translation_key = "season"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [SEASON_COOLING, SEASON_HEATING, SEASON_NEUTRAL]
    _attr_icon = "mdi:sun-snowflake-variant"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "season")

    @property
    def native_value(self) -> str:
        return self.coordinator.season


class IndoorAverageSensor(HouseEntity, SensorEntity):
    _attr_translation_key = "indoor_average"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "indoor_average")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.inside if data else None


class OverrideCountSensor(HouseEntity, SensorEntity):
    """How many manual changes have been recorded for learning."""

    _attr_translation_key = "manual_changes"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:hand-back-right"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "manual_changes")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.overrides)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"recent": self.coordinator.overrides[-10:]}
