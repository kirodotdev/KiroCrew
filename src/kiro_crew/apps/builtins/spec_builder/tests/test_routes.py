"""Tests for the Spec Builder builtin backend (``backend/routes.py``).

Locks in the builtin route contract and the pieces of behaviour that survive
the port from the external app:

  * ``register_routes(app)`` wires the expected ``/api/apps/spec-builder/*``
    method+path set onto the gateway aiohttp Application (builtin contract —
    full paths, no returned AppRoute list);
  * the settings endpoint round-trips (GET default → PUT → GET) and rejects a
    relative base_path;
  * spec create validates ``name`` / ``spec_type`` / ``working_dir`` before it
    ever touches gateway state;
  * ``.spec-state.json`` is redaction-scrubbed before it leaves the backend
    (credentials never reach the browser), recursively across nested values;
  * the slot-key prefix is ``spec-builder-`` (renamed from the external app).
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
import os
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder.backend import routes

_BASE = "/api/apps/spec-builder"


# ── HTTP harness ─────────────────────────────────────────────────────────────


def _redirect_state(monkeypatch, tmp_path):
    """Point the module's state files at a tmp dir and force-enable the app."""
    state_dir = tmp_path / "spec-builder"
    monkeypatch.setattr(routes, "_STATE_DIR", state_dir)
    monkeypatch.setattr(routes, "_SETTINGS_PATH", state_dir / "settings.json")
    monkeypatch.setattr(routes, "_INDEX_PATH", state_dir / "index.json")
    # Every module-level path the app writes to, not just the ones a test happens
    # to read: the tombstone file was added later and missed here, so the
    # deletion tests wrote into the USER's live state and made their real deleted
    # specs discoverable again.
    monkeypatch.setattr(routes, "_DELETED_PATH", state_dir / "deleted.json")
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    return state_dir


def _live_state_snapshot() -> dict[str, int]:
    """Names + mtimes of the guarded live state dir, or {} when there is none.

    The guarded dir is captured on FIRST call and memoized: the capture must
    happen before any test's ``_redirect_state`` patches the ``routes``
    attributes (the autouse guard calls this before redirecting, so first use
    is always un-redirected), but it must NOT happen at import time -- a
    module-level ``routes._state_dir()`` freezes whichever ``KIROCREW_HOME``
    is active at collection, defeating pod isolation and the per-test home
    isolation (the issue #874 class the lazy-paths ratchet enforces).
    """
    global _REAL_STATE_DIR
    if _REAL_STATE_DIR is None:
        _REAL_STATE_DIR = routes._state_dir()
    try:
        return {p.name: p.stat().st_mtime_ns for p in _REAL_STATE_DIR.iterdir()}
    except OSError:
        return {}


@pytest.fixture(autouse=True)
def _never_touch_the_real_state(monkeypatch, tmp_path):
    """Safety net: a test that forgets _redirect_state must still not write to
    the USER's live ~/.kiro/crew/workspace/spec-builder/.

    Two leaks got through before this was tight enough: one test called
    _save_index without redirecting at all, and later the tombstone file was
    added to the app without being added to _redirect_state -- so the deletion
    tests rewrote the real deleted.json. The guard therefore compares the WHOLE
    directory (names + mtimes) rather than one known filename, so the next file
    this app learns to write is covered without anyone remembering to list it.
    """
    before = _live_state_snapshot()
    _redirect_state(monkeypatch, tmp_path / "_autouse_state")
    yield
    assert _live_state_snapshot() == before, (
        f"a test wrote to the live state dir: {_REAL_STATE_DIR}"
    )


#: The un-redirected state dir the autouse guard watches. ``None`` until
#: :func:`_live_state_snapshot`'s first call fills it (see its docstring for
#: why capture is first-use, not import-time).
_REAL_STATE_DIR: Path | None = None


@web.middleware
async def _auth_mw(request, handler):
    """Inject a middleware-set user (mirrors the gateway's auth middleware)."""
    request["user"] = "tester"
    return await handler(request)


def _make_client(monkeypatch, tmp_path):
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    return TestClient(TestServer(app))


# ── route set (builtin contract) ─────────────────────────────────────────────


def test_register_routes_wires_expected_set(tmp_path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application()
    # Builtin contract: register_routes(app) returns None and registers full
    # paths directly on the router (the external app's AppRoute-list contract
    # was converted during the port). mypy flags asserting a None return
    # (func-returns-value), so just call it.
    routes.register_routes(app)

    wired = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.resource is not None
    }
    expected = {
        ("GET", f"{_BASE}/settings"),
        ("PUT", f"{_BASE}/settings"),
        ("POST", f"{_BASE}/settings"),
        ("GET", f"{_BASE}/repo-info"),
        ("GET", f"{_BASE}/browse"),
        ("GET", f"{_BASE}/specs"),
        ("POST", f"{_BASE}/specs"),
        ("GET", f"{_BASE}/specs/{{name}}"),
        ("GET", f"{_BASE}/specs/{{name}}/messages"),
        ("POST", f"{_BASE}/specs/{{name}}/message"),
        ("POST", f"{_BASE}/specs/{{name}}/handoff"),
        ("POST", f"{_BASE}/specs/{{name}}/execute"),
        ("POST", f"{_BASE}/specs/{{name}}/stop"),
        ("DELETE", f"{_BASE}/specs/{{name}}"),
    }
    assert expected <= wired


def test_slot_key_prefix_renamed():
    # Ported from the external app's 'kiro-specs-' prefix to 'spec-builder-'.
    assert routes._slot_key("demo") == "spec-builder-demo"


# ── settings round-trip ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_roundtrip(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.get(f"{_BASE}/settings")
        assert resp.status == 200
        assert (await resp.json())["base_path"] == ""

        abs_base = str(tmp_path / "specs-home")
        resp = await client.put(f"{_BASE}/settings", json={"base_path": abs_base})
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "base_path": abs_base}

        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["base_path"] == abs_base


@pytest.mark.asyncio
async def test_settings_rejects_relative_base_path(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(f"{_BASE}/settings", json={"base_path": "not/absolute"})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_settings_requires_auth(tmp_path, monkeypatch):
    # No auth middleware -> _require_auth returns 401.
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/settings")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_disabled_app_denies(tmp_path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: False)
    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/settings")
        assert resp.status == 403


# ── create validation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rejects_relative_working_dir(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "demo", "working_dir": "relative/path"},
        )
        assert resp.status == 400
        assert "working_dir" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_create_rejects_bad_name(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "bad name!", "working_dir": str(tmp_path)},
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_create_rejects_missing_working_dir(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        # Absolute but non-existent path.
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "demo", "working_dir": str(tmp_path / "nope")},
        )
        assert resp.status == 400


def test_valid_name_rules():
    assert routes._valid_name("feature-1")
    assert routes._valid_name("a_b-C9")
    assert not routes._valid_name("bad name")
    assert not routes._valid_name("-leading")
    assert not routes._valid_name("")


# ── redaction ────────────────────────────────────────────────────────────────


def test_redact_scrubs_credentials():
    secret = "AKIAIOSFODNN7EXAMPLE"
    out = routes._redact(f"key is {secret} ok")
    assert secret not in out


@pytest.mark.asyncio
async def test_spec_state_is_redacted_on_get(tmp_path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)

    # A spec on disk whose .spec-state.json carries a credential in a nested
    # value — the GET handler must recursively scrub it before serving.
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    secret = "AKIAIOSFODNN7EXAMPLE"
    (spec_dir / ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [{"id": "x", "title": f"use {secret} here", "answer": None}],
                "blocking": None,
                "context": {"template": "webex"},
            }
        )
    )
    routes._save_index(
        {
            "demo": {
                "working_dir": str(tmp_path / "wd"),
                "spec_dir": str(spec_dir),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._slot_key("demo"),
            }
        }
    )

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/specs/demo")
        assert resp.status == 200
        data = await resp.json()
        # Credential scrubbed from the nested decision title.
        assert secret not in json.dumps(data["state"])
        assert data["state"]["context"]["template"] == "webex"


@pytest.mark.asyncio
async def test_get_unknown_spec_404(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.get(f"{_BASE}/specs/ghost")
        assert resp.status == 404


# ── path sanitization (_safe_dir chokepoint) ─────────────────────────────────
#
# CodeQL flagged 8 py/path-injection sinks on the caller-supplied directory
# values. The name regex already blocks traversal and the read paths go through
# the trusted index, but ONE gap was real: only /browse applied the sensitive
# path test, so create/settings could point spec storage — and an agent's cwd —
# at a credential directory. These lock the closed gap.


def test_safe_dir_denies_sensitive_locations():
    """A credential directory must never survive the chokepoint."""

    ssh = os.path.expanduser("~/.ssh")
    # Only meaningful when the path exists on the host; the denial itself is
    # independent of existence, which the ancestor case below covers.
    assert routes._safe_dir(ssh) is None
    assert routes._safe_dir(ssh, must_exist=False) is None


def test_safe_dir_denies_new_dir_under_sensitive_ancestor():
    """A not-yet-created dir inside a credential dir is refused, not allowed
    through on a stat miss."""

    target = os.path.expanduser("~/.aws/spec-store")
    assert routes._safe_dir(target, must_exist=False) is None


def test_safe_dir_resolves_symlinks_before_checking(tmp_path):
    """A symlink in a benign directory must not smuggle its target through."""

    link = tmp_path / "innocent"
    os.symlink(os.path.expanduser("~/.ssh"), link)
    assert routes._safe_dir(str(link)) is None


def test_safe_dir_accepts_ordinary_directory(tmp_path):
    """Non-vacuous: the guard admits a normal directory."""
    ok = routes._safe_dir(str(tmp_path))
    assert ok is not None and ok.is_dir()


def test_contained_rejects_escape(tmp_path):
    root = tmp_path / "root"
    (root).mkdir()
    assert routes._contained(root / "child", root) is True
    assert routes._contained(tmp_path / "elsewhere", root) is False


@pytest.mark.asyncio
async def test_create_rejects_sensitive_working_dir(tmp_path, monkeypatch):
    """The real finding: create must refuse a credential dir as working_dir."""

    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "probe", "working_dir": os.path.expanduser("~/.ssh"), "spec_type": "feature"},
        )
        assert resp.status == 400
        assert "sensitive" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_settings_rejects_sensitive_base_path(tmp_path, monkeypatch):
    """Spec storage must not be repointable at a credential directory."""

    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": os.path.expanduser("~/.aws")}
        )
        assert resp.status == 400


# ── GPT round-1 HIGHs (#518) ─────────────────────────────────────────────────
#
# One test group per finding, so a regression names the finding it re-opens.


# (1) symlink-following on spec reads / STOP writes


def test_spec_file_refuses_symlink_out_of_spec_dir(tmp_path):
    """A planted symlink must not be read, even to a benign target."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    os.symlink(outside, spec_dir / "requirements.md")

    assert routes._spec_file(spec_dir, "requirements.md") is None
    assert routes._read_spec_text(spec_dir, "requirements.md") is None


def test_spec_file_refuses_symlink_to_sensitive_target(tmp_path):
    """The specific exploit: requirements.md -> a credential directory."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    os.symlink(os.path.expanduser("~/.aws/credentials"), spec_dir / "design.md")
    assert routes._read_spec_text(spec_dir, "design.md") is None


def test_spec_file_allows_ordinary_file(tmp_path):
    """Non-vacuous: a real file inside the spec dir still reads."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("# hello")
    assert routes._read_spec_text(spec_dir, "requirements.md") == "# hello"


def test_stop_sentinel_write_destroys_planted_symlink(tmp_path):
    """os.replace must swap the link itself, never write through it."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    os.symlink(victim, spec_dir / routes._STOP_FILE)

    assert routes._write_stop_sentinel(spec_dir) is True
    # The victim is untouched and STOP is now a real file.
    assert victim.read_text() == "original"
    assert not (spec_dir / routes._STOP_FILE).is_symlink()


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
    assert set(out["decisions"][0]) == {"id", "title", "options", "recommended", "answer"}


def test_normalize_spec_state_rejects_non_dict():
    assert routes._normalize_spec_state([1, 2, 3]) is None
    assert routes._normalize_spec_state(None) is None


def test_normalize_spec_state_caps_list_lengths():
    big = {"decisions": [{"id": str(i), "title": "t", "options": ["o"] * 100} for i in range(500)]}
    out = routes._normalize_spec_state(big)
    assert out is not None
    assert len(out["decisions"]) <= routes._MAX_DECISIONS
    assert len(out["decisions"][0]["options"]) <= routes._MAX_OPTIONS


# (4) create must not silently adopt/overwrite an existing spec


@pytest.mark.asyncio
async def test_create_refuses_existing_spec_files(tmp_path, monkeypatch):
    """An existing .kiro/specs/<name> with Kiro markdown returns 409."""
    project = tmp_path / "proj"
    existing = project / ".kiro" / "specs" / "already"
    existing.mkdir(parents=True)
    (existing / "requirements.md").write_text("# pre-existing")

    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "already", "working_dir": str(project), "spec_type": "feature"},
        )
        assert resp.status == 409
        assert "import_existing" in (await resp.json())["error"]
        # The pre-existing file is untouched.
        assert (existing / "requirements.md").read_text() == "# pre-existing"


# (5) containment regression — worktree mode


def test_contained_accepts_the_worktree_itself(tmp_path):
    """The regression: a sibling worktree is NOT under the original checkout, so
    containment must be measured against the worktree once one is created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "repo-wt-feature"
    worktree.mkdir()
    spec_dir = worktree / ".kiro" / "specs" / "feature"
    spec_dir.mkdir(parents=True)

    # Against the original checkout it (correctly) fails — this is what the bug
    # was measuring, which is why every worktree create 400'd.
    assert routes._contained(spec_dir, repo) is False
    # Against the worktree it passes, which is what the fixed code measures.
    assert routes._contained(spec_dir, worktree) is True


# ── GPT round-3 findings (#518) ───────────────────────────────────────────────


# (1) planning-phase auto-approval never expired


def test_browse_scan_is_offloadable_and_bounded(tmp_path):
    """The scan helper is a plain blocking function (so it can go to a thread)
    and caps its output rather than returning an unbounded listing."""
    for i in range(5):
        (tmp_path / f"dir{i}").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "afile.txt").write_text("x")

    out = routes._scan_subdirs(str(tmp_path))
    names = {d["name"] for d in out}
    assert names == {f"dir{i}" for i in range(5)}      # no hidden, no skip-list, no files
    assert routes._BROWSE_MAX_DIRS > 0


def test_browse_handler_does_not_scan_on_the_event_loop():
    """Source guard: the handler must delegate to the thread offload, not call
    os.scandir inline (which stalled chat streaming on large directories)."""

    src = inspect.getsource(routes._handle_browse)
    assert "asyncio.to_thread(_scan_subdirs" in src
    assert "os.scandir" not in src


def test_scan_skips_a_symlink_to_a_sensitive_target(tmp_path):
    """Symlinks resolve BEFORE the sensitivity test, so a link inside a benign
    directory cannot get a credential directory listed."""

    os.symlink(os.path.expanduser("~/.aws"), tmp_path / "innocent")
    (tmp_path / "real").mkdir()
    names = {d["name"] for d in routes._scan_subdirs(str(tmp_path))}
    assert "innocent" not in names
    assert "real" in names


# (3) top-level imports


def test_security_helper_is_imported_at_module_scope():
    """is_sensitive_path was imported function-locally in four helpers, against
    the top-level-imports rule. It is now module scope with a fail-closed
    fallback if the security module is unavailable."""

    src = inspect.getsource(routes)
    assert "    from kiro_crew.security import is_sensitive_path" not in src
    assert callable(routes.is_sensitive_path)


# ── GPT round-4 findings (#518) ───────────────────────────────────────────────


# (1) spec reads must be descriptor-pinned, not check-then-read


def test_spec_read_goes_through_the_descriptor_pinned_helper():
    """Source guard. The previous shape validated the path and then called
    ``p.read_text()`` by name, leaving a TOCTOU window: the agent writes into
    this very directory, so a spec file could be swapped for a symlink or
    hardlink to a credential file between the check and the open, during the
    UI's 2.5s poll. Reads must use the helper that opens with O_NOFOLLOW first
    and validates the DESCRIPTOR it actually read."""

    src = inspect.getsource(routes._read_spec_text)
    assert "safe_read_file_bytes_nolink" in src
    assert "within_root" in src
    # Compare the BODY, not the docstring — the docstring legitimately names the
    # old check-then-read shape when explaining why it was replaced.
    body = re.sub(r'""".*?"""', "", src, count=1, flags=re.S)
    assert "read_text(" not in body


def test_spec_read_returns_content_for_an_ordinary_file(tmp_path):
    """Non-vacuous: the safe path still reads a real file."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("# hello")
    assert routes._read_spec_text(spec_dir, "requirements.md") == "# hello"


def test_spec_read_refuses_a_symlinked_spec_file(tmp_path):
    """The exploit shape: a spec file replaced by a link to a file outside the
    spec dir must not be read."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("credential-ish")
    os.symlink(secret, spec_dir / "design.md")
    assert routes._read_spec_text(spec_dir, "design.md") is None


def test_spec_read_is_size_capped():
    assert routes._MAX_SPEC_BYTES > 0


# (2) the app must not grant auto-approval at all


def test_app_never_grants_worker_trust():
    """The load-bearing invariant of round 4.

    This app used to stamp ``slot._trust = True`` on create/message/execute
    because a permission prompt was invisible in the embedded chat. That premise
    is gone — the embed now renders working Approve/Trust/Reject controls — and a
    backend grant could not be bounded honestly: the wall-clock TTL was enforced
    on the UI's status poll, so closing the page stopped all enforcement while
    the grant survived. The decision belongs to the user, via core's own trust
    mechanism, where it is auditable as their choice.
    """

    src = inspect.getsource(routes)
    assert "slot._trust = True" not in src
    # And no revive-by-another-name.
    assert "_trust = True" not in src


def test_no_poll_dependent_ttl_machinery_remains():
    """The TTL is gone rather than left half-wired: a guard that only runs while
    a browser tab is open is not enforcement, and keeping it would imply a bound
    that does not hold."""

    src = inspect.getsource(routes)
    for gone in ("_enforce_trust_ttl", "_mark_trust_granted", "_TRUST_TTL_SECS"):
        assert gone not in src, f"{gone} still referenced"


@pytest.mark.asyncio
async def test_halt_execution_leaves_user_trust_alone(tmp_path):
    """Stop must sentinel the loop but NOT clear trust — if the user granted it
    from the approval card, that is their decision to reverse."""

    class _Slot:
        _trust = True

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return slot

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    await routes._halt_execution(_State(), "s", spec_dir, reason="user stop")
    assert (spec_dir / routes._STOP_FILE).is_file()   # loop is sentinelled
    assert slot._trust is True                        # user's choice preserved


# ── GPT round-5 findings (#518) ───────────────────────────────────────────────


# (1) polled handlers must not do filesystem work on the event loop


def test_detail_handler_offloads_all_filesystem_work():
    """Source guard. The detail endpoint is polled every 2.5s while a build runs
    and it stat-ed three files, read up to three 1 MiB documents and read
    .spec-state.json — all inline, freezing the gateway loop (chat streaming and
    heartbeats included) for the duration of every poll."""

    src = inspect.getsource(routes._handle_get)
    assert "asyncio.to_thread(_collect_spec_documents" in src
    # No inline reader calls left in the handler body.
    for inline in ("_read_spec_files(", "_derive_phase(", "_read_spec_text("):
        assert inline not in src, f"{inline} still called inline in the detail handler"


def test_list_handler_offloads_folder_discovery():
    """The list endpoint walks every known project root's .kiro/specs; that walk
    must not run on the loop either. Both the walk AND the index read/write now
    ride one hop, so the write cannot clobber a concurrent create."""

    src = inspect.getsource(routes._handle_list)
    assert "asyncio.to_thread(_load_index_with_discovery)" in src
    for inline in ("_load_index(", "_save_index(", "_discover_folder_specs("):
        assert inline not in src, f"{inline} still called inline in the list handler"


def test_collect_spec_documents_returns_the_detail_triple(tmp_path):
    """Non-vacuous: the bundled collector actually produces phase + docs + state."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("# reqs")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / ".spec-state.json").write_text(json.dumps({"blocking": "waiting on you"}))

    phase, files, state = routes._collect_spec_documents(spec_dir)
    assert phase == "design"                      # newest present phase file wins
    assert files["requirements.md"] == "# reqs"
    assert state is not None and state["blocking"] == "waiting on you"


# (2) deleting a spec must tear down its worker slot


@pytest.mark.asyncio
async def test_delete_cancels_and_removes_the_worker_slot(tmp_path):
    """The reported leak: delete removed the nudge loop and the index entry but
    left the in-flight turn ALIVE, so the agent kept editing the user's files
    after they deleted the spec — and re-creating the same name resurrected the
    old transcript, since get_or_create_slot keys off the slot name."""
    cancelled = {"v": False}

    class _Task:
        def cancel(self):
            cancelled["v"] = True

        def __await__(self):
            async def _done():
                return None
            return _done().__await__()

    class _Slot:
        _app = "spec-builder"
        running = True
        task = _Task()

    slot = _Slot()
    slots = {"spec-builder-doomed": slot}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    await routes._teardown_worker_slot(_State(), "doomed")
    assert cancelled["v"] is True, "in-flight turn was not cancelled"
    assert "spec-builder-doomed" not in slots, "slot left in the registry"


@pytest.mark.asyncio
async def test_teardown_refuses_a_slot_this_app_does_not_own():
    """Anti-collision: a slot whose _app is not ours must be left alone rather
    than deleted because its key happens to match."""
    class _Slot:
        _app = "some-other-app"
        running = False
        task = None

    slots = {"spec-builder-x": _Slot()}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    await routes._teardown_worker_slot(_State(), "x")
    assert "spec-builder-x" in slots


@pytest.mark.asyncio
async def test_teardown_is_a_noop_without_state():
    await routes._teardown_worker_slot(None, "whatever")


def test_delete_handler_tears_down_the_slot():
    """Source guard so a future edit can't drop the teardown call."""

    assert "_teardown_worker_slot(" in inspect.getsource(routes._handle_delete)


# ── stale-index writes (concurrent delete resurrects a spec) ─────────────────


@pytest.mark.asyncio
async def test_mutate_index_reads_a_fresh_index_not_the_callers_snapshot(tmp_path, monkeypatch):
    """The reported defect, at its root: a handler loads the index, awaits, then
    writes its stale snapshot back — restoring an entry a concurrent DELETE
    removed and dropping one a concurrent CREATE added, because the WHOLE file is
    overwritten. _mutate_index re-reads inside the same hop as the write."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"kept": {"spec_dir": "/a/kept", "status": "planning"}})

    # Stand in for the concurrent request that lands during the await: it edits
    # the file directly, exactly as another handler's own mutation would.
    routes._save_index({"kept": {"spec_dir": "/a/kept", "status": "planning"},
                        "added-meanwhile": {"spec_dir": "/a/added", "status": "planning"}})

    assert await routes._mutate_index(lambda idx: idx.pop("kept", None) is not None) is True
    after = routes._load_index()
    assert "kept" not in after, "mutation did not apply"
    assert "added-meanwhile" in after, "the concurrent entry was clobbered by a stale write"


def test_load_index_releases_a_reservation_left_by_a_dead_process(tmp_path, monkeypatch):
    """R79: a delete reservation that outlived its process must not reserve a name
    forever.

    ``_mark_deleting`` writes the marker before the teardown and clears it after,
    so a crash in that window persists it with no request left to release it. The
    entry then stays hidden from the list AND its name stays reserved against a
    re-create -- permanently. Loading must release a reservation this process does
    not own."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {
            "orphaned": {
                "spec_dir": "/a/orphaned",
                "status": "planning",
                routes._DELETING: {"owner": "1234:deadbeef", "at": 1.0},
            }
        }
    )

    loaded = routes._load_index()

    assert "orphaned" in loaded, "the entry must survive -- the delete never completed"
    assert routes._DELETING not in loaded["orphaned"], (
        "a reservation from a process that is gone still hides the spec and reserves its name"
    )


def test_load_index_releases_a_legacy_bare_timestamp_reservation(tmp_path, monkeypatch):
    """An index written by an older build stores the marker as a bare float, which
    carries no owner. It cannot have come from THIS process, so it is foreign and
    must be released -- otherwise upgrading strands every in-flight delete that was
    interrupted before the upgrade."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {"legacy": {"spec_dir": "/a/legacy", "status": "planning", routes._DELETING: 1699999999.0}}
    )

    loaded = routes._load_index()

    assert routes._DELETING not in loaded["legacy"], "a pre-upgrade reservation was not released"


@pytest.mark.asyncio
async def test_load_index_keeps_a_reservation_this_process_still_owns(tmp_path, monkeypatch):
    """The ordinary case, and the one a blanket "clear all reservations" would
    break: a delete in flight reads the index repeatedly (``_mutate_index`` loads
    on every hop), so clearing indiscriminately would cancel the reservation
    underneath the very request holding it and re-open the same-name window it
    exists to close."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"live": {"spec_dir": "/a/live", "slot_key": "", "status": "planning"}})

    assert (
        await routes._mark_deleting("live", expect_spec_dir="/a/live", expect_slot_key="") is True
    )

    loaded = routes._load_index()
    assert routes._DELETING in loaded["live"], (
        "this process's own in-flight reservation was released by its own read"
    )


@pytest.mark.asyncio
async def test_released_reservation_is_persisted_by_the_next_mutation(tmp_path, monkeypatch):
    """The release needs no write of its own: ``_load_index`` is the read half of
    ``_mutate_index``, so the next mutation writes the cleaned entry back. Pins
    that the cleanup reaches disk rather than being re-derived on every read."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {
            "orphaned": {
                "spec_dir": "/a/orphaned",
                "status": "planning",
                routes._DELETING: {"owner": "1234:deadbeef", "at": 1.0},
            }
        }
    )

    assert await routes._touch_spec("orphaned", status="planning") is not None

    on_disk = json.loads((tmp_path / "spec-builder" / "index.json").read_text())
    assert routes._DELETING not in on_disk["orphaned"], (
        "the stale reservation is still on disk after a mutation"
    )


@pytest.mark.asyncio
async def test_touch_spec_refuses_to_resurrect_a_deleted_spec(tmp_path, monkeypatch):
    """A stamp on a spec that is gone must FAIL, not recreate the entry — the
    caller has to abort rather than dispatch a worker for a deleted spec."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({})

    assert await routes._touch_spec("ghost", status="executing") is None
    assert routes._load_index() == {}, "a deleted spec was resurrected by a status stamp"


