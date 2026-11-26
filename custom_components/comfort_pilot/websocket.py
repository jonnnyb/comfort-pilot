"""WebSocket API used by the Comfort Pilot sidebar panel."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .config_flow import _validate_sources, detect_room
from .const import (
    CONF_CLIMATES,
    CONF_GRID_SENSOR,
    CONF_NOTIFY,
    CONF_OUTDOOR_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_WEATHER,
    DOMAIN,
    NAME,
    OPT_ROOMS,
    OPT_RULES,
    OPT_SETTINGS,
    ROOM_COOL_MAX,
    ROOM_COOL_MIN,
    ROOM_HEAT_MAX,
    ROOM_HEAT_MIN,
    ROOM_NAME,
    ROOM_OCC_TIMEOUT,
    ROOM_OCCUPANCY,
    ROOM_POWER,
    ROOM_PRIORITY,
    ROOM_WINDOWS,
    SET_HOLD_MINUTES,
    SET_QUIET_END,
    SET_QUIET_START,
)
from .coordinator import ComfortPilotCoordinator
from .engine import (
    DAY_NAMES,
    HOUSE_MODES,
    ROOM_AUTO,
    ROOM_MODES,
    RULE_ACTIONS,
    RULE_COMFORT,
    Rule,
    Settings,
    parse_time,
)

ROOM_DEFAULTS = {
    ROOM_OCCUPANCY: [],
    ROOM_WINDOWS: [],
    ROOM_COOL_MIN: 23.0,
    ROOM_COOL_MAX: 26.0,
    ROOM_HEAT_MIN: 18.0,
    ROOM_HEAT_MAX: 21.0,
    ROOM_PRIORITY: "normal",
    ROOM_POWER: 900,
    ROOM_OCC_TIMEOUT: 20,
}


def _temp(lo: float = 10, hi: float = 35):
    return vol.All(vol.Coerce(float), vol.Range(min=lo, max=hi))


def _time(value: Any) -> str:
    try:
        return parse_time(str(value)).strftime("%H:%M:%S")
    except (ValueError, IndexError) as err:
        raise vol.Invalid(f"Invalid time: {value}") from err


ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(ROOM_NAME): vol.All(str, vol.Strip, vol.Length(min=1, max=50)),
        vol.Optional(ROOM_OCCUPANCY, default=[]): [str],
        vol.Optional(ROOM_WINDOWS, default=[]): [str],
        vol.Required(ROOM_COOL_MIN): _temp(),
        vol.Required(ROOM_COOL_MAX): _temp(),
        vol.Required(ROOM_HEAT_MIN): _temp(),
        vol.Required(ROOM_HEAT_MAX): _temp(),
        vol.Required(ROOM_PRIORITY): vol.In(["high", "normal", "low"]),
        vol.Required(ROOM_POWER): vol.All(vol.Coerce(int), vol.Range(min=100, max=5000)),
        vol.Required(ROOM_OCC_TIMEOUT): vol.All(vol.Coerce(int), vol.Range(min=1, max=240)),
    }
)

RULE_SCHEMA = vol.Schema(
    {
        vol.Optional("id"): vol.Any(None, str),
        vol.Required("room"): str,
        vol.Required("name"): vol.All(str, vol.Strip, vol.Length(min=1, max=60)),
        vol.Required("days"): vol.All([vol.All(vol.Coerce(int), vol.Range(min=0, max=6))], vol.Length(min=1)),
        vol.Required("start"): _time,
        vol.Required("end"): _time,
        vol.Required("action"): vol.In(RULE_ACTIONS),
        vol.Optional("target"): vol.Any(None, _temp(16, 30)),
        vol.Optional("enabled", default=True): bool,
        vol.Optional("source"): str,
    },
    extra=vol.REMOVE_EXTRA,
)

_SETTINGS_RANGES = {
    "cooling_season_above": (15, 40),
    "heating_season_below": (5, 25),
    "season_hysteresis": (0, 10),
    "safety_max": (25, 40),
    "safety_min": (5, 18),
    "solar_margin": (0, 3000),
    "precondition_hours": (0, 8),
    "hot_day": (25, 48),
    "hot_day_extra_hours": (0, 6),
    "fresh_air_delta": (0.5, 10),
    "fresh_air_min_indoor": (15, 32),
    "fresh_air_close_margin": (0, 5),
    "neutral_trigger": (0, 5),
}
SETTINGS_SCHEMA = vol.Schema(
    {
        **{
            vol.Optional(k): vol.All(vol.Coerce(float), vol.Range(min=lo, max=hi))
            for k, (lo, hi) in _SETTINGS_RANGES.items()
        },
        vol.Optional(SET_HOLD_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=10, max=720)),
        vol.Optional(SET_QUIET_START): _time,
        vol.Optional(SET_QUIET_END): _time,
    }
)

SOURCES_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIMATES): [str],
        vol.Required(CONF_GRID_SENSOR): str,
        vol.Optional(CONF_SOLAR_SENSOR): vol.Any(None, str),
        vol.Optional(CONF_WEATHER): vol.Any(None, str),
        vol.Optional(CONF_OUTDOOR_SENSOR): vol.Any(None, str),
        vol.Optional(CONF_NOTIFY): vol.Any(None, str),
    }
)


# --------------------------------------------------------------------- helpers

def _entry(hass: HomeAssistant) -> ConfigEntry | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def _coordinator(hass: HomeAssistant, connection, msg) -> ComfortPilotCoordinator | None:
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Comfort Pilot isn't running")
        return None
    return entry.runtime_data


async def _save_options(hass: HomeAssistant, entry: ConfigEntry, **changes: Any) -> None:
    options = {
        OPT_ROOMS: dict(entry.options.get(OPT_ROOMS, {})),
        OPT_RULES: list(entry.options.get(OPT_RULES, [])),
        OPT_SETTINGS: dict(entry.options.get(OPT_SETTINGS, {})),
    }
    options.update(changes)
    # The update listener applies this live; nothing reloads.
    hass.config_entries.async_update_entry(entry, options=options)
    await entry.runtime_data.async_apply_options(options)


def build_payload(c: ComfortPilotCoordinator) -> dict[str, Any]:
    data = c.data
    entry = c.config_entry
    rooms = []
    for room_id, cfg in c.rooms.items():
        d = data.result.decisions.get(room_id) if data else None
        st = data.room_states.get(room_id) if data else None
        raw = {**ROOM_DEFAULTS, **c.room_options.get(room_id, {}), ROOM_NAME: cfg.name}
        hold = c.holds.get(room_id)
        rooms.append(
            {
                "id": room_id,
                "name": cfg.name,
                "config": raw,
                "mode": c.room_modes.get(room_id, ROOM_AUTO),
                "hold_until": hold.isoformat() if hold else None,
                "decision": (
                    {"hvac_mode": d.hvac_mode, "target": d.target, "level": d.level, "reason": d.reason}
                    if d
                    else None
                ),
                "state": (
                    {
                        "available": st.available,
                        "current_temp": st.current_temp,
                        "hvac_mode": st.hvac_mode,
                        "target": st.target,
                        "occupied": st.occupied_now,
                        "has_occupancy_sensor": st.has_occupancy_sensor,
                    }
                    if st
                    else None
                ),
            }
        )
    names = {r: cfg.name for r, cfg in c.rooms.items()}
    rules = []
    for raw in entry.options.get(OPT_RULES, []):
        try:
            rule = Rule.from_dict(raw)
        except (KeyError, ValueError):
            continue
        rules.append(
            {
                **raw,
                "enabled": rule.enabled,
                "start": rule.start.strftime("%H:%M"),
                "end": rule.end.strftime("%H:%M"),
                "description": rule.describe(),
                "days_text": rule.describe_days(),
                "room_name": names.get(rule.room, rule.room),
                "active": rule.active_at(_now()),
            }
        )
    settings = asdict(c.settings)
    settings[SET_HOLD_MINUTES] = c.hold_minutes
    settings[SET_QUIET_START] = c.quiet_start.strftime("%H:%M")
    settings[SET_QUIET_END] = c.quiet_end.strftime("%H:%M")
    overrides = []
    for o in reversed(c.overrides[-50:]):
        overrides.append({**o, "room_name": names.get(o.get("room"), o.get("room"))})
    return {
        "house": {
            "mode": c.house_mode,
            "fresh_air": c.fresh_air,
            "season": c.season,
            "solar_budget": data.result.solar_budget if data else None,
            "grid_power": data.grid_power if data else None,
            "inside": data.inside if data else None,
            "outside": data.outside if data else None,
            "forecast_high": data.forecast_high if data else None,
            "forecast_low": data.forecast_low if data else None,
            "anyone_home": data.anyone_home if data else None,
            "advice": data.advice if data else None,
            "last_update": c.last_update_success,
        },
        "rooms": rooms,
        "rules": rules,
        "settings": settings,
        "sources": dict(entry.data),
        "overrides": overrides,
        "override_count": len(c.overrides),
        "choices": {
            "house_modes": HOUSE_MODES,
            "room_modes": ROOM_MODES,
            "rule_actions": RULE_ACTIONS,
            "days": DAY_NAMES,
        },
    }


def _now():
    return dt_util.now()


# -------------------------------------------------------------------- commands

@websocket_api.websocket_command({vol.Required("type"): "comfort_pilot/get"})
@callback
def ws_get(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/set_house_mode", vol.Required("mode"): vol.In(HOUSE_MODES)}
)
@websocket_api.async_response
async def ws_set_house_mode(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    await c.async_set_house_mode(msg["mode"])
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "comfort_pilot/set_room_mode",
        vol.Required("room"): str,
        vol.Required("mode"): vol.In(ROOM_MODES),
    }
)
@websocket_api.async_response
async def ws_set_room_mode(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    if msg["room"] not in c.rooms:
        connection.send_error(msg["id"], "not_found", "Unknown room")
        return
    await c.async_set_room_mode(msg["room"], msg["mode"])
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/set_fresh_air", vol.Required("on"): bool}
)
@websocket_api.async_response
async def ws_set_fresh_air(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    await c.async_set_fresh_air(msg["on"])
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/resume", vol.Required("room"): str}
)
@websocket_api.async_response
async def ws_resume(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    await c.async_resume(msg["room"])
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/save_room", vol.Required("room"): str, vol.Required("config"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_room(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    room = msg["room"]
    if room not in c.rooms:
        connection.send_error(msg["id"], "not_found", "Unknown room")
        return
    try:
        cfg = ROOM_SCHEMA(msg["config"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    if cfg[ROOM_COOL_MIN] > cfg[ROOM_COOL_MAX] or cfg[ROOM_HEAT_MIN] > cfg[ROOM_HEAT_MAX]:
        connection.send_error(msg["id"], "invalid", "The comfortable and economy ends are the wrong way round")
        return
    rooms = dict(c.config_entry.options.get(OPT_ROOMS, {}))
    rooms[room] = {**rooms.get(room, {}), **cfg}
    entry = c.config_entry
    await _save_options(hass, entry, **{OPT_ROOMS: rooms})
    if room in c.renamed_rooms:
        devices = dr.async_get(hass)
        device = devices.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_{room}")})
        if device:
            devices.async_update_device(device.id, name=f"{NAME} {cfg[ROOM_NAME]}")
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/save_rule", vol.Required("rule"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_rule(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    try:
        rule = RULE_SCHEMA(msg["rule"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    if rule["room"] not in c.rooms:
        connection.send_error(msg["id"], "invalid", "Unknown room")
        return
    if rule["start"] == rule["end"]:
        connection.send_error(msg["id"], "invalid", "Start and end can't be the same")
        return
    if rule["action"] != RULE_COMFORT:
        rule["target"] = None
    rule["days"] = sorted(set(rule["days"]))
    rules = list(c.config_entry.options.get(OPT_RULES, []))
    existing = next((i for i, r in enumerate(rules) if rule.get("id") and r.get("id") == rule["id"]), None)
    if existing is None:
        rule["id"] = uuid.uuid4().hex[:8]
        rule.setdefault("source", "user")
        rules.append(rule)
    else:
        rule.setdefault("source", rules[existing].get("source", "user"))
        rules[existing] = rule
    await _save_options(hass, c.config_entry, **{OPT_RULES: rules})
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/delete_rule", vol.Required("rule_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_rule(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    rules = [r for r in c.config_entry.options.get(OPT_RULES, []) if r.get("id") != msg["rule_id"]]
    await _save_options(hass, c.config_entry, **{OPT_RULES: rules})
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/save_settings", vol.Required("settings"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_settings(hass, connection, msg):
    if (c := _coordinator(hass, connection, msg)) is None:
        return
    try:
        settings = SETTINGS_SCHEMA({k: v for k, v in msg["settings"].items() if v not in (None, "")})
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    merged = {**c.config_entry.options.get(OPT_SETTINGS, {}), **settings}
    await _save_options(hass, c.config_entry, **{OPT_SETTINGS: merged})
    connection.send_result(msg["id"], build_payload(c))


@websocket_api.websocket_command(
    {vol.Required("type"): "comfort_pilot/save_sources", vol.Required("sources"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_sources(hass, connection, msg):
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Comfort Pilot isn't running")
        return
    try:
        sources = SOURCES_SCHEMA(msg["sources"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    sources = {k: v for k, v in sources.items() if v not in (None, "")}
    errors = _validate_sources(sources)
    if errors:
        messages = {
            "no_climates": "Pick at least one split system.",
            "no_outside": "Pick a weather entity or an outdoor temperature sensor.",
            "bad_notify": "Notifications should look like notify.mobile_app_yourphone.",
        }
        connection.send_error(msg["id"], "invalid", " ".join(messages.get(e, e) for e in errors.values()))
        return
    rooms = dict(entry.options.get(OPT_ROOMS, {}))
    for climate in sources[CONF_CLIMATES]:
        rooms.setdefault(climate, detect_room(hass, climate))
    rooms = {k: v for k, v in rooms.items() if k in sources[CONF_CLIMATES]}
    rules = [r for r in entry.options.get(OPT_RULES, []) if r.get("room") in rooms]
    options = {**entry.options, OPT_ROOMS: rooms, OPT_RULES: rules}
    # Sources changed: the update listener reloads the integration.
    hass.config_entries.async_update_entry(entry, data=sources, options=options)
    connection.send_result(msg["id"], {"reloading": True})


COMMANDS = (
    ws_get,
    ws_set_house_mode,
    ws_set_room_mode,
    ws_set_fresh_air,
    ws_resume,
    ws_save_room,
    ws_save_rule,
    ws_delete_rule,
    ws_save_settings,
    ws_save_sources,
)


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    for command in COMMANDS:
        websocket_api.async_register_command(hass, command)
