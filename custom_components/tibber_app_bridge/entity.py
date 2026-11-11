"""Base entity, devices and shared helpers for the platforms."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.const import MATCH_ALL
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from . import parsers
from .const import DOMAIN, MANUFACTURER
from .coordinator import TibberAppCoordinator

# Unit markers resolved from the currency at runtime.
UNIT_MONEY = "money"
UNIT_PRICE = "price"

# Device keys: "home", "pulse", "charger:<id>", "vehicle:<id>".
HOME = "home"
PULSE = "pulse"


def resolve_unit(unit: str | None, currency: str) -> str | None:
    """UNIT_MONEY -> "NOK", UNIT_PRICE -> "NOK/kWh", anything else unchanged."""
    if unit == UNIT_MONEY:
        return currency
    if unit == UNIT_PRICE:
        return f"{currency}/kWh"
    return unit


def period_start(main: TibberAppCoordinator, period: str | None) -> datetime | None:
    """last_reset for TOTAL sensors: start of "day", "month" or "year"."""
    if period is None:
        return None
    return getattr(main.periods(), period)


def dig(data: Any, *path: str) -> Any:
    """Safe nested dict lookup."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def vehicle_item(main: TibberAppCoordinator, vehicle_id: str) -> dict[str, Any]:
    """Everything known about one vehicle.

    Pushed state (vehicleState), plus the polled "my vehicles" settings under
    "settings" and the Grid Rewards flex device under "grid".
    """
    polled = next((v for v in (main.data or {}).get("vehicles") or [] if v["id"] == vehicle_id), {})
    grid = next(
        (d for d in dig(main.data, "grid_reward", "devices") or [] if d.get("id") == vehicle_id),
        {},
    )
    return {
        "settings": polled.get("settings") or {},
        "grid": grid,
        **(main.devices.vehicle(vehicle_id) or {}),
    }


def charger_item(main: TibberAppCoordinator, charger_id: str) -> dict[str, Any]:
    """Pushed charger state, this month's polled consumption under "month" and
    the car currently connected to it under "vehicle"."""
    month = next((c for c in (main.data or {}).get("chargers") or [] if c["id"] == charger_id), {})
    vehicle = parsers.connected_vehicle(main.devices.data["vehicles"], charger_id) or {}
    return {"month": month, "vehicle": vehicle, **(main.devices.charger(charger_id) or {})}


def device_info(main: TibberAppCoordinator, device: str) -> DeviceInfo:
    """Device registry entry for the home, its Pulse, a charger or a vehicle."""
    home_ident = (DOMAIN, main.home_id)
    title = main.home.get("title") or main.config_entry.title
    if device == HOME:
        return DeviceInfo(
            identifiers={home_ident},
            name=title,
            manufacturer=MANUFACTURER,
            model="Home",
        )
    if device == PULSE:
        pulse = (main.data or {}).get("pulse") or {}
        return DeviceInfo(
            identifiers={(DOMAIN, main.pulse_id or f"{main.home_id}_pulse")},
            name=f"{title} {pulse.get('name') or 'Tibber Pulse'}",
            manufacturer=MANUFACTURER,
            model="Pulse",
            sw_version=pulse.get("firmware"),
            via_device_id=main.home_device_id,
        )
    kind, _, ident = device.partition(":")
    if kind == "charger":
        name = next((c["title"] for c in main.charger_ids if c["id"] == ident), "Charger")
        return DeviceInfo(
            identifiers={(DOMAIN, ident)},
            name=f"{title} {name}",
            model="EV charger",
            via_device_id=main.home_device_id,
        )
    polled = next((v for v in (main.data or {}).get("vehicles") or [] if v["id"] == ident), {})
    gizmo = next((g for g in main.vehicle_gizmos if g["id"] == ident), {})
    grid = vehicle_item(main, ident)["grid"]
    # "My vehicles" has the brand as title and the model as description
    # ("Nissan" / "Leaf"). Brand + model gives entity ids like
    # sensor.nissan_leaf_battery_level, unlikely to clash with other integrations.
    nickname = (polled.get("settings") or {}).get("name")
    brand_model = " ".join(p for p in (polled.get("title"), polled.get("description")) if p)
    return DeviceInfo(
        identifiers={(DOMAIN, ident)},
        name=brand_model or nickname or gizmo.get("title") or grid.get("name") or "Vehicle",
        manufacturer=polled.get("title"),
        model=polled.get("description") or grid.get("kind"),
        via_device_id=main.home_device_id,
    )


class TibberAppEntity[CoordinatorT: DataUpdateCoordinator[Any]](CoordinatorEntity[CoordinatorT]):
    """Common entity bits.

    `also_listen` lets one entity combine data from two coordinators (e.g. a
    vehicle sensor using pushed state and polled settings).
    """

    _attr_has_entity_name = True
    # Attributes hold chart series; keep them out of the recorder database.
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        coordinator: CoordinatorT,
        main: TibberAppCoordinator,
        key: str,
        device: str = HOME,
        also_listen: tuple[DataUpdateCoordinator[Any], ...] = (),
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.main = main
        self._also_listen = also_listen
        suffix = device.partition(":")[2]
        self._attr_unique_id = f"{main.home_id}_{key}" + (f"_{suffix}" if suffix else "")
        self._attr_device_info = device_info(main, device)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the extra coordinators too."""
        await super().async_added_to_hass()
        for other in self._also_listen:
            self.async_on_remove(other.async_add_listener(self._handle_coordinator_update))