@pytest.mark.asyncio
async def test_touch_spec_returns_the_fresh_entry(tmp_path, monkeypatch):
    """Non-vacuous: on success it commits the fields AND hands back the entry as
    read from disk, so the caller stops reading its pre-await snapshot."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"live": {"spec_dir": "/w/spec", "working_dir": "/w", "status": "planning"}})

    fresh = await routes._touch_spec("live", status="executing")
    assert fresh is not None
    assert fresh["status"] == "executing" and fresh["working_dir"] == "/w"
    assert fresh["updated_at"] > 0
    assert routes._load_index()["live"]["status"] == "executing"


def test_no_handler_writes_the_index_from_a_stale_snapshot():
    """Source guard for the CLASS, not the two reported instances. Every handler
    awaits somewhere, so none of them may call _save_index directly: the only
    sanctioned writers are the re-reading mutator and the discovery loader."""

    src = inspect.getsource(routes)
    writers = [
        ln.strip()
        for ln in src.splitlines()
        if "_save_index(" in ln and not ln.strip().startswith(("#", "*", "def _save_index"))
    ]
    # _mutate_index and _load_index_with_discovery hold the only write sites.
    assert len(writers) == 2, f"unexpected _save_index call sites: {writers}"
    for handler in (
        routes._handle_create,
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
        routes._handle_list,
    ):
        assert "_save_index(" not in inspect.getsource(handler), (
            f"{handler.__name__} writes the index directly; it must go through _mutate_index"
        )


def test_handoff_commits_before_dispatch_and_unwinds_on_deletion():
    """Source guard for the ordering that closes the reported race: the index
    commit must precede _dispatch_turn, and the abort path must undo the loop and
    the slot it just created."""

    src = inspect.getsource(routes._handle_handoff)
    # Delimit the release helper by its own last statement: its body contains a
    # _touch_spec call (the state revert), so slicing to a commit cut it short.
    release = src.index("async def _release(")
    release_end = src.index('_audit("spec_handoff_aborted"', release)
    helper = src[release:release_end]
    assert "_remove_nudge_loop(" in helper, "the release leaves the armed nudge loop running"
    assert "_teardown_worker_slot(" in helper, "the release leaves the worker slot behind"
    assert 'status="planning"' in helper, "the release leaves the spec marked executing"

    claim = src.index("await _claim_execution(")
    arm = src.index("await authorize_and_add_nudge(")
    dispatch = src.index("_dispatch_turn(")
    # The claim is a single atomic compare-and-set that both refuses a duplicate
    # and records the state. It must precede the slot, the arm and the dispatch:
    # everything after it is a side effect that the claim is what authorizes.
    assert claim < src.index("_ensure_worker_slot("), "the slot is created before the claim"
    assert claim < arm, "the loop is armed before the execution state is recorded"
    assert claim < dispatch, "handoff dispatches before claiming the run"
    assert src[claim:dispatch].count("_release(") == 4, (
        "an abort arm does not release the loop, the recorded state and the slot"
    )


# ── blocking sentinel write on the event loop ────────────────────────────────


def test_prepare_handoff_arms_under_the_same_lock_as_the_identity_check():
    """R80: the identity check and the sentinel arm must be ONE critical section.

    Correct ordering is not enough. With the check in its own ``with`` block, a
    same-name delete plus re-import landing after the lock is released still
    leaves the check passing for a spec that is gone while the arm lands on the
    replacement -- clearing a STOP the user's Pause had just written.

    Structural because the race is a thread interleaving: reproducing it by
    timing would be flaky, while the property that forbids it -- the arm sits
    INSIDE the locked block -- is exactly stated in the source."""

    src = inspect.getsource(routes._prepare_handoff)
    tree = ast.parse(src)
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef), "expected _prepare_handoff to be a sync def"

    locked_blocks = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.With)
        and any(
            isinstance(i.context_expr, ast.Name) and i.context_expr.id == "_INDEX_LOCK"
            for i in n.items
        )
    ]
    assert len(locked_blocks) == 1, "expected exactly one _INDEX_LOCK critical section"

    def calls_arm(node):
        return any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "_arm_stop_sentinel"
            for c in ast.walk(node)
        )

    assert calls_arm(locked_blocks[0]), (
        "_arm_stop_sentinel is outside the _INDEX_LOCK block, so a same-name "
        "re-import between the check and the arm can redirect it at a replacement"
    )
    # And nowhere else: an arm outside the section would reopen the window even
    # with one inside it.
    arms_outside = [
        n.name if hasattr(n, "name") else "<stmt>"
        for n in fn.body
        if n not in locked_blocks and calls_arm(n)
    ]
    assert not arms_outside, "a second _arm_stop_sentinel call sits outside the lock"


def test_stop_write_is_refused_for_a_replaced_spec(tmp_path, monkeypatch):
    """The write half of R80: creating a STOP is as destructive as removing one.

    A stale Stop must not halt the run belonging to a spec that took the same
    name after the original was deleted."""

    spec_dir = tmp_path / "wd"
    spec_dir.mkdir()
    # The index says this name now belongs to a DIFFERENT creation.
    monkeypatch.setattr(
        routes,
        "_load_index",
        lambda: {"s": {"spec_dir": str(spec_dir), "slot_key": "new-key"}},
    )

    assert routes._write_stop_sentinel_for_spec(spec_dir, "s", "stale-key") is False
    assert not (spec_dir / routes._STOP_FILE).exists(), (
        "a stale Stop wrote a STOP into the replacement's directory"
    )
    # Ordinary case: the caller's captured key still matches, so it writes.
    assert routes._write_stop_sentinel_for_spec(spec_dir, "s", "new-key") is True
    assert (spec_dir / routes._STOP_FILE).exists()


def test_halt_execution_writes_the_sentinel_off_the_loop():
    """The reported stall: _write_stop_sentinel is realpath + is_sensitive_path +
    open + write + close + replace. Inline in an async handler, a spec dir on
    unresponsive network storage froze the whole gateway on a Stop click.

    Also pins that the write goes through the IDENTITY-PINNED wrapper: the plain
    primitive would land the STOP wherever the path now points, and the caller's
    own check cannot cover the thread hop in between."""

    src = inspect.getsource(routes._halt_execution)
    # Whitespace-insensitive: the call spans lines, and a literal anchor that a
    # reformat can break is a guard that silently stops guarding.
    assert re.search(r"asyncio\.to_thread\(\s*_write_stop_sentinel_for_spec", src), (
        "the STOP write no longer rides the identity-pinned wrapper off-loop"
    )
    assert "\n    _write_stop_sentinel(" not in src, "sentinel still written on the event loop"


def test_handoff_does_no_filesystem_work_on_the_loop():
    """Sibling of the same class: the handoff endpoint stat-ed tasks.md, unlinked
    a stale sentinel and resolved a realpath inline. All three ride one hop."""

    src = inspect.getsource(routes._handle_handoff)
    # Whitespace-insensitive: the call spans lines, and a literal anchor that
    # a reformat can break is a guard that silently stops guarding.
    assert re.search(r"asyncio\.to_thread\(\s*_prepare_handoff", src), (
        "handoff no longer hands _prepare_handoff to a worker thread"
    )
    for inline in ("_clear_stop_sentinel(", "os.path.realpath(", '/ "tasks.md"'):
        assert inline not in src, f"{inline} still runs on the event loop in handoff"


def test_prepare_handoff_reports_tasks_and_clears_a_stale_sentinel(tmp_path):
    """Non-vacuous: the bundled helper really gates on tasks.md, removes a stale
    STOP file, and returns the resolved sentinel path."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    stale = spec_dir / routes._STOP_FILE
    stale.write_text("old")

    has_tasks, sentinel = routes._prepare_handoff(spec_dir)
    assert has_tasks is False
    assert not stale.exists(), "stale STOP sentinel survived; the new run stops immediately"
    assert sentinel.endswith(routes._STOP_FILE)

    (spec_dir / "tasks.md").write_text("- [ ] task")
    assert routes._prepare_handoff(spec_dir)[0] is True


# ── GPT round-7 findings (#518) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_index_transactions_do_not_lose_a_write(tmp_path, monkeypatch):
    """The reported defect: moving the read-modify-write onto a worker thread
    fixed the await window but not thread interleaving — two concurrent creates
    could read the SAME index and the second write would silently drop the first.
    _INDEX_LOCK serializes the whole transaction inside the hop."""

    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({})

    real_load = routes._load_index

    def _slow_load():
        # Widen the read→write window so an unserialized implementation loses a
        # write deterministically rather than by luck.
        time.sleep(0.05)
        return real_load()

    monkeypatch.setattr(routes, "_load_index", _slow_load)

    def _insert(key):
        def _apply(index):
            index[key] = {"spec_dir": f"/a/{key}", "status": "planning"}
            return True
        return _apply

    await asyncio.gather(*(routes._mutate_index(_insert(f"spec-{i}")) for i in range(6)))
    after = real_load()
    assert sorted(after) == [f"spec-{i}" for i in range(6)], f"a write was lost: {sorted(after)}"


def test_index_transactions_are_serialized_by_a_shared_lock():
    """Source guard: both index writers must hold the same lock. An asyncio.Lock
    would NOT do — the transactions run on worker threads, which an asyncio lock
    does not exclude from each other."""

    assert isinstance(routes._INDEX_LOCK, type(threading.Lock()))
    for fn in (routes._mutate_index, routes._load_index_with_discovery):
        assert "_INDEX_LOCK" in inspect.getsource(fn), f"{fn.__name__} does not take the lock"


def test_list_handler_derives_phases_off_the_loop(tmp_path, monkeypatch):
    """The reported stall: the response loop called _derive_phase per spec, and
    each call stats up to three files — so a large index froze the gateway on
    every 15s poll. Phases now come back from the same thread hop."""

    # _save_index below writes to module-level STATE_DIR: without this redirect
    # the test scribbles a pytest tmp path into the USER's real index.
    _redirect_state(monkeypatch, tmp_path)
    src = inspect.getsource(routes._handle_list)
    assert "_derive_phase(" not in src, "_derive_phase still runs on the event loop"
    assert "phases.get(name" in src

    # Non-vacuous: the bundled loader really reports each spec's phase.
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s1"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    (spec_dir / "design.md").write_text("# d")
    routes._save_index({"s1": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})
    _index, phases = routes._load_index_with_discovery()
    assert phases["s1"] == "design"


def test_gateway_helpers_are_imported_at_module_scope():
    """top-level-imports: a function-local import hides the dependency and makes
    a test's mock patch target the wrong namespace. Only the dashboard submodules
    stay deferred, because dashboard.server imports THIS module (documented
    circular-import exception)."""

    tree = ast.parse(inspect.getsource(routes))
    local_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith("kiro_crew"):
                local_imports.append(inner.module or "")

    # Every remaining function-local import must be a documented dashboard cycle.
    assert local_imports, "guard is vacuous: expected the dashboard deferrals"
    for mod in local_imports:
        assert mod.startswith("kiro_crew.dashboard."), f"undocumented local import: {mod}"

    for name in (
        "safe_read_file_bytes_nolink",
        "sandboxed_spawn_argv",
        "create_subprocess_limited",
        "authorize_and_add_nudge",
        "CHAT_TURN_TIMEOUT",
    ):
        assert hasattr(routes, name), f"{name} is not bound at module scope"


# ── GPT round-8 findings (#518) ──────────────────────────────────────────────


def test_state_files_are_written_atomically(tmp_path, monkeypatch):
    """The reported defect: a truncating write_text interrupted mid-flight (SIGTERM
    on a gateway restart, a full disk) leaves invalid JSON, and both loaders treat
    a JSONDecodeError as 'empty' — so settings silently reset and EVERY indexed
    spec disappears. atomic_write writes a temp file and renames."""

    for fn in (routes._save_index, routes._save_settings):
        src = inspect.getsource(fn)
        assert "atomic_write(" in src, f"{fn.__name__} does not write atomically"
        assert "write_text(" not in src, f"{fn.__name__} still truncates in place"

    # Non-vacuous: a failing write must leave the previous file intact rather
    # than a truncated one.
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"keep": {"spec_dir": "/a/keep", "status": "planning"}})

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(routes, "atomic_write", _boom)
    with pytest.raises(OSError):
        routes._save_index({"keep": {"spec_dir": "/a/keep", "status": "planning"}, "new": {}})
    assert routes._load_index() == {"keep": {"spec_dir": "/a/keep", "status": "planning"}}


def _stub_spawn(monkeypatch):
    """Replace the sandboxed argv with a trivial no-op binary. The host running
    the suite may have no sandbox backend available, and this test is about the
    AUDIT contract, not about sandboxing or git itself."""
    monkeypatch.setattr(routes, "sandboxed_spawn_argv", lambda argv: ([sys.executable, "-c", ""], None, ""))


@pytest.mark.asyncio
async def test_git_invocations_are_audited_to_sel(tmp_path, monkeypatch):
    """The reported gap: this app spawns git against the user's repository, and a
    worktree create/remove left no tool-invocation trail — only app-level
    spec_worktree_* entries, which record neither what git ran nor whether it
    failed. Every invocation and outcome now emits log_tool_invocation."""
    events: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

        def log_api_access(self, **kw):
            pass

    monkeypatch.setattr(routes, "sel", lambda: _Sel())
    _stub_spawn(monkeypatch)
    rc, _out, _err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")

    outcomes = [e["outcome"] for e in events]
    assert "invoked" in outcomes, "no invocation event recorded"
    assert outcomes[-1] in ("success", "failure"), f"no outcome event: {outcomes}"
    assert all(e["tool_name"] == "git" for e in events)
    assert events[0]["metadata"]["subcommand"] == "rev-parse"
    # Coarse by design: the full argv is never logged (a branch name derives from
    # user input), only the subcommand + working directory.
    assert "--show-toplevel" not in json.dumps(events)
    assert rc == 0


@pytest.mark.asyncio
async def test_git_outcome_audit_failure_does_not_break_the_command(tmp_path, monkeypatch):
    """An OUTCOME audit failure must not break a command that already ran.

    Narrowed from "auditing must never break the flow it audits": the INVOCATION
    event is now a precondition for the spawn (a swallowed failure meant git ran on
    the user's repository with no tool-invocation record at all). Losing the
    outcome event afterwards is different — the process has already run, and
    turning that into an error would report a successful command as failed.
    """
    calls: list[str] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            calls.append(kw.get("outcome", ""))
            if kw.get("outcome") != "invoked":
                raise RuntimeError("sel down")

        def log_api_access(self, **kw):
            pass

    monkeypatch.setattr(routes, "sel", lambda: _Sel())
    _stub_spawn(monkeypatch)
    rc, _out, _err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")
    assert rc == 0
    assert calls[0] == "invoked" and len(calls) > 1, calls


@pytest.mark.asyncio
async def test_detail_payload_reports_live_running_state(tmp_path, monkeypatch):
    """The reported defect: the detail payload carried `status` but not `running`,
    so the selected spec's working indicator, document skeleton and fast 2.5s poll
    were all permanently off — the one place they matter."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "live"
    spec_dir.mkdir(parents=True)
    routes._save_index({"live": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    class _Slot:
        key = routes._slot_key("live")
        running = True
        messages: list = []
        # Our own slot: an UNSCOPED slot under this key is somebody else's
        # conversation and is now refused rather than adopted.
        _app = routes.APP_NAME
        project = ""
        _titled = False

    _slot = _Slot()

    class _State:
        def get_slot(self, key):
            return _slot if key == routes._slot_key("live") else None

        def get_or_create_slot(self, name, app=""):
            return _slot

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.get(f"{_BASE}/specs/live")
        body = await resp.json()
    finally:
        await client.close()

    assert body["running"] is True, "detail payload omits the live running flag"


# ── GPT round-9 findings (#518) ──────────────────────────────────────────────


def test_symlinked_spec_dir_cannot_touch_another_specs_sentinel(tmp_path):
    """The reported attack: an agent replaces its own indexed spec directory with
    a symlink to a PAUSED spec's directory. The sentinel helpers operated on
    realpath(spec_dir), so the handoff endpoint would delete the OTHER spec's
    STOP file and resume a run the user had stopped."""

    paused = tmp_path / "paused-spec"
    paused.mkdir()
    stop = paused / routes._STOP_FILE
    stop.write_text("stopped by the user")

    attacker = tmp_path / "attacker-spec"
    os.symlink(paused, attacker)
    (paused / "tasks.md").write_text("- [ ] task")

    # Every sentinel operation must refuse the symlinked directory.
    assert routes._verified_spec_dir(attacker) is None
    assert routes._arm_stop_sentinel(attacker) == ""
    assert routes._write_stop_sentinel(attacker) is False
    routes._clear_stop_sentinel(attacker)
    assert stop.read_text() == "stopped by the user", "another spec's STOP file was deleted"

    # ...and the handoff must not report itself ready off a symlinked dir, even
    # though tasks.md resolves through the link.
    ready, sentinel = routes._prepare_handoff(attacker)
    assert ready is False and sentinel == ""


def test_verified_spec_dir_accepts_an_ordinary_directory(tmp_path):
    """Non-vacuous: the guard must not reject a normal spec directory, or the
    whole Stop/handoff path silently stops working."""
    spec_dir = Path(os.path.realpath(tmp_path)) / "spec"
    spec_dir.mkdir()
    assert routes._verified_spec_dir(spec_dir) == spec_dir
    assert routes._prepare_handoff(spec_dir) == (False, str(spec_dir / routes._STOP_FILE))
    (spec_dir / "tasks.md").write_text("- [ ] t")
    assert routes._prepare_handoff(spec_dir)[0] is True
    assert routes._write_stop_sentinel(spec_dir) is True


def test_create_arbitrates_the_index_before_touching_the_shared_slot():
    """The reported race: get_or_create_slot keys off the NAME, so two concurrent
    same-name creates share one slot. Configuring it before the index decided the
    winner let the LOSER stamp its own working_dir onto the shared slot, so the
    accepted spec's agent ran in the rejected directory."""

    src = inspect.getsource(routes._handle_create)
    arbitration = src.index("_mutate_index(_insert)")
    for acquisition in ("get_or_create_slot(", "_ensure_worker_slot("):
        assert acquisition not in src[:arbitration], (
            "create acquires the shared slot before the index arbitration"
        )
    for mutation in ("slot.project =", "slot._app =", "slot.title ="):
        assert mutation not in src[:arbitration], f"{mutation} happens before arbitration"
    assert "_ensure_worker_slot(" in src[arbitration:]


# ── GPT round-10 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handoff_refuses_when_authorization_is_unavailable(tmp_path, monkeypatch):
    """The reported fail-open: the arm was wrapped in a broad except that logged
    'running single turn' and fell through to _dispatch_turn — starting an
    autonomous run with NO authorization, no message limit, no sentinel refusal
    and no SEL audit, exactly when the enforcing machinery was unavailable."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index({"s": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _State:
        def get_or_create_slot(self, name, app=""):
            class _S:
                key = name
                _app = app
            return _S()

        def get_slot(self, key):
            return None

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(f"{_BASE}/specs/s/handoff")
    finally:
        await client.close()

    assert resp.status == 503, "handoff started an unauthorized run"
    assert dispatched == [], "an autonomous turn was dispatched without authorization"
    assert routes._load_index()["s"].get("status") != "executing"


@pytest.mark.asyncio
async def test_handoff_refuses_when_authorization_raises(tmp_path, monkeypatch):
    """Same contract for an authorization helper that raises rather than
    returning an error string."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index({"s": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _boom(**_kw):
        raise RuntimeError("authz backend down")

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _boom)

    class _State:
        def get_or_create_slot(self, name, app=""):
            class _S:
                key = name
                _app = app
            return _S()

        def get_slot(self, key):
            return None

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(f"{_BASE}/specs/s/handoff")
    finally:
        await client.close()

    assert resp.status == 503
    assert dispatched == []


@pytest.mark.asyncio
async def test_pause_stops_the_in_flight_turn(tmp_path):
    """The reported defect: Pause wrote the STOP sentinel and removed the nudge
    loop — both of which only prevent FUTURE nudges — then reported 'planning'
    while the in-flight _run_chat kept editing the user's files."""
    stopped: list[str] = []
    cancelled = {"v": False}

    class _Task:
        def cancel(self):
            cancelled["v"] = True

        def done(self):
            return False

        def __await__(self):
            async def _done():
                return None
            return _done().__await__()

    class _Slot:
        key = "spec-builder-live"
        _app = "spec-builder"
        running = True
        task = _Task()

    class _Sessions:
        async def stop_turn(self, key, force=False):
            stopped.append(key)

    class _State:
        sessions = _Sessions()

        def get_slot(self, key):
            return _Slot() if key == "spec-builder-live" else None

    assert await routes._halt_active_turn(_State(), "live") is True
    assert stopped, "cooperative stop_turn was never called"
    assert cancelled["v"] is True, "the in-flight turn was not cancelled"


@pytest.mark.asyncio
async def test_pause_leaves_a_foreign_slot_alone():
    """Anti-collision: a slot this app does not own must not be stopped because
    its key happens to match."""
    class _Slot:
        key = "spec-builder-x"
        _app = "some-other-app"
        running = True
        task = None

    class _State:
        def get_slot(self, key):
            return _Slot()

    assert await routes._halt_active_turn(_State(), "x") is False


def test_stop_handler_halts_the_running_turn():
    """Source guard so the halt cannot regress to sentinel-only."""

    assert "_halt_active_turn(" in inspect.getsource(routes._halt_execution)


# ── GPT round-11 findings (#518) ─────────────────────────────────────────────


def test_no_handler_validates_paths_on_the_event_loop():
    """The reported stall: _safe_dir expands, realpaths and stats a
    caller-supplied path (plus its nearest existing ancestor), so an unresponsive
    mount froze the gateway before the browse scan ever reached its own thread.
    Every handler-side path validation now rides a thread hop."""

    for handler in (routes._handle_browse, routes._handle_put_settings, routes._handle_create):
        src = inspect.getsource(handler)
        for line in src.splitlines():
            stripped = line.strip()
            if "_safe_dir" not in stripped or stripped.startswith("#"):
                continue
            assert "to_thread" in stripped, f"{handler.__name__}: {stripped}"
        # create's remaining filesystem work rides one bundled hop.
        if handler is routes._handle_create:
            assert "asyncio.to_thread(\n        _prepare_spec_dir" in src or (
                "to_thread(" in src and "_prepare_spec_dir" in src
            )
            for inline in ("_resolve_spec_dir(", "_contained(", "spec_dir.mkdir("):
                assert inline not in src, f"{inline} still runs on the event loop in create"


def test_prepare_spec_dir_reports_each_refusal(tmp_path, monkeypatch):
    """Non-vacuous: the bundled validator must still produce the three distinct
    refusals the handler maps onto 400 / 409 / 400."""
    _redirect_state(monkeypatch, tmp_path)
    wd = Path(os.path.realpath(tmp_path)) / "wd"
    wd.mkdir()

    spec_dir, refusal = routes._prepare_spec_dir(str(wd), wd, "fresh", False)
    assert refusal == "" and spec_dir.is_dir()

    (spec_dir / "requirements.md").write_text("# r")
    _again, refusal = routes._prepare_spec_dir(str(wd), wd, "fresh", False)
    assert refusal.startswith("existing:") and "requirements.md" in refusal
    # import_existing opts in.
    assert routes._prepare_spec_dir(str(wd), wd, "fresh", True)[1] == ""

    # Containment: a settings base_path elsewhere makes the derived dir escape
    # the declared root.
    other = Path(os.path.realpath(tmp_path)) / "other"
    other.mkdir()
    routes._save_settings({"base_path": str(other)})
    _p, refusal = routes._prepare_spec_dir(str(wd), wd, "escaper", False)
    assert refusal == "" or refusal == "escape"


@pytest.mark.asyncio
async def test_discovered_spec_gets_a_scoped_slot(tmp_path, monkeypatch):
    """The reported defect: a spec created OUTSIDE the app (Kiro CLI/IDE, then
    discovered) had no slot, so the embedded chat's /api/chat created the first
    one — unscoped. No _app (it surfaced in the main sidebar) and no project, so
    approved tools ran from the gateway's working directory instead of the user's
    project. Reading the spec now materializes a SCOPED slot."""
    # The indexed working_dir now goes through _safe_dir; these fixtures use
    # synthetic paths, so accept them (the validation itself is covered
    # separately by test_indexed_working_dir_is_revalidated).
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))

    created: list[str] = []

    class _Slot:
        key = "spec-builder-found"
        running = False
        messages: list = []
        _app = ""
        project = ""
        _titled = False

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return None  # nothing exists yet — the discovered-spec case

        def get_or_create_slot(self, name, app=""):
            created.append(name)
            return slot

    meta = {"working_dir": "/projects/thing", "spec_dir": "/projects/thing/.kiro/specs/found"}
    out = await routes._ensure_worker_slot(_State(), "found", meta)

    assert out is slot
    assert created == ["spec-builder-found"], "the missing slot was not created"
    assert slot._app == routes.APP_NAME, "slot left unscoped — it would show in the main sidebar"
    assert slot.project == "/projects/thing", "slot has no project — tools would run in the wrong dir"
    assert slot._titled is True


def test_every_slot_acquisition_goes_through_the_scoping_chokepoint():
    """Source guard: no handler may call get_or_create_slot directly, or a future
    endpoint reintroduces an unscoped slot."""

    src = inspect.getsource(routes)
    sites = [
        ln.strip()
        for ln in src.splitlines()
        if "get_or_create_slot(" in ln and not ln.strip().startswith("#")
    ]
    assert len(sites) == 1, f"get_or_create_slot called outside the chokepoint: {sites}"
    for handler in (
        routes._handle_get,
        routes._handle_messages,
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_create,
    ):
        assert "_ensure_worker_slot(" in inspect.getsource(handler), (
            f"{handler.__name__} does not scope the slot it uses"
        )


# ── GPT round-12 findings (#518) ─────────────────────────────────────────────


def test_no_handler_reads_the_index_on_the_event_loop():
    """The reported stall, and the last member of this class: _load_index is a
    file read plus a JSON parse, and the detail endpoint is polled every 2.5s
    during a build — so on a stalled data home the gateway froze repeatedly.
    Handlers read through _aload_index; the sync primitive stays for the
    transaction helpers, which already run on a worker thread."""

    module_src = inspect.getsource(routes)
    tree = ast.parse(module_src)
    offenders: list[str] = []
    # Only the off-loop helpers may call the sync reader. Iterate TOP-LEVEL
    # definitions and skip an allowed one's whole subtree, so the nested
    # thread-body closures inside them are not counted as offenders.
    # _prepare_handoff joins the off-loop helpers: it re-checks the spec's
    # identity under the index lock before clearing the STOP sentinel, and it
    # only ever runs via asyncio.to_thread. _write_stop_sentinel_for_spec is its
    # counterpart for the opposite act (creating a STOP rather than removing one)
    # and earns the allowance the same way. The BLOCKING assertion below ties
    # both allowances to that contract, so the set cannot be widened to admit a
    # function that actually runs on the loop.
    allowed = {
        "_aload_index",
        "_mutate_index",
        "_load_index_with_discovery",
        "_load_index",
        "_prepare_handoff",
        "_write_stop_sentinel_for_spec",
    }
    for off_loop in (
        "_load_index_with_discovery",
        "_prepare_handoff",
        "_write_stop_sentinel_for_spec",
    ):
        doc = inspect.getdoc(getattr(routes, off_loop)) or ""
        assert "BLOCKING" in doc, (
            f"{off_loop} is allowed to read the index synchronously only because it "
            "runs on a worker thread; its docstring no longer says so"
        )
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_load_index"
            ):
                offenders.append(node.name)
    assert not offenders, f"synchronous _load_index called in: {sorted(set(offenders))}"

    for handler in (
        routes._handle_get,
        routes._handle_messages,
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
        routes._handle_create,
    ):
        assert "await _aload_index()" in inspect.getsource(handler), (
            f"{handler.__name__} does not read the index off the loop"
        )


@pytest.mark.asyncio
async def test_aload_index_returns_the_persisted_index(tmp_path, monkeypatch):
    """Non-vacuous: the offloaded reader must return what was written."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"a": {"spec_dir": "/a/a", "status": "planning"}})
    assert await routes._aload_index() == {"a": {"spec_dir": "/a/a", "status": "planning"}}
    assert await routes._aload_index() == routes._load_index()


# ── GPT round-13 findings (#518) ─────────────────────────────────────────────


def test_recents_and_settings_io_stay_off_the_event_loop():
    """The reported stall: the browse handler read (and JSON-parsed) the
    dashboard's recent-projects file plus an is_dir() per candidate inline, and
    the settings handlers read/wrote their file inline. On stalled home storage
    the picker's very first request froze the whole gateway."""

    browse = inspect.getsource(routes._handle_browse)
    assert "asyncio.to_thread(_read_recent_projects)" in browse
    assert "read_text()" not in browse, "recents still read on the event loop"

    for handler, fn in (
        (routes._handle_get_settings, "_load_settings"),
        (routes._handle_put_settings, "_save_settings"),
    ):
        src = inspect.getsource(handler)
        assert f"asyncio.to_thread({fn}" in src, f"{handler.__name__}: {fn} runs on the loop"


