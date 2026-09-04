"""Tests for the user-configurable denied-commands REST API.

Covers ``dashboard/handlers/security.py`` (the 6 CRUD endpoints + snapshot
helpers) and the ``core.api_security_stats`` re-source. Every endpoint returns
the full refreshed snapshot; mutations write ``config.json`` under
``hooks.denied_commands`` and emit a SEL audit entry (``ok`` on success,
``denied`` on reject).

The aiohttp handlers are exercised through an in-test ``TestClient`` opened with
``async with`` (matching ``test_api_kiro_hooks.py``) rather than an async-gen
fixture: the CI-pinned ``pytest-asyncio==0.20.3`` is incompatible with the
pinned ``pytest==8.4.1`` for async fixtures (its wrapper reads the
``fixturedef.unittest`` attribute removed in pytest 8.1), so the whole suite
avoids ``@pytest_asyncio.fixture`` by convention.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.security import (
    api_denied_command_builtin_toggle,
    api_denied_command_user_add,
    api_denied_command_user_delete,
    api_denied_command_user_toggle,
    api_denied_commands_disable_all,
    api_denied_commands_list,
    build_denied_commands_snapshot,
    count_effective_denied_commands,
)
from kiro_crew.security import BUILTIN_DENIED_RULES

# Number of built-in rules (default-on). Derived so catalog additions don't
# require editing every count assertion below.
_CATALOG_N = len(BUILTIN_DENIED_RULES)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect KIROCREW_HOME so the keystone file writes land in a tmp dir."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def config_file(home: Path) -> Path:
    """The keystone denied_commands.json (the opt-out store).

    Named ``config_file`` for historical continuity, but the opt-out state now
    lives in its own keystone file — its root IS the opt-out object.
    """
    return home / "denied_commands.json"


def _seed(config_file: Path, denied: dict) -> None:
    """Write the opt-out object as the keystone file root (test seed)."""
    config_file.write_text(json.dumps(denied), encoding="utf-8")


@pytest.fixture
def mock_sel():
    """Patch the late-bound ``_sel()`` so SEL audit calls are observable."""
    with patch("kiro_crew.dashboard.handlers.security._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/security/denied-commands", api_denied_commands_list)
    app.router.add_patch(
        "/api/security/denied-commands/disable-all", api_denied_commands_disable_all
    )
    app.router.add_patch(
        "/api/security/denied-commands/builtins/{id}", api_denied_command_builtin_toggle
    )
    app.router.add_post("/api/security/denied-commands/user", api_denied_command_user_add)
    app.router.add_patch("/api/security/denied-commands/user/{id}", api_denied_command_user_toggle)
    app.router.add_delete("/api/security/denied-commands/user/{id}", api_denied_command_user_delete)
    return app


def _client() -> TestClient:
    """Build an aiohttp TestClient for the denied-commands app.

    Open it with ``async with`` inside each test — an async-gen fixture would
    trip the CI-pinned pytest-asyncio (see the module docstring).
    """
    return TestClient(TestServer(_make_app()))


def _read_hooks(config_file: Path) -> dict:
    """Read the persisted opt-out object (the keystone file root)."""
    return json.loads(config_file.read_text(encoding="utf-8"))


def _a_builtin_id() -> str:
    """A freely-toggleable builtin id (never a floor-enforced one)."""
    from kiro_crew.security import builtin_denied_rules, floor_enforced_builtin_command_ids

    floor = floor_enforced_builtin_command_ids()
    return next(r["id"] for r in builtin_denied_rules() if r["id"] not in floor)


def _a_floor_id() -> str:
    """A floor-enforced (always-on, non-opt-out-able) builtin id."""
    from kiro_crew.security import floor_enforced_builtin_command_ids

    return sorted(floor_enforced_builtin_command_ids())[0]


def _floor_n() -> int:
    from kiro_crew.security import floor_enforced_builtin_command_ids

    return len(floor_enforced_builtin_command_ids())


# ── snapshot helpers ──


def test_snapshot_shape_and_defaults(home: Path):
    snap = build_denied_commands_snapshot()
    assert set(snap.keys()) == {
        "builtins",
        "user_added",
        "disable_all",
        "effective_count",
        "governance_locked",
    }
    assert snap["disable_all"] is False
    assert snap["governance_locked"] is False
    assert len(snap["builtins"]) == _CATALOG_N
    b = snap["builtins"][0]
    assert set(b.keys()) == {
        "id",
        "pattern",
        "category",
        "description",
        "enabled",
        "pinned",
        "lock_reason",
        # Discriminates a shipped rule from one contributed through the
        # ``denied_rules`` seam; see test_denied_rule_seam.py.
        "source",
    }
    assert b["enabled"] is True
    assert b["pinned"] is False
    assert b["lock_reason"] is None
    assert b["source"] == "builtin"
    assert snap["effective_count"] == _CATALOG_N


def test_count_effective_matches_snapshot(home: Path):
    assert count_effective_denied_commands() == _CATALOG_N


def test_disabled_id_lowers_effective_count(home: Path, config_file: Path):
    rid = _a_builtin_id()
    _seed(config_file, {"disabled_ids": [rid]})
    snap = build_denied_commands_snapshot()
    assert snap["effective_count"] == _CATALOG_N - 1
    disabled = [b for b in snap["builtins"] if b["id"] == rid]
    assert disabled and disabled[0]["enabled"] is False


def test_disable_all_zeroes_builtins_except_floor(home: Path, config_file: Path):
    # disable_all turns off every toggleable builtin, but the floor-enforced
    # git-publish rules stay forced-on: their always-on floor consults no
    # opt-out state, so rendering them off would be the no-op lie this
    # surface exists to avoid.
    _seed(config_file, {"disable_all": True})
    snap = build_denied_commands_snapshot()
    assert snap["disable_all"] is True
    for b in snap["builtins"]:
        assert b["enabled"] is (b["lock_reason"] == "floor")
    assert snap["effective_count"] == _floor_n()


def test_pin_forces_enabled_under_disable_all(home: Path, config_file: Path):
    rid = _a_builtin_id()
    _seed(config_file, {"disable_all": True})
    with patch("kiro_crew.security.pinned_builtin_command_ids", return_value={rid}):
        snap = build_denied_commands_snapshot()
    assert snap["governance_locked"] is True
    pinned = [b for b in snap["builtins"] if b["id"] == rid]
    assert pinned and pinned[0]["enabled"] is True and pinned[0]["pinned"] is True
    assert pinned[0]["lock_reason"] == "policy"
    assert snap["effective_count"] == 1 + _floor_n()


def test_corrupt_config_tolerated_for_snapshot(home: Path, config_file: Path):
    config_file.write_text("{not json", encoding="utf-8")
    snap = build_denied_commands_snapshot()
    assert snap["effective_count"] == _CATALOG_N


def test_snapshot_tolerates_unhashable_disabled_id(home: Path, config_file: Path):
    # A hand-edited config with a non-string disabled_ids entry (e.g. {}) must
    # NOT raise TypeError: unhashable type when the snapshot builds set(...).
    # It is filtered out, leaving all built-ins enabled.
    _seed(config_file, {"disabled_ids": [{}, 5, "", "real-id"]})
    snap = build_denied_commands_snapshot()
    # Only the one real (unknown) string id is retained; it matches no built-in,
    # so every built-in stays enabled.
    assert snap["effective_count"] == _CATALOG_N


def test_snapshot_disable_all_string_false_is_not_truthy(home: Path, config_file: Path):
    # '"disable_all": "false"' must not read as truthy and zero the built-ins.
    _seed(config_file, {"disable_all": "false"})
    snap = build_denied_commands_snapshot()
    assert snap["disable_all"] is False
    assert snap["effective_count"] == _CATALOG_N


def test_snapshot_marks_floor_rules_locked_and_forced_on(home: Path, config_file: Path):
    # Only the git-publish rules whose coverage is the UNGATED anti-obfuscation
    # branch are floor-enforced: forced enabled and lock-flagged, even when the id
    # was persisted into disabled_ids by an older build. The rest of the category
    # is gated on the per-rule enable state and renders freely toggleable.
    from kiro_crew.security import floor_enforced_builtin_command_ids

    rid = _a_floor_id()
    _seed(config_file, {"disabled_ids": [rid]})
    snap = build_denied_commands_snapshot()
    floor_ids = floor_enforced_builtin_command_ids()
    floor_rules = [b for b in snap["builtins"] if b["id"] in floor_ids]
    assert len(floor_rules) == _floor_n() > 0
    for b in floor_rules:
        assert b["enabled"] is True
        assert b["lock_reason"] == "floor"
        # `pinned` keeps its governance-only meaning; no governance here.
        assert b["pinned"] is False
    # Floor lock does NOT masquerade as governance at the panel level.
    assert snap["governance_locked"] is False
    assert snap["effective_count"] == _CATALOG_N


def test_floor_lock_reason_wins_over_policy(home: Path):
    # A rule that is BOTH governance-pinned and floor-enforced reports "floor":
    # the floor holds even if the pin were removed, so it is the stronger reason.
    rid = _a_floor_id()
    with patch("kiro_crew.security.pinned_builtin_command_ids", return_value={rid}):
        snap = build_denied_commands_snapshot()
    row = next(b for b in snap["builtins"] if b["id"] == rid)
    assert row["pinned"] is True
    assert row["lock_reason"] == "floor"


def test_floor_ids_are_derived_from_the_category():
    # Guard: the accessor reports ONLY the rules whose coverage is an ungated
    # branch of the floor. The rest of the git-publish category is now gated on
    # the per-rule enable state, so reporting the whole category would lock rows
    # whose toggle actually works — the inverse of the no-op this accessor exists
    # to prevent. Brace expansion is the ungated one: it is caught by
    # ``_AMBIGUOUS_EXPANSION_RE`` inside the unverifiable-glue check, which no
    # opt-out may reach.
    from kiro_crew.security import (
        BUILTIN_DENIED_RULES,
        floor_enforced_builtin_command_ids,
    )

    floor = floor_enforced_builtin_command_ids()
    assert floor == {"git-publish-push-brace-expansion-refspec"}
    git_publish = {r.id for r in BUILTIN_DENIED_RULES if r.category == "git-publish"}
    assert floor < git_publish, "floor ids must be a strict subset of the category"


# ── GET ──


@pytest.mark.asyncio
async def test_get_returns_snapshot(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.get("/api/security/denied-commands")
        assert resp.status == 200
        body = await resp.json()
        assert len(body["builtins"]) == _CATALOG_N
        assert body["effective_count"] == _CATALOG_N
        # reads do not audit
        mock_sel.log_api_access.assert_not_called()


# ── builtin toggle ──


@pytest.mark.asyncio
async def test_builtin_toggle_disable(config_file: Path, mock_sel):
    rid = _a_builtin_id()
    async with _client() as client:
        resp = await client.patch(
            f"/api/security/denied-commands/builtins/{rid}", json={"enabled": False}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["effective_count"] == _CATALOG_N - 1
    assert rid in _read_hooks(config_file)["disabled_ids"]
    args = mock_sel.log_api_access.call_args.kwargs
    assert args["outcome"] == "ok"


@pytest.mark.asyncio
async def test_builtin_toggle_reenable_removes_id(config_file: Path, mock_sel):
    rid = _a_builtin_id()
    _seed(config_file, {"disabled_ids": [rid]})
    async with _client() as client:
        resp = await client.patch(
            f"/api/security/denied-commands/builtins/{rid}", json={"enabled": True}
        )
        assert resp.status == 200
    assert rid not in _read_hooks(config_file).get("disabled_ids", [])


@pytest.mark.asyncio
async def test_builtin_toggle_bad_body(home: Path, mock_sel):
    rid = _a_builtin_id()
    async with _client() as client:
        resp = await client.patch(
            f"/api/security/denied-commands/builtins/{rid}", json={"enabled": "nope"}
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_builtin_toggle_unknown_id(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.patch(
            "/api/security/denied-commands/builtins/does-not-exist", json={"enabled": False}
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_builtin_toggle_disable_pinned_is_409(home: Path, mock_sel):
    rid = _a_builtin_id()
    with patch(
        "kiro_crew.security.pinned_builtin_command_ids",
        return_value={rid},
    ):
        async with _client() as client:
            resp = await client.patch(
                f"/api/security/denied-commands/builtins/{rid}", json={"enabled": False}
            )
    assert resp.status == 409
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_builtin_toggle_enable_pinned_is_200_noop(config_file: Path, mock_sel):
    rid = _a_builtin_id()
    with patch(
        "kiro_crew.security.pinned_builtin_command_ids",
        return_value={rid},
    ):
        async with _client() as client:
            resp = await client.patch(
                f"/api/security/denied-commands/builtins/{rid}", json={"enabled": True}
            )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_builtin_toggle_disable_floor_is_409(config_file: Path, mock_sel):
    rid = _a_floor_id()
    async with _client() as client:
        resp = await client.patch(
            f"/api/security/denied-commands/builtins/{rid}", json={"enabled": False}
        )
        assert resp.status == 409
        body = await resp.json()
    assert body["code"] == "floor_enforced"
    # Nothing persisted: the keystone file must not record the no-op opt-out.
    assert not config_file.exists()
    args = mock_sel.log_api_access.call_args.kwargs
    assert args["outcome"] == "denied"
    assert args["resources"] == f"{rid}=floor_enforced"


@pytest.mark.asyncio
async def test_builtin_toggle_enable_floor_is_200_noop(config_file: Path, mock_sel):
    rid = _a_floor_id()
    _seed(config_file, {"disabled_ids": [rid]})
    async with _client() as client:
        resp = await client.patch(
            f"/api/security/denied-commands/builtins/{rid}", json={"enabled": True}
        )
    assert resp.status == 200
    # Re-enable clears a stale persisted id (state from before the 409 existed).
    assert rid not in _read_hooks(config_file).get("disabled_ids", [])


# ── disable-all ──


@pytest.mark.asyncio
async def test_disable_all_sets_flag(config_file: Path, mock_sel):
    async with _client() as client:
        resp = await client.patch("/api/security/denied-commands/disable-all", json={"value": True})
        assert resp.status == 200
        body = await resp.json()
        assert body["disable_all"] is True
    assert _read_hooks(config_file)["disable_all"] is True
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "ok"


@pytest.mark.asyncio
async def test_disable_all_bad_body(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.patch("/api/security/denied-commands/disable-all", json={"value": 1})
        assert resp.status == 400


# ── user add ──


@pytest.mark.asyncio
async def test_user_add_happy(config_file: Path, mock_sel):
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user", json={"pattern": "rm -rf /tmp/mine"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert len(body["user_added"]) == 1
        added = body["user_added"][0]
        assert added["pattern"] == "rm -rf /tmp/mine"
        assert added["enabled"] is True
        assert added["id"].startswith("user-")
        # An add with no ``note`` key is the pre-existing shape and stays legal —
        # it reports an empty note rather than omitting the field.
        assert added["note"] == ""
        assert body["effective_count"] == _CATALOG_N + 1
    persisted = _read_hooks(config_file)["user_added"]
    assert persisted[0]["id"] == added["id"]
    assert persisted[0]["note"] == ""


@pytest.mark.asyncio
async def test_user_add_with_note_persists_and_surfaces_in_snapshot(config_file: Path, mock_sel):
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user",
            json={"pattern": "frobnicate.*", "note": "use --dry-run instead"},
        )
        assert resp.status == 200
        added = (await resp.json())["user_added"][0]
    # The snapshot is an allowlist REBUILD, not a passthrough — a key missing
    # from it is stored on disk but invisible to the Settings row.
    assert set(added.keys()) == {"id", "pattern", "enabled", "note"}
    assert added["note"] == "use --dry-run instead"
    persisted = _read_hooks(config_file)["user_added"][0]
    assert persisted == {
        "id": added["id"],
        "pattern": "frobnicate.*",
        "enabled": True,
        "note": "use --dry-run instead",
    }
    # A fresh snapshot read back from disk carries the note too.
    reread = build_denied_commands_snapshot()["user_added"][0]
    assert reread["note"] == "use --dry-run instead"
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "ok"


@pytest.mark.asyncio
async def test_user_add_non_string_note_is_400(home: Path, mock_sel):
    # Mirrors how ``pattern`` is handled: a present-but-wrong-typed note is a
    # reject, not a silent drop of input the caller believes it supplied.
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user", json={"pattern": "danger", "note": 42}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "note must be a string"
    kwargs = mock_sel.log_api_access.call_args.kwargs
    assert kwargs["outcome"] == "denied"
    assert kwargs["resources"] == "note_bad_type"


@pytest.mark.asyncio
async def test_user_add_oversize_note_is_400(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user", json={"pattern": "danger", "note": "n" * 201}
        )
        assert resp.status == 400
    kwargs = mock_sel.log_api_access.call_args.kwargs
    assert kwargs["outcome"] == "denied"
    assert kwargs["resources"] == "note_oversize"


@pytest.mark.asyncio
async def test_user_add_note_at_max_length_is_accepted(config_file: Path, mock_sel):
    # The cap boundary itself, so the reject above is proven to be off-by-none.
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user", json={"pattern": "danger", "note": "n" * 200}
        )
        assert resp.status == 200
        assert (await resp.json())["user_added"][0]["note"] == "n" * 200


@pytest.mark.asyncio
async def test_user_add_note_forging_the_reason_prefix_is_400(home: Path, mock_sel):
    # Pasting the refusal you just saw into the note field is a NATURAL thing to
    # do, and it would make RecoveryCard.tsx -- which parses refusals with a
    # global per-line regex -- report a second, fabricated deny pattern. Reject
    # with a real error rather than silently mangling the operator's text.
    from kiro_crew.security import DENY_REASON_PREFIX

    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user",
            json={"pattern": "danger", "note": f"{DENY_REASON_PREFIX}rm -rf /"},
        )
        assert resp.status == 400
        assert "must not contain" in (await resp.json())["error"]
    kwargs = mock_sel.log_api_access.call_args.kwargs
    assert kwargs["outcome"] == "denied"
    assert kwargs["resources"] == "note_forges_reason"
    # Nothing was written: the reject happens before the mutation.
    assert not (home / "denied_commands.json").exists()


@pytest.mark.asyncio
async def test_user_add_note_forging_without_a_space_after_the_colon_is_400(
    home: Path, mock_sel
):
    # RecoveryCard's regex is `Blocked by security policy:\s*`, so the space is
    # OPTIONAL -- this parses as a refusal line while NOT containing the emitted
    # prefix. Guarding on the emitted form (trailing space) left this bypass open.
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user",
            json={"pattern": "danger", "note": "Blocked by security policy:not-a-rule"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "note_forges_reason"
    assert not (home / "denied_commands.json").exists()


@pytest.mark.asyncio
async def test_user_add_note_whitespace_is_collapsed_to_one_line(config_file: Path, mock_sel):
    # Newlines would forge extra lines in the refusal, whose FIRST line is a
    # parsed contract — so they are collapsed rather than rejected.
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user",
            json={"pattern": "danger", "note": "  first\nsecond\t\tthird   spaced  "},
        )
        assert resp.status == 200
        note = (await resp.json())["user_added"][0]["note"]
    assert note == "first second third spaced"
    assert "\n" not in note
    assert "  " not in note
    assert _read_hooks(config_file)["user_added"][0]["note"] == note


@pytest.mark.asyncio
async def test_user_add_whitespace_only_note_is_empty_not_400(config_file: Path, mock_sel):
    # Collapsing "   " yields "" — the same shape as no note at all, so the add
    # succeeds and the rule is simply un-annotated.
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user", json={"pattern": "danger", "note": " \n\t "}
        )
        assert resp.status == 200
        assert (await resp.json())["user_added"][0]["note"] == ""


@pytest.mark.asyncio
async def test_user_add_note_rejected_before_any_write(config_file: Path, mock_sel):
    # A rejected note must not leave a half-added rule behind.
    async with _client() as client:
        resp = await client.post(
            "/api/security/denied-commands/user", json={"pattern": "danger", "note": ["nope"]}
        )
        assert resp.status == 400
    assert not config_file.exists() or _read_hooks(config_file).get("user_added", []) == []


@pytest.mark.asyncio
async def test_user_add_empty_is_400(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.post("/api/security/denied-commands/user", json={"pattern": ""})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_user_add_oversize_is_400(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.post("/api/security/denied-commands/user", json={"pattern": "x" * 513})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_user_add_bad_regex_is_400(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.post("/api/security/denied-commands/user", json={"pattern": "("})
        assert resp.status == 400
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_user_add_non_string_is_400(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.post("/api/security/denied-commands/user", json={"pattern": 42})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_non_object_json_body_is_400_not_500(home: Path, mock_sel):
    # A valid-but-non-object JSON body (e.g. a list) must yield a clean 400 on
    # every mutation, not a 500 from calling .get() on a non-dict.
    async with _client() as client:
        for method, path, payload in (
            ("post", "/api/security/denied-commands/user", []),
            ("patch", "/api/security/denied-commands/disable-all", "nope"),
            ("patch", "/api/security/denied-commands/builtins/x", [1, 2]),
            ("patch", "/api/security/denied-commands/user/x", []),
        ):
            resp = await getattr(client, method)(path, json=payload)
            assert resp.status == 400, f"{method} {path} → {resp.status}"


@pytest.mark.asyncio
async def test_user_add_redos_pattern_is_400(home: Path, mock_sel):
    # A catastrophic-backtracking regex must be rejected at add-time — it would
    # otherwise freeze the event loop when the gate runs it synchronously.
    from kiro_crew.security import _DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN

    async with _client() as client:
        resp = await client.post("/api/security/denied-commands/user", json={"pattern": "(a+)+$"})
        assert resp.status == 400
        body = await resp.json()
        assert "unsafe" in body["error"].lower()
        # No flag-run fragment in the pattern, so no fragment hint either — the
        # hint must name the actual trigger, not decorate every rejection.
        assert _DANGEROUS_AWS_FLAG_RUN not in body["error"]
        assert _LINEARIZED_AWS_FLAG_RUN not in body["error"]
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_user_add_wrapped_builtin_fragment_rejection_names_the_trigger(
    home: Path, mock_sel
):
    # A user who copies a built-in pattern and tweaks it embeds the flag-run
    # fragment verbatim; the fragment is exempt from the backtracking check only
    # as part of a complete built-in, so the tweaked copy is rejected. The
    # rejection must name the fragment so the dead end is self-explanatory
    # instead of a generic "unsafe regex" (#5837).
    from kiro_crew.security import _DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN

    async with _client() as client:
        for fragment in (_DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN):
            pattern = "aws s3" + fragment + r" rm .*my-bucket"
            resp = await client.post(
                "/api/security/denied-commands/user", json={"pattern": pattern}
            )
            assert resp.status == 400
            body = await resp.json()
            assert "unsafe" in body["error"].lower()
            assert fragment in body["error"], body["error"]
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_user_add_fragment_hint_withheld_when_not_the_trigger(home: Path, mock_sel):
    # The hint says "remove or rewrite that fragment" — advice that must be
    # TRUE before it is given. A pattern that embeds the fragment but also
    # carries its own catastrophic quantifier stays rejected after the fragment
    # is removed, so hinting at the fragment would send the user to an
    # identical 400. The hint is gated on the fragment-scrubbed residue
    # actually passing (#5837).
    from kiro_crew.security import _DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN

    async with _client() as client:
        for pattern in (
            "(x+)+" + _DANGEROUS_AWS_FLAG_RUN,  # own nested quantifier
            "a|b" + _DANGEROUS_AWS_FLAG_RUN,  # own top-level alternation
        ):
            resp = await client.post(
                "/api/security/denied-commands/user", json={"pattern": pattern}
            )
            assert resp.status == 400
            body = await resp.json()
            assert "unsafe" in body["error"].lower()
            assert _DANGEROUS_AWS_FLAG_RUN not in body["error"], body["error"]
            assert _LINEARIZED_AWS_FLAG_RUN not in body["error"], body["error"]
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


# ── user toggle / delete ──


@pytest.mark.asyncio
async def test_user_toggle(config_file: Path, mock_sel):
    async with _client() as client:
        add = await client.post("/api/security/denied-commands/user", json={"pattern": "danger"})
        rid = (await add.json())["user_added"][0]["id"]
        resp = await client.patch(
            f"/api/security/denied-commands/user/{rid}", json={"enabled": False}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["user_added"][0]["enabled"] is False
        assert body["effective_count"] == _CATALOG_N  # disabled user rule not counted


@pytest.mark.asyncio
async def test_user_toggle_preserves_note(config_file: Path, mock_sel):
    # There is no edit endpoint for a note (create-only, mirroring ``pattern``),
    # so a toggle must carry it through — losing it here would silently strip the
    # operator's remediation text on the first disable/re-enable.
    async with _client() as client:
        add = await client.post(
            "/api/security/denied-commands/user",
            json={"pattern": "danger", "note": "use the safe wrapper"},
        )
        rid = (await add.json())["user_added"][0]["id"]
        off = await client.patch(
            f"/api/security/denied-commands/user/{rid}", json={"enabled": False}
        )
        assert off.status == 200
        disabled = (await off.json())["user_added"][0]
        assert disabled["enabled"] is False
        assert disabled["note"] == "use the safe wrapper"
        assert _read_hooks(config_file)["user_added"][0]["note"] == "use the safe wrapper"
        back_on = await client.patch(
            f"/api/security/denied-commands/user/{rid}", json={"enabled": True}
        )
        assert back_on.status == 200
        reenabled = (await back_on.json())["user_added"][0]
        assert reenabled["enabled"] is True
        assert reenabled["note"] == "use the safe wrapper"


@pytest.mark.asyncio
async def test_user_toggle_unknown_is_404(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.patch(
            "/api/security/denied-commands/user/nope", json={"enabled": False}
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_user_delete(config_file: Path, mock_sel):
    async with _client() as client:
        add = await client.post("/api/security/denied-commands/user", json={"pattern": "danger"})
        rid = (await add.json())["user_added"][0]["id"]
        resp = await client.delete(f"/api/security/denied-commands/user/{rid}")
        assert resp.status == 200
        body = await resp.json()
        assert body["user_added"] == []
    assert _read_hooks(config_file).get("user_added", []) == []


@pytest.mark.asyncio
async def test_scalar_user_added_toggle_delete_are_404_not_500(config_file: Path, mock_sel):
    # A hand-edited config with a scalar (non-list) user_added must not 500 the
    # toggle/delete paths — an unknown id yields a clean 404 and the mutation
    # closures never iterate a scalar.
    _seed(config_file, {"user_added": 1})
    async with _client() as client:
        toggled = await client.patch(
            "/api/security/denied-commands/user/nope", json={"enabled": False}
        )
        assert toggled.status == 404
        deleted = await client.delete("/api/security/denied-commands/user/nope")
        assert deleted.status == 404


@pytest.mark.asyncio
async def test_user_delete_unknown_is_404(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.delete("/api/security/denied-commands/user/nope")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_malformed_user_entry_yields_404_not_500(config_file: Path, mock_sel):
    # A hand-edited config with a malformed user_added entry (no 'id') must not
    # crash the toggle/delete id lookup — an unknown id still returns 404.
    _seed(config_file, {"user_added": [{}, {"id": ""}, {"pattern": "x"}]})
    async with _client() as client:
        toggled = await client.patch(
            "/api/security/denied-commands/user/nope", json={"enabled": False}
        )
        assert toggled.status == 404
        deleted = await client.delete("/api/security/denied-commands/user/nope")
        assert deleted.status == 404


# ── keystone file isolation ──


@pytest.mark.asyncio
async def test_mutation_writes_only_the_keystone_file(home: Path, config_file: Path, mock_sel):
    # A mutation writes ONLY denied_commands.json (its root is the opt-out
    # object) and NEVER touches config.json — the opt-out store is fully
    # isolated from the agent-editable config.
    cfg_json = home / "config.json"
    cfg_json.write_text(
        json.dumps({"agent": {"bot_name": "x"}, "hooks": {"auto_deny_tools": ["foo"]}}),
        encoding="utf-8",
    )
    async with _client() as client:
        resp = await client.patch("/api/security/denied-commands/disable-all", json={"value": True})
        assert resp.status == 200
    # denied_commands.json now holds ONLY the opt-out object.
    persisted = json.loads(config_file.read_text(encoding="utf-8"))
    assert persisted["disable_all"] is True
    assert "hooks" not in persisted  # not nested under config
    # config.json never gains the opt-out state — the mutation does not write it
    # into config.json's hooks section (its pre-existing hooks keys are intact).
    cfg = json.loads(cfg_json.read_text(encoding="utf-8"))
    assert cfg["hooks"] == {"auto_deny_tools": ["foo"]}
    assert "denied_commands" not in cfg.get("hooks", {})


@pytest.mark.asyncio
async def test_mutation_on_corrupt_config_returns_500_without_wiping(config_file: Path, mock_sel):
    # A corrupt (but populated) denied_commands.json must NOT be silently reset —
    # the write path returns 500 and leaves the file byte-for-byte unchanged.
    corrupt = '{"disable_all": false, "disabled_ids": ["x"],}'  # trailing comma
    config_file.write_text(corrupt, encoding="utf-8")
    async with _client() as client:
        resp = await client.patch("/api/security/denied-commands/disable-all", json={"value": True})
        assert resp.status == 500
        body = await resp.json()
        assert "corrupt" in body["error"].lower()
    # File untouched — no data loss.
    assert config_file.read_text(encoding="utf-8") == corrupt
    # The reject is audited.
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_mutation_hot_reloads_live_hookmanager(home: Path, config_file: Path, mock_sel):
    # The opt-out must take effect WITHOUT a gateway restart: a mutation
    # hot-reloads the live HookManager so its _config reflects the new state.
    from types import SimpleNamespace

    from kiro_crew.hooks import HookManager, HooksConfig

    manager = HookManager(HooksConfig())
    assert manager._config.denied_commands_disable_all is False

    app = _make_app()
    app["state"] = SimpleNamespace(context_builder=SimpleNamespace(hooks=manager))
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.patch("/api/security/denied-commands/disable-all", json={"value": True})
        assert resp.status == 200
        # Live manager reflects the change immediately (no restart).
        assert manager._config.denied_commands_disable_all is True


# ── core.api_security_stats re-source ──


@pytest.mark.asyncio
async def test_api_security_stats_uses_effective_count(home: Path, config_file: Path):
    from kiro_crew.dashboard.handlers.core import api_security_stats

    rid = _a_builtin_id()
    _seed(config_file, {"disabled_ids": [rid]})
    req = MagicMock()
    resp = await api_security_stats(req)
    body = json.loads(resp.body.decode("utf-8"))
    assert body["denied_commands"] == _CATALOG_N - 1
    # The remaining counts are DERIVED from the controls they describe
    # (security_posture), not literals — this used to assert a hardcoded 5 while
    # the real number had grown to 16. Assert the derivation, not a magic number;
    # test_security_posture pins the per-control derivation itself.
    from kiro_crew.security_posture import build_posture_snapshot

    counts = build_posture_snapshot()["counts"]
    assert body["redaction_paths"] == counts["redaction_paths"]
    assert body["suspicious_patterns"] == counts["suspicious_patterns"]
    assert body["tool_schemas"] == counts["tool_schemas"]


def test_core_has_no_build_agent_config_import():
    import kiro_crew.dashboard.handlers.core as core

    assert not hasattr(core, "build_agent_config")


def test_enforce_denied_commands_settable_key_removed():
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert "agent.enforce_denied_commands" not in _EDITABLE_CONFIG


# ── keystone lockdown ──
#
# Regression: the keystone ``denied_commands.json`` lives in
# ``_SENSITIVE_HOME_DIRS`` and is the user-editable opt-out of the agent's own
# security ceiling. The agent process must NEVER leave it world-readable, even
# briefly. On Windows ``chmod_safe`` is a documented no-op and would leave the
# file under the inherited parent DACL; the writer routes through
# ``atomic_write(restrict_to_owner=True)`` so the lockdown is applied to the
# temp file before any content reaches it.


async def _run_write_denied_state(home: Path, config_file: Path, mutate):
    """Run ``_write_denied_state`` synchronously enough to assert on the file.

    The helper is ``async`` and runs the read-modify-write in the default
    thread executor. Driving the event loop here is enough to surface the
    after-write lockdown without going through the full aiohttp app.
    """
    from kiro_crew.dashboard.handlers.security import _write_denied_state

    return await _write_denied_state(mutate)


def test_write_denied_state_lands_owner_only_on_posix(
    home: Path, config_file: Path
) -> None:
    """``_write_denied_state`` finishes with mode 0o600 on POSIX.

    Pins the observable contract: the keystone file the next hook read will
    open is owner-only. Skipped on Windows; a DACL assertion needs a Windows
    fixture and is more invasive than the regression it catches.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX-only behavior")

    import asyncio

    def _noop(denied: dict) -> None:
        denied.setdefault("disabled_ids", [])

    asyncio.run(_run_write_denied_state(home, config_file, _noop))

    mode = config_file.stat().st_mode & 0o777
    assert mode == 0o600, (
        f"keystone denied_commands.json must be owner-only; got mode={mode:o}"
    )


