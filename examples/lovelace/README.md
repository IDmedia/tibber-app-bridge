# Lovelace examples

A showcase view for Tibber App Bridge, built from four custom cards (all on HACS):

* [button-card](https://github.com/custom-cards/button-card) ≥ 4.2 — tiles, stats, phase bars, price strip, comparison bars
* [apexcharts-card](https://github.com/RomRider/apexcharts-card) — power, history, prices, Grid Rewards, EV charging, breakdown donut
* [flex-table-card](https://github.com/custom-cards/flex-table-card) — fuse overloads, highest hours, prices, breakdown, rewards per year
* [vertical-stack-in-card](https://github.com/ofekashery/vertical-stack-in-card) — cards made of several parts (a chart with its stats, a gauge with its text) form one box

Everything else is stock Home Assistant (sections view, heading, markdown,
grid, entities, statistics-graph). No helpers are needed.

## Layout

```
ui-lovelace.yaml               example: loads the templates and includes the view
lovelace/
  tibber.yaml                  showcase view (path tibber) with every card
  button_card/*.yaml           one button-card template per file
  components/*.yaml            one card per file, included by tibber.yaml
```

`tibber.yaml` is a single view, included from `ui-lovelace.yaml` like any other
view file. It has one titled section per topic and uses each component once;
copy the components you like into your own views with
`- !include components/<name>.yaml`. Everything is prefixed `tibber_`, so the
files sit next to your own views, templates and components without clashes.

### Templates (`button_card/`)

| Template | Used for |
|---|---|
| `tibber_base` | Shared styling, accent colours, icon sizing and JS helpers (all others build on it) |
| `tibber_title` | Title + subtitle inside a stack |
| `tibber_tile`, `tibber_power_tile` | App-style tiles (the power tile has the load arc) |
| `tibber_stat` | Label, big value, secondary lines |
| `tibber_row` | Analysis row with icon box (Used / Your average price) |
| `tibber_phase` | Vertical phase-load bar |
| `tibber_price_strip` | Today's prices strip with "now" marker |
| `tibber_compare_bars`, `tibber_rating` | Compared with similar homes |
| `tibber_average_price` | "Your average price" with comparison columns |
| `tibber_grid_status`, `tibber_vehicle` | Grid Rewards status banner, vehicle row |

### Components (`components/`)

| Section | Components | What it tells you |
|---|---|---|
| Overview | `tibber_todays_prices`, `tibber_tile_*`, `tibber_cheapest_window` | Price now and through the day, the app's tiles, the cheapest 3 hours ahead |
| Right now | `tibber_pulse`, `tibber_this_hour`, `tibber_power_24h_chart` | Power now, last hour, today's kWh/cost/max/min; whether this hour becomes a new capacity peak |
| Phase load | `tibber_phase_load`, `tibber_phase_currents_chart`, `tibber_fuse_overloads`, `tibber_pulse_status` | Load per phase vs. main fuse, overload history, Pulse health |
| This month | `tibber_row_used`, `tibber_row_average_price`, `tibber_compared_with_others`, `tibber_tile_car1/2` | Usage and cost so far, average price, comparison with similar homes |
| History | `tibber_history_today`, `tibber_history_month`, `tibber_history_year` | Hourly kWh + price today, daily cost + price this month, this year vs. last year |
| Long-term history | `tibber_statistics_per_month`, `tibber_statistics_per_day`, `tibber_year_over_year` | Imported hourly statistics, year over year |
| Prices | `tibber_prices_today_chart`, `tibber_prices_tomorrow_chart`, `tibber_price_stats_today`, `tibber_prices_table` | Cheap/normal/expensive hours today and tomorrow, the next 12 hours as a table |
| Your average price | `tibber_average_price_month`, `tibber_average_price_year`, `tibber_average_price_by_type` | Your kr/kWh against low/average/high homes, per usage type |
| Compared with others | `tibber_comparison_month`, `tibber_comparison_year`, `tibber_breakdown_chart`, `tibber_breakdown_table` | Efficiency rating, what used the most |
| Capacity tariff | `tibber_capacity_stats`, `tibber_capacity_level`, `tibber_highest_hours`, `tibber_hourly_vs_limit_chart` | Top-3 average, step, the threshold for a new peak and what the next step would take; a gauge with all steps and (optionally) their prices |
| Load balancing | `tibber_load_balancing` | Main fuse, hourly limit, charger limit, overloads |
| Grid Rewards | `tibber_grid_rewards_*`, `tibber_charging_price` | Earnings and readiness to contribute |
| Your devices | `tibber_vehicle_car1/2`, `tibber_car_batteries` | Plugged in, smart charging, battery |
| EV charging | `tibber_ev_charging_now`, `tibber_charging_plan`, `tibber_charger_month`, `tibber_charging_per_day_chart` | Charger status and the session and plan of whichever car is connected |

## Setup

1. Install the three cards.
2. In `lovelace/tibber.yaml` and `lovelace/components/`, search and replace the
   placeholders with your own entity id prefixes. None of them contains another,
   so the order doesn't matter:

   | Placeholder | Replace with | Example entity |
   |---|---|---|
   | `YOURHOME` | home device prefix | `sensor.YOURHOME_current_price` |
   | `YOURPULSE` | Pulse device prefix (usually `<home>_tibber_pulse`) | `sensor.YOURPULSE_power` |
   | `YOURCHARGER` | charger device prefix (e.g. `<home>_easee_charger`) | `sensor.YOURCHARGER_charger_status` |
   | `YOURCAR1`, `YOURCAR2` | vehicle device prefixes (brand + model, e.g. `nissan_leaf`) | `sensor.YOURCAR1_battery_level` |
   | `YOURSTATID` | the 12 characters after `consumption_` in the statistic id | `tibber_app_bridge:consumption_YOURSTATID` |
   | `YOURTIERPRICES` | optional: your own sensor (e.g. a scrape of your grid company's price page) with `ranges` (`0-2 kWh/h,2-5 kWh/h,...`) and `prices` (`165,270,...` kr/month) attributes. Without it `tibber_capacity_level` lists the integration's steps without prices | `sensor.YOURTIERPRICES` |

   The prefix is the entity id without the sensor name (*Settings → Devices &
   services → Tibber App Bridge*). The statistic id isn't a sensor: it's a
   long-term statistic. You'll find it as the `statistic_id_consumption`
   attribute of the *Consumption today* / *Consumption this month* sensors, or
   under *Developer tools → Statistics* (search "Tibber App"). Chart axes say
   `kr` and `øre`; change them for other currencies (e.g. `€` / `ct`). Remove
   the lines for cards you don't need (e.g. a second car) from `tibber.yaml`.
3. Copy `tibber.yaml`, `button_card/` and `components/` into your `lovelace/`
   folder, and in `ui-lovelace.yaml` load the templates and include the view
   (see [`../ui-lovelace.yaml`](../ui-lovelace.yaml)):

   ```yaml
   button_card_templates: !include_dir_merge_named lovelace/button_card/

   views:
     - !include lovelace/power.yaml   # your own views
     - !include lovelace/tibber.yaml
   ```

   If you already load templates from another folder, keep yours and copy the
   `tibber_*` templates into it: `!include_dir_merge_named` merges every file.

Using a UI dashboard instead? The UI editor can't follow `!include`: paste the
templates under a top-level `button_card_templates:` key in the raw
configuration editor, and paste the component contents where the `!include`
lines are.

## Theming

Text and backgrounds come from your theme, so everything works in light and dark
mode. The Tibber accents can be overridden in a theme:

```yaml
tibber-teal-color: "#23b8a6"
tibber-cyan-color: "#24c3d6"
tibber-blue-color: "#145c78"
tibber-orange-color: "#f08a3c"
tibber-peach-color: "#e9b99a"
```

## Notes

* The power and phase charts use the recorder history of the Pulse sensors, so
  they fill in over the first day.
* "Highest/Lowest power today" are Tibber's own values from the live Pulse feed;
  only while that feed is down do they fall back to minute averages.
* The history charts show the current day/month/year from state attributes. For
  older periods use the `statistics-graph` cards (or the Energy dashboard) backed
  by the imported hourly statistics.