def test_read_recent_projects_filters_to_existing_dirs(tmp_path, monkeypatch):
    """Non-vacuous: the extracted helper keeps the filtering behaviour."""
    home = tmp_path / "cfg"
    home.mkdir()
    real = tmp_path / "real-project"
    real.mkdir()
    (home / "recent_projects.json").write_text(
        json.dumps([str(real), str(tmp_path / "gone"), 42])
    )
    monkeypatch.setattr(routes, "config_dir", lambda: home)

    assert routes._read_recent_projects() == [str(real)]

    (home / "recent_projects.json").write_text("not json")
    assert routes._read_recent_projects() == []


@pytest.mark.asyncio
async def test_missing_git_degrades_instead_of_500(tmp_path, monkeypatch):
    """The reported crash: browsing a folder calls _repo_info -> _git, so on a
    host without git the FileNotFoundError propagated and the project picker's
    first request returned HTTP 500. The app is usable without git (the worktree
    option simply isn't offered), so it must degrade."""
    monkeypatch.setattr(
        routes, "sandboxed_spawn_argv", lambda argv: (["/nonexistent/git"], None, "")
    )

    rc, out, err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")
    assert rc == routes._GIT_UNAVAILABLE and out == ""
    assert "git" in err

    # _repo_info must then report "not a git repo" rather than raising.
    assert await routes._repo_info(str(tmp_path)) == {"is_git": False}


@pytest.mark.asyncio
async def test_git_reports_unavailable_when_the_sandbox_refuses(tmp_path, monkeypatch):
    """Same contract when the sandbox cannot build an argv at all — this host has
    no sandbox backend, which is exactly how the suite hits that path."""
    def _boom(_argv):
        raise RuntimeError("sandbox backend unavailable")

    monkeypatch.setattr(routes, "sandboxed_spawn_argv", _boom)
    rc, _out, err = await routes._git(str(tmp_path), "status")
    assert rc == routes._GIT_UNAVAILABLE
    assert "unavailable" in err


# ── GPT round-14 findings (#518) ─────────────────────────────────────────────


def test_repo_info_validates_through_the_chokepoint_off_loop():
    """The reported stall: a hand-rolled is_absolute()/is_dir() pair stat-ed a
    caller-supplied path on the event loop (an unavailable network path froze the
    gateway) AND skipped the sensitive-path denial _safe_dir applies."""

    src = inspect.getsource(routes._handle_repo_info)
    assert "asyncio.to_thread(_safe_dir" in src
    code = [
        ln for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    for ln in code:
        assert "is_dir()" not in ln, f"repo-info still stats on the event loop: {ln.strip()}"


@pytest.mark.asyncio
async def test_repo_info_refuses_a_sensitive_path(tmp_path, monkeypatch):
    """Non-vacuous: routing through _safe_dir means a sensitive directory is now
    refused, where the old pair would have probed it with git."""
    client = _make_client(monkeypatch, tmp_path)
    probed: list[str] = []

    async def _repo(path):
        probed.append(path)
        return {"is_git": True, "root": path}

    monkeypatch.setattr(routes, "_repo_info", _repo)
    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: "secrets" in p)
    sensitive = Path(os.path.realpath(tmp_path)) / "secrets"
    sensitive.mkdir()
    ordinary = Path(os.path.realpath(tmp_path)) / "project"
    ordinary.mkdir()

    await client.start_server()
    try:
        denied = await (await client.get(f"{_BASE}/repo-info?path={sensitive}")).json()
        allowed = await (await client.get(f"{_BASE}/repo-info?path={ordinary}")).json()
    finally:
        await client.close()

    assert denied == {"is_git": False} and str(sensitive) not in probed
    assert allowed.get("is_git") is True


def test_handoff_refuses_a_symlinked_tasks_file(tmp_path):
    """The reported defect: the gate used (spec_dir / 'tasks.md').is_file(), which
    FOLLOWS a symlink — so a planted tasks.md pointing outside the spec directory
    satisfied it and the autonomous run then edited the link target."""

    spec_dir = Path(os.path.realpath(tmp_path)) / "spec"
    spec_dir.mkdir()
    outside = Path(os.path.realpath(tmp_path)) / "elsewhere.md"
    outside.write_text("- [ ] edit me instead")
    os.symlink(outside, spec_dir / "tasks.md")

    ready, sentinel = routes._prepare_handoff(spec_dir)
    assert ready is False, "handoff accepted a symlinked tasks.md"
    assert sentinel  # the spec dir itself is fine; only the file is rejected

    # A real regular file still passes.
    (spec_dir / "tasks.md").unlink()
    (spec_dir / "tasks.md").write_text("- [ ] task")
    assert routes._prepare_handoff(spec_dir)[0] is True


# ── GPT round-15 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persisted_transcript_is_read_off_the_event_loop():
    """The reported stall: the messages endpoint fell back to the PERSISTED
    transcript (a whole JSONL file) and read it inline. That fallback is the case
    that matters — a rehydrated session with no in-memory messages, i.e. right
    after a gateway restart, which is exactly when the user reopens the spec."""

    src = inspect.getsource(routes._serialize_messages)
    assert src.lstrip().startswith("async def"), "_serialize_messages is still sync"
    assert "asyncio.to_thread(" in src and "read_messages" in src
    for ln in src.splitlines():
        stripped = ln.strip()
        if stripped.startswith("#") or "logger" in stripped:
            continue
        if "conversation_log.read_messages" not in stripped:
            continue
        assert stripped.startswith("state.conversation_log.read_messages,"), (
            f"read_messages still called on the loop: {stripped}"
        )
    # And the caller must await it, not schedule a coroutine into the payload.
    assert "await _serialize_messages(" in inspect.getsource(routes._handle_messages)


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
                {"role": "user", "content": "key is AKIAIOSFODNN7EXAMPLE ok", "ts": "2"},
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
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(out), "credential not redacted"


# ── GPT round-16 findings (#518) ─────────────────────────────────────────────


def test_redaction_fails_closed_without_the_security_module(monkeypatch):
    """The reported fail-open: with _HAS_SECURITY false, _redact returned the text
    UNCHANGED — and everything flowing through it is agent- or user-authored
    (spec documents, transcripts, agent-written state), so a credential in an
    agent-written file would have gone straight to the browser. The same
    fail-closed reasoning as the is_sensitive_path fallback."""
    secret = "AKIAIOSFODNN7EXAMPLE"

    # With security available, the credential is scrubbed but the prose survives.
    scrubbed = routes._redact(f"key is {secret} ok")
    assert secret not in scrubbed and "key is" in scrubbed

    monkeypatch.setattr(routes, "_HAS_SECURITY", False)
    withheld = routes._redact(f"key is {secret} ok")
    assert secret not in withheld, "raw credential served when redaction is unavailable"
    assert withheld == routes._UNSCRUBBABLE

    # Empty input still returns empty rather than the placeholder.
    assert routes._redact("") == ""


# ── GPT round-17 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_slot_is_not_silently_re_owned(monkeypatch):
    """The reported defect: _ensure_worker_slot stamped _app unconditionally, so a
    slot another app already held under the same key was re-owned — its _app
    overwritten and its project repointed at our spec's directory, taking the
    session (and transcript) away from the app that created it."""
    # The indexed working_dir now goes through _safe_dir; these fixtures use
    # synthetic paths, so accept them (the validation itself is covered
    # separately by test_indexed_working_dir_is_revalidated).
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))

    class _Foreign:
        key = "spec-builder-demo"
        _app = "some-other-app"
        project = "/other/project"
        _titled = True

    foreign = _Foreign()

    class _State:
        def get_slot(self, key):
            return foreign if key == "spec-builder-demo" else None

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over a foreign slot")

    out = await routes._ensure_worker_slot(_State(), "demo", {"working_dir": "/ours"})
    assert out is None, "a foreign slot was handed back as ours"
    assert foreign._app == "some-other-app", "foreign ownership was overwritten"
    assert foreign.project == "/other/project", "foreign slot was repointed"


@pytest.mark.asyncio
async def test_only_our_own_slot_is_adopted_and_a_missing_one_is_created(monkeypatch):
    """Round 22 tightened this: an UNSCOPED slot under our key is somebody else's
    conversation (a main-chat session that happens to be named
    `spec-builder-<x>`), and adopting it rewrote its ownership, repointed its
    project and pulled its transcript into this app. Only a slot already owned by
    this app is adopted; a MISSING slot is still created and scoped, which is what
    keeps the discovered-spec fix working."""
    # The indexed working_dir now goes through _safe_dir; these fixtures use
    # synthetic paths, so accept them (the validation itself is covered
    # separately by test_indexed_working_dir_is_revalidated).
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))

    # (a) already ours -> adopted and (re)scoped.
    class _Ours:
        key = "spec-builder-mine"
        _app = routes.APP_NAME
        project = ""
        _titled = False

    ours = _Ours()

    class _StateOurs:
        def get_slot(self, key):
            return ours

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("should not recreate an existing owned slot")

    assert await routes._ensure_worker_slot(_StateOurs(), "mine", {"working_dir": "/p"}) is ours
    assert ours.project == "/p"

    # (b) unscoped -> refused, untouched.
    class _Bare:
        key = "spec-builder-bare"
        _app = ""
        project = "/somebody/else"
        _titled = True

    bare = _Bare()

    class _StateBare:
        def get_slot(self, key):
            return bare

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over an unscoped slot")

    assert await routes._ensure_worker_slot(_StateBare(), "bare", {"working_dir": "/ours"}) is None
    assert bare._app == "" and bare.project == "/somebody/else"

    # (c) missing -> created and scoped.
    class _New:
        key = "spec-builder-fresh"
        _app = ""
        project = ""
        _titled = False

    fresh = _New()

    class _StateNew:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return fresh

    assert await routes._ensure_worker_slot(_StateNew(), "fresh", {"working_dir": "/new"}) is fresh
    assert fresh._app == routes.APP_NAME and fresh.project == "/new"


def test_dispatching_handlers_refuse_a_foreign_slot():
    """Source guard: any handler that dispatches a turn must bail when the slot
    could not be claimed, rather than passing None into _dispatch_turn."""

    for handler in (routes._handle_message, routes._handle_handoff, routes._handle_create):
        src = inspect.getsource(handler)
        claim = src.index("_ensure_worker_slot(")
        assert "if slot is None:" in src[claim:], f"{handler.__name__} ignores a refused slot"
        assert "status=409" in src[claim:], f"{handler.__name__} does not report the conflict"


# ── GPT round-18 findings (#518) ─────────────────────────────────────────────


def test_no_async_function_touches_the_filesystem_inline():
    """The reported stall (`wt_path.exists()` in _create_worktree) was the LAST
    filesystem call in this module still running on the event loop. This guard
    closes the class rather than the line: an async def may not call a stat/read/
    write directly — every one must sit in a helper marked BLOCKING and be
    invoked through asyncio.to_thread."""

    tree = ast.parse(inspect.getsource(routes))
    fs_attrs = {
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "read_text",
        "write_text",
        "unlink",
        "mkdir",
        "iterdir",
        "stat",
    }
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            # A bare `x.exists()` call. `to_thread(x.exists)` is an attribute
            # REFERENCE, not a call, so it is correctly not flagged.
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in fs_attrs
            ):
                offenders.append(f"{node.name}: .{inner.func.attr}()")
    assert not offenders, f"filesystem calls on the event loop: {offenders}"


@pytest.mark.asyncio
async def test_create_worktree_still_refuses_an_existing_path(tmp_path, monkeypatch):
    """Non-vacuous: offloading the probe must not lose the guard — an existing
    sibling path still aborts before git runs."""
    repo = Path(os.path.realpath(tmp_path)) / "repo"
    repo.mkdir()
    (repo.parent / "repo-wt-taken").mkdir()

    ran: list[tuple] = []

    async def _git(cwd, *args):
        ran.append(args)
        return 0, "", ""

    monkeypatch.setattr(routes, "_git", _git)
    monkeypatch.setattr(routes, "_repo_info", lambda p: _async_value({"default_base": "main"}))

    out = await routes._create_worktree(str(repo), "taken")
    assert isinstance(out, str) and "already exists" in out
    assert ran == [], "git ran despite the path already existing"


async def _async_value(v):
    return v


# ── GPT round-19 findings (#518) ─────────────────────────────────────────────


def test_resolved_spec_dir_is_revalidated_for_sensitivity(tmp_path, monkeypatch):
    """The reported hole: containment only says 'under the declared root'. If that
    root is (or becomes) a symlink into a credential tree, BOTH paths resolve
    through it, so _contained passes while the spec files would be created inside
    the credential directory. The RESOLVED destination now goes back through the
    chokepoint."""
    _redirect_state(monkeypatch, tmp_path)
    vault = Path(os.path.realpath(tmp_path)) / "vault"
    vault.mkdir()
    root = Path(os.path.realpath(tmp_path)) / "project"
    os.symlink(vault, root)

    # Treat the symlink TARGET as sensitive, exactly as is_sensitive_path would
    # for a real credential directory.
    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: str(vault) in p)

    spec_dir, refusal = routes._prepare_spec_dir(str(root), root, "sneaky", False)
    assert refusal == "escape", "spec dir was accepted inside a sensitive tree"
    assert not (vault / "sneaky").exists(), "files were created in the sensitive tree"
    assert spec_dir is not None


def test_ordinary_destination_is_still_created(tmp_path, monkeypatch):
    """Non-vacuous: the extra check must not refuse a normal destination."""
    _redirect_state(monkeypatch, tmp_path)
    wd = Path(os.path.realpath(tmp_path)) / "wd"
    wd.mkdir()
    spec_dir, refusal = routes._prepare_spec_dir(str(wd), wd, "fine", False)
    assert refusal == "" and spec_dir.is_dir()


@pytest.mark.asyncio
async def test_detail_refuses_when_the_spec_is_recreated_mid_request(tmp_path, monkeypatch):
    """The reported race, refined by round 20: the detail handler read the index,
    awaited the document collection, then used that PRE-AWAIT snapshot. A spec
    deleted and recreated elsewhere under the SAME NAME is a different spec, so
    continuing would pair documents read from the old directory with the new
    metadata and point the new worker at the old project. The request must refuse
    and let the client retry."""
    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "moved"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "moved"
    new_dir.mkdir(parents=True)
    routes._save_index(
        {"moved": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}}
    )

    real_collect = routes._collect_spec_documents

    def _collect_then_recreate(spec_dir):
        # Stand in for the concurrent delete+recreate landing during the hop.
        routes._save_index(
            {"moved": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}}
        )
        return real_collect(spec_dir)

    monkeypatch.setattr(routes, "_collect_spec_documents", _collect_then_recreate)

    scoped: list[str] = []

    class _Slot:
        key = "spec-builder-moved"
        _app = routes.APP_NAME
        project = ""
        _titled = False
        messages: list = []
        running = False

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            scoped.append(name)
            return slot

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.get(f"{_BASE}/specs/moved")
    finally:
        await client.close()

    assert resp.status == 409, "stale documents were served against fresh metadata"
    assert scoped == [], "a slot was scoped from a different spec's metadata"
    assert slot.project == "", f"worker was pointed at a project: {slot.project}"


@pytest.mark.asyncio
async def test_detail_serves_normally_when_nothing_changes(tmp_path, monkeypatch):
    """Non-vacuous: the identity check must not 409 the ordinary poll."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "steady"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    routes._save_index(
        {"steady": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    class _Slot:
        key = "spec-builder-steady"
        _app = routes.APP_NAME
        project = ""
        _titled = False
        messages: list = []
        running = False

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return slot

        def get_or_create_slot(self, name, app=""):
            return slot

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.get(f"{_BASE}/specs/steady")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["phase"] == "requirements"
    assert slot.project == str(tmp_path / "wd")


@pytest.mark.asyncio
async def test_touch_spec_pins_identity_not_just_the_name(tmp_path, monkeypatch):
    """The shared mechanism: a name is not an identity. With expect_spec_dir the
    mutator refuses a same-name entry that now points somewhere else, which is
    what stops handoff dispatching a stale plan and stop/delete acting on the
    wrong spec."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"s": {"spec_dir": "/a/spec", "status": "planning"}})

    assert await routes._touch_spec("s", expect_spec_dir="/b/spec", status="executing") is None
    assert routes._load_index()["s"]["status"] == "planning", "a mismatched spec was mutated"

    ok = await routes._touch_spec("s", expect_spec_dir="/a/spec", status="executing")
    assert ok is not None and routes._load_index()["s"]["status"] == "executing"


def test_identity_pinned_sites_pass_expect_spec_dir():
    """Source guard: the three handlers that act on a captured spec_dir must pin
    identity, not merely existence."""

    for handler in (routes._handle_handoff, routes._handle_stop_execution):
        src = inspect.getsource(handler)
        assert "expect_spec_dir=" in src, f"{handler.__name__} checks existence only"
    delete_src = inspect.getsource(routes._handle_delete)
    assert "doomed_dir" in delete_src, "delete pops by name without checking identity"
    detail_src = inspect.getsource(routes._handle_get)
    assert 'spec_dir", "")) != str(spec_dir)' in detail_src


# ── GPT round-21 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_abort_cleanup_spares_a_replacement_slot():
    """The reported defect, introduced by round 20's abort path: both cleanups look
    the slot up BY NAME, so unwinding a refused handoff destroyed the slot of the
    same-name spec that had replaced ours."""
    class _Slot:
        def __init__(self, tag):
            self.tag = tag
            self.key = "spec-builder-x"
            self._app = routes.APP_NAME
            self.running = False
            self.task = None

    ours = _Slot("ours")
    replacement = _Slot("replacement")
    slots = {"spec-builder-x": replacement}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    # Pinned to OUR captured slot: the registry now holds a different object.
    await routes._teardown_worker_slot(_State(), "x", only_slot=ours)
    assert "spec-builder-x" in slots, "the replacement spec's slot was destroyed"

    # Unpinned (delete's own path) still tears the live slot down.
    await routes._teardown_worker_slot(_State(), "x")
    assert "spec-builder-x" not in slots


@pytest.mark.asyncio
async def test_abort_cleanup_spares_a_replacement_nudge_loop(monkeypatch):
    """Same for the autonudge loop: get_by_slot keys off the name, so an unpinned
    removal cancelled the replacement spec's execution loop."""
    removed: list[str] = []

    class _Loop:
        id = "loop-new"

    class _Svc:
        def get_by_slot(self, key):
            return _Loop()

        async def remove(self, loop_id):
            removed.append(loop_id)

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    await routes._remove_nudge_loop("x", only_loop_id="loop-ours")
    assert removed == [], "the replacement spec's loop was cancelled"

    await routes._remove_nudge_loop("x", only_loop_id="loop-new")
    assert removed == ["loop-new"]


def test_handoff_abort_passes_both_captures():
    """Source guard: the abort path must pin both cleanups."""

    src = inspect.getsource(routes._handle_handoff)
    assert "only_loop_id=" in src and "only_slot=slot" in src


@pytest.mark.asyncio
async def test_stale_executing_status_settles_back_to_planning(tmp_path, monkeypatch):
    """The reported defect: the nudge loop is CAPPED, and when it runs out of
    cycles the service deactivates it without telling this app — so `executing`
    stayed in the index forever and the UI showed 'building' plus a Pause button
    for a run that had already finished."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "done"
    routes._save_index(
        {"done": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"),
                  "status": "executing"}}
    )
    meta = routes._load_index()["done"]

    class _Idle:
        running = False

    # No loop at all (the capped loop was removed) -> settles.
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)
    assert await routes._effective_status("done", meta, _Idle()) == "planning"
    assert routes._load_index()["done"]["status"] == "planning", "status not persisted"


@pytest.mark.asyncio
async def test_live_execution_is_not_settled(tmp_path, monkeypatch):
    """Non-vacuous: an execution whose loop is still active must keep reporting
    executing, and a deactivated loop must not."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"live": {"spec_dir": "/s/live", "status": "executing"}})
    meta = routes._load_index()["live"]

    class _Idle:
        running = False

    class _Loop:
        def __init__(self, active):
            self.id = "l1"
            self.active = active

    def _svc(active):
        class _Svc:
            def get_by_slot(self, key):
                return _Loop(active)
        return lambda: _Svc()

    monkeypatch.setattr(routes, "_autonudge_instance", _svc(True))
    assert await routes._effective_status("live", meta, _Idle()) == "executing"
    assert routes._load_index()["live"]["status"] == "executing"

    monkeypatch.setattr(routes, "_autonudge_instance", _svc(False))
    assert await routes._effective_status("live", meta, _Idle()) == "planning"


@pytest.mark.asyncio
async def test_running_turn_keeps_executing(tmp_path, monkeypatch):
    """A single dispatched turn with no loop (the degraded path) still counts as
    executing while the slot is actually running."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"one": {"spec_dir": "/s/one", "status": "executing"}})
    meta = routes._load_index()["one"]
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _Busy:
        running = True

    assert await routes._effective_status("one", meta, _Busy()) == "executing"


def test_status_is_served_reconciled_not_raw():
    """Source guard: both read endpoints must serve the reconciled status."""

    for handler in (routes._handle_list, routes._handle_get):
        src = inspect.getsource(handler)
        assert "_effective_status(" in src, f"{handler.__name__} serves the raw status"
        assert 'meta.get("status", "planning")' not in src


# ── GPT round-22 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handoff_confirms_identity_before_acquiring_the_slot(tmp_path, monkeypatch):
    """The reported defect: _prepare_handoff awaits, so a delete+recreate can land
    first. The stale request then captured the REPLACEMENT's slot, and its own
    abort path -- correctly pinned to what it captured -- would close the NEW
    session. Checking identity before acquisition means the stale request never
    touches the replacement."""
    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "swap"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "swap"
    for d in (old_dir, new_dir):
        d.mkdir(parents=True)
        (d / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {"swap": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}}
    )

    def _prepare_then_recreate(spec_dir, name="", expect_slot_key=""):
        # Mirrors the real signature: the handler now passes the name and the
        # identity it started with so the CLEAR itself can be refused.
        routes._save_index(
            {"swap": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}}
        )
        return True, str(spec_dir / routes._STOP_FILE)

    monkeypatch.setattr(routes, "_prepare_handoff", _prepare_then_recreate)

    touched: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            touched.append(name)
            raise AssertionError("slot acquired despite the spec being replaced")

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(f"{_BASE}/specs/swap/handoff")
    finally:
        await client.close()

    assert resp.status == 409, "stale handoff proceeded against a replaced spec"
    assert touched == [], "the replacement spec's slot was acquired"
    assert dispatched == []
    # The replacement is left in planning, untouched by the stale request.
    assert routes._load_index()["swap"]["spec_dir"] == str(new_dir)
    assert routes._load_index()["swap"].get("status") != "executing"


def test_handoff_checks_identity_before_slot_acquisition():
    """Source guard on the ORDERING, which is the whole fix."""

    src = inspect.getsource(routes._handle_handoff)
    check = src.index('!= str(spec_dir)')
    acquire = src.index("_ensure_worker_slot(")
    assert check < acquire, "handoff acquires the slot before confirming identity"


# ── GPT round-23 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_refuses_a_recreated_spec(tmp_path, monkeypatch):
    """The reported defect for message: an old browser tab sends into a name that
    has since been deleted and recreated pointing elsewhere. Existence was enough,
    so the turn was dispatched into the REPLACEMENT spec's agent."""
    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "t"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "t"
    routes._save_index({"t": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}})

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    real_read = routes._read_json

    async def _read_then_recreate(request):
        body = await real_read(request)
        routes._save_index(
            {"t": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}}
        )
        return body

    monkeypatch.setattr(routes, "_read_json", _read_then_recreate)

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("slot acquired for a replaced spec")

    await client.start_server()
    try:
        client.app["state"] = _State()
        # The SPA sends the spec_dir it rendered -- that CLIENT-captured identity is
        # what makes the stale tab detectable.
        resp = await client.post(
            f"{_BASE}/specs/t/message", json={"text": "hi", "spec_dir": str(old_dir)}
        )
    finally:
        await client.close()

    assert resp.status == 409, "message dispatched into a replaced spec"
    assert dispatched == []


@pytest.mark.asyncio
async def test_pinned_halt_spares_a_replacement_loop_and_slot(tmp_path, monkeypatch):
    """stop/delete look the loop and slot up BY NAME. With captures pinned, a
    replacement that appears mid-teardown is left alone."""
    removed: list[str] = []

    class _Loop:
        id = "loop-new"

    class _Svc:
        def get_by_slot(self, key):
            return _Loop()

        async def remove(self, loop_id):
            removed.append(loop_id)

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    class _Slot:
        key = "spec-builder-p"
        _app = routes.APP_NAME
        running = True
        task = None

    replacement = _Slot()
    slots = {"spec-builder-p": replacement}

    class _State:
        _slots = slots
        sessions = None

        def get_slot(self, key):
            return slots.get(key)

    spec_dir = Path(os.path.realpath(tmp_path)) / "spec"
    spec_dir.mkdir()

    # Captured NOTHING (no loop/slot at request time) -> nothing may be touched.
    await routes._halt_execution(
        _State(), "p", spec_dir, reason="test", only_loop_id=None, only_slot=None
    )
    assert removed == [], "a loop was removed with no capture"
    assert slots["spec-builder-p"] is replacement

    # Captured a DIFFERENT loop/slot -> still refused.
    other = _Slot()
    await routes._halt_execution(
        _State(), "p", spec_dir, reason="test", only_loop_id="loop-ours", only_slot=other
    )
    assert removed == [], "the replacement's loop was removed"

    # Matching capture -> acts.
    await routes._halt_execution(
        _State(), "p", spec_dir, reason="test", only_loop_id="loop-new", only_slot=replacement
    )
    assert removed == ["loop-new"]


def test_name_only_operations_are_identity_pinned():
    """Source guard: message stamps with expect_spec_dir, and stop/delete capture
    the loop id and slot object BEFORE they await."""
    msg = inspect.getsource(routes._handle_message)
    assert "expect_spec_dir=" in msg, "message stamps by name only"

    for handler, cap, acts_on in (
        (routes._handle_stop_execution, "captured_loop_id", "await _halt_execution("),
        (routes._handle_delete, "doomed_loop_id", "await _remove_nudge_loop("),
    ):
        src = inspect.getsource(handler)
        assert f"{cap} = _exec_loop_id(" in src, f"{handler.__name__} does not capture the loop"
        assert "only_loop_id=" in src and "only_slot=" in src, (
            f"{handler.__name__} does not pin its cleanups"
        )
        # The capture must precede the teardown it pins (nothing may await in
        # between and let a recreate become the thing we act on).
        assert src.index(cap) < src.index(acts_on), f"{handler.__name__} captures too late"


# ── GPT round-24 findings (#518) ─────────────────────────────────────────────


def test_sandbox_setup_is_offloaded():
    """The reported stall: sandboxed_spawn_argv looks like a string operation but
    probes the sandbox backend (which can shell out on first use on a host) and
    writes the scrubbed-env temp file, so building the argv froze the loop on the
    first browse."""
    body = inspect.getsource(routes._git).split('"""')[-1]  # drop docstring prose
    assert "asyncio.to_thread(" in body and "_prepare_git_spawn" in body
    for inline in ("sandboxed_spawn_argv(",):
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert inline not in stripped, (
                f"spawn setup still runs on the event loop: {stripped}"
            )
    prep = inspect.getsource(routes._prepare_git_spawn)
    assert "sandboxed_spawn_argv(" in prep
    # Limits are applied AFTER exec by the shim (see test_spawn_preexec_guard), so no
    # preexec callable is built here at all.
    assert "preexec" not in prep


