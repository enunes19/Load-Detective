"""Sensor platform for Load Detective.

Every 30 seconds the last 6 hours of the source sensor are run through the
classifier. Watched Home Assistant devices (lights, TVs, the thermostat...)
are handled first: each one's wattage is learned from how much the home load
jumps when it switches, and while it is on that power is credited to it and
removed before the appliance rules run.

Power sensors show the current estimate. Energy sensors only count samples
once they are 15 minutes old, because a few rules need to see what happens
next before they are sure.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import partial

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType

try:
    from homeassistant.helpers.device_registry import DeviceInfo
except ImportError:  # older releases
    from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .classifier import (
    ALWAYS_ON,
    LABEL_NAMES,
    LABELS,
    OTHER,
    STEP_S,
    Settings,
    classify,
    resample,
)
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
)
from .learning import DeviceModel, LEARNED, FIXED, Transition, learn, measure, on_series

_LOGGER = logging.getLogger(__name__)

BUFFER_S = 6 * 3600
SETTLE_SAMPLES = 30  # 15 minutes at 30 s
UPDATE_INTERVAL = timedelta(seconds=STEP_S)
MAX_EVENTS_ATTR = 15
MEASURE_AFTER_S = 420  # wait this long after a switch before measuring it

CLIMATE_MODES = ("heating", "cooling", "fan")
ON_MEDIA_STATES = ("on", "playing", "paused", "idle", "buffering")
IGNORE = object()  # state we can't interpret (unavailable/unknown)


# --------------------------------------------------------------------------
# Helpers


def _to_kw(state) -> float | None:
    """Read a power state as kW, whatever unit the source uses."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit == UnitOfPower.WATT:
        return value / 1000
    if unit == getattr(UnitOfPower, "MEGA_WATT", "MW"):
        return value * 1000
    return value


