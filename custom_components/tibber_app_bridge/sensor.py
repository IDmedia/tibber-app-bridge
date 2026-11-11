"""Sensors for Tibber App Bridge.

Descriptions are grouped by where their data comes from:

* HOME_SENSORS / PULSE_SENSORS - main (polled) coordinator: value_fn(main, data)
* LIVE_SENSORS - live Pulse coordinator: value_fn(live, data)
* VEHICLE_SENSORS / CHARGER_SENSORS - one item per device: value_fn(item)
  (see entity.vehicle_item / entity.charger_item)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import parsers
from .const import CONF_CAPACITY_STEPS
from .coordinator import TibberAppConfigEntry, TibberAppCoordinator
from .entity import (
    HOME,
    PULSE,
    UNIT_MONEY,
    UNIT_PRICE,
    TibberAppEntity,
    charger_item,
    dig,
    period_start,
    resolve_unit,
    vehicle_item,
)
from .live import TibberLiveCoordinator
from .statistics import statistic_ids

PARALLEL_UPDATES = 0

type Main = TibberAppCoordinator
type Data = dict[str, Any]
type Item = dict[str, Any]

GRID_STATES = ["available", "unavailable", "delivering", "unknown"]


@dataclass(frozen=True, kw_only=True)
class TibberSensorEntityDescription(SensorEntityDescription):
    """Sensor description with callbacks for value and attributes."""

    value_fn: Callable[..., Any]
    attrs_fn: Callable[..., dict[str, Any] | None] | None = None
    # UNIT_MONEY / UNIT_PRICE are resolved from the currency at runtime.
    unit: str | None = None
    # "day" / "month" / "year": last_reset for TOTAL sensors.
    period: str | None = None
    exists_fn: Callable[[Main], bool] = lambda main: True
    # Unique id key when it differs from `key` (kept from earlier versions).
    unique_key: str | None = None


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _ts(value: Any) -> datetime | None:
    return parsers.parse_dt(value) if isinstance(value, str) else value


def _has_grid_rewards(main: Main) -> bool:
    return main.has_grid_rewards


def _has_capacity(main: Main) -> bool:
    return main.capacity_enabled


# Prices --------------------------------------------------------------------


def _current_price(main: Main) -> dict[str, Any] | None:
    return parsers.price_at(dig(main.data, "prices", "entries") or [], dt_util.now(main.tz))


def _next_price(main: Main) -> dict[str, Any] | None:
    if not (current := _current_price(main)):
        return None
    return parsers.price_at(dig(main.data, "prices", "entries") or [], current["end"])


def _price_attrs(main: Main, data: Data) -> dict[str, Any]:
    current = _current_price(main) or {}
    prices = data.get("prices") or {}
    return {
        "energy_price": current.get("energy"),
        "grid_price": current.get("grid"),
        "price_without_grid": current.get("total"),
        "subsidised_price": current.get("subsidised"),
        "level": current.get("level"),
        "diff_from_daily_average_pct": current.get("diff_pct"),
        "start": current["start"].isoformat() if current else None,
        "resolution": prices.get("resolution"),
        "includes_grid_price": prices.get("include_grid"),
        "grid_company": prices.get("grid_company"),
        "price_provider": prices.get("provider"),
        "thresholds": prices.get("thresholds"),
        "today": parsers.serialize_prices(prices.get("today") or []),
        "tomorrow": parsers.serialize_prices(prices.get("tomorrow") or []),
        "tomorrow_available": bool(prices.get("tomorrow")),
        # The current slot and the next 11, for "upcoming prices" tables.
        "upcoming": parsers.upcoming_prices(prices.get("entries") or [], dt_util.now(main.tz)),
    }


def _day_stat(data: Data, day: str, stat: str) -> Any:
    summary = parsers.day_summary(dig(data, "prices", day) or [])
    return summary[stat] if summary else None


def _day_stat_at(day: str, stat: str) -> Callable[[Main, Data], dict[str, Any]]:
    def attrs(main: Main, data: Data) -> dict[str, Any]:
        at = _day_stat(data, day, f"{stat}_at")
        return {"at": at.isoformat() if at else None}

    return attrs


# Usage -----------------------------------------------------------------------


def _today_kwh(main: Main, data: Data) -> float | None:
    today = data.get("today") or {}
    if today.get("billed_kwh") is None:
        return None
    return round(today["billed_kwh"] + (today.get("unbilled_kwh") or 0.0), 3)


def _today_cost(main: Main, data: Data) -> float | None:
    today = data.get("today") or {}
    if today.get("billed_cost") is None:
        return None
    # Consumption cost is Tibber's energy cost, i.e. without the grid tariff.
    price = (_current_price(main) or {}).get("total") or 0.0
    return round(today["billed_cost"] + (today.get("unbilled_kwh") or 0.0) * price, 2)


def _statistic_attrs(main: Main) -> dict[str, str]:
    """Long-term statistic ids (for the Energy dashboard / statistics-graph)."""
    consumption, cost = statistic_ids(main.home_id)
    return {"statistic_id_consumption": consumption, "statistic_id_cost": cost}


def _today_attrs(main: Main, data: Data) -> dict[str, Any]:
    today = data.get("today") or {}
    return {
        "hours": today.get("hours"),
        "metered_kwh": today.get("billed_kwh"),
        "metered_until": today.get("billed_until"),
        "estimated_from_pulse_kwh": today.get("unbilled_kwh"),
        **_statistic_attrs(main),
    }


def _period_attrs(key: str) -> Callable[[Main, Data], dict[str, Any]]:
    def attrs(main: Main, data: Data) -> dict[str, Any]:
        period = data.get(key) or {}
        return {k: period.get(k) for k in ("from", "estimated_kwh", "estimated_cost", "series")}

    return attrs


def _last_year_to_date(main: Main, data: Data, field: str) -> float | None:
    """Last year's value up to today's date (monthly data, this month prorated)."""
    series = dig(data, "last_year", "series") or []
    if not series:
        return None
    p = main.periods()
    total = 0.0
    for item in series:
        start = parsers.parse_dt(item["start"])
        if start is None or item.get(field) is None:
            continue
        if start.month < p.now.month:
            total += item[field]
        elif start.month == p.now.month:
            elapsed = (p.now - p.month).total_seconds() / 86400
            total += item[field] * min(elapsed / (p.next_month - p.month).days, 1.0)
    return round(total, 2)


def _change_pct(now: float | None, before: float | None) -> float | None:
    return round((now - before) / before * 100, 1) if now is not None and before else None


def _year_attrs(main: Main, data: Data) -> dict[str, Any]:
    ly_kwh = _last_year_to_date(main, data, "kwh")
    ly_cost = _last_year_to_date(main, data, "cost")
    return _period_attrs("year")(main, data) | {
        "last_year_to_date_kwh": ly_kwh,
        "last_year_to_date_cost": ly_cost,
        "change_vs_last_year_pct": _change_pct(dig(data, "year", "kwh"), ly_kwh),
        "cost_change_vs_last_year_pct": _change_pct(dig(data, "year", "cost"), ly_cost),
        "last_year_series": dig(data, "last_year", "series"),
    }


# Capacity tariff -------------------------------------------------------------


def _capacity_value(main: Main, data: Data) -> float | None:
    """Tibber's "average of the 3 highest hours", else our own calculation."""
    value = dig(data, "month", "top3_average")
    if value is None and (calculated := main.capacity()):
        value = calculated["average"]
    return value


