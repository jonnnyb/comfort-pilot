"""Constants for Comfort Pilot."""

DOMAIN = "comfort_pilot"
NAME = "Comfort Pilot"
VERSION = "0.4.0"

PLATFORMS = ["sensor", "select", "switch", "button"]

# Config entry data
CONF_CLIMATES = "climates"
CONF_GRID_SENSOR = "grid_sensor"
CONF_SOLAR_SENSOR = "solar_sensor"
CONF_WEATHER = "weather"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_NOTIFY = "notify"

# Options
OPT_ROOMS = "rooms"
OPT_RULES = "rules"
OPT_SETTINGS = "settings"

# Room options
ROOM_NAME = "name"
ROOM_OCCUPANCY = "occupancy_sensors"
ROOM_WINDOWS = "window_sensors"
ROOM_COOL_MIN = "cool_min"
ROOM_COOL_MAX = "cool_max"
ROOM_HEAT_MIN = "heat_min"
ROOM_HEAT_MAX = "heat_max"
ROOM_PRIORITY = "priority"
ROOM_POWER = "est_power"
ROOM_OCC_TIMEOUT = "occupancy_timeout"
ROOM_OCC_DELAY = "occupancy_delay"

# Coordinator-only settings (stored with the engine settings)
SET_HOLD_MINUTES = "hold_minutes"
SET_QUIET_START = "quiet_start"
SET_QUIET_END = "quiet_end"
SET_MIN_RUN = "min_run_minutes"
SET_MIN_OFF = "min_off_minutes"
DEFAULT_MIN_RUN = 10
DEFAULT_MIN_OFF = 5
DEFAULT_OCC_DELAY = 3
DEFAULT_HOLD_MINUTES = 120
DEFAULT_QUIET_START = "22:00:00"
DEFAULT_QUIET_END = "07:00:00"

UPDATE_INTERVAL_SECONDS = 120
GRID_SAMPLE_SECONDS = 30
GRID_WINDOW_SECONDS = 300
FORECAST_REFRESH_MINUTES = 30
WINDOW_OPEN_DELAY_SECONDS = 120
MIN_SETPOINT_CHANGE = 0.5
MIN_SECONDS_BETWEEN_SETPOINT_CHANGES = 600
OWN_CHANGE_GRACE_SECONDS = 45
NOTIFY_REPEAT_MINUTES = 120
MAX_OVERRIDE_LOG = 2000
MAX_EVENT_LOG = 500

# Mobile app notification actions
ACTION_FRESH_AIR_ON = "COMFORT_PILOT_FRESH_AIR_ON"
ACTION_FRESH_AIR_OFF = "COMFORT_PILOT_FRESH_AIR_OFF"
NOTIFY_TAG = "comfort_pilot_fresh_air"

STORAGE_VERSION = 1
