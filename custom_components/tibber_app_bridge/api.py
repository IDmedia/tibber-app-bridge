"""Async client for the GraphQL API used by the Tibber mobile app."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import time
from typing import Any

import aiohttp

from . import queries
from .const import GQL_URL, LOGIN_URL, REFRESH_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Renew the access token this many seconds before it expires.
TOKEN_MARGIN = 300


class TibberAppError(Exception):
    """Base error."""


class TibberAppAuthError(TibberAppError):
    """Credentials or session are no longer valid."""


class TibberAppConnectionError(TibberAppError):
    """Network or server problem."""


@dataclass
class TokenPair:
    """Access token (JWT, ~3 h) and rotating refresh token."""

    access_token: str
    refresh_token: str

    @property
    def expires_at(self) -> float:
        """Expiry from the JWT `exp` claim, or 0 if it can't be read."""
        try:
            payload = self.access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        except (IndexError, KeyError, ValueError):
            return 0.0


def iso(dt: datetime) -> str:
    """Format a timestamp the way the app does (offset aware, no microseconds)."""
    return dt.replace(microsecond=0).isoformat()


class TibberAppClient:
    """Minimal, read-only client (queries only, never mutations).

    Authentication mirrors the app:
      * POST /login.credentials (form encoded) -> {token, refreshToken}
      * PUT /auth-sessions/external with the refresh token as Authorization
        -> new {token, refreshToken} for the same session.

    The password is only needed for the first login (config/reauth flow);
    afterwards the rotating refresh token keeps the session alive.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str | None = None,
        refresh_token: str | None = None,
        language: str = "en-GB",
        on_tokens: Callable[[TokenPair], Awaitable[None] | None] | None = None,
    ) -> None:
        """Initialize."""
        self._session = session
        self._email = email.strip()
        self._password = password
        self._language = language
        self.on_tokens = on_tokens
        self._tokens: TokenPair | None = TokenPair("", refresh_token) if refresh_token else None
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str | None:
        """Current refresh token."""
        return self._tokens.refresh_token if self._tokens else None

    def _headers(self, authorization: str = "") -> dict[str, str]:
        return {
            "accept-language": self._language,
            "authorization": authorization,
            "user-agent": USER_AGENT,
            "x-tibber-new-ui": "true",
        }

    async def _token_request(
        self, method: str, url: str, authorization: str = "", data: Any = None
    ) -> TokenPair:
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(authorization),
                data=data,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TibberAppConnectionError(f"Login request failed: {err}") from err

        if not isinstance(body, dict):
            body = {}
        if status in (400, 401, 403):
            # Tibber's message is safe to show ("email or password is incorrect").
            raise TibberAppAuthError(str(body.get("message") or f"HTTP {status}"))
        if status >= 300 or not body.get("token") or not body.get("refreshToken"):
            raise TibberAppConnectionError(f"Unexpected login response (HTTP {status})")
        tokens = TokenPair(str(body["token"]), str(body["refreshToken"]))
        self._tokens = tokens
        if self.on_tokens:
            result = self.on_tokens(tokens)
            if asyncio.iscoroutine(result):
                await result
        return tokens

    async def login(self) -> TokenPair:
        """Log in with email and password."""
        if not self._password:
            raise TibberAppAuthError("The Tibber session expired; log in again")
        _LOGGER.debug("Logging in to Tibber")
        return await self._token_request(
            "POST",
            LOGIN_URL,
            data={"email": self._email, "password": self._password},
        )

    async def refresh(self) -> TokenPair:
        """Exchange the refresh token for a new token pair."""
        if not self._tokens or not self._tokens.refresh_token:
            return await self.login()
        try:
            return await self._token_request(
                "PUT", REFRESH_URL, authorization=self._tokens.refresh_token
            )
        except TibberAppAuthError:
            _LOGGER.debug("Refresh token rejected")
            return await self.login()

    async def _access_token(self, force: bool = False) -> str:
        async with self._lock:
            tokens = self._tokens
            if (
                force
                or tokens is None
                or not tokens.access_token
                or tokens.expires_at - TOKEN_MARGIN < time.time()
            ):
                tokens = await self.refresh()
            return tokens.access_token

    @property
    def session(self) -> aiohttp.ClientSession:
        """Underlying HTTP session (also used for the websocket)."""
        return self._session

    async def websocket_headers(self, operation: str, force: bool = False) -> dict[str, str]:
        """Headers for the GraphQL websocket; auth is sent on the upgrade request."""
        token = await self._access_token(force=force)
        return self._headers(token) | {"x-apollo-operation-name": operation}

    async def query(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        *,
        _retry: bool = True,
    ) -> dict[str, Any]:
        """Run a GraphQL query and return `data`."""
        operation = queries.operation_name(document, "query")  # read-only guard
        token = await self._access_token()
        payload = {
            "operationName": operation,
            "query": " ".join(document.split()),
            "variables": variables or {},
        }
        headers = self._headers(token) | {
            "x-apollo-operation-name": operation,
            "accept": "application/graphql-response+json, application/json",
        }
        try:
            async with self._session.post(
                GQL_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TibberAppConnectionError(f"{operation}: {err}") from err

        if status == 401:
            if _retry:
                await self._access_token(force=True)
                return await self.query(document, variables, _retry=False)
            raise TibberAppAuthError(f"{operation}: unauthorized")
        if status == 429 or status >= 500 or not isinstance(body, dict):
            raise TibberAppConnectionError(f"{operation}: HTTP {status}")

        if errors := [e for e in body.get("errors") or [] if isinstance(e, dict)]:
            messages = "; ".join(str(e.get("message")) for e in errors)
            if any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for e in errors):
                if _retry:
                    await self._access_token(force=True)
                    return await self.query(document, variables, _retry=False)
                raise TibberAppAuthError(messages)
            if not body.get("data"):
                raise TibberAppError(f"{operation}: {messages}")
            _LOGGER.debug("%s returned partial errors: %s", operation, messages)
        return body.get("data") or {}

    async def home(self, document: str, home_id: str, **variables: Any) -> dict[str, Any]:
        """Query something below `me.home(id)` and return the home object."""
        data = await self.query(document, {"homeId": home_id, **variables})
        return ((data.get("me") or {}).get("home")) or {}

    # Convenience wrappers ---------------------------------------------------

    async def get_homes(self) -> dict[str, Any]:
        """Return `me` with homes."""
        return (await self.query(queries.GET_HOMES)).get("me") or {}