@pytest.mark.asyncio
async def test_nudge_loop_removal_keeps_the_fsync_off_the_loop(tmp_path, monkeypatch):
    """The reported stall: AutoNudgeService.remove() called remove_sync(), whose
    _save() fsyncs -- so a Pause click or a spec delete blocked chat and heartbeats
    on disk. The service already documents the fix (remove_sync(persist=False) plus
    a snapshot-and-offload write, as update() does); remove() just did not use it."""
    from kiro_crew import autonudge as an

    # Assert the PROPERTY, not one implementation of it. This app needs
    # remove() to keep the fsync off the event loop (a Pause click or a spec
    # delete must not block chat on a wedged disk). Upstream now provides that
    # inline in remove() itself (#425); it previously lived in a helper this
    # branch added. Either shape satisfies the app, so the assertions below
    # describe the behaviour and stay agnostic about where it is written.
    src = inspect.getsource(an.AutoNudgeService.remove)
    assert "persist=False" in src, "remove() still fsyncs inline"
    assert "run_in_executor" in src, "remove() does not offload the write"
    assert "CancelledError" in src, "remove() does not drain the write on cancel"
    # The drain must not be time-bounded: giving up after N seconds releases the
    # lock while the worker is still fsyncing, so a later mutation can write
    # first and the abandoned older payload lands last.
    assert "wait_for" not in src, "the drain is time-bounded and can still be overtaken"

    # Behavioural: the loop really is gone and the state file really was written.
    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(slot_key="dashboard:x", message="go", idle_secs=60, max_cycles=1)
    assert svc.get_by_slot("dashboard:x") is not None

    writes: list[str] = []
    real_write = svc._write_state

    def _spy_write(payload):
        writes.append("w")
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", _spy_write)

    await svc.remove(loop.id)
    assert svc.get_by_slot("dashboard:x") is None, "loop was not removed"
    assert writes, "removal was not persisted"

    # Removing an unknown id must not write at all.
    writes.clear()
    await svc.remove("does-not-exist")
    assert writes == []


# ── GPT round-25 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_refuses_when_the_slot_is_foreign(tmp_path, monkeypatch):
    """The reported defect: the detail handler ignored _ensure_worker_slot's
    refusal and still returned 200, so ChatEmbed mounted against the unrelated
    session -- the user could read it, message into it and approve its tool
    calls from this app."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "hijacked"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {"hijacked": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    class _Foreign:
        key = "spec-builder-hijacked"
        _app = "some-other-app"
        project = "/other/project"
        running = False
        messages: list = []

    class _State:
        def get_slot(self, key):
            return _Foreign()

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over a foreign slot")

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.get(f"{_BASE}/specs/hijacked")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, "detail served a spec whose session belongs to another app"
    assert "another app" in body.get("error", "")


def test_seed_prompt_is_self_contained_and_type_aware():
    """The reported gap: the seed told the agent to "follow the `spec-workflow`
    skill exactly", but builtin apps are not run through bridges.register_app
    (that path symlinks from ~/.kiro/crew/apps/<name>/, which a wheel-shipped
    builtin has no copy of), so the skill is not on the agent's skill path. It
    also listed all three documents for every spec type, contradicting `quick`."""
    spec_dir = Path("/w/.kiro/specs/thing")

    quick = routes._seed_prompt("quick", "thing", spec_dir, "/w", "make it fast")
    assert "spec-workflow" not in quick, "seed still points at an unavailable skill"
    assert "design.md" not in quick.split("Do NOT write design.md")[0], (
        "quick spec is still told to write design.md"
    )
    assert "Do NOT write design.md" in quick
    assert str(spec_dir / "requirements.md") in quick
    assert str(spec_dir / "tasks.md") in quick

    feature = routes._seed_prompt("feature", "thing", spec_dir, "/w", "")
    for f in ("requirements.md", "design.md", "tasks.md"):
        assert str(spec_dir / f) in feature, f"feature spec omits {f}"

    bug = routes._seed_prompt("bug", "thing", spec_dir, "/w", "")
    assert "root cause" in bug.lower()

    # The state-file contract must be stated inline (it used to be "as the
    # skill's 'Structured state' section specifies").
    for token in ('"decisions"', '"blocking"', '"context"', ".spec-state.json"):
        assert token in quick, f"seed no longer states {token}"
    # ...and stay plumbing, never a chat topic or a deliverable.
    assert "never mention it in chat" in quick

    # An unknown type must not lose the deliverables entirely.
    assert str(spec_dir / "requirements.md") in routes._seed_prompt(
        "weird", "thing", spec_dir, "/w", ""
    )


# ── GPT round-26 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_persistence_cannot_be_overtaken(tmp_path):
    """The reported inversion, in my own round-24 change: run_in_executor hands
    back a future whose worker keeps writing after cancellation, so bailing out on
    CancelledError released the service lock mid-write. A later add() could then
    acquire the lock and persist FIRST, leaving the older payload to land last and
    erase the new loop across a restart. The drain keeps the lock until the write
    settles."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    doomed = await svc.add(slot_key="dashboard:a", message="a", idle_secs=60, max_cycles=1)

    order: list[str] = []
    release = threading.Event()
    real_write = svc._write_state

    def _slow_write(payload):
        order.append("write-start")
        release.wait(2.0)  # hold the worker inside the write
        real_write(payload)
        order.append("write-done")

    svc._write_state = _slow_write  # type: ignore[method-assign]

    remover = asyncio.create_task(svc.remove(doomed.id))
    await asyncio.sleep(0.05)  # let the write begin
    remover.cancel()
    await asyncio.sleep(0.05)

    # The lock must still be held: a competing writer cannot get in yet.
    assert svc._lock.locked(), "service lock released while the write was in flight"

    release.set()
    with contextlib.suppress(asyncio.CancelledError, BaseException):
        await remover

    assert order == ["write-start", "write-done"], order
    # Cancellation still propagated to the caller.
    assert remover.cancelled() or remover.done()


# ── GPT round-27 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handoff_unwinds_when_the_index_commit_raises(tmp_path, monkeypatch):
    """The original hole was a raising index write AFTER the loop was armed: the
    user was told the run failed while the persisted timer dispatched execution
    anyway. The commit now runs FIRST, so the same failure must leave nothing armed
    at all -- and still no dispatch, no slot and no "executing" state."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "boom"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {"boom": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    removed: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _Loop:
        id = "loop-armed"

    class _Svc:
        """Faithful stub: see the sibling unwind test -- the loop only exists once
        the handoff arms it, so the already-executing pre-check sees none."""

        def __init__(self):
            self.armed = False

        def get_by_slot(self, key):
            return _Loop() if self.armed else None

        async def add(self, **_kw):
            self.armed = True
            return _Loop()

        async def remove(self, loop_id):
            self.armed = False
            removed.append(loop_id)

    svc = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: svc)

    async def _authz(**_kw):
        # The handoff arms through authorize_and_add_nudge, not svc.add -- reflect
        # that here so the unwind has a loop of ours to remove.
        svc.armed = True
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    async def _boom(*_a, **_kw):
        raise OSError("index write failed")

    # The state write that can fail is now the atomic claim, and it runs BEFORE
    # anything is armed or created.
    monkeypatch.setattr(routes, "_claim_execution", _boom)

    class _Slot:
        key = "spec-builder-boom"
        _app = routes.APP_NAME
        project = ""
        _titled = False
        running = False
        task = None

    slot = _Slot()
    slots: dict = {}  # the slot does NOT pre-exist: this request creates it

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            slots["spec-builder-boom"] = slot
            return slot

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(f"{_BASE}/specs/boom/handoff")
    finally:
        await client.close()

    assert resp.status == 500
    assert dispatched == [], "a turn was dispatched despite the failed commit"
    assert removed == [], f"a loop was armed before the state was recorded: {removed}"
    assert svc.armed is False, "the loop was armed despite the failed commit"
    assert "spec-builder-boom" not in slots, "the worker slot was left behind"
    # And the spec is not left claiming to be executing.
    assert routes._load_index()["boom"].get("status") != "executing"


# ── GPT round-28 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_refuses_an_unscoped_slot():
    """The reported collision: _halt_active_turn tolerated an owner of None, so a
    plain POST /api/chat on slot `spec-builder-<name>` -- somebody else's
    conversation that merely shares the key -- could be cancelled mid-turn by this
    app's Stop button, losing that turn's response. Ownership must be EXACT here,
    as it already is in _ensure_worker_slot and _teardown_worker_slot."""
    stopped: list[str] = []
    cancelled = {"v": False}

    class _Task:
        def cancel(self):
            cancelled["v"] = True

        def done(self):
            return False

    class _Slot:
        def __init__(self, owner):
            self.key = "spec-builder-shared"
            self._app = owner
            self.running = True
            self.task = _Task()

    class _Sessions:
        async def stop_turn(self, key, force=False):
            stopped.append(key)

    def _state_for(slot):
        class _State:
            sessions = _Sessions()

            def get_slot(self, key):
                return slot
        return _State()

    for owner in (None, "", "some-other-app"):
        stopped.clear()
        cancelled["v"] = False
        slot = _Slot(owner)
        assert await routes._halt_active_turn(_state_for(slot), "shared") is False, (
            f"stopped a turn on a slot owned by {owner!r}"
        )
        assert stopped == [] and cancelled["v"] is False

    # Our own slot is still stopped.
    ours = _Slot(routes.APP_NAME)
    assert await routes._halt_active_turn(_state_for(ours), "shared") is True
    assert stopped and cancelled["v"] is True


def test_every_ownership_check_is_exact():
    """Source guard for the class: no ownership comparison may treat an unscoped
    slot as ours."""
    src = inspect.getsource(routes)
    for line in src.splitlines():
        stripped = line.strip()
        if "_app" not in stripped or stripped.startswith("#"):
            continue
        assert "not in (None" not in stripped, f"lax ownership check: {stripped}"


# ── GPT round-30 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_handoff_keeps_a_pre_existing_conversation(tmp_path, monkeypatch):
    """The reported defect: the unwind closed the worker slot unconditionally. When
    the slot ALREADY existed it carries the user's conversation (and possibly a
    running turn), so destroying it because a later index write failed lost work the
    handoff never owned. Only a slot this request created may be torn down."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "chatty"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {"chatty": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    class _Loop:
        id = "loop-armed"

    class _Svc:
        """Faithful stub: no loop exists until the handoff arms one, which is what
        the already-executing pre-check reads."""

        def __init__(self):
            self.armed = False

        def get_by_slot(self, key):
            return _Loop() if self.armed else None

        async def add(self, **_kw):
            self.armed = True
            return _Loop()

        async def remove(self, loop_id):
            self.armed = False

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    async def _authz(**_kw):
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    async def _boom(*_a, **_kw):
        raise OSError("index write failed")

    monkeypatch.setattr(routes, "_touch_spec", _boom)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: None)

    class _Slot:
        key = "spec-builder-chatty"
        _app = routes.APP_NAME
        project = ""
        _titled = True
        running = False
        task = None

    existing = _Slot()
    slots = {"spec-builder-chatty": existing}  # PRE-EXISTING conversation

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            return existing

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(f"{_BASE}/specs/chatty/handoff")
    finally:
        await client.close()

    assert resp.status == 500
    assert slots.get("spec-builder-chatty") is existing, (
        "a pre-existing conversation was destroyed by the failed handoff"
    )


@pytest.mark.asyncio
async def test_delete_commits_the_index_before_closing_the_session(tmp_path, monkeypatch):
    """The reported ordering bug: the teardown ran BEFORE the index pop, so a
    failing index write (disk full) returned 500 with the spec still listed and its
    conversation already discarded -- unusable and unrecoverable."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keepme"
    routes._save_index(
        {"keepme": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    async def _boom(_mutate):
        raise OSError("index write failed")

    monkeypatch.setattr(routes, "_mutate_index", _boom)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _Slot:
        key = "spec-builder-keepme"
        _app = routes.APP_NAME
        running = False
        task = None

    slot = _Slot()
    slots = {"spec-builder-keepme": slot}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    await client.start_server()
    try:
        client.app["state"] = _State()
        with contextlib.suppress(Exception):
            await client.delete(f"{_BASE}/specs/keepme")
    finally:
        await client.close()

    assert slots.get("spec-builder-keepme") is slot, (
        "the session was discarded before the delete was committed"
    )


def test_delete_orders_reserve_teardown_remove():
    """Source guard for the ordering the delete path depends on: the name is RESERVED
    before the session is torn down (so it cannot be taken while archival runs, and a
    rollback lands on the original entry with its own slot key), and the entry is only
    removed once the archive succeeded."""
    src = inspect.getsource(routes._handle_delete)
    reserve = src.index("_mark_deleting(")
    teardown = src.index("_teardown_worker_slot(")
    remove = src.index("_mutate_index(_pop_if_same)")
    assert reserve < teardown, "the session is torn down before the name is reserved"
    assert teardown < remove, "the entry is removed before the conversation is archived"
    # The failure arm releases the reservation rather than renaming the spec.
    assert "_unmark_deleting(" in src
    assert "_restore_under_free_name" not in src, "the renamed rollback is back"


def test_handoff_unwind_is_gated_on_having_created_the_slot():
    """Source guard: the unwind must consult the pre-existence flag."""
    src = inspect.getsource(routes._handle_handoff)
    assert "slot_pre_existed" in src
    start = src.index("async def _release(")
    # Delimit by the unwind's own last statement rather than the next "try:" —
    # the body contains one now (the best-effort loop removal), and slicing to it
    # cut the assertion's search space to nothing.
    release = src[start:src.index('_audit("spec_handoff_aborted"', start)]
    assert "if not slot_pre_existed:" in release, (
        "unwind tears down a slot it may not have created"
    )


# ── GPT round-31 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_messages_refuses_a_foreign_transcript(tmp_path, monkeypatch):
    """The reported leak: the messages endpoint ignored _ensure_worker_slot's
    refusal and served the transcript anyway -- somebody else's conversation read
    out through this app. Same refusal the detail endpoint already makes."""
    client = _make_client(monkeypatch, tmp_path)
    routes._save_index({"shared": {"spec_dir": "/s/shared", "working_dir": "/w"}})

    class _Foreign:
        key = "spec-builder-shared"
        _app = "some-other-app"
        running = True
        messages = [{"role": "user", "content": "private", "ts": "1"}]

    class _State:
        def get_slot(self, key):
            return _Foreign()

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over a foreign slot")

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.get(f"{_BASE}/specs/shared/messages")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, "served a transcript belonging to another app"
    assert "private" not in json.dumps(body)


def test_route_registration_touches_no_filesystem():
    """The reported startup stall: register_routes ran STATE_DIR.mkdir on the event
    loop during start_dashboard, so a KIROCREW_HOME on stalled network storage
    froze gateway startup -- on a directory the app may never need. Registration
    must create nothing; the off-loop writers mkdir themselves."""
    src = inspect.getsource(routes.register_routes)
    body = src.split('"""')[-1]
    for inline in ("mkdir(", "_state_dir()"):
        assert inline not in body, f"registration touches the filesystem: {inline}"
    # ...and the writers still do create it.
    for writer in (routes._save_index, routes._save_settings):
        assert "mkdir(" in inspect.getsource(writer), f"{writer.__name__} lost its mkdir"


def test_registration_works_with_an_uncreatable_state_dir(tmp_path, monkeypatch):
    """Non-vacuous: registration must succeed even when STATE_DIR cannot be made,
    which is what proves it no longer touches it."""
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "nope" / "deeper")

    def _explode(*_a, **_k):
        raise OSError("stalled storage")

    monkeypatch.setattr(Path, "mkdir", _explode)
    app = web.Application()
    routes.register_routes(app)  # must not raise
    assert any("/api/apps/spec-builder" in str(r.resource) for r in app.router.routes())


# ── GPT round-32 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_indexed_working_dir_is_revalidated(tmp_path, monkeypatch):
    """The reported escalation: the indexed working_dir was trusted verbatim and
    assigned to slot.project, which IS the agent's cwd. The index is app state on
    disk and the agent this app runs can be talked into rewriting files, so a
    rewritten entry would start the next turn inside a credential directory --
    where relative reads sidestep every per-path check this app makes. It now goes
    back through the _safe_dir chokepoint and an unusable value refuses the slot."""
    vault = Path(os.path.realpath(tmp_path)) / "vault"
    vault.mkdir()
    ordinary = Path(os.path.realpath(tmp_path)) / "project"
    ordinary.mkdir()
    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: str(vault) in str(p))

    class _Slot:
        key = "spec-builder-x"
        _app = routes.APP_NAME
        project = ""
        _titled = True

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return slot

        def get_or_create_slot(self, name, app=""):
            return slot

    # Sensitive target -> refused, and the cwd is NOT set.
    assert await routes._ensure_worker_slot(_State(), "x", {"working_dir": str(vault)}) is None
    assert slot.project == "", "a sensitive directory became the agent's cwd"

    # Non-existent path -> also refused (the chokepoint requires an existing dir).
    assert await routes._ensure_worker_slot(
        _State(), "x", {"working_dir": str(tmp_path / "gone")}
    ) is None
    assert slot.project == ""

    # Ordinary project -> accepted and scoped.
    assert await routes._ensure_worker_slot(
        _State(), "x", {"working_dir": str(ordinary)}
    ) is slot
    assert slot.project == str(ordinary)


def test_working_dir_validation_is_offloaded():
    """Source guard: the validation must not stat on the event loop, and it must
    run BEFORE the assignment it protects."""
    src = inspect.getsource(routes._ensure_worker_slot)
    assert "asyncio.to_thread(_safe_dir" in src
    assert src.index("safe_wd = await asyncio.to_thread") < src.index("slot.project ="), (
        "the cwd is assigned before it is validated"
    )
    assert "slot.project = wd" not in src, "the raw indexed value is still assigned"


# ── GPT round-33 findings (#518) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_refuses_when_the_spec_is_replaced_during_slot_setup(tmp_path, monkeypatch):
    """The window round 32 opened: making the working-dir chokepoint async means slot
    setup now AWAITS, so a delete-and-recreate can land between the index insert and
    the dispatch. The seed prompt names OUR spec_dir, so dispatching would drive the
    replacement spec's agent with our plan."""
    client = _make_client(monkeypatch, tmp_path)
    wd = Path(os.path.realpath(tmp_path)) / "wd"
    wd.mkdir()
    other = Path(os.path.realpath(tmp_path)) / "other" / ".kiro" / "specs" / "racy"
    other.mkdir(parents=True)

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    real_ensure = routes._ensure_worker_slot

    class _Slot:
        key = "spec-builder-racy"
        _app = routes.APP_NAME
        project = ""
        _titled = False

    slot = _Slot()

    async def _ensure_then_replace(state, name, meta, **kwargs):
        out = await real_ensure(state, name, meta, **kwargs)
        # The concurrent delete+recreate, landing inside the await window.
        routes._save_index({"racy": {"spec_dir": str(other), "working_dir": str(other.parent)}})
        return out

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_then_replace)

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return slot

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(
            f"{_BASE}/specs", json={"name": "racy", "working_dir": str(wd), "spec_type": "quick"}
        )
    finally:
        await client.close()

    assert resp.status == 409, "create dispatched against a replaced spec"
    assert dispatched == []
    # The REPLACEMENT's index entry survives -- the unwind is identity-pinned.
    assert routes._load_index()["racy"]["spec_dir"] == str(other), (
        "the unwind deleted the replacement spec's index entry"
    )


def test_create_unwind_is_identity_pinned():
    """Source guard: the create unwind must not pop by name alone, and the identity
    recheck must precede the dispatch."""
    src = inspect.getsource(routes._handle_create)
    assert "idx.pop(name, None)" not in src, "create unwinds by name alone"
    assert "_pop_if_ours" in src
    recheck = src.index('!= str(spec_dir)')
    assert recheck < src.index("_dispatch_turn("), "create dispatches before rechecking identity"


# ── GPT round-34 findings (#518) ─────────────────────────────────────────────


def test_persisted_shapes_are_validated(tmp_path, monkeypatch):
    """The reported crash: only the TOP-LEVEL shape was guarded. A settings file
    holding a list, or an index entry of null, reached .get() in the handlers and
    500ed the request."""
    _redirect_state(monkeypatch, tmp_path)

    # settings.json of the wrong shape -> treated as "no settings".
    for bad in ("[]", '"nope"', "null", "3"):
        routes._settings_path().parent.mkdir(parents=True, exist_ok=True)
        routes._settings_path().write_text(bad)
        assert routes._load_settings() == {"base_path": ""}, bad
        assert routes._load_settings().get("base_path") == ""

    # A malformed ENTRY is dropped; well-formed siblings survive.
    routes._index_path().write_text(
        json.dumps({"good": {"spec_dir": "/s/good"}, "bad": None, "worse": ["x"]})
    )
    idx = routes._load_index()
    assert set(idx) == {"good"}, idx
    for meta in idx.values():
        assert isinstance(meta, dict)

    # A whole file of the wrong shape is still empty, not a crash.
    routes._index_path().write_text("[1, 2]")
    assert routes._load_index() == {}


@pytest.mark.asyncio
async def test_list_survives_a_malformed_index_entry(tmp_path, monkeypatch):
    """Non-vacuous end to end: the endpoint that iterates every entry must serve
    200 rather than 500 when the file holds a bad one."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "good"
    spec_dir.mkdir(parents=True)
    routes._index_path().parent.mkdir(parents=True, exist_ok=True)
    routes._index_path().write_text(
        json.dumps({"good": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")},
                    "bad": None})
    )

    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert [s["name"] for s in body["specs"]] == ["good"]


@pytest.mark.asyncio
async def test_message_identity_comes_from_the_client(tmp_path, monkeypatch):
    """The reported vacuity in my round-23 fix: the pin read spec_dir out of the
    SAME index it then compared against, so it always matched. The identity has to
    be the one the CLIENT captured."""
    src = inspect.getsource(routes._handle_message)
    assert 'body.get("spec_dir"' in src, "message still derives identity from the index"
    assert 'expect_spec_dir=str(index[name]' not in src

    client = _make_client(monkeypatch, tmp_path)
    old_dir = tmp_path / "old" / ".kiro" / "specs" / "m"
    new_dir = tmp_path / "new" / ".kiro" / "specs" / "m"
    routes._save_index({"m": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}})

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("slot acquired for a stale client")

    await client.start_server()
    try:
        client.app["state"] = _State()
        # A stale tab claims the OLD spec_dir; the index now points elsewhere.
        stale = await client.post(
            f"{_BASE}/specs/m/message", json={"text": "hi", "spec_dir": str(old_dir)}
        )
    finally:
        await client.close()

    assert stale.status == 409, "a stale tab drove the replacement spec"
    assert dispatched == []


# ── GPT round-35 findings (#518) ─────────────────────────────────────────────


def test_discovery_validates_each_indexed_root(tmp_path, monkeypatch):
    """The reported hole: discovery derived its scan root from the indexed
    working_dir and stat-ed/enumerated it directly. A tampered entry pointing at a
    credential tree would be walked OUTSIDE the sensitive-path gate, and any
    spec-shaped directory inside it adopted into the index."""
    _redirect_state(monkeypatch, tmp_path)
    real = Path(os.path.realpath(tmp_path))
    vault, project = real / "vault", real / "project"
    for base in (vault, project):
        spec = base / ".kiro" / "specs" / f"found-{base.name}"
        spec.mkdir(parents=True)
        (spec / "requirements.md").write_text("# r")

    monkeypatch.setattr(routes, "is_sensitive_path", lambda p: str(vault) in str(p))

    index = {
        "seed-vault": {"working_dir": str(vault), "spec_dir": str(vault / "seed")},
        "seed-ok": {"working_dir": str(project), "spec_dir": str(project / "seed")},
    }
    routes._discover_folder_specs(index)

    assert "found-project" in index, "discovery stopped finding legitimate specs"
    assert "found-vault" not in index, "a spec inside a sensitive tree was adopted"


def test_discovery_root_validation_is_in_the_source():
    """Source guard: the derived scan root must go through the chokepoint, so a
    symlinked `.kiro/specs` cannot redirect the walk either."""
    src = inspect.getsource(routes._discover_folder_specs)
    assert src.count("_safe_dir(") >= 2, "discovery does not validate its scan roots"
    assert 'specs_base = Path(root) / ".kiro"' not in src


@pytest.mark.asyncio
async def test_controls_reject_a_stale_client_identity(tmp_path, monkeypatch):
    """The reported gap: execute / stop / delete sent only the NAME, so a control
    clicked in a tab whose spec had been deleted and recreated elsewhere drove the
    replacement -- executing the wrong project, or stopping/deleting its session.
    Each now carries the client's rendered spec_dir (query string for DELETE, which
    has no body) and refuses a mismatch before any side effect."""
    client = _make_client(monkeypatch, tmp_path)
    current = tmp_path / "now" / ".kiro" / "specs" / "c"
    current.mkdir(parents=True)
    (current / "tasks.md").write_text("- [ ] t")
    stale = str(tmp_path / "before" / ".kiro" / "specs" / "c")
    routes._save_index({"c": {"spec_dir": str(current), "working_dir": str(tmp_path / "now")}})

    halted: list[str] = []

    async def _spy_halt(*_a, **_k):
        halted.append("halt")

    monkeypatch.setattr(routes, "_halt_execution", _spy_halt)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: halted.append("dispatch"))
    monkeypatch.setattr(routes, "_teardown_worker_slot", lambda *a, **k: _noop())
    monkeypatch.setattr(routes, "_remove_nudge_loop", lambda *a, **k: _noop())

    await client.start_server()
    try:
        client.app["state"] = None
        ex = await client.post(f"{_BASE}/specs/c/execute", json={"spec_dir": stale})
        st = await client.post(f"{_BASE}/specs/c/stop", json={"spec_dir": stale})
        rm = await client.delete(f"{_BASE}/specs/c?spec_dir={stale}")
    finally:
        await client.close()

    assert (ex.status, st.status, rm.status) == (409, 409, 409), (
        f"a stale control was honoured: {(ex.status, st.status, rm.status)}"
    )
    assert halted == [], "a side effect ran for a stale client"
    assert "c" in routes._load_index(), "the replacement spec was deleted by a stale tab"


async def _noop(*_a, **_k):
    return None


def test_every_mutating_control_accepts_a_client_identity():
    """Source guard: the three name-only controls must consult the shared helper."""
    for handler in (
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
    ):
        src = inspect.getsource(handler)
        assert "_client_claim(request)" in src, f"{handler.__name__} takes no client identity"
        assert "_client_identity_mismatch(" in src


# ── Cold worker slot after a gateway restart ─────────────────────────────────


class _RestartState:
    """A gateway that came back up: no live slots, transcripts still on disk."""

    def __init__(self):
        self._slots: dict = {}
        self.created: list[str] = []

    def get_slot(self, key):
        return self._slots.get(key)

    def get_or_create_slot(self, name, app=""):
        self.created.append(name)
        slot = self._slots.get(name)
        if slot is None:
            slot = types.SimpleNamespace(
                key=name, messages=[], _app=app, project="", _titled=False, running=False
            )
            self._slots[name] = slot
        return slot


def _stub_restore(monkeypatch, state, *, app: str, messages: list[str]):
    """Stand in for core's rehydrate: land a slot carrying a transcript."""

    async def _restore(_state, slot_key, *, adopt_closed=False):
        assert adopt_closed is True, "a worker transcript must survive an idle-archive close"
        slot = types.SimpleNamespace(
            key=slot_key,
            messages=[{"role": "user", "content": m} for m in messages],
            _app=app,
            project="",
            _titled=True,
            running=False,
        )
        state._slots[slot_key] = slot
        return slot

    monkeypatch.setattr(routes, "rehydrate_slot_from_history_async", _restore)


