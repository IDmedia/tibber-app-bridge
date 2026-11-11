# Tibber App Bridge (unofficial)

A Home Assistant integration that reads data from the **GraphQL API the Tibber
mobile app uses** (`app.tibber.com/v4/gql`), not the public developer API. That
gives access to what the app shows: Pulse live data and phases, today's prices
(incl. grid tariff), monthly/yearly usage with forecasts, the capacity-tariff
"average of the 3 highest hours", your average price vs. others, comparison with
similar homes, consumption breakdown, Grid Rewards, EV charging and vehicles.

> **Unofficial.** This API is private and can change with any app release.
> Not affiliated with Tibber.

## Read-only by design

The integration can't change anything in your Tibber account:

* It only sends GraphQL `query` (HTTP) and `subscription` (websocket) operations.
  `queries.operation_name()` runs for every request and refuses anything else,
  including any document containing `mutation`; `tests/test_readonly.py` checks
  every document in the code.
* There are no services, buttons, switches, numbers or selects — only sensors
  and binary sensors.
* The only non-GraphQL calls are the app's own login (`POST /login.credentials`)
  and session refresh (`PUT /auth-sessions/external`), which return tokens.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS**, click the **⋮** menu in the top right corner
   and choose **Custom repositories**.
2. Paste this URL into **Repository**, choose **Integration** as the type and
   click **Add**:

   ```text
   https://github.com/IDmedia/tibber-app-bridge
   ```

3. Search for **Tibber App Bridge** in HACS, open it and click **Download**.
4. Restart Home Assistant.

### Manual

