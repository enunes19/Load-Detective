"""Constants for Load Detective."""

DOMAIN = "load_detective"
DEFAULT_NAME = "Load Detective"

CONF_SOURCE = "source"
CONF_DEVICES = "devices"
CONF_GROUPS = "groups"
CONF_EXCLUDE = "exclude"
CONF_WATTS = "watts"
CONF_LEARN_DAYS = "learn_days"

DEFAULT_LEARN_DAYS = 7

# Entity domains that can be watched individually.
WATCH_DOMAINS = [
    "climate",
    "humidifier",
    "fan",
    "media_player",
    "light",
    "switch",
    "input_boolean",
]

# Domains that can be watched as a whole, with one combined sensor.
GROUP_DOMAINS = {
    "light": "Lights",
    "switch": "Switches",
    "fan": "Fans",
    "media_player": "Media players",
}

# Boiler / oil
CONF_BOILER_ZONES = "boiler_zones"
CONF_BOILER_NAME = "boiler_name"
CONF_OIL_DELIVERIES = "oil_deliveries"  # list of [iso date, gallons]
CONF_OIL_RATE = "oil_rate"  # gallons per heating hour; 0 = learn from deliveries
CONF_TANK_GALLONS = "tank_gallons"  # 0 = unknown

DEFAULT_BOILER_NAME = "Boiler"