@pytest.mark.asyncio
async def test_restart_restores_the_worker_transcript(tmp_path, monkeypatch):
    """The reported bug: slots live in memory, so a gateway restart emptied the
    chat column ("Session ready. Type a message to start.") for a spec that was
    mid-build -- and the next message started a context-free turn -- even though
    the whole transcript was still on disk under the same key. A cold slot now
    rehydrates from history before anything creates an empty one."""
    _redirect_state(monkeypatch, tmp_path)
    state = _RestartState()
    _stub_restore(monkeypatch, state, app=routes.APP_NAME, messages=["build me a spec", "on it"])

    slot = await routes._ensure_worker_slot(state, "s1", {"working_dir": str(tmp_path)})

    assert slot is not None
    assert [m["content"] for m in slot.messages] == ["build me a spec", "on it"], (
        "the persisted conversation did not come back"
    )
    assert state.created == [], "an empty slot was created instead of restoring"


@pytest.mark.asyncio
async def test_restore_does_not_adopt_another_apps_transcript(tmp_path, monkeypatch):
    """A transcript is as owned as a live slot. The restore lands the slot in
    state._slots, so the SAME ownership check must govern it -- otherwise
    rehydration became a way to pull another app's conversation (and its project
    dir) into this app, which the live-slot check explicitly forbids."""
    _redirect_state(monkeypatch, tmp_path)
    state = _RestartState()
    _stub_restore(monkeypatch, state, app="some-other-app", messages=["their work"])

    slot = await routes._ensure_worker_slot(state, "s1", {"working_dir": str(tmp_path)})

    assert slot is None, "a foreign transcript was adopted"


@pytest.mark.asyncio
async def test_restore_failure_still_yields_a_working_slot(tmp_path, monkeypatch):
    """Fail-open on the RESTORE only: a malformed or unreadable transcript must
    not take the spec offline. The user loses scrollback, not the app."""
    _redirect_state(monkeypatch, tmp_path)
    state = _RestartState()

    async def _boom(*_a, **_k):
        raise OSError("history is corrupt")

    monkeypatch.setattr(routes, "rehydrate_slot_from_history_async", _boom)

    slot = await routes._ensure_worker_slot(state, "s1", {"working_dir": str(tmp_path)})

    assert slot is not None, "a broken transcript took the spec offline"
    assert getattr(slot, "_app", None) == routes.APP_NAME


def test_transcript_restore_runs_before_slot_creation():
    """Source guard: the restore must precede get_or_create_slot. Core's resume
    returns early when a slot already exists, so creating the empty slot first
    permanently hides the transcript -- including from the user's own manual
    resume in the sidebar."""
    # Compare CODE only: the docstring names get_or_create_slot too.
    body = inspect.getsource(routes._ensure_worker_slot).split('"""')[-1]
    assert body.index("_restore_worker_transcript") < body.index("get_or_create_slot"), (
        "the empty slot is created before the transcript is restored"
    )


# ── GPT round-37 findings + scrub/CodeQL fallout (#518) ──────────────────────


def test_settings_reader_normalizes_a_non_string_base_path(tmp_path, monkeypatch):
    """The reported crash: _load_settings validated the OUTER shape only, so
    ``{"base_path": []}`` passed as a dict and every reader then called .strip()
    on a list -- 500ing spec creation and the settings endpoint."""
    _redirect_state(monkeypatch, tmp_path)
    routes._settings_path().parent.mkdir(parents=True, exist_ok=True)

    for bad in ('{"base_path": []}', '{"base_path": 7}', '{"base_path": null}', '{"base_path": {}}'):
        routes._settings_path().write_text(bad)
        assert routes._load_settings()["base_path"] == "", bad
        # The real crash was downstream: this must not raise.
        routes._resolve_spec_dir(str(tmp_path), "s1")

    # A legitimate string still survives untouched.
    routes._settings_path().write_text('{"base_path": "/tmp/base"}')
    assert routes._load_settings()["base_path"] == "/tmp/base"


def test_opt_in_flags_require_a_real_boolean():
    """The reported bypass: bool("false") is True, so a request that said NOT to
    create a worktree (or NOT to adopt existing documents) did both. These two
    flags cause side effects a retry cannot undo, so the check is exact."""
    rejected: tuple[object, ...] = ("false", "0", "no", "", 0, [], None, 1, "true")
    for truthy_but_not_true in rejected:
        assert routes._opted_in({"use_worktree": truthy_but_not_true}, "use_worktree") is False, (
            f"{truthy_but_not_true!r} opted in"
        )
    assert routes._opted_in({"use_worktree": True}, "use_worktree") is True
    assert routes._opted_in({}, "import_existing") is False

    # Source guard: no handler may go back to coercing these flags with bool().
    src = inspect.getsource(routes)
    for field in ("use_worktree", "import_existing"):
        assert f'bool(body.get("{field}"))' not in src, f"{field} is coerced with bool() again"


@pytest.mark.asyncio
async def test_slot_acquisition_refuses_an_out_of_grammar_name(tmp_path, monkeypatch):
    """A name read back from index.json is untrusted app state, and from here it
    becomes a slot key and then a history key -- reaching core's session-key
    parsing. Unbounded input on that path is what CodeQL flagged once this
    function started restoring transcripts."""
    _redirect_state(monkeypatch, tmp_path)

    class _State:
        def __init__(self):
            self.touched: list[str] = []

        def get_slot(self, key):
            self.touched.append(key)
            return None

        def get_or_create_slot(self, name, app=""):
            self.touched.append(name)
            return types.SimpleNamespace(key=name, _app=app, project="", messages=[])

    for bad in ("x" * 400, "../etc/passwd", "has space", "", "9" * 200 + "." + "9" * 200):
        state = _State()
        assert await routes._ensure_worker_slot(state, bad, {"working_dir": str(tmp_path)}) is None, bad
        assert state.touched == [], f"{bad[:20]!r} reached the slot layer"


def test_browse_skip_list_carries_no_hidden_paths():
    """_scan_subdirs already skips every entry starting with '.', so listing
    dotted names in the skip set duplicated that rule -- and one of them put a
    literal internal path marker in the source, which the repo's scrub lint
    rejects."""
    assert not [n for n in routes._BROWSE_SKIP if n.startswith(".")]
    assert "startswith(\".\")" in inspect.getsource(routes._scan_subdirs), (
        "the hidden-entry skip that makes the dotted names redundant is gone"
    )


# ── GPT round-38 findings (#518) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_reads_the_body_before_the_index(tmp_path, monkeypatch):
    """The reported window: reading the request body is an await, and it sat
    AFTER the index read -- so a delete+recreate landing while a slow body
    arrived left the identity check describing the OLD spec while the loop id and
    slot captured afterwards belonged to the REPLACEMENT."""
    _redirect_state(monkeypatch, tmp_path)
    src = inspect.getsource(routes._handle_stop_execution)
    body_at = src.index("_client_claim(request)")
    index_at = src.index("_aload_index()")
    capture_at = src.index("_exec_loop_id(name)")
    assert body_at < index_at < capture_at, "the body await is not first"
    # And no await may sit between the verified identity and the capture.
    between = src[src.index("_client_identity_mismatch(claimed"):capture_at]
    assert "await" not in between, "an await reopened the capture window"


def test_every_identity_checked_handler_parses_the_body_first():
    """Source guard for the class: stop, delete and handoff all had the body
    await after the index read whose result the identity check compares against.
    One fix, three call sites. (Handoff also reads the index at its top to find
    the spec at all; what matters is the RE-read that the check is paired with.)"""
    for handler in (
        routes._handle_stop_execution,
        routes._handle_delete,
        routes._handle_handoff,
    ):
        src = inspect.getsource(handler)
        assert "claimed = await _client_claim(request)" in src, handler.__name__
        assert src.index("claimed = await _client_claim(request)") < src.rindex(
            "_aload_index()"
        ), f"{handler.__name__} reads the deciding index snapshot before the body"


def test_sentinel_clear_is_pinned_to_the_verified_directory(tmp_path, monkeypatch):
    """The reported attack: the directory is verified, then the agent swaps it
    for a symlink, and a PATH-based unlink resolves through the replacement and
    deletes a STOP file outside the spec. The unlink is now relative to a pinned
    non-following descriptor, so it lands in the verified directory or nowhere."""
    real = Path(os.path.realpath(tmp_path))
    spec = real / "wd" / ".kiro" / "specs" / "s1"
    spec.mkdir(parents=True)
    (spec / routes._STOP_FILE).write_text("stop")

    victim = real / "other" / ".kiro" / "specs" / "s2"
    victim.mkdir(parents=True)
    victim_stop = victim / routes._STOP_FILE
    victim_stop.write_text("do not delete me")

    # Baseline: it clears its OWN sentinel.
    routes._clear_stop_sentinel(spec)
    assert not (spec / routes._STOP_FILE).exists()

    # Swap the verified directory for a link to the victim AFTER verification by
    # making _verified_spec_dir hand back a path whose last component is a link.
    link = real / "wd" / ".kiro" / "specs" / "linked"
    link.symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr(routes, "_verified_spec_dir", lambda p: link)
    routes._clear_stop_sentinel(link)

    if routes._CAN_PIN_DIR:
        assert victim_stop.exists(), "the unlink followed a replaced directory"
    else:  # pragma: no cover - Windows fallback
        assert True


def test_sentinel_pin_capability_is_resolved_once():
    """Guard: the confinement must be decided from real platform capability, not
    a per-call guess, and the source must not fall back to a bare path unlink."""
    assert routes._CAN_PIN_DIR is (
        hasattr(os, "O_DIRECTORY") and os.unlink in os.supports_dir_fd
    )
    src = inspect.getsource(routes._clear_stop_sentinel)
    assert "dir_fd=dir_fd" in src
    assert "O_NOFOLLOW" in src


def test_slack_ts_regex_is_bounded():
    """CodeQL py/polynomial-redos: this predicate is reached with keys that did
    not come from Slack (an app backend restoring a saved conversation), so an
    unbounded \\d+ on both sides of the dot backtracked quadratically."""
    from kiro_crew.messaging import link as link_mod

    assert "\\d+\\." not in link_mod._SLACK_TS_RE.pattern, "the runs are unbounded again"
    # Real timestamps still match; a long digit run does not hang or match.
    assert link_mod.is_legacy_slack_key("1785360749.165719")
    assert not link_mod.is_legacy_slack_key("9" * 400 + "." + "9" * 400)
    assert not link_mod.is_legacy_slack_key("dashboard:spec-builder-x")


# ── GPT round-39 findings (#518) ───────────────────────────────────────────────


def test_index_entries_missing_identity_fields_are_dropped(tmp_path, monkeypatch):
    """The reported crash: the sanitizer accepted any dict, so ``{"demo": {}}``
    survived and handlers that dereference the required fields directly
    (``meta["spec_dir"]``) raised KeyError and 500ed the request."""
    _redirect_state(monkeypatch, tmp_path)
    routes._index_path().parent.mkdir(parents=True, exist_ok=True)
    good = {"spec_dir": str(tmp_path / "s"), "working_dir": str(tmp_path)}
    routes._index_path().write_text(json.dumps({
        "demo": {},                                  # the reported shape
        "blank": {"spec_dir": "   "},
        "typed": {"spec_dir": []},
        "ok": good,
    }))

    index = routes._load_index()

    assert list(index) == ["ok"], index
    # Handlers dereference this directly; the whole point is that it now exists.
    assert index["ok"]["spec_dir"]


@pytest.mark.asyncio
async def test_delete_aborts_when_the_loop_cannot_be_removed(tmp_path, monkeypatch):
    """The reported lie: a failing autonudge store was swallowed, so DELETE
    returned 200 with the spec dropped from the index while the persisted loop
    survived -- free to rearm after a restart against a re-imported same-name
    spec. The delete now fails and leaves the entry in place, so a retry means
    something."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "doomed"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {"doomed": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    async def _boom(*_a, **_k):
        raise OSError("autonudge store is read-only")

    monkeypatch.setattr(routes, "_remove_nudge_loop", _boom)
    torn_down: list[str] = []
    monkeypatch.setattr(
        routes, "_teardown_worker_slot", lambda *a, **k: _noop_await(torn_down)
    )

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/doomed")
    finally:
        await client.close()

    assert resp.status == 503, await resp.text()
    assert "doomed" in routes._load_index(), "the spec was dropped despite the failure"
    assert torn_down == [], "the session was torn down for a delete that did not happen"


async def _noop_await(sink: list) -> None:
    sink.append("called")


@pytest.mark.asyncio
async def test_stop_reports_failure_instead_of_a_halt_that_did_not_happen(
    tmp_path, monkeypatch
):
    """Same class on the stop path: reporting "planning" while the loop can still
    nudge tells the user to stop worrying about a run that is still going."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "running"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {"running": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"),
                     "status": "executing"}}
    )

    async def _boom(*_a, **_k):
        raise OSError("autonudge store is read-only")

    monkeypatch.setattr(routes, "_halt_execution", _boom)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.post(f"{_BASE}/specs/running/stop", json={})
    finally:
        await client.close()

    assert resp.status == 503, await resp.text()
    assert routes._load_index()["running"].get("status") == "executing", (
        "the spec was marked stopped after a failed halt"
    )


def test_loop_removal_does_not_swallow_failures():
    """Source guard: the helper must not go back to logging-and-continuing, and
    the two paths that stay best-effort must catch it visibly."""
    src = inspect.getsource(routes._remove_nudge_loop)
    assert "except Exception" not in src, "loop removal swallows failures again"
    delete_src = inspect.getsource(routes._handle_delete)
    assert "status=503" in delete_src and "_remove_nudge_loop" in delete_src


# ── GPT round-40 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_does_not_inherit_a_deleted_specs_conversation(tmp_path, monkeypatch):
    """The reported leak: round 36 restored transcripts with adopt_closed=True at
    the chokepoint, and a delete leaves the archived conversation on disk under a
    key derived from the NAME -- so creating a new spec with a previously used
    name handed the fresh agent the deleted spec's chat."""
    _redirect_state(monkeypatch, tmp_path)
    seen: list[bool] = []

    async def _spy(_state, _key, *, adopt_closed=True):
        seen.append(adopt_closed)
        return None

    monkeypatch.setattr(routes, "rehydrate_slot_from_history_async", _spy)

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return types.SimpleNamespace(
                key=name, _app=app, project="", messages=[], _titled=False
            )

    meta = {"working_dir": str(tmp_path), "spec_dir": str(tmp_path / "s")}
    await routes._ensure_worker_slot(_State(), "reused-name", meta, adopt_closed=False)
    assert seen == [False], "a fresh spec was offered closed history"

    # The default stays True for a spec already in the index: idle-slot cleanup
    # closes those without the user asking, and losing them was the round-36 bug.
    seen.clear()
    await routes._ensure_worker_slot(_State(), "existing", meta)
    assert seen == [True]


def test_create_handler_refuses_closed_history():
    """Source guard: the create path must pass adopt_closed=False on the CALL.
    Matching the bare keyword also matched the comment above it explaining why —
    a guard a revert could not fail."""
    src = inspect.getsource(routes._handle_create)
    assert "_ensure_worker_slot(state, name, entry, adopt_closed=False)" in src, (
        "create can adopt a deleted conversation again"
    )


@pytest.mark.asyncio
async def test_delete_restores_the_spec_when_archiving_fails(tmp_path, monkeypatch):
    """The reported data loss: the closing history write was best-effort, so a
    failed archive was logged at DEBUG while DELETE returned 200 -- the spec gone
    from the index and the conversation never written. The entry now goes back and
    the caller is told to retry."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keepme"
    spec_dir.mkdir(parents=True)
    entry = {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"),
             "spec_type": "plan"}
    routes._save_index({"keepme": entry})

    slot = types.SimpleNamespace(
        key=routes._slot_key("keepme"), _app=routes.APP_NAME, running=False, task=None,
        messages=[{"role": "user", "content": "unsaved work"}],
    )

    class _State:
        def __init__(self):
            self._slots = {routes._slot_key("keepme"): slot}

        def get_slot(self, key):
            return self._slots.get(key)

    state = _State()
    monkeypatch.setattr(routes, "_remove_nudge_loop", lambda *a, **k: _noop_await([]))

    async def _archive_boom(*_a, **_k):
        raise OSError("history volume is full")

    import kiro_crew.dashboard.chat_persistence as cp
    monkeypatch.setattr(cp, "save_slot_off_loop", _archive_boom)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.delete(f"{_BASE}/specs/keepme")
    finally:
        await client.close()

    assert resp.status == 503, await resp.text()
    index = routes._load_index()
    assert "keepme" in index, "the spec was dropped although its chat was never archived"
    assert index["keepme"]["spec_dir"] == str(spec_dir), "the entry came back altered"
    # The slot is back in the registry, so the conversation is still reachable.
    assert state.get_slot(routes._slot_key("keepme")) is slot


# ── GPT round-41 findings (#518) ───────────────────────────────────────────────


def test_deleted_specs_are_not_rediscovered(tmp_path, monkeypatch):
    """The reported resurrection: delete leaves the .md files on disk by design,
    and discovery adopts any spec-shaped directory under a known project root --
    so as long as a SIBLING spec kept that root indexed, the next list scan added
    the deleted spec straight back."""
    _redirect_state(monkeypatch, tmp_path)
    real = Path(os.path.realpath(tmp_path))
    project = real / "wd"
    keep, gone = (project / ".kiro" / "specs" / "keep"), (project / ".kiro" / "specs" / "gone")
    for d in (keep, gone):
        d.mkdir(parents=True)
        (d / "requirements.md").write_text("# r")

    index = {"keep": {"spec_dir": str(keep), "working_dir": str(project)}}

    # Baseline: without a tombstone the sibling's root brings "gone" back.
    routes._discover_folder_specs(dict(index))
    fresh = dict(index)
    assert routes._discover_folder_specs(fresh) is True
    assert "gone" in fresh

    # With the deletion remembered, it stays deleted.
    routes._remember_deleted(str(gone))
    guarded = dict(index)
    assert routes._discover_folder_specs(guarded) is False
    assert "gone" not in guarded

    # Creating it again is an explicit decision that outranks the tombstone.
    routes._forget_deleted(str(gone))
    revived = dict(index)
    assert routes._discover_folder_specs(revived) is True
    assert "gone" in revived


def test_tombstone_file_shape_is_untrusted_and_bounded(tmp_path, monkeypatch):
    """The file is app state on disk: a malformed shape must not crash discovery,
    and the list is capped so it cannot grow without limit."""
    _redirect_state(monkeypatch, tmp_path)
    routes._state_dir().mkdir(parents=True, exist_ok=True)
    for bad in ('{"not": "a list"}', "null", "[1, 2, 3]", "not json at all"):
        routes._deleted_path().write_text(bad)
        assert routes._load_deleted() == []

    for i in range(routes._MAX_TOMBSTONES + 25):
        routes._remember_deleted(f"/p/spec-{i}")
    kept = routes._load_deleted()
    assert len(kept) == routes._MAX_TOMBSTONES
    assert kept[-1] == f"/p/spec-{routes._MAX_TOMBSTONES + 24}", "newest deletion fell off"


def test_slot_key_is_per_creation_not_per_name(tmp_path, monkeypatch):
    """The reported merge: the slot key was derived from the NAME, so a spec
    recreated under a reused name wrote into the previous spec's history file --
    the fresh save preserved the archived rows and a restart rehydrated both
    conversations interleaved."""
    _redirect_state(monkeypatch, tmp_path)
    first, second = routes._new_slot_key("dup"), routes._new_slot_key("dup")
    assert first != second, "two creations of one name share a transcript"
    for key in (first, second):
        assert routes._SLOT_KEY_RE.match(key), key

    # The resolver prefers the persisted key...
    routes._save_index({"dup": {"spec_dir": "/a/dup", "slot_key": second}})
    routes._load_index()
    assert routes._slot_key("dup") == second

    # ...and falls back to the legacy form for entries written before it existed,
    # so specs that already have a transcript keep it.
    routes._save_index({"legacy": {"spec_dir": "/a/legacy"}})
    routes._load_index()
    assert routes._slot_key("legacy") == "spec-builder-legacy"


def test_persisted_slot_key_must_match_the_grammar(tmp_path, monkeypatch):
    """A slot key becomes a session FILENAME and reaches core's session-key
    parsing, so a tampered index must not be able to steer it."""
    _redirect_state(monkeypatch, tmp_path)
    for bad in ("../../etc/passwd", "spec-builder-" + "x" * 200, "other-app-key", "", 7):
        routes._save_index({"s": {"spec_dir": "/a/s", "slot_key": bad}})
        routes._load_index()
        assert routes._slot_key("s") == "spec-builder-s", bad


def test_slot_key_resolution_has_a_single_source():
    """The resolver map is rebuilt in one helper, used by both index chokepoints,
    so no handler threads the key through and none can read a stale one."""
    src = inspect.getsource(routes._refresh_slot_keys)
    # Ownership, not just grammar: the key must encode the entry's own name.
    assert "_owns_slot_key(" in src
    # Whole-dict replacement, because both chokepoints run on worker threads.
    assert "_SLOT_KEYS = {" in src


# ── GPT round-42 findings (#518) ───────────────────────────────────────────────


def test_a_freshly_minted_slot_key_survives_the_commit(tmp_path, monkeypatch):
    """The reported break in round 41's own fix: create minted a unique key, then
    committed through _mutate_index -- whose internal RE-READ rebuilt the resolver
    map from the pre-insert snapshot and discarded it. Everything afterwards (seed
    turn, embedded chat, teardown) fell back to the legacy name-derived key while
    the index held the unique one, splitting one spec across two slots."""
    _redirect_state(monkeypatch, tmp_path)
    minted = routes._new_slot_key("fresh")

    # The read chokepoint sees an index without our entry (the pre-insert state)...
    routes._save_index({})
    routes._load_index()
    # ...then the commit lands. The resolver must follow the WRITE, not just reads.
    routes._save_index({"fresh": {"spec_dir": str(tmp_path / "s"), "slot_key": minted}})

    assert routes._slot_key("fresh") == minted, "the minted key was discarded at commit"


def test_both_index_chokepoints_refresh_the_resolver():
    """Source guard: read-only refresh is what caused the split, so the writer
    must refresh too."""
    for fn in (routes._load_index, routes._save_index):
        assert "_refresh_slot_keys" in inspect.getsource(fn), fn.__name__


def test_detail_reports_the_slot_key_it_scoped(tmp_path, monkeypatch):
    """The SPA must not derive the slot key from the name -- with per-creation keys
    a reused name would mount the embed on the previous spec's transcript. The
    detail payload therefore names the session the app itself scoped."""
    src = inspect.getsource(routes._handle_get)
    assert '"slot_key": getattr(slot, "key", None) or _slot_key(name)' in src, (
        "detail no longer tells the client which slot to mount"
    )


@pytest.mark.asyncio
async def test_teardown_uses_the_captured_slots_own_key(tmp_path, monkeypatch):
    """Recomputing the key from the name tore down a DIFFERENT slot once keys are
    per-creation. The pinned slot's own key wins."""
    _redirect_state(monkeypatch, tmp_path)
    unique = routes._new_slot_key("s")
    slot = types.SimpleNamespace(
        key=unique, _app=routes.APP_NAME, running=False, task=None, messages=[]
    )

    class _State:
        def __init__(self):
            self._slots = {unique: slot}
            self.asked: list[str] = []

        def get_slot(self, key):
            self.asked.append(key)
            return self._slots.get(key)

    state = _State()
    import kiro_crew.dashboard.chat_persistence as cp
    monkeypatch.setattr(cp, "save_slot_off_loop", lambda *a, **k: _noop_await([]))

    assert await routes._teardown_worker_slot(state, "s", only_slot=slot) is True
    assert state.asked == [unique], f"looked up {state.asked} instead of the pinned key"
    assert unique not in state._slots


@pytest.mark.asyncio
async def test_git_refuses_to_run_when_the_invocation_cannot_be_audited(tmp_path, monkeypatch):
    """The reported gap: _audit_tool returned silently when SEL was missing or its
    log unwritable, so this app spawned git on the user's repository with no
    tool-invocation record. The invocation event is a precondition for the spawn."""
    spawned: list[list[str]] = []

    def _spawn_prep(argv):
        spawned.append(argv)
        raise AssertionError("git was spawned without an audit record")

    monkeypatch.setattr(routes, "_prepare_git_spawn", _spawn_prep)
    monkeypatch.setattr(routes, "sel", None)

    rc, out, err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")

    assert rc == routes._GIT_UNAVAILABLE
    assert "audit" in err
    assert spawned == []

    # Outcome events stay best-effort: a failure there must not turn a command
    # that already ran into an error.
    src = inspect.getsource(routes._git)
    gate = 'await asyncio.to_thread(_audit_tool, "invoked", subcommand, cwd, critical=True)'
    assert gate in src, "the invocation audit is not the critical, off-loop form"
    assert src.index(gate) < src.index("_prepare_git_spawn"), "git is prepared before the audit"
    # Outcome events stay queued and best-effort: they must NOT be critical, or a
    # failed log write would turn a command that already ran into an error.
    for outcome in ('"error"', '"success" if rc == 0 else "failure"'):
        for line in src.splitlines():
            if f"_audit_tool({outcome}" in line:
                assert "critical" not in line, f"outcome audit made critical: {line.strip()}"
    assert src.count('_audit_tool("error"') >= 1


# ── GPT round-43 findings (#518) ───────────────────────────────────────────────


# ── GPT round-44 finding (#518) ────────────────────────────────────────────────


def test_sentinel_write_is_pinned_to_the_verified_directory(tmp_path, monkeypatch):
    """The reported traversal, and the half of round 38 I left open: the sentinel
    CLEAR was pinned to a directory descriptor but the WRITE still worked through
    paths. An agent that swaps its verified directory for a symlink between the
    check and the open redirects both the temp create and the rename, so ANOTHER
    active spec receives the STOP file and halts."""
    real = Path(os.path.realpath(tmp_path))
    mine = real / "wd" / ".kiro" / "specs" / "mine"
    mine.mkdir(parents=True)

    victim = real / "other" / ".kiro" / "specs" / "victim"
    victim.mkdir(parents=True)

    # Baseline: it writes its own sentinel.
    assert routes._write_stop_sentinel(mine) is True
    assert (mine / routes._STOP_FILE).is_file()

    # Now the verified path's last component is a link to the victim -- the swap
    # this finding describes.
    link = real / "wd" / ".kiro" / "specs" / "swapped"
    link.symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr(routes, "_verified_spec_dir", lambda p: link)
    routes._write_stop_sentinel(link)

    if routes._CAN_PIN_DIR:
        assert not (victim / routes._STOP_FILE).exists(), (
            "the sentinel write followed a replaced directory and halted another spec"
        )
    else:  # pragma: no cover - Windows fallback
        assert True


def test_sentinel_write_leaves_no_temp_behind_on_failure(tmp_path, monkeypatch):
    """A failed rename must not strand the temp file in the spec directory."""
    real = Path(os.path.realpath(tmp_path))
    spec = real / "wd" / ".kiro" / "specs" / "s"
    spec.mkdir(parents=True)
    monkeypatch.setattr(routes, "_verified_spec_dir", lambda p: spec)

    def _boom(*_a, **_k):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", _boom)
    assert routes._write_stop_sentinel(spec) is False
    assert list(spec.iterdir()) == [], f"left {[p.name for p in spec.iterdir()]}"


