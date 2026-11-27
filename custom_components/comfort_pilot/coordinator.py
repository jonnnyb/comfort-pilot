"""Coordinator: gathers Home Assistant state, runs the engine, applies decisions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_FRESH_AIR_OFF,
    ACTION_FRESH_AIR_ON,
    CONF_CLIMATES,
    CONF_GRID_SENSOR,
    CONF_NOTIFY,
    CONF_OUTDOOR_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_WEATHER,
    DEFAULT_HOLD_MINUTES,
    DEFAULT_MIN_OFF,
    DEFAULT_MIN_RUN,
    DEFAULT_OCC_DELAY,
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DOMAIN,
    FORECAST_REFRESH_MINUTES,
    GRID_SAMPLE_SECONDS,
    GRID_WINDOW_SECONDS,
    MAX_EVENT_LOG,
    MAX_OVERRIDE_LOG,
    MIN_SECONDS_BETWEEN_SETPOINT_CHANGES,
    MIN_SETPOINT_CHANGE,
    NAME,
    NOTIFY_REPEAT_MINUTES,
    NOTIFY_TAG,
    OPT_ROOMS,
    OPT_RULES,
    OPT_SETTINGS,
    OWN_CHANGE_GRACE_SECONDS,
    SAFETY_CONFIRM_SECONDS,
    SAFETY_RELEASE_MARGIN,
    TEMP_CONFIRM_SECONDS,
    TEMP_JUMP_LIMIT,
    TEMP_STALE_SECONDS,
    ROOM_COOL_MAX,
    ROOM_COOL_MIN,
    ROOM_HEAT_MAX,
    ROOM_HEAT_MIN,
    ROOM_NAME,
    ROOM_OCC_DELAY,
    ROOM_OCC_TIMEOUT,
    ROOM_OCCUPANCY,
    ROOM_POWER,
    ROOM_PRIORITY,
    ROOM_WINDOWS,
    SET_HOLD_MINUTES,
    SET_MIN_OFF,
    SET_MIN_RUN,
    SET_QUIET_END,
    SET_QUIET_START,
    STORAGE_VERSION,
    UPDATE_INTERVAL_SECONDS,
    WINDOW_OPEN_DELAY_SECONDS,
)
from .engine import (
    ADVICE_CLOSE,
    ADVICE_OPEN,
    HOUSE_AUTO,
    HOUSE_MODES,
    HOUSE_PAUSED,
    HVAC_OFF,
    LEVEL_HOLD,
    LEVEL_OFF,
    LEVEL_PAUSED,
    LEVEL_SAFETY,
    ROOM_AUTO,
    ROOM_MODES,
    SEASON_NEUTRAL,
    Decision,
    HouseState,
    Result,
    RoomConfig,
    RoomState,
    Rule,
    Settings,
    decide,
    fresh_air_advice,
    parse_time,
)

_LOGGER = logging.getLogger(__name__)

BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN, None)
PRESENCE_CLASSES = ("occupancy", "presence")
PRESENCE_TIMEOUT_MINUTES = 5


def _unit_text(mode: str | None, target: float | None) -> str:
    if mode in (None, HVAC_OFF):
        return "off"
    return f"{str(mode).replace('_', ' ')} {target:g}°" if target is not None else str(mode)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Expected:
    mode: str
    target: float | None
    at: datetime


@dataclass
class PilotData:
    result: Result
    inside: float | None = None
    outside: float | None = None
    forecast_high: float | None = None
    forecast_low: float | None = None
    grid_power: float | None = None
    anyone_home: bool | None = None
    advice: str | None = None
    room_states: dict[str, RoomState] = field(default_factory=dict)


class ComfortPilotCoordinator(DataUpdateCoordinator[PilotData]):
    """Runs the Comfort Pilot loop."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=NAME,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        data, options = entry.data, entry.options
        self.climates: list[str] = list(data.get(CONF_CLIMATES, []))
        self.grid_sensor: str | None = data.get(CONF_GRID_SENSOR)
        self.solar_sensor: str | None = data.get(CONF_SOLAR_SENSOR)
        self.weather: str | None = data.get(CONF_WEATHER)
        self.outdoor_sensor: str | None = data.get(CONF_OUTDOOR_SENSOR)
        self.notify_target: str | None = data.get(CONF_NOTIFY) or None

        self.data_snapshot: dict = dict(data)
        self._load_options(options)

        # Runtime state (persisted where it matters)
        self.store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.house_mode: str = HOUSE_AUTO
        self.room_modes: dict[str, str] = {}
        self.fresh_air: bool = False
        self.holds: dict[str, datetime] = {}
        self.overrides: list[dict] = []
        self.season: str = SEASON_NEUTRAL

        self._last_motion: dict[str, datetime] = {}
        self._visit_start: dict[str, datetime] = {}
        self._confirmed: dict[str, bool] = {}
        self._unit_on_at: dict[str, datetime] = {}
        self._unit_off_at: dict[str, datetime] = {}
        self._logged: dict[str, tuple] = {}
        self._accepted_temp: dict[str, tuple[float, datetime]] = {}
        self._suspect_temp: dict[str, tuple[float, datetime, float]] = {}
        self._safety_since: dict[str, tuple[str, datetime]] = {}
        self._safety_active: dict[str, str] = {}
        self.log: list[dict] = []
        self._last_level: dict[str, str] = {}
        self._expected: dict[str, Expected] = {}
        self._own_contexts: deque[str] = deque(maxlen=100)
        self._last_setpoint_change: dict[str, datetime] = {}
        self._grid_samples: deque[tuple[datetime, float]] = deque(maxlen=200)
        self._forecast: tuple[float | None, float | None] = (None, None)
        self._forecast_at: datetime | None = None
        self._advice_sent: str | None = None
        self._last_notified: dict[str, datetime] = {}
        self._unsubs: list = []
        self._sensor_unsubs: list = []

    # ------------------------------------------------------------------ setup

    def _load_options(self, options: dict) -> None:
        """Parse rooms, rules and settings from the entry options."""
        room_opts: dict[str, dict] = options.get(OPT_ROOMS, {})
        self.room_options: dict[str, dict] = room_opts
        old_rooms = getattr(self, "rooms", {})
        self.rooms: dict[str, RoomConfig] = {}
        self.occupancy_sensors: dict[str, list[str]] = {}
        self.window_sensors: dict[str, list[str]] = {}
        for climate_id in self.climates:
            ro = room_opts.get(climate_id, {})
            self.rooms[climate_id] = RoomConfig(
                climate_id=climate_id,
                name=ro.get(ROOM_NAME) or self._friendly(climate_id),
                cool_min=float(ro.get(ROOM_COOL_MIN, 23.0)),
                cool_max=float(ro.get(ROOM_COOL_MAX, 26.0)),
                heat_min=float(ro.get(ROOM_HEAT_MIN, 18.0)),
                heat_max=float(ro.get(ROOM_HEAT_MAX, 21.0)),
                priority=ro.get(ROOM_PRIORITY, "normal"),
                est_power=float(ro.get(ROOM_POWER, 900)),
                occupancy_timeout=int(ro.get(ROOM_OCC_TIMEOUT, 20)),
                occupancy_delay=int(ro.get(ROOM_OCC_DELAY, DEFAULT_OCC_DELAY)),
            )
            self.occupancy_sensors[climate_id] = list(ro.get(ROOM_OCCUPANCY, []))
            self.window_sensors[climate_id] = list(ro.get(ROOM_WINDOWS, []))
        self.renamed_rooms = [
            r for r, cfg in self.rooms.items() if r in old_rooms and old_rooms[r].name != cfg.name
        ]

        self.rules: list[Rule] = []
        for raw in options.get(OPT_RULES, []):
            try:
                self.rules.append(Rule.from_dict(raw))
            except (KeyError, ValueError) as err:
                _LOGGER.warning("Skipping invalid rule %s: %s", raw, err)

        raw_settings = dict(options.get(OPT_SETTINGS, {}))
        self.hold_minutes = int(raw_settings.pop(SET_HOLD_MINUTES, DEFAULT_HOLD_MINUTES))
        self.quiet_start = parse_time(raw_settings.pop(SET_QUIET_START, DEFAULT_QUIET_START))
        self.quiet_end = parse_time(raw_settings.pop(SET_QUIET_END, DEFAULT_QUIET_END))
        self.min_run = int(raw_settings.pop(SET_MIN_RUN, DEFAULT_MIN_RUN))
        self.min_off = int(raw_settings.pop(SET_MIN_OFF, DEFAULT_MIN_OFF))
        self.settings = Settings.from_dict(raw_settings)

    async def async_apply_options(self, options: dict) -> None:
        """Apply changed rooms/rules/settings without reloading the integration."""
        self._load_options(options)
        self._subscribe_sensors()
        self._clear_protection()
        self._log("system", "Settings updated")
        await self.async_refresh()

    def _subscribe_sensors(self) -> None:
        for unsub in self._sensor_unsubs:
            unsub()
        self._sensor_unsubs.clear()
        occupancy = sorted({e for ents in self.occupancy_sensors.values() for e in ents})
        if occupancy:
            self._sensor_unsubs.append(
                async_track_state_change_event(self.hass, occupancy, self._on_occupancy_change)
            )
        windows = sorted({e for ents in self.window_sensors.values() for e in ents})
        if windows:
            self._sensor_unsubs.append(
                async_track_state_change_event(self.hass, windows, self._on_window_change)
            )

    def _friendly(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        if state and state.name:
            return state.name
        return entity_id.split(".", 1)[-1].replace("_", " ").title()

    async def async_setup(self) -> None:
        stored = await self.store.async_load() or {}
        if stored.get("house_mode") in HOUSE_MODES:
            self.house_mode = stored["house_mode"]
        self.room_modes = {
            k: v for k, v in stored.get("room_modes", {}).items() if v in ROOM_MODES
        }
        self.fresh_air = bool(stored.get("fresh_air", False))
        for room, iso in stored.get("holds", {}).items():
            when = dt_util.parse_datetime(iso)
            if when:
                self.holds[room] = when
        for room, iso in stored.get("last_motion", {}).items():
            when = dt_util.parse_datetime(iso)
            if when and room in self.rooms:
                self._last_motion[room] = when
        self.overrides = list(stored.get("overrides", []))[-MAX_OVERRIDE_LOG:]
        self.log = list(stored.get("log", []))[-MAX_EVENT_LOG:]
        self._confirmed = {
            k: bool(v) for k, v in stored.get("occupied", {}).items() if k in self.rooms
        }
        self.season = stored.get("season", SEASON_NEUTRAL)

        hass = self.hass
        self._unsubs.append(
            async_track_state_change_event(hass, self.climates, self._on_climate_change)
        )
        self._subscribe_sensors()
        persons = hass.states.async_entity_ids("person")
        if persons:
            self._unsubs.append(
                async_track_state_change_event(hass, persons, self._on_refresh_trigger)
            )
        if self.grid_sensor:
            self._sample_grid()
            self._unsubs.append(
                async_track_time_interval(
                    hass, self._sample_grid_cb, timedelta(seconds=GRID_SAMPLE_SECONDS)
                )
            )
        self._unsubs.append(
            hass.bus.async_listen("mobile_app_notification_action", self._on_notification_action)
        )

    async def async_unload(self) -> None:
        for unsub in self._unsubs + self._sensor_unsubs:
            unsub()
        self._unsubs.clear()
        self._sensor_unsubs.clear()
        await self.store.async_save(self._to_store())

    # -------------------------------------------------------------- persistence

    def _to_store(self) -> dict:
        return {
            "house_mode": self.house_mode,
            "room_modes": self.room_modes,
            "fresh_air": self.fresh_air,
            "holds": {k: v.isoformat() for k, v in self.holds.items()},
            "last_motion": {k: v.isoformat() for k, v in self._last_motion.items()},
            "overrides": self.overrides[-MAX_OVERRIDE_LOG:],
            "log": self.log[-MAX_EVENT_LOG:],
            "occupied": {k: v for k, v in self._confirmed.items() if v},
            "season": self.season,
        }

    def _save(self) -> None:
        self.store.async_delay_save(self._to_store, 5)

    # ----------------------------------------------------------- user controls

    def _clear_protection(self, room: str | None = None) -> None:
        """The user asked for something: apply it straight away."""
        for d in (self._last_setpoint_change, self._unit_on_at, self._unit_off_at):
            if room is None:
                d.clear()
            else:
                d.pop(room, None)

    async def async_set_house_mode(self, mode: str) -> None:
        self._log("mode", f"House mode set to {mode}")
        self.house_mode = mode
        self._clear_protection()
        self._save()
        await self.async_refresh()

    async def async_set_room_mode(self, room: str, mode: str) -> None:
        self._log("mode", f"Mode set to {mode}", room)
        self.room_modes[room] = mode
        self.holds.pop(room, None)
        self._clear_protection(room)
        self._save()
        await self.async_refresh()

    async def async_set_fresh_air(self, on: bool) -> None:
        self._log("mode", f"Fresh air mode {'on' if on else 'off'}")
        self.fresh_air = on
        self._advice_sent = None
        self._clear_protection()
        self._save()
        await self.async_refresh()

    async def async_resume(self, room: str) -> None:
        if self.holds.pop(room, None):
            self._log("mode", "Resumed automatic control", room)
        self._clear_protection(room)
        self._save()
        await self.async_refresh()

    # --------------------------------------------------------------- listeners

    @callback
    def _on_refresh_trigger(self, event: Event) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_occupancy_change(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        entity_id = event.data.get(ATTR_ENTITY_ID)
        rooms = [r for r, sensors in self.occupancy_sensors.items() if entity_id in sensors]
        now = dt_util.now()
        if old is not None and old.state == STATE_ON and (new is None or new.state != STATE_ON):
            for room in rooms:
                self._last_motion[room] = now
            self._save()
        if new is not None and new.state == STATE_ON:
            for room in rooms:
                self._last_motion[room] = now
                if not self._confirmed.get(room) and room not in self._visit_start:
                    self._start_visit(room, now)
            # React quickly rather than waiting for the next cycle.
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_window_change(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is not None and new.state == STATE_ON:
            async_call_later(
                self.hass, WINDOW_OPEN_DELAY_SECONDS + 5, self._delayed_refresh
            )
        else:
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _delayed_refresh(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_notification_action(self, event: Event) -> None:
        action = event.data.get("action")
        if action == ACTION_FRESH_AIR_ON:
            self.hass.async_create_task(self.async_set_fresh_air(True))
        elif action == ACTION_FRESH_AIR_OFF:
            self.hass.async_create_task(self.async_set_fresh_air(False))

    @callback
    def _on_climate_change(self, event: Event) -> None:
        """Spot manual changes (app, dashboard or the IR remote) and hold them."""
        entity_id = event.data.get(ATTR_ENTITY_ID)
        old: State | None = event.data.get("old_state")
        new: State | None = event.data.get("new_state")
        if entity_id not in self.rooms or old is None or new is None:
            return
        if new.state not in BAD_STATES:
            raw = _float(new.attributes.get("current_temperature"))
            if raw is not None:
                now = dt_util.now()
                self._safety_flags(entity_id, self._filter_temp(entity_id, raw, now), now)
        if old.state in BAD_STATES or new.state in BAD_STATES:
            return
        if old.state == HVAC_OFF and new.state != HVAC_OFF:
            self._unit_on_at[entity_id] = dt_util.now()
            self._unit_off_at.pop(entity_id, None)
        elif old.state != HVAC_OFF and new.state == HVAC_OFF:
            self._unit_off_at[entity_id] = dt_util.now()
            self._unit_on_at.pop(entity_id, None)
        old_t = _float(old.attributes.get(ATTR_TEMPERATURE))
        new_t = _float(new.attributes.get(ATTR_TEMPERATURE))
        if old.state == new.state and old_t == new_t:
            return  # only current temperature / fan etc changed
        if event.context.id in self._own_contexts:
            return
        now = dt_util.now()
        exp = self._expected.get(entity_id)
        if exp is not None:
            if (now - exp.at).total_seconds() < OWN_CHANGE_GRACE_SECONDS:
                return  # device echoing our own command
            if new.state == exp.mode and (
                exp.target is None or new_t is None or abs(new_t - exp.target) < 0.1
            ):
                return

        data = self.data
        room_state = data.room_states.get(entity_id) if data else None
        entry = {
            "at": now.isoformat(),
            "room": entity_id,
            "weekday": now.weekday(),
            "minute_of_day": now.hour * 60 + now.minute,
            "from_mode": old.state,
            "from_target": old_t,
            "to_mode": new.state,
            "to_target": new_t,
            "room_temp": _float(new.attributes.get("current_temperature")),
            "outside": data.outside if data else None,
            "grid_power": data.grid_power if data else None,
            "occupied": room_state.occupied_now if room_state else None,
            "season": self.season,
            "pilot_level": self._last_level.get(entity_id),
        }
        self.overrides.append(entry)
        del self.overrides[:-MAX_OVERRIDE_LOG]
        _LOGGER.debug("Manual change recorded: %s", entry)
        self._log(
            "manual",
            f"Changed by hand: {_unit_text(old.state, old_t)} → {_unit_text(new.state, new_t)}"
            + ("" if self.house_mode == HOUSE_PAUSED else f" – leaving it alone for {self.hold_minutes} min"),
            entity_id,
        )

        if self.house_mode != HOUSE_PAUSED:
            self.holds[entity_id] = now + timedelta(minutes=self.hold_minutes)
        # What the user set is now the baseline.
        self._expected[entity_id] = Expected(new.state, new_t, now - timedelta(hours=1))
        self._save()
        self.hass.async_create_task(self.async_request_refresh())

    # ------------------------------------------------------------ data gather

    @callback
    def _sample_grid_cb(self, _now: datetime) -> None:
        self._sample_grid()

    def _sample_grid(self) -> None:
        state = self.hass.states.get(self.grid_sensor) if self.grid_sensor else None
        if state is None:
            return
        value = _float(state.state)
        if value is None:
            return
        if str(state.attributes.get("unit_of_measurement", "")).lower() == "kw":
            value *= 1000
        self._grid_samples.append((dt_util.utcnow(), value))

    def _grid_average(self) -> float | None:
        cutoff = dt_util.utcnow() - timedelta(seconds=GRID_WINDOW_SECONDS)
        values = [v for t, v in self._grid_samples if t >= cutoff]
        if not values:
            return None
        return round(sum(values) / len(values), 0)

    def _outside_temp(self) -> float | None:
        if self.outdoor_sensor:
            state = self.hass.states.get(self.outdoor_sensor)
            value = _float(state.state) if state else None
            if value is not None:
                return value
        if self.weather:
            state = self.hass.states.get(self.weather)
            if state:
                return _float(state.attributes.get("temperature"))
        return None

    def _anyone_home(self) -> bool | None:
        persons = [self.hass.states.get(e) for e in self.hass.states.async_entity_ids("person")]
        known = [p for p in persons if p is not None and p.state not in BAD_STATES]
        if not known:
            return None
        return any(p.state == "home" for p in known)

    async def _refresh_forecast(self, now: datetime) -> None:
        if not self.weather:
            return
        if self._forecast_at and now - self._forecast_at < timedelta(minutes=FORECAST_REFRESH_MINUTES):
            return
        self._forecast_at = now
        for kind in ("daily", "twice_daily", "hourly"):
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": kind, ATTR_ENTITY_ID: self.weather},
                    blocking=True,
                    return_response=True,
                )
            except Exception:  # noqa: BLE001 - forecast type not supported, try next
                continue
            forecast = ((response or {}).get(self.weather) or {}).get("forecast") or []
            parsed = self._parse_forecast(forecast, now, kind)
            if parsed != (None, None):
                self._forecast = parsed
                return

    @staticmethod
    def _parse_forecast(forecast: list[dict], now: datetime, kind: str) -> tuple:
        today = now.date()
        temps: list[float] = []
        lows: list[float] = []
        for item in forecast:
            when = dt_util.parse_datetime(str(item.get("datetime", "")))
            if when is None:
                continue
            local = dt_util.as_local(when)
            if kind == "hourly":
                if local - now > timedelta(hours=24):
                    break
            elif local.date() != today:
                continue
            t = _float(item.get("temperature"))
            if t is not None:
                temps.append(t)
            low = _float(item.get("templow"))
            if low is not None:
                lows.append(low)
        if not temps:
            return (None, None)
        low = min(lows) if lows else (min(temps) if kind == "hourly" else None)
        return (max(temps), low)

    def _room_state(self, room: str, now: datetime) -> RoomState:
        cfg = self.rooms[room]
        state = self.hass.states.get(room)
        if state is None or state.state in BAD_STATES:
            return RoomState(available=False)
        a = state.attributes
        st = RoomState(
            available=True,
            current_temp=self._filter_temp(room, _float(a.get("current_temperature")), now),
            hvac_mode=state.state,
            target=_float(a.get(ATTR_TEMPERATURE)),
            hvac_modes=list(a.get("hvac_modes", [])),
            min_temp=_float(a.get("min_temp")) or 16.0,
            max_temp=_float(a.get("max_temp")) or 31.0,
            step=_float(a.get("target_temp_step")) or 0.5,
            room_mode=self.room_modes.get(room, ROOM_AUTO),
            hold_until=self.holds.get(room),
            last_level=self._last_level.get(room),
        )
        sensors = [self.hass.states.get(e) for e in self.occupancy_sensors.get(room, [])]
        sensors = [s for s in sensors if s is not None and s.state not in BAD_STATES]
        if sensors:
            st.has_occupancy_sensor = True
            all_presence = all(s.attributes.get("device_class") in PRESENCE_CLASSES for s in sensors)
            if all_presence:
                cfg.occupancy_timeout = min(cfg.occupancy_timeout, PRESENCE_TIMEOUT_MINUTES)
            raw_on = any(s.state == STATE_ON for s in sensors)
            self._track_occupancy(room, raw_on, now)
            confirmed = self._confirmed.get(room, False)
            st.occupied_now = raw_on and confirmed
            # Only motion we've actually seen counts (a sensor's last_changed
            # resets when Home Assistant restarts, so it can't be trusted).
            st.last_occupied = self._last_motion.get(room) if confirmed else None
            st.occupancy_pending = not confirmed and room in self._visit_start
        st.safety_hot, st.safety_cold = self._safety_flags(room, st.current_temp, now)
        windows = [self.hass.states.get(e) for e in self.window_sensors.get(room, [])]
        st.window_open = any(
            w is not None
            and w.state == STATE_ON
            and (now - w.last_changed).total_seconds() >= WINDOW_OPEN_DELAY_SECONDS
            for w in windows
        )
        return st


    # ------------------------------------------------------------- occupancy

    def _start_visit(self, room: str, now: datetime) -> None:
        self._visit_start[room] = now
        delay = self.rooms[room].occupancy_delay
        if delay > 0:
            self._log("occupancy", f"Motion detected – waiting {delay} min to confirm", room)
            async_call_later(self.hass, delay * 60 + 5, self._delayed_refresh)

    def _track_occupancy(self, room: str, raw_on: bool, now: datetime) -> None:
        """Confirm a visit after sustained motion; end it after the timeout."""
        cfg = self.rooms[room]
        confirmed = self._confirmed.get(room, False)
        delay = timedelta(minutes=cfg.occupancy_delay)
        if raw_on:
            self._last_motion[room] = now
            if confirmed:
                return
            if room not in self._visit_start:
                self._start_visit(room, now)
            if now - self._visit_start[room] >= delay:
                self._confirmed[room] = True
                self._visit_start.pop(room, None)
                self._log("occupancy", "Occupied", room)
                self._save()
            return
        last = self._last_motion.get(room)
        if confirmed:
            if last is None or now - last > timedelta(minutes=cfg.occupancy_timeout):
                self._confirmed[room] = False
                self._log("occupancy", f"Unoccupied – no motion for {cfg.occupancy_timeout} min", room)
                self._save()
        elif room in self._visit_start:
            if last is None or now - last > max(delay, timedelta(minutes=1)):
                self._visit_start.pop(room, None)
                self._log("occupancy", "Motion didn't continue – ignored", room)

    # ---------------------------------------------------- temperature checks

    def _filter_temp(self, room: str, raw: float | None, now: datetime) -> float | None:
        """Ignore one-off jumps: a reading far from the last trusted one has to
        hold steady for a few minutes before it's believed."""
        if raw is None:
            accepted = self._accepted_temp.get(room)
            return accepted[0] if accepted else None
        accepted = self._accepted_temp.get(room)
        if (
            accepted is None
            or abs(raw - accepted[0]) <= TEMP_JUMP_LIMIT
            or (now - accepted[1]).total_seconds() > TEMP_STALE_SECONDS
        ):
            self._accepted_temp[room] = (raw, now)
            self._suspect_temp.pop(room, None)
            return raw
        suspect = self._suspect_temp.get(room)
        if suspect is not None and abs(raw - suspect[2]) <= 1.5:
            if (now - suspect[1]).total_seconds() >= TEMP_CONFIRM_SECONDS:
                self._log("system", f"Room temperature confirmed at {raw:g}° (was {accepted[0]:g}°)", room)
                self._accepted_temp[room] = (raw, now)
                self._suspect_temp.pop(room, None)
                return raw
            self._suspect_temp[room] = (suspect[0], suspect[1], raw)
            return accepted[0]
        self._suspect_temp[room] = (raw, now, raw)
        self._log(
            "system",
            f"Ignoring unusual room temperature {raw:g}° (last reading {accepted[0]:g}°) unless it holds for "
            f"{TEMP_CONFIRM_SECONDS // 60} min",
            room,
        )
        return accepted[0]

    def _safety_flags(self, room: str, temp: float | None, now: datetime) -> tuple[bool, bool]:
        """Safety only acts once the room has stayed past the limit, and stays on
        until it's comfortably back inside it."""
        s = self.settings
        active = self._safety_active.get(room)
        if temp is None:
            return active == "hot", active == "cold"
        if active == "hot" and temp < s.safety_max - SAFETY_RELEASE_MARGIN:
            active = None
        elif active == "cold" and temp > s.safety_min + SAFETY_RELEASE_MARGIN:
            active = None
        if active is None:
            side = "hot" if temp >= s.safety_max else "cold" if temp <= s.safety_min else None
            since = self._safety_since.get(room)
            if side is None:
                self._safety_since.pop(room, None)
            elif since is None or since[0] != side:
                self._safety_since[room] = (side, now)
                limit = s.safety_max if side == "hot" else s.safety_min
                self._log(
                    "system",
                    f"Room at {temp:g}° is past the safety limit ({limit:g}°) – acting if it stays there for "
                    f"{SAFETY_CONFIRM_SECONDS // 60} min",
                    room,
                )
            elif (now - since[1]).total_seconds() >= SAFETY_CONFIRM_SECONDS:
                active = side
                self._safety_since.pop(room, None)
        if active:
            self._safety_active[room] = active
        else:
            self._safety_active.pop(room, None)
        return active == "hot", active == "cold"

    # ------------------------------------------------------- cycle protection

    def _protect(self, room: str, d: Decision, st: RoomState, now: datetime) -> Decision:
        """Avoid switching a unit on and off too often."""
        if d.forced or d.hvac_mode is None or not st.available:
            return d
        if d.hvac_mode == HVAC_OFF and st.running and self._last_level.get(room) != LEVEL_SAFETY:
            # (A unit started by a safety limit can stop as soon as that's over.)
            on_at = self._unit_on_at.get(room)
            if on_at and now - on_at < timedelta(minutes=self.min_run):
                until = (on_at + timedelta(minutes=self.min_run)).strftime("%H:%M")
                # Still running as before, so keep its previous level.
                return Decision(room, None, None, self._last_level.get(room, d.level),
                                f"{d.reason} – keeping on until {until} (minimum run time)")
        if d.hvac_mode != HVAC_OFF and not st.running:
            off_at = self._unit_off_at.get(room)
            if off_at and now - off_at < timedelta(minutes=self.min_off):
                until = (off_at + timedelta(minutes=self.min_off)).strftime("%H:%M")
                return Decision(room, None, None, LEVEL_OFF,
                                f"{d.reason} – can restart at {until} (minimum off time)")
        return d

    # ------------------------------------------------------------------- log

    def _log(self, category: str, message: str, room: str | None = None, **details: Any) -> None:
        name = self.rooms[room].name if room and room in self.rooms else None
        entry = {
            "at": dt_util.now().isoformat(),
            "category": category,
            "room": room,
            "room_name": name,
            "message": message,
        }
        if details:
            entry["details"] = details
        self.log.append(entry)
        del self.log[:-MAX_EVENT_LOG]
        _LOGGER.debug("%s%s", f"{name}: " if name else "", message)
        self._save()

    # ------------------------------------------------------------- main loop

    async def _async_update_data(self) -> PilotData:
        now = dt_util.now()
        for room, until in list(self.holds.items()):
            if until <= now:
                self.holds.pop(room)

        await self._refresh_forecast(now)
        if self.grid_sensor and not self._grid_samples:
            self._sample_grid()

        states = {room: self._room_state(room, now) for room in self.rooms}
        house = HouseState(
            now=now,
            house_mode=self.house_mode,
            anyone_home=self._anyone_home(),
            grid_power=self._grid_average(),
            outside_temp=self._outside_temp(),
            forecast_high=self._forecast[0],
            forecast_low=self._forecast[1],
            fresh_air=self.fresh_air,
            previous_season=self.season,
        )
        result = decide(self.rooms, states, self.rules, house, self.settings)
        if result.season != self.season:
            self._log("system", f"Season changed from {self.season} to {result.season}",
                      outside=house.outside_temp, forecast_high=house.forecast_high)
            self.season = result.season
            self._save()

        for room, decision in list(result.decisions.items()):
            decision = self._protect(room, decision, states[room], now)
            result.decisions[room] = decision
            self._log_decision(room, decision, states[room], house, result)
            await self._apply(room, decision, states[room], now)

        advice, inside = fresh_air_advice(states, house, self.settings)
        await self._handle_advice(advice, inside, house.outside_temp, now)

        return PilotData(
            result=result,
            inside=inside,
            outside=house.outside_temp,
            forecast_high=house.forecast_high,
            forecast_low=house.forecast_low,
            grid_power=house.grid_power,
            anyone_home=house.anyone_home,
            advice=advice,
            room_states=states,
        )

    def _log_decision(self, room, d: Decision, st: RoomState, house: HouseState, result: Result) -> None:
        key = (d.level, d.hvac_mode, d.target, "guard" if "(minimum" in d.reason else "")
        if self._logged.get(room) == key:
            return
        first = room not in self._logged
        self._logged[room] = key
        if first and d.level == LEVEL_PAUSED:
            return
        setting = f"{d.hvac_mode.title()} {d.target:g}° – " if d.hvac_mode not in (None, HVAC_OFF) and d.target is not None else ""
        self._log(
            "decision",
            f"{setting}{d.reason}",
            room,
            level=d.level,
            room_temp=st.current_temp,
            occupied=st.occupied_now,
            solar_budget=result.solar_budget,
            grid_power=house.grid_power,
            outside=house.outside_temp,
        )

    async def _apply(self, room: str, d: Decision, st: RoomState, now: datetime) -> None:
        if d.level not in (LEVEL_HOLD, LEVEL_PAUSED):
            self._last_level[room] = d.level
        if d.hvac_mode is None or not st.available:
            return

        if d.hvac_mode == HVAC_OFF:
            if st.hvac_mode != HVAC_OFF:
                await self._call(room, "set_hvac_mode", {"hvac_mode": HVAC_OFF}, HVAC_OFF, st.target, now)
            return

        mode_change = st.hvac_mode != d.hvac_mode
        target_change = d.target is not None and (
            st.target is None or abs(st.target - d.target) >= MIN_SETPOINT_CHANGE
        )
        if mode_change:
            data: dict[str, Any] = {"hvac_mode": d.hvac_mode}
            if d.target is not None:
                data[ATTR_TEMPERATURE] = d.target
                await self._call(room, "set_temperature", data, d.hvac_mode, d.target, now)
            else:
                await self._call(room, "set_hvac_mode", data, d.hvac_mode, st.target, now)
            self._last_setpoint_change[room] = now
            return
        if target_change:
            last = self._last_setpoint_change.get(room)
            if last and (now - last).total_seconds() < MIN_SECONDS_BETWEEN_SETPOINT_CHANGES:
                return  # be gentle: at most one setpoint change every 10 minutes
            await self._call(room, "set_temperature", {ATTR_TEMPERATURE: d.target}, d.hvac_mode, d.target, now)
            self._last_setpoint_change[room] = now

    async def _call(
        self, room: str, service: str, data: dict, mode: str, target: float | None, now: datetime
    ) -> None:
        context = Context()
        self._own_contexts.append(context.id)
        self._expected[room] = Expected(mode, target, now)
        self._log("action", f"Sent: {_unit_text(mode, target)}", room, service=f"climate.{service}", data=data)
        try:
            await self.hass.services.async_call(
                "climate",
                service,
                {ATTR_ENTITY_ID: room, **data},
                blocking=True,
                context=context,
            )
        except Exception as err:  # noqa: BLE001 - one bad unit shouldn't stop the rest
            _LOGGER.warning("Could not control %s: %s", room, err)
            self._log("error", f"Couldn't control the unit: {err}", room)

    # ----------------------------------------------------------- notifications

    def _quiet(self, now: datetime) -> bool:
        t = now.time()
        start, end = self.quiet_start, self.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= t < end
        return t >= start or t < end

    async def _handle_advice(
        self, advice: str | None, inside: float | None, outside: float | None, now: datetime
    ) -> None:
        if advice is None:
            self._advice_sent = None
            return
        if advice == self._advice_sent or self._quiet(now):
            return
        last = self._last_notified.get(advice)
        if last and now - last < timedelta(minutes=NOTIFY_REPEAT_MINUTES):
            return
        if advice == ADVICE_OPEN:
            title = "Fresh air time"
            message = (
                f"Outside {outside:g}°, inside {inside:g}°. Good time to run the evap on fan "
                "or open up. Turn on Fresh air mode and the splits will pause."
            )
            action = {"action": ACTION_FRESH_AIR_ON, "title": "Fresh air mode on"}
        elif advice == ADVICE_CLOSE:
            title = "Time to close up"
            message = (
                f"Outside is now {outside:g}°, warmer than inside ({inside:g}°). "
                "Turn the evap off and close up – the splits will take over."
            )
            action = {"action": ACTION_FRESH_AIR_OFF, "title": "Hand back to splits"}
        else:
            return
        self._advice_sent = advice
        self._last_notified[advice] = now
        self._log("notify", f"Notification: {title} – {message}")
        await self._notify(title, message, action)

    async def _notify(self, title: str, message: str, action: dict) -> None:
        try:
            if self.notify_target and "." in self.notify_target:
                domain, service = self.notify_target.split(".", 1)
                await self.hass.services.async_call(
                    domain,
                    service,
                    {
                        "title": title,
                        "message": message,
                        "data": {"tag": NOTIFY_TAG, "actions": [action]},
                    },
                    blocking=False,
                )
            else:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {"title": title, "message": message, "notification_id": NOTIFY_TAG},
                    blocking=False,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not send notification: %s", err)
            self._log("error", f"Couldn't send notification: {err}")
