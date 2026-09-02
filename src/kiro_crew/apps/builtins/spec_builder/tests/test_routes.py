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
import pkgutil
import re
import sys
import textwrap
import threading
import time
import types
from pathlib import Path
from typing import Callable, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder import backend as backend_package
from kiro_crew.apps.builtins.spec_builder.backend.handlers import _ClientClaim
from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import (
    BACKEND_MODULES,
    backend_namespace,
    routes,
    routes_source,
)

_BASE = "/api/apps/spec-builder"
_REAL_DECISIONS_PATH: Path | None = None
_DEFAULT_DECISION_STATE = {
    "decisions": [
        {
            "id": "transport",
            "title": "Inbound transport",
            "options": [],
        }
    ]
}
_SERIALIZED_EFFECTS = (
    "_mutate_index(",  # registers or removes an index entry
    "_save_index(",  # ditto, unlocked writer
    "authorize_and_add_nudge(",  # arms an autonomous timer that will dispatch later
    "_dispatch_turn(",  # starts a turn now
)


def test_route_facade_covers_every_backend_module() -> None:
    """Keep aggregate source guards complete when the backend gains a module."""
    discovered = {
        info.name
        for info in pkgutil.walk_packages(
            backend_package.__path__,
            prefix=f"{backend_package.__name__}.",
        )
    }
    listed = {module.__name__ for module in BACKEND_MODULES}
    assert (
        listed == discovered
    ), f"missing={sorted(discovered - listed)}, extra={sorted(listed - discovered)}"


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
    # The decision ledger lives under the security keystone's trust root, OUTSIDE
    # this dir, so it needs its own redirect -- and the autouse guard below watches
    # the real one, which is what catches a test that forgets.
    monkeypatch.setattr(routes, "_DECISIONS_PATH", state_dir / "decisions.json")
    # Process-owned execution claims are scoped to the same temporary app state.
    # A failed test must not leave a pre-dispatch claim that affects the next one.
    monkeypatch.setattr(routes, "_EXECUTION_CLAIMS", {})
    monkeypatch.setattr(routes, "_EXECUTION_STOPS", {})
    monkeypatch.setattr(routes, "_PENDING_DISPATCH_CLAIMS", {})
    monkeypatch.setattr(routes, "_REVOKED_EXECUTION_CLAIMS", set())
    monkeypatch.setattr(routes, "_REVOKED_PENDING_DISPATCH_CLAIMS", set())
    monkeypatch.setattr(routes, "_STOP_ROLLBACK_TASKS", set())
    monkeypatch.setattr(routes, "_OBSERVED_SLOT_KEYS", {})
    monkeypatch.setattr(routes, "_OBSERVED_SPEC_DIRS", {})
    monkeypatch.setattr(routes, "_INDEXED_SPEC_IDENTITIES", set())
    monkeypatch.setattr(routes, "_INDEXED_SPEC_NAMES", set())
    monkeypatch.setattr(routes, "_INDEXED_SPEC_DIRS", set())
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    return state_dir


def _stage_identity(path: Path) -> dict[str, int]:
    """Serialize the inode identity captured with a duplicate reservation."""
    info = path.stat()
    return {"stage_dev": info.st_dev, "stage_ino": info.st_ino}


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

    The decision ledger needs its own watch: it lives under the security keystone's
    ``trust/`` root, OUTSIDE this app's state dir, so the directory comparison above
    cannot see it -- and it holds the user's real recorded answers.
    """
    before = _live_state_snapshot()
    before_ledger = _live_decisions_snapshot()
    _redirect_state(monkeypatch, tmp_path / "_autouse_state")
    yield
    assert (
        _live_state_snapshot() == before
    ), f"a test wrote to the live state dir: {_REAL_STATE_DIR}"
    assert _live_decisions_snapshot() == before_ledger, "a test wrote to the live decision ledger"


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
    startup_before = list(app.on_startup)
    routes.register_routes(app)

    assert (
        list(app.on_startup) == startup_before
    ), "Spec Builder filesystem recovery would block socket readiness"

    wired = {
        (r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None
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
        # Direct authority over the artifacts: record a phase approval, run one
        # task, and the label / archive / duplicate lifecycle.
        ("POST", f"{_BASE}/specs/{{name}}/approve"),
        ("POST", f"{_BASE}/specs/{{name}}/task"),
        ("POST", f"{_BASE}/specs/{{name}}/title"),
        ("POST", f"{_BASE}/specs/{{name}}/archive"),
        ("POST", f"{_BASE}/specs/{{name}}/duplicate"),
        ("DELETE", f"{_BASE}/specs/{{name}}"),
    }
    assert expected <= wired


@pytest.mark.asyncio
async def test_duplicate_recovery_runs_once_on_first_enabled_request(tmp_path, monkeypatch):
    recovered: list[str] = []
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(
        routes, "_recover_abandoned_reservations", lambda: recovered.append("recovered")
    )

    async with _make_client(monkeypatch, tmp_path) as client:
        assert (await client.get(f"{_BASE}/settings")).status == 200
        assert (await client.get(f"{_BASE}/specs")).status == 200

    assert recovered == ["recovered"]


def test_slot_key_prefix_renamed():
    # Ported from the external app's 'kiro-specs-' prefix to 'spec-builder-'.
    assert routes._slot_key("demo") == "spec-builder-demo"


# ── settings round-trip ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_roundtrip(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.get(f"{_BASE}/settings")
        assert resp.status == 200
        body = await resp.json()
        assert body["base_path"] == ""
        assert body["model"] == ""

        abs_base = str(tmp_path / "specs-home")
        resp = await client.put(f"{_BASE}/settings", json={"base_path": abs_base})
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "base_path": abs_base, "model": ""}

        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["base_path"] == abs_base


@pytest.mark.asyncio
async def test_settings_model_roundtrips_and_empty_means_inherit(tmp_path, monkeypatch):
    """The app-wide default model round-trips, and '' round-trips AS '' — an
    empty selection must come back as inherit, not be dropped or persisted as a
    literal model name. An unknown name is kept (availability is only decidable
    in a live session, where the withhold path owns it)."""
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": "  test-model-x  "}
        )
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "base_path": "", "model": "test-model-x"}

        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["model"] == "test-model-x"

        # Clearing the pick round-trips back to inherit.
        resp = await client.put(f"{_BASE}/settings", json={"base_path": "", "model": ""})
        assert resp.status == 200
        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["model"] == ""


@pytest.mark.asyncio
async def test_settings_write_without_model_key_preserves_the_stored_model(tmp_path, monkeypatch):
    """settings.json predates the model field, so a legacy client PUTting only
    base_path must not silently erase a configured model. Absence preserves;
    clearing requires an explicit ''."""
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": "test-model-x"}
        )
        assert resp.status == 200

        # Legacy-shaped write: no model key at all.
        resp = await client.put(f"{_BASE}/settings", json={"base_path": ""})
        assert resp.status == 200
        assert (await resp.json())["model"] == "test-model-x"

        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["model"] == "test-model-x", "an omitted key erased the model"


@pytest.mark.asyncio
async def test_settings_rejects_malformed_model(tmp_path, monkeypatch):
    """Mirrors the Research app's write contract: a non-string is a 400 that
    names the problem, and an over-length id is rejected rather than truncated
    (a sliced id is a different string that is never served)."""
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": ["not", "a", "string"]}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "model_not_a_string"

        resp = await client.put(
            f"{_BASE}/settings",
            json={"base_path": "", "model": "m" * (routes._MAX_MODEL_LEN + 1)},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "model_too_long"

        # GET serves the field through _redact; its fail-closed placeholder must
        # not be storable as a model if a client round-trips the read back.
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": routes._UNSCRUBBABLE}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "model_invalid"


def test_load_settings_degrades_malformed_model_to_inherit(tmp_path, monkeypatch):
    """settings.json is agent-writable, so its model FIELD is untrusted like its
    shape: a list, a number, or an over-length string loads as '' (= inherit),
    mirroring the base_path hardening at the same chokepoint."""
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(routes, "_SETTINGS_PATH", settings_path)
    bad_values: list[object] = [[], 7, None, "m" * (routes._MAX_MODEL_LEN + 1)]
    for bad in bad_values:
        settings_path.write_text(json.dumps({"base_path": "", "model": bad}))
        assert routes._load_settings()["model"] == "", f"{bad!r} did not degrade"
    # A sane value survives the same chokepoint, trimmed.
    settings_path.write_text(json.dumps({"base_path": "", "model": " test-model-x "}))
    assert routes._load_settings()["model"] == "test-model-x"


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
            json={
                "name": "probe",
                "working_dir": os.path.expanduser("~/.ssh"),
                "spec_type": "feature",
            },
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
    assert names == {f"dir{i}" for i in range(5)}  # no hidden, no skip-list, no files
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

    src = routes_source()
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

    src = routes_source()
    assert "slot._trust = True" not in src
    # And no revive-by-another-name.
    assert "_trust = True" not in src


def test_no_poll_dependent_ttl_machinery_remains():
    """The TTL is gone rather than left half-wired: a guard that only runs while
    a browser tab is open is not enforcement, and keeping it would imply a bound
    that does not hold."""

    src = routes_source()
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
    assert (spec_dir / routes._STOP_FILE).is_file()  # loop is sentinelled
    assert slot._trust is True  # user's choice preserved


# ── GPT round-5 findings (#518) ───────────────────────────────────────────────


# (1) polled handlers must not do filesystem work on the event loop


def test_detail_handler_offloads_all_filesystem_work():
    """Source guard. The detail endpoint is polled every 2.5s while a build runs
    and it stat-ed three files, read up to three 1 MiB documents and read
    .spec-state.json — all inline, freezing the gateway loop (chat streaming and
    heartbeats included) for the duration of every poll."""

    src = inspect.getsource(routes._handle_get)
    assert "asyncio.to_thread(" in src and "_collect_spec_documents" in src
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


def test_collect_spec_documents_returns_the_detail_bundle(tmp_path):
    """The bundled collector produces phase, docs, state and task metadata."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("# reqs")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / ".spec-state.json").write_text(json.dumps({"blocking": "waiting on you"}))

    phase, files, state, meta = routes._collect_spec_documents(spec_dir)
    assert phase == "design"  # newest present phase file wins
    assert files["requirements.md"] == "# reqs"
    assert state is not None and state["blocking"] == "waiting on you"
    # The hash is of the file AS STORED because it binds a phase approval to the
    # exact document reviewed.
    assert meta["docs"]["requirements.md"]["hash"] == routes._sha256_text("# reqs")
    assert set(meta["docs"]["requirements.md"]) == {"hash"}
    # No tasks.md here, so the list is empty rather than absent.
    assert meta["tasks"] == [] and meta["task_progress"] == {"done": 0, "total": 0}


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
    routes._save_index(
        {
            "kept": {"spec_dir": "/a/kept", "status": "planning"},
            "added-meanwhile": {"spec_dir": "/a/added", "status": "planning"},
        }
    )

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
    assert (
        routes._DELETING not in loaded["orphaned"]
    ), "a reservation from a process that is gone still hides the spec and reserves its name"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="atomic no-replace directory publication is unavailable",
)
def test_duplicate_publication_never_replaces_a_destination_created_concurrently(
    tmp_path, monkeypatch
):
    """The destination's creation and the publish syscall are one arbitration.

    An existence check followed by ordinary rename loses this race on POSIX:
    rename replaces an empty directory, including its identity and metadata.
    """
    stage = tmp_path / ".copy.stage"
    stage.mkdir()
    (stage / "requirements.md").write_text("# staged")
    target = tmp_path / "copy"
    target_identity: list[tuple[int, int]] = []
    real_rename = routes.rename_noreplace

    def _writer_wins_before_the_syscall(*args, **kwargs):
        target.mkdir(mode=0o700)
        stat = target.stat()
        target_identity.append((stat.st_dev, stat.st_ino))
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(routes, "rename_noreplace", _writer_wins_before_the_syscall)

    assert routes._publish_staged_copy(stage, target) == "conflict"
    after = target.stat()
    assert (after.st_dev, after.st_ino) == target_identity[0]
    assert stage.is_dir(), "the losing staged copy replaced the concurrent destination"


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate writes are unavailable",
)
def test_duplicate_doc_creation_refuses_an_ancestor_swapped_after_validation(tmp_path, monkeypatch):
    """The opened directory, not its raceable pathname, owns authorization."""
    project = tmp_path / "project"
    spec_dir = project / "copy"
    spec_dir.mkdir(parents=True)
    external_parent = tmp_path / "external"
    external_spec = external_parent / "copy"
    external_spec.mkdir(parents=True)
    original_project = tmp_path / "original-project"
    real_verified = routes._verified_spec_dir
    swapped = False

    def _verify_then_swap(path):
        nonlocal swapped
        verified = real_verified(path)
        if path == spec_dir and verified is not None and not swapped:
            project.rename(original_project)
            project.symlink_to(external_parent, target_is_directory=True)
            swapped = True
        return verified

    monkeypatch.setattr(routes, "_verified_spec_dir", _verify_then_swap)

    result, identity = routes._create_spec_doc(spec_dir, "requirements.md", "# copied")

    assert result == "unsafe_dir"
    assert identity is None
    assert not (external_spec / "requirements.md").exists()


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate cleanup is unavailable",
)
def test_duplicate_rollback_refuses_an_ancestor_swapped_after_validation(tmp_path, monkeypatch):
    """A redirected path cannot make rollback unlink an external matching inode."""
    project = tmp_path / "project"
    spec_dir = project / "copy"
    spec_dir.mkdir(parents=True)
    external_parent = tmp_path / "external"
    external_spec = external_parent / "copy"
    external_spec.mkdir(parents=True)
    external_doc = external_spec / "requirements.md"
    external_doc.write_text("# external")
    stat = external_doc.stat()
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    original_project = tmp_path / "original-project"
    real_verified = routes._verified_spec_dir
    swapped = False

    def _verify_then_swap(path):
        nonlocal swapped
        verified = real_verified(path)
        if path == spec_dir and verified is not None and not swapped:
            project.rename(original_project)
            project.symlink_to(external_parent, target_is_directory=True)
            swapped = True
        return verified

    monkeypatch.setattr(routes, "_verified_spec_dir", _verify_then_swap)

    routes._rollback_staged_docs(spec_dir, {"requirements.md": identity})

    assert external_doc.read_text() == "# external"


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate cleanup is unavailable",
)
def test_duplicate_cleanup_leaves_the_empty_stage_instead_of_racing_replacement(tmp_path):
    """POSIX has no inode-bound rmdir, so safe cleanup stops at an empty stage."""
    stage = tmp_path / ".copy.duplicate-deadbeef"
    stage.mkdir()

    routes._rollback_staged_docs(stage, {})

    assert stage.is_dir()
    assert not any(stage.iterdir())


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate staging is unavailable",
)
def test_duplicate_marker_failure_does_not_remove_a_redirected_stage(tmp_path, monkeypatch):
    """Marker unwind cannot follow a swapped ancestor into an external tree."""
    project = tmp_path / "project"
    project.mkdir()
    external_parent = tmp_path / "external"
    external_parent.mkdir()
    token = "d" * 32
    stage = project / f".copy.duplicate-{token}"
    external_stage = external_parent / stage.name
    external_stage.mkdir()
    original_project = tmp_path / "original-project"

    def _swap_then_refuse(_stage_fd, _duplicate_token):
        project.rename(original_project)
        project.symlink_to(external_parent, target_is_directory=True)
        return False

    monkeypatch.setattr(routes, "_write_duplicate_marker_at", _swap_then_refuse)

    result = routes._create_duplicate_stage(stage, token)

    assert result == "write_failed"
    assert external_stage.is_dir()
    assert (original_project / stage.name).is_dir()


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_write_failure_retains_provenance_until_documents_are_rolled_back(
    tmp_path, monkeypatch
):
    """Failure cleanup cannot create markerless staged-document residue."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    real_create = routes._create_spec_doc
    calls = 0

    def _fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return "write_failed", None
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _fail_second)
    result, created = routes._write_and_publish_duplicate(
        stage,
        target,
        {"requirements.md": "# copied", "design.md": "# fails"},
        token,
    )

    assert result == "write_failed"
    assert routes._duplicate_marker_matches(stage, token)
    assert routes._rollback_staged_docs(stage, created) is True
    assert routes._duplicate_marker_matches(stage, token)


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_fsyncs_staged_directory_entries_before_publication(tmp_path, monkeypatch):
    """A durable rename cannot expose child entries that were never persisted."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    stage_stat = stage.stat()
    stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
    stage_synced = False
    real_fsync = routes.os.fsync

    def _record_stage_fsync(fd):
        nonlocal stage_synced
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) == stage_identity:
            stage_synced = True
        return real_fsync(fd)

    monkeypatch.setattr(routes.os, "fsync", _record_stage_fsync)

    result, _created = routes._write_and_publish_duplicate(
        stage, target, {"requirements.md": "# copied"}, token
    )

    assert result == ""
    assert stage_synced
    assert (target / "requirements.md").read_text() == "# copied"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_refuses_a_stage_replaced_at_the_publication_boundary(tmp_path, monkeypatch):
    """The renamed directory must be the descriptor-pinned transaction stage."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    displaced_stage = tmp_path / ".displaced-transaction"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    real_publish = routes._publish_staged_copy

    def _replace_then_publish(stage_dir, target_dir):
        stage_dir.rename(displaced_stage)
        stage_dir.mkdir()
        (stage_dir / "requirements.md").write_text("# attacker payload")
        return real_publish(stage_dir, target_dir)

    monkeypatch.setattr(routes, "_publish_staged_copy", _replace_then_publish)

    result, _created = routes._write_and_publish_duplicate(
        stage, target, {"requirements.md": "# intended payload"}, token
    )

    assert result == "identity_mismatch"
    assert (target / "requirements.md").read_text() == "# attacker payload"
    assert (displaced_stage / "requirements.md").read_text() == "# intended payload"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_refuses_a_stage_replaced_before_document_creation(tmp_path, monkeypatch):
    """Copied documents are written only through the reservation's stage inode."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    displaced_stage = tmp_path / ".displaced-transaction"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    expected_identity = routes._duplicate_stage_identity(stage, token)
    assert expected_identity is not None
    real_create = routes._create_spec_doc

    def _replace_then_create(*args, **kwargs):
        stage.rename(displaced_stage)
        stage.mkdir()
        (stage / routes._DUPLICATE_MARKER).write_text(token)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _replace_then_create)

    result, _created = routes._write_and_publish_duplicate(
        stage,
        target,
        {"requirements.md": "# intended payload"},
        token,
        expected_identity,
    )

    assert result == "identity_mismatch"
    assert not (stage / "requirements.md").exists()
    assert (displaced_stage / routes._DUPLICATE_MARKER).read_text() == token


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_publishes_a_complete_duplicate_staged_by_a_dead_process(
    tmp_path, monkeypatch
):
    """A crash after all documents are staged must not hide the copy forever.

    Custom ``base_path`` roots are not part of folder discovery, so dropping the
    reservation would strand the files while their name remains unusable.
    """
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    documents = {"requirements.md": "# copied", "design.md": ""}
    for fname, text in documents.items():
        (stage / fname).write_text(text)
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": {
                        fname: routes._sha256_text(text) for fname, text in documents.items()
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" in loaded
    assert routes._DUPLICATING not in loaded["copy"]
    assert not stage.exists()
    assert (target / "requirements.md").read_text() == "# copied"
    assert (target / "design.md").read_text() == ""


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_refuses_a_stage_replaced_at_the_publication_boundary(
    tmp_path, monkeypatch
):
    """Recovery must publish the inode whose marker and manifest it validated."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    displaced_stage = target.parent / ".displaced-transaction"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# intended payload")
    identity = _stage_identity(stage)
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **identity,
                    "documents": {
                        "requirements.md": routes._sha256_text("# intended payload"),
                    },
                },
            }
        }
    )
    real_publish = routes._publish_staged_copy

    def _replace_then_publish(stage_dir, target_dir):
        stage_dir.rename(displaced_stage)
        stage_dir.mkdir()
        (stage_dir / "requirements.md").write_text("# attacker payload")
        return real_publish(stage_dir, target_dir)

    monkeypatch.setattr(routes, "_publish_staged_copy", _replace_then_publish)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert (target / "requirements.md").read_text() == "# attacker payload"
    assert (displaced_stage / "requirements.md").read_text() == "# intended payload"


def test_startup_recovery_does_not_adopt_a_competing_target_after_identity_check_crash(
    tmp_path, monkeypatch
):
    """An ambiguous post-rename crash remains reserved instead of adopting files."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    displaced_stage = target.parent / ".displaced-transaction"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# intended payload")
    identity = _stage_identity(stage)
    stage.rename(displaced_stage)
    target.mkdir()
    (target / "requirements.md").write_text("# attacker payload")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **identity,
                    "documents": {
                        "requirements.md": routes._sha256_text("# intended payload"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert routes._DUPLICATING in loaded["copy"]
    assert (target / "requirements.md").read_text() == "# attacker payload"
    assert (displaced_stage / routes._DUPLICATE_MARKER).read_text() == token


def test_startup_recovery_preserves_a_spec_with_unproven_duplicate_metadata(tmp_path, monkeypatch):
    """Untrusted reservation metadata is not proof that the entry is disposable."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = tmp_path / "custom" / "real-spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# keep me")
    token = "d" * 32
    routes._save_index(
        {
            "real-spec": {
                "working_dir": str(tmp_path),
                "spec_dir": str(spec_dir),
                "status": "planning",
                "slot_key": "spec-builder-real-spec-deadbeef",
                "approvals": {"requirements": {"hash": "a" * 64}},
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(spec_dir.parent / f".real-spec.duplicate-{token}"),
                    "documents": {
                        "requirements.md": routes._sha256_text("# keep me"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "real-spec" in loaded
    assert routes._DUPLICATING not in loaded["real-spec"]
    assert loaded["real-spec"]["approvals"] == {"requirements": {"hash": "a" * 64}}
    assert (spec_dir / "requirements.md").read_text() == "# keep me"


def test_startup_recovery_preserves_an_unproven_reservation_with_no_files(tmp_path, monkeypatch):
    """Well-shaped metadata alone cannot prove that recovery may delete a spec."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "offline" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    approvals = {"requirements": {"hash": "a" * 64}}
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                "approvals": approvals,
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" in loaded
    assert routes._DUPLICATING not in loaded["copy"]
    assert loaded["copy"]["approvals"] == approvals


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
@pytest.mark.parametrize("target_exists_before_recovery", [False, True])
def test_startup_recovery_discards_duplicate_when_an_external_target_wins(
    tmp_path, monkeypatch, target_exists_before_recovery
):
    """A proven duplicate must not adopt a competing writer's target files."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# copied")
    if target_exists_before_recovery:
        target.mkdir()
        (target / "requirements.md").write_text("# external")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    def _concurrent_writer_wins(stage_dir, target_dir):
        assert not target_exists_before_recovery
        target_dir.mkdir()
        (target_dir / "requirements.md").write_text("# external")
        return "conflict"

    monkeypatch.setattr(routes, "_publish_staged_copy", _concurrent_writer_wins)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert (target / "requirements.md").read_text() == "# external"
    assert stage.is_dir()
    assert not any(stage.iterdir())


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires descriptor-pinned cleanup",
)
@pytest.mark.parametrize("external_target", [False, True])
def test_startup_recovery_cleanup_stays_on_the_proven_stage_inode(
    tmp_path, monkeypatch, external_target
):
    """Cleanup cannot unlink files from a replacement at the reserved stage name."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    displaced_stage = target.parent / ".displaced-transaction"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# copied")
    if external_target:
        target.mkdir()
        (target / "requirements.md").write_text("# external")
    documents = {"requirements.md": routes._sha256_text("# copied")}
    if not external_target:
        documents["design.md"] = routes._sha256_text("# missing")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": documents,
                },
            }
        }
    )
    real_clear = routes._clear_duplicate_stage_documents_at

    def _replace_then_clear(dir_fd, marker_token, manifest):
        stage.rename(displaced_stage)
        stage.mkdir()
        (stage / "requirements.md").write_text("# unrelated")
        return real_clear(dir_fd, marker_token, manifest)

    monkeypatch.setattr(routes, "_clear_duplicate_stage_documents_at", _replace_then_clear)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert (stage / "requirements.md").read_text() == "# unrelated"
    assert not (displaced_stage / "requirements.md").exists()
    assert (displaced_stage / routes._DUPLICATE_MARKER).read_text() == token
    if external_target:
        assert (target / "requirements.md").read_text() == "# external"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_keeps_a_duplicate_published_by_a_dead_process(tmp_path, monkeypatch):
    """The exact post-rename/pre-index crash window keeps the visible copy."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    target.mkdir(parents=True)
    (target / routes._DUPLICATE_MARKER).write_text(token)
    (target / "requirements.md").write_text("# copied")
    stage = target.parent / f".copy.duplicate-{token}"
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(target),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" in loaded
    assert routes._DUPLICATING not in loaded["copy"]
    assert (target / "requirements.md").read_text() == "# copied"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_discards_an_incomplete_duplicate_stage_from_a_dead_process(
    tmp_path, monkeypatch
):
    """A marker-provenanced partial stage is transaction residue, not user data."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# only the first document landed")
    documents = {
        "requirements.md": routes._sha256_text("# only the first document landed"),
        "design.md": routes._sha256_text("# never written"),
    }
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": documents,
                },
            }
        }
    )

    real_save = routes._save_index

    def _assert_cleanup_order(index):
        if "copy" not in index:
            assert routes._duplicate_marker_matches(stage, token)
            assert not (stage / "requirements.md").exists()
        real_save(index)

    monkeypatch.setattr(routes, "_save_index", _assert_cleanup_order)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert stage.is_dir()
    assert not any(stage.iterdir()), "the abandoned transaction left hidden copied documents"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires descriptor-pinned cleanup",
)
@pytest.mark.parametrize("external_target", [False, True])
def test_startup_recovery_preserves_a_mismatched_staged_document(
    tmp_path, monkeypatch, external_target
):
    """A marker proves the stage directory, not ownership of replaced files."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    moved_file = tmp_path / "user-requirements.md"
    moved_file.write_text("# user file moved into the abandoned stage")
    moved_file.replace(stage / "requirements.md")
    if external_target:
        target.mkdir()
        (target / "requirements.md").write_text("# external target")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert routes._DUPLICATING in loaded["copy"]
    assert (stage / "requirements.md").read_text() == "# user file moved into the abandoned stage"
    if external_target:
        assert (target / "requirements.md").read_text() == "# external target"


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
    assert (
        routes._DELETING in loaded["live"]
    ), "this process's own in-flight reservation was released by its own read"


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
    assert (
        routes._DELETING not in on_disk["orphaned"]
    ), "the stale reservation is still on disk after a mutation"


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
    """Source guard for the CLASS, not the reported instances. Every handler
    awaits somewhere, so none of them may call _save_index directly: the only
    sanctioned writers are the re-reading mutator, startup recovery, and the
    discovery loader."""

    src = routes_source()
    writers = [
        ln.strip()
        for ln in src.splitlines()
        if "_save_index(" in ln and not ln.strip().startswith(("#", "*", "def _save_index"))
    ]
    # Each sanctioned writer holds _INDEX_LOCK around its read-modify-write.
    assert len(writers) == 3, f"unexpected _save_index call sites: {writers}"
    for handler in (
        routes._handle_create,
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_stop_execution,
        routes._handle_delete,
        routes._handle_list,
    ):
        assert "_save_index(" not in inspect.getsource(
            handler
        ), f"{handler.__name__} writes the index directly; it must go through _mutate_index"


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
    assert (
        "_remove_nudge_loop_for_slot(" in helper
    ), "the release leaves the armed nudge loop running"
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
    assert src[claim:dispatch].count("_release(") == 11, (
        "an abort arm does not release the loop, the recorded state and the slot"
        " — every early return between the claim and the dispatch must release,"
        " including both same-slot busy checks and the final alias check"
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
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
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
    assert not (
        spec_dir / routes._STOP_FILE
    ).exists(), "a stale Stop wrote a STOP into the replacement's directory"
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
    assert re.search(
        r"asyncio\.to_thread\(\s*_write_stop_sentinel_for_spec", src
    ), "the STOP write no longer rides the identity-pinned wrapper off-loop"
    assert "\n    _write_stop_sentinel(" not in src, "sentinel still written on the event loop"


def test_handoff_does_no_filesystem_work_on_the_loop():
    """Sibling of the same class: the handoff endpoint stat-ed tasks.md, unlinked
    a stale sentinel and resolved a realpath inline. All three ride one hop."""

    src = inspect.getsource(routes._handle_handoff)
    # Whitespace-insensitive: the call spans lines, and a literal anchor that
    # a reformat can break is a guard that silently stops guarding.
    assert re.search(
        r"asyncio\.to_thread\(\s*_prepare_handoff", src
    ), "handoff no longer hands _prepare_handoff to a worker thread"
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


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n\n\t\n",
        "# Tasks\n\nProse with no checkbox at all.\n",
        "- [x] done\n- [X] also done\n",
        "- [ x] not an empty box\n",
        "-[ ] no space after the marker\n",
    ],
)
def test_open_task_predicate_rejects_a_plan_with_nothing_to_do(body):
    """A tasks.md the autonomous prompt cannot act on: the prompt tells the agent
    to work through each UNCHECKED task, so an empty file, prose, or an
    all-checked list leaves the run with no work."""
    assert routes._has_open_task(body) is False


@pytest.mark.parametrize(
    "body",
    [
        "- [ ] task",
        "* [ ] star marker",
        "+ [ ] plus marker",
        "1. [ ] ordered marker",
        "2) [ ] paren-ordered marker",
        "- [] bare brackets",
        "  - [ ] indented under a heading",
        "\t- [ ] tab-indented",
        "- [x] done first\n- [ ] then this one is open\n",
    ],
)
def test_open_task_predicate_accepts_the_marker_styles_a_model_writes(body):
    """The list is model-written and its marker style varies between runs, so the
    gate accepts every Markdown checkbox spelling rather than only ``- [ ]``."""
    assert routes._has_open_task(body) is True


def test_prepare_handoff_refuses_a_tasks_file_with_no_open_task(tmp_path):
    """The reported gap: the gate stat-ed tasks.md and accepted it on existence
    alone, so a zero-byte or all-checked plan armed an autonomous loop that had
    nothing to act on while the spec still read as a finished Tasks phase."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    tasks = spec_dir / "tasks.md"

    tasks.write_text("")
    assert routes._prepare_handoff(spec_dir)[0] is False, "empty tasks.md armed a run"

    tasks.write_text("- [x] everything already done\n")
    assert routes._prepare_handoff(spec_dir)[0] is False, "all-checked tasks.md armed a run"

    tasks.write_text("- [x] done\n- [ ] still open\n")
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

    tree = ast.parse(routes_source())
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
    monkeypatch.setattr(
        routes, "sandboxed_spawn_argv", lambda argv: ([sys.executable, "-c", ""], None, "")
    )


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
    monkeypatch.setattr(routes, "_CAN_PUBLISH_DIR_NOREPLACE", False)
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
    assert body["duplicate_supported"] is False


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
        assert (
            acquisition not in src[:arbitration]
        ), "create acquires the shared slot before the index arbitration"
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
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-s",
            }
        }
    )

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
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-s",
            }
        }
    )

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
    assert (
        slot.project == "/projects/thing"
    ), "slot has no project — tools would run in the wrong dir"
    assert slot._titled is True


def test_every_slot_acquisition_goes_through_the_scoping_chokepoint():
    """Source guard: no handler may call get_or_create_slot directly, or a future
    endpoint reintroduces an unscoped slot."""

    src = routes_source()
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
        assert "_ensure_worker_slot(" in inspect.getsource(
            handler
        ), f"{handler.__name__} does not scope the slot it uses"


# ── GPT round-12 findings (#518) ─────────────────────────────────────────────


def test_no_handler_reads_the_index_on_the_event_loop():
    """The reported stall, and the last member of this class: _load_index is a
    file read plus a JSON parse, and the detail endpoint is polled every 2.5s
    during a build — so on a stalled data home the gateway froze repeatedly.
    Handlers read through _aload_index; the sync primitive stays for the
    transaction helpers, which already run on a worker thread."""

    module_src = routes_source()
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
        "_aload_index_with_slot_identity",
        "_aload_index_with_decision_alias_status",
        "_mutate_index",
        "_load_index_with_discovery",
        "_recover_abandoned_reservations",
        "_load_index",
        "_prepare_handoff",
        "_write_stop_sentinel_for_spec",
        "_claim_decision_locked",
        "_forget_decisions_locked",
        "_alias_slots_locked",
    }
    for off_loop in (
        "_aload_index_with_slot_identity",
        "_aload_index_with_decision_alias_status",
        "_load_index_with_discovery",
        "_recover_abandoned_reservations",
        "_prepare_handoff",
        "_write_stop_sentinel_for_spec",
        "_claim_decision_locked",
        "_forget_decisions_locked",
        "_alias_slots_locked",
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
        source = inspect.getsource(handler)
        assert (
            "await _aload_index()" in source or "await _aload_index_with_slot_identity(" in source
        ), f"{handler.__name__} does not read the index off the loop"


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
    (home / "recent_projects.json").write_text(json.dumps([str(real), str(tmp_path / "gone"), 42]))
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
    code = [ln for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#")]
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
        assert stripped.startswith(
            "state.conversation_log.read_messages,"
        ), f"read_messages still called on the loop: {stripped}"
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


@pytest.mark.asyncio
async def test_default_model_is_stamped_on_a_bare_slot(monkeypatch, tmp_path):
    """The app-wide default model from settings lands on a worker slot that has
    no explicit pick — the single creation chokepoint, same place project and
    title are stamped."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": "test-model-x"})

    class _New:
        key = "spec-builder-fresh"
        _app = ""
        project = ""
        model = ""
        _titled = False

    fresh = _New()

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return fresh

    out = await routes._ensure_worker_slot(_State(), "fresh", {"working_dir": "/new"})
    assert out is fresh
    assert fresh.model == "test-model-x", "the app default was not stamped"


@pytest.mark.asyncio
async def test_default_model_never_overwrites_an_explicit_slot_pick(monkeypatch, tmp_path):
    """A per-slot model set through the chat API stays authoritative: the ensure
    chokepoint runs on EVERY dispatch, so an unconditional stamp would silently
    revert an explicit pick on the next message — the main defect to avoid."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": "test-model-x"})

    class _Ours:
        key = "spec-builder-mine"
        _app = routes.APP_NAME
        project = ""
        model = "test-model-explicit"
        _titled = True

    ours = _Ours()

    class _State:
        def get_slot(self, key):
            return ours

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("should not recreate an existing owned slot")

    out = await routes._ensure_worker_slot(_State(), "mine", {"working_dir": "/p"})
    assert out is ours
    assert ours.model == "test-model-explicit", "an explicit per-slot pick was overwritten"