def test_both_sentinel_helpers_pin_the_directory():
    """Source guard: write and clear must BOTH operate relative to a pinned
    descriptor, and the capability probe must cover every op they use."""
    for fn in (routes._write_stop_sentinel, routes._clear_stop_sentinel):
        src = inspect.getsource(fn)
        assert "O_DIRECTORY" in src and "dir_fd" in src, fn.__name__
    # The probe spans several lines, so slice to the closing paren of the
    # expression rather than the first ")" inside it.
    src = inspect.getsource(routes)
    probe = src.split("_CAN_PIN_DIR = ", 1)[1].split("\n)", 1)[0]
    for op in ("os.open", "os.unlink", "os.rename"):
        assert op in probe, f"{op} is not covered by the pin capability probe"


# ── GPT round-45 findings (#518) ───────────────────────────────────────────────


def test_redirect_state_covers_every_path_the_app_writes():
    """The reported leak: DELETED_PATH was added to the app in the tombstone fix
    but not to _redirect_state, so the deletion tests rewrote the USER's live
    deleted.json and made their real deleted specs discoverable again.

    Enumerated from the module rather than a hand-kept list, so the next file this
    app learns to write fails here instead of in someone's home directory.
    """
    written = {
        n for n, v in vars(routes).items()
        if n.endswith("_PATH") and isinstance(v, Path) and n != "SPEC_STATE_PATH"
    }
    redirected = set(re.findall(r'setattr\(routes, "(\w+_PATH)"', inspect.getsource(_redirect_state)))
    assert written <= redirected, f"not redirected in tests: {sorted(written - redirected)}"


def test_state_guard_watches_the_whole_directory():
    """The guard used to assert one known filename, which is why the second leak
    got through. It now compares the directory listing."""
    src = inspect.getsource(_never_touch_the_real_state)
    assert "_live_state_snapshot()" in src
    assert "_REAL_STATE_DIR" in inspect.getsource(_live_state_snapshot)


def test_state_guard_compares_the_real_dir_not_the_redirect():
    """The captured dir must survive the autouse redirect active right now.

    _REAL_STATE_DIR is captured on first use rather than at import (banned by
    issue #874). The property that makes the guard work is that it holds the
    un-redirected dir even while routes._STATE_DIR points at a tmp dir -- the
    guard re-reads it AFTER its yield, with the redirect still applied. If the
    memoization were ever dropped so it re-resolved live, before == after would
    compare tmp-to-tmp and this app's tests could rewrite real user specs
    undetected, which has already happened twice.
    """
    assert _REAL_STATE_DIR is not None, "the first-use capture did not run"
    assert _REAL_STATE_DIR != routes._STATE_DIR, (
        "the live-state guard is pointed at the redirected tmp dir, so it can no "
        "longer detect a test writing to the user's real state"
    )


# ── GPT round-46 findings (#518) ───────────────────────────────────────────────


def test_slot_key_is_the_deciding_identity(tmp_path, monkeypatch):
    """The reported gap: a spec_dir does not identify a CREATION. Delete leaves the
    documents on disk by design, so re-importing under the same name and path
    produces a different spec with the same spec_dir -- and a stale tab's Pause
    would cancel the replacement's run. The per-creation slot key distinguishes
    them."""
    same_dir = "/p/.kiro/specs/s"
    old_key, new_key = "spec-builder-s-aaaa1111", "spec-builder-s-bbbb2222"

    # Same directory, different creation -> refused on the key alone.
    assert routes._client_identity_mismatch(
        routes._ClientClaim(same_dir, old_key), same_dir, new_key
    ) is True
    # Same creation -> allowed.
    assert routes._client_identity_mismatch(
        routes._ClientClaim(same_dir, new_key), same_dir, new_key
    ) is False
    # A directory mismatch still refuses on its own.
    assert routes._client_identity_mismatch(
        routes._ClientClaim("/p/other", new_key), same_dir, new_key
    ) is True
    # Unpinned stays unpinned: an older tab sends neither field.
    assert routes._client_identity_mismatch(routes._ClientClaim("", ""), same_dir, new_key) is False
    # A server-side entry with no key yet cannot refuse on the key.
    assert routes._client_identity_mismatch(
        routes._ClientClaim(same_dir, old_key), same_dir, ""
    ) is False


@pytest.mark.asyncio
async def test_client_claim_reads_body_and_query(tmp_path, monkeypatch):
    """Both fields travel in the body for POSTs and the query string for DELETE."""
    client = _make_client(monkeypatch, tmp_path)
    seen: list[routes._ClientClaim] = []

    async def _probe(request):
        seen.append(await routes._client_claim(request))
        return web.json_response({"ok": True})

    # Routes must be registered BEFORE the app starts: the router freezes on start.
    client.app.router.add_post("/probe", _probe)
    client.app.router.add_delete("/probe", _probe)
    await client.start_server()
    try:
        await client.post("/probe", json={"spec_dir": "/d", "slot_key": "spec-builder-s-1"})
        await client.delete("/probe?spec_dir=/d&slot_key=spec-builder-s-1")
    finally:
        await client.close()

    assert seen == [routes._ClientClaim("/d", "spec-builder-s-1")] * 2, seen


@pytest.mark.asyncio
async def test_second_handoff_is_refused_while_executing(tmp_path, monkeypatch):
    """The reported duplicate: a second execute queued another prompt on the same
    slot. Pause cancels the running turn, then the queued duplicate drains
    immediately -- so the run the user stopped carried on. The second handoff is
    now refused BEFORE any side effect."""
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "busy"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("turn"))

    class _Loop:
        id = "loop-live"

    class _Svc:
        def get_by_slot(self, key):
            return None

        async def remove(self, loop_id):
            pass

    # The autonudge service must be available: it is checked before the claim (it
    # writes nothing), so an unavailable service would answer 503 first and the
    # duplicate refusal would never be reached.
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    async def _authz(**_kw):
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    for label, index, slot_running in (
        ("indexed status", {"busy": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"),
                                     "status": "executing"}}, False),
        ("live slot", {"busy": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}, True),
    ):
        # The client redirects state, so the index must be written AFTER it exists
        # (the autouse fixture points elsewhere until then).
        client = _make_client(monkeypatch, tmp_path)
        routes._save_index(index)
        slot = types.SimpleNamespace(
            key=routes._slot_key("busy"), _app=routes.APP_NAME, running=slot_running,
            project="", messages=[], _titled=True,
        )

        class _State:
            def get_slot(self, key):
                return slot

            def get_or_create_slot(self, name, app=""):
                return slot

        await client.start_server()
        try:
            client.app["state"] = _State()
            resp = await client.post(f"{_BASE}/specs/busy/execute", json={})
            # Read the body BEFORE closing: the stream dies with the client.
            status, body = resp.status, await resp.json()
        finally:
            await client.close()

        assert status == 409, f"{label}: {status}"
        assert "already building" in body["error"], label
        assert dispatched == [], f"{label}: a duplicate turn was dispatched"


def test_handoff_refuses_before_any_side_effect():
    """Source guard: the already-executing check must precede the slot acquisition
    and the dispatch, or the refusal comes too late to matter."""
    src = inspect.getsource(routes._handle_handoff)
    guard = src.index("already building")
    assert guard < src.index("_ensure_worker_slot("), "the slot is acquired before the refusal"
    assert guard < src.index("_dispatch_turn("), "the turn is dispatched before the refusal"


# ── GPT round-47 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_touch_spec_pins_the_creation_not_just_the_directory(tmp_path, monkeypatch):
    """The reported gap: the re-reading mutators compared spec_dir only. A delete +
    re-import at the same name AND path leaves spec_dir identical, so a stale
    request passed the check and stamped (or dropped) the replacement."""
    _redirect_state(monkeypatch, tmp_path)
    same_dir = str(tmp_path / "p" / ".kiro" / "specs" / "s")
    routes._save_index(
        {"s": {"spec_dir": same_dir, "slot_key": "spec-builder-s-bbbb2222"}}
    )

    # Stale claim: right directory, previous creation -> refused.
    assert (
        await routes._touch_spec(
            "s", expect_spec_dir=same_dir, expect_slot_key="spec-builder-s-aaaa1111",
            status="executing",
        )
        is None
    )
    assert routes._load_index()["s"].get("status") != "executing", "the stale stamp landed"

    # Current creation -> accepted.
    fresh = await routes._touch_spec(
        "s", expect_spec_dir=same_dir, expect_slot_key="spec-builder-s-bbbb2222",
        status="executing",
    )
    assert fresh is not None and fresh["status"] == "executing"

    # An entry predating slot keys carries none and cannot be pinned on it.
    routes._save_index({"old": {"spec_dir": same_dir}})
    assert (
        await routes._touch_spec(
            "old", expect_spec_dir=same_dir, expect_slot_key="spec-builder-old-1",
            status="planning",
        )
        is not None
    )


def test_every_reread_mutation_pins_the_creation():
    """Source guard: each handler that re-reads the index to mutate it must carry
    the slot key it verified, not just the directory."""
    for handler in (
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
    ):
        src = inspect.getsource(handler)
        assert "slot_key" in src, f"{handler.__name__} mutates without pinning the creation"


@pytest.mark.asyncio
async def test_authorization_failure_reverts_the_recorded_execution_state(tmp_path, monkeypatch):
    """The reported ordering defect: the loop was armed BEFORE the execution state
    was recorded, so a shutdown between the two persisted a shielded timer with no
    execution state -- and the restored timer ran something Pause could not stop,
    because Pause keys off that state. Recording first inverts the failure, which
    means a refused authorization must now revert what was recorded."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "nope"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {"nope": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"),
                  "slot_key": "spec-builder-nope-1234abcd"}}
    )
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _Svc:
        def get_by_slot(self, key):
            return None

        async def remove(self, loop_id):
            pass

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    async def _refuse(**_kw):
        return None, "slot is owned by another app", 403

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _refuse)

    slot = types.SimpleNamespace(
        key=routes._slot_key("nope"), _app=routes.APP_NAME, running=False,
        project="", messages=[], _titled=True, task=None,
    )
    slots: dict = {}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            slots[slot.key] = slot
            return slot

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(f"{_BASE}/specs/nope/execute", json={})
        status = resp.status
    finally:
        await client.close()

    assert status == 403
    assert dispatched == [], "a turn was dispatched despite refused authorization"
    entry = routes._load_index()["nope"]
    assert entry.get("status") != "executing", "the spec is left claiming to be executing"
    assert slot.key not in slots, "the worker slot was left behind"


@pytest.mark.asyncio
async def test_deletion_during_authorization_removes_the_armed_loop(tmp_path, monkeypatch):
    """Recording before arming moved one window: a DELETE landing during the arm
    tears down the loops it can see BY NAME, and ours arrives after. The post-arm
    re-verification catches that and removes it."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "gone"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    routes._save_index(
        {"gone": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )
    removed: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("x"))

    class _Loop:
        id = "loop-armed"

    class _Svc:
        def __init__(self):
            self.armed = False

        def get_by_slot(self, key):
            return _Loop() if self.armed else None

        async def remove(self, loop_id):
            self.armed = False
            removed.append(loop_id)

    svc = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: svc)

    async def _authz(**_kw):
        # The spec is deleted while authorization is in flight.
        routes._save_index({})
        svc.armed = True
        return _Loop(), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authz)

    slot = types.SimpleNamespace(
        key=routes._slot_key("gone"), _app=routes.APP_NAME, running=False,
        project="", messages=[], _titled=True, task=None,
    )
    slots: dict = {}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            slots[slot.key] = slot
            return slot

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(f"{_BASE}/specs/gone/execute", json={})
        status = resp.status
    finally:
        await client.close()

    assert status == 409
    assert dispatched == [], "a turn was dispatched for a deleted spec"
    assert removed == ["loop-armed"], f"the armed loop was left nudging: {removed}"
    assert slot.key not in slots, "the worker slot was left behind"


# ── GPT round-48 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_polling_does_not_reconcile_away_the_arming_window(tmp_path, monkeypatch):
    """The reported consequence of recording state before arming: between those two
    steps there is legitimately no loop and no running turn, so a detail/list poll
    landing in that window reconciled "executing" back to "planning" -- which hid
    Pause for the whole run that followed. The arming marker exempts exactly that
    window, and only while it is fresh."""
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _Idle:
        running = False

    spec_dir = str(tmp_path / "p" / ".kiro" / "specs" / "arming")

    # Arming right now: no loop yet, but the state must stand.
    meta = {"spec_dir": spec_dir, "status": "executing", "exec_arming_at": time.time()}
    routes._save_index({"arming": dict(meta)})
    assert await routes._effective_status("arming", meta, _Idle()) == "executing"
    assert routes._load_index()["arming"]["status"] == "executing", "the poll cleared it"

    # A stale marker must NOT mask the reconciliation forever: a process that died
    # mid-arm would otherwise leave the spec building with nothing running.
    stale = {
        "spec_dir": spec_dir,
        "status": "executing",
        "exec_arming_at": time.time() - (routes._ARMING_GRACE_SECS + 5),
    }
    routes._save_index({"arming": dict(stale)})
    assert await routes._effective_status("arming", stale, _Idle()) == "planning"

    # A non-numeric marker is ignored rather than raising.
    junk = {"spec_dir": spec_dir, "status": "executing", "exec_arming_at": "soon"}
    routes._save_index({"arming": dict(junk)})
    assert await routes._effective_status("arming", junk, _Idle()) == "planning"


def test_handoff_stamps_and_clears_the_arming_marker():
    """Source guard: the marker is set by the pre-arm commit and cleared once the
    loop exists, so the exemption lasts for the arming window and no longer."""
    # The stamp is part of the atomic claim; the clear is in the handler, after the
    # loop exists.
    assert 'meta["exec_arming_at"] = now' in inspect.getsource(routes._claim_execution), (
        "the claim no longer marks the pre-arm window"
    )
    src = inspect.getsource(routes._handle_handoff)
    claim = src.index("await _claim_execution(")
    arm = src.index("await authorize_and_add_nudge(")
    clear = src.index("exec_arming_at=0.0", arm)
    assert claim < arm < clear, "the marker does not bracket the arm"


# ── GPT round-49 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_execute_claims_are_serialized(tmp_path, monkeypatch):
    """The reported hole: reading the status and committing it in a separate step is
    not a guard. Two concurrent execute requests both read "planning", both pass,
    and both dispatch -- Pause then cancels one prompt while the other drains and
    keeps editing the user's files. Exactly one caller may win the claim."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "p" / ".kiro" / "specs" / "race")
    routes._save_index({"race": {"spec_dir": spec_dir, "slot_key": "spec-builder-race-1"}})
    monkeypatch.setattr(routes, "_exec_loop_active", lambda _n: False)

    results = await asyncio.gather(
        *[
            routes._claim_execution(
                "race",
                expect_spec_dir=spec_dir,
                expect_slot_key="spec-builder-race-1",
                live_running=False,
            )
            for _ in range(8)
        ]
    )
    winners = [r for r, _entry in results if r == routes._CLAIM_OK]
    assert len(winners) == 1, f"{len(winners)} callers claimed the same run"
    assert all(r == routes._CLAIM_TAKEN for r, _e in results if r != routes._CLAIM_OK)
    assert routes._load_index()["race"]["status"] == "executing"


@pytest.mark.asyncio
async def test_claim_refuses_a_different_creation(tmp_path, monkeypatch):
    """The claim carries identity for the same reason every other mutation does: a
    delete + re-import at the same name AND path is a different spec."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "p" / ".kiro" / "specs" / "s")
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": "spec-builder-s-new"}})
    monkeypatch.setattr(routes, "_exec_loop_active", lambda _n: False)

    reason, _entry = await routes._claim_execution(
        "s", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-old", live_running=False
    )
    assert reason == routes._CLAIM_GONE
    assert routes._load_index()["s"].get("status") != "executing"

    # A live turn on the slot counts as taken even when the index says planning.
    reason, _entry = await routes._claim_execution(
        "s", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-new", live_running=True
    )
    assert reason == routes._CLAIM_TAKEN


@pytest.mark.asyncio
async def test_delete_tombstones_before_dropping_the_entry(tmp_path, monkeypatch):
    """The reported window: the tombstone was written AFTER the session teardown, so
    between the entry being dropped and the tombstone landing the documents were on
    disk and untombstoned -- a list poll re-adopted them through discovery and the
    DELETE returned 200 with the spec still listed."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "bye"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    routes._save_index(
        {"bye": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )
    order: list[str] = []
    real_remember = routes._remember_deleted

    def _spy(d):
        order.append("tombstone")
        real_remember(d)

    monkeypatch.setattr(routes, "_remember_deleted", _spy)

    original_mutate = routes._mutate_index

    async def _watched(fn):
        result = await original_mutate(fn)
        order.append("pop" if result else "pop-failed")
        return result

    monkeypatch.setattr(routes, "_mutate_index", _watched)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/bye")
        status = resp.status
    finally:
        await client.close()

    assert status == 200
    assert order[:2] == ["tombstone", "pop"], f"the entry was dropped untombstoned: {order}"
    assert str(spec_dir) in routes._load_deleted(), "the directory is not tombstoned"


@pytest.mark.asyncio
async def test_failed_delete_clears_the_tombstone(tmp_path, monkeypatch):
    """Tombstoning first means every path that does NOT delete has to clear it --
    otherwise a spec that was never deleted stays suppressed from discovery."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "stay"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# r")
    routes._save_index(
        {"stay": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    async def _teardown_fails(*_a, **_kw):
        return False

    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown_fails)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/stay")
        status = resp.status
    finally:
        await client.close()

    assert status == 503
    assert "stay" in routes._load_index(), "the spec was not restored"
    assert str(spec_dir) not in routes._load_deleted(), (
        "a spec that still exists is tombstoned; discovery will hide its documents"
    )


def test_delete_orders_the_tombstone_before_the_pop():
    """Source guard: the tombstone write must precede the index pop, and every
    non-deleting arm must clear it."""
    src = inspect.getsource(routes._handle_delete)
    remember = src.index("_remember_deleted")
    pop = src.index("_mutate_index(_pop_if_same)")
    assert remember < pop, "the entry is dropped before the directory is tombstoned"
    assert src.count("_forget_deleted") >= 2, (
        "a non-deleting arm leaves the tombstone behind"
    )


# ── GPT round-50 findings (#518) ───────────────────────────────────────────────


def test_concurrent_tombstone_writes_do_not_lose_deletions(tmp_path, monkeypatch):
    """The reported race: _remember_deleted read the list, appended, and wrote it
    back without holding the lock. Two concurrent deletes both read the
    pre-existing list, so the second write dropped the first spec's tombstone --
    and that spec was rediscovered and reappeared after the user deleted it.

    Interleaves the transactions deliberately: each writer is parked between its
    read and its write, which is exactly the window the lock has to close."""
    _redirect_state(monkeypatch, tmp_path)
    routes._remember_deleted("/p/keep-me")

    real_load = routes._load_deleted
    parked = threading.Barrier(3, timeout=10)

    def _slow_load():
        current = real_load()
        # Every writer waits here until both have read, then they race to write.
        try:
            parked.wait()
        except threading.BrokenBarrierError:
            pass
        return current

    monkeypatch.setattr(routes, "_load_deleted", _slow_load)

    threads = [
        threading.Thread(target=routes._remember_deleted, args=(f"/p/spec-{i}",))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    try:
        parked.wait()
    except threading.BrokenBarrierError:
        pass
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "a tombstone write deadlocked"

    monkeypatch.setattr(routes, "_load_deleted", real_load)
    recorded = routes._load_deleted()
    assert "/p/spec-0" in recorded and "/p/spec-1" in recorded, (
        f"a concurrent delete lost its tombstone: {recorded}"
    )
    assert "/p/keep-me" in recorded, "an unrelated tombstone was dropped"


def test_both_tombstone_transactions_hold_the_lock():
    """Source guard: read and write must be inside the same critical section. A
    lock taken around the write alone still lets two readers see the same list."""
    for fn in (routes._remember_deleted, routes._forget_deleted):
        src = inspect.getsource(fn)
        assert "with _INDEX_LOCK:" in src, f"{fn.__name__} mutates the tombstones unlocked"
        held = src.index("with _INDEX_LOCK:")
        assert held < src.index("_load_deleted()"), f"{fn.__name__} reads before locking"
        assert held < src.index("atomic_write("), f"{fn.__name__} writes outside the lock"


def test_every_error_response_carries_a_machine_readable_code():
    """The repo's error-code contract (see test/test_error_code_contract.py): the
    dashboard renders `error` prose verbatim into a localized UI, so the `code` is
    the contract and the prose is advisory. This app-local guard keeps the file at
    zero rather than relying on the repo-wide ratchet, which only fails when the
    per-file count grows."""
    src = Path(routes.__file__).read_text(encoding="utf-8")
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


# ── GPT round-51 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_abort_does_not_drop_a_replacement_spec(tmp_path, monkeypatch):
    """The reported gap: the create path's two identity checks compared spec_dir
    only. A delete plus a re-import at the same name AND path during slot setup
    leaves spec_dir identical, so a stale create either removed the replacement's
    index entry on its abort path or drove the replacement's agent with its own seed
    prompt. The per-creation slot key is what separates them."""
    client = _make_client(monkeypatch, tmp_path)
    working = tmp_path / "wd"
    spec_dir = working / ".kiro" / "specs" / "reused"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# adopted")
    seeded: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: seeded.append("seed"))

    # A REPLACEMENT spec occupies the name at the same path, with its own creation.
    replacement = {
        "working_dir": str(working),
        "spec_dir": str(spec_dir),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": "spec-builder-reused-99999999",
    }

    slot = types.SimpleNamespace(
        key="spec-builder-reused-99999999", _app=routes.APP_NAME, running=False,
        project="", messages=[], _titled=True, task=None,
    )

    class _State:
        def get_slot(self, key):
            return slot

        def get_or_create_slot(self, name, app=""):
            return slot

    real_ensure = routes._ensure_worker_slot

    async def _ensure_then_replace(state, name, meta, **kw):
        got = await real_ensure(state, name, meta, **kw)
        # The delete + re-import lands while the slot is being set up.
        routes._save_index({"reused": dict(replacement)})
        return got

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_then_replace)

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "reused", "working_dir": str(working), "spec_type": "feature",
                  "import_existing": True},
        )
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 409, f"{status}: {body}"
    assert body["code"] == "spec_changed_during_create"
    assert seeded == [], "the stale create seeded the replacement spec's agent"
    # And the replacement's entry survived the abort.
    entry = routes._load_index().get("reused")
    assert entry is not None, "the abort deleted the replacement spec's index entry"
    assert entry["slot_key"] == "spec-builder-reused-99999999"


def test_create_identity_checks_pin_the_creation():
    """Source guard: both create-path checks compare the slot key, not just the
    directory."""
    src = inspect.getsource(routes._handle_create)
    pop = src.index("def _pop_if_ours(")
    pop_body = src[pop:src.index("del idx[name]", pop)]
    assert "slot_key" in pop_body, "the unwind pops on the directory alone"
    post = src.index("live = current.get(name) or {}")
    assert "slot_key" in src[post:src.index("_unwind_create()", post)], (
        "the post-slot-setup check compares the directory alone"
    )


# ── GPT round-52 findings (#518) ───────────────────────────────────────────────


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
    # _usable_name drop the entry at load (round 57), testing nothing about egress.
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

    await client.start_server()
    try:
        client.app["state"] = None
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
    routes._save_index(
        {"leaky": {**routes._load_index()["leaky"], "spec_type": "feature/../etc"}}
    )
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

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.get(f"{_BASE}/specs")
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 200, f"the listing crashed on a malformed timestamp: {status}"
    names = [s["name"] for s in body["specs"]]
    assert set(names) == {"numeric", "stringy", "junk", "missing"}
    # Usable timestamps still order the list; unusable ones sort last rather than raise.
    assert names[0] == "missing" and names[1] == "numeric", names


# ── GPT round-53 findings (#518) ───────────────────────────────────────────────


def test_sentinel_helpers_fail_closed_without_directory_pinning(tmp_path, monkeypatch):
    """The reported vector: without dir_fd pinning the fallback resolved the sentinel
    BY PATH, so an agent that swapped its own spec directory for a junction between
    the verification and the write landed STOP in another spec -- halting an unrelated
    run. The clear path had the same shape, and could delete another spec's STOP file,
    letting that run resume. Both now do nothing at all."""
    spec_dir = tmp_path / "p" / ".kiro" / "specs" / "pinned"
    spec_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "_CAN_PIN_DIR", False)

    assert routes._write_stop_sentinel(spec_dir) is False, "wrote without pinning"
    assert not (spec_dir / routes._STOP_FILE).exists(), "a STOP file was created by path"
    assert list(spec_dir.iterdir()) == [], f"left a temp file behind: {list(spec_dir.iterdir())}"

    # A pre-existing sentinel is LEFT ALONE rather than unlinked through a path that
    # could have been redirected.
    (spec_dir / routes._STOP_FILE).write_text("1")
    routes._clear_stop_sentinel(spec_dir)
    assert (spec_dir / routes._STOP_FILE).exists(), "cleared by path without pinning"

    # With pinning the same calls still work, so the guard is conditional, not a
    # blanket disable.
    monkeypatch.setattr(routes, "_CAN_PIN_DIR", True)
    routes._clear_stop_sentinel(spec_dir)
    assert not (spec_dir / routes._STOP_FILE).exists(), "pinned clear did nothing"
    assert routes._write_stop_sentinel(spec_dir) is True
    assert (spec_dir / routes._STOP_FILE).is_file()


def test_no_sentinel_path_is_resolved_by_string():
    """Source guard: every sentinel filesystem call is relative to a pinned
    descriptor. Asserting on the CALLS, not on the comment: a `dir_fd=` keyword on
    each mutating call is the property that makes the swap unreachable."""
    for fn in (routes._write_stop_sentinel, routes._clear_stop_sentinel):
        src = inspect.getsource(fn)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "_STOP_FILE" not in stripped:
                continue
            if "os.open(" in stripped or "os.unlink(" in stripped or "os.replace(" in stripped:
                assert "dir_fd" in stripped, f"{fn.__name__} touches the sentinel by path: {stripped}"
        # And no path arithmetic builds a sentinel target any more.
        assert "real_dir / _STOP_FILE" not in src, f"{fn.__name__} still joins the path"


@pytest.mark.asyncio
async def test_halt_still_stops_the_run_without_a_sentinel(tmp_path, monkeypatch):
    """Failing closed must not weaken Pause: the loop removal and the turn cancel are
    the authoritative stops, and both have to happen even when no sentinel could be
    written."""
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_write_stop_sentinel", lambda _d: False)
    removed: list[str] = []
    halted: list[str] = []

    async def _remove(name, **kw):
        removed.append(name)

    async def _halt(state, name, **kw):
        halted.append(name)
        return True

    monkeypatch.setattr(routes, "_remove_nudge_loop", _remove)
    monkeypatch.setattr(routes, "_halt_active_turn", _halt)

    await routes._halt_execution(
        None, "quiet", tmp_path / "p" / ".kiro" / "specs" / "quiet", reason="user stop"
    )
    assert removed == ["quiet"], "the nudge loop was not removed"
    assert halted == ["quiet"], "the in-flight turn was not cancelled"


