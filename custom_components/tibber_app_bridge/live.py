"""Live Pulse coordinator: websocket push with a polling fallback."""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import parsers, queries
from .api import TibberAppAuthError, TibberAppError, iso
from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import TibberAppConfigEntry, TibberAppCoordinator

_LOGGER = logging.getLogger(__name__)

# Fields that only pushes carry; kept while falling back to polling.
PUSH_ONLY_FIELDS = (
    "accumulatedConsumption",
    "accumulatedCost",
    "accumulatedConsumptionCurrentHour",
    "estimatedAccumulatedConsumptionCurrentHour",
    "accumulatedProduction",
    "accumulatedProductionCurrentHour",
    "accumulatedReward",
    "averagePower",
    "maxPower",
    "maxPowerTimestamp",
    "minPower",
    "minPowerTimestamp",
    "peakControlConsumptionState",
    "effectiveHourlyConsumptionLimit",
    "currency",
)
# Skip the poll while pushes are this fresh.
PUSH_FRESH = timedelta(seconds=45)
# Pushes older than this are the replay sent right after subscribing.
BACKLOG_AGE = timedelta(seconds=30)
STALE_AFTER = timedelta(minutes=5)


class TibberLiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Live Pulse data.

    The main source is the app's `pulseSubscription` (a push every ~10 s with
    today's totals and max/min). Polling the 10 s measurement cache only
    happens when no push has arrived for a while.
    """

    config_entry: TibberAppConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TibberAppConfigEntry,
        main: TibberAppCoordinator,
        interval: int,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} live {main.home_id[:8]}",
            update_interval=timedelta(seconds=interval),
        )
        self.main = main
        self.max_power: tuple[float, datetime] | None = None
        self.min_power: tuple[float, datetime] | None = None
        self._day: date | None = None
        self._window = timedelta(seconds=max(interval * 3, 60))
        self._recent: deque[dict[str, Any]] = deque(maxlen=30)
        self._pushed: dict[str, Any] = {}
        self._last_push: datetime | None = None

    # Push ------------------------------------------------------------------

    def handle_push(self, data: dict[str, Any]) -> None:
        """liveMeasurement push. Right after subscribing ~10 min are replayed."""
        measurement = data.get("liveMeasurement")
        if not measurement or measurement.get("power") is None:
            return
        now = dt_util.now(self.main.tz)
        self._track([measurement], now)
        ts = parsers.parse_dt(measurement.get("timestamp"))
        if ts is None or now - ts > BACKLOG_AGE:
            return  # replayed history only feeds max/min and the recent buffer
        self._pushed = {k: measurement.get(k) for k in PUSH_ONLY_FIELDS} | {"day": now.date()}
        self._last_push = now
        self.async_set_updated_data(self._build(measurement, now, source="push"))

    # Poll fallback -----------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.now(self.main.tz)
        if self.data and self._last_push and now - self._last_push < PUSH_FRESH:
            return self.data
        try:
            home = await self.main.client.home(
                queries.PULSE_MEASUREMENTS_CACHE,
                self.main.home_id,
                deviceId=self.main.pulse_id,
                **{"from": iso(now - self._window), "to": iso(now)},
            )
        except TibberAppAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except TibberAppError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="live_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        samples = [m for m in home.get("pulseMeasurements") or [] if m.get("power") is not None]
        if not samples:
            # Keep the last value; entities turn unavailable once it is stale.
            return {**(self.data or {}), "stale": True}
        self._track(samples, now)
        return self._build(samples[-1], now, source="poll")

    # Helpers -----------------------------------------------------------------

    def _track(self, samples: list[dict[str, Any]], now: datetime) -> None:
        """Keep today's max/min and a short buffer of recent samples."""
        if self._day != now.date():
            self._day = now.date()
            self.max_power = self.min_power = None
        for m in samples:
            ts = parsers.parse_dt(m.get("timestamp"))
            if ts is None:
                continue
            last = parsers.parse_dt(self._recent[-1]["t"]) if self._recent else None
            if last is None or ts > last:
                self._recent.append({"t": ts.isoformat(), "w": m["power"]})
            if ts.date() != now.date():
                continue
            if self.max_power is None or m["power"] > self.max_power[0]:
                self.max_power = (m["power"], ts)
            if self.min_power is None or m["power"] < self.min_power[0]:
                self.min_power = (m["power"], ts)

    def _build(self, latest: dict[str, Any], now: datetime, source: str) -> dict[str, Any]:
        """Latest sample plus today's push-only totals."""
        pushed = self._pushed if self._pushed.get("day") == now.date() else {}
        ts = parsers.parse_dt(latest.get("timestamp"))
        return {
            **pushed,
            **{k: v for k, v in latest.items() if v is not None},
            "timestamp": ts,
            "source": source,
            "stale": ts is None or now - ts > STALE_AFTER,
            "recent": list(self._recent),
        }

    def extreme(self, which: str) -> tuple[float, datetime] | None:
        """Today's max or min power (value, when).

        Tibber's own value from the push is exact; without it, the larger/
        smaller of the minute history and the live samples seen since startup.
        """
        data = self.data or {}
        value = data.get(f"{which}Power")
        at = parsers.parse_dt(data.get(f"{which}PowerTimestamp"))
        today = dt_util.now(self.main.tz).date()
        if value is not None and at is not None and at.date() == today:
            return value, at

        candidates: list[tuple[float, datetime]] = []
        seen = self.max_power if which == "max" else self.min_power
        if seen:
            candidates.append(seen)
        today_data = (self.main.data or {}).get("today") or {}
        stored_at = parsers.parse_dt(today_data.get(f"{which}_power_at"))
        if today_data.get(f"{which}_power") is not None and stored_at is not None:
            candidates.append((today_data[f"{which}_power"], stored_at))
        if not candidates:
            return None
        pick = max if which == "max" else min
        return pick(candidates, key=lambda c: c[0])
