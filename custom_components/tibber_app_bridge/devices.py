"""Push-only coordinator for vehicle and charger state."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import parsers
from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import TibberAppConfigEntry

_LOGGER = logging.getLogger(__name__)


class TibberDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """State pushed by vehicleStateSubscription / vehicleChargerSubscription.

    Kept apart from the main coordinator so frequent pushes only touch the
    entities that use them. Identical pushes are dropped.
    """

    def __init__(self, hass: HomeAssistant, entry: TibberAppConfigEntry, home_id: str) -> None:
        """Initialize (no polling: data only arrives through pushes)."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} devices {home_id[:8]}")
        self.data = {"vehicles": {}, "chargers": {}}

    def vehicle(self, vehicle_id: str) -> dict[str, Any] | None:
        """Latest pushed state of a vehicle."""
        return self.data["vehicles"].get(vehicle_id)

    def charger(self, charger_id: str) -> dict[str, Any] | None:
        """Latest pushed state of a charger."""
        return self.data["chargers"].get(charger_id)

    def handle_vehicle(self, data: dict[str, Any]) -> None:
        """vehicleState push."""
        if state := data.get("vehicleState"):
            self._update("vehicles", parsers.vehicle(state))

    def handle_charger(self, data: dict[str, Any]) -> None:
        """vehicleChargerState push."""
        if state := data.get("vehicleChargerState"):
            self._update("chargers", parsers.vehicle_charger(state))

    def _update(self, kind: str, parsed: dict[str, Any]) -> None:
        if not (item_id := parsed.get("id")) or self.data[kind].get(item_id) == parsed:
            return
        self.async_set_updated_data({**self.data, kind: {**self.data[kind], item_id: parsed}})
