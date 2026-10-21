"""Route tests: what leaves the backend.

Index-derived strings, transcripts, settings and broadcast-eligible appends are
redacted; ``.spec-state.json`` and the settings model are normalized before they
are served; timestamps and statuses are validated or allowlisted on the way out;
redaction fails closed; and a failure is reported with a machine-readable code
instead of being swallowed.
"""

from __future__ import annotations

import ast
import inspect
import json
import re

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _make_client,
    _never_touch_the_real_state,
    _NoopState,
    _RecordingSlot,
    _redirect_state,
)

# ── symlink containment, state normalization, create pre-checks ──────────────
#
# One test group per finding, so a regression names the finding it re-opens.


# (2) unexpiring auto-approval + unbounded nudge loop


def test_normalize_spec_state_drops_malformed_decisions():
    """`decisions: [null]` crashed the panel; it must be dropped."""
    out = routes._normalize_spec_state({"decisions": [None, 42, {"no_title": 1}]})
    assert out is not None and out["decisions"] == []


def test_normalize_spec_state_redacts_keys_not_just_values():
    """A credential in an object KEY bypassed the old value-only scrub."""
    out = routes._normalize_spec_state(
        {"decisions": [{"id": "a", "title": "t", "options": ["ok"]}], "blocking": "b"}
    )
    assert out is not None
    # Only the documented keys survive — arbitrary (possibly secret-bearing)
    # keys cannot reach the browser at all.
    assert set(out) == {"decisions", "blocking", "context"}
    assert set(out["decisions"][0]) == {"id", "title", "options", "recommended", "answer", "locked"}
    # `locked` is this backend's own field, never the agent's: normalization always
    # reports False and only _apply_recorded_answers may set it.
    assert out["decisions"][0]["locked"] is False


def test_normalize_spec_state_rejects_non_dict():
    assert routes._normalize_spec_state([1, 2, 3]) is None
    assert routes._normalize_spec_state(None) is None


def test_normalize_spec_state_caps_list_lengths():
    big = {"decisions": [{"id": str(i), "title": "t", "options": ["o"] * 100} for i in range(500)]}
    out = routes._normalize_spec_state(big)
    assert out is not None
    assert len(out["decisions"]) <= routes._MAX_DECISIONS
    assert len(out["decisions"][0]["options"]) <= routes._MAX_OPTIONS


# ── persisted transcripts are read off the loop and redacted ─────────────────


@pytest.mark.asyncio
async def test_persisted_transcript_is_served_and_redacted():
    """Non-vacuous: the offloaded fallback still returns the persisted rows, drops
    system messages and redacts content."""
    calls: list[str] = []

    class _Log:
        def read_messages(self, key):
            calls.append(key)
            return [
                {"role": "system", "content": "internal", "ts": "1"},
                {"role": "user", "content": "key is glpat-IOSFODNN7EXAMPLE ok", "ts": "2"},
                {"role": "assistant", "content": "ok", "ts": "3"},
            ]

    class _State:
        conversation_log = _Log()

        def get_slot(self, key):
            return None  # rehydrated: nothing in memory, forces the fallback

    out = await routes._serialize_messages(_State(), "spec-builder-x")
    assert calls, "the persisted transcript was never read"
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant"], roles
    assert "glpat-IOSFODNN7EXAMPLE" not in json.dumps(out), "credential not redacted"


# ── redaction fails closed ───────────────────────────────────────────────────