def device_mode(entity_id: str, state):
    """What a watched device is doing: None (off), a mode string, or IGNORE."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return IGNORE
    domain = entity_id.split(".", 1)[0]
    if domain == "climate":
        action = state.attributes.get("hvac_action")
        if action in ("heating", "preheating", "defrosting"):
            return "heating"
        if action in ("cooling", "drying"):
            return "cooling"
        if action == "fan":
            return "fan"
        return None
    if domain == "media_player":
        return "on" if state.state in ON_MEDIA_STATES else None
    if domain == "humidifier":
        action = state.attributes.get("action")
        if action is not None:
            return "on" if action in ("drying", "humidifying") else None
    return "on" if state.state == "on" else None


def device_keys(entity_id: str) -> list[str]:
    if entity_id.startswith("climate."):
        return [f"{entity_id}|{m}" for m in CLIMATE_MODES]
    return [entity_id]


def mode_key(entity_id: str, mode: str | None) -> str | None:
    if mode is None:
        return None
    return f"{entity_id}|{mode}" if entity_id.startswith("climate.") else entity_id


def _pretty(entity_id: str) -> str:
    obj = entity_id.split(".", 1)[1].split("|")[0]
    text = obj.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _key_name(key: str) -> str:
    base = _pretty(key)
    if "|" in key:
        return f"{base} {key.split('|')[1]}"
    return base


def _transitions_from(entity_id: str, changes, slow: bool) -> list[Transition]:
    """changes: list of (ts, mode) where mode differs from the previous one."""
    out = []
    prev = None
    first = True
    for ts, mode in changes:
        if not first:
            old_key, new_key = mode_key(entity_id, prev), mode_key(entity_id, mode)
            if old_key:
                out.append(Transition(ts, old_key, False, slow))
            if new_key:
                out.append(Transition(ts, new_key, True, slow))
        prev = mode
        first = False
    return out


def _intervals_from(entity_id: str, changes, start_ts: float):
    """On-intervals per key from (ts, mode) changes, clipped to start_ts."""
    res: dict[str, list] = {}
    cur_key, cur_start = None, None
    for ts, mode in changes:
        if cur_key is not None:
            res.setdefault(cur_key, []).append((max(cur_start, start_ts), ts))
        cur_key, cur_start = mode_key(entity_id, mode), ts
    if cur_key is not None:
        res.setdefault(cur_key, []).append((max(cur_start, start_ts), None))
    return {k: [iv for iv in v if iv[1] is None or iv[1] > start_ts] for k, v in res.items()}


def _changes_from_states(entity_id: str, states) -> list[tuple[float, str | None]]:
    changes = []
    last = IGNORE
    for st in states:
        mode = device_mode(entity_id, st)
        if mode is IGNORE or mode == last:
            continue
        changes.append((st.last_updated.timestamp(), mode))
        last = mode
    return changes


def _learn_job(samples, entity_changes, models, slow_entities):
    """Executor: learn wattages from a long stretch of history."""
    if len(samples) < 10:
        return 0
    times, values = resample(samples, samples[0][0], samples[-1][0])
    transitions = []
    for eid, changes in entity_changes.items():
        transitions.extend(_transitions_from(eid, changes, eid in slow_entities))
    transitions.sort(key=lambda t: t.ts)
    return learn(times, values, transitions, models)


def _run_job(samples, now_ts, settings, intervals, kw_by_key, ready, all_ts):
    """Executor: build the grid, credit known devices, classify, measure switches."""
    times, values = resample(samples, now_ts - BUFFER_S, now_ts)
    hours = [
        dt_util.as_local(datetime.fromtimestamp(t, tz=timezone.utc)).hour for t in times
    ]
    dev_power: dict[str, list[float]] = {}
    known = [0.0] * len(times)
    for key, ivs in intervals.items():
        kw = kw_by_key.get(key, 0.0)
        if kw <= 0:
            continue
        flags = on_series(times, ivs)
        series = [kw if f else 0.0 for f in flags]
        # never credit more than the house is actually using
        for i, v in enumerate(values):
            if v is None:
                series[i] = 0.0
        dev_power[key] = series
        for i, x in enumerate(series):
            known[i] += x
    # scale down if watched devices would exceed the measured load
    for i, v in enumerate(values):
        if v is not None and known[i] > v and known[i] > 0:
            f = v / known[i]
            for series in dev_power.values():
                series[i] *= f
            known[i] = v
    out, events = classify(values, hours, settings, known=known)
    steps = []
    if times:
        sorted_ts = sorted(all_ts)
        for tr in ready:
            steps.append((tr.key, measure(times, values, times[0], tr, sorted_ts)))
    return times, values, out, events, dev_power, steps


# --------------------------------------------------------------------------
# Coordinator


class LoadDetective:
    """Keeps buffers, learns devices, runs the classifier, feeds the entities."""

    def __init__(self, hass: HomeAssistant, config) -> None:
        self.hass = hass
        self.source: str = config[CONF_SOURCE]
        self.settings = Settings()
        self.devices: list[str] = list(config.get(CONF_DEVICES, []))
        self.group_domains: list[str] = [d.strip().lower() for d in config.get(CONF_GROUPS, [])]
        self.exclude: set[str] = set(config.get(CONF_EXCLUDE, []))
        self.watts: dict[str, float] = dict(config.get(CONF_WATTS, {}))
        self.learn_days: int = int(config.get(CONF_LEARN_DAYS, DEFAULT_LEARN_DAYS))

        self.group_members: dict[str, list[str]] = {d: [] for d in self.group_domains}
        self.models: dict[str, DeviceModel] = {}
        self.samples: deque[tuple[float, float]] = deque()
        self.changes: dict[str, deque] = {}
        self.pending: list[Transition] = []
        self.current: dict[str, float | None] = {k: None for k in LABELS}
        self.device_now: dict[str, float] = {}
        self.events: list[dict] = []
        self.started = False
        self.stopped = False
        self._settled_ts: float | None = None
        self._running = False
        self._listeners: list = []
        self._energy_listeners: list = []
        self._unsubs: list = []
        for eid in self.devices:
            self._add_entity(eid)

    # -- setup ------------------------------------------------------------

    def _add_entity(self, eid: str) -> None:
        if eid in self.changes:
            return
        self.changes[eid] = deque()
        for key in device_keys(eid):
            model = DeviceModel(key)
            fixed = self.watts.get(key)
            if fixed is None and "|" not in key:
                fixed = self.watts.get(eid)
            if fixed is not None:
                model.fixed_kw = fixed / 1000
            self.models[key] = model

    @property
    def watched(self) -> list[str]:
        return list(self.changes)

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def add_energy_listener(self, cb) -> None:
        self._energy_listeners.append(cb)

    async def async_start(self, _hass=None) -> None:
        # Groups are resolved once Home Assistant has started, so every
        # integration's entities exist.
        explicit = set(self.devices)
        for domain in self.group_domains:
            members = [
                eid
                for eid in sorted(self.hass.states.async_entity_ids(domain))
                if eid not in self.exclude and eid not in explicit
            ]
            self.group_members[domain] = members
            for eid in members:
                self._add_entity(eid)

        await self._async_backfill()
        if self.stopped:  # unloaded while history was loading
            return

        self._unsubs.append(
            async_track_state_change_event(self.hass, [self.source], self._handle_source)
        )
        if self.watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.watched, self._handle_device)
            )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._handle_interval, UPDATE_INTERVAL)
        )
        self.started = True
        _LOGGER.info(
            "Load Detective watching %d devices: %s",
            len(self.watched),
            {k: m.summary()["status"] for k, m in self.models.items()},
        )
        await self.async_refresh()

    async def _async_backfill(self) -> None:
        """Load recent history: 6 h for the buffers, learn_days for learning."""
        end = dt_util.utcnow()
        learn_start = end - timedelta(days=self.learn_days)
        buf_start = end - timedelta(seconds=BUFFER_S)
        start = min(learn_start, buf_start)
        recorder = get_instance(self.hass)
        try:
            src = await recorder.async_add_executor_job(
                partial(
                    history.state_changes_during_period,
                    self.hass,
                    start,
                    end,
                    self.source,
                    include_start_time_state=True,
                )
            )
        except Exception:  # noqa: BLE001 - backfill is a nicety, not required
            _LOGGER.warning("Could not load history for %s; starting empty", self.source)
            src = {}
        samples = []
        for state in src.get(self.source.lower(), []):
            kw = _to_kw(state)
            if kw is not None:
                samples.append((state.last_updated.timestamp(), kw))
        samples.sort()

        dev_states = {}
        if self.watched:
            try:
                dev_states = await recorder.async_add_executor_job(
                    partial(
                        history.get_significant_states,
                        self.hass,
                        start,
                        end,
                        entity_ids=self.watched,
                        include_start_time_state=True,
                        significant_changes_only=False,
                        minimal_response=False,
                        no_attributes=False,
                    )
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Could not load history for watched devices")
                dev_states = {}

        entity_changes = {}
        for eid in self.watched:
            changes = _changes_from_states(eid, dev_states.get(eid.lower(), []))
            if not changes:  # no history: start from the current state
                mode = device_mode(eid, self.hass.states.get(eid))
                if mode is not IGNORE:
                    changes = [(time.time(), mode)]
            entity_changes[eid] = changes
            self.changes[eid].extend(changes)

        if self.learn_days and samples and self.watched:
            learn_samples = [s for s in samples if s[0] >= learn_start.timestamp()]
            slow = {e for e in self.watched if e.startswith("climate.")}
            added = await self.hass.async_add_executor_job(
                _learn_job, learn_samples, entity_changes, self.models, slow
            )
            _LOGGER.info("Load Detective learned from %d past switches", added)

        cutoff = buf_start.timestamp() - 900
        self.samples.extend(s for s in samples if s[0] >= cutoff)

    # -- live updates -----------------------------------------------------

    @callback
    def _handle_source(self, event: Event) -> None:
        state = event.data.get("new_state")
        kw = _to_kw(state)
        if kw is None:
            return
        ts = state.last_updated.timestamp()
        if self.samples and ts <= self.samples[-1][0]:
            return
        self.samples.append((ts, kw))

    @callback
    def _handle_device(self, event: Event) -> None:
        eid = event.data.get("entity_id")
        state = event.data.get("new_state")
        if eid not in self.changes:
            return
        mode = device_mode(eid, state)
        if mode is IGNORE:
            return
        changes = self.changes[eid]
        prev = changes[-1][1] if changes else None
        if changes and mode == prev:
            return
        ts = state.last_updated.timestamp()
        changes.append((ts, mode))
        if len(changes) > 1:
            slow = eid.startswith("climate.")
            self.pending.extend(_transitions_from(eid, [(0, prev), (ts, mode)], slow))

    async def _handle_interval(self, _now) -> None:
        await self.async_refresh()

    @callback
    def async_stop(self) -> None:
        """Stop listening (integration unloaded or reloaded)."""
        self.stopped = True
        self.started = False
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()
        self._listeners.clear()
        self._energy_listeners.clear()

    async def async_refresh(self) -> None:
        if self._running or not self.started or self.stopped:
            return
        now_ts = time.time()
        cutoff = now_ts - BUFFER_S - 900
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        if len(self.samples) < 10:
            return

        # On-intervals for each device over the buffer.
        buf_start = now_ts - BUFFER_S
        intervals: dict[str, list] = {}
        all_ts: list[float] = []
        for eid, changes in self.changes.items():
            # keep the last change before the buffer so the state is known
            while len(changes) > 1 and changes[1][0] < cutoff:
                changes.popleft()
            intervals.update(_intervals_from(eid, list(changes), buf_start))
            all_ts.extend(ts for ts, _ in changes)
        kw_by_key = {k: m.kw for k, m in self.models.items()}
        ready = [t for t in self.pending if t.ts + MEASURE_AFTER_S <= now_ts]
        self.pending = [t for t in self.pending if t.ts + MEASURE_AFTER_S > now_ts]

        self._running = True
        try:
            times, values, out, events, dev_power, steps = await self.hass.async_add_executor_job(
                _run_job, list(self.samples), now_ts, self.settings,
                intervals, kw_by_key, ready, all_ts,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Load Detective classification failed")
            return
        finally:
            self._running = False
        if self.stopped:
            return

        for key, step in steps:
            if step is not None and key in self.models:
                self.models[key].add(step)

        last = len(values) - 1
        while last >= 0 and values[last] is None:
            last -= 1
        for k in LABELS:
            self.current[k] = out[k][last] if last >= 0 else None
        self.device_now = {
            key: (dev_power[key][last] if key in dev_power and last >= 0 else 0.0)
            for key in self.models
        }

        self.events = [
            {
                "appliance": LABEL_NAMES[e["label"]],
                "start": dt_util.as_local(
                    datetime.fromtimestamp(times[e["start"]], tz=timezone.utc)
                ).isoformat(timespec="minutes"),
                "minutes": round((e["end"] - e["start"] + 1) * STEP_S / 60, 1),
                "kwh": round(e["kwh"], 2),
                "ongoing": e["end"] >= len(times) - 2,
            }
            for e in events
        ][-MAX_EVENTS_ATTR:]

        # Energy: credit samples once they are settled.
        settle_idx = len(times) - 1 - SETTLE_SAMPLES
        if settle_idx > 0:
            settle_ts = times[settle_idx]
            if self._settled_ts is None:
                self._settled_ts = settle_ts
            elif settle_ts > self._settled_ts:
                deltas = {k: 0.0 for k in LABELS}
                for key in self.models:
                    deltas[key] = 0.0
                for i in range(settle_idx + 1):
                    if times[i] <= self._settled_ts:
                        continue
                    for k in LABELS:
                        deltas[k] += out[k][i] * STEP_S / 3600
                    for key, series in dev_power.items():
                        deltas[key] += series[i] * STEP_S / 3600
                for domain, members in self.group_members.items():
                    deltas[f"group:{domain}"] = sum(
                        deltas.get(key, 0.0) for eid in members for key in device_keys(eid)
                    )
                self._settled_ts = settle_ts
                for cb in self._energy_listeners:
                    cb(deltas)

        for cb in self._listeners:
            cb()

    # -- read helpers for entities ----------------------------------------

    def power_for(self, target: str) -> float | None:
        if target in LABELS:
            return self.current.get(target)
        if target.startswith("group:"):
            domain = target.split(":", 1)[1]
            return sum(
                self.device_now.get(key, 0.0)
                for eid in self.group_members.get(domain, [])
                for key in device_keys(eid)
            )
        return self.device_now.get(target, 0.0)


# --------------------------------------------------------------------------
# Platform


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    config = {**entry.data, **entry.options}
    detective = LoadDetective(hass, config)
    title = entry.title or DEFAULT_NAME
    ctx = (detective, entry.entry_id, title)

    entities: list[SensorEntity] = [ActivitySensor(*ctx), LearnedDevicesSensor(*ctx)]
    for label in LABELS:
        name = LABEL_NAMES[label]
        entities.append(PowerSensor(*ctx, label, name, _ICONS.get(label)))
        entities.append(EnergySensor(*ctx, label, name, _ICONS.get(label)))
    for eid in detective.devices:
        for key in device_keys(eid):
            name = _key_name(key)
            icon = _domain_icon(eid, key)
            entities.append(PowerSensor(*ctx, key, name, icon))
            entities.append(EnergySensor(*ctx, key, name, icon))
    for domain in detective.group_domains:
        target = f"group:{domain}"
        name = f"All {GROUP_DOMAINS.get(domain, domain).lower()}"
        icon = _DOMAIN_ICONS.get(domain, "mdi:devices")
        entities.append(PowerSensor(*ctx, target, name, icon))
        entities.append(EnergySensor(*ctx, target, name, icon))
    async_add_entities(entities)

    entry.async_on_unload(detective.async_stop)
    entry.async_on_unload(async_at_started(hass, detective.async_start))


def _slug(target: str) -> str:
    return target.replace(".", "_").replace("|", "_").replace(":", "_")


class _Base(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, detective: LoadDetective, entry_id: str, title: str) -> None:
        self._d = detective
        self._uid_base = entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=title,
            manufacturer="Load Detective",
            model="Home load disaggregation",
            entry_type=DeviceEntryType.SERVICE,
        )


class PowerSensor(_Base):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2

    def __init__(self, detective, entry_id, title, target, name, icon) -> None:
        super().__init__(detective, entry_id, title)
        self._target = target
        self._attr_name = f"{name} power"
        self._attr_unique_id = f"{self._uid_base}_{_slug(target)}_power"
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        self._d.add_listener(self._update)

    @callback
    def _update(self) -> None:
        val = self._d.power_for(self._target)
        self._attr_native_value = None if val is None else round(val, 3)
        self.async_write_ha_state()


class EnergySensor(_Base, RestoreSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, detective, entry_id, title, target, name, icon) -> None:
        super().__init__(detective, entry_id, title)
        self._target = target
        self._total = 0.0
        self._attr_name = f"{name} energy"
        self._attr_unique_id = f"{self._uid_base}_{_slug(target)}_energy"
        self._attr_icon = icon
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._total = float(last.native_value)
            except (TypeError, ValueError):
                self._total = 0.0
        self._attr_native_value = round(self._total, 3)
        self._d.add_energy_listener(self._add)

    @callback
    def _add(self, deltas: dict[str, float]) -> None:
        inc = deltas.get(self._target, 0.0)
        if inc <= 0:
            return
        self._total += inc
        self._attr_native_value = round(self._total, 3)
        self.async_write_ha_state()


class ActivitySensor(_Base):
    """What is running right now, plus the recent event list."""

    _attr_icon = "mdi:home-lightning-bolt-outline"

    def __init__(self, detective, entry_id, title) -> None:
        super().__init__(detective, entry_id, title)
        self._attr_name = "Activity"
        self._attr_unique_id = f"{self._uid_base}_activity"

    async def async_added_to_hass(self) -> None:
        self._d.add_listener(self._update)

    @callback
    def _update(self) -> None:
        running = [
            LABEL_NAMES[k]
            for k in LABELS
            if k not in (ALWAYS_ON, OTHER) and (self._d.current.get(k) or 0) > 0.05
        ]
        running += [
            _key_name(key)
            for key, kw in sorted(self._d.device_now.items())
            if kw >= 0.05
        ]
        self._attr_native_value = ", ".join(running)[:255] if running else "Idle"
        self._attr_extra_state_attributes = {"recent_events": self._d.events}
        self.async_write_ha_state()


class LearnedDevicesSensor(_Base):
    """How many watched devices have a learned wattage, with details."""

    _attr_icon = "mdi:school-outline"
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, detective, entry_id, title) -> None:
        super().__init__(detective, entry_id, title)
        self._attr_name = "Learned devices"
        self._attr_unique_id = f"{self._uid_base}_learned_devices"

    async def async_added_to_hass(self) -> None:
        self._d.add_listener(self._update)

    @callback
    def _update(self) -> None:
        models = self._d.models
        self._attr_native_value = sum(
            1 for m in models.values() if m.status in (LEARNED, FIXED)
        )
        self._attr_extra_state_attributes = {
            "devices": {key: m.summary() for key, m in sorted(models.items())},
            "watching": len(self._d.watched),
        }
        self.async_write_ha_state()


_ICONS = {
    "water_heater": "mdi:water-boiler",
    "dryer": "mdi:tumble-dryer",
    "range": "mdi:stove",
    "coffee_maker": "mdi:coffee-maker",
    "toaster_oven": "mdi:toaster-oven",
    "small_appliance": "mdi:power-plug",
    "always_on": "mdi:server",
    "other": "mdi:help-circle-outline",
}

_DOMAIN_ICONS = {
    "light": "mdi:lightbulb-group",
    "media_player": "mdi:television",
    "fan": "mdi:fan",
    "switch": "mdi:toggle-switch",
    "humidifier": "mdi:air-humidifier",
    "climate": "mdi:thermostat",
}


def _domain_icon(eid: str, key: str) -> str:
    if key.endswith("|heating"):
        return "mdi:fire"
    if key.endswith("|cooling"):
        return "mdi:snowflake"
    if key.endswith("|fan"):
        return "mdi:fan"
    return _DOMAIN_ICONS.get(eid.split(".", 1)[0], "mdi:power-plug")
