"""Config and options flows for Comfort Pilot."""

from __future__ import annotations

from typing import Any
import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)

from .const import (
    CONF_CLIMATES,
    CONF_GRID_SENSOR,
    CONF_NOTIFY,
    CONF_OUTDOOR_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_WEATHER,
    DEFAULT_HOLD_MINUTES,
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
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
from .engine import DAY_NAMES, RULE_ACTIONS, RULE_COMFORT, Rule, Settings

OCCUPANCY_CLASSES = ("motion", "occupancy", "presence")
WINDOW_CLASSES = ("window",)


# --------------------------------------------------------------------- helpers

def _entity_area(entry: er.RegistryEntry, devices: dr.DeviceRegistry) -> str | None:
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        device = devices.async_get(entry.device_id)
        if device:
            return device.area_id
    return None


def detect_room(hass: HomeAssistant, climate_id: str) -> dict[str, Any]:
    """Default settings for a room, with sensors found in the same area."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    state = hass.states.get(climate_id)
    name = state.name if state else climate_id.split(".", 1)[-1].replace("_", " ").title()
    occupancy: list[str] = []
    windows: list[str] = []
    entry = entities.async_get(climate_id)
    area_id = _entity_area(entry, devices) if entry else None
    if area_id:
        area = areas.async_get_area(area_id)
        if area:
            name = area.name
        for ent in entities.entities.values():
            if ent.domain != "binary_sensor" or ent.disabled_by:
                continue
            if _entity_area(ent, devices) != area_id:
                continue
            device_class = ent.device_class or ent.original_device_class
            if device_class in OCCUPANCY_CLASSES:
                occupancy.append(ent.entity_id)
            elif device_class in WINDOW_CLASSES:
                windows.append(ent.entity_id)
    return {
        ROOM_NAME: name,
        ROOM_OCCUPANCY: occupancy,
        ROOM_WINDOWS: windows,
        ROOM_COOL_MIN: 23.0,
        ROOM_COOL_MAX: 26.0,
        ROOM_HEAT_MIN: 18.0,
        ROOM_HEAT_MAX: 21.0,
        ROOM_PRIORITY: "normal",
        ROOM_POWER: 900,
        ROOM_OCC_TIMEOUT: 20,
    }


def _notify_options(hass: HomeAssistant) -> list[str]:
    services = hass.services.async_services().get("notify", {})
    names = sorted(services, key=lambda n: (not n.startswith("mobile_app_"), n))
    return [f"notify.{n}" for n in names if n not in ("send_message", "persistent_notification")]


def _temp(minimum=10, maximum=32) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.BOX
        )
    )


def _number(minimum, maximum, step, unit=None) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
        )
    )


def _sources_schema(hass: HomeAssistant, defaults: dict[str, Any]) -> vol.Schema:
    def opt(key):
        value = defaults.get(key)
        return {"suggested_value": value} if value else {}

    return vol.Schema(
        {
            vol.Required(CONF_CLIMATES, default=defaults.get(CONF_CLIMATES, [])): EntitySelector(
                EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Required(CONF_GRID_SENSOR, **({"default": defaults[CONF_GRID_SENSOR]} if defaults.get(CONF_GRID_SENSOR) else {})): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Optional(CONF_SOLAR_SENSOR, description=opt(CONF_SOLAR_SENSOR)): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Optional(CONF_WEATHER, description=opt(CONF_WEATHER)): EntitySelector(
                EntitySelectorConfig(domain="weather")
            ),
            vol.Optional(CONF_OUTDOOR_SENSOR, description=opt(CONF_OUTDOOR_SENSOR)): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Optional(CONF_NOTIFY, description=opt(CONF_NOTIFY)): SelectSelector(
                SelectSelectorConfig(
                    options=_notify_options(hass),
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _validate_sources(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not user_input.get(CONF_CLIMATES):
        errors[CONF_CLIMATES] = "no_climates"
    if not user_input.get(CONF_WEATHER) and not user_input.get(CONF_OUTDOOR_SENSOR):
        errors["base"] = "no_outside"
    notify = user_input.get(CONF_NOTIFY)
    if notify and not notify.startswith("notify."):
        errors[CONF_NOTIFY] = "bad_notify"
    return errors


# ----------------------------------------------------------------- config flow

class ComfortPilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """First-time setup: pick the splits and the energy/weather sources."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(user_input)
            if not errors:
                rooms = {c: detect_room(self.hass, c) for c in user_input[CONF_CLIMATES]}
                return self.async_create_entry(
                    title=NAME,
                    data=user_input,
                    options={OPT_ROOMS: rooms, OPT_RULES: [], OPT_SETTINGS: {}},
                )
        schema = _sources_schema(self.hass, user_input or {})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ComfortPilotOptionsFlow()


# ---------------------------------------------------------------- options flow

class ComfortPilotOptionsFlow(OptionsFlow):
    """Rooms, rules and settings. Each step saves when submitted."""

    def __init__(self) -> None:
        self._room: str | None = None

    @property
    def _options(self) -> dict[str, Any]:
        opts = dict(self.config_entry.options)
        opts.setdefault(OPT_ROOMS, {})
        opts.setdefault(OPT_RULES, [])
        opts.setdefault(OPT_SETTINGS, {})
        return opts

    def _room_options(self) -> list[SelectOptionDict]:
        rooms = self._options[OPT_ROOMS]
        return [
            SelectOptionDict(value=c, label=rooms.get(c, {}).get(ROOM_NAME, c))
            for c in self.config_entry.data.get(CONF_CLIMATES, [])
        ]

    def _save(self, **changes: Any) -> ConfigFlowResult:
        opts = self._options
        opts.update(changes)
        return self.async_create_entry(title="", data=opts)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["rooms", "add_rule", "remove_rule", "settings", "sources"],
        )

    # --- rooms

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._room = user_input["room"]
            return await self.async_step_room_edit()
        schema = vol.Schema(
            {vol.Required("room"): SelectSelector(SelectSelectorConfig(options=self._room_options()))}
        )
        return self.async_show_form(step_id="rooms", data_schema=schema)

    async def async_step_room_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        room = self._room
        assert room is not None
        rooms = dict(self._options[OPT_ROOMS])
        current = rooms.get(room) or detect_room(self.hass, room)
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[ROOM_COOL_MIN] > user_input[ROOM_COOL_MAX]:
                errors[ROOM_COOL_MIN] = "band_order"
            if user_input[ROOM_HEAT_MIN] > user_input[ROOM_HEAT_MAX]:
                errors[ROOM_HEAT_MIN] = "band_order"
            if not errors:
                updated = {**current, **user_input}
                updated.setdefault(ROOM_OCCUPANCY, [])
                updated.setdefault(ROOM_WINDOWS, [])
                updated[ROOM_POWER] = int(updated[ROOM_POWER])
                updated[ROOM_OCC_TIMEOUT] = int(updated[ROOM_OCC_TIMEOUT])
                rooms[room] = updated
                return self._save(**{OPT_ROOMS: rooms})
            current = {**current, **user_input}

        schema = vol.Schema(
            {
                vol.Required(ROOM_NAME, default=current.get(ROOM_NAME, room)): TextSelector(),
                vol.Optional(ROOM_OCCUPANCY, default=current.get(ROOM_OCCUPANCY, [])): EntitySelector(
                    EntitySelectorConfig(domain="binary_sensor", multiple=True)
                ),
                vol.Optional(ROOM_WINDOWS, default=current.get(ROOM_WINDOWS, [])): EntitySelector(
                    EntitySelectorConfig(domain="binary_sensor", multiple=True)
                ),
                vol.Required(ROOM_COOL_MIN, default=current.get(ROOM_COOL_MIN, 23.0)): _temp(),
                vol.Required(ROOM_COOL_MAX, default=current.get(ROOM_COOL_MAX, 26.0)): _temp(),
                vol.Required(ROOM_HEAT_MIN, default=current.get(ROOM_HEAT_MIN, 18.0)): _temp(),
                vol.Required(ROOM_HEAT_MAX, default=current.get(ROOM_HEAT_MAX, 21.0)): _temp(),
                vol.Required(ROOM_PRIORITY, default=current.get(ROOM_PRIORITY, "normal")): SelectSelector(
                    SelectSelectorConfig(options=["high", "normal", "low"], translation_key="priority")
                ),
                vol.Required(ROOM_POWER, default=current.get(ROOM_POWER, 900)): _number(100, 5000, 50, "W"),
                vol.Required(ROOM_OCC_TIMEOUT, default=current.get(ROOM_OCC_TIMEOUT, 20)): _number(1, 240, 1, "min"),
            }
        )
        return self.async_show_form(
            step_id="room_edit",
            data_schema=schema,
            errors=errors,
            description_placeholders={"room": current.get(ROOM_NAME, room)},
        )

    # --- rules

    async def async_step_add_rule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("days"):
                errors["days"] = "no_days"
            elif user_input["start"] == user_input["end"]:
                errors["end"] = "same_time"
            else:
                rule = {
                    "id": uuid.uuid4().hex[:8],
                    "room": user_input["room"],
                    "name": user_input["name"].strip() or "Rule",
                    "days": sorted(int(d) for d in user_input["days"]),
                    "start": user_input["start"],
                    "end": user_input["end"],
                    "action": user_input["action"],
                    "target": user_input.get("target"),
                    "enabled": True,
                    "source": "user",
                }
                Rule.from_dict(rule)  # validate
                return self._save(**{OPT_RULES: [*self._options[OPT_RULES], rule]})

        day_options = [SelectOptionDict(value=str(i), label=n) for i, n in enumerate(DAY_NAMES)]
        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required("room", default=defaults.get("room", vol.UNDEFINED)): SelectSelector(
                    SelectSelectorConfig(options=self._room_options())
                ),
                vol.Required("name", default=defaults.get("name", "")): TextSelector(),
                vol.Required("action", default=defaults.get("action", RULE_COMFORT)): SelectSelector(
                    SelectSelectorConfig(options=RULE_ACTIONS, translation_key="rule_action")
                ),
                vol.Required("days", default=defaults.get("days", [str(i) for i in range(7)])): SelectSelector(
                    SelectSelectorConfig(options=day_options, multiple=True)
                ),
                vol.Required("start", default=defaults.get("start", "21:00:00")): TimeSelector(),
                vol.Required("end", default=defaults.get("end", "06:30:00")): TimeSelector(),
                vol.Optional("target", description={"suggested_value": defaults.get("target")}): _temp(16, 30),
            }
        )
        return self.async_show_form(step_id="add_rule", data_schema=schema, errors=errors)

    async def async_step_remove_rule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        rules = self._options[OPT_RULES]
        if not rules:
            return self.async_abort(reason="no_rules")
        if user_input is not None:
            remove = set(user_input.get("rules", []))
            return self._save(**{OPT_RULES: [r for r in rules if r["id"] not in remove]})
        rooms = self._options[OPT_ROOMS]
        options = []
        for raw in rules:
            rule = Rule.from_dict(raw)
            room_name = rooms.get(rule.room, {}).get(ROOM_NAME, rule.room)
            options.append(SelectOptionDict(value=rule.id, label=f"{room_name} – {rule.describe()}"))
        schema = vol.Schema(
            {vol.Optional("rules", default=[]): SelectSelector(
                SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
            )}
        )
        return self.async_show_form(step_id="remove_rule", data_schema=schema)

    # --- settings

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = dict(self._options[OPT_SETTINGS])
        if user_input is not None:
            return self._save(**{OPT_SETTINGS: {**current, **user_input}})
        base = Settings.from_dict({k: v for k, v in current.items() if hasattr(Settings, k)})

        def d(key, default):
            return current.get(key, getattr(base, key, default))

        schema = vol.Schema(
            {
                vol.Required("cooling_season_above", default=d("cooling_season_above", 26)): _temp(15, 40),
                vol.Required("heating_season_below", default=d("heating_season_below", 17)): _temp(5, 25),
                vol.Required("safety_max", default=d("safety_max", 32)): _temp(25, 40),
                vol.Required("safety_min", default=d("safety_min", 12)): _temp(5, 18),
                vol.Required("solar_margin", default=d("solar_margin", 250)): _number(0, 3000, 50, "W"),
                vol.Required("precondition_hours", default=d("precondition_hours", 2)): _number(0, 8, 0.5, "h"),
                vol.Required("hot_day", default=d("hot_day", 35)): _temp(25, 48),
                vol.Required("fresh_air_delta", default=d("fresh_air_delta", 2)): _number(0.5, 10, 0.5, "°C"),
                vol.Required("fresh_air_min_indoor", default=d("fresh_air_min_indoor", 24)): _temp(15, 32),
                vol.Required(SET_HOLD_MINUTES, default=current.get(SET_HOLD_MINUTES, DEFAULT_HOLD_MINUTES)): _number(10, 720, 10, "min"),
                vol.Required(SET_QUIET_START, default=current.get(SET_QUIET_START, DEFAULT_QUIET_START)): TimeSelector(),
                vol.Required(SET_QUIET_END, default=current.get(SET_QUIET_END, DEFAULT_QUIET_END)): TimeSelector(),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    # --- sources (splits, energy, weather, notifications)

    async def async_step_sources(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(user_input)
            if not errors:
                self.hass.config_entries.async_update_entry(self.config_entry, data=user_input)
                rooms = dict(self._options[OPT_ROOMS])
                for climate in user_input[CONF_CLIMATES]:
                    rooms.setdefault(climate, detect_room(self.hass, climate))
                rooms = {k: v for k, v in rooms.items() if k in user_input[CONF_CLIMATES]}
                rules = [r for r in self._options[OPT_RULES] if r["room"] in rooms]
                return self._save(**{OPT_ROOMS: rooms, OPT_RULES: rules})
        defaults = user_input or dict(self.config_entry.data)
        return self.async_show_form(
            step_id="sources", data_schema=_sources_schema(self.hass, defaults), errors=errors
        )
