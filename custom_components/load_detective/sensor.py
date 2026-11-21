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
from datetime import date, datetime, timedelta, timezone
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
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
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
from homeassistant.helpers.storage import Store
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
from .boiler import (
    BOILER_ID,
    KWH_PER_GALLON,
    add_hours,
    calibrate,
    merge_zones,
    since_last_delivery,
)
from .const import (
    CONF_BOILER_NAME,
    CONF_BOILER_ZONES,
    CONF_DEVICES,
    CONF_OIL_DELIVERIES,
    CONF_OIL_RATE,
    CONF_TANK_GALLONS,
    DEFAULT_BOILER_NAME,
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


def _key_name(key: str, hass: HomeAssistant | None = None) -> str:
    """Display name for a watched device, using its name in Home Assistant."""
    eid = key.split("|")[0]
    base = None
    if hass is not None:
        state = hass.states.get(eid)
        if state is not None:
            base = state.attributes.get("friendly_name")
        if not base:
            entry = er.async_get(hass).async_get(eid)
            if entry is not None:
                base = entry.name or entry.original_name
    base = base or _pretty(key)
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


def _run_job(samples, now_ts, settings, intervals, kw_by_key, ready, all_ts, boiler_ivs):
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
    boiler_on = on_series(times, boiler_ivs) if boiler_ivs is not None else None
    return times, values, out, events, dev_power, steps, boiler_on


# --------------------------------------------------------------------------
# Coordinator


class LoadDetective:
    """Keeps buffers, learns devices, runs the classifier, feeds the entities."""

    def __init__(self, hass: HomeAssistant, config, entry_id: str = "default") -> None:
        self.hass = hass
        self.source: str = config[CONF_SOURCE]
        self.settings = Settings()
        # Boiler: several zone thermostats counted as one appliance.
        self.zones: list[str] = list(config.get(CONF_BOILER_ZONES, []))
        self.boiler_name: str = config.get(CONF_BOILER_NAME) or DEFAULT_BOILER_NAME
        self.deliveries = sorted(
            (date.fromisoformat(d), float(g)) for d, g in config.get(CONF_OIL_DELIVERIES, [])
        )
        self.manual_rate: float = float(config.get(CONF_OIL_RATE) or 0)
        self.tank_gallons: float = float(config.get(CONF_TANK_GALLONS) or 0)
        self.zone_modes: dict[str, str | None] = {}
        self._store = Store(hass, 1, f"load_detective.{entry_id}")
        self.heat: dict = {"daily": {}, "since": None, "tracked_until": None}
        self.oil: dict = {}
        # a zone in the boiler isn't also watched on its own (no double counting)
        self.devices: list[str] = [
            d for d in config.get(CONF_DEVICES, []) if d not in set(self.zones)
        ]
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
        if self.zones:
            self._add_entity(BOILER_ID)
            self.models[BOILER_ID].average_off_steps = True
            if "boiler" in self.watts:
                self.models[BOILER_ID].fixed_kw = self.watts["boiler"] / 1000

    @property
    def has_boiler(self) -> bool:
        return bool(self.zones)

    def display_name(self, key: str) -> str:
        if key == BOILER_ID:
            return self.boiler_name
        return _key_name(key, self.hass)

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
        """Real entities watched one by one (not the combined boiler)."""
        return [e for e in self.changes if e != BOILER_ID]

    @property
    def subscribed(self) -> list[str]:
        return self.watched + self.zones

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

        stored = await self._store.async_load()
        if isinstance(stored, dict) and isinstance(stored.get("daily"), dict):
            self.heat.update(stored)
        await self._async_backfill()
        if self.stopped:  # unloaded while history was loading
            return
        self._update_oil()

        self._unsubs.append(
            async_track_state_change_event(self.hass, [self.source], self._handle_source)
        )
        if self.subscribed:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.subscribed, self._handle_device)
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
        if self.subscribed:
            try:
                dev_states = await recorder.async_add_executor_job(
                    partial(
                        history.get_significant_states,
                        self.hass,
                        start,
                        end,
                        entity_ids=self.subscribed,
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

        if self.zones:
            zone_changes = {}
            for eid in self.zones:
                changes = _changes_from_states(eid, dev_states.get(eid.lower(), []))
                if not changes:
                    mode = device_mode(eid, self.hass.states.get(eid))
                    if mode is not IGNORE:
                        changes = [(time.time(), mode)]
                zone_changes[eid] = changes
                if changes:
                    self.zone_modes[eid] = changes[-1][1]
            boiler_changes = merge_zones(zone_changes)
            entity_changes[BOILER_ID] = boiler_changes
            self.changes[BOILER_ID].extend(boiler_changes)
            self._backfill_hours(boiler_changes, start.timestamp(), end.timestamp())

        if self.learn_days and samples and entity_changes:
            learn_samples = [s for s in samples if s[0] >= learn_start.timestamp()]
            slow = {e for e in entity_changes if e.startswith("climate.") or e == BOILER_ID}
            added = await self.hass.async_add_executor_job(
                _learn_job, learn_samples, entity_changes, self.models, slow
            )
            _LOGGER.info("Load Detective learned from %d past switches", added)

        cutoff = buf_start.timestamp() - 900
        self.samples.extend(s for s in samples if s[0] >= cutoff)

    def _backfill_hours(self, boiler_changes, hist_start: float, now_ts: float) -> None:
        """Fill heating hours the live loop didn't see (first install, restarts)."""
        end = (int(now_ts // STEP_S) * STEP_S) - SETTLE_SAMPLES * STEP_S
        start = self.heat.get("tracked_until") or hist_start
        start = max(start, hist_start)
        if self.heat.get("since") is None:
            self.heat["since"] = hist_start
        if end > start:
            ivs = _intervals_from(BOILER_ID, boiler_changes, start).get(BOILER_ID, [])
            add_hours(self.heat["daily"], ivs, start, end, dt_util.DEFAULT_TIME_ZONE)
        self.heat["tracked_until"] = max(end, self.heat.get("tracked_until") or 0)
        self._settled_ts = end
        self._save()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.heat, 60)

    def _update_oil(self) -> None:
        """Recompute the oil rate and the since-delivery figures."""
        if not self.has_boiler:
            return
        tz = dt_util.DEFAULT_TIME_ZONE
        since_ts = self.heat.get("since") or time.time()
        since = datetime.fromtimestamp(since_ts, tz).date()
        today = dt_util.now().date()
        cal_rate, periods = calibrate(self.deliveries, self.heat["daily"], since_ts, tz)
        rate = self.manual_rate or cal_rate
        hours_since, estimated, last = since_last_delivery(
            self.deliveries, self.heat["daily"], since_ts, tz, today
        )
        used_since = rate * hours_since if (rate and hours_since is not None) else None
        remaining = None
        if used_since is not None and self.tank_gallons:
            remaining = max(self.tank_gallons - used_since, 0.0)
        self.oil = {
            "rate": rate,
            "source": "entered" if self.manual_rate else ("deliveries" if cal_rate else None),
            "calibrated_rate": cal_rate,
            "periods": periods,
            "last_delivery": last.isoformat() if last else None,
            "hours_since_delivery": hours_since,
            "used_since_delivery": used_since,
            "remaining": remaining,
            "estimated": estimated,
            "tracking_since": since.isoformat(),
        }

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
        if eid in self.zones:
            self._handle_zone(eid, state)
        if eid not in self.changes or eid == BOILER_ID:
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

    def _handle_zone(self, eid: str, state) -> None:
        mode = device_mode(eid, state)
        if mode is IGNORE:
            return
        self.zone_modes[eid] = mode
        new = "on" if any(m == "heating" for m in self.zone_modes.values()) else None
        changes = self.changes[BOILER_ID]
        prev = changes[-1][1] if changes else None
        if changes and new == prev:
            return
        ts = state.last_updated.timestamp()
        changes.append((ts, new))
        if len(changes) > 1:
            self.pending.extend(_transitions_from(BOILER_ID, [(0, prev), (ts, new)], True))

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
        boiler_ivs = intervals.get(BOILER_ID, []) if self.has_boiler else None
        ready = [
            t for t in self.pending
            if t.ts + MEASURE_AFTER_S <= now_ts
            and not (t.key in self.models and self.models[t.key].average_off_steps and t.turned_on)
        ]
        self.pending = [t for t in self.pending if t.ts + MEASURE_AFTER_S > now_ts]

        self._running = True
        try:
            (
                times, values, out, events, dev_power, steps, boiler_on,
            ) = await self.hass.async_add_executor_job(
                _run_job, list(self.samples), now_ts, self.settings,
                intervals, kw_by_key, ready, all_ts, boiler_ivs,
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
                if boiler_on is not None:
                    hours = 0.0
                    daily = self.heat["daily"]
                    for i in range(settle_idx + 1):
                        if times[i] <= self._settled_ts or not boiler_on[i]:
                            continue
                        day = dt_util.as_local(
                            datetime.fromtimestamp(times[i], tz=timezone.utc)
                        ).date().isoformat()
                        daily[day] = daily.get(day, 0.0) + STEP_S / 3600
                        hours += STEP_S / 3600
                    self.heat["tracked_until"] = settle_ts
                    self._save()
                    self._update_oil()
                    rate = self.oil.get("rate")
                    deltas["boiler:hours"] = hours
                    if rate:
                        deltas["boiler:oil"] = hours * rate
                        deltas["boiler:oil_kwh"] = hours * rate * KWH_PER_GALLON
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
    detective = LoadDetective(hass, config, entry.entry_id)
    title = entry.title or DEFAULT_NAME
    ctx = (detective, entry.entry_id, title)

    entities: list[SensorEntity] = [ActivitySensor(*ctx), LearnedDevicesSensor(*ctx)]
    for label in LABELS:
        name = LABEL_NAMES[label]
        entities.append(PowerSensor(*ctx, label, name, _ICONS.get(label)))
        entities.append(EnergySensor(*ctx, label, name, _ICONS.get(label)))
    for eid in detective.devices:
        for key in device_keys(eid):
            name = _key_name(key, hass)
            icon = _domain_icon(eid, key)
            entities.append(PowerSensor(*ctx, key, name, icon, watched=True))
            entities.append(EnergySensor(*ctx, key, name, icon, watched=True))
    for domain in detective.group_domains:
        target = f"group:{domain}"
        name = f"All {GROUP_DOMAINS.get(domain, domain).lower()}"
        icon = _DOMAIN_ICONS.get(domain, "mdi:devices")
        entities.append(PowerSensor(*ctx, target, name, icon))
        entities.append(EnergySensor(*ctx, target, name, icon))
    if detective.has_boiler:
        bname = detective.boiler_name
        entities.append(PowerSensor(*ctx, BOILER_ID, bname, "mdi:water-boiler"))
        entities.append(EnergySensor(*ctx, BOILER_ID, bname, "mdi:water-boiler"))
        entities += [
            TotalSensor(*ctx, "boiler:hours", f"{bname} heating hours", "mdi:timer-outline",
                        UnitOfTime.HOURS, SensorDeviceClass.DURATION),
            TotalSensor(*ctx, "boiler:oil", f"{bname} oil used", "mdi:oil",
                        UnitOfVolume.GALLONS, SensorDeviceClass.VOLUME),
            TotalSensor(*ctx, "boiler:oil_kwh", f"{bname} oil energy", "mdi:fire",
                        UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
            OilSensor(*ctx, "used_since_delivery", f"{bname} oil since last delivery"),
            OilSensor(*ctx, "rate", f"{bname} oil per heating hour"),
        ]
        if detective.tank_gallons:
            entities.append(OilSensor(*ctx, "remaining", f"{bname} oil remaining"))
    async_add_entities(entities)

    entry.async_on_unload(detective.async_stop)
    entry.async_on_unload(async_at_started(hass, detective.async_start))


def _slug(target: str) -> str:
    return target.replace(".", "_").replace("|", "_").replace(":", "_")


class _Base(SensorEntity):
    """Names are the appliance itself ("Dryer"), not "Load Detective Dryer"."""

    _attr_should_poll = False
    _attr_has_entity_name = False

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


class _ApplianceBase(_Base):
    """Shared naming for power/energy pairs.

    Energy sensors are named for the appliance alone ("Dryer"), which is what
    the Energy dashboard shows; power sensors add " power". For a watched
    device the name follows that device's name in Home Assistant, so renaming
    the TV renames its sensors too.
    """

    _suffix = ""

    def __init__(self, detective, entry_id, title, target, name, icon, watched=False) -> None:
        super().__init__(detective, entry_id, title)
        self._target = target
        self._watched = watched
        self._attr_name = f"{name}{self._suffix}"
        self._attr_icon = icon

    def _refresh_name(self) -> None:
        if not self._watched or self.hass is None:
            return
        name = f"{_key_name(self._target, self.hass)}{self._suffix}"
        if name != self._attr_name:
            self._attr_name = name


class PowerSensor(_ApplianceBase):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2
    _suffix = " power"

    def __init__(self, detective, entry_id, title, target, name, icon, watched=False) -> None:
        super().__init__(detective, entry_id, title, target, name, icon, watched)
        self._attr_unique_id = f"{self._uid_base}_{_slug(target)}_power"

    async def async_added_to_hass(self) -> None:
        self._refresh_name()
        self._d.add_listener(self._update)

    @callback
    def _update(self) -> None:
        self._refresh_name()
        val = self._d.power_for(self._target)
        self._attr_native_value = None if val is None else round(val, 3)
        self.async_write_ha_state()


class EnergySensor(_ApplianceBase, RestoreSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, detective, entry_id, title, target, name, icon, watched=False) -> None:
        super().__init__(detective, entry_id, title, target, name, icon, watched)
        self._total = 0.0
        self._attr_unique_id = f"{self._uid_base}_{_slug(target)}_energy"
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._total = float(last.native_value)
            except (TypeError, ValueError):
                self._total = 0.0
        self._attr_native_value = round(self._total, 3)
        self._refresh_name()
        self._d.add_energy_listener(self._add)
        self._d.add_listener(self._maybe_rename)

    @callback
    def _maybe_rename(self) -> None:
        """Pick up the device's name once its integration has loaded."""
        old = self._attr_name
        self._refresh_name()
        if self._attr_name != old:
            self.async_write_ha_state()

    @callback
    def _add(self, deltas: dict[str, float]) -> None:
        inc = deltas.get(self._target, 0.0)
        if inc <= 0:
            return
        self._total += inc
        self._attr_native_value = round(self._total, 3)
        self._refresh_name()
        self.async_write_ha_state()


class ActivitySensor(_Base):
    """What is running right now, plus the recent event list."""

    _attr_icon = "mdi:home-lightning-bolt-outline"

    def __init__(self, detective, entry_id, title) -> None:
        super().__init__(detective, entry_id, title)
        self._attr_name = f"{title} activity"
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
            self._d.display_name(key)
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
        self._attr_name = f"{title} learned devices"
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


class TotalSensor(_Base, RestoreSensor):
    """A running total (heating hours, gallons) that survives restarts."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1

    def __init__(self, detective, entry_id, title, target, name, icon, unit, device_class) -> None:
        super().__init__(detective, entry_id, title)
        self._target = target
        self._total = 0.0
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_unique_id = f"{self._uid_base}_{_slug(target)}"
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


class OilSensor(_Base):
    """Oil figures that go up and down: since last delivery, remaining, rate."""

    def __init__(self, detective, entry_id, title, field, name) -> None:
        super().__init__(detective, entry_id, title)
        self._field = field
        self._attr_name = name
        self._attr_unique_id = f"{self._uid_base}_boiler_oil_{field}"
        if field == "rate":
            self._attr_native_unit_of_measurement = "gal/h"
            self._attr_icon = "mdi:speedometer"
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_suggested_display_precision = 2
        elif field == "remaining":
            self._attr_native_unit_of_measurement = UnitOfVolume.GALLONS
            self._attr_device_class = SensorDeviceClass.VOLUME_STORAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_icon = "mdi:storage-tank-outline"
            self._attr_suggested_display_precision = 0
        else:
            self._attr_native_unit_of_measurement = UnitOfVolume.GALLONS
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_icon = "mdi:oil"
            self._attr_suggested_display_precision = 1

    async def async_added_to_hass(self) -> None:
        self._d.add_listener(self._update)

    @callback
    def _update(self) -> None:
        oil = self._d.oil
        val = oil.get(self._field)
        self._attr_native_value = None if val is None else round(val, 3)
        attrs = {
            "last_delivery": oil.get("last_delivery"),
            "tracking_since": oil.get("tracking_since"),
        }
        if self._field == "rate":
            attrs.update(
                source=oil.get("source"),
                learned_from_deliveries=(
                    round(oil["calibrated_rate"], 3) if oil.get("calibrated_rate") else None
                ),
                delivery_periods=oil.get("periods"),
            )
        else:
            attrs.update(
                heating_hours_since_delivery=(
                    round(oil["hours_since_delivery"], 1)
                    if oil.get("hours_since_delivery") is not None else None
                ),
                estimated_from_partial_data=oil.get("estimated"),
            )
        self._attr_extra_state_attributes = attrs
        self.async_write_ha_state()
