"""API client against a local fake of the Tibber endpoints."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
import json
import time

import aiohttp
from aiohttp import web
import pytest
from tibber_app_bridge import api
from tibber_app_bridge.api import (
    TibberAppAuthError,
    TibberAppClient,
    TibberAppConnectionError,
)

QUERY = "query GetCustomerId { me { id } }"


def _jwt(expires_in: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + expires_in}).encode())
    return f"h.{payload.decode().rstrip('=')}.s"


class FakeTibber:
    def __init__(self) -> None:
        self.refresh_count = 0
        self.gql_status = 200
        self.reject_next_token = False
        self.seen_tokens: list[str] = []

    async def login(self, request: web.Request) -> web.Response:
        form = await request.post()
        if form.get("password") != "right":
            return web.json_response({"message": "Wrong email or password"}, status=401)
        return web.json_response({"token": _jwt(3600), "refreshToken": "r0"})

    async def refresh(self, request: web.Request) -> web.Response:
        if not request.headers["authorization"].startswith("r"):
            return web.json_response({"message": "bad"}, status=401)
        self.refresh_count += 1
        return web.json_response({"token": _jwt(3600), "refreshToken": f"r{self.refresh_count}"})

    async def gql(self, request: web.Request) -> web.Response:
        token = request.headers["authorization"]
        self.seen_tokens.append(token)
        if self.reject_next_token:
            self.reject_next_token = False
            return web.Response(status=401)
        if self.gql_status != 200:
            return web.Response(status=self.gql_status, text="<html>oops</html>")
        body = await request.json()
        assert body["operationName"] == "GetCustomerId"
        return web.json_response({"data": {"me": {"id": "abc"}}})


@pytest.fixture
async def fake(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeTibber]:
    tibber = FakeTibber()
    app = web.Application()
    app.router.add_post("/login.credentials", tibber.login)
    app.router.add_put("/auth-sessions/external", tibber.refresh)
    app.router.add_post("/v4/gql", tibber.gql)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]
    monkeypatch.setattr(api, "LOGIN_URL", f"{base}/login.credentials")
    monkeypatch.setattr(api, "REFRESH_URL", f"{base}/auth-sessions/external")
    monkeypatch.setattr(api, "GQL_URL", f"{base}/v4/gql")
    yield tibber
    await runner.cleanup()


async def test_wrong_password(fake: FakeTibber) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TibberAppAuthError):
            await TibberAppClient(session, "a@b.c", "wrong").login()


async def test_refresh_token_keeps_session_and_rotates(fake: FakeTibber) -> None:
    saved: list[str] = []
    async with aiohttp.ClientSession() as session:
        client = TibberAppClient(session, "a@b.c", refresh_token="r0")
        client.on_tokens = lambda tokens: saved.append(tokens.refresh_token)
        assert await client.query(QUERY) == {"me": {"id": "abc"}}
        assert await client.query(QUERY) == {"me": {"id": "abc"}}
    assert fake.refresh_count == 1  # access token reused until it expires
    assert saved == ["r1"]


async def test_401_triggers_one_refresh_and_retry(fake: FakeTibber) -> None:
    async with aiohttp.ClientSession() as session:
        client = TibberAppClient(session, "a@b.c", refresh_token="r0")
        await client.query(QUERY)
        fake.reject_next_token = True
        assert await client.query(QUERY) == {"me": {"id": "abc"}}
    assert fake.refresh_count == 2


async def test_expired_session_without_password_is_auth_error(fake: FakeTibber) -> None:
    async with aiohttp.ClientSession() as session:
        client = TibberAppClient(session, "a@b.c", refresh_token="invalid")
        with pytest.raises(TibberAppAuthError):
            await client.query(QUERY)


@pytest.mark.parametrize("status", [429, 500, 502])
async def test_server_errors_are_connection_errors(fake: FakeTibber, status: int) -> None:
    fake.gql_status = status
    async with aiohttp.ClientSession() as session:
        client = TibberAppClient(session, "a@b.c", refresh_token="r0")
        with pytest.raises(TibberAppConnectionError):
            await client.query(QUERY)


async def test_unreachable_host_is_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "LOGIN_URL", "http://127.0.0.1:9/login.credentials")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TibberAppConnectionError):
            await TibberAppClient(session, "a@b.c", "right").login()
