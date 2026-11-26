"""Unit tests for the pure decision engine."""

from datetime import datetime, time, timedelta, timezone

import pytest

from custom_components.comfort_pilot.engine import (
    ADVICE_CLOSE,
    ADVICE_OPEN,
    HOUSE_AUTO,
    HOUSE_AWAY,
    HOUSE_BOOST,
    HOUSE_PAUSED,
    HVAC_COOL,
    HVAC_HEAT,
    HVAC_OFF,
    LEVEL_COMFORT,
    LEVEL_ECO,
    LEVEL_HOLD,
    LEVEL_OFF,
    LEVEL_PRECOOL,
    LEVEL_RULE,
    LEVEL_SAFETY,
    ROOM_COMFORT,
    ROOM_OFF,
    RULE_COMFORT,
    RULE_OFF,
    SEASON_COOLING,
    SEASON_HEATING,
    SEASON_NEUTRAL,
    HouseState,
    RoomConfig,
    RoomState,
    Rule,
    Settings,
    decide,
    decide_season,
    fresh_air_advice,
)

PERTH = timezone(timedelta(hours=8))
# Wednesday 26 Nov 2026, 14:00
NOW = datetime(2026, 11, 26, 14, 0, tzinfo=PERTH)


def rooms(*names, **kw):
    return {f"climate.{n}": RoomConfig(f"climate.{n}", n.title(), **kw) for n in names}


def occupied(temp=27.0, **kw):
    return RoomState(current_temp=temp, has_occupancy_sensor=True, occupied_now=True, **kw)


def empty(temp=27.0, **kw):
    return RoomState(current_temp=temp, has_occupancy_sensor=True, occupied_now=False, **kw)


def house(**kw):
    defaults = dict(now=NOW, anyone_home=True, grid_power=0.0, outside_temp=32.0, forecast_high=34.0)
    defaults.update(kw)
    return HouseState(**defaults)


S = Settings()


# --- season ----------------------------------------------------------------

def test_season_cooling_heating_neutral():
    assert decide_season(house(forecast_high=30), S) == SEASON_COOLING
    assert decide_season(house(forecast_high=15), S) == SEASON_HEATING
    assert decide_season(house(forecast_high=21), S) == SEASON_NEUTRAL


def test_season_hysteresis_keeps_cooling():
    h = house(forecast_high=25, previous_season=SEASON_COOLING)
    assert decide_season(h, S) == SEASON_COOLING
    h = house(forecast_high=23, previous_season=SEASON_COOLING)
    assert decide_season(h, S) == SEASON_NEUTRAL


def test_season_falls_back_to_outside_temp():
    assert decide_season(house(forecast_high=None, outside_temp=29), S) == SEASON_COOLING


# --- solar vs grid ----------------------------------------------------------

def test_occupied_room_on_solar_gets_comfort():
    r = rooms("office")
    res = decide(r, {"climate.office": occupied()}, [], house(grid_power=-2000), S)
    d = res.decisions["climate.office"]
    assert (d.hvac_mode, d.target, d.level) == (HVAC_COOL, 23.0, LEVEL_COMFORT)
    assert "solar" in d.reason


def test_occupied_room_on_grid_gets_eco():
    r = rooms("office")
    res = decide(r, {"climate.office": occupied()}, [], house(grid_power=400), S)
    d = res.decisions["climate.office"]
    assert (d.hvac_mode, d.target, d.level) == (HVAC_COOL, 26.0, LEVEL_ECO)


def test_running_unit_power_is_reclaimed_so_it_does_not_flap():
    # Office is running at comfort and has soaked up the export: grid ~ 0.
    r = rooms("office")
    st = occupied(hvac_mode=HVAC_COOL, last_level=LEVEL_COMFORT)
    res = decide(r, {"climate.office": st}, [], house(grid_power=50), S)
    assert res.decisions["climate.office"].level == LEVEL_COMFORT


