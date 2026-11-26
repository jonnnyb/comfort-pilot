"""Comfort Pilot decision engine.

Pure Python: no Home Assistant imports, so it can be unit tested on its own.
The coordinator gathers state from Home Assistant, calls ``decide`` and then
applies the returned decisions to the real climate entities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from statistics import mean

# Seasons
SEASON_COOLING = "cooling"
SEASON_HEATING = "heating"
SEASON_NEUTRAL = "neutral"

# Per-room modes (select entity)
ROOM_AUTO = "auto"
ROOM_COMFORT = "comfort"
ROOM_ECO = "eco"
ROOM_OFF = "off"
ROOM_MODES = [ROOM_AUTO, ROOM_COMFORT, ROOM_ECO, ROOM_OFF]

# House modes (select entity)
HOUSE_AUTO = "auto"
HOUSE_HOME = "home"
HOUSE_AWAY = "away"
HOUSE_BOOST = "boost"
HOUSE_OFF = "off"
HOUSE_PAUSED = "paused"
HOUSE_MODES = [HOUSE_AUTO, HOUSE_HOME, HOUSE_AWAY, HOUSE_BOOST, HOUSE_OFF, HOUSE_PAUSED]

# Rule actions
RULE_COMFORT = "comfort"  # keep the room comfortable during the window
RULE_ECO = "eco"  # room may run, but never at the comfortable end
RULE_OFF = "off"  # keep the room off during the window
RULE_ACTIONS = [RULE_COMFORT, RULE_ECO, RULE_OFF]

# Levels reported in decisions
LEVEL_COMFORT = "comfort"
LEVEL_ECO = "eco"
LEVEL_BOOST = "boost"
LEVEL_RULE = "rule"
LEVEL_SAFETY = "safety"
LEVEL_OFF = "off"
LEVEL_HOLD = "hold"
LEVEL_PAUSED = "paused"
LEVEL_PRECOOL = "precondition"

HVAC_OFF = "off"
HVAC_COOL = "cool"
HVAC_HEAT = "heat"

# Need buckets (lower = more important when sharing solar)
NEED_NOW = 1
NEED_UPCOMING = 2

PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_time(value: str | time) -> time:
    """Parse 'HH:MM' or 'HH:MM:SS' into a time."""
    if isinstance(value, time):
        return value
    parts = [int(p) for p in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


@dataclass
class Rule:
    """A schedule rule, created by the user or (later) learned."""

    id: str
    rooms: list[str]  # climate entity ids
    name: str
    days: list[int]  # 0 = Monday
    start: time
    end: time  # end <= start means the window runs past midnight
    action: str = RULE_COMFORT
    target: float | None = None
    enabled: bool = True
    source: str = "user"

    @classmethod
    def from_dict(cls, data: dict) -> "Rule":
        rooms = data.get("rooms") or ([data["room"]] if data.get("room") else [])
        if not rooms:
            raise ValueError("Rule has no rooms")
        return cls(
            id=data["id"],
            rooms=list(rooms),
            name=data.get("name") or "Rule",
            days=[int(d) for d in data.get("days", list(range(7)))],
            start=parse_time(data["start"]),
            end=parse_time(data["end"]),
            action=data.get("action", RULE_COMFORT),
            target=data.get("target"),
            enabled=data.get("enabled", True),
            source=data.get("source", "user"),
        )

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    def window_containing(self, now: datetime) -> tuple[datetime, datetime] | None:
        """Return the (start, end) window active at ``now``, if any."""
        if not self.enabled:
            return None
        today = now.date()
        candidates = [today, today - timedelta(days=1)]
        for day in candidates:
            if day.weekday() not in self.days:
                continue
            start = datetime.combine(day, self.start, tzinfo=now.tzinfo)
            end_day = day + timedelta(days=1) if self.overnight else day
            end = datetime.combine(end_day, self.end, tzinfo=now.tzinfo)
            if start <= now < end:
                return start, end
        return None

    def active_at(self, now: datetime) -> bool:
        return self.window_containing(now) is not None

    def next_start(self, now: datetime) -> datetime | None:
        """Next window start strictly after ``now`` (within 8 days)."""
        if not self.enabled or not self.days:
            return None
        for offset in range(0, 8):
            day = now.date() + timedelta(days=offset)
            if day.weekday() not in self.days:
                continue
            start = datetime.combine(day, self.start, tzinfo=now.tzinfo)
            if start > now:
                return start
        return None

    def describe(self) -> str:
        days = self.describe_days()
        span = f"{self.start.strftime('%H:%M')}–{self.end.strftime('%H:%M')}"
        what = {
            RULE_COMFORT: "keep comfortable"
            + (f" at {self.target:g}°" if self.target is not None else ""),
            RULE_ECO: "economy only",
            RULE_OFF: "keep off",
        }.get(self.action, self.action)
        return f"{self.name}: {what}, {days} {span}"

    def describe_days(self) -> str:
        d = sorted(set(self.days))
        if d == list(range(7)):
            return "every day"
        if d == [0, 1, 2, 3, 4]:
            return "weekdays"
        if d == [5, 6]:
            return "weekends"
        return ", ".join(DAY_NAMES[i] for i in d)


def rule_rooms(raw: dict) -> list[str]:
    """Rooms of a stored rule dict (handles the old single-room format)."""
    return list(raw.get("rooms") or ([raw["room"]] if raw.get("room") else []))


def prune_rules(raw_rules: list[dict], rooms) -> list[dict]:
    """Drop removed rooms from rules, and rules left with no rooms."""
    out = []
    for raw in raw_rules:
        kept = [r for r in rule_rooms(raw) if r in rooms]
        if kept:
            new = {k: v for k, v in raw.items() if k != "room"}
            new["rooms"] = kept
            out.append(new)
    return out


@dataclass
class RoomConfig:
    climate_id: str
    name: str
    cool_min: float = 23.0
    cool_max: float = 26.0
    heat_min: float = 18.0
    heat_max: float = 21.0
    priority: str = "normal"
    est_power: float = 900.0  # watts while running, used to share solar
    occupancy_timeout: int = 20  # minutes a room stays "occupied" after motion
    occupancy_delay: int = 3  # minutes motion must continue before a room counts as occupied


@dataclass
class RoomState:
    available: bool = True
    current_temp: float | None = None
    hvac_mode: str = HVAC_OFF
    target: float | None = None
    hvac_modes: list[str] = field(default_factory=lambda: [HVAC_OFF, HVAC_COOL, HVAC_HEAT])
    min_temp: float = 16.0
    max_temp: float = 31.0
    step: float = 0.5
    has_occupancy_sensor: bool = False
    occupied_now: bool = False
    occupancy_pending: bool = False  # motion seen, waiting to confirm
    last_occupied: datetime | None = None
    window_open: bool = False
    room_mode: str = ROOM_AUTO
    hold_until: datetime | None = None
    last_level: str | None = None  # level chosen on the previous run

    @property
    def running(self) -> bool:
        return self.available and self.hvac_mode not in (HVAC_OFF, "", None)


@dataclass
class HouseState:
    now: datetime
    house_mode: str = HOUSE_AUTO
    anyone_home: bool | None = None
    grid_power: float | None = None  # W, smoothed. Positive = importing, negative = exporting
    outside_temp: float | None = None
    forecast_high: float | None = None
    forecast_low: float | None = None
    fresh_air: bool = False
    previous_season: str = SEASON_NEUTRAL


@dataclass
class Settings:
    cooling_season_above: float = 26.0  # forecast high (or outside temp) at/above → cooling season
    heating_season_below: float = 17.0  # forecast high at/below → heating season
    season_hysteresis: float = 2.0
    safety_max: float = 32.0  # cool even when nobody's there, above this
    safety_min: float = 12.0  # heat even when nobody's there, below this
    solar_margin: float = 250.0  # W of grid import tolerated before a room drops out of comfort
    precondition_hours: float = 2.0  # pre-cool/heat on spare solar this long before a rule starts
    hot_day: float = 35.0  # forecast high at/above → precondition earlier
    hot_day_extra_hours: float = 2.0
    fresh_air_delta: float = 2.0  # outside must be this much cooler than inside
    fresh_air_min_indoor: float = 24.0  # only suggest when inside is at least this warm
    fresh_air_close_margin: float = 0.5  # suggest closing up when outside is this much warmer
    neutral_trigger: float = 1.0  # in neutral season, act if a room is this far outside its band
    start_margin: float = 0.5  # start cooling only above setpoint + this (heating: below setpoint - this)
    stop_margin: float = 1.5  # stop cooling once below setpoint - this (heating: above setpoint + this)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Settings":
        s = cls()
        for key, value in (data or {}).items():
            if hasattr(s, key) and value is not None:
                setattr(s, key, type(getattr(s, key))(value))
        return s


@dataclass
class Decision:
    room: str
    hvac_mode: str | None  # None = leave the unit alone
    target: float | None
    level: str
    reason: str
    forced: bool = False  # must happen now, even inside minimum run/off times


@dataclass
class Result:
    season: str
    solar_budget: float | None
    decisions: dict[str, Decision]


def decide_season(house: HouseState, settings: Settings) -> str:
    """Pick the house-wide season from outside conditions, with hysteresis."""
    reference = house.forecast_high if house.forecast_high is not None else house.outside_temp
    if reference is None:
        return house.previous_season
    if reference >= settings.cooling_season_above:
        return SEASON_COOLING
    if reference <= settings.heating_season_below:
        return SEASON_HEATING
    h = settings.season_hysteresis
    if house.previous_season == SEASON_COOLING and reference >= settings.cooling_season_above - h:
        return SEASON_COOLING
    if house.previous_season == SEASON_HEATING and reference <= settings.heating_season_below + h:
        return SEASON_HEATING
    return SEASON_NEUTRAL


def _room_hvac(season: str, cfg: RoomConfig, st: RoomState, settings: Settings) -> str | None:
    """Which way a room should condition, or None if it needn't."""
    if season == SEASON_COOLING:
        return HVAC_COOL
    if season == SEASON_HEATING:
        return HVAC_HEAT
    t = st.current_temp
    if t is None:
        return None
    if t >= cfg.cool_max + settings.neutral_trigger:
        return HVAC_COOL
    if t <= cfg.heat_min - settings.neutral_trigger:
        return HVAC_HEAT
    return None


