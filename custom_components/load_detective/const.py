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
