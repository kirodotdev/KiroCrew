"""Dev Fleet per-worktree context and the fleet payload's honesty fields.

Covers issue and ticket links, the purpose one-liner, the payload fields that
disclose where pods cannot run, the CLI entry point the pods use, and pod-down.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod

# =============================================================================
# Per-worktree context: issue/ticket links + purpose one-liner
# =============================================================================


# --- issue-ref extraction ---
def test_extract_issue_refs_keyworded_and_bare_with_dedup():
    txt = "Fixes #12, Closes #34 and Resolves #56. See also #12 and bare #78."
    assert mod._extract_issue_refs(txt) == [12, 34, 56, 78]


def test_extract_issue_refs_rejects_hex_and_alnum_and_empty():
    # #fff / #1a2b (colours) and #12abc must NOT be parsed as issue refs.
    assert mod._extract_issue_refs("colour #fff border #1a2b tag #12abc") == []
    assert mod._extract_issue_refs("") == []
    assert mod._extract_issue_refs(None) == []  # type: ignore[arg-type]


def test_extract_issue_refs_boundaries():
    # Trailing punctuation / parens still yield the number.
    assert mod._extract_issue_refs("bump (#120) then #7.") == [120, 7]


# --- ticket-id extraction ---
def test_extract_ticket_ids_matches_and_dedups():
    txt = "TT-123 blocked by JIRA-4567; TT-123 again; PROJECT-9"
    assert mod._extract_ticket_ids(txt) == ["TT-123", "JIRA-4567", "PROJECT-9"]


def test_extract_ticket_ids_none_present():
    assert mod._extract_ticket_ids("no tickets here, just words") == []
    assert mod._extract_ticket_ids("") == []


# --- ticket-url template rendering ---
def test_render_ticket_url_with_template():
    assert mod._render_ticket_url("https://t.example/{id}", "TT-9") == "https://t.example/TT-9"


def test_render_ticket_url_empty_or_no_placeholder_returns_none():
    assert mod._render_ticket_url("", "TT-9") is None
    assert mod._render_ticket_url("https://t.example/browse", "TT-9") is None


# --- version-bump detection + summary pick ---
def test_is_version_bump():
    assert mod._is_version_bump("chore: bump version to 1.2.3") is True
    assert mod._is_version_bump("Bump version") is True
    assert mod._is_version_bump("1.2.3") is True
    assert mod._is_version_bump("release 2.0.0") is True
    assert mod._is_version_bump("feat: add pagination") is False


def test_pick_summary_skips_version_bumps():
    subjects = ["chore: bump version 1.2.3", "feat: add real feature", "wip"]
    assert mod._pick_summary(subjects) == "feat: add real feature"


def test_pick_summary_falls_back_to_latest_when_all_bumps():
    subjects = ["chore: bump version 1.2.3", "release 1.2.2"]
    assert mod._pick_summary(subjects) == "chore: bump version 1.2.3"
    assert mod._pick_summary([]) is None


# --- html origin parsing (issue link base) ---
def test_parse_html_repo_base_variants():
    p = mod._parse_html_repo_base
    assert p("git@github.com:kirodotdev/KiroCrew.git") == "https://github.com/kirodotdev/KiroCrew"
    assert (
        p("https://github.com/kirodotdev/KiroCrew.git") == "https://github.com/kirodotdev/KiroCrew"
    )
    assert p("https://github.com/kirodotdev/KiroCrew") == "https://github.com/kirodotdev/KiroCrew"
    assert (
        p("ssh://git@github.com/kirodotdev/KiroCrew.git")
        == "https://github.com/kirodotdev/KiroCrew"
    )
    assert p("") is None
    assert p("not a url") is None


# --- _pr_query_one carries title, hides body, and _redact_pr drops internals ---
@pytest.mark.asyncio
async def test_pr_query_one_carries_title_and_hides_body():
    payload = json.dumps(
        [
            {
                "number": 42,
                "state": "OPEN",
                "url": "https://github.com/o/r/pull/42",
                "isDraft": False,
                "title": "My PR title",
                "body": "Fixes #7",
                "headRefOid": "a" * 40,
            }
        ]
    )
    with patch.object(
        runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, payload, "")
    ):
        pr = await mod._pr_query_one("o/r", "feat/x")
    assert pr is not None
    assert pr["title"] == "My PR title"
    assert pr["_body"] == "Fixes #7"
    assert pr["_head_oid"] == "a" * 40
    assert "body" not in pr  # moved to internal _body
    assert "headRefOid" not in pr
    redacted = mod._redact_pr(pr)
    assert redacted["title"] == "My PR title"
    assert redacted["number"] == 42
    assert "_body" not in redacted  # internal fields dropped from payload
    assert "_repo" not in redacted
    assert "_head_oid" not in redacted


# --- _build_context: parses PR body + commits, builds links ---
@pytest.mark.asyncio
async def test_build_context_parses_pr_body_and_commits():
    log = (
        "feat(dev-fleet): surface context\x1fFixes #147\nrelated #99\x1e"
        "wip TT-5 progress\x1f\x1e"
    )
    with (
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=log),
        patch.object(
            fleet_state_mod,
            "_html_repo_base",
            new_callable=AsyncMock,
            return_value="https://github.com/kirodotdev/KiroCrew",
        ),
        patch.object(
            repository_mod,
            "_load_dev_fleet_cfg",
            return_value={"ticket_url_template": "https://t.example/{id}"},
        ),
    ):
        ctx = await mod._build_context("feat/thing", "/wt/thing", {"_body": "Closes #147\nsee #12"})
    # ordered-unique across pr body + commit subjects + commit bodies
    assert [i["number"] for i in ctx["issues"]] == [147, 12, 99]
    assert ctx["issues"][0]["url"] == "https://github.com/kirodotdev/KiroCrew/issues/147"
    assert [t["id"] for t in ctx["tickets"]] == ["TT-5"]
    assert ctx["tickets"][0]["url"] == "https://t.example/TT-5"
    assert ctx["summary"] == "feat(dev-fleet): surface context"


@pytest.mark.asyncio
async def test_build_context_graceful_when_git_fails():
    # git log fails (returns None) and there is no PR — issues empty, but a
    # ticket in the BRANCH NAME still resolves; never raises.
    with (
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=None),
        patch.object(fleet_state_mod, "_html_repo_base", new_callable=AsyncMock, return_value=None),
        patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}),
    ):
        ctx = await mod._build_context("TT-42-fix-thing", "/wt/x", None)
    assert ctx["issues"] == []
    assert [t["id"] for t in ctx["tickets"]] == ["TT-42"]
    assert ctx["tickets"][0]["url"] is None  # no template configured
    assert ctx["summary"] is None


@pytest.mark.asyncio
async def test_context_cached_skips_main_and_base():
    empty = {"issues": [], "tickets": [], "summary": None}
    assert await mod._context_cached(None, "/wt", None) == empty
    assert await mod._context_cached(mod.BASE_BRANCH, "/wt", None) == empty


@pytest.mark.asyncio
async def test_context_cached_serves_from_cache(monkeypatch):
    calls = []

    async def fake_build(branch, path, pr, facts_key=None):
        calls.append(branch)
        return {"issues": [{"number": 1, "url": None}], "tickets": [], "summary": "s"}

    monkeypatch.setattr(fleet_state_mod, "_build_context", fake_build)
    mod._CTX_CACHE.pop("feat/cache-me", None)
    try:
        a = await mod._context_cached("feat/cache-me", "/wt", None)
        b = await mod._context_cached("feat/cache-me", "/wt", None)
        assert a == b
        assert len(calls) == 1  # second call served from cache
    finally:
        mod._CTX_CACHE.pop("feat/cache-me", None)


# --- fleet payload carries the new context fields per worktree ---
@pytest.mark.asyncio
async def test_build_fleet_payload_has_context_fields():
    sentinel = {
        "issues": [{"number": 147, "url": "https://github.com/o/r/issues/147"}],
        "tickets": [{"id": "TT-5", "url": None}],
        "summary": "feat: do the thing",
    }
    worktrees = [
        {"path": "/repo", "branch": "main", "is_main": True},
        {"path": "/repo-wt-x", "branch": "feat/x", "is_main": False},
    ]
    ginfo = {
        "branch": "feat/x",
        "head": "abc1234",
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "last_updated_at": 111,
    }
    pr = {"number": 9, "state": "OPEN", "url": "u", "title": "T"}
    with (
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(
            repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=worktrees
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(repository_mod, "_git_info", new_callable=AsyncMock, return_value=ginfo),
        patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=pr),
        patch.object(repository_mod, "_git_ahead", new_callable=AsyncMock, return_value=0),
        patch.object(
            fleet_state_mod, "_context_cached", new_callable=AsyncMock, return_value=sentinel
        ),
        patch.object(
            live_mod, "_gateway_service_active", new_callable=AsyncMock, return_value=False
        ),
    ):
        fleet = await mod._build_fleet()
    rows = {w["name"]: w for w in fleet["worktrees"]}
    feat = rows["repo-wt-x"]
    assert feat["issues"] == sentinel["issues"]
    assert feat["tickets"] == sentinel["tickets"]
    assert feat["summary"] == "feat: do the thing"
    assert feat["pr"]["title"] == "T"  # title carried into the payload
    # main row still present and carries empty context (no crash)
    assert rows["main"]["summary"] is None
    assert rows["main"]["issues"] == []


# =============================================================================
# Non-Linux honesty: the fleet payload must DISCLOSE that pods cannot run here,
# and must still report build state (a plain filesystem fact).
# =============================================================================
async def _fleet_with(worktrees, **patches):
    """Build a fleet payload with everything external stubbed out."""
    ginfo = {
        "branch": "feat/x",
        "head": "abc1234",
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "last_updated_at": 111,
    }
    defaults = {
        "_live_worktree_path": AsyncMock(return_value=None),
        "_discover_worktrees": AsyncMock(return_value=worktrees),
        "_git_info": AsyncMock(return_value=ginfo),
        "_pr_status_cached": AsyncMock(return_value=None),
        "_git_ahead": AsyncMock(return_value=0),
        "_context_cached": AsyncMock(return_value={"issues": [], "tickets": [], "summary": None}),
        "_gateway_service_active": AsyncMock(return_value=False),
    }
    owners = {
        "_live_worktree_path": live_mod,
        "_discover_worktrees": repository_mod,
        "_git_info": repository_mod,
        "_pr_status_cached": fleet_state_mod,
        "_git_ahead": repository_mod,
        "_context_cached": fleet_state_mod,
        "_gateway_service_active": live_mod,
        "_POD_AVAILABLE": runtime_mod,
        "_POD_IMPORTED": runtime_mod,
        "_POD_ERROR": runtime_mod,
        "_load_cfg": runtime_mod,
        "_staged_target": live_mod,
        "_staged_cancel_available": live_mod,
    }
    with ExitStack() as stack:
        for attr, repl in defaults.items():
            stack.enter_context(patch.object(owners[attr], attr, repl))
        for attr, value in patches.items():
            stack.enter_context(patch.object(owners[attr], attr, value))
        return await mod._build_fleet()


@pytest.mark.asyncio
async def test_fleet_pod_health_is_identity_gated_not_a_bare_port_probe():
    """A squatter's 200 must not paint this worktree's row healthy.

    A pod's port is derived from its name across 199 slots and can be pinned by
    hand, so it is ordinarily held by another pod or by the live gateway; the row
    therefore reads health from the identity-gated probe, which needs the pod NAME
    as well as the port. Pinning the call shape here is what stops a future edit
    reverting to a port-only probe -- the reported failure was a crash-looping pod
    showing a healthy dot because somebody else answered its port.
    """
    seen: list[tuple] = []

    def _health(cfg, name, port, timeout=3):
        seen.append((name, port, timeout))
        return runtime_mod.rt.HEALTH_FOREIGN

    fake_cfg = SimpleNamespace()
    with (
        patch.object(runtime_mod.rt, "health", _health),
        patch.object(runtime_mod.rt, "active_names", lambda cfg: {"repo-wt-x"}),
        patch.object(runtime_mod.rt, "derive_port", lambda cfg, name: 7811),
    ):
        fleet = await _fleet_with(
            [{"path": "/repo-wt-x", "branch": "feat/x", "is_main": False}],
            _POD_AVAILABLE=True,
            _POD_IMPORTED=True,
            _load_cfg=lambda: fake_cfg,
        )

    row = {w["name"]: w for w in fleet["worktrees"]}["repo-wt-x"]
    assert seen == [("repo-wt-x", 7811, 2)]
    # Not a 2xx/401/403, so the frontend's `health >= 200` test renders this row
    # as unhealthy rather than as an open pod.
    assert row["health"] == runtime_mod.rt.HEALTH_FOREIGN
    assert row["health"] < 200


@pytest.mark.asyncio
async def test_fleet_enumerates_active_pods_once_for_all_worktrees():
    """Fleet polling must not run one full service-manager query per row."""
    active_names = MagicMock(return_value=set())
    fake_cfg = SimpleNamespace()
    worktrees = [
        {"path": "/repo", "branch": "main", "is_main": True},
        *[
            {"path": f"/repo-wt-{idx}", "branch": f"feat/{idx}", "is_main": False}
            for idx in range(4)
        ],
    ]
    with patch.object(runtime_mod.rt, "active_names", active_names):
        fleet = await _fleet_with(
            worktrees,
            _POD_AVAILABLE=True,
            _POD_IMPORTED=True,
            _load_cfg=lambda: fake_cfg,
        )

    active_names.assert_called_once_with(fake_cfg)
    assert all(not row["running"] for row in fleet["worktrees"])


@pytest.mark.asyncio
async def test_fleet_payload_marks_an_inferred_main_checkout():
    with patch.object(repository_mod, "MAIN_REPO_INFERRED", True):
        fleet = await _fleet_with([{"path": mod.MAIN_REPO, "branch": "main", "is_main": True}])

    assert fleet["main_repo"] == mod.MAIN_REPO
    assert fleet["main_repo_inferred"] is True


@pytest.mark.asyncio
async def test_fleet_payload_redacts_credentials_in_main_repo():
    sensitive = f"/tmp/ghp_{'A' * 40}/checkout"
    with patch.object(repository_mod, "_repo", return_value=sensitive):
        fleet = await _fleet_with([{"path": "/repo", "branch": "main", "is_main": True}])

    assert "ghp_" not in fleet["main_repo"]
    assert "[REDACTED" in fleet["main_repo"]


@pytest.mark.asyncio
async def test_fleet_payload_preserves_ordinary_main_repo_path():
    ordinary = "/home/user/oss/KiroCrew"
    with patch.object(repository_mod, "_repo", return_value=ordinary):
        fleet = await _fleet_with([{"path": "/repo", "branch": "main", "is_main": True}])

    assert fleet["main_repo"] == ordinary


@pytest.mark.asyncio
async def test_fleet_payload_discloses_why_pods_are_unavailable():
    """_POD_ERROR computed but read by NOTHING leaves a non-Linux user with pod
    controls that silently fail. It must reach the payload."""
    reason = "Pods are Linux systemd --user units; this host is darwin."
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _POD_AVAILABLE=False,
        _POD_ERROR=reason,
        _load_cfg=lambda: None,
    )
    assert fleet["pods_available"] is False
    assert fleet["pods_unavailable_reason"] == reason


@pytest.mark.asyncio
async def test_fleet_payload_reports_no_reason_when_pods_work():
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _POD_AVAILABLE=True,
        _POD_ERROR="",
        _load_cfg=lambda: None,
    )
    assert fleet["pods_available"] is True
    assert fleet["pods_unavailable_reason"] is None


@pytest.mark.asyncio
async def test_fleet_payload_says_live_state_is_known_when_the_pointer_reads():
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _load_cfg=lambda: None,
    )
    assert fleet["live_state_known"] is True


@pytest.mark.asyncio
async def test_fleet_payload_says_live_state_is_unknown_on_a_pointer_outage():
    """A broker outage must not render as "nothing is live": the rows still come
    back (no badge), and one field says the live/staged state is UNKNOWN so the
    surface can tell the user to check the gateway instead of staging a cutover."""

    async def _down(*, fresh=False):
        raise live_mod.PointerUnavailable("gateway not answering")

    with patch.object(live_mod, "pointer_state", _down):
        fleet = await _fleet_with(
            [{"path": "/repo", "branch": "main", "is_main": True}],
            _load_cfg=lambda: None,
        )
    assert fleet["live_state_known"] is False
    assert fleet["staged_target"] is None
    assert fleet["staged_cancel_available"] is False
    assert len(fleet["worktrees"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_path", "verified", "expected"),
    [
        ("/repo", True, True),
        ("/repo", False, False),
        (None, True, True),
    ],
)
async def test_fleet_payload_live_verified_says_whether_the_live_row_is_confirmed(
    live_path, verified, expected
):
    """A live row taken from the pointer alone (a packaged gateway cannot confirm
    it runs that checkout) has live_verified false, so the page shows "live?"
    instead of asserting it. The row keeps is_live for the guards."""

    async def _state(*, fresh=False):
        return live_mod.PointerState(
            live=live_path, staged=None, staged_cancel_available=False, live_verified=verified
        )

    with patch.object(live_mod, "pointer_state", _state):
        fleet = await _fleet_with(
            [{"path": "/repo", "branch": "main", "is_main": True}],
            _load_cfg=lambda: None,
        )
    assert fleet["live_state_known"] is True
    assert fleet["live_verified"] is expected
    assert fleet["worktrees"][0]["is_live"] is (live_path is not None)


# =============================================================================
# staged_cancel_available: the payload must mirror the _make_live cancel
# branch's own precondition (not can_restart), NOT _gateway_service_active
# (which also goes true for the foreground last resort, where the pointer-only
# cancel still works). Otherwise the dashboard offers a cancel that /make-live
# refuses with staged_cutover_pending.
# =============================================================================
@pytest.mark.asyncio
async def test_staged_cancel_available_truth_table():
    # No service backend at all: nothing to drive, cancel accepted.
    with patch.object(live_mod, "_gateway_backend", return_value=None):
        assert await mod._staged_cancel_available() is True
    # Drivable manager (unit status ok): _make_live refuses the pointer-only
    # cancel, so the payload must say unavailable.
    with (
        patch.object(live_mod, "_gateway_backend", return_value=object()),
        patch.object(live_mod, "_live_user_unit_status", new_callable=AsyncMock, return_value="ok"),
    ):
        assert await mod._staged_cancel_available() is False
    # Manager present but not drivable (e.g. system unit): cancel accepted —
    # including the foreground-eligible codes, where _gateway_service_active
    # would report True but can_restart stays False.
    with (
        patch.object(live_mod, "_gateway_backend", return_value=object()),
        patch.object(
            live_mod, "_live_user_unit_status", new_callable=AsyncMock, return_value="no_user_unit"
        ),
    ):
        assert await mod._staged_cancel_available() is True


@pytest.mark.asyncio
async def test_fleet_payload_reports_staged_cancel_available_with_stage():
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _load_cfg=lambda: None,
        _staged_target=lambda: "/w/other",
        _staged_cancel_available=AsyncMock(return_value=True),
    )
    assert fleet["staged_cancel_available"] is True


@pytest.mark.asyncio
async def test_fleet_payload_staged_cancel_false_without_stage():
    """No stage pending: the field is False and the host is NOT probed — the
    fleet endpoint is polled, so an unconditional service-manager probe would
    add a subprocess per poll for a control that cannot render anyway."""
    probe = AsyncMock(return_value=True)
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _load_cfg=lambda: None,
        _staged_target=lambda: None,
        _staged_cancel_available=probe,
    )
    assert fleet["staged_cancel_available"] is False
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_state_is_reported_even_where_pods_cannot_run(tmp_path):
    """Regression: has_venv/has_dist sat behind the pod-runnable gate, so every
    worktree showed as "not built" off Linux even when it was fully built.
    They are plain filesystem checks — knowable on every platform."""
    wt = tmp_path / "repo-wt-built"
    binp = wt / ".venv" / ("Scripts" if platform_compat.IS_WINDOWS else "bin")
    binp.mkdir(parents=True)
    exe = binp / ("kirocrew.exe" if platform_compat.IS_WINDOWS else "kirocrew")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (wt / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)

    fleet = await _fleet_with(
        [
            {"path": str(tmp_path / "repo"), "branch": "main", "is_main": True},
            {"path": str(wt), "branch": "feat/x", "is_main": False},
        ],
        # Pods cannot run, and _load_cfg therefore yields no config — the exact
        # state a macOS or Windows host is in.
        _POD_AVAILABLE=False,
        _POD_ERROR="pods require Linux systemd",
        _load_cfg=lambda: None,
    )
    row = {w["name"]: w for w in fleet["worktrees"]}["repo-wt-built"]
    assert row["has_venv"] is True
    assert row["has_dist"] is True
    # Pod state stays false — it genuinely cannot be known here.
    assert row["running"] is False
    assert row["port"] is None


@pytest.mark.asyncio
async def test_main_checkout_build_state_is_probed(tmp_path):
    """Regression: the build-state probes were gated on ``not is_main``,
    so a fully provisioned MAIN checkout always rendered as unprovisioned —
    during a cutover that reads as "the cutover failed". Build state is a plain
    filesystem check and is knowable for every worktree, main included; only
    the POD-state check legitimately skips main."""
    main_co = tmp_path / "repo"
    binp = main_co / ".venv" / ("Scripts" if platform_compat.IS_WINDOWS else "bin")
    binp.mkdir(parents=True)
    exe = binp / ("kirocrew.exe" if platform_compat.IS_WINDOWS else "kirocrew")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (main_co / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)

    fleet = await _fleet_with(
        [{"path": str(main_co), "branch": "main", "is_main": True}],
        _POD_AVAILABLE=False,
        _POD_ERROR="pods require Linux systemd",
        _load_cfg=lambda: None,
    )
    (row,) = fleet["worktrees"]
    assert row["is_main"] is True
    assert row["has_venv"] is True
    assert row["has_dist"] is True
    # Pod state still never applies to main.
    assert row["running"] is False
    assert row["port"] is None


# =============================================================================
# Regression: _find_cli must target a RUNNABLE entry point
# =============================================================================
def test_find_cli_targets_kiro_crew_package(nonbundled_python_without_user_site):
    """_find_cli must invoke the ``kiro_crew`` package (its __main__), not
    ``kiro_crew.cli`` — the latter has no __main__ guard and no-ops silently."""
    import sys

    assert mod._find_cli() == [sys.executable, "-s", "-P", "-m", "kiro_crew"]


def test_kiro_crew_module_entry_actually_runs():
    """The entry point _find_cli uses must actually run main() and emit output.

    Guards the root cause of #220: ``python -m kiro_crew.cli`` imported the
    module, ran no main(), and exited 0 with EMPTY output — so every pod op was
    a silent no-op reported as success. A runnable entry prints usage on --help.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "kiro_crew", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip(), "entry point produced no output (silent no-op regression)"