@pytest.mark.asyncio
async def test_empty_default_model_leaves_the_slot_inheriting(monkeypatch, tmp_path):
    """'' = inherit: with no app default configured, the slot's model stays
    empty and the session layer's resolution chain applies unchanged."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": ""})

    class _New:
        key = "spec-builder-plain"
        _app = ""
        project = ""
        model = ""
        _titled = False

    fresh = _New()

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return fresh

    out = await routes._ensure_worker_slot(_State(), "plain", {"working_dir": "/new"})
    assert out is fresh
    assert fresh.model == "", "an empty default must not stamp anything"


@pytest.mark.asyncio
async def test_default_model_is_not_stamped_on_an_adopted_slot(monkeypatch, tmp_path):
    """A slot that already exists (restored across a gateway restart, or simply
    re-ensured on a later dispatch) keeps running exactly as it was: the help
    copy promises a changed default applies to spec sessions started AFTER the
    change, so only a slot this call CREATES may receive the stamp."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": "test-model-x"})

    class _Restored:
        key = "spec-builder-mine"
        _app = routes.APP_NAME
        project = ""
        model = ""  # inheriting, and it must STAY inheriting
        _titled = True

    ours = _Restored()

    class _State:
        def get_slot(self, key):
            return ours

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("should not recreate an existing owned slot")

    out = await routes._ensure_worker_slot(_State(), "mine", {"working_dir": "/p"})
    assert out is ours
    assert ours.model == "", "an adopted slot was re-stamped with the app default"


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

    tree = ast.parse(routes_source())
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
    routes._save_index({"moved": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}})

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
    routes._save_index({"steady": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

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
        {
            "done": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "status": "executing",
            }
        }
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
    routes._save_index({"swap": {"spec_dir": str(old_dir), "working_dir": str(tmp_path / "old")}})

    def _prepare_then_recreate(spec_dir, name="", expect_slot_key=""):
        # Mirrors the real signature: the handler now passes the name and the
        # identity it started with so the CLEAR itself can be refused.
        routes._forget_observed_slot_identity("swap", expect_slot_key)
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
    check = src.index("!= str(spec_dir)")
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
        routes._save_index({"t": {"spec_dir": str(new_dir), "working_dir": str(tmp_path / "new")}})
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
        assert (
            "only_loop_id=" in src and "only_slot=" in src
        ), f"{handler.__name__} does not pin its cleanups"
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
            assert inline not in stripped, f"spawn setup still runs on the event loop: {stripped}"
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
    # inline in remove() itself or in the transaction-owned helper it delegates
    # to. Either shape satisfies the app, so inspect the complete call path.
    src = inspect.getsource(an.AutoNudgeService.remove) + inspect.getsource(
        an.AutoNudgeService._remove_unserialized
    )
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
    assert (
        "design.md" not in quick.split("Do NOT write design.md")[0]
    ), "quick spec is still told to write design.md"
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
    routes._save_index({"boom": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

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
        assert (
            await routes._halt_active_turn(_state_for(slot), "shared") is False
        ), f"stopped a turn on a slot owned by {owner!r}"
        assert stopped == [] and cancelled["v"] is False

    # Our own slot is still stopped.
    ours = _Slot(routes.APP_NAME)
    assert await routes._halt_active_turn(_state_for(ours), "shared") is True
    assert stopped and cancelled["v"] is True


def test_every_ownership_check_is_exact():
    """Source guard for the class: no ownership comparison may treat an unscoped
    slot as ours."""
    src = routes_source()
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
        {
            "chatty": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-chatty",
            }
        }
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
    assert (
        slots.get("spec-builder-chatty") is existing
    ), "a pre-existing conversation was destroyed by the failed handoff"


@pytest.mark.asyncio
async def test_delete_commits_the_index_before_closing_the_session(tmp_path, monkeypatch):
    """The reported ordering bug: the teardown ran BEFORE the index pop, so a
    failing index write (disk full) returned 500 with the spec still listed and its
    conversation already discarded -- unusable and unrecoverable."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keepme"
    routes._save_index({"keepme": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

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

    assert (
        slots.get("spec-builder-keepme") is slot
    ), "the session was discarded before the delete was committed"


def test_delete_orders_reserve_teardown_remove():
    """Source guard for the ordering the delete path depends on: the name is RESERVED
    before the session is torn down (so it cannot be taken while archival runs, and a
    rollback lands on the original entry with its own slot key), and the entry is only
    removed once the archive succeeded."""
    src = inspect.getsource(routes._handle_delete)
    reserve = src.index("_mark_deleting(")
    teardown = src.index("_teardown_worker_slot(")
    remove = src.index("await _mutate_index(", src.index("def _pop_if_same"))
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
    release = src[start : src.index('_audit("spec_handoff_aborted"', start)]
    assert (
        "if owned and not slot_pre_existed:" in release
    ), "unwind tears down a slot it may not have created"


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
    assert (
        await routes._ensure_worker_slot(_State(), "x", {"working_dir": str(tmp_path / "gone")})
        is None
    )
    assert slot.project == ""

    # Ordinary project -> accepted and scoped.
    assert await routes._ensure_worker_slot(_State(), "x", {"working_dir": str(ordinary)}) is slot
    assert slot.project == str(ordinary)


def test_working_dir_validation_is_offloaded():
    """Source guard: the validation must not stat on the event loop, and it must
    run BEFORE the assignment it protects."""
    src = inspect.getsource(routes._ensure_worker_slot)
    assert "asyncio.to_thread(_safe_dir" in src
    assert src.index("safe_wd = await asyncio.to_thread") < src.index(
        "slot.project ="
    ), "the cwd is assigned before it is validated"
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
        routes._forget_observed_slot_identity(name, str(meta.get("slot_key", "")))
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
    assert routes._load_index()["racy"]["spec_dir"] == str(
        other
    ), "the unwind deleted the replacement spec's index entry"


def test_create_unwind_is_identity_pinned():
    """Source guard: the create unwind must not pop by name alone, and the identity
    recheck must precede the dispatch."""
    src = inspect.getsource(routes._handle_create)
    assert "idx.pop(name, None)" not in src, "create unwinds by name alone"
    assert "_pop_if_ours" in src
    recheck = src.index("!= str(spec_dir)")
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
        assert routes._load_settings() == {"base_path": "", "model": ""}, bad
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
        json.dumps(
            {"good": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}, "bad": None}
        )
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
    assert "expect_spec_dir=str(index[name]" not in src

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

    assert (ex.status, st.status, rm.status) == (
        409,
        409,
        409,
    ), f"a stale control was honoured: {(ex.status, st.status, rm.status)}"
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
    assert [m["content"] for m in slot.messages] == [
        "build me a spec",
        "on it",
    ], "the persisted conversation did not come back"
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
    assert body.index("_restore_worker_transcript") < body.index(
        "get_or_create_slot"
    ), "the empty slot is created before the transcript is restored"


# ── GPT round-37 findings + scrub/CodeQL fallout (#518) ──────────────────────


def test_settings_reader_normalizes_a_non_string_base_path(tmp_path, monkeypatch):
    """The reported crash: _load_settings validated the OUTER shape only, so
    ``{"base_path": []}`` passed as a dict and every reader then called .strip()
    on a list -- 500ing spec creation and the settings endpoint."""
    _redirect_state(monkeypatch, tmp_path)
    routes._settings_path().parent.mkdir(parents=True, exist_ok=True)

    for bad in (
        '{"base_path": []}',
        '{"base_path": 7}',
        '{"base_path": null}',
        '{"base_path": {}}',
    ):
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
        assert (
            routes._opted_in({"use_worktree": truthy_but_not_true}, "use_worktree") is False
        ), f"{truthy_but_not_true!r} opted in"
    assert routes._opted_in({"use_worktree": True}, "use_worktree") is True
    assert routes._opted_in({}, "import_existing") is False

    # Source guard: no handler may go back to coercing these flags with bool().
    src = routes_source()
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
        assert (
            await routes._ensure_worker_slot(state, bad, {"working_dir": str(tmp_path)}) is None
        ), bad
        assert state.touched == [], f"{bad[:20]!r} reached the slot layer"


def test_browse_skip_list_carries_no_hidden_paths():
    """_scan_subdirs already skips every entry starting with '.', so listing
    dotted names in the skip set duplicated that rule -- and one of them put a
    literal internal path marker in the source, which the repo's scrub lint
    rejects."""
    assert not [n for n in routes._BROWSE_SKIP if n.startswith(".")]
    assert 'startswith(".")' in inspect.getsource(
        routes._scan_subdirs
    ), "the hidden-entry skip that makes the dotted names redundant is gone"


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
    between = src[src.rindex("_client_identity_mismatch(claimed", 0, capture_at) : capture_at]
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
        deciding_read = max(
            src.rfind("_aload_index()"),
            src.rfind("_aload_index_with_slot_identity("),
        )
        assert deciding_read >= 0, f"{handler.__name__} has no indexed identity read"
        assert (
            src.index("claimed = await _client_claim(request)") < deciding_read
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
    assert routes._CAN_PIN_DIR is (hasattr(os, "O_DIRECTORY") and os.unlink in os.supports_dir_fd)
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
    routes._index_path().write_text(
        json.dumps(
            {
                "demo": {},  # the reported shape
                "blank": {"spec_dir": "   "},
                "typed": {"spec_dir": []},
                "ok": good,
            }
        )
    )

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
    routes._save_index({"doomed": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    async def _boom(*_a, **_k):
        raise OSError("autonudge store is read-only")

    monkeypatch.setattr(routes, "_remove_nudge_loop", _boom)
    torn_down: list[str] = []
    monkeypatch.setattr(routes, "_teardown_worker_slot", lambda *a, **k: _noop_await(torn_down))

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

    await client.start_server()
    try:
        client.app["state"] = None
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
            return types.SimpleNamespace(key=name, _app=app, project="", messages=[], _titled=False)

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
    assert (
        "_ensure_worker_slot(state, name, entry, adopt_closed=False)" in src
    ), "create can adopt a deleted conversation again"


@pytest.mark.asyncio
async def test_delete_restores_the_spec_when_archiving_fails(tmp_path, monkeypatch):
    """The reported data loss: the closing history write was best-effort, so a
    failed archive was logged at DEBUG while DELETE returned 200 -- the spec gone
    from the index and the conversation never written. The entry now goes back and
    the caller is told to retry."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "keepme"
    spec_dir.mkdir(parents=True)
    entry = {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd"), "spec_type": "plan"}
    routes._save_index({"keepme": entry})

    slot = types.SimpleNamespace(
        key=routes._slot_key("keepme"),
        _app=routes.APP_NAME,
        running=False,
        task=None,
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
    for fn in (routes._load_index_snapshot, routes._save_index):
        assert "_refresh_slot_keys" in inspect.getsource(fn), fn.__name__


def test_detail_reports_the_slot_key_it_scoped(tmp_path, monkeypatch):
    """The SPA must not derive the slot key from the name -- with per-creation keys
    a reused name would mount the embed on the previous spec's transcript. The
    detail payload therefore names the session the app itself scoped."""
    src = inspect.getsource(routes._handle_get)
    assert (
        '"slot_key": getattr(slot, "key", None) or _slot_key(name)' in src
    ), "detail no longer tells the client which slot to mount"


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
        assert not (
            victim / routes._STOP_FILE
        ).exists(), "the sentinel write followed a replaced directory and halted another spec"
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
    src = routes_source()
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
        n
        for n, v in backend_namespace().items()
        if n.endswith("_PATH") and isinstance(v, Path) and n != "SPEC_STATE_PATH"
    }
    redirected = set(
        re.findall(r'setattr\(routes, "(\w+_PATH)"', inspect.getsource(_redirect_state))
    )
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
    assert (
        routes._client_identity_mismatch(routes._ClientClaim(same_dir, old_key), same_dir, new_key)
        is True
    )
    # Same creation -> allowed.
    assert (
        routes._client_identity_mismatch(routes._ClientClaim(same_dir, new_key), same_dir, new_key)
        is False
    )
    # A directory mismatch still refuses on its own.
    assert (
        routes._client_identity_mismatch(
            routes._ClientClaim("/p/other", new_key), same_dir, new_key
        )
        is True
    )
    # Unpinned stays unpinned: an older tab sends neither field.
    assert routes._client_identity_mismatch(routes._ClientClaim("", ""), same_dir, new_key) is False
    # A server-side entry with no key yet cannot refuse on the key.
    assert (
        routes._client_identity_mismatch(routes._ClientClaim(same_dir, old_key), same_dir, "")
        is False
    )


@pytest.mark.asyncio
async def test_client_claim_reads_body_and_query(tmp_path, monkeypatch):
    """Both fields travel in the body for POSTs and the query string for DELETE."""
    client = _make_client(monkeypatch, tmp_path)
    seen: list[_ClientClaim] = []

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
        (
            "indexed status",
            {
                "busy": {
                    "spec_dir": str(spec_dir),
                    "working_dir": str(tmp_path / "wd"),
                    "status": "executing",
                }
            },
            False,
        ),
        (
            "live slot",
            {"busy": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}},
            True,
        ),
    ):
        # The client redirects state, so the index must be written AFTER it exists
        # (the autouse fixture points elsewhere until then).
        client = _make_client(monkeypatch, tmp_path)
        routes._save_index(index)
        slot = types.SimpleNamespace(
            key=routes._slot_key("busy"),
            _app=routes.APP_NAME,
            running=slot_running,
            project="",
            messages=[],
            _titled=True,
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
    routes._save_index({"s": {"spec_dir": same_dir, "slot_key": "spec-builder-s-bbbb2222"}})

    # Stale claim: right directory, previous creation -> refused.
    assert (
        await routes._touch_spec(
            "s",
            expect_spec_dir=same_dir,
            expect_slot_key="spec-builder-s-aaaa1111",
            status="executing",
        )
        is None
    )
    assert routes._load_index()["s"].get("status") != "executing", "the stale stamp landed"

    # Current creation -> accepted.
    fresh = await routes._touch_spec(
        "s",
        expect_spec_dir=same_dir,
        expect_slot_key="spec-builder-s-bbbb2222",
        status="executing",
    )
    assert fresh is not None and fresh["status"] == "executing"

    # An entry predating slot keys carries none and cannot be pinned on it.
    routes._save_index({"old": {"spec_dir": same_dir}})
    assert (
        await routes._touch_spec(
            "old",
            expect_spec_dir=same_dir,
            expect_slot_key="spec-builder-old-1",
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
        {
            "nope": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": "spec-builder-nope-1234abcd",
            }
        }
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
        key=routes._slot_key("nope"),
        _app=routes.APP_NAME,
        running=False,
        project="",
        messages=[],
        _titled=True,
        task=None,
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
    captured_slot_key = "spec-builder-gone-1234abcd"
    routes._save_index(
        {
            "gone": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": captured_slot_key,
            }
        }
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
            return _Loop() if self.armed and key == captured_slot_key else None

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
        key=routes._slot_key("gone"),
        _app=routes.APP_NAME,
        running=False,
        project="",
        messages=[],
        _titled=True,
        task=None,
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
    assert 'meta["exec_arming_at"] = now' in inspect.getsource(
        routes._claim_execution
    ), "the claim no longer marks the pre-arm window"
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

    # Delete publishes its reservation before tearing down the slot. A handoff
    # queued behind that boundary must not claim the hidden entry while teardown
    # is in flight.
    deleting = routes._load_index()
    deleting["s"][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
    routes._save_index(deleting)
    reason, _entry = await routes._claim_execution(
        "s", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-new", live_running=False
    )
    assert reason == routes._CLAIM_GONE

    # A live turn on the slot counts as taken even when the index says planning.
    deleting["s"].pop(routes._DELETING)
    routes._save_index(deleting)
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
    routes._save_index({"bye": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})
    order: list[str] = []
    real_remember = routes._remember_deleted

    def _spy(d):
        order.append("tombstone")
        real_remember(d)

    monkeypatch.setattr(routes, "_remember_deleted", _spy)

    original_mutate = routes._mutate_index

    async def _watched(fn, **kwargs):
        result = await original_mutate(fn, **kwargs)
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
    routes._save_index({"stay": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

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
    assert (
        str(spec_dir) not in routes._load_deleted()
    ), "a spec that still exists is tombstoned; discovery will hide its documents"


def test_delete_orders_the_tombstone_before_the_pop():
    """Source guard: the tombstone write must precede the index pop, and every
    non-deleting arm must clear it."""
    src = inspect.getsource(routes._handle_delete)
    remember = src.index("_remember_deleted")
    pop = src.index("await _mutate_index(", src.index("def _pop_if_same"))
    assert remember < pop, "the entry is dropped before the directory is tombstoned"
    assert src.count("_forget_deleted") >= 2, "a non-deleting arm leaves the tombstone behind"


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
        threading.Thread(target=routes._remember_deleted, args=(f"/p/spec-{i}",)) for i in range(2)
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
    assert (
        "/p/spec-0" in recorded and "/p/spec-1" in recorded
    ), f"a concurrent delete lost its tombstone: {recorded}"
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
        key="spec-builder-reused-99999999",
        _app=routes.APP_NAME,
        running=False,
        project="",
        messages=[],
        _titled=True,
        task=None,
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
        routes._forget_observed_slot_identity(name, str(meta.get("slot_key", "")))
        routes._save_index({"reused": dict(replacement)})
        return got

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_then_replace)

    await client.start_server()
    try:
        client.app["state"] = _State()
        resp = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "reused",
                "working_dir": str(working),
                "spec_type": "feature",
                "import_existing": True,
            },
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
    pop_body = src[pop : src.index("del idx[name]", pop)]
    assert "slot_key" in pop_body, "the unwind pops on the directory alone"
    post = src.index("live = current.get(name) or {}")
    assert (
        "slot_key" in src[post : src.index("_unwind_create()", post)]
    ), "the post-slot-setup check compares the directory alone"


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
                assert (
                    "dir_fd" in stripped
                ), f"{fn.__name__} touches the sentinel by path: {stripped}"
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
    assert (
        calls and calls[0]["critical"] is True
    ), f"the invocation audit did not ask for a durable write: {calls}"


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
        {
            "keeper": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "slot_key": key,
            }
        }
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
        {
            "busy": {
                "spec_dir": str(spec_dir),
                "working_dir": str(working),
                "slot_key": "spec-builder-busy-11112222",
            }
        }
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
    routes._save_index({"stuck": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    async def _ok_teardown(*_a, **_kw):
        return True

    real_mutate = routes._mutate_index
    calls = {"n": 0}

    async def _fail_the_removal(fn):
        calls["n"] += 1
        # The mark and durable teardown boundary succeed; the removal does not.
        if calls["n"] >= 3:
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
    monkeypatch.setattr(routes, "_PROCESS_ID", "replacement-process")
    restarted = routes._load_index()["stuck"]
    assert restarted.get(routes._DELETING), "restart exposed the torn-down spec"


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
        {
            "odd": {
                "spec_dir": str(base / "odd"),
                "working_dir": str(tmp_path / "wd"),
                "status": "AKIAIOSFODNN7EXAMPLE",
            }
        }
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
    monkeypatch.setattr(routes, "_prepare_git_spawn", lambda argv: (list(argv), {}, None))

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
    tree = ast.parse(routes_source())
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_git"
    )
    reraise_arms = [
        handler
        for handler in (h for t in ast.walk(target) if isinstance(t, ast.Try) for h in t.handlers)
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
        def __init__(self):
            self._background_tasks: set[asyncio.Task] = set()

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
    assert (
        await routes._ensure_worker_slot(state, "s", meta) is None
    ), "a projectless entry was handed a slot"
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
        assert (
            await routes._ensure_worker_slot(state, "s", meta) is None
        ), f"{label} working_dir was allowed to produce a slot"

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
    routes._save_index(
        {
            "doomed": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._new_slot_key("doomed"),
            }
        }
    )

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
    routes._save_index(
        {
            "doomed": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._new_slot_key("doomed"),
            }
        }
    )
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
    # Every decision-delivery await after the re-pin is recoverable: the claim writes a
    # pending outbox entry, and the delivery helper finalizes it only after relay. The
    # directory turn lock still excludes delete and other Spec Builder dispatches.
    tail = between[between.index("_touch_spec(") :]
    awaits = [
        ln.strip() for ln in tail.splitlines() if "await " in ln and not ln.strip().startswith("#")
    ]
    allowed = (
        "asyncio.to_thread(",
        "_claim_decision(",
        "_pending_decisions(",
        "_deliver_pending_decision(",
        "_final_alias_conflict(",
    )
    assert all(
        any(name in ln for name in allowed) for ln in awaits
    ), f"a non-pinning await slipped in before dispatch: {awaits!r}"


@pytest.mark.asyncio
async def test_failed_loop_removal_releases_both_tombstone_and_reservation(tmp_path, monkeypatch):
    """Moving the reservation earlier means the 503 abort now owns two things to
    undo. Leaving either behind would hide a spec the user still has."""
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "keeper").mkdir(parents=True)
    sd = str(base / "keeper")
    routes._save_index(
        {
            "keeper": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._new_slot_key("keeper"),
            }
        }
    )

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
    assert not idx["keeper"].get(
        routes._DELETING
    ), "reservation left behind — spec hidden from the list"
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
    routes._save_index(
        {
            "s": {
                "spec_dir": sd,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": original_key,
            }
        }
    )

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
    unwind = src[src.index("async def _unwind_create") :]
    pop = unwind.index("was_ours = await _mutate_index(")
    call = unwind.index("_rollback_worktree_if_ours(")
    assert pop < call, "ownership must be established before the rollback"
    args = unwind[call : unwind.index(")", unwind.index("worktree_branch=worktree_branch", call))]
    assert "was_ours=was_ours" in args, (
        "the rollback is not gated on the pinned pop -- a stale unwind would "
        "force-delete a replacement spec's worktree and branch"
    )
    # The raw destructive call must NOT survive alongside the gated one.
    assert (
        "_remove_worktree(" not in unwind
    ), "the unwind still calls _remove_worktree directly, bypassing the gate"


def test_remove_worktree_is_destructive_enough_to_need_the_gate():
    """Pins WHY the gate matters. If _remove_worktree ever stops being a
    force-remove + branch delete, the gate can be re-argued rather than assumed."""
    src = inspect.getsource(routes._remove_worktree)
    assert '"worktree", "remove", "--force"' in src
    assert '"branch", "-D"' in src


def test_only_the_post_insert_unwind_needs_the_gate():
    """Scope guard: the six early rollbacks are unconditional on purpose.

    They run before the index insert, so no other request can hold the name --
    and _create_worktree would itself have failed had a concurrent create already
    made `<repo>-wt-<name>`. Gating those would orphan a worktree on every
    legitimate 400/409. Only the post-insert unwind spans an await (the slot
    acquisition) during which a delete + recreate can land.

    The ledger refusals are reached after awaits of their own, but both precede the
    insert, so this create has claimed nothing and ``created_worktree`` is still only
    ever the one it made itself -- the same reasoning that leaves the others ungated.
    """
    src = inspect.getsource(routes._handle_create)
    early = src[: src.index("async def _unwind_create")]
    assert early.count("_remove_worktree(") == 6, (
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

    got = await routes._ensure_worker_slot(state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)})
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

    got = await routes._ensure_worker_slot(state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)})
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

    got = await routes._ensure_worker_slot(state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)})
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
    guards = [
        i for i in range(len(src)) if src.startswith("_slot_identity_moved(name, slot_key)", i)
    ]
    assert len(guards) == 2, f"expected 2 identity re-checks, found {len(guards)}"
    for a in awaits:
        assert any(g > a for g in guards), "an await is not followed by an identity re-check"
    # Creation must use the captured key.
    assert (
        "get_or_create_slot(name=slot_key" in src
    ), "slot creation does not use the captured identity"


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
    capture = src.index('started_slot_key = str(meta.get("slot_key", ""))')
    await_match = re.search(r"await asyncio\.to_thread\(\s*_prepare_handoff", src)
    assert await_match, "handoff no longer hands _prepare_handoff to a worker thread"
    await_at = await_match.start()
    guard = src.index("spec_changed_during_start")
    assert capture < await_at, "the identity is captured after the await it must survive"
    # The claim check must precede the await too: _prepare_handoff CLEARS the STOP
    # sentinel, so a stale execute that reaches it disarms a replacement's Pause
    # before any comparison has run.
    claim_at = src.index("_client_identity_mismatch(claimed, spec_dir, started_slot_key)")
    assert (
        claim_at < await_at
    ), "the client-claim check moved after the sentinel clear it is meant to gate"
    assert await_at < guard, "the reread guard does not follow the await"
    window = src[await_at:guard]
    assert "!= started_slot_key" in window, (
        "the reread is pinned on spec_dir only -- a same-name, same-path " "replacement passes it"
    )
    assert (
        'str(meta.get("spec_dir", "")) != str(spec_dir)' in window
    ), "the directory pin was dropped"


def test_no_index_mutation_is_pinned_on_the_directory_alone():
    """Class guard. Every _touch_spec call that pins spec_dir must also pin
    slot_key -- the two are one identity, and rounds 62 and 68 were both a
    caller passing only half of it."""
    src = routes_source()
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
        call = src[match.start() : i + 1]
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
        assert not (
            tmp_path / ".kiro" / "specs" / _CREDENTIAL_SHAPED_NAME
        ).exists(), "create left a spec directory for a name the index cannot hold"


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
    src = routes_source()
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
        assert (
            routes._safe_dir(relative) is None
        ), f"_safe_dir accepted the relative value {relative!r}"


def test_safe_dir_still_accepts_absolute_and_tilde(tmp_path, monkeypatch):
    """The guard must not cost the two forms that are legitimately absolute."""
    assert routes._safe_dir(str(tmp_path)) == Path(
        os.path.realpath(str(tmp_path))
    ), "_safe_dir rejected a plain absolute directory"

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "inside").mkdir()
    assert (
        routes._safe_dir("~/inside") is not None
    ), "_safe_dir rejected a ~-relative path, which expands to an absolute one"


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
    assert (
        isabs_at < realpath_at
    ), "the absoluteness test moved after realpath, where it can never fail"


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
        self.queued_origins: list[bool] = []

    def queue_append(self, message, **kwargs):
        self.queued.append(message)
        self.queued_origins.append(kwargs["directive_user_origin"])

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


# ── direct authority over the artifacts ──────────────────────────────────────
# Before these endpoints the user could only ASK the agent to change anything:
# a typo cost a model turn, an approval was a chat message that left no trace,
# and the task list could only be handed over whole. Each test below pins the
# guard that makes the new write path safe rather than merely present.


def _seed_spec(tmp_path, name="live", *, files=None, extra=None):
    """Seed one spec on disk and in the index. Returns (spec_dir, working_dir)."""
    working_dir = tmp_path / "wd"
    spec_dir = working_dir / ".kiro" / "specs" / name
    spec_dir.mkdir(parents=True)
    for fname, text in (files or {}).items():
        (spec_dir / fname).write_text(text)
    entry = {
        "spec_dir": str(spec_dir),
        "working_dir": str(working_dir),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": routes._slot_key(name),
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    entry.update(extra or {})
    routes._save_index({name: entry})
    return spec_dir, working_dir


def _spec_identity(name="live"):
    """Return the creation identity a detail response gives the client."""
    entry = routes._load_index()[name]
    return {"spec_dir": entry["spec_dir"], "slot_key": entry["slot_key"]}


class _IdleSlot:
    """A slot of OUR app that is not mid-turn."""

    running = False
    messages: list = []
    _app = routes.APP_NAME
    project = ""
    _titled = False
    title = ""

    def __init__(self, key):
        self.key = key
        self.dispatched: list[str] = []


def _state_for(*names):
    """A fake gateway state serving an idle slot per spec name."""
    slots = {routes._slot_key(n): _IdleSlot(routes._slot_key(n)) for n in names}

    class _State:
        def get_slot(self, key):
            return slots.get(key)

        def get_or_create_slot(self, name, app=""):
            key = routes._slot_key(name)
            slots.setdefault(key, _IdleSlot(key))
            return slots[key]

    return _State(), slots


# ── gap 1: documents are reviewable through their redacted rendering ─────────


def test_read_spec_files_returns_only_the_raw_hash_for_a_redacted_document(tmp_path, monkeypatch):
    """The browser gets safe rendering plus the hash needed for approval."""
    monkeypatch.setattr(routes, "_redact", lambda t: t.replace("sk-secret", "[redacted]"))
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("token is sk-secret")
    (spec_dir / "design.md").write_text("nothing sensitive")

    files, docs, tasks = routes._read_spec_files(spec_dir)

    assert files["requirements.md"] == "token is [redacted]"
    # The hash is of the file AS STORED, never of the redacted copy: an approval
    # binds to the real version that was reviewed.
    assert docs["requirements.md"] == {"hash": routes._sha256_text("token is sk-secret")}
    assert set(docs["design.md"]) == {"hash"}
    assert tasks == []


def test_duplicate_doc_create_only_succeeds_while_the_file_is_absent(tmp_path):
    """O_EXCL makes an external writer the winner rather than its victim."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()

    result, identity = routes._create_spec_doc(spec_dir, "design.md", "# fresh")
    assert result == "" and identity is not None
    assert (spec_dir / "design.md").read_text() == "# fresh"

    result, identity = routes._create_spec_doc(spec_dir, "design.md", "# second")
    assert result == "conflict" and identity is None
    assert (spec_dir / "design.md").read_text() == "# fresh"


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="duplicate document writes require descriptor-relative directory pinning",
)
def test_duplicate_doc_create_refuses_a_spec_directory_swapped_for_a_symlink(tmp_path):
    """Same attack the sentinel writers refuse: an agent replaces its own indexed
    directory with a symlink, and a path-based write then lands user-authored text
    somewhere else entirely."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    attacker = tmp_path / "attacker-spec"
    os.symlink(real, attacker)

    assert routes._create_spec_doc(attacker, "design.md", "# text")[0] == "unsafe_dir"
    assert not (real / "design.md").exists()


def test_duplicate_doc_create_retries_short_writes(tmp_path, monkeypatch):
    """A successful save means the complete UTF-8 payload reached the temporary
    file; a regular-file write is allowed to accept only a prefix."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    real_write = routes.os.write
    writes: list[int] = []

    def _short_write(fd, data):
        chunk = data[:2]
        writes.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(routes.os, "write", _short_write)
    content = "complete payload"

    assert routes._create_spec_doc(spec_dir, "design.md", content)[0] == ""
    assert (spec_dir / "design.md").read_text() == content
    assert len(writes) > 1


# ── gap 2: approval is recorded against the version approved ─────────────────


@pytest.mark.asyncio
async def test_approve_records_the_version_and_the_user(tmp_path, monkeypatch):
    """Approval used to be nothing but a chat message: the server never knew a
    phase had been signed off, by whom, or against which text."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reviewed"})
    digest = routes._sha256_text("# reviewed")

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={**_spec_identity(), "phase": "requirements", "hash": digest},
        )
        assert resp.status == 200, await resp.json()
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
    finally:
        await client.close()

    stored = routes._load_index()["live"]["approvals"]["requirements"]
    assert stored["hash"] == digest and stored["user"] == "tester" and stored["at"] > 0
    assert detail["approvals"]["requirements"]["stale"] is False


@pytest.mark.asyncio
async def test_approve_refuses_a_hash_that_is_not_the_current_document(tmp_path, monkeypatch):
    """Approving a version nobody has seen records nothing meaningful, so the
    claim is checked against the file rather than trusted."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# actually on disk"})

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={
                **_spec_identity(),
                "phase": "requirements",
                "hash": routes._sha256_text("# something else"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "doc_changed"
    assert "approvals" not in routes._load_index()["live"]


@pytest.mark.asyncio
async def test_approve_refuses_a_same_path_spec_recreated_during_hash_read(tmp_path, monkeypatch):
    """A directory path is reusable; the per-creation slot key is the identity."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reviewed"})
    old_key = routes._load_index()["live"]["slot_key"]
    real_read = routes._read_spec_text
    replaced = False

    def _replace_identity(spec_dir, fname):
        nonlocal replaced
        text = real_read(spec_dir, fname)
        if fname == "requirements.md" and not replaced:
            replaced = True
            index = routes._load_index()
            index["live"]["slot_key"] = old_key + "-replacement"
            routes._save_index(index)
        return text

    monkeypatch.setattr(routes, "_read_spec_text", _replace_identity)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={
                **_spec_identity(),
                "phase": "requirements",
                "hash": routes._sha256_text("# reviewed"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "stale_client"
    assert "approvals" not in routes._load_index()["live"]


def test_approvals_report_stale_once_the_document_moves(tmp_path):
    """Why the approved VERSION is recorded rather than a bare flag: a document the
    agent rewrote after sign-off must stop reading as approved."""
    approved = routes._sha256_text("# as reviewed")
    record = {"requirements": {"hash": approved, "at": 5.0, "user": "tester"}}

    unchanged = routes._normalize_approvals(record, {"requirements.md": {"hash": approved}})
    assert unchanged["requirements"]["stale"] is False

    moved = routes._normalize_approvals(
        record, {"requirements.md": {"hash": routes._sha256_text("# rewritten since")}}
    )
    assert moved["requirements"]["stale"] is True

    # The document disappearing is also stale: nothing is left that it describes.
    assert routes._normalize_approvals(record, {})["requirements"]["stale"] is True


def test_approvals_from_the_index_are_normalized_not_trusted():
    """The index is reachable by the agent (it runs shell commands as the user),
    exactly like every other index field this module scrubs on egress. A forged
    or malformed record must not reach the browser as-is."""
    junk = {
        "requirements": {"hash": "not-a-digest", "at": "soon", "user": "x"},
        "design": "not even a dict",
        "tasks": {"hash": "0" * 64},  # not an approvable phase
        "../evil": {"hash": "0" * 64},
    }
    out = routes._normalize_approvals(junk, {})
    assert out == {}, out


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["tasks", "new", "", "../requirements"])
async def test_approve_rejects_a_phase_outside_the_approvable_two(phase, tmp_path, monkeypatch):
    """There is no "approve tasks" step: approving the task list IS the handoff."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# r"})

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={**_spec_identity(), "phase": phase, "hash": "0" * 64},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 400 and body["code"] == "invalid_phase"


# ── gap 3: one task at a time, addressed by position AND text ────────────────


def test_parse_tasks_enumerates_hashes_and_derives_progress():
    text = (
        "# Tasks\n\n"
        "- [x] wire the endpoint\n"
        "- [ ] add the tests\n"
        "1. [ ] update the docs\n"
        "- [ ]\n"  # no text: not something a user can run
        "just prose\n"
    )
    tasks = routes._parse_tasks(text)

    assert [t["index"] for t in tasks] == [0, 1, 2]
    assert [t["done"] for t in tasks] == [True, False, False]
    assert [t["text"] for t in tasks] == ["wire the endpoint", "add the tests", "update the docs"]
    assert tasks[1]["hash"] == routes._sha256_text("add the tests")
    # The gate's predicate reads the same parse, so the two cannot disagree.
    assert routes._has_open_task(text) is True


@pytest.mark.asyncio
async def test_task_run_dispatches_one_scoped_turn(tmp_path, monkeypatch):
    """Deliberately a single turn and NOT an autonudge loop: the whole-list handoff
    arms a loop that keeps going, while running one task must end where the user
    expects it to."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, working_dir = _seed_spec(
        tmp_path, files={"tasks.md": "- [x] done already\n- [ ] add the tests\n"}
    )
    state, slots = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    await client.start_server()
    try:
        client.app["state"] = state
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        target = detail["tasks"][1]
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": target["index"], "hash": target["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert detail["task_progress"] == {"done": 1, "total": 2}
    assert len(sent) == 1
    # Names the task by text and occurrence, and tells the agent to stop rather than continue.
    assert "add the tests" in sent[0]
    assert "checklist item 2" in sent[0]
    assert "Do NOT continue" in sent[0]
    assert str(spec_dir / "tasks.md") in sent[0] and str(working_dir) in sent[0]