def test_redaction_fails_closed_without_the_security_module(monkeypatch):
    """The reported fail-open: with _HAS_SECURITY false, _redact returned the text
    UNCHANGED — and everything flowing through it is agent- or user-authored
    (spec documents, transcripts, agent-written state), so a credential in an
    agent-written file would have gone straight to the browser. The same
    fail-closed reasoning as the is_sensitive_path fallback."""
    secret = "glpat-IOSFODNN7EXAMPLE"

    # With security available, the credential is scrubbed but the prose survives.
    scrubbed = routes._redact(f"key is {secret} ok")
    assert secret not in scrubbed and "key is" in scrubbed

    monkeypatch.setattr(routes, "_HAS_SECURITY", False)
    withheld = routes._redact(f"key is {secret} ok")
    assert secret not in withheld, "raw credential served when redaction is unavailable"
    assert withheld == routes._UNSCRUBBABLE

    # Empty input still returns empty rather than the placeholder.
    assert routes._redact("") == ""


# ── slot ownership and default-model stamping ────────────────────────────────


def test_load_settings_degrades_a_credential_shaped_model_to_inherit(tmp_path, monkeypatch):
    """slot.model is serialized into dashboard payloads raw and settings.json is
    agent-writable, so a value the redactor would alter must never survive the
    read chokepoint. The marker stands in for any credential-shaped string; the
    fake redactor mirrors the real one's contract (clean text passes unchanged)."""
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(routes, "_redact", lambda t: t.replace("SECRET-MARKER", "[redacted]"))
    (tmp_path / "settings.json").write_text(json.dumps({"base_path": "", "model": "SECRET-MARKER"}))
    assert (
        routes._load_settings()["model"] == ""
    ), "a credential-shaped model survived the read chokepoint"
    # Contract check on the fake: a clean id passes through untouched.
    (tmp_path / "settings.json").write_text(json.dumps({"base_path": "", "model": "clean-model"}))
    assert routes._load_settings()["model"] == "clean-model"


@pytest.mark.asyncio
async def test_settings_write_rejects_a_credential_shaped_model(tmp_path, monkeypatch):
    """The write path is the other half of the load-chokepoint degrade: a
    credential-shaped value gets a machine-readable 400 instead of being
    persisted and riding the slot stamp to the browser."""
    monkeypatch.setattr(routes, "_redact", lambda t: t.replace("SECRET-MARKER", "[redacted]"))
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": "SECRET-MARKER"}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "model_invalid"
        # Nothing was persisted.
        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["model"] == ""


# ── failures are reported, never swallowed ───────────────────────────────────