def test_write_denied_state_does_not_fall_back_to_chmod_safe(
    home: Path, config_file: Path
) -> None:
    """``_write_denied_state`` does not call ``chmod_safe`` on the keystone.

    ``chmod_safe`` is a no-op on Windows; if a future refactor folds the
    lockdown back to ``chmod_safe``, the Windows keystone silently reverts to
    the inherited parent DACL. The helper is allowed to call ``chmod_safe``
    elsewhere (e.g. for the temp file's pre-write mode), but the audit
    pin is on the keystone path itself, which is what this test patches.
    """
    import kiro_crew.atomic_write as atomic_write_mod

    captured: dict = {}

    def _spy(path, *args, **kwargs):
        if Path(str(path)).resolve() == config_file.resolve():
            captured["called"] = True
        # No-op: don't run real atomic_write (which would shell out to icacls
        # on Windows and may fail in the test sandbox). The audit pin is on
        # routing, not on the lockdown step itself.

    import asyncio

    def _noop(denied: dict) -> None:
        denied.setdefault("disabled_ids", [])

    with patch.object(atomic_write_mod, "atomic_write", side_effect=_spy):
        asyncio.run(_run_write_denied_state(home, config_file, _noop))

    assert captured.get("called"), (
        "_write_denied_state must route the keystone write through atomic_write"
    )


@pytest.mark.asyncio
async def test_a_failed_lockdown_publishes_no_denied_commands(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """restrict_to_owner runs on the temp; a failure must not leave the keystone file."""
    monkeypatch.setattr(
        "kiro_crew.atomic_write.platform_compat.restrict_to_owner",
        lambda path: (_ for _ in ()).throw(OSError("icacls: transient failure")),
    )
    from kiro_crew.dashboard.handlers.security import _write_denied_state

    with pytest.raises(OSError, match="icacls"):
        await _write_denied_state(lambda d: d.update({"disable_all": True}))
    assert not (home / "denied_commands.json").exists()
    assert not list(home.glob("*.tmp"))
