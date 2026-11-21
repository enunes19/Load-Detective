# Load Detective

Estimates what's running in your house from a single whole-home power sensor
(here, the Powerwall's home load), Sense-style but local.

It works in two layers:

1. **Watched devices.** Things Home Assistant already knows are on or off:
   the thermostat, dehumidifier, TVs, lights. Each one's wattage is learned
   from how much the home load jumps when it switches. While it's on, that
   power is credited to it.
2. **Pattern rules** for what's left, appliances Home Assistant can't see:

| Appliance | What it looks for |
|---|---|
| Water heater element | Flat step of about +4.4 kW that holds 5+ minutes |
| Dryer | 25+ minutes of big on/off swings (about +2.6 / +5.2 kW) every minute or two |
| Range / cooking | Bursts above about +0.9 kW lasting more than a few minutes |
| Coffee maker | Morning bursts (5–10 am) of about +1.5 kW, under 3.5 minutes |
| Toaster oven | Morning bursts of 3.5–20 minutes |
| Small appliance | Short bursts outside the morning (microwave, kettle, hair dryer) |
| Always on | Trailing low point of the signal (rack, fridge, standby loads) |
| Other | Everything left over |

The rules were tuned on 10 days of 30-second Powerwall data from one house,
so other homes' appliances may need different thresholds.

## Install (HACS)

1. In Home Assistant, open **HACS** → ⋮ (top right) → **Custom repositories**.
2. Repository: `https://github.com/enunes19/Load-Detective`, type
   **Integration** → **Add**.
3. Search HACS for **Load Detective** → **Download**.
4. Restart Home Assistant (Settings → System → power icon → Restart).

## Set up

1. **Settings → Devices & services → Add integration → Load Detective.**
2. Pick the home power sensor. For a Powerwall that's the home load sensor
   (the Tesla integration names it like `sensor.<site name>_load_power`).
3. Pick the devices to watch:
   - **Devices:** each gets its own sensors. Thermostats get three: heating,
     cooling and fan.
   - **Whole groups:** for example Lights, with one combined sensor.
   - **Known wattages:** one per line, like `media_player.tv: 110`.
   - **Days of history:** how far back to learn from at startup (default 7).

To change the list later, use the integration's **Configure** button.
Learning restarts from history when you do.

All sensors appear under one **Load Detective** device.

### Sensor names

Energy sensors are named for the appliance ("Dryer", "Range / cooking"), so
that's what the Energy dashboard shows. Power sensors add "power". Watched
devices use their own Home Assistant name, with the thermostat split into
"heating", "cooling" and "fan". Rename the device in Home Assistant and its
sensors follow. To override any single name, open the sensor → ⚙ → Name.

## What you get

- **Pattern appliances:** a power (kW, live) and energy (kWh, running total)
  sensor for each row in the table above.
- **Each watched device:** a power and energy pair.
- **Each group:** one combined pair, e.g. "All lights."
- **Activity:** what's running now, with a `recent_events` attribute
  listing the last 15 pattern events.
- **Learned devices:** how many devices have a wattage. Its `devices`
  attribute shows, for each one, the learned watts, the uncertainty, how
  many switches were measured, and a status:

| Status | Meaning |
|---|---|
| learned | Wattage known; it's being credited |
| learning | Not enough clean switches yet |
| too small to measure | 40+ switches and still lost in the noise (most single LED bulbs) |
| not switching | Never seen turning on and off (speakers that sit at "idle") |
| fixed | You gave it a wattage |

A device only gets credited once it's learned or fixed. Until then its usage
stays in the pattern layer or Other.

### Boiler and oil

For an oil (or gas) boiler with several heating zones, put all the zone
thermostats under **Boiler zones** in the setup screen, not under Devices.
The boiler then counts as one appliance that runs whenever any zone calls
for heat. A thermostat listed in both places is only counted in the boiler.

You get:

| Sensor | What it is |
|---|---|
| Boiler / Boiler power | Electricity for the burner motor and circulators |
| Boiler heating hours | Running total of hours any zone called for heat |
| Boiler oil used | Running total of gallons burned (estimate) |
| Boiler oil energy | The same oil in kWh (1 gal ≈ 40.6 kWh), for the Energy dashboard's Gas slot |
| Boiler oil since last delivery | Gallons burned since your last fill-up |
| Boiler oil remaining | Tank size minus that (only if you enter a tank size) |
| Boiler oil per heating hour | The rate behind the estimate, with how it was worked out |

**How the oil estimate works.** Oil burned = heating hours × gallons per
heating hour. Enter your deliveries in the setup screen, one per line:

```
2026-10-14: 151.2
2026-11-19: 138.7
```

A fill-up's gallons are what was burned since the previous fill-up. Once two
deliveries bracket a stretch where heating hours were tracked (at least half
of it), the rate is worked out from them. Each new delivery you add refines
it. Until then, the oil sensors stay empty, unless you enter a rate yourself
(for example nozzle size × about 0.5–0.7 for the share of each call the
burner actually fires).

Heating hours are saved, survive restarts, and gaps while Home Assistant was
off are filled in from history. Each thermostat must report `hvac_action`
(heating / idle).

**Electricity.** The burner cycles on and off inside each call for heat, so
the boiler's wattage is learned from how much the load drops when the last
zone stops calling, averaged over many calls. That gives the average draw
over a call rather than the peak.



For anything slow to learn (a TV used once a day) or too small to measure
(individual bulbs), enter it in the setup screen's wattage box, one per line:

```
media_player.living_room_tv: 110
light.porch: 9
climate.thermostat|cooling: 3000
```

The last line shows the mode-specific form for thermostats.

### Energy dashboard

Settings → Dashboards → Energy → Individual devices → add the `..._energy`
sensors you want. Skip **Other**: Home Assistant already shows what's left as
"untracked." History starts from install.

## How live updates work

- Every 30 seconds it classifies the last 6 hours.
- Each switch of a watched device is measured about 7 minutes later, once
  the "after" reading is in, so learning keeps improving on its own.
- Switches that happen within a few minutes of another watched switch (a
  lighting scene, say) are skipped, since they can't be separated.
- A new water heater run shows after it has held for about 5 minutes. Until
  then it is reported as Other, so the display doesn't flash a wrong label.
- Energy sensors count each sample once it's 15 minutes old, when every rule
  has seen enough to be sure. Totals lag 15 minutes but don't get revised.

## Tuning the pattern rules

The thresholds (water heater step size, dryer length, morning window and so
on) are set in `Settings` in `classifier.py`, tuned to the house they were built on. They
aren't in the setup screen yet.

## Checking the rules against your own history

`tools/backtest.py` runs the pattern rules on a History panel CSV export of
the home load sensor (it doesn't include watched devices):

```bash
python3 tools/backtest.py history.csv                   # daily kWh per appliance
python3 tools/backtest.py history.csv --day 2026-11-19  # that day's events
python3 tools/backtest.py history.csv --events          # every event
python3 tools/backtest.py history.csv --out labeled.csv # per-sample output
```

## Known limits

- The Powerwall reading wobbles by about ±50 W, so a device needs to switch
  many times to be learned. Big loads (furnace blower, AC, dehumidifier)
  learn within a day or two; 30–50 W devices take a few weeks; single LED
  bulbs usually never do. Use `watts:` for those.
- Dimmable lights are treated as full power when on.
- Lights changed together by a scene are skipped for learning. Set `watts:`
  if your lights mostly change in scenes.
- New entities in a group are picked up at the next restart or reconfigure.
- The thermostat needs an `hvac_action` attribute (most do). Without it,
  heating and cooling can't be told apart and nothing is learned for it.
- For a heat pump water heater, only its 4.4 kW elements are detected. Its
  compressor runs (a few hundred watts) mostly land in Other; a compressor
  rule needs data with the compressor running.
- Range and toaster oven are told apart by time of day and length. A long
  midday oven bake and a dishwasher's heating cycle can both land in Range.