@pytest.mark.asyncio
async def test_stop_reports_failure_instead_of_a_halt_that_did_not_happen(tmp_path, monkeypatch):
    """Same class on the stop path: reporting "planning" while the loop can still
    nudge tells the user to stop worrying about a run that is still going."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "running"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {
            "running": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "status": "executing",
            }
        }
    )

    async def _boom(*_a, **_k):
        raise OSError("autonudge store is read-only")

    monkeypatch.setattr(routes, "_halt_execution", _boom)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/running/stop", json={})
    finally:
        await client.close()

    assert resp.status == 503, await resp.text()
    assert (
        routes._load_index()["running"].get("status") == "executing"
    ), "the spec was marked stopped after a failed halt"


def test_loop_removal_does_not_swallow_failures():
    """Source guard: the helper must not go back to logging-and-continuing, and
    the two paths that stay best-effort must catch it visibly."""
    src = inspect.getsource(routes._remove_nudge_loop)
    assert "except Exception" not in src, "loop removal swallows failures again"
    delete_src = inspect.getsource(routes._handle_delete)
    assert "status=503" in delete_src and "_remove_nudge_loop" in delete_src


# ── tombstone writes hold the lock; errors carry a code ──────────────────────


def test_every_error_response_carries_a_machine_readable_code():
    """The repo's error-code contract (see test/test_error_code_contract.py): the
    dashboard renders `error` prose verbatim into a localized UI, so the `code` is
    the contract and the prose is advisory. This app-local guard keeps the file at
    zero rather than relying on the repo-wide ratchet, which only fails when the
    per-file count grows."""
    src = routes_source()
    tree = ast.parse(src)
    offenders: list[str] = []
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "json_response"):
            continue
        status = next((k.value for k in node.keywords if k.arg == "status"), None)
        if not (isinstance(status, ast.Constant) and isinstance(status.value, int)):
            continue
        if status.value < 400 or not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        keys = [k.value for k in node.args[0].keys if isinstance(k, ast.Constant)]
        if "code" not in keys:
            offenders.append(f"line {node.lineno} (status {status.value})")
            continue
        for key, value in zip(node.args[0].keys, node.args[0].values):
            if getattr(key, "value", "") == "code" and isinstance(value, ast.Constant):
                codes.add(str(value.value))
    assert not offenders, f"error responses without a `code`: {offenders}"
    bad = [c for c in codes if not re.fullmatch(r"[a-z][a-z0-9_]*", c)]
    assert not bad, f"codes must be lower_snake identifiers: {bad}"


# ── index-derived strings are redacted on egress ─────────────────────────────


@pytest.mark.asyncio
async def test_index_derived_strings_are_redacted_on_egress(tmp_path, monkeypatch):
    """The reported leak: index.json is AGENT-WRITABLE -- the worker runs inside the
    user's project -- and list/detail returned its strings verbatim, so a credential
    pattern written into a path field reached the dashboard unscrubbed. Every
    index-derived string now goes through _redact, as transcript and file content
    already did."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "leaky"
    spec_dir.mkdir(parents=True)
    # Scrub only path-shaped values: a stub that also rewrote the NAME would make
    # _usable_name drop the entry at load, testing nothing about egress.
    monkeypatch.setattr(
        routes, "_redact", lambda text: "[SCRUBBED]" if text and "/" in str(text) else text
    )
    routes._save_index(
        {
            "leaky": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "worktree_branch": "spec/leaky",
            }
        }
    )

    client.app["state"] = None
    await client.start_server()
    try:
        listing = await (await client.get(f"{_BASE}/specs")).json()
        detail = await (await client.get(f"{_BASE}/specs/leaky")).json()
    finally:
        await client.close()

    entry = listing["specs"][0]
    # The stub scrubs path-shaped values; both payloads must route these through it.
    for field in ("working_dir", "spec_dir"):
        assert entry[field] == "[SCRUBBED]", f"list leaked {field}: {entry[field]}"
        assert detail[field] == "[SCRUBBED]", f"detail leaked {field}: {detail[field]}"
    # spec_type still goes through _redact -- proven by a path-shaped value in it.
    routes._save_index({"leaky": {**routes._load_index()["leaky"], "spec_type": "feature/../etc"}})
    assert detail["context"]["worktree_branch"] == "[SCRUBBED]", "detail leaked the branch"


