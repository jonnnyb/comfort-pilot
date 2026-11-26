"""Integration tests against a real (test) Home Assistant core."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.comfort_pilot.const import (
    ACTION_FRESH_AIR_ON,
    DOMAIN,
    OPT_ROOMS,
    OPT_RULES,
    OPT_SETTINGS,
)
from custom_components.comfort_pilot.config_flow import detect_room

OFFICE = "climate.office"
BEDROOM = "climate.bedroom"


class FakeSplits:
    """Minimal climate + notify + weather services that record calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, dict]] = []
        self.notifications: list[dict] = []
        hass.services.async_register("climate", "set_temperature", self._set_temperature)
        hass.services.async_register("climate", "set_hvac_mode", self._set_hvac_mode)
        hass.services.async_register("notify", "mobile_app_phone", self._notify)
        hass.services.async_register(
            "weather", "get_forecasts", self._forecast, supports_response=SupportsResponse.ONLY
        )
        self.forecast_high = 34.0

    def set_unit(self, entity_id, mode="off", target=24.0, current=27.0, context=None):
        self.hass.states.async_set(
            entity_id,
            mode,
            {
                "current_temperature": current,
                "temperature": target,
                "hvac_modes": ["off", "cool", "heat", "dry", "fan_only"],
                "min_temp": 16,
                "max_temp": 31,
                "target_temp_step": 0.5,
                "friendly_name": entity_id.split(".")[1].title(),
            },
            context=context,
        )

    def _update(self, entity_id, mode=None, target=None):
        st = self.hass.states.get(entity_id)
        attrs = dict(st.attributes)
        if target is not None:
            attrs["temperature"] = target
        self.hass.states.async_set(entity_id, mode or st.state, attrs)

    async def _set_temperature(self, call: ServiceCall):
        data = dict(call.data)
        ids = data.pop("entity_id")
        ids = ids if isinstance(ids, list) else [ids]
        self.calls.append(("set_temperature", {"entity_id": ids[0], **data}))
        for e in ids:
            self._update(e, data.get("hvac_mode"), data.get("temperature"))

    async def _set_hvac_mode(self, call: ServiceCall):
        data = dict(call.data)
        ids = data.pop("entity_id")
        ids = ids if isinstance(ids, list) else [ids]
        self.calls.append(("set_hvac_mode", {"entity_id": ids[0], **data}))
        for e in ids:
            self._update(e, data["hvac_mode"])

    async def _notify(self, call: ServiceCall):
        self.notifications.append(dict(call.data))

    async def _forecast(self, call: ServiceCall):
        if call.data.get("type") != "daily":
            raise ValueError("unsupported")
        now = dt_util.now()
        return {
            "weather.home": {
                "forecast": [
                    {"datetime": now.isoformat(), "temperature": self.forecast_high, "templow": 19}
                ]
            }
        }


