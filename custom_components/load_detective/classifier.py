"""Rule-based load disaggregation for a single whole-home power sensor.

Pure Python with no Home Assistant imports, so the same code runs inside the
integration and in the offline backtest tool.

Input is a regular grid of whole-home power readings in kW (one every
STEP_S seconds, None where data is missing). Output is a per-sample power
estimate in kW for each appliance label, plus a list of detected events.

The rules were tuned on 30-second Tesla Powerwall "home load" data:

* Water heater element: a sustained step of about +4.4 kW in the *floor*
  of the signal (its low envelope), which keeps it separate from loads that
  cycle on top of it.
* Dryer: 25+ minutes of large (about 2.6 and 5.2 kW) on/off swings every
  minute or two, reaching at least about 4.3 kW above baseline.
* Coffee maker / toaster oven: short morning bursts of about +1.5 kW,
  separated by how long they last.
* Range: other bursts above about 0.9 kW lasting more than a few minutes.
* Always-on: a trailing low percentile of the signal.
"""
from __future__ import annotations

from dataclasses import dataclass

STEP_S = 30
SAMPLES_PER_MIN = 60 // STEP_S

WATER_HEATER = "water_heater"
DRYER = "dryer"
RANGE = "range"
COFFEE = "coffee_maker"
TOASTER = "toaster_oven"
SMALL = "small_appliance"
ALWAYS_ON = "always_on"
OTHER = "other"

LABELS = [WATER_HEATER, DRYER, RANGE, COFFEE, TOASTER, SMALL, ALWAYS_ON, OTHER]

LABEL_NAMES = {
    WATER_HEATER: "Water heater element",
    DRYER: "Dryer",
    RANGE: "Range / cooking",
    COFFEE: "Coffee maker",
    TOASTER: "Toaster oven",
    SMALL: "Small appliance",
    ALWAYS_ON: "Always on",
    OTHER: "Other",
}


@dataclass
class Settings:
    """Tunable thresholds. Defaults fit the data this was built from."""

    # Water heater element (sustained step in the signal floor)
    wh_min_kw: float = 3.6
    wh_max_kw: float = 5.4
    wh_min_minutes: float = 5.0
    # Dryer (long run of large on/off swings)
    dryer_edge_kw: float = 1.8
    dryer_min_peak_kw: float = 4.3
    dryer_min_edges: int = 4
    dryer_window_minutes: float = 10.0
    dryer_min_minutes: float = 25.0
    # Short bursts (coffee maker, toaster oven, range, other small loads)
    burst_on_kw: float = 0.9
    burst_merge_minutes: float = 2.0
    burst_min_minutes: float = 1.0
    coffee_max_minutes: float = 3.5
    toaster_max_minutes: float = 20.0
    morning_start_hour: int = 5
    morning_end_hour: int = 10
    # Always-on baseline
    baseline_hours: float = 3.0
    baseline_percentile: float = 0.05


def _pct(values, q):
    """Percentile (nearest rank) ignoring None. Returns None if empty."""
    s = sorted(v for v in values if v is not None)
    if not s:
        return None
    k = int(round(q * (len(s) - 1)))
    return s[min(len(s) - 1, max(0, k))]


