"""Learn how much power a device draws from its on/off switches.

Pure Python, no Home Assistant imports. Each time a watched device switches,
the whole-home load just before and just after the switch is compared. The
median of many such steps is the device's wattage. Switches that happen
close to another watched device's switch, or while the load is jumping
around for other reasons, are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .classifier import STEP_S

# Seconds relative to the switch: (before_start, before_end, after_start, after_end)
WINDOW_DEFAULT = (-120, -15, 45, 150)
# Furnaces start the blower late and compressors ramp, so look later.
WINDOW_SLOW = (-120, -15, 150, 360)

MIN_SAMPLES = 5
MIN_KW = 0.02
SPREAD_KW = 0.30
MAX_DELTAS = 150
TOO_SMALL_AFTER = 40  # give small devices plenty of switches before giving up

LEARNING = "learning"
LEARNED = "learned"
TOO_SMALL = "too small to measure"
NOT_SWITCHING = "not switching"
FIXED = "fixed"


@dataclass
class Transition:
    ts: float
    key: str  # device key, e.g. "fan.purifier" or "climate.home|cooling"
    turned_on: bool
    slow: bool = False


@dataclass
class DeviceModel:
    key: str
    deltas: list[float] = field(default_factory=list)
    fixed_kw: float | None = None

    def add(self, delta: float) -> None:
        self.deltas.append(delta)
        if len(self.deltas) > MAX_DELTAS:
            del self.deltas[: len(self.deltas) - MAX_DELTAS]

    @property
    def n(self) -> int:
        return len(self.deltas)

    def estimate(self) -> tuple[float, float]:
        """(median kW, standard error kW)."""
        if not self.deltas:
            return 0.0, float("inf")
        s = sorted(self.deltas)
        med = _median(s)
        mad = _median(sorted(abs(x - med) for x in s))
        se = 1.253 * 1.4826 * mad / (len(s) ** 0.5) if len(s) > 1 else float("inf")
        return med, se

    @property
    def status(self) -> str:
        if self.fixed_kw is not None:
            return FIXED
        if self.n == 0:
            return NOT_SWITCHING
        med, se = self.estimate()
        if self.n >= MIN_SAMPLES and med >= max(MIN_KW, 3 * se):
            return LEARNED
        if self.n >= TOO_SMALL_AFTER:
            return TOO_SMALL
        return LEARNING

    @property
    def kw(self) -> float:
        """Power to report while on (0 until learned)."""
        if self.fixed_kw is not None:
            return self.fixed_kw
        if self.status != LEARNED:
            return 0.0
        return max(self.estimate()[0], 0.0)

    def summary(self) -> dict:
        med, se = self.estimate()
        return {
            "status": self.status,
            "watts": round(self.kw * 1000) if self.kw else None,
            "measured_watts": round(med * 1000) if self.deltas else None,
            "uncertainty_watts": round(se * 1000) if self.n > 1 else None,
            "switches_measured": self.n,
        }


def _median(s):
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2


def measure(times, values, t0, tr: Transition, all_ts: list[float]) -> float | None:
    """Step in kW caused by one switch, or None if it can't be measured cleanly.

    times/values: the regular STEP_S grid (t0 = times[0]); all_ts: sorted
    times of every watched switch, used to skip overlapping ones.
    """
    b0, b1, a0, a1 = WINDOW_SLOW if tr.slow else WINDOW_DEFAULT
    # Skip if any other switch lands in the measurement span.
    # all_ts contains this switch itself; anything more means overlap
    # (including a mode change like heating -> cooling, listed twice).
    lo, hi = tr.ts + b0 - 30, tr.ts + a1 + 30
    if sum(1 for _ in _range_ts(all_ts, lo, hi)) > 1:
        return None
    before = _window(times, values, t0, tr.ts + b0, tr.ts + b1)
    after = _window(times, values, t0, tr.ts + a0, tr.ts + a1)
    if len(before) < 2 or len(after) < 2:
        return None
    bs, as_ = sorted(before), sorted(after)
    step = _median(as_) - _median(bs)
    if not tr.turned_on:
        step = -step
    if bs[-1] - bs[0] > SPREAD_KW:
        return None
    if as_[-1] - as_[0] > SPREAD_KW + 0.3 * abs(step):
        return None
    return step


def _window(times, values, t0, start, end):
    if not times:
        return []
    i0 = max(0, int((start - t0) // STEP_S) + 1)
    i1 = min(len(times) - 1, int((end - t0) // STEP_S))
    return [values[i] for i in range(i0, i1 + 1) if values[i] is not None]


def _range_ts(sorted_ts, lo, hi):
    import bisect

    i = bisect.bisect_left(sorted_ts, lo)
    while i < len(sorted_ts) and sorted_ts[i] <= hi:
        yield sorted_ts[i]
        i += 1


def learn(times, values, transitions: list[Transition], models: dict[str, DeviceModel]) -> int:
    """Add measurements for every transition that can be measured. Returns count."""
    if not times:
        return 0
    all_ts = sorted(t.ts for t in transitions)
    t0 = times[0]
    added = 0
    for tr in transitions:
        model = models.get(tr.key)
        if model is None:
            continue
        step = measure(times, values, t0, tr, all_ts)
        if step is not None:
            model.add(step)
            added += 1
    return added


def on_series(times, intervals) -> list[bool]:
    """Turn (start_ts, end_ts or None) on-intervals into a per-sample flag list."""
    flags = [False] * len(times)
    if not times:
        return flags
    t0 = times[0]
    last = len(times) - 1
    for start, end in intervals:
        i0 = max(0, int((start - t0) // STEP_S) + 1)
        i1 = last if end is None else min(last, int((end - t0) // STEP_S))
        for i in range(i0, i1 + 1):
            flags[i] = True
    return flags