def test_hysteresis_keeps_comfort_with_small_import():
    r = rooms("office", est_power=900)
    st = occupied(hvac_mode=HVAC_COOL, last_level=LEVEL_COMFORT)
    # importing 200 W: budget = 700, threshold for incumbent = 650
    res = decide(r, {"climate.office": st}, [], house(grid_power=200), S)
    assert res.decisions["climate.office"].level == LEVEL_COMFORT
    # importing 400 W: budget = 500 → drops to eco
    res = decide(r, {"climate.office": st}, [], house(grid_power=400), S)
    assert res.decisions["climate.office"].level == LEVEL_ECO


def test_budget_shared_by_priority():
    r = rooms("office", "bedroom")
    r["climate.bedroom"].priority = "high"
    states = {"climate.office": occupied(), "climate.bedroom": occupied()}
    res = decide(r, states, [], house(grid_power=-1000), S)
    assert res.decisions["climate.bedroom"].level == LEVEL_COMFORT
    assert res.decisions["climate.office"].level == LEVEL_ECO


def test_no_grid_reading_falls_back_to_eco():
    r = rooms("office")
    res = decide(r, {"climate.office": occupied()}, [], house(grid_power=None), S)
    assert res.decisions["climate.office"].level == LEVEL_ECO
    assert res.solar_budget is None


# --- occupancy --------------------------------------------------------------

def test_empty_room_turns_off():
    r = rooms("office")
    res = decide(r, {"climate.office": empty()}, [], house(grid_power=-3000), S)
    d = res.decisions["climate.office"]
    assert (d.hvac_mode, d.level, d.reason) == (HVAC_OFF, LEVEL_OFF, "Unoccupied")


def test_recent_motion_counts_as_occupied():
    r = rooms("office", occupancy_timeout=20)
    st = empty(last_occupied=NOW - timedelta(minutes=10))
    res = decide(r, {"climate.office": st}, [], house(), S)
    assert res.decisions["climate.office"].hvac_mode == HVAC_COOL
    st = empty(last_occupied=NOW - timedelta(minutes=30))
    res = decide(r, {"climate.office": st}, [], house(), S)
    assert res.decisions["climate.office"].hvac_mode == HVAC_OFF


def test_nobody_home_turns_everything_off():
    r = rooms("office")
    res = decide(r, {"climate.office": occupied()}, [], house(anyone_home=False), S)
    assert res.decisions["climate.office"].reason == "Nobody home"
    res = decide(r, {"climate.office": occupied()}, [], house(house_mode=HOUSE_AWAY), S)
    assert res.decisions["climate.office"].hvac_mode == HVAC_OFF


def test_room_without_sensor_follows_home_presence():
    r = rooms("study")
    st = RoomState(current_temp=27)
    res = decide(r, {"climate.study": st}, [], house(), S)
    assert res.decisions["climate.study"].hvac_mode == HVAC_COOL


# --- rules ------------------------------------------------------------------

def bedroom_night_rule(**kw):
    data = dict(id="r1", room="climate.bedroom", name="Bedroom cool at night",
                days=list(range(7)), start=time(21, 0), end=time(6, 30), action=RULE_COMFORT)
    data.update(kw)
    return Rule(**data)


def test_overnight_rule_active_after_midnight():
    rule = bedroom_night_rule()
    assert rule.active_at(NOW.replace(hour=2))
    assert rule.active_at(NOW.replace(hour=22))
    assert not rule.active_at(NOW.replace(hour=14))


def test_rule_day_filter_for_overnight_window():
    # Only Fridays (4). Saturday 02:00 belongs to Friday's window.
    rule = bedroom_night_rule(days=[4])
    fri_22 = datetime(2026, 11, 27, 22, 0, tzinfo=PERTH)
    sat_02 = datetime(2026, 11, 28, 2, 0, tzinfo=PERTH)
    thu_02 = datetime(2026, 11, 26, 2, 0, tzinfo=PERTH)
    assert rule.active_at(fri_22) and rule.active_at(sat_02)
    assert not rule.active_at(thu_02)