def _capacity_step(main: Main, data: Data) -> dict[str, Any]:
    steps = parsers.parse_steps(main.options[CONF_CAPACITY_STEPS])
    return parsers.capacity_step(_capacity_value(main, data), steps) or {}


def _capacity_attrs(main: Main, data: Data) -> dict[str, Any]:
    calculated = main.capacity() or {}
    previous = main.capacity(main.periods().prev_month) or {}
    step = _capacity_step(main, data)
    peaks = calculated.get("peaks") or []
    return {
        "source": "tibber" if dig(data, "month", "top3_average") is not None else "calculated",
        "calculated_average": calculated.get("average"),
        "peaks": peaks,
        # Same names and shape as the tibber_data integration, so existing
        # templates keep working after switching (highest first).
        "peak_consumptions": [p["kwh"] for p in peaks],
        "peak_consumption_dates": [p["start"] for p in peaks],
        "step": step.get("step"),
        "step_lower": step.get("lower"),
        "step_upper": step.get("upper"),
        # Upper bounds of every step (kWh/h), so cards can list all of them.
        "steps": parsers.parse_steps(main.options[CONF_CAPACITY_STEPS]),
        "previous_month": dig(data, "previous_month", "top3_average") or previous.get("average"),
        "previous_month_peaks": previous.get("peaks"),
    }