# =============================================================================
# _pod_down post-stop verification
# =============================================================================
@pytest.mark.asyncio
async def test_pod_down_fails_closed_when_still_active():
    """A CLI exit 0 must NOT be reported as success if the unit is still up."""
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/worktrees/kirocrew-wt-x"}, None),
        ),
        patch.object(
            worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None
        ),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "active_names", return_value={"kirocrew-wt-x"}),
    ):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "still active" in result["error"]


@pytest.mark.asyncio
async def test_pod_down_ok_when_unit_gone():
    """rc 0 AND the unit not active -> genuine success."""
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/worktrees/kirocrew-wt-x"}, None),
        ),
        patch.object(
            worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None
        ),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "active_names", return_value=set()),
    ):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_pod_down_fails_closed_when_verify_raises():
    """If the post-stop active-state check errors, fail closed (never claim ok)."""
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/worktrees/kirocrew-wt-x"}, None),
        ),
        patch.object(
            worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None
        ),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "active_names", side_effect=RuntimeError("boom")),
    ):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "cannot verify pod shutdown" in result["error"]


@pytest.mark.asyncio
async def test_pod_down_nonzero_rc_is_failure():
    """A non-zero CLI exit is surfaced as failure verbatim."""
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/worktrees/kirocrew-wt-x"}, None),
        ),
        patch.object(
            worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None
        ),
        patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(1, "", "stop failed")
        ),
    ):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "stop failed" in result["error"]