@pytest.mark.asyncio
async def test_malformed_timestamps_do_not_break_the_listing(tmp_path, monkeypatch):
    """The reported crash: the sort key mixed whatever the index held, so one entry
    with a string timestamp made `str < float` raise TypeError -- a 500 on EVERY list
    request, which takes the whole app dark with no way back through the UI."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    for name in ("numeric", "stringy", "junk", "missing"):
        (base / name).mkdir(parents=True)
    routes._save_index(
        {
            "numeric": {"spec_dir": str(base / "numeric"), "updated_at": 1000.0},
            "stringy": {"spec_dir": str(base / "stringy"), "updated_at": "2026-07-31"},
            "junk": {"spec_dir": str(base / "junk"), "updated_at": {"nested": 1}},
            # No timestamps at all, and a numeric created_at as the only usable field.
            "missing": {"spec_dir": str(base / "missing"), "created_at": 2000.0},
        }
    )

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs")
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 200, f"the listing crashed on a malformed timestamp: {status}"
    names = [s["name"] for s in body["specs"]]
    assert set(names) == {"numeric", "stringy", "junk", "missing"}
    # Usable timestamps still order the list; unusable ones sort last rather than raise.
    assert names[0] == "missing" and names[1] == "numeric", names


# ── slot-key ownership, and timestamps validated on egress ───────────────────


@pytest.mark.asyncio
async def test_timestamps_are_validated_on_egress(tmp_path, monkeypatch):
    """Redacting the index's STRING fields is not enough on its own:
    created_at/updated_at are whatever the agent-writable index holds, so a credential
    parked in a timestamp would reach the dashboard verbatim."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "stamped").mkdir(parents=True)
    routes._save_index(
        {
            "stamped": {
                "spec_dir": str(base / "stamped"),
                "working_dir": str(tmp_path / "wd"),
                "created_at": "glpat-IOSFODNN7EXAMPLE",
                "updated_at": {"nested": "junk"},
            }
        }
    )

    client.app["state"] = None
    await client.start_server()
    try:
        body = await (await client.get(f"{_BASE}/specs")).json()
    finally:
        await client.close()

    entry = body["specs"][0]
    assert entry["created_at"] == 0.0, f"leaked a string timestamp: {entry['created_at']!r}"
    assert entry["updated_at"] == 0.0, f"leaked a non-numeric timestamp: {entry['updated_at']!r}"
    assert isinstance(entry["created_at"], float) and isinstance(entry["updated_at"], float)
    # A real timestamp still survives the coercion.
    assert routes._numeric(1234.5) == 1234.5
    assert routes._numeric("1700000000") == 1700000000.0


# ── unusable index keys are dropped; status is allowlisted ───────────────────


@pytest.mark.asyncio
async def test_status_is_allowlisted_not_echoed(tmp_path, monkeypatch):
    """The reported echo: `_effective_status` returned the stored value verbatim
    whenever it was not "executing", so an injected string was served as the status.
    The set is closed, so an unrecognised value now reads as "planning" -- which is
    also the truth for a spec with no live loop."""
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _Idle:
        running = False

    injected = "planning glpat-IOSFODNN7EXAMPLE"
    meta = {"spec_dir": "/p/s", "status": injected}
    routes._save_index({"s": dict(meta)})
    assert await routes._effective_status("s", meta, _Idle()) == "planning"

    # The recognised values still pass through untouched.
    assert routes._known_status("planning") == "planning"
    assert routes._known_status("executing") == "executing"
    # And anything else -- wrong type, empty, unknown word -- collapses to planning.
    for value in (None, "", 17, {"nested": 1}, "deleting", "EXECUTING"):
        assert routes._known_status(value) == "planning", value


@pytest.mark.asyncio
async def test_list_serves_only_allowlisted_statuses(tmp_path, monkeypatch):
    """End to end: whatever the index holds, the payload's status is one of ours."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "odd").mkdir(parents=True)
    routes._save_index(
        {
            "odd": {
                "spec_dir": str(base / "odd"),
                "working_dir": str(tmp_path / "wd"),
                "status": "glpat-IOSFODNN7EXAMPLE",
            }
        }
    )

    client.app["state"] = None
    await client.start_server()
    try:
        body = await (await client.get(f"{_BASE}/specs")).json()
    finally:
        await client.close()

    assert [s["status"] for s in body["specs"]] == ["planning"], body["specs"]


# ── non-finite timestamps; git is killed on every exceptional exit ───────────


@pytest.mark.asyncio
async def test_non_finite_timestamps_do_not_poison_the_list_response(tmp_path, monkeypatch):
    """The reported break: `float("NaN")` succeeds, so a non-finite timestamp passed
    the round-55 egress check and `json.dumps` wrote it as bare `NaN` -- not JSON.
    `JSON.parse` throws on the whole document, so one poisoned spec took out the
    entire list. Asserts on the RAW body text, because the client's .json() is the
    lenient Python parser and would happily accept what a browser rejects."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    for name in ("nan-stamp", "inf-stamp", "healthy"):
        (base / name).mkdir(parents=True)
    routes._save_index(
        {
            "nan-stamp": {
                "spec_dir": str(base / "nan-stamp"),
                "working_dir": str(tmp_path / "wd"),
                "created_at": "NaN",
                "updated_at": 1.0,
            },
            "inf-stamp": {
                "spec_dir": str(base / "inf-stamp"),
                "working_dir": str(tmp_path / "wd"),
                "created_at": float("inf"),
                "updated_at": float("-inf"),
            },
            "healthy": {
                "spec_dir": str(base / "healthy"),
                "working_dir": str(tmp_path / "wd"),
                "created_at": 1700000000.0,
                "updated_at": 1700000001.0,
            },
        }
    )

    client.app["state"] = None
    await client.start_server()
    try:
        raw = await (await client.get(f"{_BASE}/specs")).text()
    finally:
        await client.close()

    # The browser's parser, not Python's: strict JSON has no NaN or Infinity.
    assert "NaN" not in raw, f"emitted invalid JSON: {raw[:400]}"
    assert "Infinity" not in raw, f"emitted invalid JSON: {raw[:400]}"
    parsed = json.loads(raw, parse_constant=_reject_json_constant)
    stamps = {s["name"]: (s["created_at"], s["updated_at"]) for s in parsed["specs"]}
    assert stamps["nan-stamp"] == (0.0, 1.0)
    assert stamps["inf-stamp"] == (0.0, 0.0)
    # The healthy spec is untouched: one bad neighbour must not flatten the rest.
    assert stamps["healthy"] == (1700000000.0, 1700000001.0)