def _capacity_outlook(main: Main) -> dict[str, Any]:
    p = main.periods()
    prefix = p.month.date().isoformat()[:7]
    peaks = {k: v for k, v in main.stored["peaks"].items() if k.startswith(prefix)}
    steps = parsers.parse_steps(main.options[CONF_CAPACITY_STEPS])
    return parsers.capacity_outlook(peaks, p.day.date().isoformat(), steps) or {}


def _cheapest(main: Main) -> dict[str, Any]:
    entries = dig(main.data, "prices", "entries") or []
    return parsers.cheapest_window(entries, dt_util.now(main.tz), hours=3) or {}


# Other helpers ---------------------------------------------------------------


def _pick(key: str, *fields: str) -> Callable[[Main, Data], dict[str, Any]]:
    """Attributes copied from data[key]."""

    def attrs(main: Main, data: Data) -> dict[str, Any]:
        source = data.get(key) or {}
        return {f: source.get(f) for f in fields}

    return attrs


def _live_or_polled(live_key: str, polled: Callable[[Data], Any]) -> Callable[[Main, Data], Any]:
    """Grid Rewards value from the subscription, else from the history query."""

    def value(main: Main, data: Data) -> Any:
        pushed = dig(data, "grid_reward", live_key)
        return round(pushed, 2) if pushed is not None else polled(data)

    return value


def _overloads_count(main: Main, data: Data) -> int:
    cutoff = dt_util.now() - timedelta(days=30)
    return sum(1 for o in data.get("overloads") or [] if (t := _ts(o["start"])) and t >= cutoff)


# Home ------------------------------------------------------------------------

