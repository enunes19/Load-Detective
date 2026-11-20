#!/usr/bin/env python3
"""Run the Load Detective classifier over a Home Assistant history CSV export.

Usage:
    python3 backtest.py history.csv [--tz America/New_York] [--day 2026-11-19]
                                    [--events] [--out labeled.csv]

The CSV is the History panel download (entity_id,state,last_changed).
Values are assumed to be kW; add --watts if your sensor reports W.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CLASSIFIER = os.path.join(HERE, "..", "custom_components", "load_detective", "classifier.py")
_spec = importlib.util.spec_from_file_location("ld_classifier", CLASSIFIER)
C = importlib.util.module_from_spec(_spec)
sys.modules["ld_classifier"] = C
_spec.loader.exec_module(C)


def load(path, watts=False):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                v = float(row["state"])
            except (TypeError, ValueError):
                continue
            ts = datetime.fromisoformat(row["last_changed"].replace("Z", "+00:00")).timestamp()
            rows.append((ts, v / 1000 if watts else v))
    rows.sort()
    # Long-term statistics rows (hourly means) mixed in at the start of an
    # export would smear events, so keep only the dense raw section.
    dense_from = 0
    for i in range(1, len(rows)):
        if rows[i][0] - rows[i - 1][0] > 900:
            dense_from = i
    return rows[dense_from:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--watts", action="store_true")
    ap.add_argument("--day", help="only list events for this local date (YYYY-MM-DD)")
    ap.add_argument("--events", action="store_true", help="list every event")
    ap.add_argument("--out", help="write per-sample labels to this CSV")
    a = ap.parse_args()

    tz = ZoneInfo(a.tz)
    samples = load(a.csv, a.watts)
    times, p = C.resample(samples, samples[0][0], samples[-1][0])
    local = [datetime.fromtimestamp(t, timezone.utc).astimezone(tz) for t in times]
    hours = [d.hour for d in local]
    out, events = C.classify(p, hours)

    print(f"{len(samples)} readings, {local[0]:%Y-%m-%d %H:%M} to {local[-1]:%Y-%m-%d %H:%M} ({a.tz})\n")

    # Daily kWh by label
    daily = defaultdict(lambda: defaultdict(float))
    total = defaultdict(float)
    for i, d in enumerate(local):
        key = d.strftime("%a %m-%d")
        if p[i] is not None:
            daily[key]["measured"] += p[i] * C.STEP_S / 3600
        for lab in C.LABELS:
            daily[key][lab] += out[lab][i] * C.STEP_S / 3600
    cols = ["measured"] + C.LABELS
    short = {"measured": "total", C.WATER_HEATER: "wtr_htr", C.DRYER: "dryer", C.RANGE: "range",
             C.COFFEE: "coffee", C.TOASTER: "toaster", C.SMALL: "small", C.ALWAYS_ON: "alwaysOn",
             C.OTHER: "other"}
    print("Daily energy (kWh)")
    print(f"{'day':10}" + "".join(f"{short[c]:>9}" for c in cols))
    for key, row in daily.items():
        print(f"{key:10}" + "".join(f"{row[c]:9.1f}" for c in cols))
        for c in cols:
            total[c] += row[c]
    days = len(daily)
    print(f"{'avg/day':10}" + "".join(f"{total[c] / days:9.1f}" for c in cols))

    print("\nEvents" + (f" on {a.day}" if a.day else ""))
    for e in events:
        d = local[e["start"]]
        if a.day and d.strftime("%Y-%m-%d") != a.day:
            continue
        if not a.day and not a.events:
            continue
        mins = (e["end"] - e["start"] + 1) * C.STEP_S / 60
        print(f"  {d:%a %m-%d %H:%M}  {C.LABEL_NAMES[e['label']]:22} {mins:6.1f} min  "
              f"{e['kwh']:5.2f} kWh  peak +{e['peak_kw']:.1f} kW")
    if not a.day and not a.events:
        counts = defaultdict(int)
        for e in events:
            counts[e["label"]] += 1
        print("  " + ", ".join(f"{C.LABEL_NAMES[k]}: {v}" for k, v in counts.items()))
        print("  (use --events or --day YYYY-MM-DD to list them)")

    if a.out:
        with open(a.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "measured_kw"] + C.LABELS)
            for i, d in enumerate(local):
                w.writerow([d.isoformat(), p[i]] + [round(out[lab][i], 3) for lab in C.LABELS])
        print(f"\nWrote {a.out}")


if __name__ == "__main__":
    main()