@pytest.mark.asyncio
async def test_task_run_identifies_the_selected_duplicate_occurrence(tmp_path, monkeypatch):
    """Repeated labels still dispatch the exact checkbox occurrence the user clicked."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] run the tests\n- [ ] run the tests\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    await client.start_server()
    try:
        client.app["state"] = state
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        target = detail["tasks"][1]
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": target["index"], "hash": target["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert len(sent) == 1
    assert "checklist item 2" in sent[0]


@pytest.mark.asyncio
async def test_task_run_revalidates_the_task_after_slot_setup(tmp_path, monkeypatch):
    """An IDE edit during the awaited slot setup must win over the stale click."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path, files={"tasks.md": "- [ ] original task\n"})
    state, slots = _state_for("live")
    sent: list[str] = []

    async def _setup_then_edit(_state, _name, _meta):
        (spec_dir / "tasks.md").write_text("- [ ] replacement task\n")
        return slots[routes._slot_key("live")]

    monkeypatch.setattr(routes, "_ensure_worker_slot", _setup_then_edit)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": 0, "hash": routes._sha256_text("original task")},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "task_changed"
    assert sent == [], "dispatched the snapshot captured before slot setup"


@pytest.mark.asyncio
async def test_task_run_refuses_a_task_whose_text_moved(tmp_path, monkeypatch):
    """The reason position alone is not an identity: the agent rewrites tasks.md
    between polls, so a click on "task 2" could otherwise dispatch whatever ended
    up second."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] the list was reordered\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={
                **_spec_identity(),
                "index": 0,
                "hash": routes._sha256_text("what the user actually clicked"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "task_changed"
    assert sent == [], "dispatched a task the user did not choose"


@pytest.mark.asyncio
async def test_task_run_uses_the_same_redacted_identity_the_detail_endpoint_serves(
    tmp_path, monkeypatch
):
    """Reloading produces a runnable raw identity and a redacted task label."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] rotate sk-secret\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_redact", lambda text: text.replace("sk-secret", "[redacted]"))
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    await client.start_server()
    try:
        client.app["state"] = state
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        task = detail["tasks"][0]
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": task["index"], "hash": task["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert len(sent) == 1 and "rotate [redacted]" in sent[0]


@pytest.mark.asyncio
async def test_task_run_hashes_raw_text_when_redaction_hides_a_change(tmp_path, monkeypatch):
    """A stale click must not survive when only a redacted credential changes."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path, files={"tasks.md": "- [ ] rotate secret-old\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(
        routes,
        "_redact",
        lambda text: re.sub(r"secret-(?:old|new)", "[redacted]", text),
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    await client.start_server()
    try:
        client.app["state"] = state
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        task = detail["tasks"][0]
        (spec_dir / "tasks.md").write_text("- [ ] rotate secret-new\n")
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": task["index"], "hash": task["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert task["text"] == "rotate [redacted]"
    assert resp.status == 409 and body["code"] == "task_changed"
    assert sent == []


@pytest.mark.asyncio
async def test_task_run_is_refused_while_the_whole_list_is_building(tmp_path, monkeypatch):
    """A loop already working the list and a single-task turn write the same files
    and check off the same boxes, so they must not overlap."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"}, extra={"status": "executing"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))
    monkeypatch.setattr(routes, "_effective_status", _always_executing)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "already_executing"
    assert sent == []


@pytest.mark.asyncio
async def test_task_run_refuses_a_handoff_that_claims_during_slot_setup(tmp_path, monkeypatch):
    """The handoff owns the spec as soon as its index claim lands, before its
    nudge loop or first turn makes the slot look busy."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, _ = _state_for("live")
    real_ensure = routes._ensure_worker_slot
    sent: list[str] = []

    async def _ensure_after_handoff_claim(*args, **kwargs):
        slot = await real_ensure(*args, **kwargs)
        await routes._touch_spec(
            "live",
            status="executing",
            exec_arming_at=time.time(),
        )
        return slot

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_after_handoff_claim)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: sent.append("sent"))

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "already_executing"
    assert sent == []


@pytest.mark.asyncio
async def test_concurrent_task_runs_dispatch_only_once(tmp_path, monkeypatch):
    """Two requests may pass the early status check before either owns the slot."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, slots = _state_for("live")
    slot = slots[routes._slot_key("live")]
    real_ensure = routes._ensure_worker_slot
    first_waiting = asyncio.Event()
    release = asyncio.Event()
    arrivals = 0
    sent: list[str] = []

    async def _held_ensure(*args, **kwargs):
        nonlocal arrivals
        resolved = await real_ensure(*args, **kwargs)
        arrivals += 1
        if arrivals == 1:
            first_waiting.set()
        await release.wait()
        return resolved

    def _mark_running(_state, _slot, message):
        sent.append(message)
        slot.running = True

    monkeypatch.setattr(routes, "_ensure_worker_slot", _held_ensure)
    monkeypatch.setattr(routes, "_dispatch_turn", _mark_running)
    body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}

    await client.start_server()
    try:
        client.app["state"] = state
        first = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        second = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(first_waiting.wait(), timeout=5)
        release.set()
        responses = await asyncio.gather(first, second)
        payloads = [await response.json() for response in responses]
    finally:
        release.set()
        await client.close()

    assert sorted(response.status for response in responses) == [200, 409]
    assert any(payload.get("code") == "agent_running" for payload in payloads)
    assert arrivals == 1, "the losing request materialized the slot before checking the winner"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_task_slot_materialization_serializes_delete_capture(tmp_path, monkeypatch):
    """Delete cannot capture no slot while a task is about to restore that slot."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, slots = _state_for()
    ensure_waiting = asyncio.Event()
    release_ensure = asyncio.Event()
    delete_waiting = asyncio.Event()
    mark_entered = asyncio.Event()
    real_lock = routes._turn_lock
    real_mark = routes._mark_deleting
    lock_calls = 0
    captured: list[object] = []
    order: list[str] = []

    async def _held_slot_materialization(_state, _name, _meta):
        ensure_waiting.set()
        await release_ensure.wait()
        key = routes._slot_key("live")
        slots[key] = _IdleSlot(key)
        return slots[key]

    def _watched_lock(spec_dir):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            delete_waiting.set()
        return real_lock(spec_dir)

    async def _watched_mark(*args, **kwargs):
        mark_entered.set()
        order.append("reserve-delete")
        return await real_mark(*args, **kwargs)

    async def _remove_loop(*args, **kwargs):
        return None

    async def _capture_teardown(*args, **kwargs):
        captured.append(kwargs.get("only_slot"))
        return True

    monkeypatch.setattr(routes, "_ensure_worker_slot", _held_slot_materialization)
    monkeypatch.setattr(routes, "_turn_lock", _watched_lock)
    monkeypatch.setattr(routes, "_mark_deleting", _watched_mark)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: order.append("dispatch-task"))
    monkeypatch.setattr(routes, "_remove_nudge_loop", _remove_loop)
    monkeypatch.setattr(routes, "_teardown_worker_slot", _capture_teardown)

    await client.start_server()
    try:
        client.app["state"] = state
        body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}
        task_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(ensure_waiting.wait(), timeout=5)
        delete_request = asyncio.create_task(client.delete(f"{_BASE}/specs/live"))
        await asyncio.wait_for(delete_waiting.wait(), timeout=5)
        assert not mark_entered.is_set(), "Delete captured the runtime during slot restore"
        release_ensure.set()
        task_response, delete_response = await asyncio.gather(task_request, delete_request)
    finally:
        release_ensure.set()
        await client.close()

    assert task_response.status == 200 and delete_response.status == 200
    assert order == ["dispatch-task", "reserve-delete"]
    assert captured == [slots[routes._slot_key("live")]], "Delete missed the restored slot"


@pytest.mark.asyncio
async def test_task_final_snapshot_serializes_the_whole_plan_claim(tmp_path, monkeypatch):
    """Execute cannot claim the spec while a task's final disk snapshot awaits."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, slots = _state_for("live")
    slot = slots[routes._slot_key("live")]
    release_snapshot = asyncio.Event()
    snapshot_waiting = asyncio.Event()
    execute_waiting = asyncio.Event()
    claim_entered = asyncio.Event()
    real_to_thread = routes.asyncio.to_thread
    real_lock = routes._turn_lock
    real_claim = routes._claim_execution
    snapshot_calls = 0
    lock_calls = 0
    sent: list[str] = []

    async def _held_final_snapshot(func, *args, **kwargs):
        nonlocal snapshot_calls
        if getattr(func, "__name__", "") == "_task_snapshot":
            snapshot_calls += 1
            if snapshot_calls == 2:
                snapshot_waiting.set()
                await release_snapshot.wait()
        return await real_to_thread(func, *args, **kwargs)

    def _watched_lock(spec_dir):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            execute_waiting.set()
        return real_lock(spec_dir)

    async def _watched_claim(*args, **kwargs):
        claim_entered.set()
        return await real_claim(*args, **kwargs)

    def _mark_running(_state, _slot, message):
        sent.append(message)
        slot.running = True

    monkeypatch.setattr(routes.asyncio, "to_thread", _held_final_snapshot)
    monkeypatch.setattr(routes, "_turn_lock", _watched_lock)
    monkeypatch.setattr(routes, "_claim_execution", _watched_claim)
    monkeypatch.setattr(routes, "_dispatch_turn", _mark_running)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    await client.start_server()
    try:
        client.app["state"] = state
        body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}
        task_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(snapshot_waiting.wait(), timeout=5)
        execute_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/execute"))
        await asyncio.wait_for(execute_waiting.wait(), timeout=5)
        assert not claim_entered.is_set(), "Execute crossed the task's final snapshot"
        release_snapshot.set()
        task_response, execute_response = await asyncio.gather(task_request, execute_request)
        execute_body = await execute_response.json()
    finally:
        release_snapshot.set()
        slot.running = False
        await client.close()

    assert task_response.status == 200
    assert execute_response.status == 409 and execute_body["code"] == "already_executing"
    assert claim_entered.is_set() and len(sent) == 1


@pytest.mark.asyncio
async def test_task_final_snapshot_serializes_delete_reservation(tmp_path, monkeypatch):
    """Delete reserves teardown only after a final task snapshot has dispatched."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, _ = _state_for("live")
    release_snapshot = asyncio.Event()
    snapshot_waiting = asyncio.Event()
    delete_waiting = asyncio.Event()
    mark_entered = asyncio.Event()
    real_to_thread = routes.asyncio.to_thread
    real_lock = routes._turn_lock
    real_mark = routes._mark_deleting
    snapshot_calls = 0
    lock_calls = 0
    order: list[str] = []

    async def _held_final_snapshot(func, *args, **kwargs):
        nonlocal snapshot_calls
        if getattr(func, "__name__", "") == "_task_snapshot":
            snapshot_calls += 1
            if snapshot_calls == 2:
                snapshot_waiting.set()
                await release_snapshot.wait()
        return await real_to_thread(func, *args, **kwargs)

    def _watched_lock(spec_dir):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            delete_waiting.set()
        return real_lock(spec_dir)

    async def _watched_mark(*args, **kwargs):
        mark_entered.set()
        order.append("reserve-delete")
        return await real_mark(*args, **kwargs)

    async def _remove_loop(*args, **kwargs):
        return None

    async def _teardown(*args, **kwargs):
        return True

    monkeypatch.setattr(routes.asyncio, "to_thread", _held_final_snapshot)
    monkeypatch.setattr(routes, "_turn_lock", _watched_lock)
    monkeypatch.setattr(routes, "_mark_deleting", _watched_mark)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: order.append("dispatch-task"))
    monkeypatch.setattr(routes, "_remove_nudge_loop", _remove_loop)
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)

    await client.start_server()
    try:
        client.app["state"] = state
        body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}
        task_request = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(snapshot_waiting.wait(), timeout=5)
        delete_request = asyncio.create_task(client.delete(f"{_BASE}/specs/live"))
        await asyncio.wait_for(delete_waiting.wait(), timeout=5)
        assert not mark_entered.is_set(), "Delete crossed the task's final snapshot"
        release_snapshot.set()
        task_response, delete_response = await asyncio.gather(task_request, delete_request)
    finally:
        release_snapshot.set()
        await client.close()

    assert task_response.status == 200 and delete_response.status == 200
    assert order == ["dispatch-task", "reserve-delete"]


@pytest.mark.asyncio
async def test_task_run_refuses_between_orchestration_stages(tmp_path, monkeypatch):
    """A staged plan owns the slot even when no individual turn task is live."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, slots = _state_for("live")
    slots[routes._slot_key("live")]._in_stage_execution = True
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: sent.append("sent"))

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "agent_running"
    assert sent == []


async def _always_executing(name, meta, slot):
    return "executing"


@pytest.mark.asyncio
async def test_task_run_refuses_a_task_already_checked_off(tmp_path, monkeypatch):
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [x] already finished\n"})
    state, _ = _state_for("live")

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={
                **_spec_identity(),
                "index": 0,
                "hash": routes._sha256_text("already finished"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "task_done"


# ── gap 5: label, archive, duplicate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_title_relabels_without_touching_the_identity(tmp_path, monkeypatch):
    """A rename of the LABEL only. The name is simultaneously the on-disk directory,
    the git branch and the chat slot key -- and _owns_slot_key requires the key to
    ENCODE the name -- so renaming the identity would move a directory the IDE and
    CLI also read and orphan the spec's transcript, which is exactly what
    delete-and-recreate loses."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/title",
            json={**_spec_identity(), "title": "Checkout rewrite"},
        )
        assert resp.status == 200, await resp.json()
    finally:
        await client.close()

    entry = routes._load_index()["live"]
    assert entry["title"] == "Checkout rewrite"
    assert entry["spec_dir"] == str(spec_dir), "the directory moved"
    assert entry["slot_key"] == routes._slot_key("live"), "the transcript was orphaned"
    assert spec_dir.is_dir()


@pytest.mark.asyncio
async def test_archive_marks_the_spec_without_deleting_it(tmp_path, monkeypatch):
    """The non-destructive counterpart to delete: documents, transcript and index
    entry all stay. Delete used to be the only way out of the rail, so tidying up
    and destroying the work were the same action."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path, files={"requirements.md": "# keep me"})

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        assert (
            await client.post(
                f"{_BASE}/specs/live/archive",
                json={**_spec_identity(), "archived": True},
            )
        ).status == 200

        listed = await (await client.get(f"{_BASE}/specs")).json()
        # Still reachable directly, and still on disk.
        detail = await (await client.get(f"{_BASE}/specs/live")).json()

        assert (
            await client.post(
                f"{_BASE}/specs/live/archive",
                json={**_spec_identity(), "archived": False},
            )
        ).status == 200
        restored = await (await client.get(f"{_BASE}/specs")).json()
    finally:
        await client.close()

    assert [(s["name"], s["archived"]) for s in listed["specs"]] == [("live", True)]
    assert detail["archived"] is True
    assert (spec_dir / "requirements.md").read_text() == "# keep me"
    assert [s["name"] for s in restored["specs"]] == ["live"]


@pytest.mark.asyncio
async def test_archive_is_refused_while_the_spec_is_building(tmp_path, monkeypatch):
    """Archiving a running spec would hide a loop that keeps editing files, leaving
    the user no surface to stop it from."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, extra={"status": "executing"})
    monkeypatch.setattr(routes, "_effective_status", _always_executing)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/archive",
            json={**_spec_identity(), "archived": True},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "spec_executing"
    assert routes._load_index()["live"].get("archived") is not True


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_copies_the_documents_into_a_fresh_spec(tmp_path, monkeypatch):
    """The recovery path rename cannot serve: a spec whose NAME is wrong once it
    already has a branch or history. The copy takes the documents and nothing
    else, so it gets its own slot key and therefore its own conversation."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs", "design.md": "# design"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body
    copy_dir = Path(body["spec_dir"])
    assert copy_dir.name == "live-copy"
    assert (copy_dir / "requirements.md").read_text() == "# reqs"
    assert (copy_dir / "design.md").read_text() == "# design"
    assert not (copy_dir / routes._DUPLICATE_MARKER).exists()

    index = routes._load_index()
    assert index["live"]["spec_dir"] != index["live-copy"]["spec_dir"]
    assert index["live-copy"]["slot_key"] != index["live"]["slot_key"], "shared a transcript"
    # A duplicate never inherits a worktree: branching off someone's repo is not a
    # copy operation, and it is an opt-in at create time.
    assert index["live-copy"]["worktree_branch"] == ""
    # The fresh conversation is told what it is looking at, and told not to rewrite it.
    assert len(sent) == 1 and "copy of 'live'" in sent[0] and "Do NOT rewrite" in sent[0]


@pytest.mark.asyncio
async def test_duplicate_refuses_while_the_source_agent_is_writing(tmp_path, monkeypatch):
    """A live agent turn can rewrite phase files while they are being copied."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs"})
    state, slots = _state_for("live")
    slots[routes._slot_key("live")].running = True
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "agent_running"
    assert "live-copy" not in routes._load_index()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_refuses_a_mixed_source_document_snapshot(tmp_path, monkeypatch):
    """Every copied phase file must belong to one stable source snapshot."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _working_dir = _seed_spec(
        tmp_path,
        files={"requirements.md": "# requirements v1", "design.md": "# design v1"},
    )
    real_read = routes._read_spec_text
    changed = False

    def _change_plan_between_reads(directory, fname):
        nonlocal changed
        text = real_read(directory, fname)
        if fname == "requirements.md" and not changed:
            changed = True
            (spec_dir / "requirements.md").write_text("# requirements v2")
            (spec_dir / "design.md").write_text("# design v2")
        return text

    monkeypatch.setattr(routes, "_read_spec_text", _change_plan_between_reads)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "mixed-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "spec_changed_during_duplicate"
    assert "mixed-copy" not in routes._load_index()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_redacts_the_returned_spec_directory(tmp_path, monkeypatch):
    """A credential-like project path must not cross the HTTP response boundary raw."""
    client = _make_client(monkeypatch, tmp_path)
    secret_root = tmp_path / "secret-segment"
    _seed_spec(secret_root, files={"requirements.md": "# copied"})
    monkeypatch.setattr(
        routes,
        "_redact",
        lambda text: text.replace("secret-segment", "[REDACTED]"),
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda _state, _slot, _message: None)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body
    assert "secret-segment" not in body["spec_dir"]
    assert "[REDACTED]" in body["spec_dir"]
    assert Path(routes._load_index()["live-copy"]["spec_dir"]).is_dir()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_publishes_recovery_provenance_before_index_reservation(
    tmp_path, monkeypatch
):
    """A crash after reservation must leave proof that recovery may discard it."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    real_mutate = routes._mutate_index

    async def _assert_provenance(fn):
        changed = await real_mutate(fn)
        if changed and getattr(fn, "__name__", "") == "_insert":
            meta = routes._load_index()["live-copy"]
            held = meta[routes._DUPLICATING]
            assert routes._duplicate_marker_matches(Path(held["stage_dir"]), held["token"])
        return changed

    monkeypatch.setattr(routes, "_mutate_index", _assert_provenance)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_cancelled_duplicate_finishes_its_published_transaction(tmp_path, monkeypatch):
    """Request cancellation cannot hide a published copy behind our reservation."""
    _redirect_state(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    state, _ = _state_for("live")
    published = threading.Event()
    release_write = threading.Event()
    real_write = routes._write_and_publish_duplicate

    def _publish_then_block(*args, **kwargs):
        result = real_write(*args, **kwargs)
        assert result[0] == ""
        published.set()
        assert release_write.wait(5), "cancelled transaction was never released"
        return result

    async def _body(_request):
        return {"new_name": "cancelled-copy"}

    async def _fresh(_request, _name, _body):
        return routes._load_index()["live"]

    monkeypatch.setattr(routes, "_require_auth", lambda _request: None)
    monkeypatch.setattr(routes, "_read_json", _body)
    monkeypatch.setattr(routes, "_pinned_entry", _fresh)
    monkeypatch.setattr(routes, "_write_and_publish_duplicate", _publish_then_block)
    request = cast(
        web.Request,
        types.SimpleNamespace(match_info={"name": "live"}, app={"state": state}),
    )

    handler = asyncio.create_task(routes._handle_duplicate(request))
    try:
        assert await asyncio.to_thread(published.wait, 5)
        handler.cancel()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await handler
    finally:
        release_write.set()

    meta = routes._load_index()["cancelled-copy"]
    target = Path(meta["spec_dir"])
    assert routes._DUPLICATING not in meta
    assert (target / "requirements.md").read_text() == "# copied"
    assert not (target / routes._DUPLICATE_MARKER).exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_preserves_an_existing_empty_document(tmp_path, monkeypatch):
    """An empty phase file is data, not the same thing as a missing document."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs", "design.md": ""})
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body
    copy_dir = Path(body["spec_dir"])
    assert (copy_dir / "requirements.md").read_text() == "# reqs"
    assert (copy_dir / "design.md").is_file()
    assert (copy_dir / "design.md").read_text() == ""


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_refuses_an_existing_document_that_cannot_be_read(tmp_path, monkeypatch):
    """An oversized source document must not be silently omitted from a copy."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(
        tmp_path,
        files={
            "requirements.md": "# copied",
            "design.md": "x" * (routes._MAX_SPEC_BYTES + 1),
        },
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "incomplete-copy"},
        )
        assert resp.status == 409
        body = await resp.json()
    finally:
        await client.close()

    assert body["code"] == "spec_document_unreadable"
    assert "incomplete-copy" not in routes._load_index()
    assert not (working_dir / ".kiro" / "specs" / "incomplete-copy").exists()


@pytest.mark.asyncio
async def test_duplicate_refuses_a_name_already_in_use(tmp_path, monkeypatch):
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs"})

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        same = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live"},
        )
        same_body = await same.json()
        bad = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "../escape"},
        )
        bad_body = await bad.json()
    finally:
        await client.close()

    assert same.status == 409 and same_body["code"] == "spec_exists"
    assert bad.status == 400 and bad_body["code"] == "invalid_name"


@pytest.mark.asyncio
async def test_duplicate_refuses_a_spec_with_no_documents_yet(tmp_path, monkeypatch):
    """Nothing to copy is a refusal, not an empty spec created as a side effect."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "empty-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "nothing_to_copy"
    assert "empty-copy" not in routes._load_index()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_reserves_its_name_before_populating_the_destination(tmp_path, monkeypatch):
    """A concurrent create must not win the destination index entry while the
    duplicate is already copying documents into that same directory."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    state, _ = _state_for("live")
    real_create = routes._create_spec_doc
    save_started = threading.Event()
    release_save = threading.Event()

    def _held_save(*args, **kwargs):
        save_started.set()
        assert release_save.wait(5), "concurrent create never reached arbitration"
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _held_save)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args, **_kwargs: None)

    await client.start_server()
    try:
        client.app["state"] = state
        duplicate_request = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/live/duplicate",
                json={**_spec_identity(), "new_name": "shared-name"},
            )
        )
        assert await asyncio.to_thread(save_started.wait, 5)
        create = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "shared-name",
                "working_dir": str(working_dir),
                "spec_type": "feature",
                "description": "concurrent create",
                "use_worktree": False,
            },
        )
        create_body = await create.json()
        release_save.set()
        duplicate = await duplicate_request
        duplicate_body = await duplicate.json()
    finally:
        release_save.set()
        await client.close()

    assert create.status == 409 and create_body["code"] == "spec_exists"
    assert duplicate.status == 201, duplicate_body
    copy_dir = Path(duplicate_body["spec_dir"])
    assert (copy_dir / "requirements.md").read_text() == "# copied"
    assert routes._load_index()["shared-name"]["spec_dir"] == str(copy_dir)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_refuses_when_the_destination_changes_after_reservation(
    tmp_path, monkeypatch
):
    """The index reservation and copied files must always name one directory."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    moved_base = tmp_path / "moved-specs"
    moved_base.mkdir()
    state, _ = _state_for("live")
    real_mutate = routes._mutate_index
    moved = False

    async def _move_settings_after_reservation(fn):
        nonlocal moved
        changed = await real_mutate(fn)
        if changed and not moved and "live-copy" in routes._load_index():
            moved = True
            routes._save_settings({"base_path": str(moved_base), "model": ""})
        return changed

    monkeypatch.setattr(routes, "_mutate_index", _move_settings_after_reservation)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args, **_kwargs: None)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "spec_destination_changed"
    assert "live-copy" not in routes._load_index()
    assert not (moved_base / "live-copy").exists()
    assert not (working_dir / ".kiro" / "specs" / "live-copy").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_rolls_back_only_files_it_created_after_a_partial_failure(
    tmp_path, monkeypatch
):
    """A failed copy must not become discoverable as an incomplete spec."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(
        tmp_path,
        files={"requirements.md": "# copied", "design.md": "# fails"},
    )
    state, _ = _state_for("live")
    real_create = routes._create_spec_doc
    calls = 0

    def _fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return "write_failed", None
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _fail_second)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "partial-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "partial-copy"
    assert resp.status == 400 and body["code"] == "doc_write_failed"
    assert "partial-copy" not in routes._load_index()
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_preserves_a_published_copy_when_index_finalization_fails(
    tmp_path, monkeypatch
):
    """A post-rename failure remains recoverable instead of poisoning retries."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    real_mutate = routes._mutate_index

    async def _fail_finish(fn):
        if getattr(fn, "__name__", "") == "_finish":
            return False
        return await real_mutate(fn)

    monkeypatch.setattr(routes, "_mutate_index", _fail_finish)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "recoverable-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "recoverable-copy"
    assert resp.status == 409 and body["code"] == "spec_changed_during_create"
    assert (target / "requirements.md").read_text() == "# copied"
    assert (target / routes._DUPLICATE_MARKER).is_file()
    assert routes._DUPLICATING in routes._load_index()["recoverable-copy"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_contains_an_index_finalization_exception(tmp_path, monkeypatch):
    """A post-publication index error must return the recoverable-copy response."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    real_mutate = routes._mutate_index

    async def _raise_finish(fn):
        if getattr(fn, "__name__", "") == "_finish":
            raise OSError("index storage unavailable")
        return await real_mutate(fn)

    monkeypatch.setattr(routes, "_mutate_index", _raise_finish)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "recoverable-copy"},
        )
        assert resp.status == 409
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "recoverable-copy"
    assert body["code"] == "spec_changed_during_create"
    assert (target / "requirements.md").read_text() == "# copied"
    assert (target / routes._DUPLICATE_MARKER).is_file()
    assert routes._DUPLICATING in routes._load_index()["recoverable-copy"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_keeps_its_committed_copy_when_slot_setup_is_refused(tmp_path, monkeypatch):
    """Session arbitration must not delete a copy already committed to the index."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})

    async def _refuse_slot(*_args, **_kwargs):
        return None

    monkeypatch.setattr(routes, "_ensure_worker_slot", _refuse_slot)

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "committed-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "committed-copy"
    assert resp.status == 409 and body["code"] == "slot_owned_by_another_app"
    assert (target / "requirements.md").read_text() == "# copied"
    assert "committed-copy" in routes._load_index()


# ── shared guards for the new mutations ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "approve", {"phase": "requirements", "hash": "0" * 64}),
        ("post", "task", {"index": 0, "hash": "0" * 64}),
        ("post", "title", {"title": "t"}),
        ("post", "archive", {"archived": True}),
        ("post", "duplicate", {"new_name": "a-copy"}),
    ],
)
async def test_new_mutations_refuse_a_stale_client_identity(
    method, path, body, tmp_path, monkeypatch
):
    """Every mutation carries the identity the CLIENT rendered, so a stale tab
    cannot drive a same-name spec that was deleted and recreated elsewhere. The
    per-creation slot key is the decisive field: two specs can share a directory
    across a delete + re-import, but never a key."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# r", "tasks.md": "- [ ] t\n"})

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await getattr(client, method)(
            f"{_BASE}/specs/live/{path}",
            json={**body, "spec_dir": "/somewhere/else", "slot_key": "spec-builder-live-deadbeef"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload["code"] == "stale_client"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "approve", {"phase": "requirements", "hash": "0" * 64}),
        ("post", "task", {"index": 0, "hash": "0" * 64}),
        ("post", "title", {"title": "t"}),
        ("post", "archive", {"archived": True}),
        ("post", "duplicate", {"new_name": "a-copy"}),
    ],
)
async def test_new_mutations_require_a_complete_client_identity(
    method, path, body, tmp_path, monkeypatch
):
    """A control rendered before detail loads must not mutate whatever creation
    currently happens to own the same name."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# r", "tasks.md": "- [ ] t\n"})

    await client.start_server()
    try:
        client.app["state"] = _state_for("live")[0]
        resp = await getattr(client, method)(f"{_BASE}/specs/live/{path}", json=body)
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload["code"] == "stale_client"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "approve"),
        ("post", "task"),
        ("post", "title"),
        ("post", "archive"),
        ("post", "duplicate"),
    ],
)
async def test_new_mutations_require_authentication(method, path, tmp_path, monkeypatch):
    """No auth middleware, so request['user'] is unset: every new write must 401
    rather than fall through to the filesystem."""
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application()  # deliberately WITHOUT _auth_mw
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await getattr(client, method)(f"{_BASE}/specs/live/{path}", json={})
        assert resp.status == 401


def test_new_mutations_do_not_write_the_index_from_a_stale_snapshot():
    """Extends the existing class guard to the new handlers: each awaits (a body
    read at minimum), so none of them may call _save_index directly."""
    for handler in (
        routes._handle_approve,
        routes._handle_run_task,
        routes._handle_title,
        routes._handle_archive,
        routes._handle_duplicate,
    ):
        assert "_save_index(" not in inspect.getsource(
            handler
        ), f"{handler.__name__} writes the index directly; it must go through _mutate_index"


def test_document_and_task_reads_stay_off_the_event_loop():
    """Source guard, same reason the detail handler has one: these handlers read
    and write caller-supplied paths, and the detail endpoint beside them is polled
    every 2.5s. Blocking filesystem work must ride a worker thread."""
    for handler, blocking in (
        (routes._approve_locked, "_current_hash"),
        (routes._handle_run_task, "_tasks"),
        (routes._duplicate_locked, "_copy"),
    ):
        src = inspect.getsource(handler)
        # Matched on the two tokens rather than one formatted call string: the
        # arguments wrap, so pinning the exact whitespace would fail on a reformat
        # while saying nothing about whether the work is still offloaded.
        assert (
            "asyncio.to_thread(" in src and blocking in src
        ), f"{handler.__name__} may be doing filesystem work inline"


def _live_decisions_snapshot() -> tuple[bool, int]:
    """(exists, size) of the REAL decision ledger, captured before redirection."""
    global _REAL_DECISIONS_PATH
    if _REAL_DECISIONS_PATH is None:
        _REAL_DECISIONS_PATH = routes._decisions_path()
    try:
        return True, _REAL_DECISIONS_PATH.stat().st_size
    except OSError:
        return False, 0


@web.middleware
async def _app_auth_mw(request, handler):
    """Model an authenticated app token, which also carries a user identity."""
    request["user"] = "tester"
    request["app"] = "spec-builder"
    return await handler(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"text": "ordinary app-authored message"},
        {
            "text": "Decision - Transport: HTTPS",
            "decision_id": "transport",
            "decision_option": "HTTPS",
        },
    ],
)
async def test_app_tokens_cannot_mint_human_spec_messages(body, tmp_path, monkeypatch):
    """App tokens are authenticated but cannot authorize user-only directives."""
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application(middlewares=[_app_auth_mw])
    routes.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"{_BASE}/specs/s/message", json=body)
        payload = await response.json()

    assert response.status == 403
    assert payload["code"] == "interactive_user_required"


def test_normalize_spec_state_ignores_an_agent_authored_lock():
    """The agent must not be able to declare a decision locked (or unlock one it
    answered) by writing the field itself."""
    out = routes._normalize_spec_state(
        {"decisions": [{"id": "a", "title": "t", "locked": True, "answer": "x"}]}
    )
    assert out is not None
    assert out["decisions"][0]["locked"] is False


@pytest.mark.asyncio
async def test_nudge_maintenance_pause_is_durable_and_waits_for_a_firing_timer(
    tmp_path,
):
    """Orphan cleanup gets a restart marker before it snapshots the worker slot."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    original_timer = svc._timers.pop(loop.id)
    original_timer.cancel()
    await original_timer

    write_done = threading.Event()
    real_write = svc._write_state

    def _record_write(payload):
        real_write(payload)
        write_done.set()

    svc._write_state = _record_write  # type: ignore[method-assign]
    release = asyncio.Event()
    firing_timer = asyncio.create_task(release.wait())
    svc._timers[loop.id] = firing_timer
    svc._firing.add(loop.id)
    pause = asyncio.create_task(svc.deactivate_and_wait(loop.id))
    assert await asyncio.to_thread(write_done.wait, 2.0)

    assert loop.active is False
    assert not pause.done()
    reloaded = await an.AutoNudgeService.load_for_maintenance(base_dir=tmp_path)
    persisted = reloaded.get_by_slot(loop.slot_key)
    assert persisted is not None and persisted.active is False

    release.set()
    assert await pause is True
    svc._firing.discard(loop.id)


@pytest.mark.asyncio
async def test_nudge_maintenance_pause_waits_for_a_replacement_timer(
    tmp_path,
):
    """A turn-complete rearm during the persisted pause cannot escape cleanup."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    original_timer = svc._timers.pop(loop.id)
    original_timer.cancel()
    await original_timer

    write_done = threading.Event()
    real_write = svc._write_state

    def _record_write(payload):
        real_write(payload)
        write_done.set()

    svc._write_state = _record_write  # type: ignore[method-assign]
    await svc._lock.acquire()
    pause = asyncio.create_task(svc.deactivate_and_wait(loop.id))
    await asyncio.sleep(0)

    release = asyncio.Event()
    replacement_timer = asyncio.create_task(release.wait())
    svc._timers[loop.id] = replacement_timer
    svc._firing.add(loop.id)
    svc._lock.release()
    assert await asyncio.to_thread(write_done.wait, 2.0)

    assert loop.active is False
    assert not pause.done()
    release.set()
    assert await pause is True
    svc._firing.discard(loop.id)