HOME_SENSORS: tuple[TibberSensorEntityDescription, ...] = (
    TibberSensorEntityDescription(
        key="current_price",
        translation_key="current_price",
        unit=UNIT_PRICE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m, d: (_current_price(m) or {}).get("price"),
        attrs_fn=_price_attrs,
    ),
    TibberSensorEntityDescription(
        key="price_level",
        translation_key="price_level",
        device_class=SensorDeviceClass.ENUM,
        options=["cheap", "normal", "expensive"],
        value_fn=lambda m, d: (_current_price(m) or {}).get("level"),
    ),
    TibberSensorEntityDescription(
        key="next_price",
        translation_key="next_price",
        unit=UNIT_PRICE,
        suggested_display_precision=2,
        value_fn=lambda m, d: (_next_price(m) or {}).get("price"),
        attrs_fn=lambda m, d: {
            "start": n["start"].isoformat() if (n := _next_price(m)) else None,
            "level": (n or {}).get("level"),
        },
    ),
    *(
        TibberSensorEntityDescription(
            key=f"{day}_{stat}_price",
            translation_key=f"{day}_{stat}_price",
            unit=UNIT_PRICE,
            suggested_display_precision=2,
            value_fn=lambda m, d, day=day, stat=stat: _day_stat(d, day, stat),
            attrs_fn=None if stat == "avg" else _day_stat_at(day, stat),
        )
        for day in ("today", "tomorrow")
        for stat in ("min", "max", "avg")
    ),
    TibberSensorEntityDescription(
        key="cheapest_window",
        translation_key="cheapest_window",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda m, d: _cheapest(m).get("start"),
        attrs_fn=lambda m, d: {
            "end": c["end"].isoformat() if (c := _cheapest(m)).get("end") else None,
            "average_price": c.get("average"),
            "hours": c.get("hours"),
        },
    ),
    TibberSensorEntityDescription(
        key="month_spot_price",
        translation_key="month_spot_price",
        unit=UNIT_PRICE,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda m, d: ((dig(d, "prices", "monthly") or [{}])[-1]).get("total"),
        attrs_fn=lambda m, d: {
            "months": dig(d, "prices", "monthly"),
            "days": dig(d, "prices", "daily"),
        },
    ),
    TibberSensorEntityDescription(
        key="today_consumption",
        translation_key="today_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        period="day",
        value_fn=_today_kwh,
        attrs_fn=_today_attrs,
    ),
    TibberSensorEntityDescription(
        key="today_cost",
        translation_key="today_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        period="day",
        value_fn=_today_cost,
    ),
    TibberSensorEntityDescription(
        key="month_consumption",
        translation_key="month_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        period="month",
        value_fn=lambda m, d: dig(d, "month", "kwh"),
        attrs_fn=lambda m, d: _period_attrs("month")(m, d) | _statistic_attrs(m),
    ),
    TibberSensorEntityDescription(
        key="month_cost",
        translation_key="month_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        period="month",
        value_fn=lambda m, d: dig(d, "month", "cost"),
    ),
    TibberSensorEntityDescription(
        key="month_estimated_consumption",
        translation_key="month_estimated_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "month", "estimated_kwh"),
    ),
    TibberSensorEntityDescription(
        key="month_estimated_cost",
        translation_key="month_estimated_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "month", "estimated_cost"),
    ),
    TibberSensorEntityDescription(
        key="previous_month_consumption",
        translation_key="previous_month_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "previous_month", "kwh"),
        attrs_fn=_period_attrs("previous_month"),
    ),
    TibberSensorEntityDescription(
        key="previous_month_cost",
        translation_key="previous_month_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "previous_month", "cost"),
    ),
    TibberSensorEntityDescription(
        key="year_consumption",
        translation_key="year_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        period="year",
        value_fn=lambda m, d: dig(d, "year", "kwh"),
        attrs_fn=_year_attrs,
    ),
    TibberSensorEntityDescription(
        key="year_cost",
        translation_key="year_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        period="year",
        value_fn=lambda m, d: dig(d, "year", "cost"),
    ),
    TibberSensorEntityDescription(
        key="year_estimated_consumption",
        translation_key="year_estimated_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "year", "estimated_kwh"),
    ),
    TibberSensorEntityDescription(
        key="year_estimated_cost",
        translation_key="year_estimated_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "year", "estimated_cost"),
    ),
    TibberSensorEntityDescription(
        key="last_year_consumption",
        translation_key="last_year_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "last_year", "kwh"),
        attrs_fn=_period_attrs("last_year"),
    ),
    TibberSensorEntityDescription(
        key="last_year_cost",
        translation_key="last_year_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "last_year", "cost"),
    ),
    TibberSensorEntityDescription(
        key="top3_hourly_average",
        translation_key="top3_hourly_average",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        exists_fn=_has_capacity,
        value_fn=_capacity_value,
        attrs_fn=_capacity_attrs,
    ),
    TibberSensorEntityDescription(
        key="capacity_step",
        translation_key="capacity_step",
        exists_fn=_has_capacity,
        value_fn=lambda m, d: _capacity_step(m, d).get("step"),
    ),
    TibberSensorEntityDescription(
        key="capacity_peak_threshold",
        translation_key="capacity_peak_threshold",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        exists_fn=_has_capacity,
        value_fn=lambda m, d: _capacity_outlook(m).get("threshold"),
        attrs_fn=lambda m, d: {
            k: _capacity_outlook(m).get(k) for k in ("to_next_step", "next_step", "next_step_lower")
        },
    ),
    *(
        TibberSensorEntityDescription(
            key=f"average_price_{period}",
            translation_key=f"average_price_{period}",
            unit=UNIT_PRICE,
            suggested_display_precision=2,
            value_fn=lambda m, d, period=period: dig(
                d, f"price_performance_{period}", "average_price"
            ),
            attrs_fn=_pick(
                f"price_performance_{period}",
                "from",
                "low",
                "average",
                "high",
                "price_area",
                "disaggregation",
                "grid_rewards_reduction",
            ),
        )
        for period in ("month", "year")
    ),
    *(
        TibberSensorEntityDescription(
            key=f"comparison_{period}",
            translation_key=f"comparison_{period}",
            native_unit_of_measurement=PERCENTAGE,
            suggested_display_precision=1,
            value_fn=lambda m, d, period=period: dig(d, f"comparison_{period}", "diff_pct"),
            attrs_fn=_pick(
                f"comparison_{period}",
                "from",
                "efficiency",
                "stars",
                "description",
                "home_kwh",
                "average_kwh",
                "efficient_kwh",
            ),
        )
        for period in ("month", "year")
    ),
    TibberSensorEntityDescription(
        key="consumption_breakdown",
        translation_key="consumption_breakdown",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda m, d: dig(d, "disaggregation", "total"),
        attrs_fn=lambda m, d: d.get("disaggregation"),
    ),
    TibberSensorEntityDescription(
        key="grid_reward_status",
        translation_key="grid_reward_status",
        device_class=SensorDeviceClass.ENUM,
        options=GRID_STATES,
        exists_fn=_has_grid_rewards,
        value_fn=lambda m, d: dig(d, "grid_reward", "state"),
        attrs_fn=lambda m, d: {
            k: dig(d, "grid_reward", k) for k in ("reasons", "kind", "title", "description")
        },
    ),
    TibberSensorEntityDescription(
        key="grid_rewards_month",
        translation_key="grid_rewards_month",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        period="month",
        exists_fn=_has_grid_rewards,
        value_fn=_live_or_polled("month", lambda d: dig(d, "grid_rewards_month", "total")),
        attrs_fn=_pick("grid_rewards_month", "vehicle", "battery", "series"),
    ),
    TibberSensorEntityDescription(
        key="grid_rewards_year",
        translation_key="grid_rewards_year",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        exists_fn=_has_grid_rewards,
        value_fn=lambda m, d: dig(d, "grid_rewards_year", "total"),
        attrs_fn=_pick("grid_rewards_year", "vehicle", "battery", "series"),
    ),
    TibberSensorEntityDescription(
        key="grid_rewards_total",
        translation_key="grid_rewards_total",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        exists_fn=_has_grid_rewards,
        value_fn=_live_or_polled("all_time", lambda d: d.get("grid_rewards_total")),
        attrs_fn=lambda m, d: {
            "years": [
                {"year": y, "total": v}
                for y, v in sorted((d.get("grid_rewards_years") or {}).items())
            ]
        },
    ),
    TibberSensorEntityDescription(
        key="charging_average_price",
        translation_key="charging_average_price",
        unit=UNIT_PRICE,
        suggested_display_precision=2,
        exists_fn=lambda m: bool(m.charger_ids) and m.has_grid_rewards,
        value_fn=lambda m, d: d.get("charging_average_price"),
    ),
)