@pytest.fixture
async def world(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Australia/Perth")
    fake = FakeSplits(hass)
    fake.set_unit(OFFICE)
    fake.set_unit(BEDROOM)
    hass.states.async_set("sensor.grid_power", "-2500", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set("weather.home", "sunny", {"temperature": 31})
    hass.states.async_set("person.me", "home")
    hass.states.async_set("binary_sensor.office_motion", "on", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.bedroom_motion", "off", {"device_class": "motion"})
    return fake


def make_entry(rules=None, settings=None):
    rooms = {
        OFFICE: {"name": "Office", "occupancy_sensors": ["binary_sensor.office_motion"], "window_sensors": []},
        BEDROOM: {"name": "Bedroom", "occupancy_sensors": ["binary_sensor.bedroom_motion"], "window_sensors": []},
    }
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Comfort Pilot",
        data={
            "climates": [OFFICE, BEDROOM],
            "grid_sensor": "sensor.grid_power",
            "weather": "weather.home",
            "notify": "notify.mobile_app_phone",
        },
        options={OPT_ROOMS: rooms, OPT_RULES: rules or [], OPT_SETTINGS: settings or {}},
    )


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def test_occupied_room_cools_at_comfort_on_solar(hass, world):
    coordinator = await setup(hass, make_entry())
    office_calls = [c for c in world.calls if c[1]["entity_id"] == OFFICE]
    assert office_calls == [
        ("set_temperature", {"entity_id": OFFICE, "hvac_mode": "cool", "temperature": 23.0})
    ]
    # Bedroom is empty and already off: left alone.
    assert not [c for c in world.calls if c[1]["entity_id"] == BEDROOM]

    status = hass.states.get("sensor.comfort_pilot_office_status")
    assert status.state.startswith("Cool 23° – Occupied – solar available")
    assert hass.states.get("sensor.comfort_pilot_season").state == "cooling"
    assert coordinator.data.forecast_high == 34.0


async def test_grid_import_moves_to_economy(hass, world):
    hass.states.async_set("sensor.grid_power", "600", {"unit_of_measurement": "W"})
    await setup(hass, make_entry())
    assert world.calls[-1] == (
        "set_temperature", {"entity_id": OFFICE, "hvac_mode": "cool", "temperature": 26.0}
    )


async def test_kw_grid_sensor_is_converted(hass, world):
    hass.states.async_set("sensor.grid_power", "-2.5", {"unit_of_measurement": "kW"})
    coordinator = await setup(hass, make_entry())
    assert coordinator.data.grid_power == -2500


async def test_manual_change_creates_hold_and_is_recorded(hass, world):
    coordinator = await setup(hass, make_entry())
    # Pretend the device echo grace period has passed.
    coordinator._expected[OFFICE].at -= timedelta(minutes=5)
    world.calls.clear()

    # User grabs the remote and sets 21°.
    world.set_unit(OFFICE, mode="cool", target=21.0)
    await hass.async_block_till_done()

    assert OFFICE in coordinator.holds
    assert len(coordinator.overrides) == 1
    entry = coordinator.overrides[0]
    assert entry["to_target"] == 21.0 and entry["from_target"] == 23.0

    await coordinator.async_refresh()
    assert not [c for c in world.calls if c[1]["entity_id"] == OFFICE]
    status = hass.states.get("sensor.comfort_pilot_office_status")
    assert "Manual change" in status.state
    assert hass.states.get("sensor.comfort_pilot_manual_changes_recorded").state == "1"

    # Resume button clears the hold and Comfort Pilot takes over again.
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.comfort_pilot_office_resume_auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert OFFICE not in coordinator.holds
    assert world.calls[-1][1]["temperature"] == 23.0


async def test_own_changes_are_not_overrides(hass, world):
    coordinator = await setup(hass, make_entry())
    assert coordinator.overrides == []
    assert coordinator.holds == {}


async def test_fresh_air_notification_and_action(hass, world):
    hass.states.async_set("weather.home", "clear-night", {"temperature": 19})
    world.forecast_high = 30
    entry = make_entry(settings={"quiet_start": "00:00:00", "quiet_end": "00:00:00"})
    coordinator = await setup(hass, entry)

    assert len(world.notifications) == 1
    note = world.notifications[0]
    assert note["title"] == "Fresh air time"
    assert note["data"]["actions"][0]["action"] == ACTION_FRESH_AIR_ON

    # Not repeated on the next run.
    await coordinator.async_refresh()
    assert len(world.notifications) == 1

    # Tapping the notification button turns on fresh air mode: splits off.
    hass.bus.async_fire("mobile_app_notification_action", {"action": ACTION_FRESH_AIR_ON})
    await hass.async_block_till_done()
    assert coordinator.fresh_air is True
    assert hass.states.get("switch.comfort_pilot_fresh_air_mode").state == "on"
    assert hass.states.get(OFFICE).state == "off"
    assert "Fresh air mode" in hass.states.get("sensor.comfort_pilot_office_status").state


async def test_house_mode_select_and_room_mode(hass, world):
    coordinator = await setup(hass, make_entry())
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.comfort_pilot_house_mode", "option": "away"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(OFFICE).state == "off"
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.comfort_pilot_house_mode", "option": "auto"}, blocking=True,
    )
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.comfort_pilot_office_mode", "option": "off"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(OFFICE).state == "off"
    assert coordinator.room_modes[OFFICE] == "off"


async def test_state_persists_across_reload(hass, world):
    entry = make_entry()
    coordinator = await setup(hass, entry)
    await coordinator.async_set_house_mode("boost")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.house_mode == "boost"
    assert hass.states.get("select.comfort_pilot_house_mode").state == "boost"


async def test_rule_keeps_bedroom_cool(hass, world):
    rule = {
        "id": "r1", "room": BEDROOM, "name": "Bedroom at night", "days": list(range(7)),
        "start": "00:00:00", "end": "23:59:00", "action": "comfort", "target": 24,
    }
    await setup(hass, make_entry(rules=[rule]))
    bedroom = [c for c in world.calls if c[1]["entity_id"] == BEDROOM]
    assert bedroom == [
        ("set_temperature", {"entity_id": BEDROOM, "hvac_mode": "cool", "temperature": 24.0})
    ]
    attrs = hass.states.get("sensor.comfort_pilot_bedroom_status").attributes
    assert attrs["rules"] == ["Bedroom at night: keep comfortable at 24°, every day 00:00–23:59"]


async def test_window_open_turns_unit_off_after_delay(hass, world):
    entry = make_entry()
    entry.options[OPT_ROOMS][OFFICE]["window_sensors"] = ["binary_sensor.office_window"]
    hass.states.async_set("binary_sensor.office_window", "off", {"device_class": "window"})
    coordinator = await setup(hass, entry)
    assert hass.states.get(OFFICE).state == "cool"
    hass.states.async_set("binary_sensor.office_window", "on", {"device_class": "window"})
    await hass.async_block_till_done()
    assert hass.states.get(OFFICE).state == "cool"  # not yet
    future = dt_util.utcnow() + timedelta(seconds=200)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dt_util, "utcnow", lambda: future)
        mp.setattr(dt_util, "now", lambda tz=None: dt_util.as_local(future))
        async_fire_time_changed(hass, future)
        await hass.async_block_till_done()
        await coordinator.async_refresh()
    assert hass.states.get(OFFICE).state == "off"