@pytest.mark.asyncio
async def test_nudge_remove_failure_restores_the_retryable_loop(tmp_path, monkeypatch):
    """A failed durable delete cannot make its retained row invisible in memory."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await svc.update(loop.id, active=False)
    events: list[str] = []
    svc.subscribe(lambda event, _loop: events.append(event))
    real_write = svc._write_state

    def _fail_write(_payload):
        raise OSError("store unavailable")

    monkeypatch.setattr(svc, "_write_state", _fail_write)
    with pytest.raises(OSError, match="store unavailable"):
        await svc.remove(loop.id)

    assert svc.get_by_slot(loop.slot_key) is loop
    assert loop in svc.list_all()
    assert "removed" not in events

    monkeypatch.setattr(svc, "_write_state", real_write)
    await svc.remove(loop.id)
    assert svc.get_by_slot(loop.slot_key) is None
    assert events == ["removed"]


@pytest.mark.asyncio
async def test_nudge_maintenance_transaction_serializes_service_startup(tmp_path, monkeypatch):
    """Startup loads only after an offline cleanup commits its authoritative view."""
    from kiro_crew import autonudge as an

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    monkeypatch.setattr(an, "_INSTANCE", None)
    seed = an.AutoNudgeService(base_dir=tmp_path)
    loop = await seed.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await seed.update(loop.id, active=False)

    live = an.AutoNudgeService(base_dir=tmp_path)
    load_started = threading.Event()
    real_load = live._load

    def _record_load():
        load_started.set()
        real_load()

    monkeypatch.setattr(live, "_load", _record_load)
    starter = None
    try:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as offline:
            starter = asyncio.create_task(live.start())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not load_started.is_set()
            await offline.remove(loop.id)

        await starter
        assert load_started.is_set()
        assert live.list_all() == []
        assert live._timers == {}
    finally:
        live.stop()
        if starter is not None and not starter.done():
            starter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await starter


@pytest.mark.asyncio
async def test_nudge_maintenance_transactions_reload_after_a_peer_commit(tmp_path, monkeypatch):
    """Concurrent offline cleanup cannot write from a pre-transaction snapshot."""
    from kiro_crew import autonudge as an

    monkeypatch.setattr(an, "_INSTANCE", None)
    seed = an.AutoNudgeService(base_dir=tmp_path)
    first_loop = await seed.add(
        slot_key="spec-builder-s-1234abcd",
        message="first",
        idle_secs=60,
        max_cycles=1,
    )
    second_loop = await seed.add(
        slot_key="spec-builder-t-deadbeef",
        message="second",
        idle_secs=60,
        max_cycles=1,
    )
    await seed.update(first_loop.id, active=False)
    await seed.update(second_loop.id, active=False)

    peer_entered = asyncio.Event()

    async def _peer_view() -> set[str]:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as peer:
            peer_entered.set()
            return {loop.id for loop in peer.list_all()}

    peer_task = None
    try:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as first:
            peer_task = asyncio.create_task(_peer_view())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not peer_entered.is_set()
            await first.remove(first_loop.id)

        assert await peer_task == {second_loop.id}
    finally:
        if peer_task is not None and not peer_task.done():
            peer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await peer_task


@pytest.mark.asyncio
async def test_nudge_maintenance_excludes_a_failing_live_remove(tmp_path, monkeypatch):
    """A rolled-back live removal stays visible to the maintenance scan."""
    from kiro_crew import autonudge as an

    live = an.AutoNudgeService(base_dir=tmp_path)
    loop = await live.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await live.update(loop.id, active=False)
    monkeypatch.setattr(an, "_INSTANCE", live)
    write_started = threading.Event()
    release_write = threading.Event()
    real_write = live._write_state

    def _fail_removal(payload):
        if not payload["loops"]:
            write_started.set()
            release_write.wait(timeout=5)
            raise OSError("disk unavailable")
        real_write(payload)

    monkeypatch.setattr(live, "_write_state", _fail_removal)
    maintenance_entered = asyncio.Event()

    async def _scan() -> set[str]:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            maintenance_entered.set()
            return {item.id for item in view.list_all()}

    removal = asyncio.create_task(live.remove(loop.id))
    scan = None
    try:
        assert await asyncio.to_thread(write_started.wait, 2)
        scan = asyncio.create_task(_scan())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not maintenance_entered.is_set()
        release_write.set()
        with pytest.raises(OSError, match="disk unavailable"):
            await removal
        assert await scan == {loop.id}
    finally:
        release_write.set()
        live.stop()
        for task in (removal, scan):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
async def test_nudge_maintenance_excludes_a_live_reactivation(tmp_path, monkeypatch):
    """An external update cannot revive a row during administrative removal."""
    from kiro_crew import autonudge as an

    live = an.AutoNudgeService(base_dir=tmp_path)
    loop = await live.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await live.update(loop.id, active=False)
    monkeypatch.setattr(an, "_INSTANCE", live)
    updater = None
    try:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            updater = asyncio.create_task(live.update(loop.id, active=True))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not updater.done()
            await view.remove(loop.id)

        assert await updater is None
        assert live.list_all() == []
        assert live._timers == {}
    finally:
        live.stop()
        if updater is not None and not updater.done():
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater


@pytest.mark.asyncio
async def test_partial_slot_teardown_keeps_the_delete_reserved(tmp_path, monkeypatch):
    """A spec cannot reappear after one captured worker was already destroyed."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    stale_slot_key = "spec-builder-s-feedbeef"
    routes._OBSERVED_SPEC_DIRS[stale_slot_key] = routes._decision_key(spec_dir)
    slots = {
        slot_key: types.SimpleNamespace(key=slot_key),
        stale_slot_key: types.SimpleNamespace(key=stale_slot_key),
    }

    class _State:
        def get_slot(self, key):
            return slots.get(key)

    teardown_results = iter((True, False))

    async def _partially_teardown(*_args, **_kwargs):
        return next(teardown_results)

    monkeypatch.setattr(routes, "_teardown_worker_slot", _partially_teardown)

    await client.start_server()
    try:
        client.app["state"] = _State()
        response = await client.delete(
            f"{_BASE}/specs/s",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 503, body
    assert body["code"] == "archive_failed"
    assert "retry" in body["error"]
    entry = routes._load_index()["s"]
    assert entry.get(routes._DELETING), "the partially deleted spec was unhidden"
    assert spec_dir in routes._load_deleted(), "the partial delete lost its tombstone"
    monkeypatch.setattr(routes, "_PROCESS_ID", "replacement-process")
    restarted = routes._load_index()["s"]
    assert restarted.get(routes._DELETING), "restart exposed the partial delete"


@pytest.mark.asyncio
async def test_index_and_legacy_runtime_identity_are_captured_under_one_lock(tmp_path, monkeypatch):
    """A replacement writer cannot refresh the resolver between the two values."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "wd" / ".kiro" / "specs" / "legacy")
    routes._save_index({"legacy": {"spec_dir": spec_dir}})
    snapshot_read = threading.Event()
    release_snapshot = threading.Event()
    real_load = routes._load_index
    load_count = 0

    def _load_and_pause_once():
        nonlocal load_count
        index = real_load()
        load_count += 1
        if load_count == 1:
            snapshot_read.set()
            assert release_snapshot.wait(timeout=5)
        return index

    monkeypatch.setattr(routes, "_load_index", _load_and_pause_once)
    capture = asyncio.create_task(routes._aload_index_with_slot_identity("legacy"))
    assert await asyncio.to_thread(snapshot_read.wait, 5)
    replacement_key = routes._new_slot_key("legacy")

    def _replace(index):
        index["legacy"]["slot_key"] = replacement_key
        return True

    replacement = asyncio.create_task(routes._mutate_index(_replace))
    await asyncio.sleep(0)
    release_snapshot.set()

    index, runtime_key, observed_key = await capture
    assert await replacement
    assert "slot_key" not in index["legacy"]
    assert runtime_key == "spec-builder-legacy"
    assert observed_key == ""
    assert routes._load_index()["legacy"]["slot_key"] == replacement_key


@pytest.mark.asyncio
async def test_legacy_delete_refuses_same_path_replacement_before_reservation(
    tmp_path, monkeypatch
):
    """A missing legacy key still has the captured name-derived creation identity.

    Replacing that row at the same name and directory after DELETE's snapshot must
    not let the stale request reserve and remove the replacement.
    """
    client = _make_client(monkeypatch, tmp_path)
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "legacy").mkdir(parents=True)
    spec_dir = str(base / "legacy")
    routes._save_index(
        {
            "legacy": {
                "spec_dir": spec_dir,
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
            }
        }
    )
    replacement_keys: list[str] = []
    real_alias_status = routes._aload_index_with_decision_alias_status

    async def _replace_then_check_aliases(directory):
        index = await routes._aload_index()
        replacement_key = routes._new_slot_key("legacy")
        replacement_keys.append(replacement_key)
        index["legacy"]["slot_key"] = replacement_key
        routes._save_index(index)
        return await real_alias_status(directory)

    monkeypatch.setattr(
        routes, "_aload_index_with_decision_alias_status", _replace_then_check_aliases
    )

    await client.start_server()
    try:
        client.app["state"] = None
        response = await client.delete(f"{_BASE}/specs/legacy")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 404, body
    current = routes._load_index()["legacy"]
    assert current["slot_key"] == replacement_keys[0]
    assert routes._DELETING not in current
    assert spec_dir not in routes._load_deleted()


def test_queued_dispatch_preserves_structural_directive_provenance():
    slot = _RecordingSlot()

    routes._dispatch_turn(_NoopState(), slot, "automated seed")
    routes._dispatch_turn(
        _NoopState(),
        slot,
        "human answer",
        directive_user_origin=True,
    )

    assert slot.queued_origins == [False, True]


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (routes._handle_create, False),
        (routes._handle_handoff, False),
        (routes._handle_message, True),
        (routes._deliver_pending_decision, True),
    ],
)
def test_spec_dispatch_callers_assign_directive_provenance(handler, expected):
    """Only direct human input may authorize session-retargeting directives."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_dispatch_turn"
    ]
    assert len(calls) == 1
    keyword = next(
        (kw for kw in calls[0].keywords if kw.arg == "directive_user_origin"),
        None,
    )
    if keyword is None:
        actual = False
    else:
        assert isinstance(keyword.value, ast.Constant)
        actual = keyword.value.value
    assert actual is expected


async def _mark_reserved(name: str) -> bool:
    """Stamp a delete reservation this process owns, as the delete handler does."""

    def _apply(index: dict) -> bool:
        index[name][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
        return True

    return await routes._mutate_index(_apply)


def _final_answer(decision_id: str, option: str) -> dict[str, str]:
    """Build one final durable entry for tests that seed the protected store."""
    return {
        "decision_id": decision_id,
        "option": option,
        "fingerprint": "",
        "status": "final",
        "message": "",
        "delivery_id": "",
    }


def _decision_record(store: dict, spec_dir: str) -> dict[str, str]:
    """Test projection of durable entries as decision id to option."""
    return {
        entry["decision_id"]: entry["option"]
        for entry in routes._decision_entries(store, spec_dir).values()
    }


async def _recorded_answers(spec_dir: str) -> dict[str, str]:
    """Read the test projection without blocking the event loop."""

    def _read() -> dict[str, str]:
        with routes._DECISIONS_LOCK:
            store, _usable = routes._read_decisions()
            return _decision_record(store, spec_dir)

    return await asyncio.to_thread(_read)


def _decision_spec(tmp_path, *, state: dict | None = _DEFAULT_DECISION_STATE) -> tuple[str, str]:
    """Index one spec named ``s`` and return ``(spec_dir, slot_key)``."""
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        (spec_dir / ".spec-state.json").write_text(json.dumps(state))
    slot_key = routes._new_slot_key("s")
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": slot_key,
            }
        }
    )
    return str(spec_dir), slot_key


def test_decision_fingerprint_ignores_option_order():
    """Reordering the same choices cannot make a settled question answerable again."""
    first = {
        "id": "transport",
        "title": "Inbound transport",
        "options": ["HTTPS", "WebSocket"],
    }
    reordered = {
        "id": "transport",
        "title": "Inbound transport",
        "options": ["WebSocket", "HTTPS"],
    }

    assert routes._decision_fingerprint(first) == routes._decision_fingerprint(reordered)


@pytest.mark.asyncio
async def test_reusing_an_id_for_a_different_question_does_not_inherit_the_answer(
    tmp_path, monkeypatch
):
    """The id is only one field of a decision's identity.

    An agent may legitimately reuse a short id after replacing a question. Matching the
    durable answer by id alone locks the replacement card to a choice that it never
    offered and prevents the user from answering the new question.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)

    state, _slot = _slot_stub()
    state._background_tasks = set()
    await client.start_server()
    try:
        client.app["state"] = state
        first = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        assert first.status == 200, await first.json()

        Path(spec_dir, ".spec-state.json").write_text(
            json.dumps(
                {
                    "decisions": [
                        {
                            "id": "transport",
                            "title": "Storage engine",
                            "options": ["SQLite", "PostgreSQL"],
                        }
                    ]
                }
            )
        )
        detail = await (await client.get(f"{_BASE}/specs/s")).json()
        replacement = detail["state"]["decisions"][0]

        second = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "SQLite",
                "text": "Decision - Storage engine: SQLite",
            },
        )
        assert second.status == 200, await second.json()
        if state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

        # Re-emitting the original fingerprint must find its original immutable
        # answer. Keeping only the newest fingerprint lets this third card overwrite
        # the first record and reverse a choice the agent already consumed.
        Path(spec_dir, ".spec-state.json").write_text(json.dumps(_DEFAULT_DECISION_STATE))
        third = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "WebSocket",
                "text": "Decision - Inbound transport: WebSocket",
            },
        )
        third_payload = await third.json()
    finally:
        await client.close()

    assert replacement["locked"] is False
    assert replacement["answer"] == ""
    assert third.status == 409, third_payload
    assert third_payload.get("code") == "decision_already_answered", third_payload
    assert third_payload.get("answer") == "HTTPS", third_payload
    assert dispatched == [
        "Decision - Inbound transport: HTTPS",
        "Decision - Storage engine: SQLite",
    ]


@pytest.mark.asyncio
async def test_a_claim_for_an_absent_decision_is_refused(tmp_path, monkeypatch):
    """A stale card cannot mint a durable answer after its question disappeared."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"decisions": []})
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_not_found", payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_claim_is_refused_when_decision_state_is_unreadable(tmp_path, monkeypatch):
    """A failed state read cannot establish which question the stale card names."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    Path(spec_dir, ".spec-state.json").write_text("{not json")
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "decision_state_unreadable", payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_claim_left_pending_by_a_crash_is_replayed_only_by_a_post(tmp_path, monkeypatch):
    """The durable write must be an outbox, not a premature delivered receipt.

    This is the exact crash window: the claim reaches disk and the process exits before
    `_dispatch_turn`. A replacement gateway's first detail poll may report that recovery
    is needed, but it must stay read-only; the authenticated recovery POST delivers that
    same pending message and then makes the answer final.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)

    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-crash",
    )
    assert outcome == routes._CLAIM_RECORDED

    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    state, _slot = _slot_stub()
    state._background_tasks = set()
    await client.start_server()
    try:
        client.app["state"] = state
        detail_resp = await client.get(f"{_BASE}/specs/s")
        detail = await detail_resp.json()
        assert detail_resp.status == 200, detail
        assert detail.get("decision_recovery_pending") is True
        assert dispatched == [], "a GET request dispatched an agent turn"
        assert [
            entry.get("delivery_id") for entry in await routes._pending_decisions(spec_dir)
        ] == ["delivery-crash"]

        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": True}
    assert dispatched == ["Decision - Inbound transport: HTTPS"]
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    pending = await routes._pending_decisions(spec_dir)
    assert pending == [], "the replayed outbox entry was not marked final"

    outcome, held = await routes._claim_decision(
        "s",
        "transport",
        "Streaming",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: Streaming",
        delivery_id="delivery-later",
    )
    assert (outcome, held) == (routes._CLAIM_TAKEN, "HTTPS")


@pytest.mark.asyncio
async def test_crash_replay_preserves_a_maximum_length_decision_option(tmp_path, monkeypatch):
    """The durable prompt is bounded without truncating the selected option."""
    title = "T" * routes._MAX_FIELD
    option = "O" * routes._MAX_FIELD
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "transport", "title": title, "options": [option]}]},
    )
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    prompt = routes._decision_answer_prompt(decision, option)
    assert len(prompt) <= routes._MAX_DECISION_PROMPT
    assert prompt.endswith(option)

    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        option,
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message=prompt,
        delivery_id="delivery-long-option",
    )
    assert outcome == routes._CLAIM_RECORDED

    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    state, _slot = _slot_stub()
    state._background_tasks = set()
    await client.start_server()
    try:
        client.app["state"] = state
        detail_resp = await client.get(f"{_BASE}/specs/s")
        detail = await detail_resp.json()
        assert detail_resp.status == 200, detail
        assert detail.get("decision_recovery_pending") is True
        assert dispatched == []
        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": True}
    assert dispatched == [prompt]
    assert dispatched[0].endswith(option)
    source = inspect.getsource(routes._handle_message)
    assert "message=_decision_answer_prompt(current_decision, decision_option)" in source


@pytest.mark.asyncio
async def test_replay_abandons_an_answer_for_a_question_that_was_replaced(tmp_path, monkeypatch):
    """Crash recovery must revalidate the durable prompt before relaying it.

    A channel turn can replace the decision after the answer is claimed but before
    delivery. The surviving outbox row names the old fingerprint, so replay must drop
    that unconsumed row rather than inject a stale answer into the new conversation.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-stale",
    )
    assert outcome == routes._CLAIM_RECORDED
    Path(spec_dir, ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "id": "storage",
                        "title": "Storage engine",
                        "options": ["SQLite", "PostgreSQL"],
                    }
                ]
            }
        )
    )

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    state._background_tasks = set()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.get(f"{_BASE}/specs/s")
        payload = await resp.json()
        assert resp.status == 200, payload
        assert payload.get("decision_recovery_pending") is True
        assert dispatched == []
        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": False}
    assert dispatched == []
    assert await routes._pending_decisions(spec_dir) == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_replay_retains_a_relayed_answer_when_the_model_may_have_consumed_it(
    tmp_path, monkeypatch
):
    """A post-consumption crash must not erase the durable one-way decision.

    The model can consume Q1, write Q2, and then lose the gateway before the watcher
    marks Q1 final. Its persisted delivery row proves relay but not consumption, so
    the durable pre-model relay boundary is the surviving ambiguity marker. Recovery
    cannot safely resend or delete the row; retaining it is the only fail-closed answer.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-maybe-consumed",
    )
    assert outcome == routes._CLAIM_RECORDED
    assert await routes._mark_decision_relayed(
        spec_dir,
        "transport",
        fingerprint,
        "delivery-maybe-consumed",
    )
    Path(spec_dir, ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "id": "storage",
                        "title": "Storage engine",
                        "options": ["SQLite", "PostgreSQL"],
                    }
                ]
            }
        )
    )
    pending = (await routes._pending_decisions(spec_dir))[0]
    state, slot = _slot_stub()
    state._background_tasks = set()
    slot.messages = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is False
    assert dispatched == []
    retained = await routes._pending_decisions(spec_dir)
    assert [entry.get("delivery_id") for entry in retained] == ["delivery-maybe-consumed"]
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_replay_skips_a_stale_relay_that_precedes_a_current_answer(tmp_path, monkeypatch):
    """A retained ambiguity marker cannot permanently starve a newer answer."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    first, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and first is not None
    first_fingerprint = routes._decision_fingerprint(first)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=first_fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-stale-relay",
    )
    assert outcome == routes._CLAIM_RECORDED
    assert await routes._mark_decision_relayed(
        spec_dir,
        "transport",
        first_fingerprint,
        "delivery-stale-relay",
    )

    Path(spec_dir, ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "id": "storage",
                        "title": "Storage engine",
                        "options": ["SQLite", "PostgreSQL"],
                    }
                ]
            }
        )
    )
    second, usable = routes._current_decision(Path(spec_dir), "storage")
    assert usable and second is not None
    outcome, _held = await routes._claim_decision(
        "s",
        "storage",
        "SQLite",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=routes._decision_fingerprint(second),
        message="Decision - Storage engine: SQLite",
        delivery_id="delivery-current",
    )
    assert outcome == routes._CLAIM_RECORDED

    state, slot = _slot_stub()
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **_k: dispatched.append(a[2]))

    assert await routes._replay_pending_decision(
        state,
        slot,
        "s",
        routes._load_index()["s"],
    )
    assert dispatched == ["Decision - Storage engine: SQLite"]
    retained = await routes._pending_decisions(spec_dir)
    assert retained[0]["delivery_id"] == "delivery-stale-relay"


@pytest.mark.asyncio
async def test_delivery_refuses_to_start_when_the_relay_boundary_cannot_be_saved(
    tmp_path, monkeypatch
):
    """The model cannot consume an answer whose durable ambiguity marker failed."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-no-boundary",
    )
    assert outcome == routes._CLAIM_RECORDED
    pending = (await routes._pending_decisions(spec_dir))[0]
    state, slot = _slot_stub()
    state._background_tasks = set()
    dispatched: list[str] = []

    async def _fail_relay_boundary(*_args, **_kwargs):
        return False

    monkeypatch.setattr(routes, "_mark_decision_relayed", _fail_relay_boundary)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is False
    assert dispatched == []
    retained = await routes._pending_decisions(spec_dir)
    assert [(entry.get("status"), entry.get("delivery_id")) for entry in retained] == [
        ("pending", "delivery-no-boundary")
    ]


@pytest.mark.asyncio
async def test_a_turn_that_takes_the_slot_during_relay_restores_pending(tmp_path, monkeypatch):
    """A pre-dispatch refusal cannot retain a relay the model never received."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-raced",
    )
    assert outcome == routes._CLAIM_RECORDED
    pending = (await routes._pending_decisions(spec_dir))[0]
    state, slot = _slot_stub()
    state._background_tasks = set()
    reservation = asyncio.create_task(asyncio.sleep(0))
    await reservation
    slot.task = reservation
    real_mark_relayed = routes._mark_decision_relayed

    async def _mark_then_lose_reservation(*args, **kwargs):
        marked = await real_mark_relayed(*args, **kwargs)
        rival = asyncio.create_task(asyncio.sleep(0))
        await rival
        slot.task = rival
        return marked

    monkeypatch.setattr(routes, "_mark_decision_relayed", _mark_then_lose_reservation)
    monkeypatch.setattr(
        routes,
        "_dispatch_turn",
        lambda *_args, **_kwargs: pytest.fail("the raced answer was dispatched"),
    )

    assert not await routes._deliver_pending_decision(
        state,
        slot,
        spec_dir,
        pending,
        turn_reservation=reservation,
    )
    retained = await routes._pending_decisions(spec_dir)
    assert [(entry.get("status"), entry.get("delivery_id")) for entry in retained] == [
        ("pending", "delivery-raced")
    ]


@pytest.mark.asyncio
async def test_a_channel_turn_replacing_the_question_during_claim_cannot_receive_stale_answer(
    tmp_path, monkeypatch
):
    """The slot reservation and final revalidation close the generic-chat race."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    state, slot = _slot_stub()
    state._background_tasks = set()
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    real_claim = routes._claim_decision
    reservation_seen = False

    async def _claim_while_channel_turn_replaces_question(*args, **kwargs):
        nonlocal reservation_seen
        reservation_seen = slot.task is asyncio.current_task()
        outcome = await real_claim(*args, **kwargs)
        Path(spec_dir, ".spec-state.json").write_text(
            json.dumps(
                {
                    "decisions": [
                        {
                            "id": "storage",
                            "title": "Storage engine",
                            "options": ["SQLite", "PostgreSQL"],
                        }
                    ]
                }
            )
        )

        async def _completed_channel_turn() -> None:
            return None

        rival = asyncio.create_task(_completed_channel_turn())
        await rival
        slot.task = rival
        return outcome

    monkeypatch.setattr(routes, "_claim_decision", _claim_while_channel_turn_replaces_question)
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert reservation_seen, "generic chat could still observe the slot as idle"
    assert resp.status == 409, payload
    assert payload.get("code") == "decision_changed_before_delivery", payload
    assert dispatched == []
    assert await routes._pending_decisions(spec_dir) == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_message_queued_behind_a_refused_reservation_is_drained(monkeypatch):
    """A validation refusal must not strand generic chat queued behind it."""
    from kiro_crew.dashboard import chat_runner

    state, slot = _slot_stub()
    slot._queue = []
    drained = asyncio.Event()

    async def _drain(_state, _slot):
        assert _state is state
        assert _slot is slot
        drained.set()
        return True

    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _drain)

    async def _refused_request() -> None:
        reservation = routes._reserve_slot_turn(state, slot)
        assert reservation is asyncio.current_task()
        slot._queue.append({"id": "queued", "content": "continue"})

    request_task = asyncio.create_task(_refused_request())
    await request_task
    await asyncio.wait_for(drained.wait(), timeout=1)
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))


@pytest.mark.asyncio
async def test_recovery_replays_an_unconsumed_delivery_without_a_second_chat_row(
    tmp_path, monkeypatch
):
    """A durable chat row proves display, not model consumption."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-relayed",
    )
    assert outcome == routes._CLAIM_RECORDED
    assert await routes._mark_decision_relayed(
        spec_dir,
        "transport",
        fingerprint,
        "delivery-relayed",
    )

    dispatched: list[tuple[str, bool]] = []
    consumed_callbacks: list[Callable[[bool], None]] = []

    def _dispatch(*args, **kwargs):
        dispatched.append((args[2], kwargs.get("append_user", True)))
        consumed_callbacks.append(kwargs["on_consumed"])

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    state, slot = _slot_stub()
    state._background_tasks = set()
    slot.messages = [
        {
            "role": "user",
            "content": "Decision - Inbound transport: HTTPS",
            "meta": {"spec_decision_delivery_id": "delivery-relayed"},
        }
    ]
    await client.start_server()
    try:
        client.app["state"] = state
        detail_resp = await client.get(f"{_BASE}/specs/s")
        detail = await detail_resp.json()
        assert detail_resp.status == 200, detail
        assert detail.get("decision_recovery_pending") is True
        assert dispatched == [], "a GET request dispatched an agent turn"

        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": True}
    assert dispatched == [("Decision - Inbound transport: HTTPS", False)]
    assert await routes._pending_decisions(
        spec_dir
    ), "a transcript marker finalized the answer before the model consumed it"

    async def _report_consumed() -> None:
        consumed_callbacks[0](True)

    await asyncio.create_task(_report_consumed())
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    assert await routes._pending_decisions(spec_dir) == []


@pytest.mark.asyncio
async def test_a_consumed_delivery_cannot_replay_during_or_after_finalization(
    tmp_path, monkeypatch
):
    """A detail poll's stale pending snapshot must not duplicate a consumed answer."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-settling",
    )
    assert outcome == routes._CLAIM_RECORDED
    stale_pending = (await routes._pending_decisions(spec_dir))[0]

    state, slot = _slot_stub()
    state._background_tasks = set()
    alias_slot = types.SimpleNamespace(running=False, messages=[], task=None)
    turns: list[asyncio.Task[None]] = []
    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        async def _consuming_turn() -> None:
            kwargs["on_consumed"](True)

        dispatched.append(args[2])
        turn = asyncio.create_task(_consuming_turn())
        slot.task = turn
        turns.append(turn)
        return turn

    finalize_started = asyncio.Event()
    allow_finalize = asyncio.Event()
    real_finalize = routes._finalize_decision

    async def _slow_finalize(*args, **kwargs):
        finalize_started.set()
        await allow_finalize.wait()
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    monkeypatch.setattr(routes, "_finalize_decision", _slow_finalize)

    assert await routes._deliver_pending_decision(state, slot, spec_dir, stale_pending) is True
    await turns[0]
    await finalize_started.wait()
    assert (
        await routes._deliver_pending_decision(state, alias_slot, spec_dir, stale_pending) is False
    ), "an alias slot bypassed the directory-wide in-flight settlement"

    allow_finalize.set()
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    assert await routes._pending_decisions(spec_dir) == []
    assert (
        await routes._deliver_pending_decision(state, alias_slot, spec_dir, stale_pending) is False
    ), "a stale pending snapshot bypassed the durable final status"
    assert dispatched == ["Decision - Inbound transport: HTTPS"]


@pytest.mark.asyncio
async def test_irreversible_consumption_finalizes_before_the_turn_finishes(tmp_path, monkeypatch):
    """A streamed token closes the crash-replay window immediately."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-irrevocable",
    )
    assert outcome == routes._CLAIM_RECORDED
    pending = (await routes._pending_decisions(spec_dir))[0]

    state, slot = _slot_stub()
    state._background_tasks = set()
    irreversible_reported = asyncio.Event()
    allow_turn_finish = asyncio.Event()
    finalize_done = asyncio.Event()
    real_finalize = routes._finalize_decision

    def _dispatch(*_args, **kwargs):
        on_irreversibly_consumed = kwargs["on_irreversibly_consumed"]

        async def _streaming_turn() -> None:
            await on_irreversibly_consumed()
            irreversible_reported.set()
            await allow_turn_finish.wait()

        turn = asyncio.create_task(_streaming_turn())
        slot.task = turn
        return turn

    async def _observe_finalize(*args, **kwargs):
        finalized = await real_finalize(*args, **kwargs)
        finalize_done.set()
        return finalized

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    monkeypatch.setattr(routes, "_finalize_decision", _observe_finalize)

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is True
    await irreversible_reported.wait()
    await finalize_done.wait()
    assert not allow_turn_finish.is_set()
    assert await routes._pending_decisions(spec_dir) == []

    allow_turn_finish.set()
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))


@pytest.mark.asyncio
async def test_a_failed_finalization_retries_without_replaying_the_consumed_answer(
    tmp_path, monkeypatch
):
    """A transient ledger failure keeps the process-local consumption claim."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-finalize-retry",
    )
    assert outcome == routes._CLAIM_RECORDED
    stale_pending = (await routes._pending_decisions(spec_dir))[0]

    state, slot = _slot_stub()
    state._background_tasks = set()
    alias_slot = types.SimpleNamespace(running=False, messages=[], task=None)
    turns: list[asyncio.Task[None]] = []
    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        async def _consuming_turn() -> None:
            kwargs["on_consumed"](True)

        dispatched.append(args[2])
        turn = asyncio.create_task(_consuming_turn())
        slot.task = turn
        turns.append(turn)
        return turn

    real_finalize = routes._finalize_decision
    finalize_calls = 0

    async def _fail_once_then_finalize(*args, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            return False
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    monkeypatch.setattr(routes, "_finalize_decision", _fail_once_then_finalize)

    assert await routes._deliver_pending_decision(state, slot, spec_dir, stale_pending) is True
    await turns[0]
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    assert await routes._pending_decisions(spec_dir)

    assert (
        await routes._deliver_pending_decision(state, alias_slot, spec_dir, stale_pending) is False
    )
    assert await routes._pending_decisions(spec_dir) == []
    assert finalize_calls == 2
    assert dispatched == ["Decision - Inbound transport: HTTPS"]


@pytest.mark.asyncio
async def test_a_retracted_consumption_report_leaves_the_delivery_pending(tmp_path, monkeypatch):
    """The first empty response reports completion, then retracts before return."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-empty",
    )
    assert outcome == routes._CLAIM_RECORDED

    state, slot = _slot_stub()
    state._background_tasks = set()
    turns: list[asyncio.Task[None]] = []

    def _dispatch(*_args, **kwargs):
        async def _empty_turn() -> None:
            kwargs["on_consumed"](True)
            kwargs["on_consumed"](False)

        turn = asyncio.create_task(_empty_turn())
        turns.append(turn)
        return turn

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    pending = (await routes._pending_decisions(spec_dir))[0]

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is True
    await turns[0]
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))

    assert await routes._pending_decisions(
        spec_dir
    ), "the transient completion report finalized a prompt that was requeued"


@pytest.mark.asyncio
async def test_second_answer_to_one_decision_is_refused(tmp_path, monkeypatch):
    """The first answer dispatches; a later one for the same decision does not.

    This is the case the state file cannot catch: the agent has not written
    ``answer`` yet (its turn has not run), so nothing on disk says the decision is
    settled -- only this backend's record does."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    dispatched: list = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)

    state, _slot = _slot_stub()
    state._background_tasks = set()
    await client.start_server()
    try:
        client.app["state"] = state
        body = {"spec_dir": spec_dir, "slot_key": slot_key, "decision_id": "transport"}
        first = await client.post(
            f"{_BASE}/specs/s/message",
            json={**body, "decision_option": "HTTPS", "text": "Decision - transport: HTTPS"},
        )
        second = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                **body,
                "decision_option": "Streaming",
                "text": "Decision - transport: Streaming",
            },
        )
        payload = await second.json()
    finally:
        await client.close()

    assert first.status == 200
    assert second.status == 409, payload
    assert payload.get("code") == "decision_already_answered", payload
    # The refusal names the answer the agent actually has, so the client can show
    # the settled state rather than a bare error.
    assert payload.get("answer") == "HTTPS", payload
    assert dispatched == [
        "Decision - Inbound transport: HTTPS"
    ], "a second answer reached the agent, reversing a settled decision"


@pytest.mark.asyncio
async def test_concurrent_answers_to_one_decision_dispatch_once(tmp_path, monkeypatch):
    """The double-click race. Both requests read the decision as unanswered, so
    only an atomic claim can keep one of them out -- a client-side lock cannot,
    and neither can a check-then-write."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        body = {"spec_dir": spec_dir, "slot_key": slot_key, "decision_id": "transport"}
        results = await asyncio.gather(
            client.post(
                f"{_BASE}/specs/s/message", json={**body, "decision_option": "A", "text": "A"}
            ),
            client.post(
                f"{_BASE}/specs/s/message", json={**body, "decision_option": "B", "text": "B"}
            ),
        )
        statuses = sorted(r.status for r in results)
    finally:
        await client.close()

    assert statuses == [200, 409], statuses
    assert len(dispatched) == 1, f"both clicks reached the agent: {dispatched}"


@pytest.mark.asyncio
async def test_a_plain_message_is_never_locked(tmp_path, monkeypatch):
    """Only a decision answer is one-way. Ordinary chat keeps working, repeatedly,
    and writes nothing to the ledger."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        body = {"spec_dir": spec_dir, "slot_key": slot_key}
        one = await client.post(f"{_BASE}/specs/s/message", json={**body, "text": "looks good"})
        two = await client.post(f"{_BASE}/specs/s/message", json={**body, "text": "looks good"})
    finally:
        await client.close()

    assert (one.status, two.status) == (200, 200)
    assert len(dispatched) == 2
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_re_emitted_pending_decision_reads_as_answered(tmp_path, monkeypatch):
    """The card that came back. The agent re-writes a settled decision id with
    ``answer: null``; the detail read must still report the recorded answer and
    mark it locked, or the UI renders options for a decision that is closed."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={
            "decisions": [
                {
                    "id": "transport",
                    "title": "Inbound transport",
                    "options": ["HTTPS", "Streaming"],
                    "answer": None,
                }
            ]
        },
    )
    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )
    assert outcome == routes._CLAIM_RECORDED

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        client.app["state"] = _slot_stub()[0]
        resp = await client.get(f"{_BASE}/specs/s")
        data = await resp.json()

    assert resp.status == 200, data
    decision = data["state"]["decisions"][0]
    assert decision["answer"] == "HTTPS", "the agent's pending re-emission won"
    assert decision["locked"] is True


@pytest.mark.asyncio
async def test_a_recorded_answer_is_redacted_on_egress(tmp_path, monkeypatch):
    """index.json is agent-writable, so the ledger is untrusted input like every
    other field -- and the overlay bypasses the state file's own scrub."""
    _redirect_state(monkeypatch, tmp_path)
    secret = "AKIAIOSFODNN7EXAMPLE"
    spec_dir, _slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "d", "title": "Which key", "answer": None}]},
    )
    routes._save_decisions(
        {
            routes._decision_key(spec_dir): {
                "name": "s",
                "answers": {"d": _final_answer("d", f"use {secret}")},
            }
        }
    )

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        client.app["state"] = _slot_stub()[0]
        resp = await client.get(f"{_BASE}/specs/s")
        data = await resp.json()

    assert secret not in json.dumps(data["state"])