def resample(samples, start_ts, end_ts, max_hold_s=600):
    """Turn change-based readings into a regular STEP_S grid.

    samples: list of (epoch_seconds, kw) sorted by time. Each reading holds
    until the next one (Home Assistant state semantics). A reading older than
    max_hold_s becomes None (gap).
    Returns (grid_times, values).
    """
    t0 = int(start_ts // STEP_S) * STEP_S
    times, vals = [], []
    j = -1
    n = len(samples)
    t = t0
    while t <= end_ts:
        while j + 1 < n and samples[j + 1][0] <= t:
            j += 1
        if j >= 0 and t - samples[j][0] <= max_hold_s:
            vals.append(samples[j][1])
        else:
            vals.append(None)
        times.append(t)
        t += STEP_S
    return times, vals


def classify(p, hours, s: Settings | None = None, known=None):
    """Split whole-home power into appliance estimates.

    p: list of kW values on the STEP_S grid (None = missing).
    hours: local hour of day (0-23) for each sample.
    known: optional list of kW already explained by watched devices (learned
    from Home Assistant states); it is subtracted before the rules run.
    Returns (outputs, events) where outputs maps label -> list of kW and
    events is a list of dicts with label, start, end (indices), kwh, peak_kw.
    """
    s = s or Settings()
    n = len(p)
    out = {k: [0.0] * n for k in LABELS}
    events = []
    if n == 0:
        return out, events

    # Treat near-zero readings as dropouts (seen once in the source data).
    pv = [None if v is None or v < 0.05 else float(v) for v in p]
    if known is not None:
        pv = [None if v is None else max(v - (known[i] or 0.0), 0.05) for i, v in enumerate(pv)]

    # --- Always-on baseline: trailing low percentile of a 5-sample median.
    med5 = [_pct(pv[max(0, i - 2): i + 3], 0.5) for i in range(n)]
    win = max(1, int(s.baseline_hours * 60 * SAMPLES_PER_MIN))
    base = [None] * n
    for i in range(n):
        base[i] = _pct(med5[max(0, i - win + 1): i + 1], s.baseline_percentile)

    # --- Water heater element: sustained step in the low envelope.
    wh = [0.0] * n
    # Near the end of the data there is not yet enough lookahead to decide.
    # A step that looks like the water heater there is "pending": reported as
    # other until it can be confirmed or ruled out.
    pending = [False] * n
    lookahead = 10
    i = 1
    on = False
    start = 0
    step = 0.0
    floor_ref = 0.0
    while i < n:
        v = pv[i]
        if v is None:
            if on:
                _close_wh(wh, start, i - 1, s, events, step)
                on = False
            i += 1
            continue
        if not on:
            pre = _pct(pv[max(0, i - 8): i], 0.2)
            post_vals = [x for x in pv[i + 1: i + 1 + lookahead] if x is not None]
            if pre is not None and len(post_vals) >= 3:
                post = _pct(post_vals, 0.2)
                d = post - pre
                looks_like = s.wh_min_kw <= d <= s.wh_max_kw and v - pre >= 1.5
                if looks_like and i + lookahead >= n:
                    for k in range(i, n):
                        pending[k] = True
                    break
                if looks_like:
                    on = True
                    start = i
                    step = min(max(d, 3.8), 5.0)
                    floor_ref = post
        else:
            recent = _pct(pv[max(start, i - 6): i], 0.2)
            if recent is not None:
                floor_ref = recent
            ahead = [x for x in pv[i: i + 6] if x is not None]
            drop = floor_ref - 0.6 * step
            if v < drop and ahead and _pct(ahead, 0.5) < drop:
                _close_wh(wh, start, i - 1, s, events, step)
                on = False
                continue  # re-check this sample for a new onset
        if on:
            wh[i] = step
        i += 1
    # A run still in progress at the end of the data only counts once it has
    # lasted long enough; until then it is pending too.
    if on:
        if (n - start) / SAMPLES_PER_MIN >= s.wh_min_minutes:
            _close_wh(wh, start, n - 1, s, events, step)
        else:
            for k in range(start, n):
                wh[k] = 0.0
                pending[k] = True

    # Residual after baseline and water heater.
    r = [None] * n
    for i in range(n):
        if pv[i] is None or base[i] is None:
            continue
        x = pv[i] - base[i]
        w = min(wh[i], max(x, 0.0))  # never assign more than is there
        wh[i] = w
        r[i] = x - w

    # --- Dryer: long runs of big on/off swings.
    edge = [0] * n
    for i in range(1, n):
        if r[i] is not None and r[i - 1] is not None and abs(r[i] - r[i - 1]) >= s.dryer_edge_kw:
            edge[i] = 1
    pref = [0] * (n + 1)
    for i in range(n):
        pref[i + 1] = pref[i] + edge[i]
    hw = int(s.dryer_window_minutes * SAMPLES_PER_MIN)
    cand = [False] * n
    for i in range(n):
        a, b = max(0, i - hw), min(n, i + hw + 1)
        if pref[b] - pref[a] >= s.dryer_min_edges:
            peak = max((x for x in r[a:b] if x is not None), default=0.0)
            cand[i] = peak >= s.dryer_min_peak_kw
    dryer_mask = [False] * n
    for a, b in _runs(cand, merge_gap=6):
        # trim to the actual activity inside the window
        act = [k for k in range(a, b + 1) if r[k] is not None and r[k] > 1.0]
        if not act:
            continue
        a2, b2 = act[0], act[-1]
        if (b2 - a2 + 1) / SAMPLES_PER_MIN < s.dryer_min_minutes:
            continue
        kwh = 0.0
        peak = 0.0
        for k in range(a2, b2 + 1):
            dryer_mask[k] = True
            val = max(r[k] or 0.0, 0.0)
            out[DRYER][k] = val
            kwh += val * STEP_S / 3600
            peak = max(peak, val)
        events.append(dict(label=DRYER, start=a2, end=b2, kwh=kwh, peak_kw=peak))

    # --- Bursts on what is left.
    r2 = [None if (r[i] is None or dryer_mask[i]) else r[i] for i in range(n)]
    hot = [x is not None and x > s.burst_on_kw and not pending[i] for i, x in enumerate(r2)]
    assigned = [False] * n
    for a, b in _runs(hot, merge_gap=int(s.burst_merge_minutes * SAMPLES_PER_MIN)):
        dur_min = (b - a + 1) / SAMPLES_PER_MIN
        if dur_min < s.burst_min_minutes:
            continue  # a single spike; leave it in "other"
        h = hours[a]
        morning = s.morning_start_hour <= h < s.morning_end_hour
        if dur_min <= s.coffee_max_minutes:
            label = COFFEE if morning else SMALL
        elif morning and dur_min <= s.toaster_max_minutes:
            label = TOASTER
        else:
            label = RANGE
        kwh = 0.0
        peak = 0.0
        for k in range(a, b + 1):
            val = max(r2[k] or 0.0, 0.0)
            out[label][k] = val
            assigned[k] = True
            kwh += val * STEP_S / 3600
            peak = max(peak, val)
        events.append(dict(label=label, start=a, end=b, kwh=kwh, peak_kw=peak))

    for i in range(n):
        out[WATER_HEATER][i] = wh[i]
        if base[i] is not None and pv[i] is not None:
            out[ALWAYS_ON][i] = min(base[i], pv[i])
        if r2[i] is not None and not assigned[i]:
            out[OTHER][i] = max(r2[i], 0.0)

    # Water heater energy after clipping to what was actually measured.
    for e in events:
        if e["label"] == WATER_HEATER:
            e["kwh"] = sum(out[WATER_HEATER][e["start"]: e["end"] + 1]) * STEP_S / 3600

    events.sort(key=lambda e: e["start"])
    return out, events


def _close_wh(wh, a, b, s, events, step):
    """Finish a water heater run; drop it if it is too short to be real."""
    dur_min = (b - a + 1) / SAMPLES_PER_MIN
    if dur_min < s.wh_min_minutes:
        for k in range(a, b + 1):
            wh[k] = 0.0
        return
    for k in range(a, b + 1):
        wh[k] = step
    events.append(
        dict(
            label=WATER_HEATER,
            start=a,
            end=b,
            kwh=step * (b - a + 1) * STEP_S / 3600,
            peak_kw=step,
        )
    )


def _runs(mask, merge_gap=0):
    """Index ranges (a, b) where mask is True, merging gaps <= merge_gap."""
    runs = []
    a = None
    last = None
    for i, m in enumerate(mask):
        if not m:
            continue
        if a is None:
            a = last = i
        elif i - last - 1 <= merge_gap:
            last = i
        else:
            runs.append((a, last))
            a = last = i
    if a is not None:
        runs.append((a, last))
    return runs
