"""Binary sensors for Tibber App Bridge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TibberAppConfigEntry, TibberAppCoordinator
from .entity import PULSE, TibberAppEntity, charger_item, dig, vehicle_item

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TibberBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Binary sensor description; value_fn gets main data or a device item."""

    value_fn: Callable[[dict[str, Any]], bool | None]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


PULSE_BINARY_SENSORS: tuple[TibberBinarySensorEntityDescription, ...] = (
    TibberBinarySensorEntityDescription(
        key="pulse_connected",
        translation_key="pulse_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: dig(d, "pulse", "is_alive"),
    ),
    TibberBinarySensorEntityDescription(
        key="load_balancing",
        translation_key="load_balancing",
        value_fn=lambda d: dig(d, "pulse", "settings", "loadBalancingOn"),
    ),
    TibberBinarySensorEntityDescription(
        key="peak_control",
        translation_key="peak_control",
        value_fn=lambda d: dig(d, "peak_control", "isActive"),
    ),
    TibberBinarySensorEntityDescription(
        key="pulse_problem",
        translation_key="pulse_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (
            bool(dig(d, "pulse", "error") or dig(d, "pulse", "warning")) if d.get("pulse") else None
        ),
        attrs_fn=lambda d: {
            "error": dig(d, "pulse", "error"),
            "warning": dig(d, "pulse", "warning"),
        },
    ),
)

VEHICLE_BINARY_SENSORS: tuple[TibberBinarySensorEntityDescription, ...] = (
    TibberBinarySensorEntityDescription(
        key="plugged_in",
        translation_key="plugged_in",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda v: v["grid"].get("plugged_in"),
    ),
    TibberBinarySensorEntityDescription(
        key="smart_charging",
        translation_key="smart_charging",
        value_fn=lambda v: v.get("smart_charging", v["grid"].get("smart_charging")),
    ),
    TibberBinarySensorEntityDescription(
        key="vehicle_charging",
        translation_key="vehicle_charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda v: v["state"] == "charging" if "state" in v else None,
    ),
)

CHARGER_BINARY_SENSORS: tuple[TibberBinarySensorEntityDescription, ...] = (
    TibberBinarySensorEntityDescription(
        key="charger_online",
        translation_key="charger_online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda c: c.get("is_alive"),
    ),
    TibberBinarySensorEntityDescription(
        key="charger_load_balancing",
        translation_key="charger_load_balancing",
        value_fn=lambda c: (c.get("settings") or {}).get("fuseLoadBalancing"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TibberAppConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    main = entry.runtime_data.main
    entities: list[BinarySensorEntity] = []
    if main.pulse_id:
        entities += [TibberBinarySensor(main, d) for d in PULSE_BINARY_SENSORS]
    for vehicle_id in main.vehicle_ids():
        entities += [
            TibberItemBinarySensor(main, d, "vehicle", vehicle_id) for d in VEHICLE_BINARY_SENSORS
        ]
    for charger in main.charger_ids:
        entities += [
            TibberItemBinarySensor(main, d, "charger", charger["id"])
            for d in CHARGER_BINARY_SENSORS
        ]
    async_add_entities(entities)


class _DescribedBinarySensor(BinarySensorEntity):
    entity_description: TibberBinarySensorEntityDescription

    def _source(self) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def is_on(self) -> bool | None:
        """State."""
        return self.entity_description.value_fn(self._source())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes."""
        if fn := self.entity_description.attrs_fn:
            return fn(self._source())
        return None


class TibberBinarySensor(TibberAppEntity[TibberAppCoordinator], _DescribedBinarySensor):
    """Pulse binary sensor backed by the main coordinator."""

    def __init__(
        self, main: TibberAppCoordinator, description: TibberBinarySensorEntityDescription
    ) -> None:
        """Initialize."""
        super().__init__(main, main, description.key, PULSE)
        self.entity_description = description

    def _source(self) -> dict[str, Any]:
        return self.coordinator.data or {}


class TibberItemBinarySensor(TibberAppEntity[TibberAppCoordinator], _DescribedBinarySensor):
    """Binary sensor for one vehicle or charger (polled + pushed data)."""

    def __init__(
        self,
        main: TibberAppCoordinator,
        description: TibberBinarySensorEntityDescription,
        kind: str,
        item_id: str,
    ) -> None:
        """Initialize."""
        super().__init__(
            main, main, description.key, f"{kind}:{item_id}", also_listen=(main.devices,)
        )
        self.entity_description = description
        self._item = vehicle_item if kind == "vehicle" else charger_item
        self._item_id = item_id

    def _source(self) -> dict[str, Any]:
        return self._item(self.main, self._item_id)