@pytest.mark.asyncio
async def test_a_claim_is_pinned_to_the_spec_it_was_made_for(tmp_path, monkeypatch):
    """A delete-and-recreate in flight must not have an answer stamped onto the
    replacement's entry -- same identity pin every other mutation takes."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    wrong_dir, _ = await routes._claim_decision(
        "s", "d", "A", expect_spec_dir=str(tmp_path / "elsewhere"), expect_slot_key=slot_key
    )
    wrong_key, _ = await routes._claim_decision(
        "s", "d", "A", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-other"
    )

    assert wrong_dir == routes._CLAIM_STALE
    assert wrong_key == routes._CLAIM_STALE
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_full_ledger_refuses_rather_than_dispatching_unrecorded(tmp_path, monkeypatch):
    """The cap fails CLOSED. Dispatching an answer this backend could not record
    would produce exactly the re-answerable card the ledger exists to prevent."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "one-too-many", "title": "Last decision", "options": ["A"]}]},
    )
    # Simulate Windows, where the ledger contract case-folds every directory key.
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path.upper())
    routes._save_decisions(
        {
            routes._decision_key(spec_dir): {
                "name": "s",
                "answers": {
                    f"d{i}": _final_answer(f"d{i}", "A") for i in range(routes._MAX_RECORDED)
                },
            }
        }
    )

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "one-too-many",
                "decision_option": "A",
                "text": "A",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_ledger_full", payload
    assert dispatched == []


def test_the_claim_is_a_pending_outbox_before_delivery():
    """Source guard for the recoverable claim -> relay -> final ordering."""
    src = inspect.getsource(routes._handle_message)
    claim = src.index("_claim_decision(")
    deliver = src.index("_deliver_pending_decision(")
    assert claim < deliver, "the decision is relayed before its pending claim is durable"
    lock = src.index("async with _turn_lock(")
    assert lock < claim and lock < deliver, (
        "the claim and delivery are not both inside the turn lock, so another handler "
        "can interleave with the outbox relay"
    )
    claim_src = inspect.getsource(routes._claim_decision_locked)
    assert '"status": "pending" if delivery_id else "final"' in claim_src
    deliver_src = inspect.getsource(routes._deliver_pending_decision)
    assert "on_consumed" in deliver_src
    assert "_finalize_decision(" in deliver_src


@pytest.mark.asyncio
async def test_a_claim_is_refused_while_a_delete_is_reserved(tmp_path, monkeypatch):
    """The claim must honour the delete reservation, like every other mutation.

    Without it the claim commits (spec_dir still matches) while the pre-dispatch
    re-pin refuses, because THAT pin does honour the marker. The answer is then
    recorded and never sent -- and a delete that rolls back leaves the entry alive
    with a decision locked to an answer the agent never received, which nothing
    can re-open."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    async def _reserve(idx_name: str) -> None:
        def _apply(index: dict) -> bool:
            index[idx_name][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
            return True

        await routes._mutate_index(_apply)

    await _reserve("s")
    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )

    assert outcome == routes._CLAIM_STALE
    assert (
        await _recorded_answers(spec_dir) == {}
    ), "an answer was recorded onto a spec whose delete was in flight"


@pytest.mark.asyncio
async def test_the_ledger_key_matches_the_id_the_detail_read_serves(tmp_path, monkeypatch):
    """The overlay matches the ledger KEY against the id from the state file, so
    the two must be normalized identically.

    An id carrying whitespace (or longer than a tighter cap) used to be stripped
    on the wire but not in the state projection. The mismatch is silent in the
    worst way: the answer IS recorded, no card is ever locked, and the decision
    stays re-answerable forever."""
    client = _make_client(monkeypatch, tmp_path)
    raw_id = "  transport shape  "
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": raw_id, "title": "Inbound transport", "options": ["HTTPS"]}]},
    )

    def _dispatch(*_args, **kwargs):
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        # The SPA echoes back the id the detail read gave it, verbatim.
        served = routes._normalize_spec_state(
            json.loads((Path(spec_dir) / ".spec-state.json").read_text())
        )
        assert served is not None
        served_id = served["decisions"][0]["id"]
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": served_id,
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        assert resp.status == 200, await resp.json()
        if state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))
        detail = await (await client.get(f"{_BASE}/specs/s")).json()
    finally:
        await client.close()

    decision = detail["state"]["decisions"][0]
    assert (
        decision["locked"] is True
    ), "the recorded answer did not match the served id, so the card is still clickable"
    assert decision["answer"] == "HTTPS"


@pytest.mark.asyncio
async def test_an_answer_without_its_option_is_refused(tmp_path, monkeypatch):
    """The option is what gets recorded and later rendered. A decision answer that
    carries no option cannot be recorded honestly, so it is refused rather than
    falling back to the composed prompt."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 400, payload
    assert payload.get("code") == "decision_option_required", payload
    assert dispatched == []


@pytest.mark.asyncio
async def test_the_card_shows_the_option_not_the_whole_prompt(tmp_path, monkeypatch):
    """The agent receives a canonical prompt while the card records only the choice."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={
            "decisions": [{"id": "transport", "title": "Inbound transport", "options": ["HTTPS"]}]
        },
    )
    dispatched: list = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)

    state, _slot = _slot_stub()
    state._background_tasks = set()
    await client.start_server()
    try:
        client.app["state"] = state
        client_prompt = "Decision — “Inbound transport”: HTTPS"
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": client_prompt,
            },
        )
        assert resp.status == 200, await resp.json()
        if state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))
        detail = await (await client.get(f"{_BASE}/specs/s")).json()
    finally:
        await client.close()

    assert dispatched == [
        "Decision - Inbound transport: HTTPS"
    ], "the agent must receive the server-built prompt"
    assert (
        detail["state"]["decisions"][0]["answer"] == "HTTPS"
    ), "the card renders the composed prompt instead of the chosen option"


@pytest.mark.asyncio
async def test_a_delete_that_lands_after_the_repin_strands_no_claim(tmp_path, monkeypatch):
    """The window the in-claim reservation check does NOT cover, closed by ordering.

    A DELETE reserving after the pre-dispatch re-pin used to reach a claim that had
    already committed: the dispatch was refused, and when the delete rolled back the
    spec came back with a decision locked to an answer the agent never received.
    With the claim as the last await, the same delete makes the CLAIM refuse, so
    nothing is recorded and the user can answer again."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    real_touch = routes._touch_spec
    calls: list[int] = []

    async def _touch_then_reserve(name, **kw):
        out = await real_touch(name, **kw)
        calls.append(1)
        # The second _touch_spec is the pre-dispatch re-pin; the delete reserves
        # immediately after it returns, which is the window under test.
        if len(calls) == 2:

            def _apply(index: dict) -> bool:
                index[name][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
                return True

            await routes._mutate_index(_apply)
        return out

    monkeypatch.setattr(routes, "_touch_spec", _touch_then_reserve)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}, (
        "an answer stayed recorded although its dispatch was refused -- the decision "
        "is now locked to something the agent never received"
    )


@pytest.mark.asyncio
async def test_an_answer_is_refused_while_the_agent_is_running(tmp_path, monkeypatch):
    """A running slot QUEUES the message, and Pause clears that queue by design --
    so a queued answer may never be delivered while the ledger claims it was.

    Refused instead of queued, and nothing recorded, so the user can answer again
    once the turn ends."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    # The gate sits BEFORE the claim, so a busy click never touches the ledger at
    # all -- it does not record-then-release, which would churn index.json (and
    # leave a lock behind if the release failed).
    claims: list = []
    real_claim = routes._claim_decision

    async def _record_claim(*a, **kw):
        claims.append(a)
        return await real_claim(*a, **kw)

    monkeypatch.setattr(routes, "_claim_decision", _record_claim)

    state, slot = _slot_stub()
    slot.running = True
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_agent_busy", payload
    assert dispatched == []
    assert claims == [], "a busy click reached the ledger instead of being refused first"
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_plain_message_still_queues_behind_a_running_turn(tmp_path, monkeypatch):
    """The busy refusal is scoped to decision answers. Ordinary chat keeps its
    queue-behind-the-turn behaviour -- that is how a user steers a working agent."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, slot = _slot_stub()
    slot.running = True
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "also check the auth flow"},
        )
    finally:
        await client.close()

    assert resp.status == 200
    assert dispatched == ["also check the auth flow"]


def test_the_decision_ledger_is_not_in_the_agent_writable_index():
    """The record must not live on ``index.json``.

    That file is agent-writable by design (its own docstrings say so), so a ledger
    field on it could be erased to re-open a settled decision or forged to lock one
    the user never answered -- and a forged entry also puts an answer on screen
    that nobody chose."""
    src = routes_source()
    assert (
        "answered_decisions" not in src
    ), "the ledger is back on an index entry, where the agent can rewrite it"
    write = inspect.getsource(routes._claim_decision_locked)
    assert "_mutate_index(" not in write, (
        "the claim writes through the index mutator again, so the record is " "agent-writable"
    )
    assert "_DECISIONS_LOCK" in write
    assert "_save_decisions(" in write


@pytest.mark.parametrize(
    "path",
    [
        "~/.kiro/crew/trust/spec-builder-decisions.json",
        "~/.kirocrew/trust/spec-builder-decisions.json",
        # The PARENT, which is the half a leaf-only entry missed.
        "~/.kiro/crew/trust",
        "~/.kirocrew/trust",
    ],
)
def test_the_decision_ledger_is_on_the_security_keystone(path):
    """Read+write keystone, like the Notes vault registry and the Ops Mission
    Control policy: app-owned, not a secret, but forging or erasing it defeats the
    app's safety property, so the agent must reach it through neither the file
    tools nor a shell.

    The PARENT is gated too. Under this app's own state dir it was not: a directory
    below ``workspace/`` is not a sensitive path, so one ``ln -s`` naming it
    redirected every read and write, and this backend opens the path directly (as
    keystone writers must) so it would have followed the link."""
    from kiro_crew import security

    assert security.is_sensitive_path(path)
    assert security.is_sensitive_bash_command(f"echo x > {path}") is not None
    assert security.is_sensitive_bash_command(f"cat {path}") is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "ln -s /tmp/evil ~/.kiro/crew/trust",
        "ln -sf /tmp/evil ~/.kiro/crew/trust/spec-builder-decisions.json",
        "mv ~/.kiro/crew/trust /tmp/x",
        "mv /tmp/evil ~/.kiro/crew/trust/spec-builder-decisions.json",
        "rm -rf ~/.kiro/crew/trust",
        "cp /tmp/evil ~/.kiro/crew/trust/spec-builder-decisions.json",
    ],
)
def test_the_ledger_directory_cannot_be_swapped_or_removed(cmd):
    """The reported vector: replace the ledger's PARENT and the backend follows it.
    Every verb that could repoint or destroy the directory (or plant a file in it)
    has to be refused, not just a read or a redirect at the leaf."""
    from kiro_crew import security

    assert security.is_sensitive_bash_command(cmd) is not None, cmd


def test_the_ledger_is_not_under_the_apps_own_state_dir(tmp_path, monkeypatch):
    """Source/behaviour guard: the default location must be the gated trust root.

    Anything under the app's state dir has a replaceable parent, which is the defect
    this move fixes -- and it would come back silently, because the app opens the
    path directly and a symlinked parent reads and writes exactly like a real one."""
    monkeypatch.setattr(routes, "_DECISIONS_PATH", None)
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")

    path = routes._decisions_path()

    assert path.parent.name == "trust", path
    assert (tmp_path / "state") not in path.parents, path
    from kiro_crew import security

    assert security.is_sensitive_path(str(path))
    assert security.is_sensitive_path(str(path.parent))


@pytest.mark.asyncio
async def test_a_forged_ledger_for_another_spec_dir_is_ignored(tmp_path, monkeypatch):
    """A record is scoped to the spec_dir it was written for. A delete leaves the
    documents on disk, so a re-import under the same name at a different path is a
    DIFFERENT spec and must start with no answers."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    routes._save_decisions(
        {
            str(tmp_path / "somewhere-else"): {
                "name": "s",
                "answers": {"d": _final_answer("d", "A")},
            }
        }
    )

    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_deleting_a_spec_forgets_its_answers(tmp_path, monkeypatch):
    """Otherwise a re-import at the same name AND path inherits locks from the spec
    that was deleted, and its cards are settled before the user answers anything."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_torn_ledger_file_reads_as_nothing_recorded(tmp_path, monkeypatch):
    """Degrade toward answerable, never toward a locked card nobody can clear, and
    never toward raising inside a request."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    routes._decisions_path().parent.mkdir(parents=True, exist_ok=True)
    routes._decisions_path().write_text("{ not json")

    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_an_unreadable_ledger_refuses_the_write_instead_of_erasing_it(tmp_path, monkeypatch):
    """A corrupt read must not become data loss.

    Treating an unreadable file as an empty ledger and saving over it would erase
    every recorded answer -- for every spec -- and make settled decisions answerable
    again. Reads still fail soft (the card stays answerable, which is the safe
    direction); WRITES refuse."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    routes._save_decisions(
        {
            "/elsewhere": {
                "name": "other",
                "answers": {"kept": _final_answer("kept", "yes")},
            }
        }
    )
    corrupt = routes._decisions_path().read_text()[:-3]  # truncate the JSON
    routes._decisions_path().write_text(corrupt)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "decision_record_unreadable", payload
    assert dispatched == []
    # The unreadable file is left EXACTLY as it was rather than replaced.
    assert routes._decisions_path().read_text() == corrupt


@pytest.mark.asyncio
async def test_a_delete_reserved_during_the_claim_records_nothing(tmp_path, monkeypatch):
    """The window a separate liveness hop left open: a DELETE reserving between the
    check and the write. Both now happen under ``_INDEX_LOCK`` in one hop, and
    ``_mark_deleting`` takes that same lock, so the two serialize."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert await _mark_reserved("s")

    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )

    assert outcome == routes._CLAIM_STALE
    assert await _recorded_answers(spec_dir) == {}


def test_the_claim_checks_liveness_and_writes_in_one_transaction():
    """Source guard: the index read and the ledger write must sit inside the SAME
    ``_INDEX_LOCK`` block, not in two hops. Split across hops, a delete reserving in
    between has its answer recorded for a spec already being torn down."""
    src = inspect.getsource(routes._claim_decision_locked)
    lock = src.index("with _INDEX_LOCK:")
    assert src.index("_spec_is_live(") > lock
    assert src.index("_save_decisions(") > lock
    assert "await" not in src, "the transaction body awaits, so it is not one hop"
    # And the async wrapper must do nothing but hand it to a worker thread.
    wrapper = inspect.getsource(routes._claim_decision)
    assert "asyncio.to_thread(" in wrapper
    assert "_read_decisions(" not in wrapper


def test_the_detail_read_scopes_the_slot_from_a_fresh_index_read():
    """Source guard: nothing may await between the detail handler's FRESH index read
    and the ``_ensure_worker_slot`` call that consumes its ``meta``.

    A delete-and-re-import in that window hands the replacement's slot the stale
    entry, and the agent's next turn then runs in the OLD project directory. Reading
    this backend's decision ledger separately opened exactly that window; the read
    belongs in the one filesystem hop above instead."""
    src = inspect.getsource(routes._handle_get)
    fresh_call = "await _aload_index_with_decision_alias_status("
    fresh = src.rindex(fresh_call)
    # The CALL, not the comment that names the helper a few lines above it.
    scope = src.index("await _ensure_worker_slot(")
    assert fresh < scope, "the slot is scoped before the index is re-read"
    between = src[fresh + len(fresh_call) : scope]
    awaits = [
        ln.strip()
        for ln in between.splitlines()
        if "await " in ln and not ln.strip().startswith("#")
    ]
    assert (
        not awaits
    ), f"an await sits between the fresh index read and the slot scoping: {awaits!r}"


@pytest.mark.asyncio
async def test_the_detail_read_serves_the_recorded_answer(tmp_path, monkeypatch):
    """The overlay still applies now that it happens inside the document hop."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "transport", "title": "Inbound", "options": ["HTTPS"]}]},
    )
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        client.app["state"] = _slot_stub()[0]
        data = await (await client.get(f"{_BASE}/specs/s")).json()

    decision = data["state"]["decisions"][0]
    assert decision["locked"] is True
    assert decision["answer"] == "HTTPS"


@pytest.mark.asyncio
async def test_a_failed_index_removal_puts_the_answers_back(tmp_path, monkeypatch):
    """A spec that survives a failed delete keeps its answers.

    This is what fixes the ordering rather than compensating for it: the ledger is
    cleared only AFTER the index entry is actually gone, so a failure of the index write
    cannot leave the spec alive with its decisions answerable again. There is no restore
    path to get wrong, because nothing was removed.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    real_mutate = routes._mutate_index
    calls: list[int] = []

    async def _fail_the_final_pop(mutate):
        calls.append(1)
        # The pop is the LAST index mutation of the delete; the reservation and
        # durable teardown boundary before it must still work.
        if len(calls) >= 3:
            return False
        return await real_mutate(mutate)

    monkeypatch.setattr(routes, "_mutate_index", _fail_the_final_pop)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "index_write_failed", payload
    assert await _recorded_answers(spec_dir) == {
        "transport": "HTTPS"
    }, "the spec survived the failed delete with its recorded answers erased"


@pytest.mark.asyncio
async def test_no_turn_can_start_while_an_answer_is_being_recorded(tmp_path, monkeypatch):
    """The turn lock, behaviourally.

    A turn starting between the running-check and the dispatch would have the answer
    QUEUED behind it (and droppable by a Pause) while the ledger claims it was
    delivered. Undoing the claim at that point needs a write that can itself fail, so
    the answer path holds the lock instead: another would-be dispatcher cannot get in
    until this one has dispatched.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    # What the racing dispatcher observed when it finally got the lock: the number of
    # dispatches already made. 0 would mean it cut in before ours.
    seen_at_acquire: list[int] = []
    race: list = []
    real_claim = routes._claim_decision

    async def _claim_while_another_dispatcher_waits(*a, **kw):
        async def _other_dispatcher() -> None:
            # By DIRECTORY, the key the handler uses (see _turn_lock).
            async with routes._turn_lock(spec_dir):
                seen_at_acquire.append(len(dispatched))

        race.append(asyncio.create_task(_other_dispatcher()))
        await asyncio.sleep(0)  # let it reach the lock
        return await real_claim(*a, **kw)

    monkeypatch.setattr(routes, "_claim_decision", _claim_while_another_dispatcher_waits)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        body = await resp.text()
    finally:
        await client.close()
    await asyncio.gather(*race)

    assert resp.status == 200, body
    assert len(dispatched) == 1
    assert seen_at_acquire == [1], (
        "another dispatcher acquired the turn lock before this answer was dispatched, "
        f"so the answer could have been queued: {seen_at_acquire!r}"
    )


def test_every_turn_start_takes_the_turn_lock():
    """Source guard: a dispatcher that skips the lock can start a turn between a
    decision answer's running-check and its dispatch, which is what the lock exists to
    prevent. The CREATE path is exempt: the spec does not exist before it, so no answer
    can be in flight for it."""
    for handler in (routes._handle_message, routes._handle_handoff):
        src = inspect.getsource(handler)
        assert (
            "async with _turn_lock(" in src
        ), f"{handler.__name__} dispatches a turn without taking the turn lock"
        assert src.index("async with _turn_lock(") < src.index(
            "_dispatch_turn("
        ), f"{handler.__name__} takes the lock after dispatching"


@pytest.mark.asyncio
async def test_deleting_a_spec_retains_its_turn_lock(tmp_path, monkeypatch):
    """This test asserted the OPPOSITE until round 24, as housekeeping: a deleted
    spec's lock was dropped rather than accumulating for the process lifetime.

    That was unsafe at any reference count. A handler that called `_turn_lock()`
    before the eviction is already waiting on the OLD object, so the next arrival is
    handed a brand-new lock and the two serialize against nothing -- concurrent turns
    over the same documents, which is the hole the directory-keyed lock exists to
    close, reopened by its own cleanup. A waiter holds the object rather than an index
    entry, so no reference count can make the eviction safe; the lock stays.

    What is given up is one small asyncio.Lock per directory that ever had a turn.
    The ledger clear is unaffected and still asserted here.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    lock = routes._turn_lock(spec_dir)
    assert routes._turn_key(spec_dir) in routes._TURN_LOCKS

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert routes._turn_lock(spec_dir) is lock, (
        "the delete replaced the directory's lock, so a waiter and the next arrival "
        "would serialize on different objects"
    )
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_same_path_reimport_during_the_read_is_refused(tmp_path, monkeypatch):
    """A delete leaves the documents on disk, so a re-import at the same name AND path
    is a DIFFERENT creation. A spec_dir-only freshness check passed it, pairing the
    replacement's metadata with documents and a decision record read for the spec that
    is gone -- serving the deleted spec's locked answers on the new one."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    real_collect = routes._collect_spec_documents

    def _collect_then_reimport(sd):
        # Same name, same path, NEW creation -- exactly what a delete + re-import
        # leaves behind.
        idx = routes._load_index()
        routes._forget_observed_slot_identity("s", str(idx["s"].get("slot_key", "")))
        idx["s"]["slot_key"] = routes._new_slot_key("s")
        routes._save_index(idx)
        return real_collect(sd)

    monkeypatch.setattr(routes, "_collect_spec_documents", _collect_then_reimport)

    await client.start_server()
    try:
        client.app["state"] = _slot_stub()[0]
        resp = await client.get(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_changed_during_read", payload


@pytest.mark.asyncio
async def test_a_failed_ledger_write_is_a_named_refusal_not_a_500(tmp_path, monkeypatch):
    """A full or unwritable data home must not 500.

    A 500 carries no code the client can act on, so its optimistic lock would stay
    while NOTHING was recorded or dispatched -- a locked card for an answer the agent
    never got. The named pre-dispatch refusal is what lets the card re-open."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    def _no_space(_store: dict) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(routes, "_save_decisions", _no_space)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "decision_record_write_failed", payload
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_raised_index_removal_still_restores_the_answers(tmp_path, monkeypatch):
    """``_mutate_index`` can RAISE as well as return False (a full data home), and both
    leave the spec in the index with its answers already cleared. The restore must run
    on either path, or a restart exposes the surviving spec with its settled decisions
    answerable again."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    real_mutate = routes._mutate_index
    calls: list[int] = []

    async def _raise_on_the_final_pop(mutate):
        calls.append(1)
        if len(calls) >= 3:
            raise OSError(28, "No space left on device")
        return await real_mutate(mutate)

    monkeypatch.setattr(routes, "_mutate_index", _raise_on_the_final_pop)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "index_write_failed", payload
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_the_delete_holds_the_turn_lock(tmp_path, monkeypatch):
    """A handoff that has already passed its freshness check would otherwise acquire
    the lock after the delete released it and start a turn on a spec that is gone. The
    delete holds the lock across its whole destructive sequence, so the other handler
    sees either a live spec or none."""
    client = _make_client(monkeypatch, tmp_path)
    _decision_spec(tmp_path)

    # What the racing dispatcher saw when it got the lock: whether the spec was still
    # indexed. With the delete holding the lock it must be gone by then.
    still_indexed: list[bool] = []
    race: list = []
    real_forget = routes._forget_decisions

    async def _forget_while_another_dispatcher_waits(*a, **kw):
        async def _other_dispatcher() -> None:
            async with routes._turn_lock("s"):
                still_indexed.append("s" in await routes._aload_index())

        race.append(asyncio.create_task(_other_dispatcher()))
        await asyncio.sleep(0)
        return await real_forget(*a, **kw)

    monkeypatch.setattr(routes, "_forget_decisions", _forget_while_another_dispatcher_waits)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()
    await asyncio.gather(*race)

    assert resp.status == 200, await resp.text()
    assert still_indexed == [False], (
        "another dispatcher held the lock while the spec was still indexed mid-delete, "
        f"so it could have started a turn on a spec being removed: {still_indexed!r}"
    )


def test_a_turn_lock_from_a_dead_loop_is_not_reused(tmp_path, monkeypatch):
    """An asyncio.Lock binds to the loop that first awaits it, so a module-level
    registry outliving a loop would hand back a lock bound to the dead one and raise
    "is bound to a different event loop" on acquisition.

    Deliberately NOT an asyncio test: it drives two loops of its own, which cannot be
    done from inside a running one."""
    _redirect_state(monkeypatch, tmp_path)

    async def _take() -> int:
        lock = routes._turn_lock("s")
        async with lock:
            return id(lock)

    first = asyncio.new_event_loop()
    try:
        first_id = first.run_until_complete(_take())
    finally:
        first.close()

    second = asyncio.new_event_loop()
    try:
        second_id = second.run_until_complete(_take())  # must not raise
    finally:
        second.close()

    assert first_id != second_id, "the lock from the closed loop was reused"


def test_every_destructive_or_dispatching_handler_takes_the_turn_lock():
    """Source guard. A handler that starts OR destroys a turn without the lock can
    interleave with an answer being recorded. The CREATE path is exempt: the spec does
    not exist before it, so no answer can be in flight for it."""
    for handler in (routes._handle_message, routes._handle_handoff, routes._handle_delete):
        src = inspect.getsource(handler)
        tree = ast.parse(textwrap.dedent(src))
        locked = any(
            isinstance(node, ast.AsyncWith)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "_turn_lock"
                for item in node.items
            )
            for node in ast.walk(tree)
        )
        assert locked, f"{handler.__name__} runs without taking the turn lock"


@pytest.mark.asyncio
async def test_the_ledger_parent_is_created_on_first_write(tmp_path, monkeypatch):
    """The ledger lives under ``trust/``, not this app's state dir, so on a fresh
    install its parent may not exist yet -- and the app's own mkdir is the only thing
    that creates it. Without this the FIRST answer of a new install fails to record."""
    _redirect_state(monkeypatch, tmp_path)
    # A parent that does not exist, exactly like a fresh install's trust root.
    ledger = tmp_path / "fresh-home" / "trust" / "spec-builder-decisions.json"
    monkeypatch.setattr(routes, "_DECISIONS_PATH", ledger)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert not ledger.parent.exists()

    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )

    assert outcome == routes._CLAIM_RECORDED
    assert ledger.is_file()
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


def test_a_duplicate_decision_id_is_dropped():
    """The id IS the identity: the ledger is keyed on it and the overlay matches on it,
    so two cards claiming one id would both settle when either is answered -- and the
    second would display an answer chosen for the first. The FIRST occurrence wins."""
    out = routes._normalize_spec_state(
        {
            "decisions": [
                {"id": "transport", "title": "Inbound transport", "options": ["HTTPS"]},
                {"id": "transport", "title": "Something else entirely", "options": ["A"]},
            ]
        }
    )
    assert out is not None
    assert [d["title"] for d in out["decisions"]] == ["Inbound transport"]


@pytest.mark.asyncio
async def test_an_undispatched_answer_is_never_reopened(tmp_path, monkeypatch):
    """The crash window, resolved in the only direction that keeps the guarantee.

    A ledger write and the dispatch it authorizes are two steps and no ordering makes
    them atomic. If the process dies in between, the answer is recorded but undelivered
    -- and it STAYS recorded, across restarts. Reopening it would be a silent reversal of
    a decision that may already have reached the agent, which is the whole defect this
    file exists to prevent; a locked card showing the user's own choice is a stall the
    user clears by saying so in the spec's chat.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    # Nothing else ran: no dispatch, no second write. A restart changes nothing, since
    # the record carries no process-scoped state that a new process could disown.
    monkeypatch.setattr(routes, "_PROCESS_ID", "9999:a-new-process")

    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}
    outcome, held = await routes._claim_decision(
        "s", "transport", "Streaming", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )
    assert outcome == routes._CLAIM_TAKEN, "a recorded answer was reopened after a restart"
    assert held == "HTTPS"


def test_nothing_writes_the_ledger_after_the_dispatch():
    """Source guard: the claim is the LAST ledger write before the dispatch, and there
    is no write after it.

    A post-dispatch write to mark the answer delivered cannot work: its own failure is
    indistinguishable from the crash it would be detecting, so a reader could only treat
    the record as unconfirmed -- reopening a delivered answer. The absence of that write
    is the invariant, so it is worth pinning.
    """
    src = inspect.getsource(routes._handle_message)
    assert "_claim_decision(" in src
    after = src[src.index("_dispatch_turn(") :]
    for writer in ("_claim_decision", "_save_decisions", "_confirm_decision", "_forget_decisions"):
        assert writer not in after, f"{writer} runs after the dispatch"


@pytest.mark.asyncio
async def test_a_delete_whose_cleanup_fails_still_deletes(tmp_path, monkeypatch):
    """The ledger cleanup is housekeeping, so it cannot fail a delete that has happened.

    The index entry and the conversation are already gone by then; refusing here would
    report a spec as alive when it is not. A leftover entry is bounded and only readable
    by a spec re-created at the same name AND path -- which, by the identity this ledger
    uses, is the same spec.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    def _boom(_store):
        raise OSError("read-only data home")

    monkeypatch.setattr(routes, "_save_decisions", _boom)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, payload
    assert "s" not in await routes._aload_index()


@pytest.mark.asyncio
async def test_a_delete_clears_the_record_after_the_index_entry(tmp_path, monkeypatch):
    """...and on the happy path the entry IS cleared, so the ledger does not accumulate
    answers for specs that no longer exist."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {}


def test_the_delete_clears_the_ledger_after_the_index_write():
    """Source guard on that order. Clearing FIRST is what let a crash strand a surviving
    spec with its answers erased."""
    src = inspect.getsource(routes._handle_delete)
    pop = src.index("await _mutate_index(", src.index("def _pop_if_same"))
    assert pop < src.index(
        "_forget_decisions("
    ), "the ledger is cleared before the index entry is removed"


def test_an_undecodable_record_is_unusable_not_an_exception(tmp_path, monkeypatch):
    """``read_text`` decodes, so non-UTF-8 bytes raise UnicodeDecodeError -- a
    ValueError, which the OSError handler does not catch.

    Every caller is built around the (store, usable) contract, so an undecodable file
    has to arrive as "exists but unusable": the detail read fails soft and a claim
    refuses. Left uncaught it left this function by raising, which is a 500 carrying no
    code the client could act on.
    """
    path = tmp_path / "spec-builder-decisions.json"
    path.write_bytes(b'{"/x": {"name": "s", "answers": {"d": "\xff\xfe not utf-8"}}}')
    monkeypatch.setattr(routes, "_DECISIONS_PATH", path)

    store, usable = routes._read_decisions()

    assert store == {}
    assert usable is False, "an undecodable ledger reported itself as writable"


def test_a_recorded_answer_round_trips_non_ascii(tmp_path, monkeypatch):
    """The ledger's encoding is a property of the FILE, not of the host reading it.

    ``atomic_write`` always emits UTF-8 while ``read_text()`` without an encoding decodes
    with the platform default -- the ANSI code page on Windows. Under that asymmetry an
    option carrying an em dash came back mojibake, so the locked card displayed an answer
    the user never chose. Asserting the exact string back is what pins the explicit
    encoding on the read; the undecodable-bytes test above cannot, since on a UTF-8 host
    both spellings behave alike.
    """
    monkeypatch.setattr(routes, "_DECISIONS_PATH", tmp_path / "spec-builder-decisions.json")
    option = "HTTPS — with mTLS (日本語, café)"
    # A real directory, keyed the way the code keys it: a hard-coded "/x" resolves to a
    # drive-anchored path on Windows, so the store key and the lookup key disagreed there.
    spec_dir = tmp_path / "the-spec"
    spec_dir.mkdir()
    key = routes._decision_key(str(spec_dir))

    routes._save_decisions(
        {key: {"name": "s", "answers": {"transport": _final_answer("transport", option)}}}
    )
    store, usable = routes._read_decisions()

    assert usable is True
    assert _decision_record(store, str(spec_dir)) == {"transport": option}


@pytest.mark.asyncio
async def test_an_alias_name_for_the_same_folder_cannot_re_answer(tmp_path, monkeypatch):
    """A name is a label the agent can mint more of; the directory is the spec.

    Adding a second index entry pointing at the SAME spec directory used to give the
    alias its own empty record, so its cards rendered answerable and a click dispatched
    a conflicting answer over the same documents. Keyed on the directory, both names
    resolve to one record.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    # The alias: a second, perfectly valid index entry for the same files.
    index = await routes._aload_index()
    alias_key = routes._new_slot_key("s-alias")
    index["s-alias"] = {**index["s"], "slot_key": alias_key}
    routes._save_index(index)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s-alias/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": alias_key,
                "decision_id": "transport",
                "decision_option": "Streaming",
                "text": "Decision - transport: Streaming",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_already_answered", payload
    assert payload.get("answer") == "HTTPS"
    assert dispatched == [], "the alias dispatched a second answer for a settled decision"


def _macos_case_aliases(monkeypatch) -> None:
    """Make path identity behave like a case-insensitive Darwin volume."""
    monkeypatch.setattr(routes, "_CASE_FOLD_TURN_KEYS", True)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path)
    monkeypatch.setattr(
        routes.os.path,
        "samefile",
        lambda left, right: os.path.normpath(left).casefold() == os.path.normpath(right).casefold(),
    )


def _add_case_alias(spec_dir: str, alias_slot_key: str) -> str:
    """Add an upgrade-state alias whose lexical ledger key differs."""
    index = routes._load_index()
    alias_dir = str(Path(spec_dir).with_name(Path(spec_dir).name.swapcase()))
    index["s-alias"] = {
        **index["s"],
        "spec_dir": alias_dir,
        "slot_key": alias_slot_key,
    }
    routes._save_index(index)
    return alias_dir


