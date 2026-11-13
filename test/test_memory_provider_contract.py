"""The memory provider contract: what a provider's answer must look like.

Every value that crosses from a provider is validated; a malformed answer is
``ProviderInvalidResponse`` (treated as unavailable), and a long text is cut and
marked rather than refused. The manifest block that registers a provider is pinned
here too.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.memory_provider import contract
from kiro_crew.memory_provider.contract import (
    MAX_ITEM_TEXT,
    TRUNCATED_MARK,
    ProviderConflict,
    ProviderInvalidResponse,
    ProviderNotSupported,
    ProviderReadOnly,
    ProviderRefused,
    ProviderUnavailable,
)

_CAPS = {
    "api_version": 1,
    "read_only": False,
    "supports_search": True,
    "supports_vectors": False,
    "supports_lessons": True,
    "supports_list": True,
    "supports_delete": True,
    "ingest": "raw",
    "multi_store": True,
    "egress": "local",
    "kinds": ["rule", "fact"],
    "max_page": 500,
}


def test_a_valid_item_round_trips() -> None:
    item = contract.item_from_wire(
        {"id": "m1", "kind": "rule", "text": "Use tabs.", "scopes": ["repo:x"], "score": 0.5}
    )
    assert item.kind == "rule" and item.scopes == ("repo:x",) and item.score == 0.5
    assert contract.item_from_wire(item.to_wire()) == item


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "", "kind": "rule", "text": "x"},
        {"id": "m", "kind": "secret", "text": "x"},
        {"id": "m", "kind": "rule", "text": 3},
        {"id": "m", "kind": "rule", "text": "x", "status": "gone"},
        {"id": "m", "kind": "rule", "text": "x", "score": float("nan")},
        {"id": "x" * 257, "kind": "rule", "text": "x"},
        {"id": "m", "kind": "rule", "text": "x", "meta": {"k": 1}},
        "not an object",
    ],
)
def test_a_malformed_item_is_an_invalid_response(bad) -> None:
    with pytest.raises(ProviderInvalidResponse):
        contract.item_from_wire(bad)


@pytest.mark.parametrize(
    "parse, bad",
    [
        (contract.item_from_wire, {"id": "x", "kind": [], "text": "t"}),
        (contract.item_from_wire, {"id": "x", "kind": "fact", "text": "t", "status": {}}),
        (contract.write_result_from_wire, {"outcome": ["inserted"]}),
        (contract.capabilities_from_wire, {"egress": "local", "kinds": [["fact"]]}),
    ],
    ids=["kind-list", "status-object", "outcome-list", "kinds-nested"],
)
def test_an_unhashable_answer_is_an_invalid_response_not_a_crash(parse, bad) -> None:
    with pytest.raises(ProviderInvalidResponse):
        parse(bad)


def test_long_text_is_cut_and_marked_not_refused() -> None:
    item = contract.item_from_wire({"id": "m", "kind": "note", "text": "a" * (MAX_ITEM_TEXT + 50)})
    assert len(item.text) == MAX_ITEM_TEXT
    assert item.text.endswith(TRUNCATED_MARK)


def test_an_answer_over_the_item_cap_is_invalid() -> None:
    rows = [{"id": f"m{i}", "kind": "note", "text": "t"} for i in range(201)]
    with pytest.raises(ProviderInvalidResponse):
        contract.items_from_wire({"items": rows})


@pytest.mark.parametrize(
    "code, cls",
    [
        ("unavailable", ProviderUnavailable),
        ("refused", ProviderRefused),
        ("read_only", ProviderReadOnly),
        ("not_supported", ProviderNotSupported),
        ("conflict", ProviderConflict),
        ("invalid", ProviderInvalidResponse),
        ("made-up", ProviderInvalidResponse),
    ],
)
def test_every_error_code_maps_to_its_exception(code, cls) -> None:
    error = contract.error_from_wire({"code": code, "message": "m", "retry_after_secs": 5})
    assert type(error) is cls


def test_read_only_capabilities_turn_writes_off_and_cap_the_page() -> None:
    caps = contract.capabilities_from_wire({**_CAPS, "read_only": True})
    assert not caps.supports_lessons and not caps.supports_delete and caps.ingest == "none"
    assert caps.max_page == contract.MAX_PAGE
    assert set(caps.replace_gaps()) == {"read_only", "supports_lessons", "supports_delete"}


@pytest.mark.parametrize(
    "change",
    [{"api_version": 2}, {"egress": "cloud"}, {"ingest": "all"}, {"kinds": ["rule", "x"]}],
)
def test_capabilities_outside_the_contract_are_invalid(change) -> None:
    with pytest.raises(ProviderInvalidResponse):
        contract.capabilities_from_wire({**_CAPS, **change})


def test_write_results_speak_the_lesson_vocabulary() -> None:
    result = contract.write_result_from_wire({"outcome": "deduped", "id": "m1", "winner": "w"})
    assert result.stored and result.winner == "w"
    assert not contract.write_result_from_wire({"outcome": "refused"}).stored
    with pytest.raises(ProviderInvalidResponse):
        contract.write_result_from_wire({"outcome": "saved"})


def _manifest(providers, backend=True) -> AppManifest:
    data = {
        "name": "memory-fixture",
        "version": "1.0.0",
        "displayName": "Fixture",
        "description": "d",
        "contributes": {"memoryProviders": providers},
        "permissions": {"memoryAdmin": True},
    }
    if backend:
        data["backend"] = {"entryPoint": "backend/server.py", "healthCheck": "/health"}
    return AppManifest.from_dict(data)


def test_a_declared_provider_is_read_and_serialized() -> None:
    manifest = _manifest([{"id": "fixture", "displayName": "F", "route": "/memory-provider"}])
    assert [p.id for p in manifest.contributes.memoryProviders.usable()] == ["fixture"]
    assert manifest.permissions.memoryAdmin is True
    assert manifest.to_dict()["contributes"]["memoryProviders"][0]["route"] == "/memory-provider"
    assert not [e for e in manifest.validate() if "memoryProviders" in e]


@pytest.mark.parametrize(
    "providers, backend, needle",
    [
        ([{"id": "Bad", "displayName": "F", "route": "r"}], True, ".id must match"),
        ([{"id": "a", "displayName": "F", "route": "https://x"}], True, "plain path"),
        ([{"id": "a", "displayName": "F" * 61, "route": "r"}], True, "displayName"),
        ([{"id": "a", "displayName": "F", "route": "r"}] * 2, True, "declared twice"),
        ([{"id": f"p{i}", "displayName": "F", "route": "r"} for i in range(5)], True, "at most"),
        ([{"id": "a", "displayName": "F", "route": "r"}], False, "process backend"),
        ("nope", True, "must be an array"),
    ],
)
def test_a_malformed_declaration_refuses_the_install(providers, backend, needle) -> None:
    manifest = _manifest(providers, backend=backend)
    errors = [e for e in manifest.validate() if "memoryProviders" in e]
    assert any(needle in e for e in errors), errors
    if manifest.contributes.memoryProviders.errors:
        assert manifest.contributes.memoryProviders.usable() == []


def test_memory_admin_is_granted_only_by_a_literal_true() -> None:
    from kiro_crew.apps.manifest_sections import Permissions

    assert Permissions.from_dict({"memoryAdmin": "true"}).memoryAdmin is False
    assert Permissions.from_dict({"memoryAdmin": True}).to_dict() == {"memoryAdmin": True}


def test_a_trickling_provider_answer_stops_at_the_whole_deadline(monkeypatch) -> None:
    import socket
    import threading

    from kiro_crew.memory_provider import app_client
    from kiro_crew.memory_provider.contract import ProviderUnavailable

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(65_536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            while not stop.wait(0.05):  # flake-ok: the trickle under test, one byte per beat
                try:
                    conn.sendall(b" ")
                except OSError:
                    return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(app_client, "_backend_port", lambda app: port)
    monkeypatch.setattr(app_client, "_app_secret", lambda app: "s")
    client = app_client.AppMemoryProvider(
        app="fixture", provider="p", route="memory-provider", display_name="P", app_version="1"
    )
    try:
        with pytest.raises(ProviderUnavailable, match="timed out"):
            client._exchange("context", {}, 0.4)  # flake-ok: the deadline under test
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=5)
