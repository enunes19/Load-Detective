"""UI setup for Load Detective: pick the home power sensor and the devices to watch."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DEVICES,
    CONF_EXCLUDE,
    CONF_GROUPS,
    CONF_LEARN_DAYS,
    CONF_SOURCE,
    CONF_WATTS,
    DEFAULT_LEARN_DAYS,
    DEFAULT_NAME,
    DOMAIN,
    GROUP_DOMAINS,
    WATCH_DOMAINS,
)

CONF_NAME = "name"
CONF_WATTS_TEXT = "watts_text"


def _watts_to_text(watts: dict[str, float]) -> str:
    return "\n".join(f"{k}: {v:g}" for k, v in sorted(watts.items()))


def _text_to_watts(text: str) -> dict[str, float]:
    """Parse 'entity_id: watts' lines. Raises ValueError on a bad line."""
    out: dict[str, float] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.rpartition(":")
        key = key.strip()
        if not sep or "." not in key:
            raise ValueError(line)
        out[key] = float(val.strip())
    return out


def _devices_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_DEVICES, default=defaults.get(CONF_DEVICES, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=WATCH_DOMAINS, multiple=True)
            ),
            vol.Optional(
                CONF_GROUPS, default=defaults.get(CONF_GROUPS, [])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=k, label=v)
                        for k, v in GROUP_DOMAINS.items()
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(
                CONF_EXCLUDE, default=defaults.get(CONF_EXCLUDE, [])
            ): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
            vol.Optional(
                CONF_WATTS_TEXT,
                default=_watts_to_text(defaults.get(CONF_WATTS, {})),
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_LEARN_DAYS,
                default=defaults.get(CONF_LEARN_DAYS, DEFAULT_LEARN_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=30, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


def _clean_devices(user_input: dict[str, Any]) -> dict[str, Any]:
    """Turn form input into stored options. Raises ValueError on bad watts."""
    return {
        CONF_DEVICES: list(user_input.get(CONF_DEVICES, [])),
        CONF_GROUPS: list(user_input.get(CONF_GROUPS, [])),
        CONF_EXCLUDE: list(user_input.get(CONF_EXCLUDE, [])),
        CONF_WATTS: _text_to_watts(user_input.get(CONF_WATTS_TEXT, "")),
        CONF_LEARN_DAYS: int(user_input.get(CONF_LEARN_DAYS, DEFAULT_LEARN_DAYS)),
    }


class LoadDetectiveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_SOURCE])
            self._abort_if_unique_id_configured()
            self._data = {
                CONF_SOURCE: user_input[CONF_SOURCE],
                CONF_NAME: user_input.get(CONF_NAME) or DEFAULT_NAME,
            }
            return await self.async_step_devices()

        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_devices(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                options = _clean_devices(user_input)
            except ValueError:
                errors[CONF_WATTS_TEXT] = "invalid_watts"
            else:
                return self.async_create_entry(
                    title=self._data[CONF_NAME], data=self._data, options=options
                )
        return self.async_show_form(
            step_id="devices",
            data_schema=_devices_schema(user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return LoadDetectiveOptionsFlow()


class LoadDetectiveOptionsFlow(config_entries.OptionsFlow):
    """Change which devices are watched (Configure button on the integration)."""

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                options = _clean_devices(user_input)
            except ValueError:
                errors[CONF_WATTS_TEXT] = "invalid_watts"
            else:
                return self.async_create_entry(title="", data=options)
        current = dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init", data_schema=_devices_schema(current), errors=errors
        )