# Pulse (polled) --------------------------------------------------------------

PULSE_SENSORS: tuple[TibberSensorEntityDescription, ...] = (
    TibberSensorEntityDescription(
        key="hourly_consumption_limit",
        translation_key="hourly_consumption_limit",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=1,
        value_fn=lambda m, d: dig(d, "peak_control", "consumptionLimit"),
        attrs_fn=lambda m, d: {
            k: dig(d, "peak_control", k)
            for k in ("lowerBound", "recommendedValue", "upperBound", "isAlarmActive")
        },
    ),
    TibberSensorEntityDescription(
        key="peak_control_status",
        translation_key="peak_control_status",
        value_fn=lambda m, d: s.lower() if (s := dig(d, "pulse", "peak_control_status")) else None,
    ),
    TibberSensorEntityDescription(
        key="main_fuse_size",
        translation_key="main_fuse_size",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        value_fn=lambda m, d: (
            dig(d, "pulse", "settings", "mainFuseSize")
            or dig(d, "home", "settings", "mainFuseSize")
        ),
    ),
    TibberSensorEntityDescription(
        key="last_fuse_overload",
        translation_key="last_fuse_overload",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda m, d: _ts(o[0]["start"]) if (o := d.get("overloads")) else None,
        attrs_fn=lambda m, d: {"overloads": d.get("overloads")},
    ),
    TibberSensorEntityDescription(
        key="fuse_overloads_30d",
        translation_key="fuse_overloads_30d",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_overloads_count,
    ),
    TibberSensorEntityDescription(
        key="pulse_wifi_signal",
        translation_key="pulse_wifi_signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m, d: dig(d, "pulse", "wifi_rssi"),
        attrs_fn=lambda m, d: dig(d, "pulse", "info"),
    ),
    TibberSensorEntityDescription(
        key="pulse_measurements_per_minute",
        translation_key="pulse_measurements_per_minute",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m, d: dig(d, "pulse", "measurements_per_minute"),
    ),
)

# Pulse (live) ----------------------------------------------------------------


def _phase_load(phase: int) -> Callable[[TibberLiveCoordinator, Data], float | None]:
    def value(live: TibberLiveCoordinator, data: Data) -> float | None:
        current = data.get(f"currentPhase{phase}")
        fuse = dig(live.main.data, "pulse", "settings", "mainFuseSize")
        if current is None or not fuse:
            return None
        return round(current / float(fuse) * 100, 1)

    return value