def _occupied(cfg: RoomConfig, st: RoomState, house: HouseState) -> bool | None:
    """True/False from sensors, None if the room has no sensor."""
    if not st.has_occupancy_sensor:
        return None
    if st.occupied_now:
        return True
    if st.last_occupied is not None:
        return house.now - st.last_occupied <= timedelta(minutes=cfg.occupancy_timeout)
    return False


def _setpoint(hvac: str, level: str, cfg: RoomConfig, st: RoomState, rule_target: float | None) -> float:
    if level == LEVEL_RULE and rule_target is not None:
        value = rule_target
    elif hvac == HVAC_COOL:
        value = {
            LEVEL_COMFORT: cfg.cool_min,
            LEVEL_PRECOOL: cfg.cool_min,
            LEVEL_BOOST: cfg.cool_min - 1,
            LEVEL_SAFETY: cfg.cool_max,
        }.get(level, cfg.cool_max)
    else:
        value = {
            LEVEL_COMFORT: cfg.heat_max,
            LEVEL_PRECOOL: cfg.heat_max,
            LEVEL_BOOST: cfg.heat_max + 1,
            LEVEL_SAFETY: cfg.heat_min,
        }.get(level, cfg.heat_min)
    return round_to_step(value, st)


def needs_running(hvac: str, target: float | None, st: RoomState, settings: Settings) -> bool:
    """Should the unit run to reach ``target``? Adds start/stop margins so it
    doesn't come on for a room that's already there, or flick on and off."""
    t = st.current_temp
    if t is None or target is None:
        return True
    running = st.available and st.hvac_mode == hvac
    if hvac == HVAC_COOL:
        return t > target - settings.stop_margin if running else t >= target + settings.start_margin
    return t < target + settings.stop_margin if running else t <= target - settings.start_margin