# ── GPT round-54 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_git_refuses_when_only_the_durable_audit_write_fails(tmp_path, monkeypatch):
    """The reported gap in round 42's gate: the default log path ENQUEUES the event
    and a background writer flushes it, so `_audit_tool` returning True proved only
    that the enqueue did not raise -- the record could still be dropped when the log
    was unwritable, and git ran unaudited. The invocation event is now written with
    `critical=True`, which raises on a filesystem failure.

    The stub models exactly that asymmetry: a queued (non-critical) call succeeds, a
    critical one raises. A gate that never asked for durability would pass."""
    spawned: list[list[str]] = []
    calls: list[dict] = []

    def _spawn_prep(argv):
        spawned.append(argv)
        raise AssertionError("git was spawned without a durable audit record")

    class _Sel:
        def log_tool_invocation(self, **kw):
            calls.append(kw)
            if kw.get("critical"):
                raise OSError("audit log is unwritable")

    monkeypatch.setattr(routes, "_prepare_git_spawn", _spawn_prep)
    monkeypatch.setattr(routes, "sel", lambda: _Sel())

    rc, _out, err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")

    assert rc == routes._GIT_UNAVAILABLE, rc
    assert "audit" in err
    assert spawned == [], "git ran despite the audit write failing"
    assert calls and calls[0]["critical"] is True, (
        f"the invocation audit did not ask for a durable write: {calls}"
    )


def test_only_the_invocation_audit_is_critical():
    """Source guard on the calls: the precondition event is durable, the outcome
    events stay queued. Making the outcomes critical would let a failed log write
    turn a command that already ran into an error."""
    src = inspect.getsource(routes._git)
    critical_lines = [ln.strip() for ln in src.splitlines() if "critical=True" in ln]
    assert len(critical_lines) == 1, f"expected exactly one critical audit: {critical_lines}"
    assert '"invoked"' in critical_lines[0], critical_lines[0]
    # And it is awaited off the loop, because a critical write is synchronous I/O.
    assert "asyncio.to_thread(_audit_tool" in critical_lines[0], critical_lines[0]


def test_audit_helper_defaults_to_queued():
    """`_audit_tool` must keep its non-critical default: every other caller of it is
    an outcome event, and flipping the default would make them all blocking."""
    sig = inspect.signature(routes._audit_tool)
    assert sig.parameters["critical"].default is False


# ── GPT round-55 findings (#518) ───────────────────────────────────────────────


def test_a_spec_cannot_claim_another_specs_slot_key(tmp_path, monkeypatch):
    """The reported aliasing: index.json is agent-writable, and the filter accepted
    any key matching the generic grammar -- so an entry could carry ANOTHER spec's
    valid key, and `_ensure_worker_slot` would then adopt that spec's live session,
    delivering this spec's messages and approval cards into the other conversation.
    A key is only honoured for the entry whose name it encodes."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index(
        {
            "alpha": {"spec_dir": "/p/alpha", "slot_key": "spec-builder-alpha-1234abcd"},
            # Grammar-valid, but it names alpha -- the aliasing attempt.
            "beta": {"spec_dir": "/p/beta", "slot_key": "spec-builder-alpha-1234abcd"},
            # Legacy name-derived key stays honoured for its OWN spec.
            "gamma": {"spec_dir": "/p/gamma", "slot_key": "spec-builder-gamma"},
        }
    )
    routes._load_index()

    assert routes._slot_key("alpha") == "spec-builder-alpha-1234abcd"
    assert routes._slot_key("beta") == "spec-builder-beta", "beta aliased alpha's session"
    assert routes._slot_key("gamma") == "spec-builder-gamma"


def test_slot_key_ownership_rules():
    """The predicate itself: per-creation form, legacy form, and the shapes that a
    hand-edited index could otherwise smuggle past the grammar."""
    assert routes._owns_slot_key("s", "spec-builder-s-0123abcd") is True
    assert routes._owns_slot_key("s", "spec-builder-s") is True
    # Another spec's key, a prefix collision, a non-hex suffix, wrong suffix width.
    assert routes._owns_slot_key("s", "spec-builder-other-0123abcd") is False
    assert routes._owns_slot_key("s", "spec-builder-s2-0123abcd") is False
    assert routes._owns_slot_key("s", "spec-builder-s-ZZZZZZZZ") is False
    assert routes._owns_slot_key("s", "spec-builder-s-0123abc") is False
    assert routes._owns_slot_key("s", "spec-builder-s-0123abcd-extra") is False
    # A name that is not a valid spec name cannot own anything.
    assert routes._owns_slot_key("../evil", "spec-builder-../evil") is False
    # And every key this app MINTS satisfies its own rule.
    minted = routes._new_slot_key("demo")
    assert routes._owns_slot_key("demo", minted) is True, minted


@pytest.mark.asyncio
async def test_timestamps_are_validated_on_egress(tmp_path, monkeypatch):
    """The reported leak: round 52 redacted the index's STRING fields but left
    created_at/updated_at as whatever the agent-writable index held, so a credential
    parked in a timestamp reached the dashboard verbatim."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "stamped").mkdir(parents=True)
    routes._save_index(
        {
            "stamped": {
                "spec_dir": str(base / "stamped"),
                "working_dir": str(tmp_path / "wd"),
                "created_at": "AKIAIOSFODNN7EXAMPLE",
                "updated_at": {"nested": "junk"},
            }
        }
    )

    await client.start_server()
    try:
        client.app["state"] = None
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


# ── GPT round-56 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_archive_restores_the_original_name_and_key(tmp_path, monkeypatch):
    """The reported severance: round 43 popped the entry and, if the name had been
    taken while archival ran, restored it as `<name>-2`. Round 55 then bound slot keys
    to their entry's own name, so the renamed entry could no longer own its
    per-creation key -- `_slot_key` fell back to the name-derived form and the original
    conversation became unreachable. The name is now RESERVED for the whole teardown,
    so a failure puts the spec back exactly as it was."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keeper"
    spec_dir.mkdir(parents=True)
    key = "spec-builder-keeper-abcd1234"
    routes._save_index(
        {"keeper": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"),
                    "slot_key": key}}
    )

    async def _archive_fails(*_a, **_kw):
        return False

    monkeypatch.setattr(routes, "_teardown_worker_slot", _archive_fails)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/keeper")
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503
    assert body["code"] == "archive_failed"
    assert "nothing was deleted" in body["error"]
    # No rename: the spec is back under its own name, with its own key, and the
    # conversation therefore still resolves.
    index = routes._load_index()
    assert set(index) == {"keeper"}, f"the spec was renamed or lost: {list(index)}"
    assert index["keeper"]["slot_key"] == key
    assert routes._DELETING not in index["keeper"], "the reservation was left behind"
    routes._load_index()
    assert routes._slot_key("keeper") == key, "the original conversation is unreachable"
    assert str(spec_dir) not in routes._load_deleted(), "a live spec is tombstoned"


@pytest.mark.asyncio
async def test_a_reserved_name_cannot_be_taken_mid_delete(tmp_path, monkeypatch):
    """The reservation is what makes the rename unnecessary: while the delete runs the
    entry is still present, so create refuses the name instead of racing into it."""
    client = _make_client(monkeypatch, tmp_path)
    working = tmp_path / "wd"
    spec_dir = working / ".kiro" / "specs" / "busy"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {"busy": {"spec_dir": str(spec_dir), "working_dir": str(working),
                  "slot_key": "spec-builder-busy-11112222"}}
    )
    # Mark the entry as a delete in flight, then try to create the same name.
    assert await routes._mark_deleting(
        "busy", expect_spec_dir=str(spec_dir), expect_slot_key="spec-builder-busy-11112222"
    )

    await client.start_server()
    try:
        client.app["state"] = None
        listed = await (await client.get(f"{_BASE}/specs")).json()
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "busy", "working_dir": str(working), "spec_type": "feature"},
        )
        status = resp.status
    finally:
        await client.close()

    assert [s["name"] for s in listed["specs"]] == [], "a delete in flight is still listed"
    assert status == 409, f"the reserved name was taken: {status}"


@pytest.mark.asyncio
async def test_removal_failure_keeps_the_spec_hidden_for_a_retry(tmp_path, monkeypatch):
    """If the final index write fails the conversation is ALREADY archived, so
    un-deleting would be the lie the ordering exists to prevent. The reservation stays
    (spec hidden), and the 503 asks for a retry."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "stuck"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {"stuck": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    async def _ok_teardown(*_a, **_kw):
        return True

    real_mutate = routes._mutate_index
    calls = {"n": 0}

    async def _fail_the_removal(fn):
        calls["n"] += 1
        # The mark succeeds; the removal (second mutation) does not.
        if calls["n"] >= 2:
            return False
        return await real_mutate(fn)

    monkeypatch.setattr(routes, "_teardown_worker_slot", _ok_teardown)
    monkeypatch.setattr(routes, "_mutate_index", _fail_the_removal)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/stuck")
        status, body = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503
    assert body["code"] == "index_write_failed"
    assert "retry" in body["error"]
    monkeypatch.setattr(routes, "_mutate_index", real_mutate)
    entry = routes._load_index()["stuck"]
    assert entry.get(routes._DELETING), "the reservation was dropped, un-hiding the spec"


# ── GPT round-57 findings (#518) ───────────────────────────────────────────────


def test_index_keys_that_cannot_be_served_are_dropped_at_load(tmp_path, monkeypatch):
    """The reported egress path: a spec NAME is an index key, index.json is
    agent-writable, and `GET /specs` returns the key as `"name"` -- so a credential
    parked in the key reached the dashboard verbatim. Such an entry is dropped at load
    rather than scrubbed: a scrubbed name would no longer match the directory the
    entry points at."""
    _redirect_state(monkeypatch, tmp_path)
    # A real AWS-key shape satisfies the name grammar, which is why the grammar alone
    # was not enough -- _redact is what recognises it.
    credential = "AKIAIOSFODNN7EXAMPLE"
    assert routes._valid_name(credential), "precondition: the grammar accepts this"
    assert routes._redact(credential) != credential, "precondition: _redact rewrites it"

    routes._save_index(
        {
            "good": {"spec_dir": "/p/good"},
            credential: {"spec_dir": "/p/leak"},
            "../escape": {"spec_dir": "/p/escape"},
            "": {"spec_dir": "/p/empty"},
        }
    )
    loaded = routes._load_index()
    assert set(loaded) == {"good"}, f"an unusable key survived: {sorted(loaded)}"


def test_usable_name_requires_grammar_and_redaction_stability():
    """The predicate: a key must pass the same grammar create enforces AND survive
    _redact unchanged."""
    assert routes._usable_name("my-spec_2") is True
    assert routes._usable_name("AKIAIOSFODNN7EXAMPLE") is False
    assert routes._usable_name("../escape") is False
    assert routes._usable_name("with space") is False
    assert routes._usable_name("") is False


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

    injected = "planning AKIAIOSFODNN7EXAMPLE"
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
        {"odd": {"spec_dir": str(base / "odd"), "working_dir": str(tmp_path / "wd"),
                 "status": "AKIAIOSFODNN7EXAMPLE"}}
    )

    await client.start_server()
    try:
        client.app["state"] = None
        body = await (await client.get(f"{_BASE}/specs")).json()
    finally:
        await client.close()

    assert [s["status"] for s in body["specs"]] == ["planning"], body["specs"]


# ── GPT round-58 findings (#518) ───────────────────────────────────────────────


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

    await client.start_server()
    try:
        client.app["state"] = None
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


@pytest.mark.asyncio
async def test_cancelled_git_is_killed_before_the_handler_unwinds(tmp_path, monkeypatch):
    """The reported leak: only `await proc.communicate()` tied git's lifetime to the
    request. Cancel the handler mid-spawn and git ran on -- `worktree add` would
    create a worktree and a branch after the request that asked for it was gone."""
    client = _make_client(monkeypatch, tmp_path)
    killed: list[str] = []
    parked = asyncio.Event()

    class _HangingProc:
        returncode = None

        async def communicate(self):
            # Signal from INSIDE the await the cancellation has to land on, rather
            # than letting the test guess with a sleep. _git makes two to_thread
            # hops before this point and _prepare_git_spawn forks a sandbox probe
            # on first use, so any fixed delay races them: under load the cancel
            # arrives before the spawn, proc is still None, and the test asserts
            # against a teardown that never had a process to kill.
            parked.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        def kill(self):
            killed.append("kill")
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def _fake_spawn(*argv, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(routes, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(routes, "_audit_tool", lambda *a, **k: True)
    # Also stub the sandbox preparation. _git makes TWO to_thread hops before the
    # spawn, and this one really probes the sandbox host and writes a scrubbed-env
    # temp file -- work that can fork. Left real, it sits inside the window the
    # wait below bounds, so on a heavily parallel shard the test failed on the
    # timeout rather than on the behaviour it exists to check. The assertion is
    # only about the process being killed, so sandbox preparation is setup here,
    # not subject: stubbing it makes the window depend on scheduling alone.
    monkeypatch.setattr(
        routes, "_prepare_git_spawn", lambda argv: (list(argv), {}, None)
    )

    await client.start_server()
    try:
        task = asyncio.ensure_future(routes._git(str(tmp_path), "worktree", "add", "x"))
        await asyncio.wait_for(parked.wait(), timeout=30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.close()

    assert killed == ["kill"], "the spawned git process outlived the cancelled request"


@pytest.mark.asyncio
async def test_halt_git_tolerates_an_already_dead_process(tmp_path):
    """The teardown runs on every exceptional exit, including ones where the process
    never started or has already been reaped -- it must not raise there and mask the
    original exception."""

    class _Gone:
        returncode = None

        def kill(self):
            raise ProcessLookupError()

        async def wait(self):  # pragma: no cover - must not be reached
            raise AssertionError("waited on a process that was already gone")

    await routes._halt_git(None, "status")  # never spawned
    await routes._halt_git(_Gone(), "status")  # died between the check and the kill

    class _Reaped:
        returncode = 0

        def kill(self):  # pragma: no cover - must not be reached
            raise AssertionError("killed an already-reaped process")

        async def wait(self):  # pragma: no cover
            raise AssertionError("waited on an already-reaped process")

    await routes._halt_git(_Reaped(), "status")


def test_git_kills_the_process_on_every_exceptional_exit():
    """Source guard: the teardown is wired into the arm that re-raises, so a future
    exception class added to _git cannot silently reintroduce the orphan. Asserts on
    the CALL, not on a comment."""
    tree = ast.parse(inspect.getsource(routes))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_git"
    )
    reraise_arms = [
        handler
        for handler in (
            h for t in ast.walk(target) if isinstance(t, ast.Try) for h in t.handlers
        )
        if any(isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(handler))
    ]
    assert reraise_arms, "_git no longer re-raises; this guard needs rewriting"
    for arm in reraise_arms:
        calls = [
            n.func.id
            for n in ast.walk(arm)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "_halt_git" in calls, (
            "an arm of _git re-raises without calling _halt_git: a cancelled "
            "worktree add would leave git running against the user's repository"
        )


# ── GPT round-59 findings (#518) ───────────────────────────────────────────────


def _slot_stub():
    """A slot + state pair shaped like the ones _ensure_worker_slot expects."""

    class _Slot:
        key = "spec-builder-s"
        running = False
        messages: list = []
        _app = ""
        project = ""
        _titled = False

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return slot

    return _State(), slot


@pytest.mark.asyncio
async def test_index_entry_without_a_working_dir_is_refused_a_slot(tmp_path, monkeypatch):
    """The reported escape: the refusal was gated on `wd` being truthy, so DELETING
    working_dir from the agent-writable index skipped it entirely. The slot then
    came back with no project, and chat_runner passes cwd=slot.project -- so the
    worker's CLI inherited the GATEWAY's working directory and would run approved
    relative tools from there. Absent must refuse exactly like invalid."""
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "s").mkdir(parents=True)
    meta = {
        "spec_dir": str(base / "s"),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": routes._new_slot_key("s"),
    }
    assert "working_dir" not in meta
    # The entry still passes the load filter, which is deliberate: it is the slot
    # chokepoint that must refuse, not the index reader.
    assert routes._entry_is_usable(meta)

    state, slot = _slot_stub()
    assert await routes._ensure_worker_slot(state, "s", meta) is None, (
        "a projectless entry was handed a slot"
    )
    assert slot.project == "", "the slot was scoped from a missing working_dir"


@pytest.mark.asyncio
async def test_working_dir_absent_and_invalid_refuse_identically(tmp_path, monkeypatch):
    """Both arms of the same class, so a future edit cannot re-split them."""
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "s").mkdir(parents=True)
    (base / "s" / "requirements.md").write_text("# r")
    common = {
        "spec_dir": str(base / "s"),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": routes._new_slot_key("s"),
    }

    for label, meta in (
        ("absent", dict(common)),
        ("empty", {**common, "working_dir": ""}),
        ("nonexistent", {**common, "working_dir": str(tmp_path / "gone")}),
        ("not-a-dir", {**common, "working_dir": str(base / "s" / "requirements.md")}),
    ):
        state, _ = _slot_stub()
        assert await routes._ensure_worker_slot(state, "s", meta) is None, (
            f"{label} working_dir was allowed to produce a slot"
        )

    # ...and a real one still works, so the guard is not vacuous.
    state, slot = _slot_stub()
    ok = {**common, "working_dir": str(tmp_path / "wd")}
    assert await routes._ensure_worker_slot(state, "s", ok) is not None
    assert slot.project == str(tmp_path / "wd")


def test_slot_scoping_never_gates_its_working_dir_check_on_presence():
    """Source guard: the refusal must not be conditioned on the value being
    non-empty, which is the shape that let a deleted key through. Asserts on the
    CONDITION, not on a comment."""
    src = inspect.getsource(routes._ensure_worker_slot)
    assert "if safe_wd is None:" in src, "the unconditional refusal is gone"
    assert "if wd and safe_wd is None:" not in src, (
        "the working_dir refusal is gated on presence again: deleting the key from "
        "the index would yield an unscoped slot running in the gateway's cwd"
    )


# ── GPT round-60 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_touch_spec_refuses_an_entry_reserved_for_deletion(tmp_path, monkeypatch):
    """The reported race: _DELETING was honoured only by the list filter, so a
    message landing mid-delete stamped the doomed entry and got a non-None return --
    which every caller reads as "the spec is live" -- then dispatched a turn. The
    agent kept editing the user's files after DELETE returned 200."""
    _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "doomed").mkdir(parents=True)
    sd = str(base / "doomed")
    routes._save_index({
        "doomed": {
            "spec_dir": sd,
            "working_dir": str(tmp_path / "wd"),
            "spec_type": "feature",
            "status": "planning",
            "slot_key": routes._new_slot_key("doomed"),
        }
    })

    # Live: the stamp lands.
    assert await routes._touch_spec("doomed", expect_spec_dir=sd, status="planning") is not None

    assert await routes._mark_deleting("doomed", expect_spec_dir=sd, expect_slot_key="")
    # Reserved: every mutation through the chokepoint is now refused.
    assert await routes._touch_spec("doomed", expect_spec_dir=sd, status="executing") is None
    assert await routes._touch_spec("doomed") is None

    # Releasing the reservation restores it, so the refusal is not permanent.
    assert await routes._unmark_deleting("doomed", expect_spec_dir=sd)
    assert await routes._touch_spec("doomed", expect_spec_dir=sd) is not None


@pytest.mark.asyncio
async def test_message_during_delete_is_refused_not_dispatched(tmp_path, monkeypatch):
    """End to end through the HTTP surface: with the delete reservation held, POST
    /message must 409 and dispatch NOTHING."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "doomed").mkdir(parents=True)
    sd = str(base / "doomed")
    routes._save_index({
        "doomed": {
            "spec_dir": sd,
            "working_dir": str(tmp_path / "wd"),
            "spec_type": "feature",
            "status": "planning",
            "slot_key": routes._new_slot_key("doomed"),
        }
    })
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a))
    assert await routes._mark_deleting("doomed", expect_spec_dir=sd, expect_slot_key="")

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.post(f"{_BASE}/specs/doomed/message", json={"text": "keep editing"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, body
    assert body.get("code") == "stale_client", body
    assert dispatched == [], "a turn was dispatched into a spec being deleted"


def test_delete_reserves_before_it_captures_the_runtime():
    """Source guard: the reservation must precede the slot/loop capture. Capturing
    first left a window where a message could materialize a NEW slot that the
    capture had already passed, so the teardown cancelled a stale handle while the
    fresh session kept running. Asserts on the ORDER of the calls."""
    src = inspect.getsource(routes._handle_delete)
    reserve = src.index("_mark_deleting(")
    for later in ("state.get_slot(", "_exec_loop_id(", "_remove_nudge_loop("):
        assert reserve < src.index(later), (
            f"{later} is captured before the delete reservation: a slot created in "
            "that window would survive the teardown"
        )


def test_message_revalidates_between_slot_acquisition_and_dispatch():
    """Source guard: _ensure_worker_slot awaits, so a delete can complete between
    the identity check and the dispatch. There must be a _touch_spec refusal AFTER
    the slot is acquired and BEFORE the turn is handed over."""
    src = inspect.getsource(routes._handle_message)
    acquire = src.index("_ensure_worker_slot(")
    dispatch = src.index("_dispatch_turn(")
    between = src[acquire:dispatch]
    assert "_touch_spec(" in between, (
        "no re-pin between slot acquisition and dispatch: a delete finishing in "
        "that window would have its turn dispatched anyway"
    )
    # ...and nothing may await between that refusal and the synchronous dispatch.
    tail = between[between.index("_touch_spec(") :]
    assert "await " not in tail, f"an await slipped in before dispatch: {tail!r}"


@pytest.mark.asyncio
async def test_failed_loop_removal_releases_both_tombstone_and_reservation(tmp_path, monkeypatch):
    """Moving the reservation earlier means the 503 abort now owns two things to
    undo. Leaving either behind would hide a spec the user still has."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "keeper").mkdir(parents=True)
    sd = str(base / "keeper")
    routes._save_index({
        "keeper": {
            "spec_dir": sd,
            "working_dir": str(tmp_path / "wd"),
            "spec_type": "feature",
            "status": "planning",
            "slot_key": routes._new_slot_key("keeper"),
        }
    })

    async def _explode(*_a, **_k):
        raise RuntimeError("loop service down")

    monkeypatch.setattr(routes, "_remove_nudge_loop", _explode)

    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/keeper")
    finally:
        await client.close()

    assert resp.status == 503
    idx = await routes._aload_index()
    assert "keeper" in idx, "the spec was dropped despite the abort"
    assert not idx["keeper"].get(routes._DELETING), "reservation left behind — spec hidden from the list"
    # The tombstone must be gone too, or discovery would refuse to re-adopt it.
    assert await routes._touch_spec("keeper", expect_spec_dir=sd) is not None