def _has_phases(main: Main) -> bool:
    return main.has_phase_currents


LIVE_SENSORS: tuple[TibberSensorEntityDescription, ...] = (
    TibberSensorEntityDescription(
        key="power",
        translation_key="power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda c, d: d.get("power"),
        attrs_fn=lambda c, d: {
            "timestamp": d["timestamp"].isoformat() if d.get("timestamp") else None,
            "source": d.get("source"),
            "recent": d.get("recent"),
        },
    ),
    TibberSensorEntityDescription(
        key="live_consumption_today",
        translation_key="live_consumption_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda c, d: _r(d.get("accumulatedConsumption")),
    ),
    TibberSensorEntityDescription(
        key="live_cost_today",
        translation_key="live_cost_today",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        period="day",
        value_fn=lambda c, d: _r(d.get("accumulatedCost")),
    ),
    TibberSensorEntityDescription(
        key="consumption_current_hour",
        translation_key="consumption_current_hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda c, d: _r(d.get("accumulatedConsumptionCurrentHour")),
    ),
    TibberSensorEntityDescription(
        key="estimated_consumption_current_hour",
        translation_key="estimated_consumption_current_hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value_fn=lambda c, d: _r(d.get("estimatedAccumulatedConsumptionCurrentHour")),
        attrs_fn=lambda c, d: {"hourly_limit": d.get("effectiveHourlyConsumptionLimit")},
    ),
    TibberSensorEntityDescription(
        key="average_power_today",
        translation_key="average_power_today",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda c, d: d.get("averagePower"),
    ),
    TibberSensorEntityDescription(
        key="peak_control_state",
        translation_key="peak_control_state",
        value_fn=lambda c, d: s.lower() if (s := d.get("peakControlConsumptionState")) else None,
    ),
    TibberSensorEntityDescription(
        key="power_production",
        translation_key="power_production",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda c, d: d.get("powerProduction"),
    ),
    *(
        TibberSensorEntityDescription(
            key=f"current_l{n}",
            translation_key=f"current_l{n}",
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            exists_fn=_has_phases,
            value_fn=lambda c, d, n=n: d.get(f"currentPhase{n}"),
        )
        for n in (1, 2, 3)
    ),
    *(
        TibberSensorEntityDescription(
            key=f"voltage_l{n}",
            translation_key=f"voltage_l{n}",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
            exists_fn=_has_phases,
            value_fn=lambda c, d, n=n: d.get(f"voltagePhase{n}"),
        )
        for n in (1, 2, 3)
    ),
    *(
        TibberSensorEntityDescription(
            key=f"phase_load_l{n}",
            translation_key=f"phase_load_l{n}",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
            exists_fn=_has_phases,
            value_fn=_phase_load(n),
        )
        for n in (1, 2, 3)
    ),
)

# Today's max/min combine the push, minute history and live samples.
EXTREME_SENSORS: tuple[TibberSensorEntityDescription, ...] = tuple(
    TibberSensorEntityDescription(
        key=f"today_{which}_power",
        translation_key=f"today_{which}_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        value_fn=lambda c, d, which=which: round(e[0]) if (e := c.extreme(which)) else None,
        attrs_fn=lambda c, d, which=which: {
            "at": e[1].isoformat() if (e := c.extreme(which)) else None
        },
    )
    for which in ("max", "min")
)

# Vehicles --------------------------------------------------------------------