def _already_there(hvac: str, target: float, st: RoomState) -> str:
    word = "Cool" if hvac == HVAC_COOL else "Warm"
    return f"{word} enough – {_fmt_t(st.current_temp)} (target {_fmt_t(target)})"


def round_to_step(value: float, st: RoomState) -> float:
    step = st.step or 0.5
    value = round(round(value / step) * step, 2)
    return max(st.min_temp, min(st.max_temp, value))


def _fmt_t(value: float) -> str:
    return f"{value:g}°"


def _fmt_w(watts: float) -> str:
    return f"{watts / 1000:.1f} kW"


def decide(
    rooms: dict[str, RoomConfig],
    states: dict[str, RoomState],
    rules: list[Rule],
    house: HouseState,
    settings: Settings,
) -> Result:
    """Decide mode and setpoint for every room."""
    now = house.now
    season = decide_season(house, settings)
    decisions: dict[str, Decision] = {}

    # Solar budget: what's being exported, plus what the running units are
    # (estimated to be) using, since that power is ours to re-allocate.
    budget: float | None = None
    if house.grid_power is not None:
        reclaim = sum(
            rooms[r].est_power for r, st in states.items() if r in rooms and st.running
        )
        budget = max(0.0, -house.grid_power + reclaim)

    away = house.house_mode == HOUSE_AWAY or (
        house.house_mode == HOUSE_AUTO and house.anyone_home is False
    )
    hot_day = house.forecast_high is not None and house.forecast_high >= settings.hot_day
    horizon = timedelta(
        hours=settings.precondition_hours + (settings.hot_day_extra_hours if hot_day else 0)
    )

    # candidates waiting on the solar budget: (sort key, room, hvac, bucket, why)
    candidates: list[tuple[tuple, str, str, int, str]] = []

    for room_id, cfg in rooms.items():
        st = states.get(room_id)
        if st is None or not st.available:
            decisions[room_id] = Decision(room_id, None, None, LEVEL_PAUSED, "Unit unavailable")
            continue

        def done(mode, level, reason, target=None, forced=False):
            decisions[room_id] = Decision(room_id, mode, target, level, reason, forced)

        def run(hvac, level, reason, target):
            """Run at ``target`` – unless the room is already there."""
            if needs_running(hvac, target, st, settings):
                done(hvac, level, reason, target)
            else:
                done(HVAC_OFF, LEVEL_OFF, f"{why} – {_already_there(hvac, target, st).lower()}")

        if house.house_mode == HOUSE_PAUSED:
            done(None, LEVEL_PAUSED, "Comfort Pilot paused")
            continue
        if st.hold_until is not None and st.hold_until > now:
            done(None, LEVEL_HOLD, f"Manual change – resuming at {st.hold_until.strftime('%H:%M')}")
            continue
        if st.room_mode == ROOM_OFF:
            done(HVAC_OFF, LEVEL_OFF, "Room set to off", forced=True)
            continue
        if house.house_mode == HOUSE_OFF:
            done(HVAC_OFF, LEVEL_OFF, "House set to off", forced=True)
            continue

        t = st.current_temp
        if t is not None and not st.window_open:
            if t >= settings.safety_max and HVAC_COOL in st.hvac_modes:
                done(HVAC_COOL, LEVEL_SAFETY, f"Safety: room at {_fmt_t(t)}",
                     _setpoint(HVAC_COOL, LEVEL_SAFETY, cfg, st, None), forced=True)
                continue
            if t <= settings.safety_min and HVAC_HEAT in st.hvac_modes:
                done(HVAC_HEAT, LEVEL_SAFETY, f"Safety: room at {_fmt_t(t)}",
                     _setpoint(HVAC_HEAT, LEVEL_SAFETY, cfg, st, None), forced=True)
                continue

        if st.window_open:
            done(HVAC_OFF, LEVEL_OFF, "Window open", forced=True)
            continue
        if house.fresh_air:
            done(HVAC_OFF, LEVEL_OFF, "Fresh air mode", forced=True)
            continue
        if away:
            done(HVAC_OFF, LEVEL_OFF, "Nobody home", forced=house.house_mode == HOUSE_AWAY)
            continue

        room_rules = [r for r in rules if room_id in r.rooms and r.enabled]
        active = [r for r in room_rules if r.active_at(now)]
        if any(r.action == RULE_OFF for r in active):
            name = next(r.name for r in active if r.action == RULE_OFF)
            done(HVAC_OFF, LEVEL_OFF, f"Rule: {name}")
            continue

        hvac = _room_hvac(season, cfg, st, settings)
        if hvac is None:
            done(HVAC_OFF, LEVEL_OFF, "Comfortable – no heating or cooling needed")
            continue
        if hvac not in st.hvac_modes:
            done(HVAC_OFF, LEVEL_OFF, f"Unit doesn't support {hvac}")
            continue

        comfort_rule = next((r for r in active if r.action == RULE_COMFORT), None)
        eco_rule = next((r for r in active if r.action == RULE_ECO), None)
        occupied = _occupied(cfg, st, house)

        if comfort_rule is not None:
            need, why = NEED_NOW, f"Rule: {comfort_rule.name}"
        elif occupied:
            need, why = NEED_NOW, "Occupied"
        elif occupied is None and st.room_mode != ROOM_AUTO:
            need, why = NEED_NOW, "No occupancy sensor"
        elif occupied is None and house.anyone_home is not False and not room_rules:
            need, why = NEED_NOW, "Someone home (no occupancy sensor)"
        else:
            upcoming = [
                (r, r.next_start(now))
                for r in room_rules
                if r.action == RULE_COMFORT and r.next_start(now) is not None
            ]
            upcoming = [(r, s) for r, s in upcoming if s - now <= horizon]
            if upcoming:
                r, s = min(upcoming, key=lambda x: x[1])
                need, why = NEED_UPCOMING, f"{r.name} starts {s.strftime('%H:%M')}"
            else:
                reason = "Motion detected – confirming" if st.occupancy_pending else "Unoccupied"
                done(HVAC_OFF, LEVEL_OFF, reason)
                continue

        if comfort_rule is not None and comfort_rule.target is not None:
            target = round_to_step(comfort_rule.target, st)
            run(hvac, LEVEL_RULE, f"{why} ({_fmt_t(target)})", target)
            continue
        if need == NEED_NOW and st.room_mode in (ROOM_COMFORT, ROOM_ECO):
            level = LEVEL_COMFORT if st.room_mode == ROOM_COMFORT else LEVEL_ECO
            run(hvac, level, f"{why} – room set to {st.room_mode}",
                _setpoint(hvac, level, cfg, st, None))
            continue
        if need == NEED_NOW and house.house_mode == HOUSE_BOOST:
            run(hvac, LEVEL_BOOST, f"{why} – boost", _setpoint(hvac, LEVEL_BOOST, cfg, st, None))
            continue
        if need == NEED_NOW and eco_rule is not None:
            run(hvac, LEVEL_ECO, f"{why} – rule: {eco_rule.name}",
                _setpoint(hvac, LEVEL_ECO, cfg, st, None))
            continue

        key = (
            need,
            PRIORITY_RANK.get(cfg.priority, 1),
            0 if st.last_level in (LEVEL_COMFORT, LEVEL_PRECOOL) else 1,
            cfg.name,
        )
        candidates.append((key, room_id, hvac, need, why))

    # Share the solar budget: most important rooms first.
    remaining = budget
    for _key, room_id, hvac, need, why in sorted(candidates):
        cfg, st = rooms[room_id], states[room_id]
        comfort_level = LEVEL_COMFORT if need == NEED_NOW else LEVEL_PRECOOL
        comfort_target = _setpoint(hvac, comfort_level, cfg, st, None)
        eco_target = _setpoint(hvac, LEVEL_ECO, cfg, st, None)
        if not needs_running(hvac, comfort_target, st, settings):
            # Already at the comfortable end: nothing to do, and no solar used.
            decisions[room_id] = Decision(
                room_id, HVAC_OFF, None, LEVEL_OFF, f"{why} – {_already_there(hvac, comfort_target, st).lower()}"
            )
            continue
        cost = cfg.est_power
        was_comfort = st.last_level in (LEVEL_COMFORT, LEVEL_PRECOOL)
        threshold = cost - settings.solar_margin if was_comfort else cost
        if remaining is not None and remaining >= threshold:
            remaining -= cost
            level = LEVEL_COMFORT if need == NEED_NOW else LEVEL_PRECOOL
            verb = "pre-cooling" if hvac == HVAC_COOL else "pre-heating"
            reason = (
                f"{why} – solar available ({_fmt_w(max(budget or 0, 0))})"
                if need == NEED_NOW
                else f"{why} – {verb} on spare solar"
            )
            decisions[room_id] = Decision(
                room_id, hvac, _setpoint(hvac, level, cfg, st, None), level, reason
            )
        elif need == NEED_NOW:
            source = "on grid power" if remaining is not None else "no grid reading"
            if needs_running(hvac, eco_target, st, settings):
                decisions[room_id] = Decision(
                    room_id, hvac, eco_target, LEVEL_ECO, f"{why} – economy, {source}"
                )
            else:
                decisions[room_id] = Decision(
                    room_id, HVAC_OFF, None, LEVEL_OFF,
                    f"{why} – {_already_there(hvac, eco_target, st).lower()} on economy ({source})",
                )
        else:
            decisions[room_id] = Decision(
                room_id, HVAC_OFF, None, LEVEL_OFF, f"{why} – waiting for spare solar"
            )

    return Result(season=season, solar_budget=budget, decisions=decisions)


# --- Fresh air advice -------------------------------------------------------

ADVICE_OPEN = "open"  # suggest switching on fresh air (evap fan / windows)
ADVICE_CLOSE = "close"  # suggest closing up and handing back to the splits


def indoor_average(states: dict[str, RoomState]) -> float | None:
    temps = [s.current_temp for s in states.values() if s.available and s.current_temp is not None]
    return round(mean(temps), 1) if temps else None


def fresh_air_advice(
    states: dict[str, RoomState], house: HouseState, settings: Settings
) -> tuple[str | None, float | None]:
    """Return (advice, indoor average)."""
    inside = indoor_average(states)
    outside = house.outside_temp
    if inside is None or outside is None:
        return None, inside
    if house.house_mode in (HOUSE_AWAY, HOUSE_OFF, HOUSE_PAUSED) or house.anyone_home is False:
        return None, inside
    if not house.fresh_air:
        if inside >= settings.fresh_air_min_indoor and outside <= inside - settings.fresh_air_delta:
            return ADVICE_OPEN, inside
        return None, inside
    if outside >= inside + settings.fresh_air_close_margin:
        return ADVICE_CLOSE, inside
    return None, inside
