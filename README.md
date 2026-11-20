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

The rules were tuned on 10 days of 30-second data from this house
(Nov 9–19, 2026).

## Install (HACS)

1. In Home Assistant, open **HACS** → ⋮ (top right) → **Custom repositories**.
2. Repository: `https://github.com/enunes19/Load-Detective`, type
   **Integration** → **Add**.
3. Search HACS for **Load Detective** → **Download**.
4. Restart Home Assistant (Settings → System → power icon → Restart).

## Set up

1. **Settings → Devices & services → Add integration → Load Detective.**
2. Pick the home power sensor. For a Powerwall that's the home load sensor
   (here `sensor.shadblow_energy_load_power_2`).
3. Pick the devices to watch:
   - **Devices:** each gets its own sensors. Thermostats get three: heating,
     cooling and fan.
   - **Whole groups:** for example Lights, with one combined sensor.
   - **Known wattages:** one per line, like `media_player.tv: 110`.
   - **Days of history:** how far back to learn from at startup (default 7).

To change the list later, use the integration's **Configure** button.
Learning restarts from history when you do.

All sensors appear under one **Load Detective** device.

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

### Known wattages

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
on) are set in `Settings` in `classifier.py`, tuned to this house. They
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
- The water heater currently shows ECC (compressor not running), so only its
  4.4 kW elements are detected. Once the compressor works, its runs of a few
  hundred watts will mostly land in Other; a compressor rule can be added once
  there's data with it running.
- Range and toaster oven are told apart by time of day and length. A long
  midday oven bake and a dishwasher's heating cycle can both land in Range.