# ── GPT round-62 findings (#518) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_predispatch_repin_uses_captured_key_when_client_sends_none(tmp_path, monkeypatch):
    """The reported hole in round 60's re-pin: slot_key is OPTIONAL on the wire, so a
    client that sends none left the pre-dispatch check with no creation pin. A delete
    plus a same-path recreate then passed it -- spec_dir still matched -- and the
    stale slot wrote into the REPLACEMENT's files.

    Simulates the window by swapping the index for a same-name, same-path spec with a
    NEW slot_key while _ensure_worker_slot is awaiting, then asserts the turn is
    refused rather than dispatched."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "s").mkdir(parents=True)
    sd = str(base / "s")
    original_key = routes._new_slot_key("s")
    routes._save_index({
        "s": {
            "spec_dir": sd,
            "working_dir": str(tmp_path / "wd"),
            "spec_type": "feature",
            "status": "planning",
            "slot_key": original_key,
        }
    })

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a))

    real_ensure = routes._ensure_worker_slot

    async def _ensure_then_recreate(state, name, meta, **kw):
        slot = await real_ensure(state, name, meta, **kw)
        # The delete+recreate lands here: same name, same path, NEW creation.
        idx = await routes._aload_index()
        idx["s"]["slot_key"] = routes._new_slot_key("s")
        routes._save_index(idx)
        return slot

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_then_recreate)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        # NO slot_key in the body -- the older-client shape the pin used to trust.
        resp = await client.post(f"{_BASE}/specs/s/message", json={"text": "edit files"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, body
    assert body.get("code") == "stale_client", body
    assert dispatched == [], "dispatched into a spec that was recreated mid-request"


def test_predispatch_repin_pins_both_halves_from_the_captured_entry():
    """Source guard: BOTH pins on the final check must come from `fresh` (server-side,
    already verified) and NEITHER from the client body. Reusing the claimed slot_key is
    what reopened the recreate window. Asserts on the ARGUMENTS, not on a comment."""
    src = inspect.getsource(routes._handle_message)
    dispatch = src.index("_dispatch_turn(")
    # The last _touch_spec before the dispatch is the re-pin.
    repin_start = src.rindex("_touch_spec(", 0, dispatch)
    repin = src[repin_start:dispatch]
    assert 'fresh.get("spec_dir")' in repin, "the re-pin stopped pinning the captured spec_dir"
    assert 'fresh.get("slot_key"' in repin, (
        "the re-pin does not pin the CAPTURED slot_key: a client that omits slot_key "
        "would have no creation pin, so a same-path recreate slips through"
    )
    assert "claimed_key" not in repin, (
        "the re-pin reads the client-claimed slot_key again -- that value is optional "
        "on the wire, so it cannot carry the creation identity"
    )


# ── Pause / Delete must not relaunch queued work (round 66) ──────────────────


# The spec name these two use. Deliberately not a name any other test creates:
# _slot_key() prefers a PERSISTED per-creation key from the module-global
# _SLOT_KEYS, so reusing a name another test created makes the key -- and the
# fake state's lookup -- miss, and the helper returns False before it ever stops.
_RELAUNCH_SPEC = "relaunch-probe"


def _relaunchable_slot(*, running: bool = True):
    """A slot carrying all three successor-turn sources, plus a cancel probe."""

    seen: dict = {"cancelled": False, "stopped": []}

    class _Task:
        def cancel(self):
            # Record the queue state AT CANCEL TIME. Clearing after the cancel is
            # useless: _run_chat's end-of-turn block runs as the task unwinds.
            seen["cancelled"] = True
            seen["queue_at_cancel"] = list(slot._queue)
            seen["steers_at_cancel"] = list(slot._pending_steers)
            seen["synthesis_at_cancel"] = slot._pending_synthesis

        def done(self):
            return False

    class _Slot:
        # Both derived from the module, never hardcoded: the ownership check
        # compares against routes.APP_NAME, and the lookup against _slot_key().
        key: str = routes._slot_key(_RELAUNCH_SPEC)
        _app: str = routes.APP_NAME
        running: bool = True
        task: object = None
        _queue: list = []
        _pending_steers: list = []
        _pending_synthesis: bool = True

    slot = _Slot()
    slot.running = running
    slot.task = _Task()
    slot._queue = [{"id": "q1", "content": "keep editing my files"}]
    slot._pending_steers = ["and also do this"]
    slot._pending_synthesis = True
    return slot, seen


def _state_with(slot, seen):
    class _Sessions:
        async def stop_turn(self, key, force=False):
            # Same reasoning as the cancel probe: a cooperative stop ends the turn
            # too, so the queue must already be empty when it lands.
            seen["stopped"].append(key)
            seen["queue_at_stop"] = list(slot._queue)
            seen["steers_at_stop"] = list(slot._pending_steers)
            seen["synthesis_at_stop"] = slot._pending_synthesis

    class _State:
        sessions = _Sessions()

        def __init__(self):
            self._slots = {slot.key: slot}

        def get_slot(self, key):
            return self._slots.get(key)

    return _State()


@pytest.mark.asyncio
async def test_pause_discards_queued_work_before_stopping():
    """Pause must not hand the agent its next prompt.

    _run_chat swallows its CancelledError and its end-of-turn block then starts
    the next queued message (requeuing unconsumed steers into the queue head
    first) or a pending synthesis. So a Pause that only stopped the turn left the
    agent editing the user's files afterwards.
    """
    slot, seen = _relaunchable_slot()
    assert await routes._halt_active_turn(_state_with(slot, seen), _RELAUNCH_SPEC) is True

    assert seen["stopped"], "cooperative stop_turn was never called"
    assert seen["cancelled"] is True, "the in-flight turn was not cancelled"
    # Empty at BOTH stops -- the cooperative one ends the turn as surely as the
    # cancel, so a clear that only preceded the cancel would still race.
    assert seen["queue_at_stop"] == [], "queued message survived into the cooperative stop"
    assert seen["steers_at_stop"] == [], "pending steer survived into the cooperative stop"
    assert seen["synthesis_at_stop"] is False, "pending synthesis survived the cooperative stop"
    assert seen["queue_at_cancel"] == [], "queued message survived into the cancel"
    assert seen["steers_at_cancel"] == [], "pending steer survived into the cancel"
    assert seen["synthesis_at_cancel"] is False, "pending synthesis survived into the cancel"


@pytest.mark.asyncio
async def test_delete_discards_queued_work_before_cancelling(monkeypatch):
    """Delete has the same exposure, and worse: the successor turn would write
    into a spec directory the request is about to archive."""
    slot, seen = _relaunchable_slot()

    async def _saved(*a, **k):
        return None

    monkeypatch.setitem(
        sys.modules,
        "kiro_crew.dashboard.chat_persistence",
        types.SimpleNamespace(save_slot_off_loop=_saved),
    )
    assert await routes._teardown_worker_slot(_state_with(slot, seen), _RELAUNCH_SPEC) is True

    assert seen["cancelled"] is True, "the in-flight turn was not cancelled"
    assert seen["queue_at_cancel"] == [], "queued message survived into the cancel"
    assert seen["steers_at_cancel"] == [], "pending steer survived into the cancel"
    assert seen["synthesis_at_cancel"] is False, "pending synthesis survived into the cancel"


def test_discard_covers_every_relaunch_source():
    """All three sources, or the ones left behind still start a successor."""
    slot, _ = _relaunchable_slot()
    routes._discard_queued_work(slot)
    assert slot._queue == []
    assert slot._pending_steers == []
    assert slot._pending_synthesis is False


def test_discard_tolerates_a_slot_without_the_attributes():
    """A foreign or half-built slot must not make teardown raise."""
    routes._discard_queued_work(types.SimpleNamespace())


def test_both_stop_helpers_discard_before_they_stop():
    """Source guard on the ORDER: the discard must precede every stop in both
    helpers. Asserts on the calls, not on a comment."""
    for fn in (routes._halt_active_turn, routes._teardown_worker_slot):
        src = inspect.getsource(fn)
        assert "_discard_queued_work(" in src, f"{fn.__name__} does not discard queued work"
        discard = src.index("_discard_queued_work(")
        for stop in ("task.cancel()", "stop_turn("):
            at = src.find(stop)
            if at == -1:
                continue
            assert discard < at, (
                f"{fn.__name__}: _discard_queued_work must precede {stop} -- clearing "
                "afterwards races _run_chat's end-of-turn block"
            )


def test_run_chat_still_relaunches_from_these_three_fields():
    """Pins the UPSTREAM behaviour this fix exists for. If the gateway ever stops
    relaunching from the end-of-turn block, this test should fail loudly so the
    discard can be re-justified rather than cargo-culted."""
    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner)
    assert "_start_next_queued_turn" in src
    assert "_requeue_unconsumed_steers" in src
    # The swallow is the reason a cancel behaves like a clean finish here.
    assert re.search(r"except asyncio\.CancelledError:", src)


# ── A stale create-unwind must not delete a replacement's worktree (round 67) ─


def _removal_probe(monkeypatch):
    """Record what _remove_worktree would destroy, without touching git."""
    removed: list = []

    async def _fake_remove(repo_root, worktree_path, branch=""):
        removed.append((worktree_path, branch))

    monkeypatch.setattr(routes, "_remove_worktree", _fake_remove)
    return removed


@pytest.mark.asyncio
async def test_rollback_removes_the_worktree_when_the_name_is_still_ours(monkeypatch):
    """The control. Without this, every failed create orphans a worktree+branch."""
    removed = _removal_probe(monkeypatch)
    did = await routes._rollback_worktree_if_ours(
        "s",
        was_ours=True,
        repo_root="/repo",
        created_worktree="/repo-wt-s",
        worktree_branch="spec/s",
    )
    assert did is True
    assert removed == [("/repo-wt-s", "spec/s")]


@pytest.mark.asyncio
async def test_rollback_spares_a_replacements_worktree(monkeypatch):
    """The reported hazard. A concurrent delete + same-name recreate makes the
    identity-pinned pop return False; the worktree path is derived from the NAME,
    so it now belongs to the replacement. Removing it would force-delete that
    spec's uncommitted work and hard-delete its branch."""
    removed = _removal_probe(monkeypatch)
    did = await routes._rollback_worktree_if_ours(
        "s",
        was_ours=False,
        repo_root="/repo",
        created_worktree="/repo-wt-s",
        worktree_branch="spec/s",
    )
    assert did is False
    assert removed == [], "a stale unwind destroyed the replacement spec's worktree"


@pytest.mark.asyncio
async def test_rollback_is_a_noop_when_this_create_made_no_worktree(monkeypatch):
    removed = _removal_probe(monkeypatch)
    assert (
        await routes._rollback_worktree_if_ours(
            "s", was_ours=True, repo_root="/repo", created_worktree="", worktree_branch=""
        )
        is False
    )
    assert removed == []


def test_unwind_gates_the_rollback_on_the_pinned_pop():
    """Source guard on the WIRING: the unwind must pass the pop's own result
    through, not re-derive ownership or hardcode it."""
    src = inspect.getsource(routes._handle_create)
    unwind = src[src.index("async def _unwind_create"):]
    pop = unwind.index("was_ours = await _mutate_index(_pop_if_ours)")
    call = unwind.index("_rollback_worktree_if_ours(")
    assert pop < call, "ownership must be established before the rollback"
    args = unwind[call:unwind.index(")", unwind.index("worktree_branch=worktree_branch", call))]
    assert "was_ours=was_ours" in args, (
        "the rollback is not gated on the pinned pop -- a stale unwind would "
        "force-delete a replacement spec's worktree and branch"
    )
    # The raw destructive call must NOT survive alongside the gated one.
    assert "_remove_worktree(" not in unwind, (
        "the unwind still calls _remove_worktree directly, bypassing the gate"
    )


def test_remove_worktree_is_destructive_enough_to_need_the_gate():
    """Pins WHY the gate matters. If _remove_worktree ever stops being a
    force-remove + branch delete, the gate can be re-argued rather than assumed."""
    src = inspect.getsource(routes._remove_worktree)
    assert '"worktree", "remove", "--force"' in src
    assert '"branch", "-D"' in src


def test_only_the_post_insert_unwind_needs_the_gate():
    """Scope guard: the three EARLY rollbacks are unconditional on purpose.

    They run before the index insert, so no other request can hold the name --
    and _create_worktree would itself have failed had a concurrent create already
    made `<repo>-wt-<name>`. Gating those would orphan a worktree on every
    legitimate 400/409. Only the post-insert unwind spans an await (the slot
    acquisition) during which a delete + recreate can land.
    """
    src = inspect.getsource(routes._handle_create)
    early = src[:src.index("async def _unwind_create")]
    assert early.count("_remove_worktree(") == 3, (
        "the early-rollback count changed -- re-audit whether the new one spans "
        "an await after the index insert (if so it needs the ownership gate too)"
    )
    assert "was_ours" not in early, "an early rollback should not need the gate"


# ── Slot identity must survive both awaits in _ensure_worker_slot (round 68) ──

_IDENTITY_SPEC = "identity-probe"


def _identity_state(slot_key: str, *, slot=None):
    """Minimal state whose registry is keyed by ONE slot key."""

    created: list = []

    class _State:
        def __init__(self):
            self._slots = {slot_key: slot} if slot is not None else {}

        def get_slot(self, key):
            return self._slots.get(key)

        def get_or_create_slot(self, name, app=""):
            created.append(name)
            made = types.SimpleNamespace(key=name, _app=app, project="", _titled=False)
            self._slots[name] = made
            return made

    return _State(), created


@pytest.mark.asyncio
async def test_replacement_during_transcript_restore_is_refused(monkeypatch, tmp_path):
    """Window 1. _slot_key reads the module-global _SLOT_KEYS; a delete +
    same-name recreate rewrites it. Recomputing after the restore await made the
    stale request adopt the REPLACEMENT's slot and stamp its own project on it."""
    monkeypatch.setitem(routes._SLOT_KEYS, _IDENTITY_SPEC, "spec-builder-old")

    async def _restore(state, name, adopt_closed=False):
        # The concurrent delete + recreate lands while we are awaiting.
        routes._SLOT_KEYS[name] = "spec-builder-new"

    monkeypatch.setattr(routes, "_restore_worker_transcript", _restore)
    state, created = _identity_state("spec-builder-old")

    got = await routes._ensure_worker_slot(
        state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)}
    )
    assert got is None, "a stale request acquired the replacement spec's slot"
    assert created == [], "the stale request created a slot under the new identity"


@pytest.mark.asyncio
async def test_replacement_during_working_dir_check_is_refused(monkeypatch, tmp_path):
    """Window 2. _safe_dir runs off-loop; the identity can move during it, and
    the code then stamped _app/project onto the slot it captured earlier."""
    monkeypatch.setitem(routes._SLOT_KEYS, _IDENTITY_SPEC, "spec-builder-old")
    ours = types.SimpleNamespace(
        key="spec-builder-old", _app=routes.APP_NAME, project="", _titled=False
    )
    state, _ = _identity_state("spec-builder-old", slot=ours)

    real_safe_dir = routes._safe_dir

    def _safe_dir_then_replace(path):
        out = real_safe_dir(path)
        routes._SLOT_KEYS[_IDENTITY_SPEC] = "spec-builder-new"
        return out

    monkeypatch.setattr(routes, "_safe_dir", _safe_dir_then_replace)

    got = await routes._ensure_worker_slot(
        state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)}
    )
    assert got is None, "the stale request kept going after its spec was replaced"
    assert ours.project == "", "a replaced spec's slot was repointed at the stale project"
    assert ours._app == routes.APP_NAME


@pytest.mark.asyncio
async def test_stable_identity_still_acquires_the_slot(monkeypatch, tmp_path):
    """The control: nothing moves, so the slot is acquired and scoped as before.
    Without this, refusing unconditionally would also pass the two tests above."""
    monkeypatch.setitem(routes._SLOT_KEYS, _IDENTITY_SPEC, "spec-builder-stable")
    ours = types.SimpleNamespace(
        key="spec-builder-stable", _app=routes.APP_NAME, project="", _titled=False
    )
    state, _ = _identity_state("spec-builder-stable", slot=ours)

    got = await routes._ensure_worker_slot(
        state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)}
    )
    assert got is ours, "a stable identity failed to acquire its own slot"
    assert got.project == str(tmp_path), "the slot was not scoped to the spec's project"


def test_slot_key_is_resolved_once_and_both_awaits_are_guarded():
    """Source guard on the CALLS and their ORDER, not on a comment.

    One resolution before any await, and an identity re-check after each await.
    A second resolution anywhere in the body is the bug this round fixed.
    """
    src = inspect.getsource(routes._ensure_worker_slot)
    assert src.count("_slot_key(name)") == 1, (
        f"_slot_key(name) is resolved {src.count('_slot_key(name)')} times -- "
        "recomputing it after an await reintroduces the mid-flight identity swap"
    )
    resolve = src.index("slot_key = _slot_key(name)")
    awaits = [
        src.index("await _restore_worker_transcript("),
        src.index("await asyncio.to_thread(_safe_dir"),
    ]
    assert resolve < min(awaits), "the identity is captured after an await"
    guards = [i for i in range(len(src)) if src.startswith("_slot_identity_moved(name, slot_key)", i)]
    assert len(guards) == 2, f"expected 2 identity re-checks, found {len(guards)}"
    for a in awaits:
        assert any(g > a for g in guards), "an await is not followed by an identity re-check"
    # Creation must use the captured key.
    assert "get_or_create_slot(name=slot_key" in src, (
        "slot creation does not use the captured identity"
    )


def test_identity_guard_refuses_and_audits():
    """The guard itself: same key passes, changed key refuses."""
    routes._SLOT_KEYS["guard-probe"] = "spec-builder-k1"
    assert routes._slot_identity_moved("guard-probe", "spec-builder-k1") is False
    assert routes._slot_identity_moved("guard-probe", "spec-builder-k0") is True


# ── Identity pins must be (spec_dir AND slot_key), never directory-only ──────
# The rule this file already states in _unwind_create: a delete + re-import at
# the same name AND path leaves spec_dir identical, so the directory alone cannot
# distinguish our spec from the replacement's.


@pytest.mark.asyncio
async def test_status_reconcile_pins_on_slot_key_not_just_the_directory(monkeypatch):
    """_effective_status stamps status=planning when it decides an execution has
    finished. Pinned on spec_dir alone, that stamp landed on a REPLACEMENT spec
    created at the same name and path while the caller held a stale snapshot."""
    calls: list = []

    async def _spy_touch(name, **kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(routes, "_touch_spec", _spy_touch)
    monkeypatch.setattr(routes, "_exec_loop_active", lambda name: False)

    meta = {
        "status": "executing",
        "spec_dir": "/w/.kiro/specs/s",
        "slot_key": "spec-builder-s-aaaa1111",
    }
    out = await routes._effective_status("s", meta, types.SimpleNamespace(running=False))
    assert out == "planning"
    assert calls, "the reconcile did not stamp at all"
    kw = calls[0]
    assert kw.get("expect_spec_dir") == "/w/.kiro/specs/s"
    assert kw.get("expect_slot_key") == "spec-builder-s-aaaa1111", (
        "the reconciling stamp is pinned on the directory only -- a same-name, "
        "same-path replacement would be reset to planning"
    )


@pytest.mark.asyncio
async def test_reconcile_stamp_survives_the_arming_window(monkeypatch):
    """The hole the three early guards do NOT close, and the reason the slot_key
    pin is required rather than merely tidy.

    A replacement mid-ARMING has written status=executing but has not armed its
    nudge loop, so `_exec_loop_active` is False and no turn is running. The arming
    grace cannot rescue it either: `exec_arming_at` is read from the CALLER'S
    STALE meta, not from the replacement's fresh entry. All three guards fall
    through and the stamp is attempted -- so the pin is what refuses it.
    """
    calls: list = []

    async def _spy_touch(name, **kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(routes, "_touch_spec", _spy_touch)
    monkeypatch.setattr(routes, "_exec_loop_active", lambda name: False)

    # Stale snapshot: no arming stamp of its own, so no grace period applies.
    stale = {
        "status": "executing",
        "spec_dir": "/w/.kiro/specs/s",
        "slot_key": "spec-builder-s-OLD",
        "exec_arming_at": 0.0,
    }
    out = await routes._effective_status("s", stale, types.SimpleNamespace(running=False))
    assert out == "planning"
    assert calls and calls[0].get("expect_slot_key") == "spec-builder-s-OLD", (
        "the stamp carried no creation pin, so _touch_spec could not tell it "
        "apart from the replacement's entry"
    )


def test_handoff_captures_its_identity_before_the_await_and_pins_on_both():
    """Source guard on the ORDER and the ARGUMENTS.

    The capture must precede the _prepare_handoff await, and the reread must
    compare BOTH spec_dir and the captured slot_key. The slot_key check that
    already existed validates only the CLIENT's claim, so a request carrying no
    claim previously had no identity check at all.
    """
    src = inspect.getsource(routes._handle_handoff)
    capture = src.index("started_slot_key = str(meta.get(\"slot_key\", \"\"))")
    await_match = re.search(r"await asyncio\.to_thread\(\s*_prepare_handoff", src)
    assert await_match, "handoff no longer hands _prepare_handoff to a worker thread"
    await_at = await_match.start()
    guard = src.index("spec_changed_during_start")
    assert capture < await_at, "the identity is captured after the await it must survive"
    # The claim check must precede the await too: _prepare_handoff CLEARS the STOP
    # sentinel, so a stale execute that reaches it disarms a replacement's Pause
    # before any comparison has run.
    claim_at = src.index("_client_identity_mismatch(claimed, spec_dir, started_slot_key)")
    assert claim_at < await_at, (
        "the client-claim check moved after the sentinel clear it is meant to gate"
    )
    assert await_at < guard, "the reread guard does not follow the await"
    window = src[await_at:guard]
    assert "!= started_slot_key" in window, (
        "the reread is pinned on spec_dir only -- a same-name, same-path "
        "replacement passes it"
    )
    assert 'str(meta.get("spec_dir", "")) != str(spec_dir)' in window, (
        "the directory pin was dropped"
    )


def test_no_index_mutation_is_pinned_on_the_directory_alone():
    """Class guard. Every _touch_spec call that pins spec_dir must also pin
    slot_key -- the two are one identity, and rounds 62 and 68 were both a
    caller passing only half of it."""
    src = inspect.getsource(routes)
    for match in re.finditer(r"_touch_spec\(", src):
        depth, i = 0, match.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = src[match.start():i + 1]
        if "expect_spec_dir" in call:
            assert "expect_slot_key" in call, (
                "a _touch_spec call pins the directory without the creation key, "
                f"so a same-path replacement passes it:\n{call}"
            )

# ── index admission: the write side must use the read side's predicate ──


#: Satisfies _NAME_RE (letters, digits, '-', '_', <=64) but _redact rewrites it,
#: so _load_index would refuse to serve it. Assembled from parts so the literal
#: is not itself a credential-shaped constant in the source.
_CREDENTIAL_SHAPED_NAME = "ghp" + "_" + "0123456789abcdef0123456789abcdef0123"


def test_the_probe_name_is_the_shape_this_class_is_about():
    """Guards the fixture: grammar-valid, but not admissible to the index.

    If _redact stops rewriting this shape the other tests here would pass for the
    wrong reason, so pin both halves explicitly.
    """
    assert routes._valid_name(_CREDENTIAL_SHAPED_NAME), "probe must satisfy the grammar"
    assert not routes._usable_name(_CREDENTIAL_SHAPED_NAME), "probe must fail admission"


@pytest.mark.asyncio
async def test_create_refuses_a_name_the_loader_would_discard(tmp_path, monkeypatch):
    """Accepting on the grammar alone built a spec the next load dropped.

    The handler creates the directory, worktree and session BEFORE anything reads
    the index back, so the orphan outlived the request that made it.
    """
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={
                "name": _CREDENTIAL_SHAPED_NAME,
                "working_dir": str(tmp_path),
                "spec_type": "feature",
                "description": "d",
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["code"] == "invalid_name"
        assert not (tmp_path / ".kiro" / "specs" / _CREDENTIAL_SHAPED_NAME).exists(), (
            "create left a spec directory for a name the index cannot hold"
        )


def test_discovery_does_not_adopt_a_name_the_loader_would_discard(tmp_path, monkeypatch):
    """Discovery WRITES index[name]; admitting on the grammar alone made it
    re-add an entry the next load drops, rediscovering it on every call.

    Scan roots come from the index's own working_dir values, so one ordinary
    entry is what puts tmp_path in scope -- and it doubles as the control that
    proves discovery still works.
    """
    specs_base = tmp_path / ".kiro" / "specs"
    for spec_name in ("ordinary-spec", _CREDENTIAL_SHAPED_NAME, "second-ordinary"):
        d = specs_base / spec_name
        d.mkdir(parents=True)
        (d / "requirements.md").write_text("r")

    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(routes, "_load_deleted", lambda: set())

    index = {
        "ordinary-spec": {
            "working_dir": str(tmp_path),
            "spec_dir": str(specs_base / "ordinary-spec"),
        }
    }
    routes._discover_folder_specs(index)

    assert "second-ordinary" in index, "discovery stopped adopting ordinary directories"
    assert _CREDENTIAL_SHAPED_NAME not in index, (
        "discovery adopted a name _load_index will drop, so it would be "
        "rediscovered and re-saved on every list poll"
    )


def test_no_index_write_path_admits_on_the_grammar_alone():
    """Class guard. _load_index admits keys with _usable_name; a path that PUTS a
    name into the index (or resolves a slot for one) must use the same predicate,
    or it writes entries the next read discards.

    Asserts on the calls, not on a comment. _owns_slot_key is the one allowed
    _valid_name caller: its name always comes from an already-admitted index
    key, so the predicates agree there.
    """
    import inspect

    src = inspect.getsource(routes)
    allowed = {"_owns_slot_key", "_usable_name"}
    offenders = []
    for match in re.finditer(r"^(?:async )?def (\w+)\(", src, re.M):
        fname = match.group(1)
        start = match.end()
        nxt = re.search(r"^(?:async )?def \w+\(", src[start:], re.M)
        body = src[start : start + (nxt.start() if nxt else len(src) - start)]
        if "_valid_name(" in body and fname not in allowed:
            offenders.append(fname)
    assert not offenders, (
        "these gate on _valid_name, the grammar half only, and will admit names "
        f"_load_index discards: {offenders}"
    )

# ── _safe_dir: absolute-only, enforced where it can actually fail ──


def test_safe_dir_refuses_a_relative_working_dir(tmp_path, monkeypatch):
    """A relative value must be refused, not silently resolved against the cwd.

    index.json is agent-writable, so a `working_dir` of "." would otherwise
    normalize to the gateway's own checkout and a spec's worktree plus the agent
    running in it would target that tree.
    """
    monkeypatch.chdir(tmp_path)
    for relative in (".", "..", "relative/path", "./sub", ""):
        assert routes._safe_dir(relative) is None, (
            f"_safe_dir accepted the relative value {relative!r}"
        )


def test_safe_dir_still_accepts_absolute_and_tilde(tmp_path, monkeypatch):
    """The guard must not cost the two forms that are legitimately absolute."""
    assert routes._safe_dir(str(tmp_path)) == Path(
        os.path.realpath(str(tmp_path))
    ), "_safe_dir rejected a plain absolute directory"

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "inside").mkdir()
    assert routes._safe_dir("~/inside") is not None, (
        "_safe_dir rejected a ~-relative path, which expands to an absolute one"
    )


def test_absoluteness_is_checked_before_realpath():
    """Source guard: order is the whole point.

    ``os.path.realpath`` resolves against the process cwd and ALWAYS returns an
    absolute path, so an is_absolute()/isabs() test placed after it is dead code
    and the documented guarantee is not enforced. Asserts on the order of the
    calls, not on a comment.
    """
    import inspect

    src = inspect.getsource(routes._safe_dir)
    isabs_at = src.index("os.path.isabs(")
    realpath_at = src.index("os.path.realpath(")
    assert isabs_at < realpath_at, (
        "the absoluteness test moved after realpath, where it can never fail"
    )

# ── a stale execute must not clear a replacement's STOP sentinel ──


def _paused_spec(tmp_path, slot_key):
    """A spec whose Pause has been persisted: tasks.md present, STOP on disk."""
    spec_dir = tmp_path / "proj" / ".kiro" / "specs" / "paused"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tasks.md").write_text("- [ ] task")
    stop = spec_dir / routes._STOP_FILE
    stop.write_text("stop")
    routes._save_index(
        {
            "paused": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "proj"),
                "slot_key": slot_key,
            }
        }
    )
    return spec_dir, stop


def test_prepare_handoff_refuses_to_clear_when_the_identity_moved(tmp_path, monkeypatch):
    """The clear is destructive, so it is gated on identity, not merely ordered.

    Arming removes the STOP a Pause wrote. A stale same-name, same-path execute
    carrying NO client claim cannot be refused by comparing claims, so the act
    itself has to check: with the index now on a different creation, the sentinel
    must survive.
    """
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    spec_dir, stop = _paused_spec(tmp_path, slot_key="spec-builder-paused-replacement")

    ready, sentinel = routes._prepare_handoff(spec_dir, "paused", "spec-builder-paused-stale")

    assert ready is False, "a stale identity was allowed to arm the run"
    assert stop.exists(), "the replacement's STOP sentinel was cleared by a stale execute"


def test_prepare_handoff_still_clears_for_the_matching_identity(tmp_path, monkeypatch):
    """The gate must not break the ordinary path it guards."""
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    spec_dir, stop = _paused_spec(tmp_path, slot_key="spec-builder-paused-abc")

    ready, sentinel = routes._prepare_handoff(spec_dir, "paused", "spec-builder-paused-abc")

    assert ready is True, "the matching identity was refused"
    assert not stop.exists(), "the stale STOP was not cleared for the current creation"
    assert sentinel == str(spec_dir / routes._STOP_FILE)


def test_prepare_handoff_unpinned_call_keeps_working(tmp_path, monkeypatch):
    """Specs predating per-creation keys carry no slot_key, so the gate is skipped
    rather than turning every legacy handoff into a refusal."""
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")
    spec_dir, stop = _paused_spec(tmp_path, slot_key="")

    assert routes._prepare_handoff(spec_dir, "paused", "")[0] is True
    assert not stop.exists()

# ── broadcast-eligible appends must not carry raw caller text ──


#: Roles _ChatSlot.append suppresses the global SSE push for. Everything else is
#: broadcast to every connected dashboard client, so its content leaves the
#: process and must be redacted first. "chunk"/"done" are skipped
#: unconditionally; "user" is skipped only because this app never passes the
#: host's ``broadcast_user=True`` opt-in (guarded by
#: test_the_host_still_exempts_only_these_roles_from_broadcast).
_NON_BROADCAST_ROLES = ("chunk", "done", "user")


class _RecordingSlot:
    """Minimal slot: records what _dispatch_turn appends, and reports running."""

    def __init__(self):
        self.running = True
        self.appended: list[tuple[str, str]] = []
        self.queued: list[str] = []

    def queue_append(self, message):
        self.queued.append(message)

    def append(self, role, content, cls="", ts="", *, broadcast=True, meta=None):
        self.appended.append((role, content))


class _NoopState:
    def push_slots_update(self):
        pass


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
        "the host's unconditional broadcast skip set changed; "
        "_NON_BROADCAST_ROLES is now stale"
    )
    assert '(role != "user" or broadcast_user)' in src, (
        "the host no longer skips user rows by default; _NON_BROADCAST_ROLES is "
        "now stale"
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
    assert secret not in content, (
        "the queued message reached the broadcast path with the credential intact"
    )
    # The queue itself still carries the real text: the agent must receive what
    # the user actually typed. Only the broadcast copy is scrubbed.
    assert slot.queued and secret in slot.queued[-1], (
        "the redaction leaked into the queue, so the agent would get scrubbed input"
    )


def test_no_broadcast_eligible_append_passes_raw_caller_text():
    """Class guard: every slot.append whose role is NOT in the host's skip set
    must route its content through _redact.

    The `user` append is deliberately exempt -- `user` IS skipped, and the host's
    own send path stores raw for the same reason (the author is the only reader;
    redaction happens at the emit sites).
    """
    import inspect

    src = inspect.getsource(routes)
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
        redacted = (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "_redact"
        )
        if not redacted:
            offenders.append(f"{role} at line {node.lineno}")
    assert not offenders, (
        "these appends are broadcast to every dashboard client but pass content "
        f"that did not go through _redact: {offenders}"
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
        assert secret not in body["base_path"], (
            "the agent-written base_path reached the dashboard with the credential intact"
        )


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
    import inspect

    src = inspect.getsource(routes)
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
        "these handlers return agent-writable settings without _redact, so a "
        f"credential in the file reaches the dashboard raw: {offenders}"
    )
