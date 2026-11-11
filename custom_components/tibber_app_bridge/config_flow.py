"""Config flow for Tibber App Bridge."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client
from .api import TibberAppAuthError, TibberAppError
from .const import (
    CAPACITY_TARIFF_STEPS,
    CONF_BACKFILL_DAYS,
    CONF_CAPACITY_STEPS,
    CONF_HOME_ID,
    CONF_INCLUDE_GRID_PRICE,
    CONF_LIVE_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_INCLUDE_GRID_PRICE,
    DEFAULT_LIVE_INTERVAL,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    MAX_LIVE_INTERVAL,
    MIN_LIVE_INTERVAL,
)
from .parsers import parse_steps

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)


def _home_title(home: dict[str, Any]) -> str:
    return home.get("title") or (home.get("address") or {}).get("addressText") or "Tibber"


class TibberAppConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with the same credentials as the Tibber app.

    The password is only used to log in; the entry stores the email and the
    rotating refresh token.
    """

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize."""
        self._account: dict[str, Any] = {}
        self._homes: list[dict[str, Any]] = []

    async def _login(self, email: str, password: str) -> dict[str, str]:
        """Log in, fetch the homes and return the error dict (empty on success)."""
        client = create_client(self.hass, email.strip(), password)
        try:
            await client.login()
            me = await client.get_homes()
        except TibberAppAuthError:
            return {"base": "invalid_auth"}
        except TibberAppError as err:
            _LOGGER.debug("Login failed: %s", err)
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error while logging in to Tibber")
            return {"base": "unknown"}
        self._account = {CONF_EMAIL: email.strip(), CONF_REFRESH_TOKEN: client.refresh_token}
        self._homes = [h for h in me.get("homes") or [] if h.get("id")]
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            if not errors:
                if not self._homes:
                    return self.async_abort(reason="no_homes")
                if len(self._homes) == 1:
                    return await self._create(self._homes[0])
                return await self.async_step_home()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, {CONF_EMAIL: (user_input or {}).get(CONF_EMAIL)}
            ),
            errors=errors,
        )

    async def async_step_home(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a home when the account has several."""
        if user_input is not None:
            home = next(h for h in self._homes if h["id"] == user_input[CONF_HOME_ID])
            return await self._create(home)
        configured = {e.unique_id for e in self._async_current_entries()}
        options = [
            SelectOptionDict(value=h["id"], label=_home_title(h))
            for h in self._homes
            if h["id"] not in configured
        ]
        if not options:
            return self.async_abort(reason="already_configured")
        return self.async_show_form(
            step_id="home",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOME_ID): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def _create(self, home: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(home["id"])
        self._abort_if_unique_id_configured(updates=self._account)
        return self.async_create_entry(
            title=_home_title(home), data={**self._account, CONF_HOME_ID: home["id"]}
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The session expired or the password changed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._login(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            if not errors:
                if entry.data[CONF_HOME_ID] not in {h["id"] for h in self._homes}:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data_updates=self._account)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TibberAppOptionsFlow:
        """Options."""
        return TibberAppOptionsFlow()


class TibberAppOptionsFlow(OptionsFlowWithReload):
    """Polling and calculation options (the entry reloads after saving)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                parse_steps(user_input.get(CONF_CAPACITY_STEPS, ""))
            except ValueError:
                errors[CONF_CAPACITY_STEPS] = "invalid_steps"
            else:
                return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_LIVE_INTERVAL, default=DEFAULT_LIVE_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_LIVE_INTERVAL,
                        max=MAX_LIVE_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_BACKFILL_DAYS, default=DEFAULT_BACKFILL_DAYS): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=MAX_BACKFILL_DAYS,
                        step=1,
                        unit_of_measurement="d",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_CAPACITY_STEPS, default=self._default_capacity_steps()
                ): TextSelector(),
                vol.Required(
                    CONF_INCLUDE_GRID_PRICE, default=DEFAULT_INCLUDE_GRID_PRICE
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or self.config_entry.options
            ),
            errors=errors,
        )

    def _default_capacity_steps(self) -> str:
        """Norway's standard steps for Norwegian homes, otherwise none."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        country = runtime.main.country if runtime else None
        return CAPACITY_TARIFF_STEPS.get(country or "", "")
