"""Main (polling) coordinator for Tibber App Bridge.

Live Pulse data lives in `live.py` and pushed vehicle/charger state in
`devices.py`; this coordinator owns the client, discovery, the polled data
groups, the long-term statistics import and the websocket subscriptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import parsers, queries
from .api import TibberAppAuthError, TibberAppClient, TibberAppError, iso
from .const import (
    CAPACITY_TARIFF_STEPS,
    CONF_BACKFILL_DAYS,
    CONF_CAPACITY_STEPS,
    CONF_INCLUDE_GRID_PRICE,
    CONF_LIVE_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_INCLUDE_GRID_PRICE,
    DEFAULT_LIVE_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
)
from .devices import TibberDeviceCoordinator
from .statistics import async_import_hourly
from .websocket import GraphQLSubscription, fetch_once

if TYPE_CHECKING:
    from .live import TibberLiveCoordinator

_LOGGER = logging.getLogger(__name__)

type TibberAppConfigEntry = ConfigEntry[TibberAppRuntime]


@dataclass
class TibberAppRuntime:
    """Objects shared by the platforms (entry.runtime_data)."""

    client: TibberAppClient
    main: TibberAppCoordinator
    devices: TibberDeviceCoordinator
    live: TibberLiveCoordinator | None = None


# Calendar helpers ----------------------------------------------------------


@dataclass
class Periods:
    """Calendar boundaries in the home's time zone."""

    now: datetime
    day: datetime
    tomorrow: datetime
    month: datetime
    next_month: datetime
    prev_month: datetime
    year: datetime
    next_year: datetime
    prev_year: datetime

    @classmethod
    def at(cls, now: datetime) -> Periods:
        """Build boundaries for `now` (aware)."""
        tz = now.tzinfo
        day = datetime(now.year, now.month, now.day, tzinfo=tz)
        month = datetime(now.year, now.month, 1, tzinfo=tz)
        return cls(
            now=now,
            day=day,
            tomorrow=add_days(day, 1),
            month=month,
            next_month=add_months(month, 1),
            prev_month=add_months(month, -1),
            year=datetime(now.year, 1, 1, tzinfo=tz),
            next_year=datetime(now.year + 1, 1, 1, tzinfo=tz),
            prev_year=datetime(now.year - 1, 1, 1, tzinfo=tz),
        )


def add_days(day: datetime, days: int) -> datetime:
    """Midnight `days` after `day` (DST safe)."""
    d = day.date() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, tzinfo=day.tzinfo)


def add_months(month: datetime, months: int) -> datetime:
    """First of the month `months` after `month`."""
    index = month.year * 12 + month.month - 1 + months
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=month.tzinfo)


def _range(start: datetime, end: datetime, resolution: str) -> dict[str, Any]:
    return {"from": iso(start), "to": iso(end), "resolution": resolution}


# Polling ---------------------------------------------------------------------


@dataclass
class _Task:
    """One group of queries with its own interval and failure state."""

    name: str
    interval: timedelta
    func: Callable[[], Awaitable[dict[str, Any]]]
    last_run: datetime | None = None
    last_success: datetime | None = None
    failing: bool = False

    def due(self, now: datetime) -> bool:
        return self.last_run is None or now - self.last_run >= self.interval

    def failed(self, now: datetime, err: BaseException) -> None:
        # Log once when it starts failing, then quietly retry sooner than usual.
        if not isinstance(err, TibberAppError):
            _LOGGER.error("Unexpected error updating %s", self.name, exc_info=err)
        elif not self.failing:
            _LOGGER.warning("Could not update %s from Tibber: %s", self.name, err)
        else:
            _LOGGER.debug("Updating %s still fails: %s", self.name, err)
        self.failing = True
        self.last_run = now - self.interval + timedelta(minutes=2)

    def succeeded(self, now: datetime) -> None:
        if self.failing:
            _LOGGER.info("Updating %s from Tibber works again", self.name)
        self.failing = False
        self.last_run = self.last_success = now


class TibberAppCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls everything except the live Pulse feed.

    It ticks every minute, but each data group has its own interval so the
    API sees roughly the same load as someone using the app.
    """

    config_entry: TibberAppConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TibberAppConfigEntry,
        client: TibberAppClient,
        home_id: str,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {home_id[:8]}",
            update_interval=timedelta(minutes=1),
        )
        self.client = client
        self.home_id = home_id
        self.devices = TibberDeviceCoordinator(hass, entry, home_id)

        # Filled in by discovery (_async_setup).
        self.home: dict[str, Any] = {}
        self.account_home_count = 1
        self.pulse_id: str | None = None
        self.charger_ids: list[dict[str, str]] = []
        self.vehicle_gizmos: list[dict[str, str]] = []
        self.has_grid_rewards = False
        self.home_device_id: str | None = None  # set in async_setup_entry
        self.tz = dt_util.get_default_time_zone()

        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{home_id}")
        self.stored: dict[str, Any] = {"peaks": {}}
        self._backfill_done = asyncio.Event()
        self._subscriptions: list[GraphQLSubscription] = []
        self._tasks = [
            _Task("Pulse status", timedelta(minutes=5), self._fetch_pulse),
            _Task("today's consumption", timedelta(minutes=5), self._fetch_today),
            _Task("peak control", timedelta(minutes=5), self._fetch_peak),
            _Task("prices", timedelta(minutes=15), self._fetch_prices),
            _Task("this month", timedelta(minutes=15), self._fetch_month),
            _Task("this year", timedelta(hours=1), self._fetch_year),
            _Task("hourly history", timedelta(hours=1), self._import_history),
            _Task("account details", timedelta(hours=6), self._fetch_slow),
        ]

    # Derived settings ----------------------------------------------------------

    @property
    def country(self) -> str | None:
        """Country code of the home's address (NO, SE, DE, NL ...)."""
        return (self.home.get("address") or {}).get("country")

    @property
    def options(self) -> dict[str, Any]:
        """Entry options with defaults (capacity steps depend on the country)."""
        return {
            CONF_LIVE_INTERVAL: DEFAULT_LIVE_INTERVAL,
            CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
            CONF_CAPACITY_STEPS: CAPACITY_TARIFF_STEPS.get(self.country or "", ""),
            CONF_INCLUDE_GRID_PRICE: DEFAULT_INCLUDE_GRID_PRICE,
        } | dict(self.config_entry.options)

    @property
    def capacity_enabled(self) -> bool:
        """Capacity tariff sensors make sense (Norway, or steps configured)."""
        return self.country in CAPACITY_TARIFF_STEPS or bool(
            parsers.parse_steps(self.options[CONF_CAPACITY_STEPS])
        )

    @property
    def currency(self) -> str:
        """Currency reported by the API, else Home Assistant's currency."""
        return (self.data or {}).get("currency") or self.hass.config.currency

    @property
    def has_phase_currents(self) -> bool:
        """The Pulse reports per-phase current/voltage."""
        return ((self.data or {}).get("pulse") or {}).get("has_phase_currents") is not False

    def periods(self) -> Periods:
        """Calendar periods for the home's time zone."""
        return Periods.at(dt_util.now(self.tz))

    def vehicle_ids(self) -> list[str]:
        """Vehicles that belong to this home.

        Vehicles are account-wide; the home's gizmos say which ones are linked
        here. Without gizmos the account's list is only used for single-home
        accounts, so two homes never claim the same car.
        """
        if self.vehicle_gizmos:
            return [g["id"] for g in self.vehicle_gizmos]
        if self.account_home_count > 1:
            return []
        return [v["id"] for v in (self.data or {}).get("vehicles") or []]

    # Discovery -------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Discover the home, Pulse, chargers, vehicles and Grid Rewards once."""
        self.stored = await self._store.async_load() or {"peaks": {}}
        self.stored.setdefault("peaks", {})
        try:
            me = await self.client.get_homes()
            gizmos = await self.client.home(queries.GET_HOME_GIZMOS, self.home_id)
            history = await self.client.home(queries.GRID_REWARDS_HISTORY, self.home_id)
        except TibberAppAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except TibberAppError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="setup_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        homes = me.get("homes") or []
        self.account_home_count = max(len(homes), 1)
        self.home = next((h for h in homes if h.get("id") == self.home_id), {})
        if not self.home:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="home_not_found",
                translation_placeholders={"home_id": self.home_id},
            )
        if tz := dt_util.get_time_zone(self.home.get("timeZone") or ""):
            self.tz = tz

        for gizmo in _flatten_gizmos(gizmos):
            kind, gizmo_id = gizmo.get("type"), gizmo.get("id")
            if not gizmo_id:
                continue
            if kind == "REAL_TIME_METER":
                self.pulse_id = gizmo_id
            elif kind == "EV_CHARGER":
                self.charger_ids.append({"id": gizmo_id, "title": gizmo.get("title") or "Charger"})
            elif kind == "ELECTRIC_VEHICLE":
                self.vehicle_gizmos.append({"id": gizmo_id, "title": gizmo.get("title") or ""})

        if not self.pulse_id:
            # Not every home exposes the gizmo; the Pulse query can resolve it.
            try:
                pulse = (await self.client.home(queries.PULSE_QUERY, self.home_id)).get("pulse")
            except TibberAppError as err:
                _LOGGER.debug("No Pulse found for this home: %s", err)
            else:
                self.pulse_id = (pulse or {}).get("id")

        self.has_grid_rewards = bool(
            (history.get("gridRewardsHistory") or {}).get("valuesFrom")
            or self.vehicle_gizmos
            or self.charger_ids
        )
        if self.has_grid_rewards:
            self._tasks.append(
                _Task("Grid Rewards", timedelta(minutes=15), self._fetch_grid_reward)
            )
        _LOGGER.debug(
            "Discovered home %s: pulse=%s chargers=%d vehicles=%d grid_rewards=%s",
            self.home_id[:8],
            bool(self.pulse_id),
            len(self.charger_ids),
            len(self.vehicle_gizmos),
            self.has_grid_rewards,
        )
        self.config_entry.async_create_background_task(
            self.hass, self._backfill(), f"{DOMAIN} history import {self.home_id[:8]}"
        )

    # Update loop -------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        data = dict(self.data or {})
        due = [t for t in self._tasks if t.due(now) or self._prices_need_tomorrow(t, data, now)]
        if not due:
            return data

        results = await asyncio.gather(*(t.func() for t in due), return_exceptions=True)
        for task, result in zip(due, results, strict=True):
            if isinstance(result, TibberAppAuthError):
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from result
            if isinstance(result, BaseException):
                task.failed(now, result)
                continue
            task.succeeded(now)
            data.update(result)

        if all(t.failing for t in due):
            # Nothing at all came back: mark entities unavailable.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(results[0])},
            )
        data["last_update"] = {
            t.name: t.last_success.isoformat() if t.last_success else None for t in self._tasks
        }
        return data

    def _prices_need_tomorrow(self, task: _Task, data: dict[str, Any], now: datetime) -> bool:
        """Poll prices every 5 min after 13:00 until tomorrow's prices appear."""
        return (
            task.func == self._fetch_prices
            and task.last_run is not None
            and now - task.last_run >= timedelta(minutes=5)
            and dt_util.as_local(now).astimezone(self.tz).hour >= 13
            and not (data.get("prices") or {}).get("tomorrow")
        )

    # Fetchers ------------------------------------------------------------------

    async def _fetch_pulse(self) -> dict[str, Any]:
        if not self.pulse_id:
            return {}
        home = await self.client.home(queries.PULSE_QUERY, self.home_id, deviceId=self.pulse_id)
        return {"pulse": parsers.pulse_info(home.get("pulse") or {})}

    async def _fetch_today(self) -> dict[str, Any]:
        p = self.periods()
        home = await self.client.home(
            queries.CONSUMPTION_WITH_CHART,
            self.home_id,
            useDemoData=False,
            **_range(p.day, p.tomorrow, "daily"),
        )
        item = parsers.analysis_item(home)
        points = parsers.hourly_points(item)
        self._record_peaks(points)
        metered_until = points[-1][0] + timedelta(hours=1) if points else p.day
        today: dict[str, Any] = {
            "hours": parsers.chart_series(item),
            "billed_kwh": round(sum(k for _, k, _ in points), 3),
            "billed_cost": round(sum(c for _, _, c in points), 2),
            "billed_until": metered_until.isoformat(),
        }

        if self.pulse_id:
            # Minute averages since midnight: min/max power and the part of the
            # day that isn't in the hourly meter data yet.
            home = await self.client.home(
                queries.PULSE_MEASUREMENTS_STORAGE,
                self.home_id,
                deviceId=self.pulse_id,
                **{"from": iso(p.day), "to": iso(p.now)},
            )
            samples = [
                (ts, m["power"])
                for m in home.get("pulseMeasurementHistory") or []
                if m.get("power") is not None and (ts := parsers.parse_dt(m.get("timestamp")))
            ]
            if samples:
                hi = max(samples, key=lambda s: s[1])
                lo = min(samples, key=lambda s: s[1])
                today |= {
                    "max_power": round(hi[1]),
                    "max_power_at": hi[0].isoformat(),
                    "min_power": round(lo[1]),
                    "min_power_at": lo[0].isoformat(),
                }
            today["unbilled_kwh"] = round(
                sum(w for t, w in samples if t >= metered_until) / 60000, 3
            )

        result: dict[str, Any] = {"today": today}
        if currency := (item or {}).get("currency"):
            result["currency"] = currency
        return result

    async def _fetch_peak(self) -> dict[str, Any]:
        if not self.pulse_id:
            return {}
        peak, fuse = await asyncio.gather(
            self.client.home(queries.PEAK_CONTROL, self.home_id),
            self.client.home(queries.FUSE_LOAD_EXCESS, self.home_id),
        )
        return {
            "peak_control": peak.get("peakControlData") or {},
            "overloads": parsers.fuse_overloads(fuse),
        }

    async def _fetch_prices(self) -> dict[str, Any]:
        home = await self.client.home(queries.PRICE_RATING, self.home_id)
        rating = (home.get("subscription") or {}).get("priceRating") or {}
        grid = rating.get("gridPriceToggler") or {}
        include_grid = bool(
            self.options[CONF_INCLUDE_GRID_PRICE] and grid.get("gridCompanyProvidesData")
        )
        quarter = parsers.price_entries(
            rating.get("quarterHourly"), include_grid, timedelta(minutes=15)
        )
        entries = quarter or parsers.price_entries(
            rating.get("hourly"), include_grid, timedelta(hours=1)
        )
        thresholds = rating.get("thresholdPercentages") or {}
        parsers.classify_prices(entries, thresholds.get("low", -10), thresholds.get("high", 10))

        p = self.periods()
        day_after = add_days(p.tomorrow, 1)
        daily = parsers.price_entries(rating.get("daily"), include_grid, timedelta(days=1))
        monthly = parsers.price_entries(rating.get("monthly"), False, timedelta(days=28))
        result: dict[str, Any] = {
            "prices": {
                "entries": entries,
                "today": [e for e in entries if p.day <= e["start"] < p.tomorrow],
                "tomorrow": [e for e in entries if p.tomorrow <= e["start"] < day_after],
                "resolution": "quarter_hourly" if quarter else "hourly",
                "include_grid": include_grid,
                "grid_company": grid.get("gridCompanyName"),
                "provider": rating.get("nemoPriceProviderName"),
                "thresholds": thresholds,
                "daily": parsers.serialize_prices(daily),
                "monthly": [
                    {"start": e["start"].isoformat(), "energy": e["energy"], "total": e["total"]}
                    for e in monthly
                ],
            }
        }
        if currency := (rating.get("hourly") or rating.get("quarterHourly") or {}).get("currency"):
            result["currency"] = currency
        return result

    async def _fetch_month(self) -> dict[str, Any]:
        p = self.periods()
        month_range = _range(p.month, p.next_month, "monthly")
        consumption, gizmo_cost, gizmo_price = await asyncio.gather(
            self.client.home(
                queries.CONSUMPTION_WITH_CHART, self.home_id, useDemoData=False, **month_range
            ),
            self.client.home(queries.CONSUMPTION_GIZMO, self.home_id),
            self.client.home(queries.PRICE_PERFORMANCE_GIZMO, self.home_id),
        )
        result: dict[str, Any] = {
            "month": parsers.period_summary(parsers.analysis_item(consumption)),
            "gizmo": {
                "cost": (gizmo_cost.get("consumptionGizmoData") or {}).get("cost"),
                "average_price": (gizmo_price.get("pricePerformanceGizmoData") or {}).get("value"),
            },
            "chargers": [await self._fetch_charger(c, p) for c in self.charger_ids],
        }
        if self.has_grid_rewards:
            rewards = await self.client.home(
                queries.GRID_REWARDS_PERIOD, self.home_id, **month_range
            )
            result["grid_rewards_month"] = parsers.rewards_period(rewards)
        return result

    async def _fetch_charger(self, charger: dict[str, str], p: Periods) -> dict[str, Any]:
        args = {"chargerId": charger["id"], "from": iso(p.month), "to": iso(p.next_month)}
        monthly, daily = await asyncio.gather(
            self.client.home(queries.CHARGER_MONTHLY, self.home_id, **args),
            self.client.home(queries.CHARGER_CONSUMPTION, self.home_id, resolution="daily", **args),
        )
        months = monthly.get("evChargerConsumptionMonths") or []
        month = months[0] if months else {}
        return {
            **charger,
            "kwh": month.get("consumption"),
            "cost": month.get("energyCost"),
            "days": [
                {"start": d.get("from"), "kwh": d.get("consumption"), "cost": d.get("energyCost")}
                for d in daily.get("evChargerConsumption") or []
            ],
        }

    async def _fetch_year(self) -> dict[str, Any]:
        p = self.periods()
        hid = self.home_id

        def consumption(start: datetime, end: datetime, res: str) -> Awaitable[dict[str, Any]]:
            return self.client.home(
                queries.CONSUMPTION_WITH_CHART, hid, useDemoData=False, **_range(start, end, res)
            )

        def comparison(start: datetime, end: datetime, res: str) -> Awaitable[dict[str, Any]]:
            return self.client.home(
                queries.COMPARISON_ITEMS, hid, useDemoData=False, **_range(start, end, res)
            )

        def performance(start: datetime, end: datetime, res: str) -> Awaitable[dict[str, Any]]:
            # The app sends plain dates for this query.
            return self.client.home(
                queries.PRICE_PERFORMANCE_ITEMS,
                hid,
                **{
                    "from": start.date().isoformat(),
                    "to": end.date().isoformat(),
                    "resolution": res,
                },
            )

        year, last_year, comp_month, comp_year, perf_month, perf_year = await asyncio.gather(
            consumption(p.year, p.next_year, "annual"),
            consumption(p.prev_year, p.year, "annual"),
            comparison(p.month, p.next_month, "monthly"),
            comparison(p.year, p.next_year, "annual"),
            performance(p.month, p.next_month, "monthly"),
            performance(p.year, p.next_year, "annual"),
        )
        result: dict[str, Any] = {
            "year": parsers.period_summary(parsers.analysis_item(year)),
            "last_year": parsers.period_summary(parsers.analysis_item(last_year)),
            "comparison_month": parsers.comparison(comp_month),
            "comparison_year": parsers.comparison(comp_year),
            "price_performance_month": parsers.price_performance(perf_month),
            "price_performance_year": parsers.price_performance(perf_year),
        }
        if self.has_grid_rewards:
            rewards = await self.client.home(
                queries.GRID_REWARDS_PERIOD, hid, **_range(p.year, p.next_year, "annual")
            )
            result["grid_rewards_year"] = parsers.rewards_period(rewards)
        return result

    async def _fetch_slow(self) -> dict[str, Any]:
        p = self.periods()
        hid = self.home_id
        prev_range = _range(p.prev_month, p.month, "monthly")
        me, vehicles, prev_month, disagg = await asyncio.gather(
            self.client.get_homes(),
            self.client.query(queries.MY_VEHICLES),
            self.client.home(queries.CONSUMPTION_WITH_CHART, hid, useDemoData=False, **prev_range),
            self.client.home(queries.COST_DISAGGREGATION, hid, useDemoData=False, **prev_range),
        )
        if home := next((h for h in me.get("homes") or [] if h.get("id") == hid), None):
            self.home = home
        result: dict[str, Any] = {
            "home": {"settings": parsers.settings_dict(self.home.get("settings"))},
            "vehicles": parsers.my_vehicles(vehicles.get("me") or {}),
            "previous_month": parsers.period_summary(parsers.analysis_item(prev_month)),
            "disaggregation": parsers.cost_disaggregation(disagg),
        }
        if self.has_grid_rewards:
            result |= await self._fetch_rewards_history(p)
        return result

    async def _fetch_rewards_history(self, p: Periods) -> dict[str, Any]:
        """Grid Rewards per year since the first reward, and charging price."""
        history, info = await asyncio.gather(
            self.client.home(queries.GRID_REWARDS_HISTORY, self.home_id),
            self.client.home(queries.GRID_REWARDS_INFO, self.home_id),
        )
        years: dict[int, float] = {}
        if first := parsers.parse_dt((history.get("gridRewardsHistory") or {}).get("valuesFrom")):
            for year in range(first.year, p.now.year + 1):
                start = datetime(year, 1, 1, tzinfo=self.tz)
                end = datetime(year + 1, 1, 1, tzinfo=self.tz)
                period = await self.client.home(
                    queries.GRID_REWARDS_PERIOD, self.home_id, **_range(start, end, "annual")
                )
                years[year] = parsers.rewards_period(period).get("total") or 0.0
        charging = (info.get("gridRewardsInfo") or {}).get(
            "chargingPricePerformanceLastMonth"
        ) or {}
        return {
            "grid_rewards_years": years,
            "grid_rewards_total": round(sum(years.values()), 2) if years else None,
            "charging_average_price": charging.get("averagePrice"),
        }

    async def _fetch_grid_reward(self) -> dict[str, Any]:
        """Snapshot via the one-shot subscription (fallback for the stream)."""
        try:
            data = await fetch_once(
                self.client, queries.GRID_REWARDS_SUBSCRIPTION, {"homeId": self.home_id}
            )
        except TibberAppAuthError as err:
            # A refused socket alone shouldn't start a reauth; HTTP queries decide.
            raise TibberAppError(str(err)) from err
        parsed = parsers.grid_reward(data or {})
        return {"grid_reward": parsed} if parsed else {}

    # Subscriptions ---------------------------------------------------------------

    def start_subscriptions(self, live: TibberLiveCoordinator | None) -> None:
        """Open the push subscriptions the app uses (one socket each)."""

        def add(
            name: str,
            document: str,
            variables: dict[str, Any],
            handler: Callable[[dict[str, Any]], None],
            refresh_after: float | None = None,
        ) -> None:
            sub = GraphQLSubscription(
                self.client, document, variables, handler, name, refresh_after
            )
            sub.start(
                lambda coro: self.config_entry.async_create_background_task(
                    self.hass, coro, f"{DOMAIN} {name} {self.home_id[:8]}"
                )
            )
            self._subscriptions.append(sub)

        if live and self.pulse_id:
            # The poll in live.py covers gaps; re-subscribe after 60 s of silence.
            add(
                "Pulse",
                queries.PULSE_SUBSCRIPTION,
                {"deviceId": self.pulse_id},
                live.handle_push,
                refresh_after=60,
            )
        if self.has_grid_rewards:
            add(
                "Grid Rewards",
                queries.GRID_REWARD_TRANSITIONS_SUBSCRIPTION,
                {"homeId": self.home_id},
                self._handle_grid_reward,
            )
        for vehicle_id in self.vehicle_ids():
            # Pushes about every 20 s; re-subscribe if it goes quiet.
            add(
                f"vehicle {vehicle_id[:8]}",
                queries.VEHICLE_STATE_SUBSCRIPTION,
                {"vehicleId": vehicle_id},
                self.devices.handle_vehicle,
                refresh_after=120,
            )
        for charger in self.charger_ids:
            # Answers once per subscribe; the app re-subscribes, so do we.
            add(
                f"charger {charger['id'][:8]}",
                queries.VEHICLE_CHARGER_SUBSCRIPTION,
                {"chargerId": charger["id"]},
                self.devices.handle_charger,
                refresh_after=30,
            )

    async def async_stop_subscriptions(self) -> None:
        """Close open sockets."""
        await asyncio.gather(*(sub.stop() for sub in self._subscriptions))
        self._subscriptions.clear()

    def _handle_grid_reward(self, data: dict[str, Any]) -> None:
        if parsed := parsers.grid_reward(data):
            self.async_set_updated_data({**(self.data or {}), "grid_reward": parsed})

    # History / statistics ----------------------------------------------------

    async def _day_points(self, day: datetime) -> list[tuple[datetime, float, float]]:
        home = await self.client.home(
            queries.CONSUMPTION_WITH_CHART,
            self.home_id,
            useDemoData=False,
            **_range(day, add_days(day, 1), "daily"),
        )
        points = parsers.hourly_points(parsers.analysis_item(home))
        self._record_peaks(points)
        return points

    def _record_peaks(self, points: list[tuple[datetime, float, float]]) -> None:
        """Remember the highest hour per day (for the capacity tariff)."""
        by_day: dict[str, tuple[datetime, float]] = {}
        for start, kwh, _ in points:
            key = start.astimezone(self.tz).date().isoformat()
            if key not in by_day or kwh > by_day[key][1]:
                by_day[key] = (start, kwh)
        peaks = self.stored["peaks"]
        for key, (start, kwh) in by_day.items():
            peaks[key] = {"date": key, "start": start.isoformat(), "kwh": round(kwh, 3)}
        cutoff = (dt_util.now(self.tz).date() - timedelta(days=430)).isoformat()  # ~14 months
        for key in [k for k in peaks if k < cutoff]:
            del peaks[key]
        self._store.async_delay_save(lambda: self.stored, 10)

    async def _backfill(self) -> None:
        """Import hourly history into long-term statistics (once per range)."""
        try:
            p = self.periods()
            days = int(self.options[CONF_BACKFILL_DAYS])
            start = min(add_days(p.day, -days), p.month)
            done_from = self.stored.get("backfilled_from")
            if done_from and date.fromisoformat(done_from) <= start.date():
                return
            _LOGGER.info("Importing %d days of hourly history from Tibber", days)
            points: list[tuple[datetime, float, float]] = []
            skipped = 0
            day = start
            while day <= p.day:
                try:
                    points.extend(await self._day_points(day))
                except TibberAppAuthError:
                    raise
                except TibberAppError as err:
                    skipped += 1
                    _LOGGER.debug("No history for %s: %s", day.date(), err)
                day = add_days(day, 1)
                await asyncio.sleep(0.3)  # be gentle with the API
            if skipped:
                _LOGGER.warning("History import skipped %d days that Tibber didn't return", skipped)
            await async_import_hourly(self.hass, self.home_id, self._title, self.currency, points)
            self.stored["backfilled_from"] = start.date().isoformat()
            await self._store.async_save(self.stored)
            self.async_update_listeners()
        except TibberAppAuthError:
            _LOGGER.warning("History import stopped: the Tibber session is no longer valid")
        except Exception:
            _LOGGER.exception("History import failed")
        finally:
            self._backfill_done.set()

    async def _import_history(self) -> dict[str, Any]:
        """Re-import the last three days (meter values arrive with a delay)."""
        if not self._backfill_done.is_set():
            return {}
        p = self.periods()
        points: list[tuple[datetime, float, float]] = []
        for offset in (-2, -1, 0):
            points.extend(await self._day_points(add_days(p.day, offset)))
        await async_import_hourly(self.hass, self.home_id, self._title, self.currency, points)
        return {}

    @property
    def _title(self) -> str:
        return self.home.get("title") or self.config_entry.title

    def capacity(self, month: datetime | None = None) -> dict[str, Any] | None:
        """Calculated top-3 hours for a month from stored daily peaks."""
        prefix = (month or self.periods().month).date().isoformat()[:7]
        return parsers.top_three(
            {k: v for k, v in self.stored["peaks"].items() if k.startswith(prefix)}
        )


def _flatten_gizmos(home: dict[str, Any]) -> list[dict[str, Any]]:
    """Gizmos may be grouped; return them as one flat list."""
    flat: list[dict[str, Any]] = []
    for gizmo in home.get("gizmos") or []:
        flat.extend(gizmo.get("gizmos") or [gizmo])
    return flat
