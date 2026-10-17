"""Provider-provenance security boundary for SharePoint locator redaction."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from kiro_crew import history
from kiro_crew import locator_attestations as registry
from kiro_crew import platform_compat
from kiro_crew.acp import _dispatch, locator_attestation
from kiro_crew.dashboard import chat_utils
from kiro_crew.platform.defaults import DefaultCredentialPolicy
from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor, redact_credentials
from kiro_crew.testing.wait import await_until, default_timeout

_KEY = "Kx3Q51tPusVkD0URlGfMmNbVc7Z8yJhLpQrStUwZ"
_ID = f"IgCBh-{_KEY}"
_URL = f"https://contoso.sharepoint.com/:f:/s/LaunchMaterials/{_ID}?e=zzO8op"
_OTHER = _URL.replace(_ID, "Eabcd-" + _KEY)
_THIRD = _URL.replace(_ID, "E1111-" + _KEY)
_FIELDS = frozenset({"webUrl"})
_HOSTS = frozenset({"Contoso.SharePoint.com"})


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / registry.STORE_DIR_NAME / registry.STORE_FILE_NAME
    _floor_monkeypatch.setattr(registry, "_path_override", path)
    _floor_monkeypatch.setattr(registry, "_snapshot", None)
    _floor_monkeypatch.setattr(registry, "_loading", False)
    _floor_monkeypatch.setattr(registry, "_load_thread", None)
    _floor_monkeypatch.setattr(registry, "_next_validation_ns", 0)
    chat_utils._clear_display_redaction_cache()
    yield path
    thread = registry._load_thread
    if thread is not None:
        thread.join(timeout=default_timeout())
        assert not thread.is_alive()
    chat_utils._clear_display_redaction_cache()


def _attest(url: str = _URL) -> int:
    return registry.attest_provider_result(
        update={"rawOutput": {"webUrl": url}},
        raw_input={},
        declared_fields=_FIELDS,
        trusted_hosts=_HOSTS,
    )


def _redact_with_attestations(text: str) -> tuple[str, list[str]]:
    return redact_credentials(text, attested_sharepoint_digests=registry.snapshot()[1])


async def _attest_and_parse(update: dict[str, Any], **kwargs: Any) -> list[_dispatch.AcpEvent]:
    await _dispatch.attest_session_update(
        update,
        raw_params_cache=kwargs.get("raw_params_cache"),
        mcp_server_name_cache=kwargs.get("mcp_server_name_cache"),
        tool_name_cache=kwargs.get("tool_name_cache"),
        cache_scope=kwargs.get("cache_scope", ""),
        identity_unreadable_cache=kwargs.get("identity_unreadable_cache"),
    )
    return _dispatch.parse_session_update(update, **kwargs)


def test_forged_url_text_never_attests_itself() -> None:
    out, warnings = redact_credentials(_URL)
    assert REDACTED_CREDENTIAL_TAG in out
    assert _KEY not in out
    assert warnings


def test_declared_provider_field_persists_only_canonical_digest(
    _isolated_registry: Path,
) -> None:
    assert _attest() == 1
    assert _redact_with_attestations(_URL) == (_URL, [])
    payload = _isolated_registry.read_text()
    assert "https://" not in payload
    assert "sharepoint.com" not in payload
    assert _ID not in payload
    assert registry._locator_digests(_URL).pop() in payload
    if not platform_compat.IS_WINDOWS:
        assert oct(_isolated_registry.stat().st_mode & 0o777) == "0o600"


def test_declared_path_extracts_only_its_exact_scalar_leaf() -> None:
    update = {
        "rawOutput": {
            "webUrl": _URL,
            "body": json.dumps({"webUrl": _OTHER}),
            "document": {"webUrl": _OTHER},
            "items": [{"webUrl": _OTHER}],
        }
    }
    assert (
        registry.attest_provider_result(
            update=update,
            raw_input={},
            declared_fields={"webUrl"},
            trusted_hosts=_HOSTS,
        )
        == 1
    )
    assert _redact_with_attestations(_URL) == (_URL, [])
    assert REDACTED_CREDENTIAL_TAG in redact_credentials(_OTHER)[0]


def test_declared_nested_path_crosses_only_provider_envelope_lists() -> None:
    update = {
        "rawOutput": {
            "items": [
                {
                    "webUrl": _URL,
                    "document": {"webUrl": _OTHER},
                }
            ]
        }
    }
    assert (
        registry.attest_provider_result(
            update=update,
            raw_input={},
            declared_fields={"items.webUrl"},
            trusted_hosts=_HOSTS,
        )
        == 1
    )
    assert _redact_with_attestations(_URL) == (_URL, [])
    assert REDACTED_CREDENTIAL_TAG in redact_credentials(_OTHER)[0]


def test_same_call_input_echo_is_rejected() -> None:
    assert (
        registry.attest_provider_result(
            update={"rawOutput": {"webUrl": _URL}},
            raw_input={"url": _URL},
            declared_fields=_FIELDS,
            trusted_hosts=_HOSTS,
        )
        == 0
    )
    assert REDACTED_CREDENTIAL_TAG in redact_credentials(_URL)[0]


def test_absent_truncated_or_incomplete_input_refuses_attestation() -> None:
    update = {"rawOutput": {"webUrl": _URL}}
    common = {"update": update, "declared_fields": _FIELDS, "trusted_hosts": _HOSTS}
    assert registry.attest_provider_result(raw_input=None, **common) == 0
    oversized = {"values": [None] * (registry.MAX_COLLECTION_ENTRIES + 1)}
    assert registry.attest_provider_result(raw_input=oversized, **common) == 0
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(registry.MAX_PATH_SEGMENTS + 2):
        child: dict[str, object] = {}
        cursor["next"] = child
        cursor = child
    assert registry.attest_provider_result(raw_input=nested, **common) == 0
    assert REDACTED_CREDENTIAL_TAG in redact_credentials(_URL)[0]


@pytest.mark.parametrize(
    "url,hosts",
    [
        (_URL.replace("contoso", "attacker"), _HOSTS),
        (_URL.replace("/:f:/s/", "/:f:/r/"), _HOSTS),
        (_URL.replace(_ID, _ID + "A"), _HOSTS),
        (_URL.replace("https://", "http://"), _HOSTS),
        (_URL, frozenset({"attacker.sharepoint.com"})),
    ],
)
def test_wrong_host_scheme_route_or_id_earns_no_attestation(
    url: str, hosts: frozenset[str]
) -> None:
    assert (
        registry.attest_provider_result(
            update={"rawOutput": {"webUrl": url}},
            raw_input={},
            declared_fields=_FIELDS,
            trusted_hosts=hosts,
        )
        == 0
    )


def test_undeclared_neighbor_field_is_ignored() -> None:
    assert (
        registry.attest_provider_result(
            update={"rawOutput": {"display": _URL}},
            raw_input={},
            declared_fields=_FIELDS,
            trusted_hosts=_HOSTS,
        )
        == 0
    )


def test_batch_stream_punctuation_and_slack_forms_preserve_only_attested_locator() -> None:
    assert _attest() == 1
    text = f"See {_URL}., then <{_URL}|Launch materials>."
    assert _redact_with_attestations(text) == (text, [])

    redactor = StreamRedactor(lambda value: _redact_with_attestations(value)[0])
    chunks = (
        "See https://contoso.sharepoint.com/:",
        f"f:/s/LaunchMaterials/{_ID[:15]}",
        f"{_ID[15:]}?e=zzO8op.",
    )
    streamed = "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()
    assert streamed == f"See {_URL}."


def test_neighboring_secret_and_fixed_secret_inside_locator_stay_redacted() -> None:
    assert _attest() == 1
    adjacent = f"{_URL} {_KEY}"
    out, warnings = _redact_with_attestations(adjacent)
    assert _URL in out
    assert out.endswith(REDACTED_CREDENTIAL_TAG)
    assert warnings

    fixed = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"
    forged = _URL.replace(_ID, f"IgCBh-{fixed}")
    out, warnings = redact_credentials(forged)
    assert fixed not in out
    assert warnings


def test_corrupt_existing_file_is_never_replaced(_isolated_registry: Path) -> None:
    _isolated_registry.parent.mkdir(parents=True)
    original = b"{not-json"
    _isolated_registry.write_bytes(original)
    assert _attest() == 0
    assert _isolated_registry.read_bytes() == original
    assert registry.snapshot()[1] == frozenset()
    assert REDACTED_CREDENTIAL_TAG in _redact_with_attestations(_URL)[0]


def test_unreadable_existing_file_is_never_replaced(
    _isolated_registry: Path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_registry.parent.mkdir(parents=True)
    original = b"not-readable"
    _isolated_registry.write_bytes(original)

    def refuse_open(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("refused by test")

    _floor_monkeypatch.setattr(registry.os, "open", refuse_open)
    assert _attest() == 0
    assert _isolated_registry.read_bytes() == original


def test_hardlinked_existing_file_is_never_replaced(_isolated_registry: Path) -> None:
    assert _attest() == 1
    original = _isolated_registry.read_bytes()
    linked = _isolated_registry.with_name("linked.json")
    try:
        os.link(_isolated_registry, linked)
    except OSError:
        pytest.skip("hardlink creation unavailable")
    assert _isolated_registry.stat().st_nlink > 1
    assert _attest(_OTHER) == 0
    assert _isolated_registry.read_bytes() == original
    assert linked.read_bytes() == original
    assert REDACTED_CREDENTIAL_TAG in redact_credentials(_OTHER)[0]


def test_restart_reload_recovers_links_and_first_write_preserves_prior_file() -> None:
    assert _attest() == 1
    registry._snapshot = None
    assert _attest(_OTHER) == 1
    registry._snapshot = None
    registry.preload()
    assert _redact_with_attestations(_URL) == (_URL, [])
    assert _redact_with_attestations(_OTHER) == (_OTHER, [])


def test_flat_locator_lru_touches_and_evicts_per_digest(
    _floor_monkeypatch: pytest.MonkeyPatch,
    _isolated_registry: Path,
) -> None:
    _floor_monkeypatch.setattr(registry, "MAX_ATTESTATIONS", 2)
    assert _attest(_URL) == 1
    assert _attest(_OTHER) == 1
    assert _attest(_URL) == 1
    assert _attest(_THIRD) == 1
    state = json.loads(_isolated_registry.read_text())
    assert "workspaces" not in state
    assert set(state) == {"version", "generation", "clock", "entries"}
    assert [entry["digest"] for entry in state["entries"]] == [
        registry._locator_digests(_THIRD).pop(),
        registry._locator_digests(_URL).pop(),
    ]
    assert REDACTED_CREDENTIAL_TAG in redact_credentials(_OTHER)[0]
    assert _redact_with_attestations(_THIRD) == (_THIRD, [])


def test_adapter_is_exact_and_every_exception_fails_to_no_attestation(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = SimpleNamespace(
        exempt_exact_hosts=lambda: _HOSTS,
        provider_generated_sharepoint_fields=lambda: {
            ("amazon-sharepoint-mcp", "sharepoint_read_file"): _FIELDS
        },
    )
    _floor_monkeypatch.setattr(
        locator_attestation, "installed_context", lambda: SimpleNamespace(credentials=credentials)
    )
    assert locator_attestation.sharepoint_attestation_policy(
        "amazon-sharepoint-mcp", "sharepoint_read_file"
    ) == (_FIELDS, _HOSTS)
    assert locator_attestation.sharepoint_attestation_policy("other", "sharepoint_read_file") == (
        frozenset(),
        frozenset(),
    )

    credentials.provider_generated_sharepoint_fields = lambda: (_ for _ in ()).throw(
        RuntimeError("adapter failed")
    )
    assert locator_attestation.sharepoint_attestation_policy(
        "amazon-sharepoint-mcp", "sharepoint_read_file"
    ) == (frozenset(), frozenset())
    assert DefaultCredentialPolicy().provider_generated_sharepoint_fields() == {}


@pytest.mark.asyncio
async def test_missing_gateway_raw_input_cache_refuses_attestation(
    _floor_monkeypatch: pytest.MonkeyPatch,
    _isolated_registry: Path,
) -> None:
    key = _dispatch.scoped_tool_cache_key("session-a", "call-1")
    _floor_monkeypatch.setattr(
        _dispatch,
        "sharepoint_attestation_policy",
        lambda _server, _tool: (_FIELDS, _HOSTS),
    )
    await _dispatch.attest_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-1",
            "status": "completed",
            "rawOutput": {"webUrl": _URL},
        },
        cache_scope="session-a",
        raw_params_cache={},
        mcp_server_name_cache={key: "amazon-sharepoint-mcp"},
        tool_name_cache={key: "sharepoint_read_file"},
        identity_unreadable_cache={key: False},
    )
    assert not _isolated_registry.exists()


@pytest.mark.asyncio
async def test_async_parser_offloads_attestation_before_result_redaction(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    loop_ticked = asyncio.Event()
    release = threading.Event()
    attested = False

    def blocked_attestation(**_kwargs: object) -> int:
        nonlocal attested
        loop.call_soon_threadsafe(started.set)
        assert release.wait(default_timeout())  # flake-ok: bounded wait on test-owned signal
        attested = True
        return 1

    def ordered_redaction(text: str) -> str:
        assert attested
        return text

    _floor_monkeypatch.setattr(_dispatch, "attest_tool_result", blocked_attestation)
    _floor_monkeypatch.setattr(_dispatch, "_redact", ordered_redaction)
    _floor_monkeypatch.setattr(
        _dispatch,
        "sharepoint_attestation_policy",
        lambda _server, _tool: (_FIELDS, _HOSTS),
    )
    key = _dispatch.scoped_tool_cache_key("session-a", "call-1")
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "status": "completed",
        "rawOutput": {"webUrl": _URL},
    }
    task = asyncio.create_task(
        _attest_and_parse(
            update,
            cache_scope="session-a",
            raw_params_cache={key: {"site": "contoso"}},
            mcp_server_name_cache={key: "amazon-sharepoint-mcp"},
            tool_name_cache={key: "sharepoint_read_file"},
            identity_unreadable_cache={key: False},
        )
    )
    try:
        await await_until(started, what="attestation worker starting")
        loop.call_soon(loop_ticked.set)
        await await_until(loop_ticked, what="event loop progressing during persistence")
        assert not task.done()
    finally:
        release.set()
    events = await await_until(task, what="attested result parsing")
    assert len(events) == 1
    assert _URL in events[0].tool_output


@pytest.mark.asyncio
async def test_async_gateway_result_uses_identity_cache_and_rejects_echo(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _floor_monkeypatch.setattr(
        _dispatch,
        "sharepoint_attestation_policy",
        lambda _server, _tool: (_FIELDS, _HOSTS),
    )
    key = _dispatch.scoped_tool_cache_key("session-a", "call-1")
    common = {
        "cache_scope": "session-a",
        "mcp_server_name_cache": {key: "amazon-sharepoint-mcp"},
        "tool_name_cache": {key: "sharepoint_read_file"},
        "identity_unreadable_cache": {key: False},
    }
    events = await _attest_and_parse(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-1",
            "status": "completed",
            "rawOutput": {"webUrl": _URL},
        },
        raw_params_cache={key: {"site": "contoso"}},
        **common,
    )
    assert events and _URL in events[0].tool_output

    key2 = _dispatch.scoped_tool_cache_key("session-b", "call-2")
    events = await _attest_and_parse(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-2",
            "status": "completed",
            "rawOutput": {"webUrl": _OTHER},
        },
        cache_scope="session-b",
        raw_params_cache={key2: {"source": _OTHER}},
        mcp_server_name_cache={key2: "amazon-sharepoint-mcp"},
        tool_name_cache={key2: "sharepoint_read_file"},
        identity_unreadable_cache={key2: False},
    )
    assert events and _OTHER not in events[0].tool_output


def test_nonterminal_or_failed_result_never_attests() -> None:
    caches = {
        "raw_input": {"site": "contoso"},
        "declared_fields": _FIELDS,
        "trusted_hosts": _HOSTS,
    }
    for status in ("running", "failed"):
        assert (
            locator_attestation.attest_tool_result(
                update={"status": status, "rawOutput": {"webUrl": _URL}}, **caches
            )
            == 0
        )
    assert REDACTED_CREDENTIAL_TAG in redact_credentials(_URL)[0]


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks unavailable")
def test_symlinked_registry_is_never_read_or_replaced(_isolated_registry: Path) -> None:
    assert _attest() == 1
    payload = _isolated_registry.read_bytes()
    alternate = _isolated_registry.with_name("alternate.json")
    alternate.write_bytes(payload)
    _isolated_registry.unlink()
    try:
        _isolated_registry.symlink_to(alternate)
    except OSError:
        pytest.skip("symlink creation unavailable")
    registry._snapshot = None
    registry.preload()
    assert registry.snapshot()[1] == frozenset()
    assert REDACTED_CREDENTIAL_TAG in _redact_with_attestations(_URL)[0]
    assert _attest(_OTHER) == 0
    assert _isolated_registry.is_symlink()
    assert alternate.read_bytes() == payload


def test_windows_registry_read_uses_retrying_reader(
    _isolated_registry: Path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _attest() == 1
    calls: list[int | None] = []
    real = registry.read_bytes_with_retry

    def counted(path: Path, *, max_bytes: int | None = None) -> bytes:
        calls.append(max_bytes)
        return real(path, max_bytes=max_bytes)

    _floor_monkeypatch.setattr(registry.platform_compat, "IS_WINDOWS", True)
    _floor_monkeypatch.setattr(registry, "read_bytes_with_retry", counted)
    registry._snapshot = None
    registry.preload()
    assert calls == [registry.MAX_FILE_BYTES + 1]
    assert registry.snapshot()[1]
    assert _redact_with_attestations(_URL) == (_URL, [])


def test_external_generation_refreshes_off_thread(
    _isolated_registry: Path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _attest() == 1
    before = registry.snapshot()
    state = json.loads(_isolated_registry.read_text())
    state["clock"] += 1
    state["generation"] += 1
    state["entries"].insert(
        0, {"digest": registry._locator_digests(_OTHER).pop(), "seen": state["clock"]}
    )
    registry.atomic_write(
        _isolated_registry,
        json.dumps(state, separators=(",", ":"), sort_keys=True),
        fsync=True,
        mode=0o600,
        restrict_to_owner=True,
    )

    caller_thread = threading.get_ident()
    read_threads: list[int] = []
    real_read = registry._read_file

    def tracked_read(*, strict: bool = False):  # type: ignore[no-untyped-def]
        read_threads.append(threading.get_ident())
        return real_read(strict=strict)

    _floor_monkeypatch.setattr(registry, "_read_file", tracked_read)
    registry._next_validation_ns = 0
    assert registry.snapshot() == before
    thread = registry._load_thread
    assert thread is not None
    thread.join(timeout=default_timeout())  # flake-ok: bounded join on test-owned thread
    assert not thread.is_alive()
    assert read_threads and all(value != caller_thread for value in read_threads)
    after = registry.snapshot()
    assert after[0] == before[0] + 1
    assert _redact_with_attestations(_OTHER) == (_OTHER, [])


@pytest.mark.asyncio
async def test_gateway_preload_runs_post_ready_off_loop_in_both_server_modes(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    from kiro_crew.dashboard import server

    caller_thread = threading.get_ident()
    preload_threads: list[int] = []
    state = SimpleNamespace(ready=True, _background_tasks=set())

    def capture_preload() -> None:
        assert state.ready
        preload_threads.append(threading.get_ident())

    _floor_monkeypatch.setattr(registry, "preload", capture_preload)
    server._dispatch_locator_attestation_preload(state)
    assert state._background_tasks
    await asyncio.gather(*tuple(state._background_tasks))
    assert preload_threads and preload_threads[0] != caller_thread
    for entrypoint in (server.start_dashboard, server.start_api_server):
        source = inspect.getsource(entrypoint)
        assert source.index("state.ready = True") < source.index(
            "_dispatch_locator_attestation_preload(state)"
        )
        assert "await _preload_locator_attestations()" not in source


def test_snapshot_caches_digest_frozenset() -> None:
    assert _attest() == 1
    snapshot = registry._snapshot
    assert snapshot is not None
    assert isinstance(snapshot[1], int)
    assert isinstance(snapshot[2], frozenset)
    generation, digests = registry.snapshot()
    assert generation == snapshot[1]
    assert digests is snapshot[2]
    assert _attest(_OTHER) == 1
    assert registry._snapshot is not snapshot
    assert snapshot[2] == frozenset(registry._locator_digests(_URL))


def test_non_sharepoint_text_skips_registry_lookup(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _floor_monkeypatch.setattr(
        registry,
        "snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("registry lookup ran")),
    )
    assert registry.digests_for_text("ordinary text") == frozenset()


def test_registry_generation_invalidates_display_cache() -> None:
    assert _URL not in chat_utils._redact_for_display(_URL)
    before = chat_utils._display_redaction_cache_key(_URL)[0]
    assert _attest() == 1
    after = chat_utils._display_redaction_cache_key(_URL)[0]
    assert after != before
    assert chat_utils._redact_for_display(_URL) == _URL


def test_non_gateway_redacts_while_gateway_display_and_history_preserve() -> None:
    assert _attest() == 1
    public, warnings = redact_credentials(_URL)
    assert REDACTED_CREDENTIAL_TAG in public
    assert warnings
    assert _dispatch.redact_text(_URL) == _URL
    assert chat_utils._redact_for_display(_URL) == _URL
    assert history._redact_at_write_boundary("assistant", _URL) == _URL


def test_restart_display_fails_closed_until_snapshot_load_completes() -> None:
    assert _attest() == 1
    registry._snapshot = None
    chat_utils._clear_display_redaction_cache()
    assert REDACTED_CREDENTIAL_TAG in chat_utils._redact_for_display(_URL)
    thread = registry._load_thread
    assert thread is not None
    thread.join(timeout=default_timeout())  # flake-ok: bounded join on test-owned thread
    assert not thread.is_alive()
    chat_utils._clear_display_redaction_cache()
    assert chat_utils._redact_for_display(_URL) == _URL


@pytest.mark.asyncio
async def test_empty_public_policy_performs_no_thread_hop(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _dispatch.scoped_tool_cache_key("session-a", "call-1")

    def no_thread_hop(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("public policy scheduled a worker")

    _floor_monkeypatch.setattr(
        _dispatch,
        "sharepoint_attestation_policy",
        lambda _server, _tool: (frozenset(), frozenset()),
    )
    _floor_monkeypatch.setattr(_dispatch.asyncio, "to_thread", no_thread_hop)
    await _dispatch.attest_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-1",
            "status": "completed",
            "rawOutput": {"webUrl": _URL},
        },
        cache_scope="session-a",
        raw_params_cache={key: {"site": "contoso"}},
        mcp_server_name_cache={key: "amazon-sharepoint-mcp"},
        tool_name_cache={key: "sharepoint_read_file"},
        identity_unreadable_cache={key: False},
    )


@pytest.mark.parametrize(
    "case",
    [
        "absent_identity_cache",
        "absent_identity_key",
        "unreadable_identity",
        "absent_raw_cache",
        "absent_raw_key",
        "incomplete_raw_input",
        "unresolved_server",
        "unresolved_tool",
    ],
)
@pytest.mark.asyncio
async def test_unreadable_or_incomplete_provenance_fails_before_thread_hop(
    case: str,
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _dispatch.scoped_tool_cache_key("session-a", "call-1")
    raw_params: dict[str, dict[str, str]] | None = {key: {"site": "contoso"}}
    servers = {key: "amazon-sharepoint-mcp"}
    tools = {key: "sharepoint_read_file"}
    unreadable: dict[str, bool] | None = {key: False}
    if case == "absent_identity_cache":
        unreadable = None
    elif case == "absent_identity_key":
        unreadable = {}
    elif case == "unreadable_identity":
        unreadable = {key: True}
    elif case == "absent_raw_cache":
        raw_params = None
    elif case == "absent_raw_key":
        raw_params = {}
    elif case == "incomplete_raw_input":
        raw_params = {key: {}}
    elif case == "unresolved_server":
        servers = {}
    elif case == "unresolved_tool":
        tools = {}

    def no_thread_hop(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"{case} scheduled a worker")

    _floor_monkeypatch.setattr(
        _dispatch,
        "sharepoint_attestation_policy",
        lambda _server, _tool: (_FIELDS, _HOSTS),
    )
    _floor_monkeypatch.setattr(_dispatch.asyncio, "to_thread", no_thread_hop)
    await _dispatch.attest_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-1",
            "status": "completed",
            "rawOutput": {"webUrl": _URL},
        },
        cache_scope="session-a",
        raw_params_cache=raw_params,
        mcp_server_name_cache=servers,
        tool_name_cache=tools,
        identity_unreadable_cache=unreadable,
    )


def test_cold_snapshot_does_not_wait_for_concurrent_fsync(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_atomic_write(*_args: object, **_kwargs: object) -> None:
        entered.set()
        assert release.wait(default_timeout())  # flake-ok: bounded wait on test-owned signal

    _floor_monkeypatch.setattr(registry, "atomic_write", blocking_atomic_write)
    writer = threading.Thread(target=_attest)
    writer.start()
    assert entered.wait(default_timeout())  # flake-ok: bounded wait on test-owned signal
    registry._snapshot = None
    observed: list[tuple[int, frozenset[str]]] = []
    reader = threading.Thread(target=lambda: observed.append(registry.snapshot()))
    reader.start()
    reader.join(timeout=default_timeout())  # flake-ok: bounded join on test-owned thread
    try:
        assert not reader.is_alive()
        assert observed == [(0, frozenset())]
    finally:
        release.set()
        writer.join(timeout=default_timeout())  # flake-ok: bounded join on test-owned thread
    assert not writer.is_alive()


def test_redaction_has_no_locator_registry_import_or_io() -> None:
    import ast
    import inspect

    import kiro_crew.security.redaction as redaction

    tree = ast.parse(inspect.getsource(redaction))
    imported = {node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert "kiro_crew" not in imported
    assert "kiro_crew.locator_attestations" not in imported
    assert "kiro_crew.security.exfil" not in imported
    assert not any(name.startswith("kiro_crew.platform") for name in imported)
    source = inspect.getsource(redaction._token_param_exempt_prefixes)
    assert "from kiro_crew.platform.context import installed_context" in source


def test_registry_is_enclosed_by_existing_agent_hidden_root() -> None:
    from kiro_crew import sandbox
    from kiro_crew.security import _SENSITIVE_HOME_DIRS

    assert any(path.endswith("/" + registry.STORE_MASK_ROOT) for path in _SENSITIVE_HOME_DIRS)
    assert registry.STORE_MASK_ROOT in sandbox._CREW_HIDDEN_LEAVES
    assert registry.STORE_MASK_ROOT in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert registry.STORE_DIR_NAME not in sandbox._CREW_HIDDEN_LEAVES


def test_registry_write_is_atomic_owner_only_and_failure_adds_nothing(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def capture(_path: Path, _content: str, **kwargs: object) -> None:
        calls.append(kwargs)

    _floor_monkeypatch.setattr(registry, "atomic_write", capture)
    assert _attest() == 1
    assert calls == [{"fsync": True, "mode": 0o600, "restrict_to_owner": True}]

    registry._snapshot = None
    _floor_monkeypatch.setattr(
        registry, "atomic_write", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    assert _attest(_OTHER) == 0
    assert REDACTED_CREDENTIAL_TAG in _redact_with_attestations(_OTHER)[0]