@pytest.mark.asyncio
async def test_macos_case_alias_cannot_mint_a_second_decision_ledger(tmp_path, monkeypatch):
    """Upgrade-state aliases with distinct lexical keys fail closed on claim."""
    _redirect_state(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    alias_slot_key = routes._new_slot_key("s-alias")
    alias_dir = _add_case_alias(spec_dir, alias_slot_key)
    outcome, _held = await routes._claim_decision(
        "s-alias",
        "transport",
        "Streaming",
        expect_spec_dir=alias_dir,
        expect_slot_key=alias_slot_key,
    )

    assert outcome == routes._CLAIM_ALIAS_CONFLICT
    store, usable = routes._read_decisions()
    assert usable is True
    assert routes._decision_key(alias_dir) not in store
    assert _decision_record(store, spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_macos_single_path_rewrite_cannot_fork_the_decision_ledger(tmp_path, monkeypatch):
    """A protected old key fails closed after the sole index spelling changes."""
    client = _make_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    rewritten_dir = str(Path(spec_dir).with_name("S"))
    index = routes._load_index()
    index["s"]["spec_dir"] = rewritten_dir
    routes._save_index(index)

    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "Streaming",
        expect_spec_dir=rewritten_dir,
        expect_slot_key=slot_key,
    )

    await client.start_server()
    try:
        client.app["state"] = None
        detail = await client.get(f"{_BASE}/specs/s")
        detail_body = await detail.json()
        deleted = await client.delete(f"{_BASE}/specs/s")
        delete_body = await deleted.json()
    finally:
        await client.close()

    assert outcome == routes._CLAIM_ALIAS_CONFLICT
    assert detail.status == 409, detail_body
    assert detail_body["code"] == "decision_directory_alias_conflict"
    assert deleted.status == 409, delete_body
    assert delete_body["code"] == "decision_directory_alias_conflict"
    store, usable = routes._read_decisions()
    assert usable is True
    assert routes._decision_key(rewritten_dir) not in store
    assert _decision_record(store, spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_claim_uses_one_ledger_snapshot_for_alias_validation_and_write(tmp_path, monkeypatch):
    """A transient alias-check read cannot be followed by a write from a new snapshot."""
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    rewritten_dir = str(Path(spec_dir).with_name("S"))
    index = routes._load_index()
    index["s"]["spec_dir"] = rewritten_dir
    routes._save_index(index)
    read_decisions = routes._read_decisions
    reads = 0

    def _fail_once():
        nonlocal reads
        reads += 1
        return ({}, False) if reads == 1 else read_decisions()

    monkeypatch.setattr(routes, "_read_decisions", _fail_once)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "Streaming",
        expect_spec_dir=rewritten_dir,
        expect_slot_key=slot_key,
    )
    monkeypatch.setattr(routes, "_read_decisions", read_decisions)

    assert outcome == routes._CLAIM_UNREADABLE
    assert reads == 1
    store, usable = routes._read_decisions()
    assert usable is True
    assert routes._decision_key(rewritten_dir) not in store
    assert _decision_record(store, spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_macos_case_alias_conflict_refuses_detail_and_delete(tmp_path, monkeypatch):
    """Serving or deleting cannot strand a settled record under another spelling."""
    client = _make_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED
    alias_slot_key = routes._new_slot_key("s-alias")
    _add_case_alias(spec_dir, alias_slot_key)

    await client.start_server()
    try:
        client.app["state"] = None
        detail = await client.get(f"{_BASE}/specs/s")
        detail_body = await detail.json()
        deleted = await client.delete(f"{_BASE}/specs/s")
        delete_body = await deleted.json()
    finally:
        await client.close()

    assert detail.status == 409, detail_body
    assert detail_body["code"] == "decision_directory_alias_conflict"
    assert deleted.status == 409, delete_body
    assert delete_body["code"] == "decision_directory_alias_conflict"
    assert set(routes._load_index()) == {"s", "s-alias"}
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_alias_added_during_delete_prevents_the_index_pop(tmp_path, monkeypatch):
    """A late alias keeps an already-torn-down delete reserved for retry."""
    client = _make_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    async def _teardown_with_alias(*_args, **_kwargs) -> bool:
        alias_slot_key = routes._new_slot_key("s-alias")
        index = routes._load_index()
        alias = dict(index["s"])
        alias["spec_dir"] = str(Path(spec_dir).with_name("S"))
        alias["slot_key"] = alias_slot_key
        index["s-alias"] = alias
        routes._save_index(index)
        return True

    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown_with_alias)

    await client.start_server()
    try:
        client.app["state"] = None
        response = await client.delete(f"{_BASE}/specs/s")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 409, body
    assert body["code"] == "decision_directory_alias_conflict"
    assert "retry" in body["error"]
    index = routes._load_index()
    assert set(index) == {"s", "s-alias"}
    assert index["s"].get(routes._DELETING), "the torn-down spec was re-exposed"
    assert spec_dir in routes._load_deleted(), "the delete lost its tombstone"
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


def test_malformed_index_path_cannot_crash_directory_identity(tmp_path, monkeypatch):
    """Agent-written NUL paths are rejected and samefile failures are contained."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"broken": {"spec_dir": "\x00"}})

    assert routes._load_index() == {}
    assert routes._same_spec_dir("\x00", str(tmp_path)) is False


@pytest.mark.asyncio
async def test_a_renamed_spec_keeps_its_recorded_answers(tmp_path, monkeypatch):
    """The other side of the same coin: renaming the index entry cannot reopen a
    decision, because the record is not keyed on the name.

    This is what makes an index rewrite structurally unable to reach a settled answer,
    rather than something that has to be detected and refused.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    index = await routes._aload_index()
    index["renamed"] = index.pop("s")
    routes._save_index(index)

    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}
    outcome, held = await routes._claim_decision(
        "renamed",
        "transport",
        "Streaming",
        expect_spec_dir=spec_dir,
        expect_slot_key=index["renamed"]["slot_key"],
    )
    assert outcome == routes._CLAIM_TAKEN, "a rename reopened a settled decision"
    assert held == "HTTPS"


def test_a_symlinked_spelling_cannot_record_a_second_answer(tmp_path, monkeypatch):
    """Until round 28 this asserted the KEY collapsed a symlinked spelling.

    It did, via resolve() -- and that bought a worse hole, because the spec directory
    belongs to the agent: swapping the directory for a symlink moved the key while the
    index identity still matched, so a settled record went missing and the card
    re-opened. A key derived from mutable filesystem state is a key the agent can move.

    The alias-by-spelling guarantee therefore moved to the WRITE end. The key is now
    lexical (two spellings ARE two keys), and `_claim_decision_locked` refuses a
    spec_dir that does not verify as itself -- so the aliased spelling cannot record
    anything, which is what the collapse existed to prevent.
    """
    _redirect_state(monkeypatch, tmp_path)
    real = tmp_path / "real-spec"
    real.mkdir()
    link = tmp_path / "link-spec"
    link.symlink_to(real, target_is_directory=True)

    # Lexical now, so the spellings no longer share a key...
    assert routes._decision_key(str(link)) != routes._decision_key(str(real))
    # ...and that is safe because the aliased spelling is refused at the write gate.
    assert (
        routes._verified_spec_dir(link) is None
    ), "a symlinked spelling verifies as itself, so it could mint a second record"
    assert (
        routes._verified_spec_dir(real) is not None
    ), "the real directory must still verify, or no spec could record an answer"


@pytest.mark.asyncio
async def test_a_claim_through_an_aliased_spelling_is_refused(tmp_path, monkeypatch):
    """The write gate that replaced the resolving key.

    Two index entries, one spelling the directory directly and one through a symlink.
    The key is lexical now, so the aliased entry has its OWN key -- which would be an
    empty record, exactly the alias hole. Once both spellings are live, claims fail
    closed before either key can be written.
    """
    _redirect_state(monkeypatch, tmp_path)
    real = tmp_path / "proj" / ".kiro" / "specs" / "real"
    real.mkdir(parents=True)
    link = tmp_path / "proj" / ".kiro" / "specs" / "alias"
    link.symlink_to(real, target_is_directory=True)

    entry = {
        "working_dir": str(tmp_path / "proj"),
        "spec_type": "feature",
        "status": "planning",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    real_entry = {
        **entry,
        "spec_dir": str(real),
        "slot_key": "spec-builder-real-aaaaaaaa",
    }
    routes._save_index({"real": real_entry})

    # The real spec records its answer normally.
    outcome, _held = await routes._claim_decision(
        "real",
        "transport",
        "HTTPS",
        expect_spec_dir=str(real),
        expect_slot_key="spec-builder-real-aaaaaaaa",
    )
    assert outcome == routes._CLAIM_RECORDED, outcome

    routes._save_index(
        {
            "real": real_entry,
            "alias": {
                **entry,
                "spec_dir": str(link),
                "slot_key": "spec-builder-alias-bbbbbbbb",
            },
        }
    )

    # The aliased spelling is refused rather than minting its own record.
    outcome, _held = await routes._claim_decision(
        "alias",
        "transport",
        "Streaming",
        expect_spec_dir=str(link),
        expect_slot_key="spec-builder-alias-bbbbbbbb",
    )
    assert outcome == routes._CLAIM_ALIAS_CONFLICT, (
        f"the aliased spelling recorded an answer ({outcome}), so a settled decision "
        "can be answered a second time through a symlink"
    )
    store, _usable = routes._read_decisions()
    assert (
        routes._decision_key(str(link)) not in store
    ), "a second ledger record was minted for the aliased spelling"
    assert _decision_record(store, str(real)) == {"transport": "HTTPS"}


def test_the_ledger_key_touches_no_filesystem(tmp_path):
    """The property that makes a settled decision un-reopenable by the agent.

    Replaces test_nothing_canonicalizes_a_path_on_the_event_loop, which policed WHERE
    the resolving key could be computed. Purity makes that concern structurally
    impossible instead: the key cannot move, so it cannot be moved off a record, and
    there is no filesystem call to keep off the event loop.
    """
    real = tmp_path / "spec"
    real.mkdir()
    key_before = routes._decision_key(str(real))

    # Replace the directory with a symlink to somewhere else -- exactly the swap an
    # agent can perform on its own spec directory.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real.rmdir()
    real.symlink_to(elsewhere, target_is_directory=True)

    assert routes._decision_key(str(real)) == key_before, (
        "swapping the directory moved the ledger key, so a settled decision would read "
        "as unanswered and the card would re-open"
    )
    # And the derivation itself calls nothing but lexical helpers. Checked on the AST,
    # not the text: a substring scan matches the docstring that explains the rule.
    fn = ast.parse(textwrap.dedent(inspect.getsource(routes._decision_key))).body[0]
    called = set()
    for inner in ast.walk(fn):
        if isinstance(inner, ast.Call):
            target = inner.func
            called.add(
                target.attr
                if isinstance(target, ast.Attribute)
                else getattr(target, "id", "<expr>")
            )
    assert called <= {
        "normcase",
        "normpath",
    }, f"the ledger key derivation calls more than lexical helpers: {sorted(called)}"


@pytest.mark.asyncio
async def test_a_delete_clears_the_record_for_that_folder_only(tmp_path, monkeypatch):
    """The delete clears by directory, so it cannot reach another spec's record."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    other = str(tmp_path / "another-spec")
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED
    store, _usable = routes._read_decisions()
    store[routes._decision_key(other)] = {
        "name": "other",
        "answers": {"kept": _final_answer("kept", "yes")},
    }
    routes._save_decisions(store)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {}
    assert await _recorded_answers(other) == {"kept": "yes"}


@pytest.mark.asyncio
async def test_deleting_one_alias_leaves_the_shared_record_alone(tmp_path, monkeypatch):
    """The record belongs to the directory, so it outlives any one name pointing at it.

    Keying on the directory is what closes the alias hole, but it also means a single
    name's delete must not clear the record: the surviving alias still serves those
    documents, and a cleared record would hand it a clean slate for decisions that are
    already settled -- the alias hole again, reached through a delete.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    index = await routes._aload_index()
    alias_key = routes._new_slot_key("s-alias")
    index["s-alias"] = {**index["s"], "slot_key": alias_key}
    routes._save_index(index)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {
        "transport": "HTTPS"
    }, "deleting one name cleared the record the surviving alias still needs"
    outcome, held = await routes._claim_decision(
        "s-alias",
        "transport",
        "Streaming",
        expect_spec_dir=spec_dir,
        expect_slot_key=alias_key,
    )
    assert outcome == routes._CLAIM_TAKEN, "the surviving alias could re-answer"
    assert held == "HTTPS"


@pytest.mark.asyncio
async def test_an_alias_mid_turn_blocks_the_answer(tmp_path, monkeypatch):
    """A running turn under ANY name on this directory blocks the answer.

    Aliases are separate sessions over the same documents, so checking only this
    request's own slot let one alias record and dispatch an answer while the other's
    agent was still working -- two concurrent agents, one set of files, and an answer
    delivered to whichever won.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    index = await routes._aload_index()
    alias_key = routes._new_slot_key("s-alias")
    index["s-alias"] = {**index["s"], "slot_key": alias_key}
    routes._save_index(index)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    # The ALIAS is mid-turn; the name being answered is idle.
    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot

    def _get_slot(key):
        return busy if key == alias_key else real_get_slot(key)

    state.get_slot = _get_slot

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    # `spec_busy_elsewhere`, not `decision_agent_busy`: the blocker is a DIFFERENT
    # session on these files, which refuses every kind of dispatch, not just answers.
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "an answer was dispatched while an alias's agent was working"
    assert (
        await _recorded_answers(spec_dir) == {}
    ), "the answer was recorded even though nothing was dispatched"


@pytest.mark.asyncio
async def test_an_alias_turn_that_starts_and_finishes_during_claim_defers_the_answer(
    tmp_path, monkeypatch
):
    """Final alias verification must observe a cleared task's generation.

    Dashboard chat does not take Spec Builder's directory lock. An alias turn can
    therefore fit wholly inside an off-loop ledger write and read idle at both ends;
    production clears its task back to ``None``, so only monotonic history can prevent
    concurrent decision dispatch.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    index = await routes._aload_index()
    alias_key = _alias_of(index, "s", "s-alias")
    state, _slot = _slot_stub()

    from kiro_crew.dashboard.state import _ChatSlot

    alias_slot = _ChatSlot(alias_key)
    real_get_slot = state.get_slot

    def _get_slot(key):
        return alias_slot if key == alias_key else real_get_slot(key)

    state.get_slot = _get_slot
    state._background_tasks = set()
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    real_mark_relayed = routes._mark_decision_relayed

    async def _mark_while_alias_turn_runs(*args, **kwargs):
        marked = await real_mark_relayed(*args, **kwargs)

        async def _alias_turn() -> None:
            return None

        alias_slot.task = asyncio.create_task(_alias_turn())
        await alias_slot.task
        # Normal queue teardown publishes idle by clearing the task. A snapshot of
        # only ``slot.task`` therefore sees ``None`` both before and after this turn.
        alias_slot.append("done", "", "done")
        alias_slot.task = None
        return marked

    monkeypatch.setattr(routes, "_mark_decision_relayed", _mark_while_alias_turn_runs)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "decision_delivery_pending", payload
    assert dispatched == []
    pending = await routes._pending_decisions(spec_dir)
    assert [(entry.get("status"), entry.get("option")) for entry in pending] == [
        ("pending", "HTTPS")
    ]


def test_the_turn_lock_is_keyed_by_directory_not_name():
    """Source guard. A per-name lock is what let two aliases start turns on one
    directory concurrently, so the key is worth pinning."""
    for handler in (routes._handle_message, routes._handle_handoff, routes._handle_delete):
        body = inspect.getsource(handler)
        assert (
            "_turn_lock(name)" not in body
        ), f"{handler.__name__} still locks by name, so an alias gets its own lock"
        assert (
            "_turn_lock(_decision_key" not in body
        ), f"{handler.__name__} should hold the key in a local, not re-derive it"
        assert (
            "_decision_key(" in body
        ), f"{handler.__name__} does not derive its lock key from the directory"


@pytest.mark.asyncio
async def test_deleting_one_alias_keeps_the_shared_turn_lock(tmp_path, monkeypatch):
    """The lock is keyed by directory, so it must outlive any one name using it.

    Dropping it while another alias is still indexed would hand the next arrival a
    brand-new lock object, and a turn could then start on documents another turn is
    already working -- the very race the directory-keyed lock closes. Only the LAST name
    on a directory may retire its lock.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    routes._turn_lock(spec_dir)
    key = routes._turn_key(spec_dir)
    assert key in routes._TURN_LOCKS
    held = routes._TURN_LOCKS[key]

    index = await routes._aload_index()
    index["s-alias"] = {**index["s"], "slot_key": routes._new_slot_key("s-alias")}
    routes._save_index(index)

    await client.start_server()
    try:
        client.app["state"] = None
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert routes._TURN_LOCKS.get(key) is held, "the surviving alias lost the lock it serializes on"


def _alias_of(index: dict, name: str, alias: str) -> str:
    """Add a second index entry for the same directory. Returns its slot key."""
    alias_key = routes._new_slot_key(alias)
    index[alias] = {**index[name], "slot_key": alias_key}
    routes._save_index(index)
    return alias_key


class _HeldTurnLock:
    """Expose when a handler reaches its directory-lock boundary."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        await self.release.wait()
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


@pytest.mark.asyncio
async def test_an_alias_added_while_message_waits_for_the_turn_lock_is_seen(tmp_path, monkeypatch):
    """The alias snapshot must be taken after entering the directory lock.

    An agent may add an index alias while a request waits. If the request scans first,
    the alias can dispatch under the shared lock and the stale request then starts a
    second agent over the same files without seeing it.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    state, _slot = _slot_stub()
    held_lock = _HeldTurnLock()
    monkeypatch.setattr(routes, "_turn_lock", lambda _key: held_lock)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    request_task = None
    await client.start_server()
    try:
        client.app["state"] = state
        request_task = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/message",
                json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
            )
        )
        await held_lock.entered.wait()

        alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
        busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
        real_get_slot = state.get_slot
        state.get_slot = lambda key: busy if key == alias_key else real_get_slot(key)
        held_lock.release.set()

        resp = await request_task
        payload = await resp.json()
    finally:
        held_lock.release.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request_task
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "the stale pre-lock alias snapshot admitted a second agent"


@pytest.mark.asyncio
async def test_an_alias_added_while_handoff_waits_for_the_turn_lock_is_seen(tmp_path, monkeypatch):
    """A handoff must discover aliases only after entering the directory lock."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    state, _slot = _slot_stub()
    held_lock = _HeldTurnLock()
    monkeypatch.setattr(routes, "_turn_lock", lambda _key: held_lock)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    authorized: list = []
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    async def _authorized(**_kw):
        authorized.append(True)
        return types.SimpleNamespace(id="loop-1"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)

    request_task = None
    await client.start_server()
    try:
        client.app["state"] = state
        request_task = asyncio.create_task(
            client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        )
        await held_lock.entered.wait()

        alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
        busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
        real_get_slot = state.get_slot
        state.get_slot = lambda key: busy if key == alias_key else real_get_slot(key)
        held_lock.release.set()

        resp = await request_task
        payload = await resp.json()
    finally:
        held_lock.release.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request_task
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert authorized == [], "the stale pre-lock alias snapshot armed a second agent"
    assert dispatched == [], "the stale pre-lock alias snapshot dispatched a second agent"


@pytest.mark.asyncio
async def test_stop_that_finishes_before_handoff_gets_the_lock_prevents_dispatch(
    tmp_path, monkeypatch
):
    """A successful Stop must revoke a handoff claim that is still waiting.

    Handoff records ``executing`` before it acquires the directory lock. Stop can
    therefore take the lock first, halt that creation, and commit ``planning``.
    Once Stop reports success, the older handoff must not arm or dispatch a run.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")

    slot = types.SimpleNamespace(
        key=slot_key,
        _app=routes.APP_NAME,
        running=False,
        task=None,
        messages=[],
        project=str(tmp_path / "wd"),
        _titled=True,
    )

    class _State:
        def __init__(self):
            self._background_tasks: set[asyncio.Task] = set()

        def get_slot(self, key):
            return slot if key == slot_key else None

        def get_or_create_slot(self, _key, app=""):
            return slot

    state = _State()
    claimed = asyncio.Event()
    resume_handoff = asyncio.Event()
    real_ensure = routes._ensure_worker_slot

    async def _hold_after_claim(*args, **kwargs):
        claimed.set()
        await resume_handoff.wait()
        return await real_ensure(*args, **kwargs)

    async def _halted(*_args, **_kwargs):
        return None

    authorized: list[bool] = []
    dispatched: list[str] = []

    async def _authorized(**_kwargs):
        authorized.append(True)
        return types.SimpleNamespace(id="loop-1"), "", 200

    monkeypatch.setattr(routes, "_ensure_worker_slot", _hold_after_claim)
    monkeypatch.setattr(routes, "_halt_execution", _halted)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())
    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    handoff = None
    await client.start_server()
    try:
        client.app["state"] = state
        handoff = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/handoff",
                json={"spec_dir": spec_dir, "slot_key": slot_key},
            )
        )
        await claimed.wait()
        assert routes._load_index()["s"]["status"] == "executing"

        stopped = await client.post(
            f"{_BASE}/specs/s/stop",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        stop_payload = await stopped.json()
        assert stopped.status == 200, stop_payload
        assert routes._load_index()["s"]["status"] == "planning"

        resume_handoff.set()
        response = await handoff
        payload = await response.json()
    finally:
        resume_handoff.set()
        if handoff is not None and not handoff.done():
            handoff.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handoff
        await client.close()

    assert response.status == 409, payload
    assert payload.get("code") == "execution_stopped_during_start", payload
    assert authorized == [], "the stopped handoff still armed an autonomous loop"
    assert dispatched == [], "the stopped handoff restarted the agent after Stop returned"


@pytest.mark.asyncio
async def test_stop_barrier_revokes_the_old_generation_and_refuses_a_new_one():
    """Agent-writable index fields cannot mint or preserve handoff ownership."""
    dir_key = "/canonical/spec"
    slot_key = "spec-builder-s-first"
    first, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
    assert first and refusal == ""

    async with routes._execution_stop_barrier(dir_key, slot_key, "s") as capture:
        assert not routes._execution_claim_is_current(dir_key, first)
        blocked, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
        assert blocked == "" and refusal == "stopping"
        capture.commit()

    second, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
    assert second and second != first and refusal == ""
    async with routes._execution_stop_barrier(
        f"{dir_key}-replacement", f"{slot_key}-replacement", "replacement"
    ):
        assert routes._execution_claim_is_current(dir_key, second)
    rewritten_dir_key = f"{dir_key}-rewritten"
    async with routes._execution_stop_barrier(rewritten_dir_key, slot_key, "s") as capture:
        assert not routes._execution_claim_is_current(dir_key, second)
        blocked, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
        assert blocked == "" and refusal == "stopping"
        capture.commit()
    assert not routes._execution_claim_is_current(dir_key, second)


@pytest.mark.asyncio
async def test_uncommitted_stop_barrier_restores_a_directory_matched_claim():
    """A stale control cannot consume the only handle to an older live loop."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    token, refusal = routes._reserve_execution_claim(old_dir, old_slot, "s")
    assert token and refusal == ""

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t") as capture:
        assert old_slot in capture
        assert not routes._execution_claim_is_current(old_dir, token)
        routes._prune_finished_dispatch_claims()

    assert routes._execution_claim_is_current(old_dir, token)
    async with routes._execution_stop_barrier(old_dir, old_slot, "s") as capture:
        capture.commit()


@pytest.mark.asyncio
async def test_uncommitted_stop_barrier_restores_a_directory_matched_pending_turn():
    """A refused teardown cannot erase an ordinary turn's ownership claim."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    token = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    assert token

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t"):
        assert not routes._pending_dispatch_is_current(token)
        assert token in routes._PENDING_DISPATCH_CLAIMS

    assert routes._pending_dispatch_is_current(token)
    routes._drop_pending_dispatch(token)


@pytest.mark.asyncio
async def test_stop_rollback_prunes_a_published_pending_turn_that_finished_revoked():
    """A completion callback hidden by provisional revocation is reconciled."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    release = asyncio.Event()
    turn = asyncio.create_task(release.wait())
    slot = types.SimpleNamespace(task=turn, running=True)
    token = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    assert token
    routes._bind_pending_dispatch_to_turn(token, slot, turn)

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t"):
        slot.running = False
        release.set()
        await turn
        await asyncio.sleep(0)
        assert token in routes._PENDING_DISPATCH_CLAIMS

    assert token not in routes._PENDING_DISPATCH_CLAIMS
    replacement = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    assert replacement
    routes._drop_pending_dispatch(replacement)


@pytest.mark.asyncio
async def test_stop_rollback_rebinds_a_pending_claim_to_its_live_successor():
    """Rollback preserves automatic retry ownership while the retry is active."""
    old_dir = "/canonical/spec"
    old_slot = "spec-builder-s-1234abcd"
    first_release = asyncio.Event()
    successor_release = asyncio.Event()
    first = asyncio.create_task(first_release.wait())
    successor = asyncio.create_task(successor_release.wait())
    slot = types.SimpleNamespace(task=first, running=True)
    token = routes._reserve_pending_dispatch(old_dir, old_slot, "s")
    routes._bind_pending_dispatch_to_turn(token, slot, first)

    async with routes._execution_stop_barrier(old_dir, "spec-builder-t-deadbeef", "t"):
        slot.task = successor
        first_release.set()
        await first
        await asyncio.sleep(0)

    assert routes._PENDING_DISPATCH_CLAIMS[token][3] is successor
    successor_release.set()
    await successor
    await asyncio.sleep(0)
    assert token not in routes._PENDING_DISPATCH_CLAIMS


@pytest.mark.asyncio
async def test_rolled_back_stop_settles_a_handoff_that_observed_revocation(tmp_path, monkeypatch):
    """A failed teardown cannot leave an aborted handoff durably executing."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    slot_key = "spec-builder-s-1234abcd"
    routes._save_index(
        {
            "s": {
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "status": "executing",
            }
        }
    )
    reserved = asyncio.Event()
    token_box: list[str] = []

    async def _handoff_owner():
        token, refusal = routes._reserve_execution_claim(spec_dir, slot_key, "s")
        assert token and refusal == ""
        token_box.append(token)
        reserved.set()
        while routes._execution_claim_is_current(spec_dir, token):
            await asyncio.sleep(0)

    owner = asyncio.create_task(_handoff_owner())
    await reserved.wait()
    async with routes._execution_stop_barrier(spec_dir, "spec-builder-t-deadbeef", "t"):
        await owner

    await asyncio.gather(*tuple(routes._STOP_ROLLBACK_TASKS))
    assert routes._load_index()["s"]["status"] == "planning"
    assert not routes._execution_claim_is_current(spec_dir, token_box[0])


@pytest.mark.asyncio
async def test_index_keeps_resolving_an_observed_per_creation_slot_key(tmp_path, monkeypatch):
    """An agent cannot detach a live turn by replacing its authenticated slot key."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    original = "spec-builder-s-1234abcd"
    replacement = "spec-builder-s-deadbeef"
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": original}})
    assert routes._load_index()["s"]["slot_key"] == original

    routes._index_path().write_text(
        json.dumps({"s": {"spec_dir": spec_dir, "slot_key": replacement}})
    )

    changed = routes._load_index()["s"]
    assert changed["slot_key"] == replacement
    assert routes._slot_key("s") == original
    assert routes._OBSERVED_SLOT_KEYS["s"] == original
    assert await routes._pin_legacy_slot_identity("s", changed) is None


def test_index_refresh_replaces_observed_identity_maps(tmp_path, monkeypatch):
    """A worker refresh cannot resize a snapshot being read on the event loop."""
    _redirect_state(monkeypatch, tmp_path)
    first_slot = "spec-builder-s-1234abcd"
    second_slot = "spec-builder-t-deadbeef"
    first_dir = str(tmp_path / "first")
    second_dir = str(tmp_path / "second")
    routes._save_index({"s": {"spec_dir": first_dir, "slot_key": first_slot}})
    slot_keys_snapshot = routes._OBSERVED_SLOT_KEYS
    spec_dirs_snapshot = routes._OBSERVED_SPEC_DIRS

    routes._save_index(
        {
            "s": {"spec_dir": first_dir, "slot_key": first_slot},
            "t": {"spec_dir": second_dir, "slot_key": second_slot},
        }
    )

    assert routes._OBSERVED_SLOT_KEYS is not slot_keys_snapshot
    assert routes._OBSERVED_SPEC_DIRS is not spec_dirs_snapshot
    assert slot_keys_snapshot == {"s": first_slot}
    assert spec_dirs_snapshot == {first_slot: routes._decision_key(first_dir)}


def test_identity_release_replaces_observed_identity_maps(tmp_path, monkeypatch):
    """Worker teardown cannot resize a snapshot being read on the event loop."""
    _redirect_state(monkeypatch, tmp_path)
    first_slot = "spec-builder-s-1234abcd"
    second_slot = "spec-builder-t-deadbeef"
    routes._OBSERVED_SLOT_KEYS.update({"s": first_slot, "t": second_slot})
    routes._OBSERVED_SPEC_DIRS.update({first_slot: "/first", second_slot: "/second"})
    slot_keys_snapshot = routes._OBSERVED_SLOT_KEYS
    spec_dirs_snapshot = routes._OBSERVED_SPEC_DIRS

    routes._forget_observed_slot_identity("s", first_slot)

    assert routes._OBSERVED_SLOT_KEYS is not slot_keys_snapshot
    assert routes._OBSERVED_SPEC_DIRS is not spec_dirs_snapshot
    assert slot_keys_snapshot == {"s": first_slot, "t": second_slot}
    assert spec_dirs_snapshot == {first_slot: "/first", second_slot: "/second"}
    assert routes._OBSERVED_SLOT_KEYS == {"t": second_slot}
    assert routes._OBSERVED_SPEC_DIRS == {second_slot: "/second"}


@pytest.mark.asyncio
async def test_deleted_creation_releases_its_observed_slot_identity(tmp_path, monkeypatch):
    """A trusted delete permits a later creation to mint a fresh identity."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    original = "spec-builder-s-1234abcd"
    replacement = "spec-builder-s-deadbeef"
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": original}})

    routes._index_path().write_text(
        json.dumps({"s": {"spec_dir": spec_dir, "slot_key": replacement}})
    )
    routes._load_index()
    assert routes._slot_key("s") == original

    def _delete(index):
        del index["s"]
        return True

    assert await routes._mutate_index(
        _delete,
        on_commit=lambda: routes._forget_observed_slot_identity("s", replacement, original),
    )
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": replacement}})

    assert routes._load_index()["s"]["slot_key"] == replacement


@pytest.mark.asyncio
async def test_restored_execution_loop_blocks_dispatch_after_identity_rewrite(
    tmp_path, monkeypatch
):
    """A restart-persistent loop remains exclusive without process claims."""
    _redirect_state(monkeypatch, tmp_path)
    old_dir = str(tmp_path / "old-spec")
    new_dir = str(tmp_path / "new-spec")
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-t-deadbeef"
    loop = types.SimpleNamespace(
        id="restored-loop",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. The plan is approved.",
        stop_sentinel_path=str(Path(old_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == old_slot else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    routes._save_index({"t": {"spec_dir": old_dir, "slot_key": new_slot}})
    assert routes._reserve_pending_dispatch(old_dir, new_slot, "t") == ""
    assert routes._reserve_execution_claim(old_dir, new_slot, "t") == (
        "",
        "taken",
    )

    routes._save_index({"t": {"spec_dir": new_dir, "slot_key": new_slot}})
    assert routes._reserve_pending_dispatch(new_dir, new_slot, "t") == ""
    async with routes._execution_stop_barrier(new_dir, new_slot, "t") as capture:
        assert capture[old_slot] == loop.id


@pytest.mark.asyncio
async def test_restored_execution_loop_keeps_rewritten_same_directory_executing(
    tmp_path, monkeypatch
):
    """Detail keeps Pause visible for a restored loop under its old slot key."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-t-deadbeef"
    loop = types.SimpleNamespace(
        id="restored-loop",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(spec_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == old_slot else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    meta = {"spec_dir": spec_dir, "slot_key": new_slot, "status": "executing"}
    routes._save_index({"t": meta})

    status = await routes._effective_status("t", meta, types.SimpleNamespace(running=False))

    assert status == "executing"
    assert routes._load_index()["t"]["status"] == "executing"


def _maintenance_loader(service):
    """Build the AutoNudge class seam around one authoritative test service."""

    class _Loader:
        @classmethod
        @contextlib.asynccontextmanager
        async def maintenance_service(cls, base_dir=None):
            yield service

    return _Loader


@pytest.mark.asyncio
async def test_create_cleanup_removes_a_fully_orphaned_execution_loop(tmp_path, monkeypatch):
    """An empty index has an app-owned recovery path for a durable orphan."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    removed: list[str] = []
    torn_down: list[object] = []
    orphan_slot_state = types.SimpleNamespace(
        key=orphan_slot,
        _app=routes.APP_NAME,
        running=True,
    )

    class _State:
        def __init__(self):
            self.slot: object | None = orphan_slot_state

        def get_slot(self, key):
            return self.slot if key == orphan_slot else None

    state = _State()

    class _Svc:
        def list_all(self):
            return [] if removed else [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot and not removed else None

        async def deactivate_and_wait(self, loop_id):
            assert loop_id == loop.id
            loop.active = False
            return True

        async def remove(self, loop_id):
            removed.append(loop_id)
            loop.active = False

    async def _teardown(_state, _name, *, only_slot, require_archive):
        assert require_archive is True
        torn_down.append(only_slot)
        state.slot = None
        return True

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)
    routes._save_index({})
    assert routes._reserve_pending_dispatch("/new/spec", "spec-builder-n-deadbeef", "n") == ""

    assert await routes._remove_orphaned_executions(state) == {orphan_slot}

    assert removed == [loop.id]
    assert torn_down == [orphan_slot_state]
    token = routes._reserve_pending_dispatch("/new/spec", "spec-builder-n-deadbeef", "n")
    assert token
    routes._drop_pending_dispatch(token)


@pytest.mark.asyncio
async def test_create_cleanup_rechecks_a_slot_materialized_while_its_loop_quiesces(
    tmp_path, monkeypatch
):
    """A firing timer cannot publish a worker after cleanup's slot snapshot."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    published = types.SimpleNamespace(
        key=orphan_slot,
        _app=routes.APP_NAME,
        running=True,
    )
    torn_down: list[object] = []

    class _State:
        slot = None

        def get_slot(self, key):
            return self.slot if key == orphan_slot else None

    state = _State()

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot else None

        async def deactivate_and_wait(self, loop_id):
            assert loop_id == loop.id
            loop.active = False
            state.slot = published
            return True

        async def remove(self, loop_id):
            assert loop_id == loop.id

    async def _teardown(_state, _name, *, only_slot, require_archive):
        assert require_archive is True
        torn_down.append(only_slot)
        state.slot = None
        return True

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)
    routes._save_index({})

    assert await routes._remove_orphaned_executions(state) == {orphan_slot}
    assert torn_down == [published]


@pytest.mark.asyncio
async def test_create_cleanup_keeps_a_quiesced_loop_until_worker_archive_succeeds(
    tmp_path, monkeypatch
):
    """A retry or restart retains the identity when cancellation times out."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    slot = types.SimpleNamespace(key=orphan_slot, _app=routes.APP_NAME, running=True)
    removed: list[str] = []

    class _State:
        def get_slot(self, key):
            return slot if key == orphan_slot else None

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot else None

        async def deactivate_and_wait(self, loop_id):
            assert loop_id == loop.id
            loop.active = False
            return True

        async def remove(self, loop_id):
            removed.append(loop_id)

    async def _failed_archive(*_args, **_kwargs):
        return False

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _failed_archive)
    routes._save_index({})

    with pytest.raises(RuntimeError, match="could not be archived"):
        await routes._remove_orphaned_executions(_State())

    assert loop.active is False
    assert removed == []
    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {
        orphan_slot: loop.id
    }


@pytest.mark.asyncio
async def test_create_cleanup_loads_durable_loops_when_autonudge_is_disabled(tmp_path, monkeypatch):
    """Disabling timers cannot hide an old loop from Create recovery."""
    _redirect_state(monkeypatch, tmp_path)
    orphan_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="orphan-loop",
        active=True,
        slot_key=orphan_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(tmp_path / "old-spec" / "STOP"),
    )
    removed: list[str] = []

    class _OfflineSvc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == orphan_slot else None

        async def deactivate_and_wait(self, loop_id):
            assert loop_id == loop.id
            loop.active = False
            return True

        async def remove(self, loop_id):
            removed.append(loop_id)

    offline = _OfflineSvc()

    class _Loader:
        @classmethod
        @contextlib.asynccontextmanager
        async def maintenance_service(cls, base_dir=None):
            yield offline

    class _State:
        def get_slot(self, _key):
            return None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)
    monkeypatch.setattr(routes, "_AutoNudgeService", _Loader)
    routes._save_index({})

    assert await routes._remove_orphaned_executions(_State()) == {orphan_slot}
    assert removed == [loop.id]


def test_create_removes_orphaned_loops_before_filesystem_side_effects():
    """Recovery precedes worktree and spec-directory creation."""
    src = inspect.getsource(routes._handle_create)
    cleanup_at = src.index("await _remove_orphaned_executions(")
    worktree_at = src.index("await _create_worktree(")
    spec_dir_at = src.index("_prepare_spec_dir")

    assert cleanup_at < worktree_at < spec_dir_at


@pytest.mark.asyncio
async def test_create_refuses_an_unreadable_index_before_orphan_cleanup(tmp_path, monkeypatch):
    """Invalid index bytes cannot make durable workers look orphaned."""
    client = _make_client(monkeypatch, tmp_path)
    index_path = routes._index_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("not-json")
    cleaned: list[bool] = []
    prepared: list[bool] = []

    async def _cleanup(_state):
        cleaned.append(True)
        return set()

    def _prepare(*_args, **_kwargs):
        prepared.append(True)
        return tmp_path / "spec", ""

    monkeypatch.setattr(routes, "_remove_orphaned_executions", _cleanup)
    monkeypatch.setattr(routes, "_prepare_spec_dir", _prepare)
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs",
            json={"name": "new", "working_dir": str(tmp_path), "spec_type": "quick"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 503, payload
    assert payload.get("code") == "spec_index_unavailable"
    assert cleaned == []
    assert prepared == []
    assert index_path.read_text() == "not-json"


@pytest.mark.asyncio
async def test_create_refuses_before_side_effects_when_orphan_cleanup_fails(tmp_path, monkeypatch):
    """An unremovable orphan cannot overlap a partially-created replacement."""
    client = _make_client(monkeypatch, tmp_path)
    prepared: list[str] = []

    async def _fail_cleanup(_state):
        raise OSError("autonudge store unavailable")

    def _prepare(*_args, **_kwargs):
        prepared.append("prepared")
        return tmp_path / "spec", None

    monkeypatch.setattr(routes, "_remove_orphaned_executions", _fail_cleanup)
    monkeypatch.setattr(routes, "_prepare_spec_dir", _prepare)
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs",
            json={"name": "new", "working_dir": str(tmp_path), "spec_type": "quick"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 503, payload
    assert payload.get("code") == "orphaned_execution_cleanup_failed"
    assert prepared == []


@pytest.mark.asyncio
async def test_create_cleanup_archives_an_orphaned_direct_turn_without_a_loop(
    tmp_path, monkeypatch
):
    """Removing the last index row cannot detach an already-running worker."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    old_dir = str(tmp_path / "old-spec")
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index({})
    slot = types.SimpleNamespace(key=old_slot, _app=routes.APP_NAME, running=True)
    torn_down: list[object] = []

    class _Svc:
        def list_all(self):
            return []

    class _State:
        def __init__(self):
            self.slot: object | None = slot

        def get_slot(self, key):
            return self.slot if key == old_slot else None

    state = _State()

    async def _teardown(_state, _name, *, only_slot, require_archive):
        assert require_archive is True
        torn_down.append(only_slot)
        state.slot = None
        return True

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)

    assert await routes._remove_orphaned_executions(state) == {old_slot}
    assert torn_down == [slot]
    assert old_slot not in routes._OBSERVED_SLOT_KEYS.values()
    assert old_slot not in routes._OBSERVED_SPEC_DIRS


