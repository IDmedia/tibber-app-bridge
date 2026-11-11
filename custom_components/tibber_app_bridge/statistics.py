"""Long-term statistics import (hourly consumption and cost).

Statistics are written as *external* statistics
(`tibber_app_bridge:consumption_<home>` / `..._cost_<home>`), the same approach
the core Tibber integration uses. They show up in the Energy dashboard and the
statistics-graph card and don't conflict with any entity's own recorder data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def statistic_ids(home_id: str) -> tuple[str, str]:
    """Consumption and cost statistic ids for a home."""
    key = home_id.replace("-", "")[:12]
    return f"{DOMAIN}:consumption_{key}", f"{DOMAIN}:cost_{key}"


async def _sum_before(hass: HomeAssistant, statistic_id: str, start: datetime) -> float:
    """Cumulative sum of the last row before `start` (0 if none)."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(days=60),
        start,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    series = rows.get(statistic_id) or []
    return float(series[-1].get("sum") or 0.0) if series else 0.0


async def async_import_hourly(
    hass: HomeAssistant,
    home_id: str,
    title: str,
    currency: str,
    points: list[tuple[datetime, float, float]],
) -> None:
    """Write (start, kWh, cost) hours, rewriting from the first point onward."""
    if not points:
        return
    points = sorted({p[0]: p for p in points}.values(), key=lambda p: p[0])
    first = dt_util.as_utc(points[0][0])
    consumption_id, cost_id = statistic_ids(home_id)

    for statistic_id, index, unit, unit_class, name in (
        (consumption_id, 1, UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS, "consumption"),
        (cost_id, 2, currency, None, "cost"),
    ):
        total = await _sum_before(hass, statistic_id, first)
        rows: list[StatisticData] = []
        for point in points:
            total += point[index]
            rows.append(
                StatisticData(
                    start=dt_util.as_utc(point[0]),
                    state=point[index],
                    sum=round(total, 4),
                )
            )
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{title} {name} (Tibber App)",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )
        async_add_external_statistics(hass, metadata, rows)
    _LOGGER.debug("Imported %s hours from %s for %s", len(points), first, home_id)
