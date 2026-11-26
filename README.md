# Comfort Pilot

Solar-aware, occupancy-aware control for split systems and heat pumps in Home Assistant.

Instead of setting temperatures yourself, you give each room a **comfort band** (for example, cool between 23° and 26°). Comfort Pilot picks where in that band each unit should sit:

| Situation | What happens |
|---|---|
| Someone in the room, spare solar | Comfortable end (23°) |
| Someone in the room, running on grid | Economy end (26°) |
| Room empty | Off |
| A rule says the room will be needed soon, and there's spare solar | Pre-cools early on free power |
| It's cooler outside than in | Notification to run the evap on fan / open up |

It only sets **mode and setpoint** on your existing climate entities. The splits do their own regulation.

> Status: phase 1. Learning from your manual changes is being *recorded* already; turning those into suggested rules arrives in phase 2.

## Install (HACS)

1. HACS → ⋮ (top right) → **Custom repositories**
2. Repository: `https://github.com/YOUR_GITHUB_USERNAME/comfort-pilot`, Type: **Integration** → Add
3. Find **Comfort Pilot** in HACS → **Download**
4. Restart Home Assistant
5. Settings → Devices & services → **Add integration** → Comfort Pilot

## Setup

You pick:

- **Split systems** – your climate entities
- **Grid power sensor** – positive when importing, negative when exporting (W or kW)
- **Weather** – for outside temperature and today's forecast (or an outdoor sensor)
- **Notifications** – your phone, e.g. `notify.mobile_app_yourphone`

Rooms are named from your Home Assistant areas, and motion/presence/window sensors in the same area are added automatically. Adjust anything under **Configure**:

- **Rooms** – comfort bands, sensors, priority when sharing solar, typical power draw
- **Add a rule** / **Remove rules** – schedules
- **Settings** – season thresholds, safety limits, notification quiet hours, how long manual changes are respected
- **Split systems, energy, weather and notifications** – change what you picked at setup

### Rules

Rules handle the things sensors can't see. The most useful one:

> **Bedroom** – Keep comfortable – every day – 21:00 to 06:30 – 24°

Motion sensors don't see sleeping people, so bedrooms need a rule like this. Windows that end earlier than they start run past midnight.

| Action | Effect |
|---|---|
| Keep comfortable | Room counts as occupied. With a fixed temperature it holds that; without, solar decides comfortable vs economy. Also pre-cools before the window on spare solar. |
| Economy only | Room may run, but never at the comfortable end |
| Keep off | Room stays off |

## What you get

**House device**

| Entity | Purpose |
|---|---|
| `select.comfort_pilot_house_mode` | Auto (uses person tracking), Home, Away, Boost, Off, Paused |
| `switch.comfort_pilot_fresh_air_mode` | Evap/windows running: splits pause |
| `sensor.comfort_pilot_status` | Summary, with outside/inside temps, forecast and rules as attributes |
| `sensor.comfort_pilot_solar_budget` | Power available for comfort (export + what the splits are already using) |
| `sensor.comfort_pilot_season` | Cooling / heating / neutral |
| `sensor.comfort_pilot_indoor_average` | Average of the units' room temperatures |
| `sensor.comfort_pilot_manual_changes_recorded` | Learning data collected so far |

**Per room**

| Entity | Purpose |
|---|---|
| `sensor.comfort_pilot_<room>_status` | Plain English: *"Cool 23° – Occupied – solar available (2.1 kW)"* |
| `select.comfort_pilot_<room>_mode` | Auto, Comfort, Eco, Off |
| `button.comfort_pilot_<room>_resume_auto` | Cancel a manual hold |

### Manual changes

If you change a unit yourself (app, dashboard or IR remote), Comfort Pilot leaves that room alone for 2 hours (configurable) and records the change for learning. Press **Resume auto** to hand back early.

### Fresh air

When it's at least 2° cooler outside than the indoor average (and inside is 24° or warmer), you get a notification with a **Fresh air mode on** button. While it's on, the splits stay off. When outside becomes warmer than inside, you get a **close up** notification with a button to hand back to the splits. No notifications during quiet hours (22:00–07:00 by default).

## Example dashboard card

```yaml
type: entities
title: Comfort Pilot
entities:
  - select.comfort_pilot_house_mode
  - switch.comfort_pilot_fresh_air_mode
  - sensor.comfort_pilot_solar_budget
  - type: section
  - entity: sensor.comfort_pilot_office_status
    name: Office
  - entity: sensor.comfort_pilot_bedroom_status
    name: Bedroom
```

## How it decides (every 2 minutes, and straight away on motion)

1. Paused, manual hold, room or house off → leave alone / off
2. Safety: room above 32° or below 12° → condition it regardless
3. Window open (2 min), fresh air mode, nobody home → off
4. Season from today's forecast high: ≥ 26° cooling, ≤ 17° heating, otherwise neutral (only acts if a room drifts outside its band)
5. Is the room needed? Rule active, occupied, or a rule starting soon
6. Share the solar budget by priority: occupied rooms first, then pre-cooling. Rooms that don't get solar run at the economy end.
7. Gentle output: at most one setpoint change per room every 10 minutes, ignores changes under 0.5°

## Roadmap

- **Phase 2** – learned occupancy patterns and rules from your manual changes (editable lists), learned room warm-up/cool-down rates for smarter pre-cooling, learned power draw per unit
- **Phase 3** – sidebar panel for rules and learnings, weekly solar vs grid summary

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```