@pytest.mark.asyncio
async def test_create_cleanup_keeps_the_witness_when_orphan_archive_fails(tmp_path, monkeypatch):
    """A failed transcript archive remains retryable and blocks replacement."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    old_dir = str(tmp_path / "old-spec")
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index({})
    slot = types.SimpleNamespace(key=old_slot, _app=routes.APP_NAME, running=True)

    class _Svc:
        def list_all(self):
            return []

    class _State:
        def get_slot(self, key):
            return slot if key == old_slot else None

    async def _failed_archive(*_args, **_kwargs):
        return False

    service = _Svc()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: service)
    monkeypatch.setattr(routes, "_AutoNudgeService", _maintenance_loader(service))
    monkeypatch.setattr(routes, "_teardown_worker_slot", _failed_archive)

    with pytest.raises(RuntimeError, match="could not be archived"):
        await routes._remove_orphaned_executions(_State())

    assert routes._OBSERVED_SLOT_KEYS["s"] == old_slot
    assert routes._OBSERVED_SPEC_DIRS[old_slot] == routes._decision_key(old_dir)


@pytest.mark.asyncio
async def test_stop_barrier_captures_unindexed_observed_turn_after_full_rewrite(
    tmp_path, monkeypatch
):
    """Changing name, directory, and slot cannot detach an embedded-chat turn."""
    _redirect_state(monkeypatch, tmp_path)
    old_dir = str(tmp_path / "old-spec")
    old_slot = "spec-builder-s-1234abcd"
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._load_index()
    routes._save_index(
        {
            "t": {
                "spec_dir": str(tmp_path / "new-spec"),
                "slot_key": "spec-builder-t-deadbeef",
            }
        }
    )

    async with routes._execution_stop_barrier(
        str(tmp_path / "new-spec"), "spec-builder-t-deadbeef", "t"
    ) as capture:
        assert old_slot in capture


@pytest.mark.asyncio
async def test_unrelated_teardown_does_not_capture_a_same_name_slot_rewrite(tmp_path, monkeypatch):
    """A surviving name remains the control endpoint for its observed worker."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    rewritten_slot = "spec-builder-s-deadbeef"
    unrelated_slot = "spec-builder-t-feedface"
    old_dir = str(tmp_path / "old-spec")
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index(
        {
            "s": {"spec_dir": old_dir, "slot_key": rewritten_slot},
            "t": {
                "spec_dir": str(tmp_path / "unrelated"),
                "slot_key": unrelated_slot,
            },
        }
    )

    assert old_slot not in routes._unindexed_observed_slot_keys()
    async with routes._execution_stop_barrier(
        str(tmp_path / "unrelated"), unrelated_slot, "t"
    ) as capture:
        assert old_slot not in capture


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_slot", [None, "not-owned"])
async def test_unrelated_teardown_does_not_capture_a_surviving_name_with_bad_slot(
    tmp_path, monkeypatch, raw_slot
):
    """A missing or invalid raw key does not erase the old control endpoint."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    old_dir = str(tmp_path / "old-spec")
    loop = types.SimpleNamespace(
        id="loop-s",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(old_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == old_slot else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": old_slot}})
    routes._save_index(
        {
            "s": {"spec_dir": old_dir, "slot_key": raw_slot},
            "t": {
                "spec_dir": str(tmp_path / "unrelated"),
                "slot_key": "spec-builder-t-feedface",
            },
        }
    )

    assert old_slot not in routes._unindexed_observed_slot_keys()
    assert old_slot not in routes._matching_execution_loops("", "", set(), include_orphans=True)


def test_global_orphan_scan_ignores_an_unrelated_monitor_without_a_sentinel(tmp_path, monkeypatch):
    """An empty target cannot turn two empty directory keys into a direct match."""
    _redirect_state(monkeypatch, tmp_path)
    loop = types.SimpleNamespace(
        id="ordinary-loop",
        active=True,
        slot_key="spec-builder-ordinary-1234abcd",
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}ordinary'. Continue.",
        stop_sentinel_path="",
    )

    class _Svc:
        def list_all(self):
            return [loop]

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({})

    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {}


@pytest.mark.parametrize("raw_slot", [None, "not-owned"])
def test_restart_keeps_a_bad_slot_row_from_orphaning_its_durable_loop(
    tmp_path, monkeypatch, raw_slot
):
    """Cold-start name and directory witnesses do not depend on a valid raw key."""
    _redirect_state(monkeypatch, tmp_path)
    old_dir = str(tmp_path / "old-spec")
    old_slot = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="loop-s",
        active=True,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(old_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": old_dir, "slot_key": raw_slot}})

    assert routes._OBSERVED_SLOT_KEYS == {}
    assert routes._SLOT_KEYS == {}
    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {}


def test_targeted_cleanup_finds_an_inactive_loop_after_a_valid_slot_rewrite(tmp_path, monkeypatch):
    """A rewritten control endpoint still reaches its retained recovery marker."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-s-deadbeef"
    loop = types.SimpleNamespace(
        id="loop-s",
        active=False,
        slot_key=old_slot,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(spec_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": new_slot}})

    assert routes._matching_execution_loops("", "", set(), include_orphans=True) == {}
    assert routes._matching_execution_loops(
        "s",
        routes._decision_key(spec_dir),
        {new_slot},
        include_orphans=True,
        include_inactive_direct=True,
    ) == {old_slot: loop.id}


def test_inactive_indexed_loop_does_not_block_a_new_dispatch(tmp_path, monkeypatch):
    """A bounded run's recovery record stays teardown-only after planning resumes."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    slot_key = "spec-builder-s-1234abcd"
    loop = types.SimpleNamespace(
        id="loop-s",
        active=False,
        slot_key=slot_key,
        message=f"{routes._EXECUTION_HANDOFF_PREFIX}s'. Continue.",
        stop_sentinel_path=str(Path(spec_dir) / "STOP"),
    )

    class _Svc:
        def list_all(self):
            return [loop]

        def get_by_slot(self, key):
            return loop if key == slot_key else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": slot_key}})

    assert not routes._dispatch_claim_conflicts(spec_dir, slot_key, "s")
    assert routes._matching_execution_loops(
        "s",
        routes._decision_key(spec_dir),
        {slot_key},
        include_inactive_direct=True,
    ) == {slot_key: loop.id}


def test_delete_releases_every_captured_observed_creation_identity(tmp_path, monkeypatch):
    """Successful teardown clears witnesses whose old name also disappeared."""
    _redirect_state(monkeypatch, tmp_path)
    old_slot = "spec-builder-s-1234abcd"
    new_slot = "spec-builder-t-deadbeef"
    routes._OBSERVED_SLOT_KEYS.update({"s": old_slot, "t": new_slot})
    routes._SLOT_KEYS.update({"s": old_slot, "t": new_slot})
    routes._OBSERVED_SPEC_DIRS.update({old_slot: "/old", new_slot: "/new"})

    routes._forget_observed_slot_identity("t", new_slot, old_slot)

    assert "s" not in routes._OBSERVED_SLOT_KEYS
    assert "s" not in routes._SLOT_KEYS
    assert "t" not in routes._OBSERVED_SLOT_KEYS
    assert "t" not in routes._SLOT_KEYS
    assert old_slot not in routes._OBSERVED_SPEC_DIRS
    assert new_slot not in routes._OBSERVED_SPEC_DIRS


@pytest.mark.asyncio
async def test_dispatch_claims_arbitrate_across_rewritten_identity():
    """A rewritten index cannot start a second generation during final validation."""
    old_dir_key = "/canonical/spec"
    rewritten_dir_key = "/rewritten/spec"
    old_slot_key = "spec-builder-s-first"
    rewritten_slot_key = "spec-builder-s-second"

    first = routes._reserve_pending_dispatch(old_dir_key, old_slot_key, "s")
    assert first
    assert routes._reserve_pending_dispatch(rewritten_dir_key, rewritten_slot_key, "s") == ""
    blocked, refusal = routes._reserve_execution_claim(rewritten_dir_key, rewritten_slot_key, "s")
    assert blocked == "" and refusal == "taken"

    routes._drop_pending_dispatch(first)
    execution, refusal = routes._reserve_execution_claim(rewritten_dir_key, rewritten_slot_key, "s")
    assert execution and refusal == ""
    assert routes._reserve_pending_dispatch(old_dir_key, old_slot_key, "s") == ""


@pytest.mark.asyncio
async def test_finished_pending_claim_owner_does_not_block_a_later_request():
    """Request teardown is an ownership boundary even before its callback runs."""

    async def _reserve() -> str:
        return routes._reserve_pending_dispatch("/canonical/spec", "slot", "s")

    owner = asyncio.create_task(_reserve())
    stale = await owner
    assert stale
    assert owner.done()

    current = routes._reserve_pending_dispatch("/rewritten/spec", "replacement", "s")
    assert current and current != stale
    assert not routes._pending_dispatch_is_current(stale)


@pytest.mark.asyncio
async def test_published_claim_keeps_same_slot_message_queuing():
    """A matching live turn is not a rewritten second generation."""
    dir_key = "/canonical/spec"
    slot_key = "spec-builder-s-first"
    turn = asyncio.create_task(asyncio.sleep(60))
    slot = types.SimpleNamespace(task=turn)
    first = routes._reserve_pending_dispatch(dir_key, slot_key, "s")
    routes._bind_pending_dispatch_to_turn(first, slot, turn)
    try:
        queued = routes._reserve_pending_dispatch(dir_key, slot_key, "s")
        assert queued
        routes._drop_pending_dispatch(queued)
        assert (
            routes._reserve_pending_dispatch("/rewritten/spec", "spec-builder-s-second", "s") == ""
        )
    finally:
        routes._drop_pending_dispatch(first)
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn


@pytest.mark.asyncio
async def test_stale_stop_does_not_revoke_the_current_creation_claim(tmp_path, monkeypatch):
    """A rejected control from an old tab cannot cancel replacement startup."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    dir_key = routes._decision_key(spec_dir)
    token, refusal = routes._reserve_execution_claim(dir_key, slot_key, "s")
    assert token and refusal == ""

    halted: list[str] = []

    async def _halt(*_args, **_kwargs):
        halted.append("halted")

    monkeypatch.setattr(routes, "_halt_execution", _halt)
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs/s/stop",
            json={"spec_dir": spec_dir, "slot_key": f"{slot_key}-stale"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 409, payload
    assert payload.get("code") == "stale_client"
    assert routes._execution_claim_is_current(dir_key, token)
    assert halted == []


def test_handoff_serializes_its_durable_claim_before_slot_creation():
    """Stop must not finish before an older handoff's delayed claim write."""
    src = inspect.getsource(routes._handle_handoff)
    reserve_at = src.index("_reserve_execution_claim(")
    claim_at = src.index("await _claim_execution(", reserve_at)
    slot_at = src.index("_ensure_worker_slot(", claim_at)
    lock_at = src.index("async with _turn_lock(", reserve_at, slot_at)
    token_check_at = src.index("_execution_claim_is_current(", lock_at, claim_at)

    assert reserve_at < lock_at < token_check_at < claim_at < slot_at


@pytest.mark.asyncio
@pytest.mark.parametrize("physically_equivalent", [True, False])
async def test_final_alias_scan_refuses_own_slot_directory_rewrite(
    tmp_path, monkeypatch, physically_equivalent
):
    """A rewritten current entry cannot move Stop onto a different lock key."""
    _redirect_state(monkeypatch, tmp_path)
    original_dir = "/physical/spec-a"
    rewritten_dir = "/alias/spec-b"
    slot_key = "spec-builder-s-1234abcd"
    routes._save_index(
        {
            "s": {
                "spec_dir": rewritten_dir,
                "slot_key": slot_key,
                "state": {"phase": "tasks"},
            }
        }
    )
    monkeypatch.setattr(
        routes,
        "_same_spec_dir",
        lambda left, right: physically_equivalent
        and {left, right} == {original_dir, rewritten_dir},
    )

    conflict = await routes._final_alias_conflict(
        types.SimpleNamespace(get_slot=lambda _key: None),
        original_dir,
        slot_key,
        {},
        {},
        own_name="s",
    )

    assert conflict == "s"


@pytest.mark.asyncio
async def test_final_alias_scan_refuses_own_slot_identity_rewrite(tmp_path, monkeypatch):
    """A rewritten current entry cannot move Stop onto a different slot."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = "/physical/spec"
    original_slot = "spec-builder-s-1234abcd"
    routes._save_index(
        {
            "s": {
                "spec_dir": spec_dir,
                "slot_key": "spec-builder-s-deadbeef",
                "state": {"phase": "tasks"},
            }
        }
    )
    monkeypatch.setattr(routes, "_same_spec_dir", lambda left, right: left == right)

    conflict = await routes._final_alias_conflict(
        types.SimpleNamespace(get_slot=lambda _key: None),
        spec_dir,
        original_slot,
        {},
        {},
        own_name="s",
    )

    assert conflict == "s"


@pytest.mark.asyncio
async def test_legacy_message_pins_its_name_derived_slot_before_alias_scan(tmp_path, monkeypatch):
    """A genuine pre-key spec remains usable without weakening missing-key aliases."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {
            "s": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "spec_type": "feature",
                "status": "planning",
            }
        }
    )
    state, _slot = _slot_stub()
    dispatched: list[str] = []
    monkeypatch.setattr(
        routes,
        "_dispatch_turn",
        lambda *_args, **_kwargs: dispatched.append("sent"),
    )

    await client.start_server()
    try:
        client.app["state"] = state
        response = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": str(spec_dir), "text": "continue"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 200, payload
    assert routes._load_index()["s"]["slot_key"] == "spec-builder-s"
    assert dispatched == ["sent"]


@pytest.mark.asyncio
async def test_legacy_slot_read_cannot_erase_observed_per_creation_identity(tmp_path, monkeypatch):
    """A temporary legacy key cannot make later key removal look like migration."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = str(tmp_path / "spec")
    per_creation = "spec-builder-s-1234abcd"
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": per_creation}})
    routes._save_index({"s": {"spec_dir": spec_dir, "slot_key": "spec-builder-s"}})
    routes._save_index({"s": {"spec_dir": spec_dir}})

    assert routes._OBSERVED_SLOT_KEYS["s"] == per_creation
    assert routes._slot_key("s") == per_creation
    assert await routes._pin_legacy_slot_identity("s", {"spec_dir": spec_dir}) is None


@pytest.mark.asyncio
async def test_stop_revokes_a_message_after_its_final_scan_captured_old_identity(
    tmp_path, monkeypatch
):
    """Stop on a rewritten path cannot be followed by the stale turn publication."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    state, _slot = _slot_stub()
    final_scan_waiting = asyncio.Event()
    release_final_scan = asyncio.Event()
    scans = 0

    async def _scan(*_args, **_kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            final_scan_waiting.set()
            await release_final_scan.wait()
        return {}

    monkeypatch.setattr(routes, "_alias_slots", _scan)
    dispatched: list[str] = []
    monkeypatch.setattr(
        routes,
        "_dispatch_turn",
        lambda *_args, **_kwargs: dispatched.append("sent"),
    )

    await client.start_server()
    try:
        client.app["state"] = state
        request = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/message",
                json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "continue"},
            )
        )
        await asyncio.wait_for(final_scan_waiting.wait(), timeout=1)
        rewritten_dir = str(tmp_path / "wd" / ".kiro" / "specs" / "rewritten")
        replacement_slot = "spec-builder-s-deadbeef"
        routes._save_index(
            {
                "s": {
                    "spec_dir": rewritten_dir,
                    "working_dir": str(tmp_path / "wd"),
                    "spec_type": "feature",
                    "status": "planning",
                    "slot_key": replacement_slot,
                }
            }
        )
        async with routes._execution_stop_barrier(
            routes._decision_key(rewritten_dir), replacement_slot, "s"
        ) as capture:
            capture.commit()
        release_final_scan.set()
        response = await request
        payload = await response.json()
    finally:
        release_final_scan.set()
        await client.close()

    assert response.status == 409, payload
    assert payload.get("code") == "execution_stopped_during_start"
    assert dispatched == []


@pytest.mark.asyncio
async def test_stop_targets_the_published_slot_after_index_identity_rewrite(tmp_path, monkeypatch):
    """A published turn remains stoppable under the identity it actually uses."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, old_slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    replacement_slot_key = "spec-builder-s-deadbeef"
    old_turn = asyncio.create_task(asyncio.sleep(60))
    old_slot = types.SimpleNamespace(
        key=old_slot_key,
        _app=routes.APP_NAME,
        running=True,
        task=old_turn,
    )

    class _State:
        def get_slot(self, key):
            return old_slot if key == old_slot_key else None

    token = routes._reserve_pending_dispatch(routes._decision_key(spec_dir), old_slot_key, "s")
    routes._bind_pending_dispatch_to_turn(token, old_slot, old_turn)
    rewritten = routes._load_index()
    rewritten["s"]["slot_key"] = replacement_slot_key
    routes._save_index(rewritten)
    halted: list[object] = []

    async def _halted(*_args, **kwargs):
        halted.append(kwargs.get("only_slot"))

    monkeypatch.setattr(routes, "_halt_execution", _halted)
    await client.start_server()
    try:
        client.app["state"] = _State()
        response = await client.post(
            f"{_BASE}/specs/s/stop",
            json={"spec_dir": spec_dir, "slot_key": old_slot_key},
        )
        payload = await response.json()
    finally:
        old_turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await old_turn
        await client.close()

    assert response.status == 200, payload
    assert halted == [old_slot]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "delete"])
@pytest.mark.parametrize("with_claim", [True, False])
@pytest.mark.parametrize("rewrite_directory", [True, False])
async def test_teardown_targets_an_idle_published_autonudge_after_identity_rewrite(
    tmp_path, monkeypatch, operation, with_claim, rewrite_directory
):
    """Stop and Delete find the old loop both before and after a gateway restart."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, old_slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    replacement_slot_key = "spec-builder-t-deadbeef"
    old_turn = asyncio.create_task(asyncio.sleep(0))
    old_slot = types.SimpleNamespace(
        key=old_slot_key,
        _app=routes.APP_NAME,
        running=False,
        task=old_turn,
    )
    loop = types.SimpleNamespace(
        id="loop-old-identity",
        active=not with_claim,
        slot_key=old_slot_key,
        stop_sentinel_path=Path(spec_dir) / "STOP",
    )
    removed: list[str] = []

    class _Svc:
        def list_all(self):
            return [loop] if loop.active else []

        def get_by_slot(self, key):
            return loop if key == old_slot_key and loop.active else None

        async def remove(self, loop_id):
            removed.append(loop_id)
            loop.active = False

    class _State:
        def get_slot(self, key):
            return old_slot if key == old_slot_key else None

    async def _archived(*_args, **_kwargs):
        return True

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    monkeypatch.setattr(routes, "_teardown_worker_slot", _archived)
    if with_claim:
        claim, refusal = routes._reserve_execution_claim(
            routes._decision_key(spec_dir), old_slot_key, "s"
        )
        assert claim and refusal == ""
        loop.active = True
        routes._bind_execution_claim_to_turn(
            routes._decision_key(spec_dir), claim, old_slot, old_turn
        )
    await old_turn
    await asyncio.sleep(0)

    rewritten = routes._load_index().pop("s")
    rewritten_dir = str(tmp_path / "rewritten-spec") if rewrite_directory else spec_dir
    rewritten.update(
        spec_dir=rewritten_dir,
        slot_key=replacement_slot_key,
        status="executing",
    )
    routes._save_index({"t": rewritten})

    await client.start_server()
    try:
        client.app["state"] = _State()
        body = {
            "spec_dir": rewritten_dir,
            "slot_key": replacement_slot_key,
        }
        if operation == "stop":
            response = await client.post(f"{_BASE}/specs/t/stop", json=body)
        else:
            response = await client.delete(f"{_BASE}/specs/t", json=body)
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 200, payload
    assert removed == [loop.id]
    assert loop.active is False
    if operation == "delete":
        assert old_slot_key not in routes._OBSERVED_SPEC_DIRS
        assert old_slot_key not in routes._OBSERVED_SLOT_KEYS.values()


@pytest.mark.asyncio
async def test_stop_arriving_during_authorization_unwinds_before_dispatch(tmp_path, monkeypatch):
    """The post-authorization gate observes Stop before Stop acquires the lock."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    slot = types.SimpleNamespace(
        key=slot_key,
        _app=routes.APP_NAME,
        running=False,
        task=None,
        messages=[],
        project=str(tmp_path / "wd"),
        _titled=True,
    )

    class _State:
        def __init__(self):
            self._background_tasks: set[asyncio.Task] = set()

        def get_slot(self, key):
            return slot if key == slot_key else None

        def get_or_create_slot(self, _key, app=""):
            return slot

    loop = types.SimpleNamespace(id="loop-authorization", active=False)
    removed: list[str] = []

    class _Svc:
        def get_by_slot(self, _key):
            return loop if loop.active else None

        async def remove(self, loop_id):
            removed.append(loop_id)
            loop.active = False

    authorizing = asyncio.Event()
    release_authorization = asyncio.Event()
    stop_published = asyncio.Event()

    async def _authorized(**_kwargs):
        authorizing.set()
        await release_authorization.wait()
        loop.active = True
        return loop, "", 200

    async def _halted(*_args, **_kwargs):
        return None

    real_barrier = routes._execution_stop_barrier

    @contextlib.asynccontextmanager
    async def _observed_barrier(dir_key, barrier_slot_key, barrier_name):
        async with real_barrier(dir_key, barrier_slot_key, barrier_name) as claimed_slot_keys:
            stop_published.set()
            yield claimed_slot_keys

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())
    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    monkeypatch.setattr(routes, "_halt_execution", _halted)
    monkeypatch.setattr(routes, "_execution_stop_barrier", _observed_barrier)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    handoff = stop = None
    await client.start_server()
    try:
        client.app["state"] = _State()
        handoff = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/handoff",
                json={"spec_dir": spec_dir, "slot_key": slot_key},
            )
        )
        await authorizing.wait()
        stop = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/stop",
                json={"spec_dir": spec_dir, "slot_key": slot_key},
            )
        )
        await stop_published.wait()
        release_authorization.set()

        handoff_response = await handoff
        handoff_payload = await handoff_response.json()
        stop_response = await stop
        stop_payload = await stop_response.json()
    finally:
        release_authorization.set()
        for task in (handoff, stop):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await client.close()

    assert handoff_response.status == 409, handoff_payload
    assert handoff_payload.get("code") == "execution_stopped_during_start"
    assert stop_response.status == 200, stop_payload
    assert removed == [loop.id], "the handoff's armed loop survived its revoked claim"
    assert dispatched == [], "handoff dispatched after Stop published its barrier"


@pytest.mark.asyncio
async def test_an_alias_mid_turn_blocks_an_ordinary_message(tmp_path, monkeypatch):
    """Not just decision answers: ANY dispatch would be a second agent on these files.

    Gating the alias check on `decision_id` left the plain message path wide open -- the
    ordinary way a user talks to a spec -- so two agents could edit one spec's documents
    concurrently while the decision lock looked after itself.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: busy if k == alias_key else real_get_slot(k)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "a turn started while another view's agent was working"


@pytest.mark.asyncio
async def test_a_completed_alias_turn_during_repin_blocks_an_ordinary_message(
    tmp_path, monkeypatch
):
    """An ordinary message's final gate sees alias history across its await."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
    from kiro_crew.dashboard.state import _ChatSlot

    alias_slot = _ChatSlot(alias_key)
    state, _slot = _slot_stub()
    real_get_slot = state.get_slot
    state.get_slot = lambda key: alias_slot if key == alias_key else real_get_slot(key)
    real_touch = routes._touch_spec
    touches = 0

    async def _touch_while_alias_turn_completes(*args, **kwargs):
        nonlocal touches
        result = await real_touch(*args, **kwargs)
        touches += 1
        if touches == 2:
            alias_slot.task = asyncio.create_task(asyncio.sleep(0))
            await alias_slot.task
            alias_slot.append("done", "", "done")
            alias_slot.task = None
        return result

    monkeypatch.setattr(routes, "_touch_spec", _touch_while_alias_turn_completes)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == []


@pytest.mark.asyncio
async def test_an_alias_missing_its_persisted_slot_key_refuses_a_message(tmp_path, monkeypatch):
    """An agent-writable index edit cannot hide an alias's active worker.

    A per-creation worker keeps running under the key minted when the alias was
    created. If the alias later drops ``slot_key`` from the index, falling back to
    its legacy name-derived key looks up a different, idle slot and admits a second
    agent over the same files. An ownership-invalid key has to make the alias
    occupied until its identity can be established again.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    index = await routes._aload_index()
    alias_key = _alias_of(index, "s", "s-alias")

    edited = await routes._aload_index()
    edited["s-alias"].pop("slot_key")
    routes._save_index(edited)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda key: busy if key == alias_key else real_get_slot(key)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "the missing alias identity hid its active worker"


@pytest.mark.asyncio
async def test_an_alias_mid_turn_blocks_a_handoff(tmp_path, monkeypatch):
    """...and a handoff, which starts an autonomous build -- the most expensive way to
    end up with two agents in one spec directory.

    Until round 27 this asserted that the refusal RELEASES the loop it had already
    armed, because arming happened before the turn lock: a bare 409 left an active
    timer that later dispatched the very build the refusal denied. Round 27 moved
    arming inside the lock and AFTER this check, so no loop is armed on this path at
    all. The assertion is therefore stronger now -- authorization is never reached,
    so there is nothing to leak and no release to get right.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: busy if k == alias_key else real_get_slot(k)

    armed: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: armed.append(a[2]))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    authorized: list = []

    async def _authorized(**_kw):
        # (armed_loop, authz_err, status) -- the tuple the handler unpacks.
        authorized.append(True)
        return types.SimpleNamespace(id="loop-1"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)

    released: list = []

    async def _remove(_slot_key, *, only_loop_id=None):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert armed == [], "a build was armed while another view's agent was working"
    assert authorized == [], (
        "the handoff armed a nudge loop before checking whether an alias was mid-turn, "
        "so the refusal has a live timer to clean up again"
    )
    assert released == [], "nothing was armed, so nothing should have been released"


@pytest.mark.asyncio
async def test_a_completed_alias_turn_during_authorization_blocks_a_handoff(tmp_path, monkeypatch):
    """The final handoff gate sees a turn whose task returned to ``None``."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
    from kiro_crew.dashboard.state import _ChatSlot

    alias_slot = _ChatSlot(alias_key)
    state, _slot = _slot_stub()
    real_get_slot = state.get_slot
    state.get_slot = lambda key: alias_slot if key == alias_key else real_get_slot(key)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _authorized(**_kw):
        alias_slot.task = asyncio.create_task(asyncio.sleep(0))
        await alias_slot.task
        alias_slot.append("done", "", "done")
        alias_slot.task = None
        return types.SimpleNamespace(id="loop-alias-raced"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    released: list[str] = []

    async def _remove(_slot_key, *, only_loop_id=None):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == []
    assert released == ["loop-alias-raced"]
    assert routes._load_index()["s"].get("status") == "planning"


@pytest.mark.asyncio
async def test_handoff_refuses_when_its_own_slot_starts_during_authorization(tmp_path, monkeypatch):
    """Authorization awaits while channel traffic can start the same slot.

    The pre-arm snapshot is no longer authoritative after that await. Dispatching the
    build would queue it behind the new turn, and Pause deliberately clears that queue,
    despite this endpoint reporting that execution started.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    state, slot = _slot_stub()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _authorized(**_kw):
        slot.running = True
        return types.SimpleNamespace(id="loop-raced"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    released: list[str] = []

    async def _remove(_slot_key, *, only_loop_id=None):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_agent_busy", payload
    assert dispatched == []
    assert released == ["loop-raced"], "the armed timer survived the busy refusal"
    assert routes._load_index()["s"].get("status") == "planning"


@pytest.mark.asyncio
async def test_handoff_refuses_when_its_own_slot_starts_during_the_final_repin(
    tmp_path, monkeypatch
):
    """The final freshness write awaits, so channel traffic can start there too."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    state, slot = _slot_stub()
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _authorized(**_kw):
        return types.SimpleNamespace(id="loop-repin-raced"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    real_touch = routes._touch_spec

    async def _touch_then_start(*args, **kwargs):
        result = await real_touch(*args, **kwargs)
        if kwargs.get("exec_arming_at") == 0.0 and kwargs.get("status") is None:
            slot.running = True
        return result

    monkeypatch.setattr(routes, "_touch_spec", _touch_then_start)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    released: list[str] = []

    async def _remove(_slot_key, *, only_loop_id=None):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_agent_busy", payload
    assert dispatched == []
    assert released == ["loop-repin-raced"], "the armed timer survived the busy refusal"
    assert routes._load_index()["s"].get("status") == "planning"


def test_the_busy_refusal_cannot_leak_an_armed_loop():
    """Source guard: the busy check must precede the arm.

    Round 18 fixed a leaked timer by releasing it in the refusal; round 27 removed
    the leak instead by arming after the check. Ordering is the property worth
    pinning -- a future edit that moves arming back above the check reintroduces a
    hazard that only shows up as a build firing minutes after a 409.
    """
    src = inspect.getsource(routes._handle_handoff)
    lock = src.index("async with _turn_lock(handoff_dir_key):")
    busy = src.index("_busy_alias(", lock)
    arm = src.index("await authorize_and_add_nudge(", lock)
    assert busy < arm, (
        "the handoff arms its nudge loop before checking for a busy alias, so a "
        "refusal leaves a timer that dispatches the build it just denied"
    )
    assert lock < arm, (
        "the loop is armed outside the directory turn lock, so its idle timer can "
        "dispatch while this handler is still waiting for the lock"
    )


@pytest.mark.asyncio
async def test_the_alias_set_excludes_our_own_slot(tmp_path, monkeypatch):
    """Our own slot is the same session, and must never read as a concurrent editor.

    That exclusion is what preserves same-slot queuing: a message to the session that is
    working is queued by _dispatch_turn, the long-standing behaviour. Only ANOTHER name's
    slot is a second agent on these documents, and only that refuses.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    key = routes._decision_key(spec_dir)
    aliases = await routes._alias_slots(key, own_slot_key=slot_key)

    assert aliases == {alias_key: "s-alias"}
    assert slot_key not in aliases, "our own running slot would read as a concurrent editor"


def test_an_unresolvable_directory_keeps_its_normalized_literal_key(tmp_path, monkeypatch):
    """A lexical key normalizes even when the directory cannot be resolved."""
    loop_a = tmp_path / "A"
    loop_b = tmp_path / "B"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path.lower())

    assert routes._decision_key(str(loop_a)) == os.path.normpath(str(loop_a)).lower()


@pytest.mark.asyncio
async def test_an_alias_with_an_armed_build_blocks_the_answer(tmp_path, monkeypatch):
    """A running turn is not the only way an alias occupies these documents.

    An autonudge execution loop sits IDLE between its nudge cycles, so a busy check that
    asks only whether the slot is running right now reads a live build as free: the other
    name dispatches, then the loop's timer fires, and two agents write one spec's files.
    The loop is as much an occupant as the turn it periodically starts.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    # The alias's slot is IDLE -- only its loop is armed, which is the whole point.
    state, _slot = _slot_stub()
    idle = types.SimpleNamespace(key=alias_key, running=False, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: idle if k == alias_key else real_get_slot(k)

    class _Svc:
        def get_by_slot(self, key):
            return types.SimpleNamespace(active=True) if key == alias_key else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "an answer was dispatched while an alias's build was armed"
    assert (
        await _recorded_answers(spec_dir) == {}
    ), "the answer was recorded even though nothing was dispatched"


@pytest.mark.asyncio
async def test_a_finished_alias_loop_does_not_block(tmp_path, monkeypatch):
    """...and a loop the service has already deactivated is NOT an occupant.

    The loop is capped, and the service flips `active` itself without telling this app, so
    treating any registry hit as busy would strand every later dispatch behind a build that
    finished.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    idle = types.SimpleNamespace(key=alias_key, running=False, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: idle if k == alias_key else real_get_slot(k)

    class _Svc:
        def get_by_slot(self, key):
            return types.SimpleNamespace(active=False)

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert dispatched == ["just a note"]


def test_the_busy_check_reads_no_filesystem():
    """Source guard: `_busy_alias` runs ON the event loop, so it must ask the nudge
    registry by SLOT KEY and derive nothing itself.

    Round 19 justified this with a claim that was wrong: `_slot_key` reads the
    module-global `_SLOT_KEYS`, not the index, so calling it is not a filesystem hop.
    The guard stands for the reason that survived round 21 -- key derivation belongs
    in ONE off-loop place (`_alias_slots_locked`), because that is what makes every
    alias key ownership-validated instead of trusted raw from an agent-writable file.
    Re-deriving per call here would also invite an unvalidated shortcut back in.
    """
    src = inspect.getsource(routes._busy_alias)
    assert "_exec_loop_active_for_slot(" in src, "the busy check ignores an armed loop"
    assert "_exec_loop_active(" not in src.replace(
        "_exec_loop_active_for_slot(", ""
    ), "the busy check resolves a loop BY NAME, re-deriving a key it was handed"
    assert "_slot_key(" not in src, "the busy check derives a key instead of using the resolved one"


def test_alias_keys_are_resolved_not_trusted_from_the_index():
    """Source guard: the alias scan must not read ``slot_key`` raw from the entry.

    index.json is agent-writable, so a raw read lets an alias hide from the busy
    scan. This is the one place the resolution happens, so pin it here.
    """
    src = inspect.getsource(routes._alias_slots_locked)
    assert "_slot_key(other)" in src, "alias keys are not resolved through the validated map"
    assert "_owns_slot_key(other, persisted)" in src
    assert "out[persisted]" not in src, "the alias scan trusts the raw persisted key"


class _AsyncReturn:
    """An awaitable stand-in that ignores its arguments and returns ``value``."""

    def __init__(self, value):
        self.value = value

    async def __call__(self, *_a, **_kw):
        return self.value


def _create_client(monkeypatch, tmp_path):
    """A client whose create tail is stubbed out, plus its state.

    ``_make_client`` performs the state redirect, so it must run BEFORE anything
    seeds the ledger -- seeding first writes into the pre-redirect location, and a
    test asserting "no answers recorded" would then pass while proving nothing.

    The clear under test runs long before the worker slot is touched, so the slot
    setup and the seed dispatch are stubbed: neither is what these tests measure.
    """
    client = _make_client(monkeypatch, tmp_path)
    state, slot = _slot_stub()
    monkeypatch.setattr(routes, "_ensure_worker_slot", _AsyncReturn(slot))
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: None)
    return client, state


@pytest.mark.asyncio
async def test_creating_a_spec_clears_an_orphaned_record_at_its_path(tmp_path, monkeypatch):
    """The residue a failed delete-cleanup leaves must not settle the NEXT spec.

    A delete clears the ledger only after the index entry is gone, and only
    best-effort, so a crash in that window leaves answers for documents that no
    longer exist. Decision ids are agent-authored labels that recur across specs,
    so without this the new spec's own question renders locked to an answer the
    user never gave for it -- and answering it is refused.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "reborn"
    client, state = _create_client(monkeypatch, tmp_path)

    # The residue: a record for this exact directory, with no index entry and no
    # documents -- precisely what a delete whose cleanup failed leaves behind.
    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "reborn",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    routes._save_index({})
    # The fixture must be readable through the redirect, or this test proves nothing.
    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "reborn", "working_dir": str(project), "spec_type": "feature"},
        )
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()

    assert await _recorded_answers(str(spec_dir)) == {}, (
        "the new spec inherited the deleted spec's answers, so a question it never "
        "asked renders locked and cannot be answered"
    )


@pytest.mark.asyncio
async def test_creating_a_spec_keeps_a_live_alias_answers(tmp_path, monkeypatch):
    """The boundary on the other side: clearing must never reopen a settled decision.

    Two names can serve one directory. If another name is still indexed on it, its
    answers are live -- erasing them on a create would hand that spec a clean slate
    for decisions already settled, which is the reversal this design refuses.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "shared"
    client, state = _create_client(monkeypatch, tmp_path)

    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "alias",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    # A live alias: a DIFFERENT name, indexed, pointing at the same directory.
    routes._save_index(
        {
            "alias": {
                "working_dir": str(project),
                "spec_dir": str(spec_dir),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": "spec-builder-alias-1",
                "created_at": 1.0,
                "updated_at": 1.0,
            }
        }
    )
    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "shared", "working_dir": str(project), "spec_type": "feature"},
        )
        assert resp.status in (201, 409), await resp.json()
    finally:
        await client.close()

    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}, (
        "creating a second name on the directory erased the live alias's settled "
        "answer, reopening a decision the user already made"
    )


@pytest.mark.asyncio
async def test_importing_existing_documents_keeps_their_answers(tmp_path, monkeypatch):
    """import_existing adopts documents that already exist, so their answers stand.

    This is the case where the record was written FOR these files. Clearing here
    would reopen settled decisions on re-adoption, so the create-time clear is
    deliberately scoped to a spec whose documents are new.
    """
    project = tmp_path / "proj"
    spec_dir = project / ".kiro" / "specs" / "adopted"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# theirs")
    client, state = _create_client(monkeypatch, tmp_path)

    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "adopted",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    routes._save_index({})
    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "adopted",
                "working_dir": str(project),
                "spec_type": "feature",
                "import_existing": True,
            },
        )
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()

    assert await _recorded_answers(str(spec_dir)) == {
        "transport": "HTTPS"
    }, "adopting the documents an answer was recorded for dropped that answer"


def test_the_create_time_clear_is_gated_on_new_documents():
    """Source guard: the clear must sit behind ``not import_existing``.

    Ungating it would clear on adoption too, which is the reversal direction --
    and no unit test of the happy path would notice.
    """
    src = inspect.getsource(routes._handle_create)
    assert "if not import_existing:" in src, "the create-time ledger clear is not gated"
    gate = src.index("if not import_existing:")
    clear = src.index("_forget_decisions(", gate)
    body = src[gate:clear]
    # Every line between the gate and the clear must stay indented under it.
    for line in body.splitlines()[1:]:
        if line.strip():
            assert line.startswith(
                "        "
            ), "the ledger clear is not inside the import_existing gate"


@pytest.mark.asyncio
async def test_create_refuses_when_a_stale_record_survives_the_clear(tmp_path, monkeypatch):
    """A failed clear over a READABLE record must refuse, not warn and continue.

    Otherwise the create proceeds and the new spec inherits the answers the clear
    was there to remove -- the round-20 bug, reached through its own failure branch.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "stuck"
    client, state = _create_client(monkeypatch, tmp_path)

    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "stuck",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    routes._save_index({})

    # The ledger write fails, exactly as a full disk or a read-only state dir would.
    def _boom(_store):
        raise OSError("no space left on device")

    monkeypatch.setattr(routes, "_save_decisions", _boom)

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "stuck", "working_dir": str(project), "spec_type": "feature"},
        )
        status, payload = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503, f"{status}: {payload}"
    assert payload["code"] == "decision_record_not_cleared", payload
    # And nothing was registered, so a retry is clean rather than a half-built spec.
    assert "stuck" not in routes._load_index(), (
        "the refused create still registered the spec, so its cards would render "
        "locked to the previous spec's answers"
    )


