"""Parsers turn app responses into flat data (synthetic payloads)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from tibber_app_bridge import parsers


def _hours(day: str, totals: list[float], grid: float = 0.45) -> dict:
    return {
        "currency": "NOK",
        "entries": [
            {
                "time": f"{day}T{h:02d}:00:00.000+01:00",
                "energy": t * 0.8,
                "total": t,
                "gridPrice": grid,
                "compensatedTotal": 0.5,
            }
            for h, t in enumerate(totals)
        ],
    }


def test_price_entries_include_grid_price() -> None:
    entries = parsers.price_entries(_hours("2026-11-10", [3.9567]), True, timedelta(hours=1))
    assert entries[0]["price"] == pytest.approx(4.4067)
    assert entries[0]["subsidised"] == 0.5
    assert entries[0]["end"] - entries[0]["start"] == timedelta(hours=1)
    without = parsers.price_entries(_hours("2026-11-10", [3.9567]), False, timedelta(hours=1))
    assert without[0]["price"] == pytest.approx(3.9567)


def test_classify_prices_relative_to_day_average() -> None:
    entries = parsers.price_entries(
        _hours("2026-11-10", [1.0, 2.0, 3.0]), False, timedelta(hours=1)
    )
    parsers.classify_prices(entries, -10, 10)
    assert [e["level"] for e in entries] == ["cheap", "normal", "expensive"]
    current = parsers.price_at(entries, entries[1]["start"] + timedelta(minutes=30))
    assert current is entries[1]
    assert parsers.day_summary(entries)["avg"] == 2.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Forbruket er timesavlest.\nSnitt av de 3 høyeste timene: 7,88 kWt", 7.88),
        ("Average of the 3 highest hours: 7.88 kWh", 7.88),
        ("Forventet forbruk: 4 833 kr (2 094 kWt).", None),
        (None, None),
    ],
)
def test_top3_from_description(text: str | None, expected: float | None) -> None:
    assert parsers.first_kwh_line(text) == expected


def test_capacity_steps() -> None:
    steps = parsers.parse_steps("2, 5;10 15")
    assert steps == [2, 5, 10, 15]
    assert parsers.capacity_step(7.88, steps) == {"step": 3, "lower": 5, "upper": 10}
    assert parsers.capacity_step(20, steps)["upper"] is None
    assert parsers.capacity_step(None, steps) is None
    assert parsers.parse_steps("") == []
    with pytest.raises(ValueError):
        parsers.parse_steps("2,x")


def test_top_three_uses_one_peak_per_day() -> None:
    peaks = {
        f"2026-11-0{d}": {"date": f"2026-11-0{d}", "start": "", "kwh": kwh}
        for d, kwh in ((1, 5.0), (2, 9.0), (3, 7.0), (4, 2.0))
    }
    assert parsers.top_three(peaks)["average"] == 7.0
    assert parsers.top_three({}) is None


def test_grid_reward() -> None:
    parsed = parsers.grid_reward(
        {
            "gridRewardStatus": {
                "state": {
                    "__typename": "GridRewardUnavailable",
                    "reasons": ["noVehiclesPluggedIn"],
                },
                "rewardCurrency": "NOK",
                "rewardCurrentMonth": 7.85,
                "rewardAllTime": 509.78,
                "flexDevices": [
                    {
                        "kind": "vehicle",
                        "vehicleId": "v1",
                        "shortName": "Car",
                        "isPluggedIn": False,
                        "isSmartChargingEnabled": True,
                        "state": {"__typename": "GridRewardDelivering", "reason": "charging"},
                    },
                ],
            }
        }
    )
    assert parsed["state"] == "unavailable"
    assert parsed["reasons"] == ["noVehiclesPluggedIn"]
    assert parsed["devices"][0]["grid_state"] == "delivering"
    assert parsed["devices"][0]["grid_reasons"] == ["charging"]
    assert parsers.grid_reward({}) is None


def test_vehicle_and_charger() -> None:
    session = {
        "kind": "smart_charging_session",
        "smartChargingSessionState": {
            "kind": "charging",
            "chargingReason": "low_price",
            "currentChargePower": 1.85,
        },
    }
    vehicle = parsers.vehicle(
        {
            "id": "v1",
            "name": "Car",
            "chargingStatus": "charging",
            "smartChargingStatus": "on",
            "battery": {"level": 75, "estimatedRange": 0, "canReadLevel": False},
            "charging": {
                "targetedStateOfCharge": 100,
                "summary": {"energyConsumed": 0.65, "totalBilled": 2.47},
                "days": [
                    {
                        "periods": [
                            {"startAt": "a", "stopAt": "b", "type": "charging", "energy": 1.4}
                        ]
                    }
                ],
                "session": session,
            },
        }
    )
    assert vehicle["battery_level"] == 75
    assert vehicle["range_km"] is None  # 0 means unknown
    assert vehicle["smart_charging"] is True
    assert (vehicle["mode"], vehicle["state"], vehicle["power_kw"]) == ("smart", "charging", 1.85)
    assert vehicle["plan"][0]["kwh"] == 1.4

    charger = parsers.vehicle_charger(
        {
            "id": "c1",
            "isAlive": True,
            "status": {"title": "Load balancing active"},
            "userSettings": [{"key": "maxCurrentCharger", "value": 8}],
            "chargeSession": None,
        }
    )
    assert charger["settings"]["maxCurrentCharger"] == 8
    assert charger["state"] is None


def test_fuse_overloads_newest_first_with_phase() -> None:
    overloads = parsers.fuse_overloads(
        {
            "fuseLoadExcess": {
                "history": {
                    "items": [
                        {
                            "startedAt": "2026-11-03T16:44:44+01:00",
                            "currentPhase1": 4,
                            "currentPhase2": 3,
                            "currentPhase3": 44.5,
                            "mainFuseSize": 40,
                        },
                        {
                            "startedAt": "2026-11-09T15:27:14+01:00",
                            "currentPhase1": 41,
                            "currentPhase2": 3,
                            "currentPhase3": None,
                            "mainFuseSize": 40,
                        },
                    ]
                }
            }
        }
    )
    assert [o["phase"] for o in overloads] == ["L1", "L3"]
    assert overloads[0]["current"] == 41


def test_comparison_and_period_summary() -> None:
    comparison = parsers.comparison(
        {
            "comparisonAnalysisItems": [
                {
                    "homeEfficency": "below average",
                    "home": {"consumption": 640},
                    "average": {"consumption": 481},
                    "efficient": {"consumption": 309},
                }
            ]
        }
    )
    assert comparison["diff_pct"] == 33.1
    assert comparison["stars"] == 1
    summary = parsers.period_summary(
        {
            "netConsumption": {"consumption": 758.19, "cost": 2111.44},
            "description": "x\nAverage of the 3 highest hours: 7.88 kWh",
            "chart": {
                "items": [
                    {
                        "from": "2026-11-01T00:00:00+01:00",
                        "totalConsumption": 73.84,
                        "totalCost": 188.72,
                        "price": 255.6,
                    }
                ]
            },
        }
    )
    assert summary["kwh"] == 758.19
    assert summary["top3_average"] == 7.88
    assert summary["series"][0]["kwh"] == 73.84


def test_hourly_points_skip_missing_hours() -> None:
    item = {
        "chart": {
            "items": [
                {"from": "2026-11-10T00:00:00+01:00", "totalConsumption": 4.1, "totalCost": 10.7},
                {"from": "2026-11-10T01:00:00+01:00", "totalConsumption": None, "totalCost": None},
            ]
        }
    }
    points = parsers.hourly_points(item)
    assert len(points) == 1
    assert points[0][0] == datetime.fromisoformat("2026-11-10T00:00:00+01:00")


def test_peaks_feed_tibber_data_compatible_attributes() -> None:
    """peak_consumptions / peak_consumption_dates line up, highest first."""
    peaks = {
        "2026-11-01": {"date": "2026-11-01", "start": "2026-11-01T18:00:00+01:00", "kwh": 6.5},
        "2026-11-03": {"date": "2026-11-03", "start": "2026-11-03T16:00:00+01:00", "kwh": 9.1},
        "2026-11-05": {"date": "2026-11-05", "start": "2026-11-05T07:00:00+01:00", "kwh": 7.2},
    }
    top = parsers.top_three(peaks)["peaks"]
    assert [p["kwh"] for p in top] == [9.1, 7.2, 6.5]
    assert top[0]["start"] == "2026-11-03T16:00:00+01:00"


def _peaks(**days: float) -> dict:
    return {d: {"date": d, "start": f"{d}T18:00:00+01:00", "kwh": v} for d, v in days.items()}


def test_capacity_outlook_threshold_and_next_step() -> None:
    steps = [2, 5, 10, 15]
    # Today (d4) isn't in the top 3 yet: it must beat the lowest of the others.
    peaks = _peaks(d1=8.0, d2=7.5, d3=7.0, d4=4.0)
    out = parsers.capacity_outlook(peaks, "d4", steps)
    assert out["threshold"] == 7.0
    # Average 7.5 is in step 3 (5-10); reaching 10 needs 3*10 - (8 + 7.5) = 14.5 kWh.
    assert out["to_next_step"] == 14.5
    assert (out["next_step"], out["next_step_lower"]) == (4, 10)

    # Today is already a top-3 day: any hour above today's peak counts.
    out = parsers.capacity_outlook(_peaks(d1=8.0, d2=7.5, d3=7.0, d4=9.0), "d4", steps)
    assert out["threshold"] == 9.0

    # Early in the month every new daily peak counts.
    out = parsers.capacity_outlook(_peaks(d1=3.0), "d2", steps)
    assert out["threshold"] == 0.0
    assert out["to_next_step"] == 3 * 5 - 3.0

    # Top step and no steps: no next step.
    assert (
        parsers.capacity_outlook(_peaks(d1=20, d2=20, d3=20), "d4", steps)["to_next_step"] is None
    )
    assert parsers.capacity_outlook(peaks, "d4", [])["to_next_step"] is None


def test_cheapest_window() -> None:
    totals = [5.0, 4.0, 1.0, 1.0, 2.0, 6.0, 1.5, 1.5, 1.5]
    entries = parsers.price_entries(_hours("2026-11-10", totals), False, timedelta(hours=1))
    now = entries[0]["start"]
    best = parsers.cheapest_window(entries, now, hours=3)
    assert best["start"] == entries[2]["start"]  # 1.0, 1.0, 2.0
    assert best["end"] == entries[4]["end"]
    assert best["average"] == pytest.approx(1.3333, abs=1e-4)
    # Once that has passed, the 1.5 window at 06:00 is the cheapest.
    assert (
        parsers.cheapest_window(entries, entries[3]["start"], hours=3)["start"]
        == entries[6]["start"]
    )
    # Later in the day only the remaining hours count.
    assert parsers.cheapest_window(entries, entries[8]["start"], hours=3) is None
    assert (
        parsers.cheapest_window(entries, entries[8]["start"], hours=1)["start"]
        == entries[8]["start"]
    )


def test_upcoming_prices_start_at_the_current_slot() -> None:
    entries = parsers.price_entries(
        _hours("2026-11-10", [1.0, 2.0, 3.0, 4.0]), False, timedelta(hours=1)
    )
    now = entries[1]["start"] + timedelta(minutes=20)
    upcoming = parsers.upcoming_prices(entries, now, count=2)
    assert [u["price"] for u in upcoming] == [2.0, 3.0]
    assert [u["now"] for u in upcoming] == [True, False]


def test_connected_vehicle_prefers_the_charging_one() -> None:
    vehicles = {
        "a": {"id": "a", "charger_id": "c1", "charging_status": "disconnected", "state": None},
        "b": {
            "id": "b",
            "charger_id": "c1",
            "charging_status": "connected",
            "state": "not_charging",
        },
        "c": {"id": "c", "charger_id": "c1", "charging_status": "charging", "state": "charging"},
        "d": {"id": "d", "charger_id": "c2", "charging_status": "charging", "state": "charging"},
    }
    assert parsers.connected_vehicle(vehicles, "c1")["id"] == "c"
    del vehicles["c"]
    assert parsers.connected_vehicle(vehicles, "c1")["id"] == "b"
    assert parsers.connected_vehicle({}, "c1") is None