1. Download the [latest version](https://github.com/IDmedia/tibber-app-bridge/archive/refs/heads/main.zip)
   and copy `custom_components/tibber_app_bridge` to `<config>/custom_components/`.
2. Restart Home Assistant.

### Set up

**Settings → Devices & services → Add integration → Tibber App Bridge**, and log
in with your Tibber app email and password. If the account has several homes,
pick one (add the integration again for each extra home).

Requires Home Assistant 2025.11 or newer.

## Security

* **The password is not stored.** It's only used to log in. The config entry
  keeps the email and the rotating refresh token, which is renewed
  automatically (the access token lasts ~3 hours). If Tibber ever revokes the
  session you get a normal *re-authenticate* prompt, which checks that you log
  in to the account that owns the configured home.
* Tokens, the email and the password never appear in logs.
* Diagnostics redact the email, tokens, address, coordinates, meter numbers and
  the Pulse's Wi-Fi details (SSID, IP, MAC).
* All traffic goes to `app.tibber.com` over HTTPS with certificate checks, using
  Home Assistant's shared HTTP session.

## Works for any Tibber home

Everything is discovered per home, and entities are only created for what the
home has:

| Feature | Created when |
|---|---|
| Prices, usage, comparison, average price, breakdown | Always (unknown where Tibber has no data) |
| Pulse live data, power control, fuse overloads | The home has a Pulse |
| Per-phase current/voltage/load | The Pulse reports phase currents |
| Capacity tariff (top-3 hours, step) | Norwegian home, or tariff steps entered in the options |
| Grid Rewards | The home has Grid Rewards history, a charger or a vehicle |
| Charger / vehicle devices | Linked to this home in the app |

Currency comes from Tibber (falling back to Home Assistant's currency), API
texts follow Home Assistant's language (Norwegian, Swedish, German, Dutch,
otherwise English) and calendar periods use the home's time zone. Entity names
are translated (English and Norwegian included).

## Options

| Option | Default | |
|---|---|---|
| Live Pulse fallback poll interval | 10 s | Only used while no Pulse push has arrived for 45 s (5–300 s). |
| Days of hourly history to import | 60 | One-time backfill into long-term statistics. |
| Capacity tariff steps | Norway: `2,5,10,15,20,25,50,75,100`, else empty | Upper bounds (kWh/h) for the *Capacity step* sensor. |
| Include grid price in prices | on | Same as the grid price toggle in the app. |

## Where the data comes from

| Data | How | Interval |
|---|---|---|
| Pulse live (power, phases, today's totals, max/min, this hour) | `pulseSubscription` push | ~10 s (poll fallback) |
| Vehicles (charging, battery, session, plan) | `vehicleStateSubscription` push | ~20 s |
| Chargers (status, power, limit) | `vehicleChargerSubscription` | every 30 s |
| Grid Rewards status | `gridRewardStateTransitionsSubscription` push + snapshot | on change / 15 min |
| Pulse status, today's hourly data, power control | queries | 5 min |
| Prices | `PriceRatingQuery` | 15 min (5 min after 13:00 until tomorrow's prices arrive) |
| This month, chargers' month | queries | 15 min |
| This/last year, comparisons, average price | queries | 1 h |
| Hourly history → statistics | queries | 1 h (last 3 days) |
| Vehicles list, last month, breakdown, Grid Rewards per year | queries | 6 h |

All sockets reconnect with backoff (5 s → 5 min) and get a fresh token after a
401 or a 4401 close.

## Storing data: trade-offs

1. **Entity states (recorder history)** for current values (power, prices,
   totals). Sensors with a `state_class` also get long-term statistics.
2. **External long-term statistics** — `tibber_app_bridge:consumption_<id>` and
   `tibber_app_bridge:cost_<id>` — hourly kWh and cost from the meter: backfilled
   once, then the last 3 days are re-imported every hour because meter values
   arrive late. Use them in the **Energy dashboard** or a `statistics-graph` card.
3. **State attributes** for chart series (today's hours, this month's days,
   this/last year's months, prices, overloads, top-3 peaks, charging plan).
   **All attributes are excluded from the recorder**, so they don't grow the
   database.

Older day/month/year "pages" aren't stored separately because they can be
derived from the hourly statistics. Daily peak hours for the capacity tariff are
cached in `.storage/tibber_app_bridge.<home_id>` (~14 months).

## Capacity tariff sensor

`sensor.<home>_average_of_3_highest_hours` is **Tibber's own value** from the
monthly consumption response ("Average of the 3 highest hours: 7.88 kWh"). If
Tibber doesn't provide it, the integration calculates it (highest hour per day,
average of the top 3 days); attributes show `calculated_average`, the `peaks`,
the `step` with its `step_lower`/`step_upper` bounds, and all `steps` (upper
bounds from the options).

Replacing `tibber_data`: remove it, then rename the new entity to your old id
(e.g. `sensor.average_of_3_highest_hourly_consumption_<home>`) so templates and
history keep working. The sensor also has `tibber_data`'s `peak_consumptions`
and `peak_consumption_dates` attributes (same shape, highest first), so
templates that read them don't need changes.

`sensor.<home>_peak_threshold_this_hour` says how much the current hour can use
before it becomes one of the month's 3 peaks (it has to beat today's own peak
and the lowest of the other days in the top 3). Its `to_next_step` attribute is
the consumption in one hour that would push the average into the next tariff
step. Compare it with the Pulse's *Estimated consumption this hour*.

## Entities

Per home, created only for what the home has (see the table above):

* **Home** — Current/Next price, Price level, Today/Tomorrow lowest/highest/average
  price, *Cheapest 3 hours* (start; end and average in attributes), Consumption
  and cost today/this month/last month/this year/last year, estimates for this
  month/year, Average of 3 highest hours, Capacity step, Peak threshold this
  hour, Average price this month/year, Compared with similar homes this
  month/year, Consumption breakdown last month, Grid Rewards status/this
  month/this year/total, Average charging price.
* **Pulse** — Power, Power production (disabled), Current/Voltage/Phase load
  L1–L3, Consumption and cost today (live), Consumption this hour, Estimated
  consumption this hour, Average power today, Highest/Lowest power today,
  Peak control state/status, Hourly consumption limit, Main fuse size, Last fuse
  overload, Fuse overloads last 30 days, Wi-Fi signal, Measurements per minute;
  binary: Pulse connected, Pulse problem, Load balancing, Peak control.
* **Charger** — Charging this month (kWh/cost/price), Charger status, Charging
  status, Charge power, Max charging current, Last seen; binary: Online, Load
  balancing.
* **Vehicles** — named brand + model (e.g. *Nissan Leaf*, entity ids
  `sensor.nissan_leaf_…`): Battery level, Battery capacity, Charging status,
  Charge state (with the charging plan), Charge power, Target charge level,
  Departure, Charged this session, Session cost, Session average price, Grid
  Rewards status; binary: Charging, Plugged in, Smart charging.

The *Consumption today* and *Consumption this month* sensors carry the
long-term statistic ids as `statistic_id_consumption` / `statistic_id_cost`
attributes (use them in the Energy dashboard or a `statistics-graph` card).

## Logging and troubleshooting

* When a data group or subscription starts failing, it logs **one warning** and
  then retries quietly; when it works again it logs **one info** line.
  Unexpected errors are logged with a traceback. If nothing at all can be
  fetched, the entities become unavailable.
* Debug logging:

  ```yaml
  logger:
    logs:
      custom_components.tibber_app_bridge: debug
  ```

* **Settings → Devices & services → Tibber App Bridge → ⋮ → Download
  diagnostics** gives a redacted snapshot to attach to issues.

## Code structure

```
custom_components/tibber_app_bridge/
  __init__.py       setup/unload, entry migration
  config_flow.py    login, home selection, reauth, options
  api.py            HTTP client: login, token refresh, GraphQL queries
  websocket.py      graphql-transport-ws subscriptions (reconnect, backoff)
  queries.py        all GraphQL documents + the read-only guard
  parsers.py        pure functions: API responses -> flat dicts (no HA imports)
  coordinator.py    main coordinator: discovery, polled data groups, statistics,
                    starting the subscriptions
  live.py           live Pulse coordinator (push + poll fallback)
  devices.py        pushed vehicle/charger state
  statistics.py     external long-term statistics import
  entity.py         base entity, devices, unit/period helpers
  sensor.py         sensor descriptions grouped by data source
  binary_sensor.py  binary sensor descriptions
  diagnostics.py    redacted diagnostics
  strings.json, translations/, icons.json
  brand/            integration icon (HA 2026.3+ shows it without the brands repo)
tests/              pytest suite (no Home Assistant needed)
examples/           ui-lovelace.yaml example, lovelace/tibber.yaml showcase view,
                    lovelace/button_card/ templates, lovelace/components/ cards
scripts/            translation generator
```

## Development

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install pytest pytest-asyncio aiohttp ruff
```

```bash
.venv/bin/pytest
```

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests cover the read-only guard, the parsers, the API client (login, token
rotation, retries, error mapping) and the websocket client (reconnects, token
refresh, re-subscribing, a failing handler) against local fake servers.
Platform and config-flow tests would need `pytest-homeassistant-custom-component`.

Entity names live in `scripts/generate_translations.py`, which writes
`strings.json` and the English/Norwegian translations and checks that every
`translation_key` in the code has a name.

**Never commit HTTP captures** — they contain your password and tokens
(`.gitignore` excludes `http-toolkit-export-*.json` and `*.har`).

## Dashboard examples

See [`examples/lovelace`](examples/lovelace/README.md).
