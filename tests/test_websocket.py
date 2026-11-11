"""graphql-transport-ws client against a local fake server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any

import aiohttp
from aiohttp import web
import pytest
from tibber_app_bridge import queries, websocket
from tibber_app_bridge.api import TibberAppAuthError

GRID = {
    "state": {"__typename": "GridRewardUnavailable", "reasons": ["noVehiclesPluggedIn"]},
    "rewardCurrency": "NOK",
    "rewardCurrentMonth": 7.85,
    "rewardAllTime": 509.78,
    "flexDevices": [],
}


class FakeServer:
    """Answers each subscribe once; mode decides what happens next."""

    def __init__(self) -> None:
        self.mode = "once"  # once | stream_then_4401
        self.connections = 0
        self.subscribes = 0
        self.reject_auth = {"expired"}

    async def handler(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("authorization") in self.reject_auth:
            return web.Response(status=401)
        ws = web.WebSocketResponse(protocols=("graphql-transport-ws",))
        await ws.prepare(request)
        self.connections += 1
        assert json.loads((await ws.receive()).data) == {"type": "connection_init"}
        await ws.send_json({"type": "ping"})
        await ws.send_json({"type": "connection_ack"})
        async for msg in ws:
            data = json.loads(msg.data)
            if data["type"] != "subscribe":
                continue
            self.subscribes += 1
            field = (
                "gridRewardStatus"
                if "Status" in data["payload"]["query"]
                else "gridRewardStateTransitions"
            )
            await ws.send_json(
                {"id": data["id"], "type": "next", "payload": {"data": {field: GRID}}}
            )
            if self.mode == "complete":
                await ws.send_json({"id": data["id"], "type": "complete"})
            if self.mode == "stream_then_4401":
                await ws.send_json(
                    {"id": data["id"], "type": "next", "payload": {"data": {field: "bad"}}}
                )
                await ws.close(code=4401)
        return ws


class FakeClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.tokens = iter(["expired", *(f"t{i}" for i in range(20))])
        self.forced = 0

    async def websocket_headers(self, operation: str, force: bool = False) -> dict[str, str]:
        self.forced += force
        return {"authorization": next(self.tokens)}


@pytest.fixture
async def server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeServer]:
    fake = FakeServer()
    app = web.Application()
    app.router.add_get("/ws", fake.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    monkeypatch.setattr(websocket, "GQL_WS_URL", f"http://127.0.0.1:{port}/ws")
    monkeypatch.setattr(websocket, "MIN_BACKOFF", 0.01)
    yield fake
    await runner.cleanup()


async def test_fetch_once(server: FakeServer) -> None:
    server.mode = "complete"
    async with aiohttp.ClientSession() as session:
        client = FakeClient(session)
        with pytest.raises(TibberAppAuthError):  # first token is rejected
            await websocket.fetch_once(client, queries.GRID_REWARDS_SUBSCRIPTION, {"homeId": "h"})
        data = await websocket.fetch_once(
            client, queries.GRID_REWARDS_SUBSCRIPTION, {"homeId": "h"}
        )
    assert data == {"gridRewardStatus": GRID}


async def _collect(sub: websocket.GraphQLSubscription, got: list[Any], count: int) -> None:
    sub.start(asyncio.ensure_future)
    for _ in range(200):
        if len(got) >= count:
            break
        await asyncio.sleep(0.02)
    await sub.stop()


async def test_resubscribes_on_same_socket(server: FakeServer) -> None:
    got: list[Any] = []
    async with aiohttp.ClientSession() as session:
        sub = websocket.GraphQLSubscription(
            FakeClient(session),
            queries.GRID_REWARD_TRANSITIONS_SUBSCRIPTION,
            {"homeId": "h"},
            got.append,
            "test",
            refresh_after=0.1,
        )
        await _collect(sub, got, 3)
    assert len(got) >= 3
    assert server.connections == 1  # after the rejected token
    assert server.subscribes >= 3


async def test_bad_payload_and_4401_reconnect_with_fresh_token(server: FakeServer) -> None:
    server.mode = "stream_then_4401"
    got: list[Any] = []

    def handler(data: dict[str, Any]) -> None:
        got.append(data)
        if data["gridRewardStateTransitions"] == "bad":
            raise KeyError("handler bug")  # must not kill the subscription

    async with aiohttp.ClientSession() as session:
        client = FakeClient(session)
        sub = websocket.GraphQLSubscription(
            client, queries.GRID_REWARD_TRANSITIONS_SUBSCRIPTION, {"homeId": "h"}, handler, "test"
        )
        await _collect(sub, got, 4)
    assert len(got) >= 4
    assert server.connections >= 2
    assert client.forced >= 1  # token refreshed after the 401 handshake / 4401 close