def test_comfort_rule_with_target_keeps_room_cool_even_when_empty():
    r = rooms("bedroom")
    rule = bedroom_night_rule(target=24)
    night = house(now=NOW.replace(hour=23), grid_power=500)
    res = decide(r, {"climate.bedroom": empty()}, [rule], night, S)
    d = res.decisions["climate.bedroom"]
    assert (d.hvac_mode, d.target, d.level) == (HVAC_COOL, 24.0, LEVEL_RULE)


def test_comfort_rule_without_target_uses_budget():
    r = rooms("bedroom")
    rule = bedroom_night_rule()
    night = house(now=NOW.replace(hour=23), grid_power=500)
    res = decide(r, {"climate.bedroom": empty()}, [rule], night, S)
    assert res.decisions["climate.bedroom"].level == LEVEL_ECO


def test_off_rule_wins():
    r = rooms("office")
    rule = Rule("r2", "climate.office", "Office off overnight", list(range(7)),
                time(13, 0), time(15, 0), RULE_OFF)
    res = decide(r, {"climate.office": occupied()}, [rule], house(grid_power=-3000), S)
    assert res.decisions["climate.office"].reason == "Rule: Office off overnight"


def test_precool_upcoming_rule_on_spare_solar():
    r = rooms("bedroom")
    rule = bedroom_night_rule()
    at_7pm = house(now=NOW.replace(hour=19, minute=30), grid_power=-2000)
    res = decide(r, {"climate.bedroom": empty()}, [rule], at_7pm, S)
    d = res.decisions["climate.bedroom"]
    assert (d.hvac_mode, d.level, d.target) == (HVAC_COOL, LEVEL_PRECOOL, 23.0)


def test_no_precool_without_solar():
    r = rooms("bedroom")
    rule = bedroom_night_rule()
    at_7pm = house(now=NOW.replace(hour=19, minute=30), grid_power=300)
    res = decide(r, {"climate.bedroom": empty()}, [rule], at_7pm, S)
    assert res.decisions["climate.bedroom"].hvac_mode == HVAC_OFF


def test_hot_day_extends_precool_horizon():
    r = rooms("bedroom")
    rule = bedroom_night_rule()
    at_6pm = NOW.replace(hour=18)  # 3h before
    res = decide(r, {"climate.bedroom": empty()}, [rule],
                 house(now=at_6pm, grid_power=-2000, forecast_high=30), S)
    assert res.decisions["climate.bedroom"].hvac_mode == HVAC_OFF
    res = decide(r, {"climate.bedroom": empty()}, [rule],
                 house(now=at_6pm, grid_power=-2000, forecast_high=38), S)
    assert res.decisions["climate.bedroom"].level == LEVEL_PRECOOL


# --- overrides, safety, modes ----------------------------------------------

def test_manual_hold_leaves_unit_alone():
    r = rooms("office")
    st = occupied(hold_until=NOW + timedelta(hours=1))
    d = decide(r, {"climate.office": st}, [], house(), S).decisions["climate.office"]
    assert d.hvac_mode is None and d.level == LEVEL_HOLD


def test_expired_hold_is_ignored():
    r = rooms("office")
    st = occupied(hold_until=NOW - timedelta(minutes=1))
    d = decide(r, {"climate.office": st}, [], house(), S).decisions["climate.office"]
    assert d.hvac_mode == HVAC_COOL


def test_safety_cooling_when_away():
    r = rooms("office")
    st = empty(temp=33)
    d = decide(r, {"climate.office": st}, [], house(anyone_home=False), S).decisions["climate.office"]
    assert (d.hvac_mode, d.level, d.target) == (HVAC_COOL, LEVEL_SAFETY, 26.0)


def test_window_and_fresh_air_turn_off():
    r = rooms("office")
    d = decide(r, {"climate.office": occupied(window_open=True)}, [], house(), S).decisions["climate.office"]
    assert d.reason == "Window open"
    d = decide(r, {"climate.office": occupied()}, [], house(fresh_air=True), S).decisions["climate.office"]
    assert d.reason == "Fresh air mode"


