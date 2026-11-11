"""The integration must never be able to change anything in a Tibber account."""

from __future__ import annotations

import aiohttp
import pytest
from tibber_app_bridge import queries
from tibber_app_bridge.api import TibberAppClient
from tibber_app_bridge.queries import ReadOnlyViolation, operation_name
from tibber_app_bridge.websocket import fetch_once

DOCUMENTS = {
    name: value
    for name, value in vars(queries).items()
    if name.isupper()
    and isinstance(value, str)
    and value.lstrip().startswith(("query", "subscription"))
}


def test_all_documents_are_read_only() -> None:
    assert len(DOCUMENTS) > 20
    for name, document in DOCUMENTS.items():
        kind = "subscription" if document.lstrip().startswith("subscription") else "query"
        assert operation_name(document, kind), name
        assert "mutation" not in document, name


def test_no_document_constant_is_a_mutation() -> None:
    for name, value in vars(queries).items():
        if isinstance(value, str):
            assert not value.lstrip().startswith("mutation"), name


@pytest.mark.parametrize(
    "document",
    [
        "mutation SetVehicleSettings { me { setVehicleSettings { id } } }",
        "query X { me { id } } mutation Y { me { id } }",
        "   mutation X { a }",
        "subscription X { a }",  # wrong transport for HTTP
        "{ me { id } }",  # anonymous operations aren't allowed either
    ],
)
def test_guard_blocks(document: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        operation_name(document, "query")


async def test_client_refuses_mutation_before_any_request() -> None:
    async with aiohttp.ClientSession() as session:
        client = TibberAppClient(session, "user@example.com", refresh_token="unused")
        with pytest.raises(ReadOnlyViolation):
            await client.query("mutation SetThing { me { id } }")


async def test_websocket_refuses_query_and_mutation() -> None:
    async with aiohttp.ClientSession() as session:
        client = TibberAppClient(session, "user@example.com", refresh_token="unused")
        for document in ("query X { me { id } }", "mutation X { a }"):
            with pytest.raises(ReadOnlyViolation):
                await fetch_once(client, document, {})
