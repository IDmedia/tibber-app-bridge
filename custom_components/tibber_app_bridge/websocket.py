"""GraphQL subscriptions over websocket (graphql-transport-ws), as used by the app.

The app opens one socket per subscription and authenticates with the access
token in the upgrade request's Authorization header (connection_init has no
payload). Both patterns are mirrored here:

* `fetch_once` – subscribe, return the first `next` payload, close.
* `GraphQLSubscription` – keep a subscription open, reconnecting with backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
import logging
import time
from typing import Any
import uuid

import aiohttp

from .api import TibberAppAuthError, TibberAppClient, TibberAppConnectionError, TibberAppError
from .const import GQL_WS_URL
from .queries import operation_name

_LOGGER = logging.getLogger(__name__)

PROTOCOL = "graphql-transport-ws"
ACK_TIMEOUT = 15
AUTH_CLOSE_CODES = {4401, 4403}
MIN_BACKOFF = 5
MAX_BACKOFF = 300


class _Socket:
    """One connected socket carrying a single subscription."""

    def __init__(self, client: TibberAppClient, document: str, variables: dict[str, Any]) -> None:
        self._client = client
        self._document = document
        self._variables = variables
        self.operation = operation_name(document, "subscription")  # read-only guard
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.sub_id: str | None = None

    async def open(self, force_token: bool = False) -> None:
        headers = await self._client.websocket_headers(self.operation, force=force_token)
        try:
            self.ws = await self._client.session.ws_connect(
                GQL_WS_URL, protocols=(PROTOCOL,), headers=headers, heartbeat=30
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise TibberAppAuthError(f"{self.operation}: HTTP {err.status}") from err
            raise TibberAppConnectionError(f"{self.operation}: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TibberAppConnectionError(f"{self.operation}: {err}") from err

        await self.ws.send_json({"type": "connection_init"})
        async with asyncio.timeout(ACK_TIMEOUT):
            while True:
                msg = await self._receive()
                if msg is None:
                    raise TibberAppConnectionError(f"{self.operation}: closed before ack")
                if msg.get("type") == "connection_ack":
                    break
        await self.subscribe()

    async def subscribe(self) -> None:
        """(Re)subscribe on the open socket, completing the previous id."""
        assert self.ws
        if self.sub_id:
            await self.ws.send_json({"type": "complete", "id": self.sub_id})
        self.sub_id = str(uuid.uuid4())
        await self.ws.send_json(
            {
                "type": "subscribe",
                "id": self.sub_id,
                "payload": {
                    "query": self._document,
                    "operationName": self.operation,
                    "variables": self._variables,
                },
            }
        )

    async def _receive(self) -> dict[str, Any] | None:
        """Next protocol message (answers pings); None when the socket closed."""
        assert self.ws
        while True:
            msg = await self.ws.receive()
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                code = self.ws.close_code
                if code in AUTH_CLOSE_CODES:
                    raise TibberAppAuthError(f"{self.operation}: closed with {code}")
                return None
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise TibberAppConnectionError(f"{self.operation}: {self.ws.exception()}")
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError as err:
                raise TibberAppConnectionError(f"{self.operation}: invalid JSON") from err
            if not isinstance(data, dict):
                continue
            if data.get("type") == "ping":
                await self.ws.send_json({"type": "pong"})
                continue
            return data

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield `data` from each `next` until `complete` or close."""
        while (msg := await self._receive()) is not None:
            if msg.get("id") not in (None, self.sub_id):
                continue  # late message for a completed subscription
            kind = msg.get("type")
            if kind == "next":
                payload = msg.get("payload") or {}
                if payload.get("errors"):
                    _LOGGER.debug("%s returned errors: %s", self.operation, payload["errors"])
                if payload.get("data"):
                    yield payload["data"]
            elif kind == "error":
                raise TibberAppError(f"{self.operation}: {msg.get('payload')}")
            elif kind == "complete":
                return

    async def close(self) -> None:
        """Close the socket if open."""
        if self.ws and not self.ws.closed:
            await self.ws.close()