VEHICLE_SENSORS: tuple[TibberSensorEntityDescription, ...] = (
    TibberSensorEntityDescription(
        key="battery_level",
        unique_key="vehicle_batteryLevel",
        translation_key="vehicle_battery_level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        # Live value; before the first push, the level last set in Tibber.
        value_fn=lambda v: v.get("battery_level", v["settings"].get("batteryLevel")),
        attrs_fn=lambda v: {
            "read_from_car": v.get("battery_level_known"),
            "range_km": v.get("range_km"),
        },
    ),
    TibberSensorEntityDescription(
        key="battery_capacity",
        unique_key="vehicle_batteryCapacity",
        translation_key="vehicle_battery_capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        value_fn=lambda v: v["settings"].get("batteryCapacity"),
    ),
    TibberSensorEntityDescription(
        key="grid_reward_state",
        translation_key="flex_grid_reward_state",
        device_class=SensorDeviceClass.ENUM,
        options=GRID_STATES,
        value_fn=lambda v: v["grid"].get("grid_state"),
        attrs_fn=lambda v: {"reasons": v["grid"].get("grid_reasons")},
    ),
    TibberSensorEntityDescription(
        key="charging_status",
        translation_key="vehicle_charging_status",
        value_fn=lambda v: v.get("charging_status"),
        attrs_fn=lambda v: {"status": v.get("status"), "is_at_home": v.get("is_at_home")},
    ),
    TibberSensorEntityDescription(
        key="charge_state",
        translation_key="vehicle_charge_state",
        value_fn=lambda v: v.get("state"),
        attrs_fn=lambda v: {
            k: v.get(k) for k in ("mode", "reason", "status", "session_started", "plan")
        },
    ),
    TibberSensorEntityDescription(
        key="charge_power",
        translation_key="vehicle_charge_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda v: (
            (v.get("power_kw") if v.get("state") == "charging" else 0.0) if "state" in v else None
        ),
        attrs_fn=lambda v: {"session_average_power_kw": v.get("average_power_kw")},
    ),
    TibberSensorEntityDescription(
        key="target_soc",
        translation_key="vehicle_target_soc",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda v: v.get("target_soc"),
    ),
    TibberSensorEntityDescription(
        key="departure",
        translation_key="vehicle_departure",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda v: parsers.parse_dt(v.get("departure")),
    ),
    TibberSensorEntityDescription(
        key="session_energy",
        translation_key="vehicle_session_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value_fn=lambda v: v.get("session_kwh"),
    ),
    TibberSensorEntityDescription(
        key="session_cost",
        translation_key="vehicle_session_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda v: v.get("session_cost"),
        attrs_fn=lambda v: {"grid_rewards": v.get("session_grid_rewards")},
    ),
    TibberSensorEntityDescription(
        key="session_average_price",
        translation_key="vehicle_session_average_price",
        unit=UNIT_PRICE,
        suggested_display_precision=2,
        value_fn=lambda v: v.get("session_average_price"),
    ),
)

# Chargers --------------------------------------------------------------------