def test_room_and_house_modes():
    r = rooms("office")
    d = decide(r, {"climate.office": occupied(room_mode=ROOM_OFF)}, [], house(), S).decisions["climate.office"]
    assert d.hvac_mode == HVAC_OFF
    d = decide(r, {"climate.office": occupied(room_mode=ROOM_COMFORT)}, [], house(grid_power=900), S).decisions["climate.office"]
    assert d.level == LEVEL_COMFORT
    d = decide(r, {"climate.office": occupied()}, [], house(house_mode=HOUSE_BOOST), S).decisions["climate.office"]
    assert d.target == 22.0
    d = decide(r, {"climate.office": occupied()}, [], house(house_mode=HOUSE_PAUSED), S).decisions["climate.office"]
    assert d.hvac_mode is None


def test_heating_season():
    r = rooms("office")
    h = house(outside_temp=10, forecast_high=14, grid_power=-2000)
    d = decide(r, {"climate.office": occupied(temp=17)}, [], h, S).decisions["climate.office"]
    assert (d.hvac_mode, d.target) == (HVAC_HEAT, 21.0)
    h = house(outside_temp=10, forecast_high=14, grid_power=500)
    d = decide(r, {"climate.office": occupied(temp=17)}, [], h, S).decisions["climate.office"]
    assert d.target == 18.0


def test_neutral_season_only_acts_outside_band():
    r = rooms("office")
    h = house(outside_temp=20, forecast_high=22)
    d = decide(r, {"climate.office": occupied(temp=24)}, [], h, S).decisions["climate.office"]
    assert d.hvac_mode == HVAC_OFF
    d = decide(r, {"climate.office": occupied(temp=27.5)}, [], h, S).decisions["climate.office"]
    assert d.hvac_mode == HVAC_COOL


def test_setpoint_respects_unit_limits_and_step():
    r = rooms("office", cool_min=15.2)
    st = occupied(min_temp=16, step=1)
    d = decide(r, {"climate.office": st}, [], house(grid_power=-3000), S).decisions["climate.office"]
    assert d.target == 16


def test_unavailable_unit_left_alone():
    r = rooms("office")
    d = decide(r, {"climate.office": RoomState(available=False)}, [], house(), S).decisions["climate.office"]
    assert d.hvac_mode is None


# --- fresh air --------------------------------------------------------------

def test_fresh_air_open_and_close():
    states = {"climate.a": occupied(temp=26), "climate.b": occupied(temp=27)}
    advice, inside = fresh_air_advice(states, house(outside_temp=21), S)
    assert advice == ADVICE_OPEN and inside == 26.5
    advice, _ = fresh_air_advice(states, house(outside_temp=25.5), S)
    assert advice is None
    cool_states = {"climate.a": occupied(temp=21)}
    advice, _ = fresh_air_advice(cool_states, house(outside_temp=22, fresh_air=True), S)
    assert advice == ADVICE_CLOSE
    advice, _ = fresh_air_advice(cool_states, house(outside_temp=19, fresh_air=True), S)
    assert advice is None


def test_fresh_air_not_suggested_when_cool_inside_or_away():
    states = {"climate.a": occupied(temp=22)}
    assert fresh_air_advice(states, house(outside_temp=15), S)[0] is None
    states = {"climate.a": occupied(temp=27)}
    assert fresh_air_advice(states, house(outside_temp=15, anyone_home=False), S)[0] is None


def test_rule_from_dict_and_describe():
    rule = Rule.from_dict({"id": "x", "room": "climate.bedroom", "name": "Bedroom",
                           "days": [0, 1, 2, 3, 4], "start": "21:00:00", "end": "06:30",
                           "action": "comfort", "target": 24})
    assert rule.describe() == "Bedroom: keep comfortable at 24°, weekdays 21:00–06:30"


def test_settings_from_dict_ignores_unknown():
    s = Settings.from_dict({"safety_max": "30", "bogus": 1})
    assert s.safety_max == 30.0