# --- config & options flows -------------------------------------------------

async def test_config_flow_detects_rooms(hass, world):
    areas = ar.async_get(hass)
    office_area = areas.async_create("Study")
    entities = er.async_get(hass)
    climate = entities.async_get_or_create("climate", "esphome", "study_unit", suggested_object_id="study_unit")
    entities.async_update_entity(climate.entity_id, area_id=office_area.id)
    motion = entities.async_get_or_create(
        "binary_sensor", "zha", "office_motion", suggested_object_id="office_motion",
        original_device_class="motion",
    )
    entities.async_update_entity(motion.entity_id, area_id=office_area.id)
    window = entities.async_get_or_create(
        "binary_sensor", "zha", "office_window", suggested_object_id="office_window",
        original_device_class="window",
    )
    entities.async_update_entity(window.entity_id, area_id=office_area.id)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"climates": [climate.entity_id], "grid_sensor": "sensor.grid_power", "weather": "weather.home",
         "notify": "notify.mobile_app_phone"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    room = result["options"][OPT_ROOMS][climate.entity_id]
    assert room["name"] == "Study"
    assert room["occupancy_sensors"] == [motion.entity_id]
    assert room["window_sensors"] == [window.entity_id]
    await hass.async_block_till_done()


async def test_config_flow_validation(hass, world):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"climates": [OFFICE], "grid_sensor": "sensor.grid_power", "notify": "phone"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_outside", "notify": "bad_notify"}


async def test_options_add_and_remove_rule(hass, world):
    entry = make_entry()
    await setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_rule"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"room": BEDROOM, "name": "Bedroom night", "action": "comfort", "days": ["0", "1", "2", "3", "4", "5", "6"],
         "start": "21:00:00", "end": "06:30:00", "target": 24},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.options[OPT_RULES]) == 1
    assert entry.runtime_data.rules[0].describe() == "Bedroom night: keep comfortable at 24°, every day 21:00–06:30"

    rule_id = entry.options[OPT_RULES][0]["id"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove_rule"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"rules": [rule_id]})
    await hass.async_block_till_done()
    assert entry.options[OPT_RULES] == []


async def test_options_room_edit_and_settings(hass, world):
    entry = make_entry()
    await setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rooms"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"room": OFFICE})
    assert result["step_id"] == "room_edit"
    form = {"name": "Office", "occupancy_sensors": ["binary_sensor.office_motion"], "window_sensors": [],
            "cool_min": 25, "cool_max": 24, "heat_min": 18, "heat_max": 21, "priority": "high",
            "est_power": 700, "occupancy_timeout": 15}
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["errors"] == {"cool_min": "band_order"}
    form["cool_min"] = 22.5
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.rooms[OFFICE].cool_min == 22.5
    assert entry.runtime_data.rooms[OFFICE].priority == "high"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert result["step_id"] == "settings"
    defaults = {k.schema: k.default() for k in result["data_schema"].schema}
    defaults["safety_max"] = 30
    result = await hass.config_entries.options.async_configure(result["flow_id"], defaults)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.settings.safety_max == 30
    assert entry.runtime_data.hold_minutes == 120


def test_detect_room_without_area(hass):
    room = detect_room(hass, "climate.nowhere")
    assert room["name"] == "Nowhere" and room["occupancy_sensors"] == []
