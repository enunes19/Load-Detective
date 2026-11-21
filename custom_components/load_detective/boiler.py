"""Boiler helpers: combine heating zones, count heating hours, estimate oil.

Pure Python, no Home Assistant imports.

A multi-zone boiler counts as running whenever any zone is calling for heat.
Heating hours are kept per local day. Oil is estimated as heating hours x a
rate in gallons per heating hour, either entered by the user or worked out
from deliveries: the gallons delivered at one fill-up are what was burned
since the previous fill-up.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

BOILER_ID = "load_detective.boiler"
KWH_PER_GALLON = 40.6  # No. 2 heating oil, about 138,500 BTU per US gallon
MIN_COVERAGE = 0.5  # need heating hours for at least half of a delivery period


def merge_zones(zone_changes: dict[str, list]) -> list[tuple[float, str | None]]:
    """Combine each zone's (ts, mode) changes into boiler (ts, "on"/None) changes.

    A zone is calling when its mode is "heating". The first change of each
    zone is its state at the start of the history.
    """
    events = []
    for eid, changes in zone_changes.items():
        for ts, mode in changes:
            events.append((ts, eid, mode == "heating"))
    events.sort(key=lambda e: e[0])
    calling: set[str] = set()
    out: list[tuple[float, str | None]] = []
    last = object()
    for ts, eid, on in events:
        if on:
            calling.add(eid)
        else:
            calling.discard(eid)
        state = "on" if calling else None
        if out and out[-1][0] == ts:  # several zones reported at the same instant
            out.pop()
            last = out[-1][1] if out else object()
        if state != last:
            out.append((ts, state))
            last = state
    return out


def add_hours(daily: dict[str, float], intervals, start_ts: float, end_ts: float, tz) -> float:
    """Add on-time from (start, end|None) intervals within [start_ts, end_ts] to
    daily[local ISO date]. Returns hours added."""
    total = 0.0
    for a, b in intervals:
        b = end_ts if b is None else b
        a, b = max(a, start_ts), min(b, end_ts)
        while a < b:
            local = datetime.fromtimestamp(a, tz)
            midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tz)
            seg_end = min(b, midnight.timestamp())
            hrs = (seg_end - a) / 3600
            key = local.date().isoformat()
            daily[key] = daily.get(key, 0.0) + hrs
            total += hrs
            a = seg_end
    return total


def _days(after: date, through: date):
    d = after + timedelta(days=1)
    while d <= through:
        yield d
        d += timedelta(days=1)


def _tracked_fraction(d: date, since_ts: float, tz) -> float:
    """How much of local day d was tracked, given tracking started at since_ts."""
    start = datetime.combine(d, datetime.min.time(), tz).timestamp()
    end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tz).timestamp()
    if since_ts <= start:
        return 1.0
    if since_ts >= end:
        return 0.0
    return (end - since_ts) / (end - start)


def _hours_between(daily, after: date, through: date, since_ts: float, tz):
    """(hours, tracked days, total days) for days in (after, through].

    Tracked days are fractional: the day tracking started counts only for
    the part after it started.
    """
    hours = 0.0
    tracked = 0.0
    total = 0
    for d in _days(after, through):
        total += 1
        frac = _tracked_fraction(d, since_ts, tz)
        if frac > 0:
            tracked += frac
            hours += daily.get(d.isoformat(), 0.0)
    return hours, tracked, total


def calibrate(deliveries, daily, since_ts: float, tz):
    """Gallons per heating hour from consecutive deliveries.

    deliveries: list of (date, gallons). Each delivery's gallons are taken as
    oil burned since the previous delivery. A period only counts if heating
    hours were tracked for at least MIN_COVERAGE of it; the untracked part is
    assumed to have had the same average heating as the tracked part.
    Returns (rate or None, list of per-period details).
    """
    ds = sorted(deliveries)
    periods = []
    gal_sum = hrs_sum = 0.0
    for (d0, _g0), (d1, g1) in zip(ds, ds[1:]):
        hours, tracked, total = _hours_between(daily, d0, d1, since_ts, tz)
        coverage = tracked / total if total else 0.0
        used = coverage >= MIN_COVERAGE and hours > 0.5
        scaled = hours / coverage if coverage else 0.0
        periods.append(
            {
                "from": d0.isoformat(),
                "to": d1.isoformat(),
                "gallons": g1,
                "heating_hours": round(scaled, 1),
                "coverage": round(coverage, 2),
                "used": used,
            }
        )
        if used:
            gal_sum += g1
            hrs_sum += scaled
    rate = gal_sum / hrs_sum if hrs_sum > 0 else None
    return rate, periods


def since_last_delivery(deliveries, daily, since_ts: float, tz, today: date):
    """Heating hours since the last delivery, scaled up if tracking started later.

    Returns (hours or None, estimated: bool, last delivery date or None).
    """
    if not deliveries:
        return None, False, None
    last = max(deliveries)[0]
    if today <= last:
        return 0.0, False, last
    hours, tracked, total = _hours_between(daily, last, today, since_ts, tz)
    if tracked <= 0:
        return None, False, last
    if tracked < total - 1e-6:
        return hours * total / tracked, True, last
    return hours, False, last


def parse_deliveries(text: str) -> list[tuple[date, float]]:
    """'YYYY-MM-DD: gallons' per line. Raises ValueError on a bad line."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        day, sep, gallons = line.partition(":")
        if not sep:
            raise ValueError(line)
        g = float(gallons.strip().lower().removesuffix("gal").removesuffix("gallons").strip())
        if g <= 0:
            raise ValueError(line)
        out.append((date.fromisoformat(day.strip()), g))
    return sorted(out)
