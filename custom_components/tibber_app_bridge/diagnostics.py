"""Diagnostics support (credentials, address and network details redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_REFRESH_TOKEN
from .coordinator import TibberAppConfigEntry

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    "title",
    "address",
    "addressText",
    "city",
    "postalCode",
    "firstName",
    "latitude",
    "longitude",
    "meterNo",
    "meteringPointIdFormatted",
    # Pulse Wi-Fi details (SSID, IP and MAC address).
    "info",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TibberAppConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    main = runtime.main
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "discovery": {
            "country": main.country,
            "pulse": bool(main.pulse_id),
            "phase_currents": main.has_phase_currents,
            "chargers": len(main.charger_ids),
            "vehicles": len(main.vehicle_ids()),
            "grid_rewards": main.has_grid_rewards,
            "capacity_tariff": main.capacity_enabled,
        },
        "stored": {k: v for k, v in main.stored.items() if k != "peaks"}
        | {"peak_days": len(main.stored.get("peaks", {}))},
        # Price lists are long and not useful for debugging.
        "data": async_redact_data(
            {k: v for k, v in (main.data or {}).items() if k != "prices"}, TO_REDACT
        ),
        "devices": async_redact_data(runtime.devices.data, TO_REDACT),
        "live": async_redact_data(
            {k: v for k, v in (runtime.live.data or {}).items() if k != "recent"}
            if runtime.live
            else {},
            TO_REDACT,
        ),
    }
