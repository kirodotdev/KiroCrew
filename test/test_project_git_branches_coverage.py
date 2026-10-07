"""Sandbox-free coverage of branch listing, switching, and refusal paths.

Git subprocess seams are replaced by table-driven responses. The real sandbox
launcher is unavailable throughout these tests, so coverage does not depend on
namespace isolation or on a Git executable.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard import repo_checkout_guard
from kiro_crew.dashboard.handlers import git_branches as gb
from kiro_crew.dashboard.handlers import worktree as wt

_TOKEN = "ghp_" + "a1B2c3" * 6


class FakeGit:
    """Map argv prefixes to completed processes or raised exceptions."""

    def __init__(self, root):
        self.table = {
            ("rev-parse", "--show-toplevel"): (0, str(root), ""),
            ("branch", "--show-current"): (0, "main\n", ""),
            ("symbolic-ref", "--quiet", "HEAD"): (0, "refs/heads/main\n", ""),
        }
        self.refs = {}
        self.calls = []

    def __call__(self, args, cwd, **kwargs):
        self.calls.append((tuple(args), cwd, kwargs))
        for prefix, result in self.table.items():
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(result, Exception):
                    raise result
                return subprocess.CompletedProcess(args, *result)
        return subprocess.CompletedProcess(args, 0, "", "")

    def bounded(self, args, **kwargs):
        self.calls.append((tuple(args), kwargs["cwd"], kwargs))
        return self.refs.get(args[-1], (0, "", False))


@pytest.fixture(autouse=True)
def unavailable_sandbox(_floor_monkeypatch):
    launcher = MagicMock(side_effect=RuntimeError("sandbox unavailable"))
    _floor_monkeypatch.setattr(wt, "sandboxed_spawn_argv", launcher)
    return launcher


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    root = str(tmp_path)
    git = FakeGit(root)
    monkeypatch.setattr(gb, "_run_git", git)
    monkeypatch.setattr(gb, "_run_git_bounded", git.bounded)
    monkeypatch.setattr(gb, "_allowed_repo_roots", lambda state: [root])
    monkeypatch.setattr(gb, "_checkout_filter", lambda root: "")
    monkeypatch.setattr(gb, "_resolve_commit", lambda root, ref: "abc123")
    monkeypatch.setattr(gb, "is_sensitive_path", lambda path: False)
    monkeypatch.setattr(gb, "sel", MagicMock())
    monkeypatch.setattr(gb, "deny_non_dashboard_caller", lambda *args: None)
    monkeypatch.setattr(gb, "require_owner_dashboard_request", AsyncMock(return_value=None))
    body = {"path": root, "branch": "feature"}
    monkeypatch.setattr(gb, "read_bounded_json", AsyncMock(return_value=(body, None)))
    request = SimpleNamespace(app={"state": None}, query={"path": root}, get=lambda key: "owner")
    return SimpleNamespace(root=root, git=git, body=body, request=request)


def _row(name="feature", **extra):
    return {
        "name": name,
        "date": "today",
        "author": "Test",
        "subject": "work",
        **extra,
    }


def _ref(name, track="", head=""):
    return gb._SEP.join((name, "today", "Test", "work", track, head))


def _body(response):
    return json.loads(response.text)


def test_real_git_boundary_refuses_without_sandbox(boundary, unavailable_sandbox):
    with pytest.raises(gb.SandboxUnavailable, match="sandbox unavailable"):
        wt._run_git(["--version"], boundary.root)
    unavailable_sandbox.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,status,code",
    [
        ("missing", 400, "path_required"),
        ("nonstring", 400, "path_required"),
        ("unknown", 403, "unknown_project_dir"),
        ("sensitive", 403, "access_denied"),
        ("not-directory", 404, "not_a_repository"),
        ("sandbox", 503, "git_sandbox_unavailable"),
        ("os-error", 503, "git_unavailable"),
        ("subprocess-error", 503, "git_unavailable"),
        ("not-repo", 404, "not_a_repository"),
        ("probe-failure", 503, "git_branches_unavailable"),
        ("empty-top", 503, "git_branches_unavailable"),
        ("outside-root", 403, "repo_root_outside_project"),
        ("sensitive-top", 403, "repo_root_outside_project"),
    ],
)
async def test_resolve_refusals_and_listing_mapping(boundary, monkeypatch, case, status, code):
    raw = boundary.root
    if case == "missing":
        raw = "  "
    elif case == "nonstring":
        raw = None
    elif case == "unknown":
        monkeypatch.setattr(gb, "_allowed_repo_roots", lambda state: [])
    elif case == "sensitive":
        monkeypatch.setattr(gb, "is_sensitive_path", lambda path: True)
    elif case == "not-directory":
        monkeypatch.setattr(gb.os.path, "isdir", lambda path: False)
    else:
        results = {
            "sandbox": gb.SandboxUnavailable("unavailable"),
            "os-error": OSError("unavailable"),
            "subprocess-error": subprocess.SubprocessError("unavailable"),
            "not-repo": (128, "", "fatal: not a git repository (or any parent): .git"),
            "probe-failure": (128, "", "fatal: dubious ownership"),
            "empty-top": (0, " \n", ""),
            "outside-root": (0, str(gb.os.path.dirname(boundary.root)), ""),
            "sensitive-top": (0, os.path.join(boundary.root, "sensitive"), ""),
        }
        boundary.git.table[("rev-parse", "--show-toplevel")] = results[case]
        if case == "sensitive-top":
            monkeypatch.setattr(
                gb, "is_sensitive_path", lambda path: os.path.basename(path) == "sensitive"
            )
    with pytest.raises(gb._Refusal) as caught:
        await gb._resolve_repo_root(boundary.request, raw, "test")
    assert (caught.value.status, caught.value.body["code"]) == (status, code)
    boundary.request.query["path"] = raw
    response = await gb.api_project_git_branches(boundary.request)
    if code == "not_a_repository":
        assert response.status == 200
        assert _body(response) == {"repo": False, "local": [], "remote": []}
    else:
        assert (response.status, _body(response)["code"]) == (status, code)


@pytest.mark.asyncio
async def test_resolve_uses_server_root_for_subdirectory(boundary):
    raw = boundary.root + "/nested/../nested"
    assert await gb._resolve_repo_root(boundary.request, raw, "test") == boundary.root
    assert boundary.git.calls[0][1] == boundary.root


def test_list_refs_parses_tracking_and_skips_malformed_rows(boundary):
    lines = [
        _ref("refs/heads/main", "ahead 3, behind 4", "*"),
        _ref("refs/heads/feature", "gone"),
        _ref("refs/heads/plain"),
        "malformed",
        _ref("refs/tags/tag"),
    ]
    boundary.git.refs["refs/heads"] = (0, "\n".join(lines), False)
    rows, truncated = gb._list_refs(boundary.root, "refs/heads")
    assert not truncated
    assert len(rows) == 3
    assert rows[0] == _row("main", current=True, ahead=3, behind=4)
    assert rows[1] == _row("feature", current=False)
    assert rows[2] == _row("plain", current=False)
    argv, root, kwargs = boundary.git.calls[-1]
    assert "--count=201" in argv and argv[-1] == "refs/heads"
    assert f"--format={gb._FORMAT_ARG}" in argv
    assert "%(upstream:track,nobracket)" in gb._FORMAT_ARG
    assert "%(objectname:short)" not in gb._FORMAT_ARG
    assert "%(upstream:short)" not in gb._FORMAT_ARG
    assert root == boundary.root
    assert kwargs["cap"] == gb.MAX_LIST_OUTPUT
    assert kwargs["env"]["LC_ALL"] == "C"
    boundary.git.refs["refs/remotes"] = (
        0,
        _ref("refs/remotes/origin/HEAD") + "\n" + _ref("refs/remotes/origin/other"),
        False,
    )
    assert gb._list_refs(boundary.root, "refs/remotes") == ([_row("origin/other")], False)


@pytest.mark.parametrize("result", [(1, "", False), (0, "", True)])
def test_list_refs_failures(boundary, result):
    boundary.git.refs["refs/heads"] = result
    assert gb._list_refs(boundary.root, "refs/heads") is None


@pytest.mark.parametrize("symref", [False, True])
def test_list_refs_truncation_includes_skipped_symref(boundary, monkeypatch, symref):
    monkeypatch.setattr(gb, "MAX_BRANCH_ROWS", 2)
    lines = [_ref(f"refs/remotes/origin/{name}") for name in ("a", "b", "HEAD" if symref else "c")]
    boundary.git.refs["refs/remotes"] = (0, "\n".join(lines), False)
    rows, truncated = gb._list_refs(boundary.root, "refs/remotes")
    assert len(rows) == 2 and truncated


@pytest.mark.parametrize("failed", ["current", "local", "remote"])
def test_branches_sync_failures(boundary, failed):
    if failed == "current":
        boundary.git.table[("branch", "--show-current")] = (1, "", "failed")
    else:
        namespace = "refs/heads" if failed == "local" else "refs/remotes"
        boundary.git.refs[namespace] = (1, "", False)
    body, status = gb._branches_sync(boundary.root)
    assert (status, body["code"]) == (503, "git_branches_unavailable")


@pytest.mark.parametrize("head_result", [(0, "abc123\n", ""), (1, "", ""), (0, "", "")])
def test_branches_detached_filter_truncation_and_remote_twins(boundary, monkeypatch, head_result):
    boundary.git.table[("branch", "--show-current")] = (0, "", "")
    boundary.git.table[("rev-parse", "--short", "HEAD")] = head_result
    monkeypatch.setattr(
        gb,
        "_list_refs",
        lambda root, ns: (
            ([_row("feature")], True)
            if ns == "refs/heads"
            else ([_row("origin/feature"), _row("origin/other")], False)
        ),
    )
    monkeypatch.setattr(gb, "_checkout_filter", lambda root: "filter.example.smudge")
    body, status = gb._branches_sync(boundary.root)
    assert status == 200 and body["current"] is None
    assert body["remote"] == [_row("origin/other")]
    assert body["truncated"] and body["switchBlocked"] == "filter"
    assert body.get("detached", False) == bool(head_result[1])
    if head_result[1]:
        assert body["head"] == "abc123"


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize(
    "name,valid",
    [("feat/x", True), ("feat+1", False), ("Fix#12", False), ("_wip", False), ("x" * 201, False)],
)
def test_redacted_rows_remote_creation_grammar(remote, name, valid):
    row = _row("origin/" + name if remote else name)
    gb._redact_rows([row], remote=remote)
    assert row["switchable"] == (valid if remote else len(name) <= gb.MAX_BRANCH_NAME)
    assert len(row["name"]) <= gb.MAX_BRANCH_NAME


def test_redacted_rows_sanitize_before_clipping():
    row = _row(
        "feat/" + _TOKEN,
        author=_TOKEN + "a" * 500,
        subject="s" * 500,
    )
    gb._redact_rows([row])
    assert not row["switchable"] and _TOKEN not in str(row)
    assert all(
        len(row[key]) <= limit
        for key, limit in (
            ("name", gb.MAX_BRANCH_NAME),
            ("author", gb.MAX_ROW_TEXT),
            ("subject", gb.MAX_ROW_TEXT),
        )
    )
    assert row["subject"].endswith("\u2026")


@pytest.mark.asyncio
@pytest.mark.parametrize("detached", [False, True])
async def test_listing_redacts_current_head_and_remote_validation(boundary, monkeypatch, detached):
    payload = {
        "repo": True,
        "current": None if detached else _TOKEN,
        "local": [_row()],
        "remote": [_row("origin/feat+1")],
    }
    if detached:
        payload["head"] = _TOKEN
    monkeypatch.setattr(gb, "_branches_sync", lambda root: (payload, 200))
    response = await gb.api_project_git_branches(boundary.request)
    assert response.status == 200 and _TOKEN not in response.text
    assert not _body(response)["remote"][0]["switchable"]


_EXCEPTIONS = [
    (gb.SandboxUnavailable("no sandbox"), 503, "git_sandbox_unavailable"),
    (subprocess.TimeoutExpired("git", 10), 504, "git_timeout"),
    (OSError("unavailable"), 503, "git_branches_unavailable"),
    (subprocess.SubprocessError("failed"), 503, "git_branches_unavailable"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,status,code", _EXCEPTIONS)
async def test_listing_exception_mapping(boundary, exc, status, code):
    boundary.git.table[("branch", "--show-current")] = exc
    response = await gb.api_project_git_branches(boundary.request)
    assert (response.status, _body(response)["code"]) == (status, code)


@pytest.mark.asyncio
async def test_listing_sync_error_response(boundary):
    boundary.git.table[("branch", "--show-current")] = (1, "", "failed")
    response = await gb.api_project_git_branches(boundary.request)
    assert (response.status, _body(response)["code"]) == (503, "git_branches_unavailable")


@pytest.mark.parametrize("name", ["main", "feat/x", "user@host/x", "feat+1"])
def test_plain_ref_names(name):
    assert gb._is_plain_ref_name(name)


@pytest.mark.parametrize(
    "name", ["", "-x", "-", "@{-1}", "a b", "a\tb", "a\nb", "a\x01b", "a\x7fb", "x" * 201]
)
def test_ambiguous_ref_names(name):
    assert not gb._is_plain_ref_name(name)


@pytest.mark.parametrize("mode", ["existing", "create", "track"])
def test_switch_argv_disables_submodule_recursion(boundary, monkeypatch, mode):
    monkeypatch.setattr(
        gb, "_resolve_commit", lambda root, ref: "" if mode == "create" else "abc123"
    )
    body, status = gb._switch_sync(
        boundary.root, "feature", mode == "create", "origin/feature" if mode == "track" else ""
    )
    assert (status, body) == (200, {"ok": True, "branch": "feature"})
    assert not any(argv[0] == "branch" for argv, _, _ in boundary.git.calls)
    assert any(argv[0] == "symbolic-ref" for argv, _, _ in boundary.git.calls) == (
        mode == "existing"
    )
    switches = [argv for argv, _, _ in boundary.git.calls if argv[0] == "switch"]
    expected = ["switch", "--no-recurse-submodules", "--no-overwrite-ignore"]
    if mode == "existing":
        expected += ["--no-guess", "--", "feature"]
    elif mode == "create":
        expected += ["--no-track", "-c", "feature"]
    else:
        expected += ["-c", "feature", "--track", "refs/remotes/origin/feature"]
    assert switches == [tuple(expected)]
    assert all(kwargs["c_locale"] is True for _, _, kwargs in boundary.git.calls)


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("filter", 409, "git_switch_filter_refused"),
        ("format", 400, "invalid_branch"),
        ("missing", 404, "git_branch_not_found"),
        ("remote-missing", 404, "git_branch_not_found"),
        ("exists", 409, "git_branch_exists"),
    ],
)
def test_switch_preflight_refusals(boundary, monkeypatch, case, status, code):
    if case == "filter":
        monkeypatch.setattr(gb, "_checkout_filter", lambda root: "filter.example.smudge")
    if case == "format":
        boundary.git.table[("check-ref-format",)] = (1, "", "invalid")
    if "missing" in case:
        monkeypatch.setattr(gb, "_resolve_commit", lambda root, ref: "")
    body, actual = gb._switch_sync(
        boundary.root,
        "feature",
        case == "exists",
        "origin/feature" if case == "remote-missing" else "",
    )
    assert (actual, body["code"]) == (status, code)
    assert not any(argv[0] == "switch" for argv, _, _ in boundary.git.calls)


@pytest.mark.parametrize("current", [(0, "refs/heads/main", ""), (1, "", ""), (0, "", "")])
def test_switch_current_noop_and_detached_head(boundary, current):
    boundary.git.table[("symbolic-ref", "--quiet", "HEAD")] = current
    body, status = gb._switch_sync(boundary.root, "main", False, "")
    assert (status, body) == (200, {"ok": True, "branch": "main"})
    assert any(argv[0] == "switch" for argv, _, _ in boundary.git.calls) == (not current[1])
    assert not any(argv[0] == "branch" for argv, _, _ in boundary.git.calls)
    assert all(kwargs["c_locale"] is True for _, _, kwargs in boundary.git.calls)


@pytest.mark.parametrize("fragment,code,status,message", gb._SWITCH_FAILURES)
def test_switch_known_failures(boundary, fragment, code, status, message):
    boundary.git.table[("switch",)] = (1, "", "fatal: " + fragment.upper())
    body, actual = gb._switch_sync(boundary.root, "feature", False, "")
    assert (actual, body) == (status, {"error": message, "code": code})


@pytest.mark.parametrize(
    "stderr,stdout,detail",
    [
        ("\n first line \nsecond", "", "first line"),
        ("", "stdout diagnostic", "stdout diagnostic"),
        (" \n\t", "", ""),
        ("x" * 400, "", "x" * 300),
    ],
)
def test_switch_generic_failure_detail(boundary, stderr, stdout, detail):
    boundary.git.table[("switch",)] = (1, stdout, stderr)
    body, status = gb._switch_sync(boundary.root, "feature", False, "")
    assert status == 400 and body["code"] == "git_switch_failed" and body["detail"] == detail


@pytest.mark.asyncio
async def test_full_diagnostic_redacted_before_cut_and_at_response(boundary):
    prefix = "fatal: " + "x" * 278
    diagnostic = prefix + _TOKEN + "/lock: cannot create\nsecond line"
    assert diagnostic.index(_TOKEN) < 300 < diagnostic.index(_TOKEN) + len(_TOKEN)
    assert _TOKEN[:15] in diagnostic[:300]
    assert gb.redact(diagnostic[:300]) == diagnostic[:300]
    boundary.git.table[("switch",)] = (1, "", diagnostic)
    payload, status = gb._switch_sync(boundary.root, "feature", False, "")
    assert status == 400 and _TOKEN[:15] not in payload["detail"]
    assert "REDACTED" in payload["detail"] and len(payload["detail"]) <= 300
    response = await gb.api_project_git_switch(boundary.request)
    assert response.status == 400 and _body(response)["detail"] == payload["detail"]
    assert gb.redact(payload["detail"]) == payload["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,code",
    [
        ({"branch": None}, "invalid_input"),
        ({"branch": 3}, "invalid_input"),
        ({"create": 1}, "invalid_input"),
        ({"track": None}, "invalid_input"),
        ({"create": True, "track": "origin/x"}, "invalid_input"),
        ({"branch": "-f"}, "invalid_branch"),
        ({"branch": ""}, "invalid_branch"),
        ({"branch": "x" * 201}, "invalid_branch"),
        ({"branch": "feat+1", "create": True}, "invalid_branch"),
        ({"branch": "feat+1", "track": "origin/feat+1"}, "invalid_branch"),
        ({"track": "@{-1}"}, "invalid_branch"),
    ],
)
async def test_switch_input_validation(boundary, patch, code):
    boundary.body.update(patch)
    response = await gb.api_project_git_switch(boundary.request)
    assert (response.status, _body(response)["code"]) == (400, code)
    assert boundary.git.calls == []


@pytest.mark.asyncio
async def test_switch_resolver_refusal(boundary):
    boundary.body["path"] = None
    response = await gb.api_project_git_switch(boundary.request)
    assert (response.status, _body(response)["code"]) == (400, "path_required")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,status,code",
    [
        (subprocess.TimeoutExpired("git", 10), 504, "git_timeout"),
        (gb.SandboxUnavailable("unavailable"), 503, "git_sandbox_unavailable"),
        (OSError("unavailable"), 500, "git_switch_failed"),
        (subprocess.SubprocessError("unavailable"), 500, "git_switch_failed"),
    ],
)
async def test_switch_exception_mapping(boundary, exc, status, code):
    boundary.git.table[("switch",)] = exc
    response = await gb.api_project_git_switch(boundary.request)
    assert (response.status, _body(response)["code"]) == (status, code)


@pytest.mark.asyncio
async def test_switch_classified_error_without_detail(boundary):
    boundary.git.table[("switch",)] = (1, "", "branch already exists")
    response = await gb.api_project_git_switch(boundary.request)
    assert (response.status, _body(response)["code"]) == (409, "git_branch_exists")
    assert "detail" not in _body(response)


@pytest.mark.asyncio
async def test_switch_success_redacts_response_and_strips_input(boundary, monkeypatch):
    boundary.body.update(branch=" feature ", track=" origin/feature ")
    switch = MagicMock(return_value=({"ok": True, "branch": _TOKEN}, 200))
    monkeypatch.setattr(gb, "_switch_sync", switch)
    response = await gb.api_project_git_switch(boundary.request)
    switch.assert_called_once_with(boundary.root, "feature", False, "origin/feature")
    assert response.status == 200 and _TOKEN not in response.text
    assert _body(response) == {"ok": True, "branch": gb.redact(_TOKEN)}


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["listing", "owner", "body"])
async def test_route_gates_return_before_git(boundary, monkeypatch, gate):
    denied = web.json_response({"error": "denied"}, status=403)
    if gate == "listing":
        monkeypatch.setattr(gb, "deny_non_dashboard_caller", lambda *args: denied)
        response = await gb.api_project_git_branches(boundary.request)
    else:
        if gate == "owner":
            monkeypatch.setattr(
                gb, "require_owner_dashboard_request", AsyncMock(return_value=denied)
            )
        else:
            monkeypatch.setattr(gb, "read_bounded_json", AsyncMock(return_value=(None, denied)))
        response = await gb.api_project_git_switch(boundary.request)
    assert response is denied and boundary.git.calls == []


@pytest.mark.parametrize("c_locale", [True, False])
def test_run_git_sets_the_c_locale_only_when_asked(_floor_monkeypatch, tmp_path, c_locale):
    seen: dict = {}

    def fake_spawn(argv, mode="standard", **kwargs):
        return list(argv), {"LC_ALL": "fr_FR.UTF-8"}, None

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    _floor_monkeypatch.setattr(wt, "sandboxed_spawn_argv", fake_spawn)
    _floor_monkeypatch.setattr(wt.subprocess, "run", fake_run)
    wt._run_git(["status"], str(tmp_path), c_locale=c_locale)
    if c_locale:
        assert (seen["env"]["LC_ALL"], seen["env"]["LANGUAGE"]) == ("C", "C")
    else:
        assert seen["env"]["LC_ALL"] == "fr_FR.UTF-8"
        assert "LANGUAGE" not in seen["env"]


class _Subagents:
    def __init__(self, pending=(), error=False):
        self.pending = set(pending)
        self.error = error
        self.asked: list[str] = []

    async def has_pending_work_for_async(self, key):
        self.asked.append(key)
        if self.error:
            raise RuntimeError("store unavailable")
        return key in self.pending


def _slot(project, key, running=False):
    return SimpleNamespace(project=str(project), key=key, running=running, session_key="")


def test_slots_in_repo_matches_root_inside_and_containing_folders(tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    state = SimpleNamespace(
        _slots={
            "a": _slot(repo, "a"),
            "b": _slot(repo / "sub", "b"),
            "c": _slot(tmp_path, "c"),
            "d": _slot(other, "d"),
            "e": _slot("", "e"),
        }
    )
    keys = [s.key for s in gb._slots_in_repo(state, os.path.realpath(repo))]
    assert keys == ["a", "b", "c"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "subagents", "expected"),
    [
        (False, None, False),
        (True, None, True),
        (False, _Subagents(), False),
        (False, _Subagents(pending={"dashboard:busy"}), True),
        (False, _Subagents(error=True), True),
    ],
)
async def test_repo_has_running_work(tmp_path, monkeypatch, running, subagents, expected):
    from kiro_crew.dashboard import chat_utils

    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    state = SimpleNamespace(_slots={"busy": _slot(tmp_path, "busy", running)}, subagents=subagents)
    assert await gb._repo_has_running_work(state, os.path.realpath(tmp_path)) is expected


@pytest.mark.asyncio
async def test_switch_refuses_while_a_session_in_the_repo_runs(boundary, monkeypatch):
    monkeypatch.setattr(gb, "_repo_has_running_work", AsyncMock(return_value=True))
    response = await gb.api_project_git_switch(boundary.request)
    assert response.status == 409
    assert json.loads(response.text)["code"] == "git_switch_session_busy"
    assert not any(call[0][0] == "switch" for call in boundary.git.calls)
    assert not repo_checkout_guard.is_reserved(boundary.root)


@pytest.fixture
def clean_reservations():
    repo_checkout_guard._reserved.clear()
    yield
    repo_checkout_guard._reserved.clear()


@pytest.mark.asyncio
async def test_switch_reserves_the_repo_before_the_busy_check(
    boundary, monkeypatch, clean_reservations
):
    seen: list[bool] = []

    async def busy(state, root):
        seen.append(repo_checkout_guard.is_reserved(root))
        return False

    monkeypatch.setattr(gb, "_repo_has_running_work", busy)
    response = await gb.api_project_git_switch(boundary.request)
    assert response.status == 200
    assert seen == [True]
    assert not repo_checkout_guard.is_reserved(boundary.root)


@pytest.mark.asyncio
async def test_switch_refuses_while_another_switch_holds_the_repo(
    boundary, monkeypatch, clean_reservations
):
    busy = AsyncMock(return_value=False)
    monkeypatch.setattr(gb, "_repo_has_running_work", busy)
    repo_checkout_guard.reserve(boundary.root)
    response = await gb.api_project_git_switch(boundary.request)
    assert (response.status, _body(response)["code"]) == (409, "git_switch_session_busy")
    busy.assert_not_awaited()
    assert not any(call[0][0] == "switch" for call in boundary.git.calls)
    assert repo_checkout_guard.is_reserved(boundary.root)


@pytest.mark.asyncio
async def test_a_cancelled_switch_keeps_the_reservation_until_git_exits(
    boundary, monkeypatch, clean_reservations
):
    monkeypatch.setattr(gb, "_repo_has_running_work", AsyncMock(return_value=False))
    started = threading.Event()
    finish = threading.Event()

    def slow_switch(root, branch, create, track):
        started.set()
        finish.wait(5)
        return {"ok": True, "branch": branch}, 200

    monkeypatch.setattr(gb, "_switch_sync", slow_switch)
    request = asyncio.create_task(gb.api_project_git_switch(boundary.request))
    assert await asyncio.to_thread(started.wait, 5)

    request.cancel()
    await asyncio.sleep(0.05)
    assert not request.done()
    assert repo_checkout_guard.is_reserved(boundary.root)

    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 5)
    assert not repo_checkout_guard.is_reserved(boundary.root)