@pytest.mark.asyncio
async def test_create_refuses_when_the_ledger_is_unreadable(tmp_path, monkeypatch):
    """This test asserted the OPPOSITE until round 26.

    Round 21 let a create proceed when the clear failed only because the store was
    unusable, reasoning that nothing can read a stale answer out of a store nothing
    can read. That is wrong, and the error is worth keeping visible: unreadability
    is a property of ONE READ, not of the store. A transient failure -- a partial
    write, a momentary IO error -- leaves the old record intact on disk, so the
    probe returned empty, the create proceeded, and the record became readable
    again afterwards and overlaid its answers onto the brand-new spec.

    So the refusal is back on the clear's own result, which is the only signal that
    actually reports whether the ledger is clean. The cost is that a corrupt
    decisions store blocks new spec creation -- loud, and the correct direction to
    fail for a trust root.
    """
    project = tmp_path / "proj"
    project.mkdir()
    client, state = _create_client(monkeypatch, tmp_path)
    routes._save_index({})
    monkeypatch.setattr(routes, "_read_decisions", lambda: ({}, False))

    await client.start_server()
    try:
        client.app["state"] = state
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "fresh", "working_dir": str(project), "spec_type": "feature"},
        )
        status, payload = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503, f"{status}: {payload}"
    assert payload["code"] == "decision_record_unreadable", payload
    assert "fresh" not in routes._load_index(), "the refused create still registered the spec"


def test_the_refusal_unwinds_a_worktree_it_created():
    """Source guard: the 503 must not leak the worktree create just made.

    Every other refusal in this handler removes it; this one is reached after the
    worktree exists, so omitting it would leave an orphaned checkout behind.
    """
    src = inspect.getsource(routes._handle_create)
    at = src.index("decision_record_not_cleared")
    # Look back from the refusal payload to the branch it belongs to.
    branch = src[src.index("if not cleared", 0) : at]
    assert (
        "_remove_worktree(" in branch
    ), "the stale-record refusal returns without removing a worktree it created"


def _two_names_one_dir(tmp_path, *, alias_key):
    """Index two names on ONE spec directory; the alias carries ``alias_key``.

    ``alias_key`` is written verbatim, which is the point: the field is
    agent-writable, so the tests drive the values an agent could actually put there.
    """
    spec_dir = tmp_path / "proj" / ".kiro" / "specs" / "shared"
    spec_dir.mkdir(parents=True)
    entry = {
        "working_dir": str(tmp_path / "proj"),
        "spec_dir": str(spec_dir),
        "spec_type": "feature",
        "status": "planning",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    index = {
        "mine": {**entry, "slot_key": "spec-builder-mine-aaaaaaaa"},
        "alias": {**entry, "slot_key": alias_key},
    }
    routes._save_index(index)
    return str(spec_dir)


@pytest.mark.asyncio
async def test_an_alias_with_no_slot_key_is_still_seen(tmp_path, monkeypatch):
    """Deleting the field used to drop the alias from the scan entirely.

    The old code skipped an entry with no key, so an agent could delete its own
    ``slot_key`` and become invisible to every busy check -- then both names ran a
    turn over the same documents.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-mine-aaaaaaaa")

    assert "alias" in aliases.values(), (
        "an alias with no slot_key vanished from the scan, so a concurrent turn "
        "under it would not block"
    )
    # Missing identity fails closed because a per-creation worker may still be running
    # under the removed key; guessing the legacy key would make it invisible.
    assert aliases == {None: "alias"}, aliases


@pytest.mark.asyncio
async def test_an_invalid_identity_cannot_claim_the_own_slot_exemption(tmp_path, monkeypatch):
    """The fallback key is not proof that it names the running creation.

    Removing a per-creation key makes ``_slot_key`` fall back to the legacy key. If
    that fallback is compared with ``own_slot_key`` before ownership validation, the
    invalid entry disappears from the busy scan even though its original worker may
    still be editing the directory.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = Path(tmp_path, "spec")
    spec_dir.mkdir()
    routes._save_index(
        {
            "mine": {
                "working_dir": str(tmp_path / "proj"),
                "spec_dir": str(spec_dir),
                "spec_type": "feature",
                "status": "planning",
                "created_at": 1.0,
                "updated_at": 1.0,
            }
        }
    )

    aliases = await routes._alias_slots(
        routes._decision_key(str(spec_dir)), own_slot_key="spec-builder-mine"
    )

    assert aliases == {None: "mine"}, (
        "an ownership-invalid entry used its guessed legacy key to disappear as "
        f"the caller's own slot: {aliases}"
    )


@pytest.mark.asyncio
async def test_an_alias_that_copies_our_key_is_still_seen(tmp_path, monkeypatch):
    """Copying the caller's key used to make the alias read as "our own slot".

    Own-slot exclusion exists so a same-session message can queue rather than be
    refused. Keyed on a field the agent writes, it became a way to borrow the
    exemption -- ownership validation rejects a key that does not encode the
    alias's own name, so the copy cannot be claimed.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="spec-builder-mine-aaaaaaaa")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-mine-aaaaaaaa")

    assert aliases == {
        None: "alias"
    }, f"the alias borrowed our slot key to escape the busy scan: {aliases}"


@pytest.mark.asyncio
async def test_a_legitimate_per_creation_key_still_resolves(tmp_path, monkeypatch):
    """The boundary: a valid, owned key must survive resolution unchanged.

    Otherwise the scan would look for a slot the alias does not actually run in,
    and a genuinely busy alias would read as free.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="spec-builder-alias-bbbbbbbb")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-mine-aaaaaaaa")

    assert aliases == {"spec-builder-alias-bbbbbbbb": "alias"}, aliases


@pytest.mark.asyncio
async def test_our_own_name_is_still_excluded(tmp_path, monkeypatch):
    """Resolution must not break same-session queuing.

    Our own slot is excluded so an ordinary message queues instead of being
    refused; resolving keys must leave that exclusion working.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="spec-builder-alias-bbbbbbbb")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-alias-bbbbbbbb")

    assert aliases == {
        "spec-builder-mine-aaaaaaaa": "mine"
    }, f"own-slot exclusion stopped working after resolution: {aliases}"


@pytest.mark.asyncio
async def test_stop_waits_for_an_in_flight_decision_answer(tmp_path, monkeypatch):
    """The race: Stop used to cancel a turn a decision answer was mid-dispatch.

    The answer path records the answer and dispatches it while holding the
    directory turn lock. Stop took no lock, so it could land BETWEEN those two
    steps -- the turn it cancelled was the one carrying the answer, and because the
    record is never rewritten the card stayed locked to an answer the agent never
    received. Stop now takes the same lock, so it cannot observe that midpoint.

    Timing is deliberate: the halt sets an Event, and the assertion is that it does
    not fire within a REAL timeout while the lock is held. An earlier version of
    this test yielded with sleep(0), which never let the request cross its thread
    hops -- so the halt was absent either way and removing the lock still passed.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _unused = _decision_spec(tmp_path)
    dir_key = routes._decision_key(spec_dir)

    halted = asyncio.Event()

    async def _halt_spy(*_a, **_kw):
        halted.set()

    monkeypatch.setattr(routes, "_halt_execution", _halt_spy)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        async with routes._turn_lock(dir_key):
            stop = asyncio.ensure_future(client.post(f"{_BASE}/specs/s/stop", json={}))
            with pytest.raises(asyncio.TimeoutError):
                # Long enough for the request to finish _client_claim, _aload_index
                # and _acanonical_dir -- i.e. long enough to reach the halt if it
                # were not serialized. The mutation that frees the lock trips this.
                await asyncio.wait_for(asyncio.shield(halted.wait()), timeout=1.5)
            assert (
                not halted.is_set()
            ), "Stop halted the run while the answer path held the turn lock"
        # Released: the same request must now get through.
        await asyncio.wait_for(halted.wait(), timeout=5)
        resp = await stop
        await resp.read()
    finally:
        await client.close()

    assert halted.is_set(), "Stop never halted after the lock was released"


def test_stop_takes_the_directory_turn_lock():
    """Source guard: Stop is destructive and must join the serialized family.

    The behavioural test above can only observe the lock it knows about; this
    pins that Stop keeps taking it, and takes it on the CANONICAL directory (a
    name-keyed lock would not serialize two names on one spec).
    """
    src = inspect.getsource(routes._handle_stop_execution)
    assert "_turn_lock(" in src, "Stop does not serialize against the answer path"
    assert "_decision_key(" in src, "Stop keys its lock on something other than the directory"
    lock_at = src.index("_turn_lock(")
    assert "_halt_execution(" in src[lock_at:], "the halt runs outside the lock"


def test_stop_rechecks_the_spec_after_waiting_for_the_lock():
    """Waiting on a lock is an await, so the pre-lock snapshot can go stale.

    Halting on that snapshot could cancel a REPLACEMENT spec's run, which is the
    same class of bug the body-first ordering above the lock already guards.
    """
    src = inspect.getsource(routes._handle_stop_execution)
    lock_at = src.index("_turn_lock(dir_key)")
    after = src[lock_at:]
    assert "_aload_index()" in after, "Stop reuses a snapshot taken before it held the lock"
    assert after.index("_aload_index()") < after.index(
        "_halt_execution("
    ), "Stop re-reads the index only after halting, which is too late"


@pytest.mark.asyncio
async def test_stop_refuses_a_spec_recreated_at_the_same_path(tmp_path, monkeypatch):
    """The directory cannot detect a delete + recreate; the slot key can.

    Round 23 rechecked the canonical directory after waiting for the lock, but a
    recreate at the SAME path resolves to the same directory -- so the recheck
    passed and Stop cancelled the replacement's run. Slot keys are minted per
    creation, so the replacement necessarily carries a different one.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dir_key = routes._decision_key(spec_dir)

    halted: list = []

    async def _halt_spy(*_a, **_kw):
        halted.append("halt")

    monkeypatch.setattr(routes, "_halt_execution", _halt_spy)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        async with routes._turn_lock(dir_key):
            stop = asyncio.ensure_future(client.post(f"{_BASE}/specs/s/stop", json={}))
            await asyncio.sleep(0.75)  # let it reach the lock and block there
            # The spec is deleted and recreated at the SAME path while Stop waits:
            # same directory, new creation, new slot key.
            index = routes._load_index()
            routes._forget_observed_slot_identity("s", slot_key)
            index["s"]["slot_key"] = "spec-builder-s-99999999"
            routes._save_index(index)
        resp = await stop
        status, payload = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 409, f"{status}: {payload}"
    assert payload["code"] == "stale_client", payload
    assert halted == [], "Stop cancelled the run of a spec created after it started waiting"
    assert slot_key != "spec-builder-s-99999999"


def test_no_path_evicts_a_turn_lock():
    """Source guard: nothing may pop `_TURN_LOCKS`.

    The eviction is unsafe from every call site for the same reason, so the rule is
    global rather than per-handler -- a future cleanup that reintroduces it here
    would silently reopen concurrent turns.
    """
    src = routes_source()
    assert "_TURN_LOCKS.pop(" not in src, "a turn lock is evicted somewhere"
    assert "del _TURN_LOCKS[" not in src, "a turn lock is deleted somewhere"
    assert "_TURN_LOCKS.clear()" not in src, "the turn lock registry is cleared somewhere"


async def _index_has(name: str, *, timeout: float) -> bool:
    """True once *name* is registered, False if it never arrives in time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if name in await asyncio.to_thread(routes._load_index):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_create_waits_for_the_directory_turn_lock(tmp_path, monkeypatch):
    """A create must not register while a delete holds the directory's lock.

    Delete removes its index entry and THEN scans for other names referencing the
    directory to decide whether to clear the ledger. A registration landing after
    that scan let the cleanup erase answers the newly adopted spec owned, so the
    two must not interleave.

    The wait is measured against a REAL timeout: create crosses several thread
    hops before reaching the lock, so a sleep(0) yield would prove nothing.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "serialized"
    client, state = _create_client(monkeypatch, tmp_path)
    routes._save_index({})
    dir_key = routes._decision_key(str(spec_dir))

    await client.start_server()
    try:
        client.app["state"] = state
        async with routes._turn_lock(dir_key):
            post = asyncio.ensure_future(
                client.post(
                    f"{_BASE}/specs",
                    json={
                        "name": "serialized",
                        "working_dir": str(project),
                        "spec_type": "feature",
                    },
                )
            )
            assert not await _index_has("serialized", timeout=1.5), (
                "the create registered while the directory's turn lock was held, so "
                "a delete's ledger cleanup could erase its answers"
            )
        # Released: the same request must now complete.
        assert await _index_has("serialized", timeout=10), "the create never registered"
        resp = await post
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()


def test_every_handler_that_starts_work_holds_the_turn_lock():
    """The invariant, widened after round 27 found the axis it was missing.

    Round 25 stated this as "every handler that mutates the index holds the lock",
    which is why a handler that ARMED A TIMER outside the lock was found by a
    reviewer instead of by this test: the timer dispatched on its own while the
    handler still waited for the lock. Causing work to start has three shapes, not
    one -- register/remove an index entry, arm a nudge loop, or dispatch a turn --
    and all three must be serialized on the directory.

    Enumerated rather than listed by name, so a handler added later is covered.
    """
    offenders: dict[str, list[str]] = {}
    checked: list[str] = []
    for attr in dir(routes):
        if not attr.startswith("_handle_"):
            continue
        fn = getattr(routes, attr)
        if not callable(fn):
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):  # pragma: no cover - not Python-defined
            continue
        effects = [e for e in _SERIALIZED_EFFECTS if e in src]
        if not effects:
            continue
        checked.append(attr)
        if "_turn_lock(" not in src:
            offenders[attr] = effects

    assert checked, "the guard found no work-starting handlers -- it has gone blind"
    assert not offenders, (
        f"these handlers start work without the directory turn lock: {offenders}. "
        "Registering an entry, arming a nudge timer and dispatching a turn are all "
        "ways to put an agent into a spec's files, so each needs serializing."
    )


def _effect_calls_outside_turn_lock(fn) -> list[str]:
    """Serialized effects in *fn* that are NOT lexically inside an `async with
    _turn_lock(...)` block.

    On the AST rather than on text, because "is the lock present in this function"
    is the question that let round 28 through: create held the lock around its index
    insert and dispatched its seed ~190 lines after the block closed. Containment is
    the property; presence is not.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))

    guarded: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            call = item.context_expr
            name = (
                call.func.id
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                else ""
            )
            if name == "_turn_lock":
                guarded.append(node)

    guarded_nodes = {id(inner) for block in guarded for inner in ast.walk(block)}
    outside = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if fname not in {
            "_mutate_index",
            "_save_index",
            "authorize_and_add_nudge",
            "_dispatch_turn",
        }:
            continue
        if id(node) not in guarded_nodes:
            outside.append(f"{fname}@line{node.lineno}")
    return outside


def test_every_serialized_effect_is_inside_the_lock_not_merely_near_it():
    """The invariant, made positional after round 28.

    Rounds 23-27 each fixed one handler that started work outside the directory turn
    lock, and each time the guard I wrote was weaker than the rule it stood for:
    first per-path, then presence-anywhere-in-the-handler. This checks CONTAINMENT on
    the AST, for every handler and every effect, which is the form that would have
    caught create dispatching its seed after the block closed.
    """
    offenders: dict[str, list[str]] = {}
    checked: list[str] = []
    for attr in dir(routes):
        if not attr.startswith("_handle_"):
            continue
        fn = getattr(routes, attr)
        if not callable(fn):
            continue
        try:
            inspect.getsource(fn)
        except (OSError, TypeError):  # pragma: no cover - not Python-defined
            continue
        outside = _effect_calls_outside_turn_lock(fn)
        src = inspect.getsource(fn)
        if not any(
            e in src
            for e in (
                "_mutate_index(",
                "_save_index(",
                "authorize_and_add_nudge(",
                "_dispatch_turn(",
            )
        ):
            continue
        checked.append(attr)
        if outside:
            offenders[attr] = outside

    assert checked, "the guard found no work-starting handlers -- it has gone blind"
    assert not offenders, (
        f"these handlers start work OUTSIDE the directory turn lock: {offenders}. "
        "Registering an entry, arming a nudge timer and dispatching a turn must each "
        "happen inside the block, not merely somewhere in the same function."
    )


@pytest.mark.asyncio
async def test_create_dispatches_its_seed_before_anything_else_can_run(tmp_path, monkeypatch):
    """A registered spec whose seed has not been dispatched must accept nothing else.

    The lock used to close at the index insert, so a list poll could expose the spec
    while create was still awaiting slot setup; a concurrent message then took the
    lock and started the FIRST turn, leaving the seed queued second and the persisted
    conversation beginning with something other than the prompt that defines the spec.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "seeded"
    client, state = _create_client(monkeypatch, tmp_path)
    routes._save_index({})
    dir_key = routes._decision_key(str(spec_dir))

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    await client.start_server()
    try:
        client.app["state"] = state
        async with routes._turn_lock(dir_key):
            post = asyncio.ensure_future(
                client.post(
                    f"{_BASE}/specs",
                    json={"name": "seeded", "working_dir": str(project), "spec_type": "feature"},
                )
            )
            await asyncio.sleep(1.0)
            assert dispatched == [], (
                "create dispatched its seed while the directory's turn lock was held, "
                "so a concurrent turn could have gone first"
            )
        resp = await post
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()

    assert dispatched, "the seed was never dispatched"


def test_discovery_is_exempt_only_because_the_tombstone_covers_it():
    """The one adopter outside the lock family, and why that is sound.

    Discovery adopts directories found on disk and cannot take a per-directory lock
    for an unbounded sweep. It is safe because a delete leaves a tombstone that
    discovery consults, so it cannot adopt a directory mid-teardown. If that
    consultation ever disappears, discovery needs the lock instead -- and this test
    is what says so.
    """
    src = inspect.getsource(routes._discover_folder_specs)
    assert "_deleted" in src or "tombstone" in src.lower(), (
        "discovery no longer consults the delete tombstone, so it can adopt a "
        "directory whose teardown is still running -- it now needs the turn lock"
    )


async def _answer(client, state, *, spec_dir, slot_key, option, decision_id="transport"):
    """POST a decision answer, returning (status, payload)."""
    resp = await client.post(
        f"{_BASE}/specs/s/message",
        json={
            "spec_dir": spec_dir,
            "slot_key": slot_key,
            "decision_id": decision_id,
            "decision_option": option,
            "text": f"Decision - {decision_id}: {option}",
        },
    )
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_an_option_the_decision_no_longer_offers_is_refused(tmp_path, monkeypatch):
    """A card is a snapshot; the decision behind it can be re-emitted.

    Same id, revised options: a tab still showing the old render posts an option the
    replacement never offered. The claim would record it and the overlay would lock
    the card -- permanently -- to a choice the user cannot see and the agent cannot
    honour. Nothing downstream can undo that, because the ledger is never rewritten.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "transport", "title": "Inbound", "options": ["gRPC", "AMQP"]}]},
    )
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        # "HTTPS" was on the card this client rendered; the decision now offers neither.
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="HTTPS"
        )
    finally:
        await client.close()

    assert status == 409, f"{status}: {payload}"
    assert payload["code"] == "decision_option_not_offered", payload
    assert dispatched == [], "the stale option reached the agent"
    assert await _recorded_answers(spec_dir) == {}, (
        "the stale option was recorded, so the card is now locked to a choice the "
        "decision does not offer"
    )


@pytest.mark.asyncio
async def test_a_decision_replaced_while_the_answer_waits_for_the_lock_is_refused(
    tmp_path, monkeypatch
):
    """Validation must describe state after the preceding serialized turn.

    A request can parse a still-visible card while another turn owns the directory
    lock. If that turn replaces the question before releasing the lock, the waiting
    request must validate again inside the lock rather than deliver its stale answer.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    held = routes._turn_lock(spec_dir)
    await held.acquire()
    lock_attempted = asyncio.Event()

    class _ProbedLock:
        async def __aenter__(self):
            lock_attempted.set()
            await held.acquire()
            return held

        async def __aexit__(self, *_exc):
            held.release()

    monkeypatch.setattr(routes, "_turn_lock", lambda _key: _ProbedLock())

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        post = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/message",
                json={
                    "spec_dir": spec_dir,
                    "slot_key": slot_key,
                    "decision_id": "transport",
                    "decision_option": "HTTPS",
                    "text": "Decision - transport: HTTPS",
                },
            )
        )
        await lock_attempted.wait()
        Path(spec_dir, ".spec-state.json").write_text(
            json.dumps(
                {
                    "decisions": [
                        {
                            "id": "storage",
                            "title": "Storage engine",
                            "options": ["SQLite", "PostgreSQL"],
                        }
                    ]
                }
            )
        )
        held.release()
        resp = await post
        payload = await resp.json()
    finally:
        if held.locked():
            held.release()
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_not_found", payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_an_option_the_decision_still_offers_is_accepted(tmp_path, monkeypatch):
    """The boundary: this must not refuse the ordinary answer.

    Without this, a fix that refused everything would still pass the test above.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={
            "decisions": [{"id": "transport", "title": "Inbound", "options": ["HTTPS", "gRPC"]}]
        },
    )
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="HTTPS"
        )
    finally:
        await client.close()

    assert status == 200, f"{status}: {payload}"
    assert await _recorded_answers(spec_dir) == {
        "transport": "HTTPS"
    }, "a legitimate answer was not recorded"
    assert dispatched, "a legitimate answer never reached the agent"


@pytest.mark.asyncio
async def test_a_decision_with_no_declared_options_accepts_any_answer(tmp_path, monkeypatch):
    """Carve-out one, and it is structural: nothing to violate.

    A decision that declares no options constrains nothing, so there is no offer an
    answer can fall outside of. Refusing here would break free-form decisions.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path, state={"decisions": [{"id": "transport", "title": "Inbound"}]}
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: None)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="anything at all"
        )
    finally:
        await client.close()

    assert status == 200, f"{status}: {payload}"


@pytest.mark.asyncio
async def test_a_decision_absent_from_state_is_refused(tmp_path, monkeypatch):
    """A stale card cannot claim an id after the current question disappeared."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"decisions": []})
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: None)

    state, _slot = _slot_stub()
    await client.start_server()
    try:
        client.app["state"] = state
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="HTTPS"
        )
    finally:
        await client.close()

    assert status == 409, f"{status}: {payload}"
    assert payload.get("code") == "decision_not_found", payload
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_two_spellings_of_one_directory_share_the_turn_lock(monkeypatch):
    """A raw path and a normalized key must resolve to the SAME lock object.

    Round 28 made the ledger key lexical via `normcase`, which lowercases on Windows.
    Call sites passing a raw path then hashed to a different dictionary entry than
    those passing `_decision_key(...)` -- two locks for one directory, so two turns
    could run on the same documents. That is the exact hole the directory-keyed lock
    exists to close, reintroduced by the fix for a different one.

    `normcase` is the identity on Linux, so this simulates Windows explicitly. Without
    the patch the assertion holds trivially and proves nothing.
    """
    monkeypatch.setattr(routes.os.path, "normcase", lambda p: p.lower())
    routes._TURN_LOCKS.clear()

    raw = "/Some/Spec/Dir"
    lock_from_raw = routes._turn_lock(raw)
    lock_from_key = routes._turn_lock(routes._decision_key(raw))
    lock_from_other_case = routes._turn_lock("/SOME/spec/DIR")

    assert lock_from_raw is lock_from_key, (
        "a raw path and its normalized key produced different locks, so two handlers "
        "would serialize on different objects for one directory"
    )
    assert (
        lock_from_raw is lock_from_other_case
    ), "two case spellings of one directory produced different locks"
    assert (
        len(routes._TURN_LOCKS) == 1
    ), f"one directory minted {len(routes._TURN_LOCKS)} lock entries"


@pytest.mark.asyncio
async def test_macos_case_variants_share_the_turn_lock(monkeypatch):
    """Darwin preserves path case even when its volume does not distinguish it."""
    monkeypatch.setattr(routes, "_CASE_FOLD_TURN_KEYS", True)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path)
    routes._TURN_LOCKS.clear()

    first = routes._turn_lock("/Some/Spec/CaseSpec")
    second = routes._turn_lock("/Some/Spec/casespec")

    assert first is second
    assert len(routes._TURN_LOCKS) == 1


@pytest.mark.asyncio
async def test_create_refuses_a_second_name_for_the_same_normalized_directory(
    tmp_path, monkeypatch
):
    """Create arbitration uses the directory identity, not only the label.

    Windows treats case variants as one path. Without a directory collision check,
    two names could each receive a slot and dispatch an agent into the same files.
    Patching ``normcase`` makes that filesystem identity deterministic on every host.
    """
    project = tmp_path / "proj"
    project.mkdir()
    client, state = _create_client(monkeypatch, tmp_path)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path.lower())
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    await client.start_server()
    try:
        client.app["state"] = state
        first = await client.post(
            f"{_BASE}/specs",
            json={"name": "CaseSpec", "working_dir": str(project), "spec_type": "feature"},
        )
        second = await client.post(
            f"{_BASE}/specs",
            json={"name": "casespec", "working_dir": str(project), "spec_type": "feature"},
        )
        first_body = await first.json()
        second_body = await second.json()
    finally:
        await client.close()

    assert first.status == 201, first_body
    assert second.status == 409, second_body
    assert second_body["code"] == "spec_dir_in_use"
    assert set(routes._load_index()) == {"CaseSpec"}
    assert dispatched == ["x"], "both names dispatched agents into one directory"


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "a case-only spelling is not a simulable alias on Windows: the second "
        "create's leaf already exists, so Path.resolve() canonicalizes that "
        "spelling back onto the first spec directory and the two creates never "
        "hold the distinct resolved paths this scenario is about"
    ),
)
async def test_create_refuses_a_macos_case_alias_for_the_same_directory(tmp_path, monkeypatch):
    """Create asks the filesystem whether differently cased paths are aliases."""
    project = tmp_path / "proj"
    project.mkdir()
    client, state = _create_client(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_CASE_FOLD_TURN_KEYS", True)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path)
    monkeypatch.setattr(
        routes.os.path,
        "samefile",
        lambda left, right: os.path.normpath(left).casefold() == os.path.normpath(right).casefold(),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    await client.start_server()
    try:
        client.app["state"] = state
        responses = await asyncio.gather(
            client.post(
                f"{_BASE}/specs",
                json={
                    "name": "CaseSpec",
                    "working_dir": str(project),
                    "spec_type": "feature",
                },
            ),
            client.post(
                f"{_BASE}/specs",
                json={
                    "name": "casespec",
                    "working_dir": str(project),
                    "spec_type": "feature",
                },
            ),
        )
        bodies = [await response.json() for response in responses]
    finally:
        await client.close()

    statuses = [response.status for response in responses]
    assert sorted(statuses) == [201, 409], list(zip(statuses, bodies))
    refusal = bodies[statuses.index(409)]
    # The loser's refusal code depends on the filesystem, not on the race: the
    # casefolded turn lock serializes both creates, and when the two spellings
    # survive as distinct resolved paths (case-sensitive volumes) the loser hits
    # the ``samefile`` alias probe and is refused as
    # ``decision_directory_alias_conflict``. When the volume collapses the second
    # spelling onto the winner's directory, the resolved keys become lexically
    # equal, the alias scan skips them, and the lexical duplicate check refuses as
    # ``spec_dir_in_use``. Both refusals satisfy the invariant this test locks in —
    # exactly one create succeeds and both spellings map to one directory — so
    # either code is a correct outcome. Windows produced NEITHER (it refused on a
    # containment arm instead), which is why the shard is skipped rather than
    # having a third code added here; see the skipif above.
    assert refusal["code"] in {"spec_dir_in_use", "decision_directory_alias_conflict"}
    assert len(routes._load_index()) == 1
    assert dispatched == ["x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("import_existing", [False, True])
async def test_create_refuses_an_equivalent_orphan_ledger_before_inserting(
    tmp_path, monkeypatch, import_existing
):
    """A protected record under another spelling cannot poison a new create."""
    client, state = _create_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    # Use two lexically distinct sibling paths and make the filesystem report them
    # as aliases. A case-only spelling is not a valid simulation on Windows:
    # ``Path.resolve()`` canonicalizes ``S`` back to the existing ``s`` directory,
    # so both spellings have the same ledger key and there is no alias to reject.
    monkeypatch.setattr(routes.os.path, "samefile", lambda _left, _right: True)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED
    routes._save_index({})
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    project = Path(spec_dir).parents[2]
    await client.start_server()
    try:
        client.app["state"] = state
        response = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "alias",
                "working_dir": str(project),
                "spec_type": "feature",
                "import_existing": import_existing,
            },
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 409, body
    assert body["code"] == "decision_directory_alias_conflict"
    assert routes._load_index() == {}
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
@pytest.mark.parametrize("import_existing", [False, True])
async def test_create_refuses_an_unreadable_ledger_before_inserting(
    tmp_path, monkeypatch, import_existing
):
    """A transient ledger read failure cannot admit a potentially poisoned alias."""
    client, state = _create_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED
    routes._save_index({})
    monkeypatch.setattr(routes, "_read_decisions", lambda: ({}, False))
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    project = Path(spec_dir).parents[2]
    await client.start_server()
    try:
        client.app["state"] = state
        response = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "S",
                "working_dir": str(project),
                "spec_type": "feature",
                "import_existing": import_existing,
            },
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 503, body
    assert body["code"] == "decision_record_unreadable"
    assert routes._load_index() == {}
    assert dispatched == []
