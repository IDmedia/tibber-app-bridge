"""Constants for Tibber App Bridge."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "tibber_app_bridge"
MANUFACTURER: Final = "Tibber"

BASE_URL: Final = "https://app.tibber.com"
GQL_URL: Final = f"{BASE_URL}/v4/gql"
GQL_WS_URL: Final = "wss://app.tibber.com/v4/gql/ws"
LOGIN_URL: Final = f"{BASE_URL}/login.credentials"
REFRESH_URL: Final = f"{BASE_URL}/auth-sessions/external"

# Mimic the Android app closely; the backend tailors responses to the client.
APP_VERSION: Final = "26.44.0"
USER_AGENT: Final = (
    f"Tibber/{APP_VERSION} (versionCode: 2644002Dalvik/2.1.0 "
    "(Linux; U; Android 11; Home Assistant))"
)

# Accept-Language sent to the API (texts such as descriptions follow it),
# keyed by Home Assistant's configured language. Anything else gets English.
API_LANGUAGES: Final = {
    "nb": "nb-NO",
    "nn": "nb-NO",
    "no": "nb-NO",
    "sv": "sv-SE",
    "de": "de-DE",
    "nl": "nl-NL",
}
DEFAULT_API_LANGUAGE: Final = "en-GB"

CONF_HOME_ID: Final = "home_id"
CONF_REFRESH_TOKEN: Final = "refresh_token"  # noqa: S105 (config key, not a secret)

CONF_LIVE_INTERVAL: Final = "live_interval"
CONF_BACKFILL_DAYS: Final = "backfill_days"
CONF_CAPACITY_STEPS: Final = "capacity_steps"
CONF_INCLUDE_GRID_PRICE: Final = "include_grid_price"

DEFAULT_LIVE_INTERVAL: Final = 10  # seconds
MIN_LIVE_INTERVAL: Final = 5
MAX_LIVE_INTERVAL: Final = 300
DEFAULT_BACKFILL_DAYS: Final = 60
MAX_BACKFILL_DAYS: Final = 730
DEFAULT_INCLUDE_GRID_PRICE: Final = True

# Capacity-based grid tariffs ("kapasitetsledd") exist in Norway. Upper bound
# of each step in kWh/h; other countries start without steps.
CAPACITY_TARIFF_STEPS: Final = {"NO": "2,5,10,15,20,25,50,75,100"}

STORAGE_VERSION: Final = 1