CHARGER_SENSORS: tuple[TibberSensorEntityDescription, ...] = (
    TibberSensorEntityDescription(
        key="month_kwh",
        unique_key="charger_kwh",
        translation_key="charger_month_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        period="month",
        value_fn=lambda c: _r(c["month"].get("kwh"), 2),
        attrs_fn=lambda c: {"days": c["month"].get("days")},
    ),
    TibberSensorEntityDescription(
        key="month_cost",
        unique_key="charger_cost",
        translation_key="charger_month_cost",
        unit=UNIT_MONEY,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        period="month",
        value_fn=lambda c: _r(c["month"].get("cost"), 2),
    ),
    TibberSensorEntityDescription(
        key="month_price",
        unique_key="charger_price",
        translation_key="charger_month_price",
        unit=UNIT_PRICE,
        suggested_display_precision=2,
        value_fn=lambda c: (
            round(c["month"]["cost"] / c["month"]["kwh"], 4)
            if c["month"].get("kwh") and c["month"].get("cost") is not None
            else None
        ),
    ),
    TibberSensorEntityDescription(
        key="charger_status",
        translation_key="charger_status",
        value_fn=lambda c: c.get("status"),
        attrs_fn=lambda c: {
            "description": c.get("status_description"),
            **{k: c.get(k) for k in ("charging_status", "connectivity", "mode", "state", "reason")},
            # The car connected to this charger: its session and charging plan.
            "vehicle": c["vehicle"].get("name"),
            "vehicle_battery_level": c["vehicle"].get("battery_level"),
            "vehicle_target_soc": c["vehicle"].get("target_soc"),
            "vehicle_departure": c["vehicle"].get("departure"),
            "session_kwh": c["vehicle"].get("session_kwh"),
            "session_cost": c["vehicle"].get("session_cost"),
            "plan": c["vehicle"].get("plan") or [],
        },
    ),
    TibberSensorEntityDescription(
        key="charger_charging_status",
        translation_key="charger_charging_status",
        value_fn=lambda c: c.get("charging_status"),
    ),
    TibberSensorEntityDescription(
        key="charger_power",
        translation_key="charger_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda c: (
            (c.get("power_kw") if c.get("state") == "charging" else 0.0) if "state" in c else None
        ),
    ),
    TibberSensorEntityDescription(
        key="charger_max_current",
        translation_key="charger_max_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        value_fn=lambda c: (c.get("settings") or {}).get("maxCurrentCharger"),
    ),
    TibberSensorEntityDescription(
        key="charger_last_seen",
        translation_key="charger_last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: parsers.parse_dt(c.get("last_seen")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TibberAppConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for what this home actually has."""
    runtime = entry.runtime_data
    main = runtime.main
    entities: list[SensorEntity] = [
        TibberSensor(main, d, HOME) for d in HOME_SENSORS if d.exists_fn(main)
    ]
    if main.pulse_id:
        entities += [TibberSensor(main, d, PULSE) for d in PULSE_SENSORS]
    if runtime.live:
        entities += [TibberLiveSensor(runtime.live, d) for d in LIVE_SENSORS if d.exists_fn(main)]
        entities += [TibberLiveSensor(runtime.live, d, extra=True) for d in EXTREME_SENSORS]
    for vehicle_id in main.vehicle_ids():
        entities += [TibberItemSensor(main, d, "vehicle", vehicle_id) for d in VEHICLE_SENSORS]
    for charger in main.charger_ids:
        entities += [TibberItemSensor(main, d, "charger", charger["id"]) for d in CHARGER_SENSORS]
    async_add_entities(entities)


class _DescribedSensor(SensorEntity):
    """Value, attributes, unit and last_reset come from the description."""

    entity_description: TibberSensorEntityDescription
    main: TibberAppCoordinator

    def _args(self) -> tuple[Any, ...]:
        raise NotImplementedError

    @property
    def native_value(self) -> Any:
        """State."""
        return self.entity_description.value_fn(*self._args())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes."""
        if fn := self.entity_description.attrs_fn:
            return fn(*self._args())
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Unit (currency based units are resolved at runtime)."""
        description = self.entity_description
        return resolve_unit(description.unit, self.main.currency) or (
            description.native_unit_of_measurement
        )

    @property
    def last_reset(self) -> datetime | None:
        """Start of the current period for TOTAL sensors."""
        return period_start(self.main, self.entity_description.period)


class TibberSensor(TibberAppEntity[TibberAppCoordinator], _DescribedSensor):
    """Home or Pulse sensor backed by the main coordinator."""

    def __init__(
        self, main: TibberAppCoordinator, description: TibberSensorEntityDescription, device: str
    ) -> None:
        """Initialize."""
        super().__init__(main, main, description.unique_key or description.key, device)
        self.entity_description = description

    def _args(self) -> tuple[Any, ...]:
        return self.main, self.coordinator.data or {}


class TibberLiveSensor(TibberAppEntity[TibberLiveCoordinator], _DescribedSensor):
    """Live Pulse sensor."""

    def __init__(
        self,
        live: TibberLiveCoordinator,
        description: TibberSensorEntityDescription,
        extra: bool = False,
    ) -> None:
        """Initialize (`extra`: also update on main coordinator refreshes)."""
        super().__init__(
            live,
            live.main,
            description.unique_key or description.key,
            PULSE,
            also_listen=(live.main,) if extra else (),
        )
        self.entity_description = description
        self._extra = extra

    def _args(self) -> tuple[Any, ...]:
        return self.coordinator, self.coordinator.data or {}

    @property
    def available(self) -> bool:
        """Unavailable while the Pulse isn't reporting (max/min stay)."""
        if self._extra:
            return True
        return super().available and not (self.coordinator.data or {}).get("stale", True)


class TibberItemSensor(TibberAppEntity[TibberAppCoordinator], _DescribedSensor):
    """Sensor for one vehicle or charger (polled data plus pushed state)."""

    def __init__(
        self,
        main: TibberAppCoordinator,
        description: TibberSensorEntityDescription,
        kind: str,
        item_id: str,
    ) -> None:
        """Initialize."""
        super().__init__(
            main,
            main,
            description.unique_key or description.key,
            f"{kind}:{item_id}",
            also_listen=(main.devices,),
        )
        self.entity_description = description
        self._item = vehicle_item if kind == "vehicle" else charger_item
        self._item_id = item_id

    def _args(self) -> tuple[Any, ...]:
        return (self._item(self.main, self._item_id),)
