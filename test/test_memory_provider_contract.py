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
    "provider": {"name": "fixture", "version": "1.0.0"},
    "egress": "local",
    "read_only": False,
    "ops": ["context", "search", "get", "list", "put", "delete", "put_batch", "changes"],
    "kinds": ["rule", "finding", "preference", "fact", "document"],
    "search": {"lexical": True, "semantic": False, "filters": ["kind"]},
    "writes": {
        "verbatim": True,
        "preserves_timestamps": True,
        "keeps_origin": True,
        "idempotent": True,
        "max_batch": 500,
        "max_text": 64_000,
    },
    "history": {"revisions": False, "retired": True, "changes": True},
    "ingest": "raw",
    "isolation": {"multi_store": True, "principal_auth": False},
    "documents": "readwrite",
    "steering": "none",
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


def test_a_full_connect_answer_has_no_gaps_and_round_trips() -> None:
    caps = contract.capabilities_from_wire(_CAPS)
    assert caps.mode_gaps("replace") == [] and caps.mode_gaps("augment") == []
    assert caps.max_batch == contract.MAX_BATCH and caps.max_page == contract.MAX_PAGE
    assert caps.holds_documents and caps.changes and caps.has("changes")
    again = contract.capabilities_from_wire(caps.to_wire())
    assert again == caps


def test_read_only_capabilities_turn_writes_off_and_cap_the_page() -> None:
    caps = contract.capabilities_from_wire({**_CAPS, "read_only": True})
    assert not caps.supports_lessons and not caps.supports_delete and caps.ingest == "none"
    assert not caps.verbatim and not caps.idempotent and caps.documents == "read"
    assert caps.max_page == contract.MAX_PAGE
    assert set(caps.replace_gaps()) == {
        "read_only",
        "op:put",
        "op:delete",
        "writes.verbatim",
        "writes.idempotent",
    }


@pytest.mark.parametrize(
    "change, gap",
    [
        ({"ops": ["context", "search", "get", "list", "put"]}, "op:delete"),
        ({"kinds": ["rule", "preference", "fact"]}, "kind:finding"),
        ({"writes": {**_CAPS["writes"], "verbatim": False}}, "writes.verbatim"),
        ({"writes": {**_CAPS["writes"], "idempotent": False}}, "writes.idempotent"),
    ],
    ids=["delete", "finding", "verbatim", "idempotent"],
)
def test_each_replace_requirement_is_named_as_a_gap(change, gap) -> None:
    caps = contract.capabilities_from_wire({**_CAPS, **change})
    assert caps.mode_gaps("replace") == [gap]


def test_augment_needs_context_or_search_and_context_is_required() -> None:
    caps = contract.capabilities_from_wire({**_CAPS, "ops": ["context"]})
    assert caps.mode_gaps("augment") == []
    with pytest.raises(ProviderInvalidResponse):
        contract.capabilities_from_wire({**_CAPS, "ops": ["search"]})


def test_a_change_feed_or_history_op_needs_its_history_flag() -> None:
    history = {"revisions": False, "retired": False, "changes": False}
    caps = contract.capabilities_from_wire(
        {**_CAPS, "ops": [*_CAPS["ops"], "revisions"], "history": history}
    )
    assert not caps.has("changes") and not caps.has("revisions")


@pytest.mark.parametrize(
    "change",
    [
        {"api_version": 2},
        {"egress": "cloud"},
        {"ingest": "all"},
        {"kinds": ["rule", "x"]},
        {"ops": ["context", "teleport"]},
        {"steering": "readwrite"},
        {"documents": "all"},
        {"writes": {"verbatim": "yes"}},
        {"search": {"filters": ["colour"]}},
    ],
)
def test_capabilities_outside_the_contract_are_invalid(change) -> None:
    with pytest.raises(ProviderInvalidResponse):
        contract.capabilities_from_wire({**_CAPS, **change})


def test_the_envelope_keeps_timestamps_origin_revision_and_reserved_meta() -> None:
    wire = {
        "id": "m1",
        "kind": "rule",
        "text": "Use tabs.",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_500.0,
        "status": "retired",
        "origin": {"provider": "builtin", "store": "default", "id": "lesson:abc"},
        "revision": 3,
        "meta": {"crew.source": "user_explicit", "crew.retired_reason": "superseded"},
    }
    item = contract.item_from_wire(wire)
    assert item.to_wire() == wire
    new = contract.new_item_of(item)
    assert new.created_at == 1_700_000_000.0 and new.origin == item.origin
    assert contract.new_item_from_wire(new.to_wire()) == new


def test_a_document_item_has_its_own_cap_and_names_its_document() -> None:
    text = "# Preferences\n" + "a" * 20_000
    item = contract.item_from_wire({"id": "d", "kind": "document", "text": text})
    assert item.text == text
    with pytest.raises(ValueError, match="crew.doc"):
        contract.new_item_from_wire({"kind": "document", "text": text})
    ok = contract.new_item_from_wire(
        {"kind": "document", "text": text, "meta": {"crew.doc": "preferences"}}
    )
    assert ok.meta["crew.doc"] == "preferences"
    with pytest.raises(ValueError, match="longer than"):
        contract.new_item_from_wire(
            {"kind": "document", "text": "a" * 64_001, "meta": {"crew.doc": "projects"}}
        )
    with pytest.raises(ValueError, match="longer than"):
        contract.new_item_from_wire({"kind": "fact", "text": "a" * (MAX_ITEM_TEXT + 1)})


def test_batch_results_answer_once_per_entry() -> None:
    raw = {"results": [{"outcome": "inserted", "id": "a"}, {"outcome": "deduped", "id": "b"}]}
    assert [r.id for r in contract.write_results_from_wire(raw, 2)] == ["a", "b"]
    with pytest.raises(ProviderInvalidResponse):
        contract.write_results_from_wire(raw, 3)


def test_the_connect_request_names_versions_mode_and_kinds() -> None:
    scope = contract.ProviderScope("default", None, "persistent", "connect")
    body = contract.connect_request("replace", scope)
    assert body["crew"]["api_versions"] == [1] and body["crew"]["want_mode"] == "replace"
    assert "document" in body["crew"]["kinds"]


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