def _reject_json_constant(name):
    raise AssertionError(f"response carried the non-JSON constant {name}")


def test_numeric_rejects_every_non_finite_float():
    """The class, at the helper: `float()` accepts four spellings of non-finite and
    every one of them is unrepresentable in JSON."""
    for value in ("NaN", "nan", "Infinity", "-inf", float("nan"), float("inf")):
        assert routes._numeric(value) == 0.0, f"{value!r} survived the coercion"
    assert routes._numeric(1700000000.5) == 1700000000.5


# ── broadcast-eligible appends must not carry raw caller text ──


#: Roles _ChatSlot.append suppresses the global SSE push for. Everything else is
#: broadcast to every connected dashboard client, so its content leaves the
#: process and must be redacted first. "chunk"/"done" are skipped
#: unconditionally; "user" is skipped only because this app never passes the
#: host's ``broadcast_user=True`` opt-in (guarded by
#: test_the_host_still_exempts_only_these_roles_from_broadcast).
_NON_BROADCAST_ROLES = ("chunk", "done", "user")


def test_the_host_still_exempts_only_these_roles_from_broadcast():
    """Fixture guard. The rule below is derived from _ChatSlot.append's skip set;
    if the host changes it, the derivation is stale and must be revisited rather
    than silently protecting the wrong roles.

    The host skips "chunk"/"done" unconditionally, and skips "user" unless the
    caller opts in via ``broadcast_user=True`` (added so a message typed in a
    CHANNEL renders in its dashboard tab -- nothing rendered it optimistically
    there). This app never passes that opt-in, so all three roles are still
    non-broadcast HERE; the third assertion is what keeps that true.
    """
    import inspect

    from kiro_crew.dashboard.state import _ChatSlot

    src = inspect.getsource(_ChatSlot.append)
    assert 'role not in ("chunk", "done")' in src, (
        "the host's unconditional broadcast skip set changed; " "_NON_BROADCAST_ROLES is now stale"
    )
    assert '(role != "user" or broadcast_user)' in src, (
        "the host no longer skips user rows by default; _NON_BROADCAST_ROLES is " "now stale"
    )
    assert "broadcast_user" not in inspect.getsource(routes._dispatch_turn), (
        "this app now opts into broadcasting user rows, so 'user' is broadcast "
        "and must be REMOVED from _NON_BROADCAST_ROLES (its content would reach "
        "every dashboard client unredacted)"
    )


