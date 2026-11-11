"""Tibber App Bridge: read-only data from the Tibber mobile app API."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TibberAppClient, TokenPair
from .const import (
    API_LANGUAGES,
    CONF_HOME_ID,
    CONF_LIVE_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_API_LANGUAGE,
    DEFAULT_LIVE_INTERVAL,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import TibberAppConfigEntry, TibberAppCoordinator, TibberAppRuntime
from .live import TibberLiveCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


def api_language(hass: HomeAssistant) -> str:
    """Accept-Language for API texts, from Home Assistant's language."""
    language = (hass.config.language or "").split("-")[0].lower()
    return API_LANGUAGES.get(language, DEFAULT_API_LANGUAGE)


def create_client(
    hass: HomeAssistant, email: str, password: str | None = None, refresh_token: str | None = None
) -> TibberAppClient:
    """Client using Home Assistant's shared HTTP session."""
    return TibberAppClient(
        async_get_clientsession(hass),
        email,
        password,
        refresh_token,
        language=api_language(hass),
    )


async def async_setup_entry(hass: HomeAssistant, entry: TibberAppConfigEntry) -> bool:
    """Set up one Tibber home."""
    client = create_client(
        hass, entry.data[CONF_EMAIL], refresh_token=entry.data.get(CONF_REFRESH_TOKEN)
    )

    def save_tokens(tokens: TokenPair) -> None:
        # The refresh token rotates; persist it so restarts don't need a login.
        if entry.data.get(CONF_REFRESH_TOKEN) != tokens.refresh_token:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: tokens.refresh_token}
            )

    client.on_tokens = save_tokens

    main = TibberAppCoordinator(hass, entry, client, entry.data[CONF_HOME_ID])
    await main.async_config_entry_first_refresh()

    live: TibberLiveCoordinator | None = None
    if main.pulse_id:
        interval = int(entry.options.get(CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL))
        live = TibberLiveCoordinator(hass, entry, main, interval)
        await live.async_config_entry_first_refresh()

    # The home device is created first so the Pulse, charger and vehicle
    # devices can point to it (via_device_id).
    home_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, main.home_id)},
        name=main.home.get("title") or entry.title,
        manufacturer=MANUFACTURER,
        model="Home",
    )
    main.home_device_id = home_device.id

    entry.runtime_data = TibberAppRuntime(client=client, main=main, devices=main.devices, live=live)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    main.start_subscriptions(live)
    entry.async_on_unload(main.async_stop_subscriptions)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TibberAppConfigEntry) -> bool:
    """Unload a config entry (background tasks stop with it)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: TibberAppConfigEntry) -> bool:
    """Migrate old entries."""
    if entry.version > 1:
        return False  # downgrade from a future version
    if entry.minor_version < 2:
        # 1.1 stored the password; the refresh token is enough.
        data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
        _LOGGER.debug("Removed the stored password from %s", entry.title)
    return True
