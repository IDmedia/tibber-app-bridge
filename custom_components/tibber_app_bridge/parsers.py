"""Pure helpers that turn app API responses into flat structures.

Kept free of Home Assistant imports so they can be tested against a capture.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
import itertools
import re
from typing import Any

_DBM_RE = re.compile(r"(-?\d+)\s*dBm")
_KWH_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*kW[ht]")


def parse_dt(value: str | None) -> datetime | None:
    """Parse the ISO strings the API returns (with or without millis)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def settings_dict(settings: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    """Settings list -> {key: value}."""
    return {s["key"]: s.get("value") for s in settings or [] if s.get("key")}


def flatten_layout(items: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten the nested settingsLayout tree."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        out.append(item)
        out.extend(flatten_layout(item.get("childItems")))
    return out


def pulse_status(pulse: dict[str, Any]) -> dict[str, Any]:
    """Extract diagnostics shown on the Pulse settings screen.

    The labels are localized, so values are matched by shape and position
    rather than by title.
    """
    status: dict[str, Any] = {"info": {}}
    for item in flatten_layout(pulse.get("settingsLayout")):
        text = item.get("valueText")
        if item.get("type") != "info" or not text:
            continue
        status["info"][item.get("title") or item.get("uid")] = text
        if (m := _DBM_RE.search(text)) and "wifi_rssi" not in status:
            status["wifi_rssi"] = int(m.group(1))
        elif re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}", text.strip()):
            status.setdefault("last_seen_text", text.strip())
        elif re.fullmatch(r"\d+\.\d+\.\d+", text.strip()):
            status.setdefault("firmware", text.strip())
    return status


def first_kwh_line(description: str | None, marker: str = "3") -> float | None:
    """Find "average of the 3 highest hours: 7,88 kWh" style values."""
    if not description:
        return None
    for line in description.splitlines():
        if f" {marker} " not in f" {line} ":
            continue
        matches = _KWH_RE.findall(line)
        if matches:
            return float(matches[-1].replace(",", "."))
    return None


# Grid Rewards (subscription payloads) --------------------------------------

_GRID_STATES = {
    "GridRewardAvailable": "available",
    "GridRewardUnavailable": "unavailable",
    "GridRewardDelivering": "delivering",
}


def _grid_state(state: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    return {
        "state": _GRID_STATES.get(state.get("__typename"), "unknown"),
        "reasons": state.get("reasons") or ([state["reason"]] if state.get("reason") else []),
        "kind": state.get("kind"),
        "title": state.get("sheetTitle"),
        "description": state.get("sheetDescription"),
    }


def grid_reward(data: dict[str, Any]) -> dict[str, Any] | None:
    """gridRewardStatus / gridRewardStateTransitions payload -> flat dict."""
    reward = data.get("gridRewardStatus") or data.get("gridRewardStateTransitions")
    if not reward:
        return None
    devices = []
    for dev in reward.get("flexDevices") or []:
        devices.append(
            {
                "kind": dev.get("kind"),
                "id": dev.get("vehicleId") or dev.get("batteryId"),
                "name": dev.get("shortName") or dev.get("make"),
                "plugged_in": dev.get("isPluggedIn"),
                "smart_charging": dev.get("isSmartChargingEnabled", dev.get("isSmartModeEnabled")),
                **{f"grid_{k}": v for k, v in _grid_state(dev.get("state")).items()},
            }
        )
    return {
        **_grid_state(reward.get("state")),
        "currency": reward.get("rewardCurrency"),
        "month": reward.get("rewardCurrentMonth"),
        "all_time": reward.get("rewardAllTime"),
        "devices": devices,
    }


# Chargers and vehicles ---------------------------------------------------


def charge_session(session: dict[str, Any] | None) -> dict[str, Any]:
    """Manual/smart session -> {mode, state, reason, power_kw}."""
    if not session:
        return {"mode": None, "state": None, "reason": None, "power_kw": None}
    state = (
        session.get("smartChargingSessionState") or session.get("manualChargeSessionState") or {}
    )
    return {
        "mode": "smart" if session.get("kind") == "smart_charging_session" else "manual",
        "state": state.get("kind"),
        "reason": state.get("chargingReason")
        or state.get("notChargingReason")
        or state.get("errorReason"),
        "power_kw": state.get("currentChargePower"),
    }


def vehicle_charger(charger: dict[str, Any]) -> dict[str, Any]:
    """VehicleCharger fragment -> flat dict."""
    return {
        "id": charger.get("id"),
        "name": charger.get("name"),
        "is_alive": charger.get("isAlive"),
        "charging_status": charger.get("chargingStatus"),
        "status": (charger.get("status") or {}).get("title"),
        "status_description": (charger.get("status") or {}).get("description"),
        "last_seen": charger.get("lastSeen"),
        "connectivity": (charger.get("connectivity") or {}).get("kind"),
        "settings": settings_dict(charger.get("userSettings")),
        "preferred_vehicle": next(
            (v.get("id") for v in charger.get("vehicles") or [] if v.get("isPreferred")), None
        ),
        **charge_session(charger.get("chargeSession")),
    }


def vehicle(data: dict[str, Any]) -> dict[str, Any]:
    """Vehicle fragment -> flat dict."""
    charging = data.get("charging") or {}
    summary = charging.get("summary") or {}
    battery = data.get("battery") or {}
    periods = [p for d in charging.get("days") or [] for p in d.get("periods") or []]
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "is_alive": data.get("isAlive"),
        "charging_status": data.get("chargingStatus"),
        "smart_charging": data.get("smartChargingStatus") == "on",
        "is_at_home": data.get("isAtHome"),
        "status": (data.get("status") or {}).get("title"),
        "battery_level": battery.get("level"),
        "battery_level_known": battery.get("canReadLevel"),
        "range_km": battery.get("estimatedRange") or None,
        "target_soc": charging.get("targetedStateOfCharge"),
        "departure": charging.get("targetedDepartureTime"),
        "session_started": charging.get("sessionStartedAt"),
        "charger_id": charging.get("chargerId"),
        "session_kwh": summary.get("energyConsumed"),
        "session_cost": summary.get("totalBilled"),
        "session_grid_rewards": summary.get("gridRewards"),
        "session_average_price": summary.get("averagePrice"),
        "average_power_kw": summary.get("averageChargePower"),
        "currency": charging.get("currency"),
        "plan": [
            {
                "start": p.get("startAt"),
                "end": p.get("stopAt"),
                "type": p.get("type"),
                "kwh": p.get("energy"),
                "cost": p.get("cost"),
                "soc_from": p.get("fromStateOfCharge"),
                "soc_to": p.get("toStateOfCharge"),
                # Minor units (øre) per kWh.
                "price": p.get("averagePrice"),
            }
            for p in periods
        ],
        **charge_session(charging.get("session")),
    }


# Prices ------------------------------------------------------------------


def price_entries(
    period: dict[str, Any] | None, include_grid: bool, slot: timedelta
) -> list[dict[str, Any]]:
    """Normalize priceRating entries.

    `total` from the API is energy + VAT + markup. The app shows
    total + gridPrice when the grid company provides data.
    """
    out = []
    for entry in (period or {}).get("entries") or []:
        start = parse_dt(entry.get("time"))
        if start is None or entry.get("total") is None:
            continue
        grid = entry.get("gridPrice") or 0.0
        out.append(
            {
                "start": start,
                "end": start + slot,
                "energy": entry.get("energy"),
                "total": entry["total"],
                "grid": entry.get("gridPrice"),
                # State-subsidised price where the market has one (e.g. Norgespris).
                "subsidised": entry.get("compensatedTotal"),
                "price": round(entry["total"] + (grid if include_grid else 0.0), 4),
            }
        )
    return out


def classify_prices(entries: list[dict[str, Any]], low_pct: float, high_pct: float) -> None:
    """Add a cheap/normal/expensive level relative to each day's average."""
    by_day: dict[date, list[dict[str, Any]]] = {}
    for e in entries:
        by_day.setdefault(e["start"].date(), []).append(e)
    for day in by_day.values():
        avg = sum(e["price"] for e in day) / len(day)
        for e in day:
            diff = (e["price"] - avg) / avg * 100 if avg else 0.0
            e["level"] = (
                "cheap" if diff <= low_pct else "expensive" if diff >= high_pct else "normal"
            )
            e["diff_pct"] = round(diff, 1)


def price_at(entries: list[dict[str, Any]], when: datetime) -> dict[str, Any] | None:
    """Entry covering `when`."""
    for e in entries:
        if e["start"] <= when < e["end"]:
            return e
    return None


def day_summary(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Min/max/avg for a list of price entries."""
    if not entries:
        return None
    prices = [e["price"] for e in entries]
    lo = min(entries, key=lambda e: e["price"])
    hi = max(entries, key=lambda e: e["price"])
    return {
        "min": lo["price"],
        "min_at": lo["start"],
        "max": hi["price"],
        "max_at": hi["start"],
        "avg": round(sum(prices) / len(prices), 4),
    }


def upcoming_prices(
    entries: list[dict[str, Any]], now: datetime, count: int = 12
) -> list[dict[str, Any]]:
    """The current slot and the next ones (`now` is True for the current one)."""
    upcoming = [e for e in entries if e["end"] > now][:count]
    return [
        {**item, "now": e["start"] <= now < e["end"]}
        for item, e in zip(serialize_prices(upcoming), upcoming, strict=True)
    ]


def connected_vehicle(
    vehicles: dict[str, dict[str, Any]], charger_id: str
) -> dict[str, Any] | None:
    """The vehicle currently connected to a charger (charging ones first)."""
    candidates = [
        v
        for v in vehicles.values()
        if v.get("charger_id") == charger_id
        and v.get("charging_status") not in (None, "disconnected")
    ]
    candidates.sort(key=lambda v: v.get("state") != "charging")
    return candidates[0] if candidates else None


def serialize_prices(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact representation for state attributes / charts."""
    return [
        {
            "start": e["start"].isoformat(),
            "price": e["price"],
            "energy": e["energy"],
            "grid": e["grid"],
            "level": e.get("level"),
        }
        for e in entries
    ]


# Consumption ---------------------------------------------------------------


def analysis_item(home: dict[str, Any]) -> dict[str, Any] | None:
    """First consumptionAnalysisItems element."""
    items = home.get("consumptionAnalysisItems") or []
    return items[0] if items else None


def chart_series(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Chart items -> [{start, kwh, cost, price}] with missing values kept as None."""
    out = []
    for c in ((item or {}).get("chart") or {}).get("items") or []:
        out.append(
            {
                "start": c["from"],
                "kwh": _round(c.get("totalConsumption"), 3),
                "cost": _round(c.get("totalCost"), 2),
                # API price is in minor units (øre/öre/cent) per kWh.
                "price": _round(c.get("price"), 2),
            }
        )
    return out


def hourly_points(item: dict[str, Any] | None) -> list[tuple[datetime, float, float]]:
    """(start, kWh, cost) for hours that have data."""
    out = []
    for c in ((item or {}).get("chart") or {}).get("items") or []:
        start = parse_dt(c.get("from"))
        if start is None or c.get("totalConsumption") is None:
            continue
        out.append((start, float(c["totalConsumption"]), float(c.get("totalCost") or 0.0)))
    return out


def top_three(daily_peaks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Capacity tariff: average of the three highest hours on different days."""
    peaks = sorted(daily_peaks.values(), key=lambda p: p["kwh"], reverse=True)[:3]
    if not peaks:
        return None
    return {
        "average": round(sum(p["kwh"] for p in peaks) / len(peaks), 3),
        "peaks": peaks,
    }


def capacity_step(value: float | None, steps: list[float]) -> dict[str, Any] | None:
    """Map a kWh/h value onto the tariff step list (upper bounds)."""
    if value is None or not steps:
        return None
    lower = 0.0
    for index, upper in enumerate(steps, start=1):
        if value < upper:
            return {"step": index, "lower": lower, "upper": upper}
        lower = upper
    return {"step": len(steps) + 1, "lower": lower, "upper": None}


def capacity_outlook(
    daily_peaks: dict[str, dict[str, Any]], today: str, steps: list[float]
) -> dict[str, Any] | None:
    """What this hour can use before it changes the capacity tariff.

    * `threshold`: an hour above this becomes one of the month's 3 peaks (it
      must beat today's own peak and the lowest of the other days' top 3).
    * `to_next_step`: the hour consumption that would push the top-3 average
      into the next tariff step (None without steps or in the top step).
    """
    today_peak = (daily_peaks.get(today) or {}).get("kwh") or 0.0
    others = sorted((p["kwh"] for day, p in daily_peaks.items() if day != today), reverse=True)
    threshold = max(today_peak, others[2]) if len(others) >= 3 else today_peak
    result: dict[str, Any] = {"threshold": round(threshold, 3), "to_next_step": None}

    current = top_three(daily_peaks)
    step = capacity_step(current["average"] if current else 0.0, steps)
    if step and step["upper"] is not None:
        # New top 3 = this hour + the two highest other days (padded with 0).
        best_two = ([*others, 0.0, 0.0])[:2]
        needed = 3 * step["upper"] - sum(best_two)
        result |= {
            "to_next_step": round(max(needed, threshold), 3),
            "next_step": step["step"] + 1,
            "next_step_lower": step["upper"],
        }
    return result


def cheapest_window(
    entries: list[dict[str, Any]], now: datetime, hours: int = 3
) -> dict[str, Any] | None:
    """Cheapest run of consecutive slots covering `hours`, starting from now."""
    upcoming = [e for e in entries if e["end"] > now]
    if not upcoming:
        return None
    slot = upcoming[0]["end"] - upcoming[0]["start"]
    count = max(int(timedelta(hours=hours) / slot), 1)
    best: tuple[float, int] | None = None
    for i in range(len(upcoming) - count + 1):
        window = upcoming[i : i + count]
        # Only contiguous slots (no gap before tomorrow's prices arrive).
        if any(b["start"] != a["end"] for a, b in itertools.pairwise(window)):
            continue
        avg = sum(e["price"] for e in window) / count
        if best is None or avg < best[0]:
            best = (avg, i)
    if best is None:
        return None
    start = upcoming[best[1]]
    return {
        "start": start["start"],
        "end": upcoming[best[1] + count - 1]["end"],
        "average": round(best[0], 4),
        "hours": hours,
    }


def parse_steps(text: str) -> list[float]:
    """ "2,5,10" -> [2.0, 5.0, 10.0] (accepts ; and whitespace too)."""
    values = []
    for part in re.split(r"[;,\s]+", text or ""):
        if part:
            values.append(float(part))
    return sorted(values)


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


# Summaries of whole responses ------------------------------------------------


def period_summary(item: dict[str, Any] | None) -> dict[str, Any]:
    """consumptionAnalysisItems[0] -> totals, forecast and chart series."""
    if not item:
        return {}
    net = item.get("netConsumption") or {}
    est = item.get("estimatedTotalConsumption") or {}
    return {
        "from": item.get("from"),
        "to": item.get("to"),
        "kwh": _round(net.get("consumption"), 3),
        "cost": _round(net.get("cost"), 2),
        "estimated_kwh": _round(est.get("consumption"), 1),
        "estimated_cost": _round(est.get("cost"), 2),
        "top3_average": first_kwh_line(item.get("description")),
        "description": item.get("description"),
        "series": chart_series(item),
        "currency": item.get("currency"),
    }


_EFFICIENCY_STARS = {"below average": 1, "average": 2, "above average": 3, "efficient": 3}


def comparison(home: dict[str, Any]) -> dict[str, Any]:
    """comparisonAnalysisItems -> home vs. average vs. efficient homes."""
    items = home.get("comparisonAnalysisItems") or []
    if not items:
        return {}
    c = items[0]
    mine = (c.get("home") or {}).get("consumption")
    avg = (c.get("average") or {}).get("consumption")
    efficiency = c.get("homeEfficency")  # sic, spelled like this in the API
    return {
        "from": c.get("from"),
        "efficiency": efficiency,
        "description": c.get("homeEfficencyDescription"),
        "home_kwh": mine,
        "average_kwh": avg,
        "efficient_kwh": (c.get("efficient") or {}).get("consumption"),
        "diff_pct": round((mine - avg) / avg * 100, 1) if mine is not None and avg else None,
        "stars": _EFFICIENCY_STARS.get((efficiency or "").lower()),
    }


def price_performance(home: dict[str, Any]) -> dict[str, Any]:
    """pricePerformanceAnalysisItems -> your average price vs. others."""
    items = home.get("pricePerformanceAnalysisItems") or []
    if not items:
        return {}
    i = items[0]
    cmp = i.get("comparison") or {}
    rewards = sum(
        d.get("absoluteValue") or 0
        for h in i.get("highlights") or []
        for d in h.get("details") or []
        if d.get("type") == "gridRewards"
    )
    return {
        "from": i.get("from"),
        "to": i.get("to"),
        "average_price": _round(i.get("homeAveragePrice"), 4),
        "low": _round(cmp.get("minAveragePrice"), 4),
        "average": _round(cmp.get("midAveragePrice"), 4),
        "high": _round(cmp.get("maxAveragePrice"), 4),
        "price_area": i.get("priceArea"),
        "disaggregation": {
            k: {"average_price": _round(v.get("averagePrice"), 4), "kwh": v.get("consumption")}
            for k, v in (i.get("disaggregation") or {}).items()
            if isinstance(v, dict)
        },
        "grid_rewards_reduction": _round(rewards, 4),
    }


def rewards_period(home: dict[str, Any]) -> dict[str, Any]:
    """gridRewardsHistoryPeriod -> totals and chart series."""
    period = home.get("gridRewardsHistoryPeriod") or {}
    return {
        "from": period.get("from"),
        "total": _round(period.get("totalReward"), 2),
        "vehicle": _round(period.get("vehicleRewards"), 2),
        "battery": _round(period.get("batteryRewards"), 2),
        "series": [
            {"start": c.get("from"), "total": _round(c.get("totalReward"), 3)}
            for c in (period.get("chart") or {}).get("items") or []
        ],
    }


def cost_disaggregation(home: dict[str, Any]) -> dict[str, Any]:
    """costDisaggregationAnalysisItems -> what used the most."""
    item = (home.get("costDisaggregationAnalysisItems") or [None])[0] or {}
    return {
        "from": item.get("from"),
        "title": item.get("title"),
        "total": item.get("total"),
        "total_kwh_text": item.get("totalDescription"),
        "items": [
            {k: i.get(k) for k in ("type", "title", "consumption", "cost", "percent")}
            for i in item.get("items") or []
        ],
    }


def fuse_overloads(home: dict[str, Any]) -> list[dict[str, Any]]:
    """fuseLoadExcess history, newest first, with the overloaded phase."""
    items = ((home.get("fuseLoadExcess") or {}).get("history") or {}).get("items") or []
    out = []
    for i in items:
        phases = {
            f"l{n}": i.get(f"currentPhase{n}")
            for n in (1, 2, 3)
            if i.get(f"currentPhase{n}") is not None
        }
        top = max(phases, key=lambda k: phases[k]) if phases else None
        out.append(
            {
                "start": i.get("startedAt"),
                "end": i.get("endedAt"),
                **{k: i.get(f"currentPhase{k[1]}") for k in ("l1", "l2", "l3")},
                "fuse": i.get("mainFuseSize"),
                "phase": top.upper() if top else None,
                "current": phases.get(top) if top else None,
            }
        )
    out.sort(key=lambda o: o["start"] or "", reverse=True)
    return out


def pulse_info(pulse: dict[str, Any]) -> dict[str, Any]:
    """PulseQuery -> flat Pulse state, settings and diagnostics."""
    return {
        "id": pulse.get("id"),
        "name": pulse.get("name"),
        "is_alive": pulse.get("isAlive"),
        "has_phase_currents": pulse.get("hasPhaseCurrents"),
        "peak_control_status": pulse.get("peakControlStatus"),
        "measurements_per_minute": pulse.get("estimatedMeasurementsPerMinute"),
        "error": (pulse.get("error") or {}).get("title"),
        "warning": (pulse.get("warning") or {}).get("title"),
        "settings": settings_dict(pulse.get("settings2")),
        **pulse_status(pulse),
    }


def my_vehicles(me: dict[str, Any]) -> list[dict[str, Any]]:
    """GetMyVehicles -> [{id, title, description, paired, settings}]."""
    return [
        {
            "id": v["id"],
            "title": v.get("title"),
            "description": v.get("description"),
            "paired": v.get("isPaired"),
            "settings": settings_dict((v.get("detailsScreen") or {}).get("settings")),
        }
        for v in ((me.get("myVehicles") or {}).get("vehicles") or [])
        if v.get("id")
    ]