def test_queued_append_is_redacted_before_it_is_broadcast(monkeypatch):
    """`queued` is broadcast, so the credential in a queued message would have
    gone to every connected dashboard client verbatim."""
    slot = _RecordingSlot()
    secret = "ghp" + "_" + "0123456789abcdef0123456789abcdef0123"

    routes._dispatch_turn(_NoopState(), slot, f"deploy with {secret} please")

    assert slot.appended, "nothing was appended for a running slot"
    role, content = slot.appended[-1]
    assert role == "queued"
    assert (
        secret not in content
    ), "the queued message reached the broadcast path with the credential intact"
    # The queue itself still carries the real text: the agent must receive what
    # the user actually typed. Only the broadcast copy is scrubbed.
    assert (
        slot.queued and secret in slot.queued[-1]
    ), "the redaction leaked into the queue, so the agent would get scrubbed input"


def test_no_broadcast_eligible_append_passes_raw_caller_text():
    """Class guard: every slot.append whose role is NOT in the host's skip set
    must route its content through the app's scrubber.

    The `user` append is deliberately exempt -- `user` IS skipped, and the host's
    own send path stores raw for the same reason (the author is the only reader;
    redaction happens at the emit sites).
    """
    src = routes_source()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "append"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "slot"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[0], ast.Constant):
            continue
        role = node.args[0].value
        if role in _NON_BROADCAST_ROLES:
            continue
        arg = node.args[1]
        scrubbed = (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "_redact"
        )
        if not scrubbed:
            offenders.append(f"{role} at line {node.lineno}")
    assert not offenders, (
        "these appends are broadcast to every dashboard client but pass content "
        f"that did not go through the app scrubber: {offenders}"
    )


# ── the settings egress redacts like every other stored value ──


@pytest.mark.asyncio
async def test_get_settings_redacts_an_agent_written_base_path(tmp_path, monkeypatch):
    """settings.json is agent-writable and _load_settings validates only its SHAPE,
    so a credential parked in base_path would be rendered verbatim in the dashboard.
    """
    secret = "ghp" + "_" + "0123456789abcdef0123456789abcdef0123"
    async with _make_client(monkeypatch, tmp_path) as client:
        routes._save_settings({"base_path": f"/srv/{secret}/specs"})

        resp = await client.get(f"{_BASE}/settings")

        assert resp.status == 200
        body = await resp.json()
        assert (
            secret not in body["base_path"]
        ), "the agent-written base_path reached the dashboard with the credential intact"


@pytest.mark.asyncio
async def test_get_settings_still_returns_an_ordinary_path_unchanged(tmp_path, monkeypatch):
    """The redaction must not mangle a normal path -- otherwise the picker would
    show a scrubbed value and a round-trip save would corrupt the setting."""
    async with _make_client(monkeypatch, tmp_path) as client:
        ordinary = str(tmp_path / "projects" / "specs")
        routes._save_settings({"base_path": ordinary})

        resp = await client.get(f"{_BASE}/settings")

        assert (await resp.json())["base_path"] == ordinary


def test_every_handler_that_returns_settings_redacts_it():
    """Class guard, keyed on RETURNING rather than on reading.

    _resolve_spec_dir and the containment check also read _load_settings, but they
    use the value to build or validate a path -- redacting there would corrupt the
    path. So the rule is: a handler that reads settings AND returns a response must
    redact. Asserts on the calls, not on a comment.
    """
    src = routes_source()
    tree = ast.parse(src)
    offenders = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_handle_"):
            continue
        body_src = ast.get_source_segment(src, node) or ""
        if "_load_settings" not in body_src:
            continue
        if "json_response" not in body_src:
            continue
        if "_redact(" not in body_src:
            offenders.append(node.name)
    assert not offenders, (
        "these handlers return agent-writable settings without the app scrubber, so a "
        f"credential in the file reaches the dashboard raw: {offenders}"
    )
