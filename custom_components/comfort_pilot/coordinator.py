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
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DOMAIN,
    FORECAST_REFRESH_MINUTES,
    GRID_SAMPLE_SECONDS,
    GRID_WINDOW_SECONDS,
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
    LEVEL_PAUSED,
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

        room_opts: dict[str, dict] = options.get(OPT_ROOMS, {})
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
            )
            self.occupancy_sensors[climate_id] = list(ro.get(ROOM_OCCUPANCY, []))
            self.window_sensors[climate_id] = list(ro.get(ROOM_WINDOWS, []))

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
        self.settings = Settings.from_dict(raw_settings)

        # Runtime state (persisted where it matters)
        self.store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.house_mode: str = HOUSE_AUTO
        self.room_modes: dict[str, str] = {}
        self.fresh_air: bool = False
        self.holds: dict[str, datetime] = {}
        self.overrides: list[dict] = []
        self.season: str = SEASON_NEUTRAL

        self._last_motion: dict[str, datetime] = {}
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

    # ------------------------------------------------------------------ setup

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
        self.season = stored.get("season", SEASON_NEUTRAL)

        hass = self.hass
        self._unsubs.append(
            async_track_state_change_event(hass, self.climates, self._on_climate_change)
        )
        occupancy = [e for ents in self.occupancy_sensors.values() for e in ents]
        if occupancy:
            self._unsubs.append(
                async_track_state_change_event(hass, occupancy, self._on_occupancy_change)
            )
        windows = [e for ents in self.window_sensors.values() for e in ents]
        if windows:
            self._unsubs.append(
                async_track_state_change_event(hass, windows, self._on_window_change)
            )
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
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
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
            "season": self.season,
        }

    def _save(self) -> None:
        self.store.async_delay_save(self._to_store, 5)

    # ----------------------------------------------------------- user controls

    async def async_set_house_mode(self, mode: str) -> None:
        self.house_mode = mode
        self._last_setpoint_change.clear()  # user asked: apply straight away
        self._save()
        await self.async_refresh()

    async def async_set_room_mode(self, room: str, mode: str) -> None:
        self.room_modes[room] = mode
        self.holds.pop(room, None)
        self._last_setpoint_change.pop(room, None)
        self._save()
        await self.async_refresh()

    async def async_set_fresh_air(self, on: bool) -> None:
        self.fresh_air = on
        self._advice_sent = None
        self._last_setpoint_change.clear()
        self._save()
        await self.async_refresh()

    async def async_resume(self, room: str) -> None:
        self.holds.pop(room, None)
        self._last_setpoint_change.pop(room, None)
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
        if old is not None and old.state == STATE_ON and (new is None or new.state != STATE_ON):
            for room, sensors in self.occupancy_sensors.items():
                if entity_id in sensors:
                    self._last_motion[room] = dt_util.now()
            self._save()
        if new is not None and new.state == STATE_ON:
            # Someone walked in: react quickly rather than waiting for the next cycle.
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
        if old.state in BAD_STATES or new.state in BAD_STATES:
            return
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
            current_temp=_float(a.get("current_temperature")),
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
            st.occupied_now = any(s.state == STATE_ON for s in sensors)
            if st.occupied_now:
                self._last_motion[room] = now
            # Only motion we've actually seen counts (a sensor's last_changed
            # resets when Home Assistant restarts, so it can't be trusted).
            st.last_occupied = self._last_motion.get(room)
            all_presence = all(s.attributes.get("device_class") in PRESENCE_CLASSES for s in sensors)
            if all_presence:
                cfg.occupancy_timeout = min(cfg.occupancy_timeout, PRESENCE_TIMEOUT_MINUTES)
        windows = [self.hass.states.get(e) for e in self.window_sensors.get(room, [])]
        st.window_open = any(
            w is not None
            and w.state == STATE_ON
            and (now - w.last_changed).total_seconds() >= WINDOW_OPEN_DELAY_SECONDS
            for w in windows
        )
        return st

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
            self.season = result.season
            self._save()

        for room, decision in result.decisions.items():
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
        _LOGGER.debug("climate.%s %s %s", service, room, data)
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