async def fetch_once(
    client: TibberAppClient, document: str, variables: dict[str, Any], wait: float = 20
) -> dict[str, Any] | None:
    """Subscribe and return the first payload."""
    sock = _Socket(client, document, variables)
    try:
        async with asyncio.timeout(wait):
            await sock.open()
            async for data in sock.messages():
                return data
    except TimeoutError as err:
        raise TibberAppConnectionError(f"{sock.operation}: timeout") from err
    finally:
        await sock.close()
    return None


class GraphQLSubscription:
    """Long-lived subscription with reconnect/backoff and token refresh."""

    def __init__(
        self,
        client: TibberAppClient,
        document: str,
        variables: dict[str, Any],
        on_data: Callable[[dict[str, Any]], None],
        name: str,
        refresh_after: float | None = None,
    ) -> None:
        """Initialize.

        `refresh_after`: if nothing arrives for this many seconds (or the server
        completes the subscription), subscribe again on the same socket. The
        charger only answers once per subscribe, and the app re-subscribes too.
        """
        self._client = client
        self._document = document
        self._variables = variables
        self._on_data = on_data
        self._name = name
        self._refresh_after = refresh_after
        self._task: asyncio.Task[None] | None = None
        self._socket: _Socket | None = None
        self._failing = False
        self.connected = False

    def start(self, create_task: Callable[..., asyncio.Task[None]]) -> None:
        """Start the reconnect loop (pass the entry's background task factory)."""
        self._task = create_task(self._run())

    async def stop(self) -> None:
        """Stop and close."""
        if self._task:
            self._task.cancel()
        if self._socket:
            await self._socket.close()

    async def _run(self) -> None:
        backoff = MIN_BACKOFF
        force_token = False
        while True:
            started = time.monotonic()
            self._socket = sock = _Socket(self._client, self._document, self._variables)
            try:
                await sock.open(force_token=force_token)
                force_token = False
                self.connected = True
                if self._failing:
                    _LOGGER.info("Tibber %s updates are back", self._name)
                    self._failing = False
                _LOGGER.debug("Subscribed to %s", sock.operation)
                await self._consume(sock)
            except TibberAppAuthError as err:
                self._report(err)
                force_token = True
            except (TibberAppError, TimeoutError, aiohttp.ClientError) as err:
                self._report(err)
            except Exception:
                _LOGGER.exception("Unexpected error in the Tibber %s subscription", self._name)
                self._failing = True
            finally:
                self.connected = False
                await sock.close()
            if time.monotonic() - started > 120:
                backoff = MIN_BACKOFF
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _report(self, err: Exception) -> None:
        """Warn once when updates stop; later failures only at debug."""
        if not self._failing:
            _LOGGER.warning("Tibber %s updates stopped, reconnecting: %s", self._name, err)
            self._failing = True
        else:
            _LOGGER.debug("Tibber %s subscription still failing: %s", self._name, err)

    def _deliver(self, data: dict[str, Any]) -> None:
        # A bad payload must not kill the subscription.
        try:
            self._on_data(data)
        except Exception:
            _LOGGER.exception("Error handling a Tibber %s update", self._name)

    async def _consume(self, sock: _Socket) -> None:
        """Deliver messages; with `refresh_after`, re-subscribe when quiet or completed."""
        while True:
            stream = sock.messages()
            try:
                while True:
                    self._deliver(await asyncio.wait_for(anext(stream), self._refresh_after))
            except StopAsyncIteration:
                if self._refresh_after is None or sock.ws is None or sock.ws.closed:
                    return
                await asyncio.sleep(self._refresh_after)  # server completed; poll gently
            except TimeoutError:
                if self._refresh_after is None:
                    raise
            finally:
                await stream.aclose()
            await sock.subscribe()
