"""HTTP surface of the auto-improvement app (``backend/routes.py``).

Lives in the repo-level ``test/`` tree rather than the app's in-package ``tests/``
so it runs on every backend shard: the app's own suites drive a real ``git`` and a
real ``gh`` through the OS sandbox, which the hosted runners cannot provide.

Handlers are driven directly with ``aiohttp.test_utils.make_mocked_request`` — the
approach ``test_papyrus_routes.py`` takes — so no port is bound, no subprocess is
spawned, and nothing is written outside ``tmp_path``. Every collaborator the module
imports is stubbed at its module attribute:

* ``runner.get_supervisor`` returns a :class:`FakeSupervisor` whose ``status`` the
  run-conflict guards read, so the 409 arms are reachable without a live loop;
* ``pr_watchers.get_registry`` returns a :class:`FakeRegistry` that records calls;
* ``commit``/``clone_setup``/``ledger_admin``/``deps``/``progress`` entry points are
  replaced by fakes, so the git- and network-owning code is asserted on by argv and
  return shape rather than executed.

What is covered, in the order a request travels: the deny-by-default
``_require_enabled`` gate, the fingerprint allowlist at the boundary, the
fail-closed redaction helpers, the run-conflict guards (both the pre-lock check and
the in-lock recheck each mutating handler carries), and then every endpoint's
validation, refusal, and error-response shape.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.auto_improvement.backend import (
    clone_setup,
)
from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
from kiro_crew.apps.builtins.auto_improvement.backend import (
    deps,
    ledger_admin,
    pr_checks,
    pr_watchers,
    profile_normalize,
    progress,
    routes,
    runner,
    sse,
    store,
)

FP = "abc123"


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeSupervisor:
    """Duck-types ``runner.RunSupervisor`` for the guards and the run endpoints.

    ``status_queue`` lets a test hand out DIFFERENT statuses on successive reads,
    which is the only way to reach the in-lock recheck arms: those exist precisely
    for the case where the pre-lock guard saw idle and a run started while the
    request waited on ``clone_lock``.
    """

    def __init__(self, status: str = runner.STATUS_IDLE) -> None:
        self.status_queue: list[str] = []
        self._status = status
        self.calls: list[str] = []
        self.start_result: dict[str, Any] = {"status": runner.STATUS_RUNNING}
        self.start_raises: BaseException | None = None
        self.calibrate_raises: BaseException | None = None

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        if self.status_queue:
            return {"status": self.status_queue.pop(0)}
        return {"status": self._status}

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("start")
        if self.start_raises is not None:
            raise self.start_raises
        return dict(self.start_result, config=config)

    def calibrate(self, config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("calibrate")
        if self.calibrate_raises is not None:
            raise self.calibrate_raises
        return {"status": runner.STATUS_CALIBRATING, "clone": config.get("clone")}

    def stop(self) -> dict[str, Any]:
        self.calls.append("stop")
        return {"status": runner.STATUS_STOPPING}


class FakeRegistry:
    """Duck-types ``pr_watchers.PRWatcherRegistry``. Starts no thread, spawns nothing."""

    def __init__(self) -> None:
        self.sessions: list[dict[str, Any]] = []
        self.reconcile_result: dict[str, Any] = {"promoted": 0}
        self.should = False
        self.loops: list[Any] = []
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.status_result: dict[str, Any] | None = {"fp": FP, "status": "nudging"}
        self.log_result: dict[str, Any] = {"entries": [], "next": 0}
        self.log_calls: list[tuple[str, int]] = []
        self.reconcile_kwargs: dict[str, Any] = {}

    def list_sessions(self) -> list[dict[str, Any]]:
        return list(self.sessions)

    def should_reconcile(self) -> bool:
        return self.should

    def reconcile_failing_prs(self, **kwargs: Any) -> dict[str, Any]:
        self.reconcile_kwargs = kwargs
        return dict(self.reconcile_result)

    def attach_loop(self, loop: Any) -> None:
        self.loops.append(loop)

    def start(self, **kwargs: Any) -> Any:
        self.started.append(kwargs)
        return mock.Mock(status="starting")

    def status(self, fp: str) -> dict[str, Any] | None:
        return self.status_result

    def stop(self, fp: str) -> bool:
        self.stopped.append(fp)
        return True

    def get_log(self, fp: str, since: int) -> dict[str, Any]:
        self.log_calls.append((fp, since))
        return dict(self.log_result)


class FakeRecipe:
    """Duck-types ``GitHubPRRecipe``: records construction, publishes nothing."""

    instances: list[FakeRecipe] = []
    ref = "https://github.com/owner/repo/pull/9"
    raises: BaseException | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.draft_calls: list[dict[str, Any]] = []
        FakeRecipe.instances.append(self)

    def draft(self, **kwargs: Any) -> str:
        self.draft_calls.append(kwargs)
        if FakeRecipe.raises is not None:
            raise FakeRecipe.raises
        return FakeRecipe.ref


# ── request/response helpers ─────────────────────────────────────────────────


def _request(
    method: str = "GET",
    path: str = "/api/apps/auto-improvement/health",
    *,
    body: Any = None,
    match_info: dict[str, str] | None = None,
) -> web.Request:
    """A mocked request whose ``json()`` yields ``body`` (or raises for a sentinel)."""
    request = make_mocked_request(method, path, match_info=match_info or {})
    if body is _BAD_JSON:
        request.json = mock.AsyncMock(  # type: ignore[method-assign]
            side_effect=ValueError("not json")
        )
    else:
        request.json = mock.AsyncMock(return_value=body)  # type: ignore[method-assign]
    return request


_BAD_JSON = object()


def _json_of(response: web.StreamResponse) -> dict[str, Any]:
    assert isinstance(response, web.Response)
    assert isinstance(response.body, (bytes, bytearray))
    parsed = json.loads(response.body)
    assert isinstance(parsed, dict)
    return parsed


def _write(path: Path, text: str) -> None:
    """Write with LF endings on every platform.

    ``Path.write_text`` translates ``\\n`` to ``\\r\\n`` on Windows while the readers
    under test open with the default translation, so a ledger line count would differ
    by platform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    _write(path, "".join(json.dumps(row) + "\n" for row in rows))


def _unreadable(monkeypatch: pytest.MonkeyPatch, *suffixes: str) -> None:
    """Make ``Path.read_text`` raise ``OSError`` for files with these suffixes.

    A permission bit would not do it portably: Windows has no ``os.fchmod``, and on
    POSIX a mode change does not stop the owner reading its own file. Patching the
    reader reaches the ``except OSError`` arms on every platform.
    """
    real = Path.read_text

    def _read(self: Path, *args: Any, **kwargs: Any) -> str:
        if any(self.name.endswith(suffix) for suffix in suffixes):
            raise OSError("simulated I/O error")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the app's data root, scratch root and crew home under ``tmp_path``.

    ``store.data_dir`` is the one seam every other path helper derives from, so
    patching it reaches ``config_path``, ``ledger_path``, ``pr_queue_dir``,
    ``results_dir`` and ``sessions_dir`` at once.
    """
    root = tmp_path / "ai-data"
    root.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "crew-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("AUTO_IMPROVEMENT_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(store, "data_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def supervisor(monkeypatch: pytest.MonkeyPatch) -> FakeSupervisor:
    """An idle fake supervisor everywhere, so no test can touch the real singleton."""
    fake = FakeSupervisor()
    monkeypatch.setattr(runner, "get_supervisor", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    fake = FakeRegistry()
    monkeypatch.setattr(pr_watchers, "get_registry", lambda: fake)
    monkeypatch.setattr(pr_watchers, "sweep_orphan_clones", lambda: 3)
    return fake


@pytest.fixture(autouse=True)
def recipe(monkeypatch: pytest.MonkeyPatch) -> type[FakeRecipe]:
    FakeRecipe.instances = []
    FakeRecipe.raises = None
    FakeRecipe.ref = "https://github.com/owner/repo/pull/9"
    monkeypatch.setattr(routes, "GitHubPRRecipe", FakeRecipe)
    return FakeRecipe


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)


def _config(data_root: Path, **fields: Any) -> Path:
    path = store.config_path()
    _write(path, json.dumps(fields))
    return path


# ── the deny-by-default gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRequireEnabled:
    async def test_refuses_while_the_app_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Routes are mounted once at gateway startup, so an opt-in app would stay
        fully callable the moment the gateway booted without this gate."""
        monkeypatch.setattr(routes, "is_app_enabled", lambda _name: False)
        guarded = routes._require_enabled(routes._handle_health)
        response = await guarded(_request())
        assert response.status == 403
        payload = _json_of(response)
        assert payload["code"] == "app_disabled"
        assert store.APP_NAME in payload["error"]

    async def test_passes_through_when_enabled(self, enabled: None) -> None:
        guarded = routes._require_enabled(routes._handle_health)
        response = await guarded(_request())
        assert response.status == 200
        assert _json_of(response) == {"ok": True, "app": store.APP_NAME}


# ── boundary helpers ─────────────────────────────────────────────────────────


class TestValidatedFingerprint:
    def test_missing_fingerprint_is_a_400(self) -> None:
        fp, bad = routes._validated_fp(_request(match_info={}))
        assert fp == ""
        assert bad is not None and bad.status == 400
        assert _json_of(bad)["code"] == "fingerprint_required"

    @pytest.mark.parametrize(
        "raw",
        ["../etc/passwd", "a/b", "..", "with space", "-leading", "x" * 65, "dot.ted"],
    )
    def test_a_traversal_shaped_fingerprint_is_rejected_not_sanitized(self, raw: str) -> None:
        fp, bad = routes._validated_fp(_request(match_info={"fp": raw}))
        assert fp == ""
        assert bad is not None and bad.status == 400
        payload = _json_of(bad)
        assert payload["code"] == "invalid_fingerprint"
        # Input-free message: it reaches an HTTP client.
        assert raw not in payload["error"]

    def test_a_valid_fingerprint_passes_through_stripped(self) -> None:
        fp, bad = routes._validated_fp(_request(match_info={"fp": f"  {FP}  "}))
        assert (fp, bad) == (FP, None)


class TestRedaction:
    def test_a_failing_redactor_withholds_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-CLOSED, unlike the watcher log: the diff is still on disk and
        re-readable, so serving nothing beats serving something unscanned."""

        def _boom(_text: str) -> str:
            raise RuntimeError("redactor unavailable")

        monkeypatch.setattr(routes, "redact", _boom)
        assert routes._redact_for_display("secret") == "[diff withheld: redaction unavailable]"

    def test_tree_redaction_recurses_and_keeps_non_strings_as_themselves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        tree = {
            "flag": True,
            "count": 3,
            "nothing": None,
            "text": "a SECRET here",
            "rows": [{"note": "SECRET"}, 7, False],
        }
        assert routes._redact_tree(tree) == {
            "flag": True,
            "count": 3,
            "nothing": None,
            "text": "a *** here",
            "rows": [{"note": "***"}, 7, False],
        }


@pytest.mark.asyncio
class TestJsonBody:
    async def test_a_malformed_body_is_an_empty_patch(self) -> None:
        assert await routes._json_body(_request(body=_BAD_JSON)) == {}

    @pytest.mark.parametrize("body", [None, [1, 2], "text", 7])
    async def test_a_non_object_body_is_an_empty_patch(self, body: Any) -> None:
        assert await routes._json_body(_request(body=body)) == {}

    async def test_an_object_body_survives(self) -> None:
        assert await routes._json_body(_request(body={"branch": "x"})) == {"branch": "x"}


@pytest.mark.asyncio
class TestRunConflictGuards:
    @pytest.mark.parametrize(
        "status",
        [runner.STATUS_RUNNING, runner.STATUS_CALIBRATING, runner.STATUS_STOPPING],
    )
    async def test_an_active_status_is_a_409(
        self, supervisor: FakeSupervisor, status: str
    ) -> None:
        supervisor._status = status
        response = await routes._refuse_while_running("doing that would strand it.")
        assert response is not None and response.status == 409
        payload = _json_of(response)
        assert payload["code"] == "run_in_progress"
        assert status in payload["error"]
        assert "Stop the run first." in payload["error"]

    @pytest.mark.parametrize(
        "status", [runner.STATUS_IDLE, runner.STATUS_DONE, runner.STATUS_ERROR]
    )
    async def test_a_settled_status_lets_the_request_proceed(
        self, supervisor: FakeSupervisor, status: str
    ) -> None:
        supervisor._status = status
        assert await routes._refuse_while_running("anything.") is None


class TestSynchronousRunGuard:
    def test_it_agrees_with_the_async_form(self, supervisor: FakeSupervisor) -> None:
        """``_run_is_active`` exists because the async guard cannot be awaited from
        inside the worker thread that holds ``clone_lock`` — the in-lock recheck."""
        supervisor._status = runner.STATUS_RUNNING
        assert routes._run_is_active() is True
        supervisor._status = runner.STATUS_IDLE
        assert routes._run_is_active() is False


# ── config ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConfig:
    async def test_get_returns_an_empty_object_when_unset(self) -> None:
        assert _json_of(await routes._handle_get_config(_request())) == {}

    async def test_get_returns_the_stored_config(self, data_root: Path) -> None:
        _config(data_root, branch="origin/main", clone="/tmp/c")
        payload = _json_of(await routes._handle_get_config(_request()))
        assert payload["branch"] == "origin/main"

    async def test_put_applies_the_allowlist_and_reports_the_rest(
        self, data_root: Path
    ) -> None:
        """``clone``/``target_url`` decide which repository the agent is turned loose
        on, so the generic PUT must never be able to set them."""
        _config(data_root, clone="/keep/me", target_url="https://github.com/o/r")
        response = await routes._handle_put_config(
            _request(
                "PUT",
                body={
                    "branch": "origin/dev",
                    "maxCycles": 4,
                    "clone": "/evil/clone",
                    "target_url": "https://evil.example/x",
                    "unknown": 1,
                },
            )
        )
        assert response.status == 200
        payload = _json_of(response)
        assert payload["rejected"] == ["clone", "target_url", "unknown"]
        assert payload["config"]["branch"] == "origin/dev"
        assert payload["config"]["maxCycles"] == 4
        assert payload["config"]["clone"] == "/keep/me"
        # And it is durable, not just echoed.
        assert store.read_json(store.config_path(), {})["clone"] == "/keep/me"

    async def test_put_refuses_while_a_run_is_live(self, supervisor: FakeSupervisor) -> None:
        supervisor._status = runner.STATUS_RUNNING
        response = await routes._handle_put_config(_request("PUT", body={"branch": "b"}))
        assert response.status == 409
        assert "workspace" in _json_of(response)["error"]

    async def test_put_refuses_when_a_run_starts_while_it_waits(
        self, supervisor: FakeSupervisor
    ) -> None:
        """The pre-lock guard only proves no run was live on arrival; ``workspace_key``
        reads config FRESH, so a run starting mid-write moves its whole artifact set."""
        supervisor.status_queue = [runner.STATUS_IDLE, runner.STATUS_RUNNING]
        response = await routes._handle_put_config(_request("PUT", body={"branch": "b"}))
        assert response.status == 409
        payload = _json_of(response)
        assert payload["code"] == "run_in_progress"
        assert "while this config change was waiting" in payload["error"]


# ── repository setup ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSetupClone:
    URL = "https://github.com/owner/repo"

    def _stub_clone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        result: dict[str, Any] | None = None,
        err: str = "",
    ) -> list[str]:
        seen: list[str] = []

        def _fake(url: str, scratch: Path, **_kw: Any) -> tuple[dict, str]:
            seen.append(url)
            return (result or {}), err

        monkeypatch.setattr(clone_setup, "setup_safe_clone", _fake)
        return seen

    async def test_a_missing_url_is_a_400(self) -> None:
        response = await routes._handle_setup_clone(_request("POST", body={"url": "  "}))
        assert response.status == 400
        assert _json_of(response)["code"] == "url_required"

    async def test_passes_noncreating_scratch_path_to_setup(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = data_root / "scratch"
        monkeypatch.setattr(store, "scratch_path", lambda: scratch)
        monkeypatch.setattr(
            store,
            "scratch_dir",
            lambda: (_ for _ in ()).throw(AssertionError("scratch_dir created too early")),
        )

        def _fake(_url: str, received: Path, **_kw: Any) -> tuple[dict, str]:
            assert received == scratch
            assert not scratch.exists()
            return {}, "controlled refusal"

        monkeypatch.setattr(clone_setup, "setup_safe_clone", _fake)
        response = await routes._handle_setup_clone(_request("POST", body={"url": self.URL}))
        assert response.status == 400

    async def test_refuses_while_a_run_is_live(self, supervisor: FakeSupervisor) -> None:
        supervisor._status = runner.STATUS_RUNNING
        response = await routes._handle_setup_clone(_request("POST", body={"url": self.URL}))
        assert response.status == 409
        assert "retargeting" in _json_of(response)["error"]

    async def test_a_run_starting_while_it_waits_is_a_409_not_a_400(
        self, supervisor: FakeSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator's url was fine; the timing was not — so this must not read as
        ``invalid_repo_url``, which is what every other error string means here."""
        supervisor.status_queue = [runner.STATUS_IDLE, runner.STATUS_RUNNING]
        seen = self._stub_clone(monkeypatch, result={"clone": "/x"})
        response = await routes._handle_setup_clone(_request("POST", body={"url": self.URL}))
        assert response.status == 409
        payload = _json_of(response)
        assert payload["code"] == "run_in_progress"
        assert payload["ok"] is False
        assert seen == [], "the clone must not run once a run has started"

    async def test_a_rejected_url_is_a_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_clone(monkeypatch, err="not a GitHub repository url")
        response = await routes._handle_setup_clone(_request("POST", body={"url": "ssh://x"}))
        assert response.status == 400
        payload = _json_of(response)
        assert payload["code"] == "invalid_repo_url"
        assert payload["ok"] is False

    async def test_a_clone_whose_push_is_not_disabled_is_never_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_clone(monkeypatch, result={"clone": "/c", "push_disabled": False})
        response = await routes._handle_setup_clone(_request("POST", body={"url": self.URL}))
        assert response.status == 400
        assert _json_of(response)["code"] == "push_not_disabled"
        assert store.read_json(store.config_path(), {}) == {}

    async def test_a_successful_setup_persists_the_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_clone(
            monkeypatch,
            result={
                "clone": "/scratch/repo",
                "display": "owner/repo",
                "origin_url": "https://github.com/owner/repo.git",
                "push_disabled": True,
            },
        )
        response = await routes._handle_setup_clone(_request("POST", body={"url": self.URL}))
        assert response.status == 200
        config = _json_of(response)["config"]
        assert config["target_url"] == self.URL
        assert config["clone"] == "/scratch/repo"
        assert config["origin_url"] == "https://github.com/owner/repo.git"
        assert config["target_display"] == "owner/repo"

    async def test_retargeting_clears_the_branch_and_the_diff_scope(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A branch belongs to the repo it came from: carried across a retarget it
        names a ref the NEW clone does not have."""
        _config(
            data_root,
            target_url="https://github.com/owner/old",
            branch="origin/feature",
            scopeDiffBase="origin/main...HEAD",
            measureReps=9,
        )
        self._stub_clone(
            monkeypatch,
            result={"clone": "/c", "display": "owner/repo", "push_disabled": True},
        )
        response = await routes._handle_setup_clone(_request("POST", body={"url": self.URL}))
        config = _json_of(response)["config"]
        assert "branch" not in config
        assert "scopeDiffBase" not in config
        assert config["measureReps"] == 9, "unrelated settings survive a retarget"

    async def test_setting_the_same_url_again_keeps_the_branch(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(data_root, target_url=self.URL, branch="origin/feature")
        self._stub_clone(
            monkeypatch,
            result={"clone": "/c", "display": "owner/repo", "push_disabled": True},
        )
        response = await routes._handle_setup_clone(_request("POST", body={"url": self.URL}))
        assert _json_of(response)["config"]["branch"] == "origin/feature"


@pytest.mark.asyncio
class TestBranches:
    async def test_no_repo_configured_is_a_409(self) -> None:
        response = await routes._handle_branches(_request())
        assert response.status == 409
        assert _json_of(response)["code"] == "no_repo_configured"

    async def test_a_listing_failure_is_a_502_with_an_empty_list(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(data_root, clone="/scratch/repo")
        monkeypatch.setattr(
            clone_setup, "list_clone_branches", lambda _clone, **_kw: ([], "git ls-remote failed")
        )
        response = await routes._handle_branches(_request())
        assert response.status == 502
        payload = _json_of(response)
        assert payload["code"] == "branch_list_failed"
        assert payload["branches"] == []

    async def test_the_branches_are_returned(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(data_root, clone="/scratch/repo")
        seen: list[str] = []

        def _list(clone: Path, **_kw: Any) -> tuple[list[str], str]:
            seen.append(os.path.realpath(str(clone)))
            return ["origin/main", "origin/dev"], ""

        monkeypatch.setattr(clone_setup, "list_clone_branches", _list)
        response = await routes._handle_branches(_request())
        assert _json_of(response)["branches"] == ["origin/main", "origin/dev"]
        assert seen == [os.path.realpath("/scratch/repo")]


# ── pull-request status ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPrStatus:
    async def test_a_missing_url_query_is_a_400(self) -> None:
        response = await routes._handle_pr_status(_request("GET", "/pr-status"))
        assert response.status == 400
        assert _json_of(response)["code"] == "url_required"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("true", True), ("yes", True), ("0", False), ("", False)],
    )
    async def test_the_refresh_flag_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
    ) -> None:
        seen: list[bool] = []

        async def _fetch(url: str, *, refresh: bool = False) -> dict[str, Any]:
            seen.append(refresh)
            return {"ok": True, "url": url}

        monkeypatch.setattr(pr_checks, "fetch_pr_status", _fetch)
        response = await routes._handle_pr_status(
            _request("GET", f"/pr-status?url=https://github.com/o/r/pull/1&refresh={raw}")
        )
        assert response.status == 200
        assert seen == [expected]

    async def test_an_unavailable_status_is_a_redacted_502(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fetch(url: str, *, refresh: bool = False) -> dict[str, Any]:
            return {"ok": False, "error": "gh failed"}

        monkeypatch.setattr(pr_checks, "fetch_pr_status", _fetch)
        monkeypatch.setattr(routes, "redact", lambda text: f"[scanned]{text}")
        response = await routes._handle_pr_status(
            _request("GET", "/pr-status?url=https://github.com/o/r/pull/1")
        )
        assert response.status == 502
        payload = _json_of(response)
        assert payload["code"] == "pr_status_unavailable"
        assert payload["error"] == "[scanned]gh failed"


# ── chat-session links ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSessions:
    async def test_listing_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store.save_session("pr-o_r-1", {"title": "fix SECRET in f()"})
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        payload = _json_of(await routes._handle_list_sessions(_request()))
        assert payload["sessions"][0]["title"] == "fix *** in f()"

    async def test_an_unsafe_key_is_a_400(self) -> None:
        response = await routes._handle_get_session(
            _request(match_info={"key": "../../etc/passwd"})
        )
        assert response.status == 400
        assert _json_of(response)["code"] == "invalid_config"

    async def test_an_unlinked_key_reads_as_a_null_record(self) -> None:
        payload = _json_of(await routes._handle_get_session(_request(match_info={"key": "none"})))
        assert payload == {"session": None}

    async def test_saving_applies_the_field_allowlist(self) -> None:
        response = await routes._handle_save_session(
            _request(
                "PUT",
                match_info={"key": "pr-o_r-1"},
                body={"slot_key": "s1", "title": "t", "evil": "x", "status": "open"},
            )
        )
        record = _json_of(response)["session"]
        assert record["slot_key"] == "s1"
        assert "evil" not in record
        assert store.load_session("pr-o_r-1") == record

    async def test_saving_under_an_unsafe_key_is_a_400(self) -> None:
        response = await routes._handle_save_session(
            _request("PUT", match_info={"key": "bad/key"}, body={"slot_key": "s"})
        )
        assert response.status == 400
        assert _json_of(response)["code"] == "invalid_request"

    async def test_deleting_reports_whether_a_record_existed(self) -> None:
        store.save_session("pr-o_r-1", {"slot_key": "s"})
        first = _json_of(await routes._handle_delete_session(_request(match_info={"key": "pr-o_r-1"})))
        second = _json_of(
            await routes._handle_delete_session(_request(match_info={"key": "pr-o_r-1"}))
        )
        assert first == {"removed": True}
        assert second == {"removed": False}

    async def test_deleting_an_unsafe_key_is_a_400(self) -> None:
        response = await routes._handle_delete_session(_request(match_info={"key": "a b"}))
        assert response.status == 400
        assert _json_of(response)["code"] == "invalid_request"


# ── run artifacts ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRuler:
    async def test_an_absent_ruler_reads_as_uncalibrated(self) -> None:
        assert _json_of(await routes._handle_ruler(_request())) == {"status": "uncalibrated"}

    async def test_a_stored_ruler_is_returned(self) -> None:
        store.write_json_atomic(store.ruler_dir() / "ruler.json", {"status": "calibrated"})
        assert _json_of(await routes._handle_ruler(_request()))["status"] == "calibrated"


@pytest.mark.asyncio
class TestFindings:
    async def test_an_absent_ledger_is_an_empty_list(self) -> None:
        assert _json_of(await routes._handle_findings(_request())) == {"findings": []}

    async def test_one_row_per_fingerprint_newest_first(self) -> None:
        """The ledger is append-only, so a finding accretes a row per status change; a
        UI keyed on the fingerprint toggled the wrong row's panel when two shared an id."""
        _jsonl(
            store.ledger_path(),
            [
                {"fp": "aaa", "status": "seen", "ts": 1},
                {"fp": "bbb", "status": "seen", "ts": 2},
                {"fp": "aaa", "status": "failed_gate", "ts": 3},
            ],
        )
        rows = _json_of(await routes._handle_findings(_request()))["findings"]
        assert [(r["fp"], r["status"]) for r in rows] == [
            ("aaa", "failed_gate"),
            ("bbb", "seen"),
        ]

    async def test_a_torn_tail_line_never_hides_earlier_findings(self) -> None:
        _write(store.ledger_path(), '{"fp": "aaa", "status": "seen"}\n\n{"fp": "bb\n')
        rows = _json_of(await routes._handle_findings(_request()))["findings"]
        assert [r["fp"] for r in rows] == ["aaa"]

    async def test_a_row_without_a_fingerprint_is_kept_rather_than_dropped(self) -> None:
        _jsonl(store.ledger_path(), [{"status": "seen"}, {"status": "seen"}])
        rows = _json_of(await routes._handle_findings(_request()))["findings"]
        assert len(rows) == 2

    async def test_a_non_object_line_is_skipped(self) -> None:
        _write(store.ledger_path(), '[1, 2]\n{"fp": "aaa"}\n')
        rows = _json_of(await routes._handle_findings(_request()))["findings"]
        assert [r["fp"] for r in rows] == ["aaa"]

    async def test_the_note_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _jsonl(store.ledger_path(), [{"fp": "aaa", "note": "used SECRET"}])
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        rows = _json_of(await routes._handle_findings(_request()))["findings"]
        assert rows[0]["note"] == "used ***"


@pytest.mark.asyncio
class TestFindingDetail:
    async def test_an_invalid_fingerprint_never_reaches_a_path(self) -> None:
        response = await routes._handle_finding_detail(_request(match_info={"fp": "../x"}))
        assert response.status == 400

    async def test_an_unknown_fingerprint_is_a_404(self) -> None:
        response = await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        assert response.status == 404
        assert _json_of(response)["code"] == "finding_not_found"

    async def test_the_evidence_is_joined_from_every_place_the_run_wrote_it(self) -> None:
        _jsonl(
            store.ledger_path(),
            [
                {"fp": FP, "kind": "bug", "target": "src/search.py::root", "status": "seen"},
                {
                    "fp": FP,
                    "kind": "bug",
                    "target": "src/search.py::root",
                    "status": "filed",
                    "cr": "https://github.com/o/r/pull/3",
                    "note": "queued",
                    "ts": 42,
                },
            ],
        )
        cand_dir = store.results_dir() / "candidates"
        meta = cand_dir / "c1_search_py_root_abcd.json"
        store.write_json_atomic(
            meta,
            {
                "status": "kept",
                "proposal": {
                    "cand_id": "c1_search_py_root_abcd",
                    "candidate": {
                        "signature": "sig",
                        "hypothesis": "hyp",
                        "evidence": "ev",
                        "severity_note": "sev",
                        "blast_radius": "one function",
                        "reproducing_test": {"path": "test/t.py"},
                    },
                },
                "bug_gate": {"passed": True, "output": "1 failed"},
                "measurement": {"primary_delta": -0.2},
            },
        )
        _write(meta.with_suffix(".diff"), "--- a\n+++ b\n")
        _jsonl(
            store.results_dir() / "candidates.jsonl",
            [{"cand_id": "other"}, {"cand_id": "c1_search_py_root_abcd", "cycle": 6}],
        )
        _write(store.pr_queue_dir() / f"{FP}.pr.md", "# summary\nbody\n")
        store.write_json_atomic(store.results_dir() / "run.meta.json", {"run_id": "r1"})

        detail = _json_of(
            await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        )["finding"]
        assert detail["status"] == "filed"
        assert detail["pr"] == "https://github.com/o/r/pull/3"
        assert len(detail["history"]) == 2
        assert detail["candidate"]["cand_id"] == "c1_search_py_root_abcd"
        assert detail["candidate"]["hypothesis"] == "hyp"
        assert detail["gate"] == {"passed": True, "output": "1 failed"}
        assert detail["measurement"] == {"primary_delta": -0.2}
        assert detail["candidateStatus"] == "kept"
        assert detail["diff"] == "--- a\n+++ b\n"
        assert detail["diffTruncated"] is False
        assert detail["archive"] == {"cand_id": "c1_search_py_root_abcd", "cycle": 6}
        assert detail["prBody"] == "# summary\nbody\n"
        assert detail["run"] == {"run_id": "r1"}

    async def test_an_oversized_diff_is_truncated_and_says_so(self) -> None:
        _jsonl(store.ledger_path(), [{"fp": FP, "kind": "perf", "target": "a.py::f"}])
        _write(store.pr_queue_dir() / f"{FP}.diff", "x" * (routes._MAX_DIFF_CHARS + 10))
        detail = _json_of(
            await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        )["finding"]
        assert len(detail["diff"]) == routes._MAX_DIFF_CHARS
        assert detail["diffTruncated"] is True

    async def test_a_slug_mismatch_leaves_the_evidence_unjoined(self) -> None:
        """The cand_id embeds the file's BASENAME, so a nested target must still match."""
        _jsonl(store.ledger_path(), [{"fp": FP, "kind": "bug", "target": "a/b/other.py::g"}])
        store.write_json_atomic(
            store.results_dir() / "candidates" / "c1_search_py_root.json", {"status": "kept"}
        )
        detail = _json_of(
            await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        )["finding"]
        assert "candidate" not in detail

    async def test_a_non_object_candidate_file_is_skipped(self) -> None:
        _jsonl(store.ledger_path(), [{"fp": FP, "kind": "bug", "target": "s.py::f"}])
        _write(store.results_dir() / "candidates" / "c1_s_py_f.json", "[1]")
        detail = _json_of(
            await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        )["finding"]
        assert "candidate" not in detail

    async def test_a_torn_archive_line_is_skipped(self) -> None:
        _jsonl(store.ledger_path(), [{"fp": FP, "kind": "bug", "target": "s.py::f"}])
        store.write_json_atomic(
            store.results_dir() / "candidates" / "c1_s_py_f.json",
            {"status": "kept", "proposal": {"cand_id": "c1_s_py_f", "candidate": "not-a-dict"}},
        )
        _write(store.results_dir() / "candidates.jsonl", "not json\n\n")
        detail = _json_of(
            await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        )["finding"]
        assert "archive" not in detail
        assert detail["candidate"]["signature"] == ""

    async def test_a_torn_ledger_line_never_hides_the_finding(self) -> None:
        """The per-finding join reads the ledger line-by-line for the same reason the
        list does: one crash-truncated line must not lose the rest of the history."""
        _write(
            store.ledger_path(),
            "not json\n\n" + json.dumps({"fp": FP, "kind": "bug", "target": "s.py::f"}) + "\n",
        )
        detail = _json_of(
            await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        )["finding"]
        assert detail["kind"] == "bug"
        assert len(detail["history"]) == 1

    async def test_an_unreadable_evidence_file_is_omitted_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every evidence read is best-effort: the detail panel degrades to the fields
        it could load rather than 500-ing on one unreadable artifact."""
        _jsonl(store.ledger_path(), [{"fp": FP, "kind": "bug", "target": "s.py::f"}])
        meta = store.results_dir() / "candidates" / "c1_s_py_f.json"
        store.write_json_atomic(meta, {"status": "kept", "proposal": {"cand_id": "c1_s_py_f"}})
        _write(meta.with_suffix(".diff"), "--- a\n")
        _write(store.pr_queue_dir() / f"{FP}.pr.md", "# t\n")
        _write(store.pr_queue_dir() / f"{FP}.diff", "--- a\n")
        _unreadable(monkeypatch, ".diff", ".pr.md")
        detail = _json_of(
            await routes._handle_finding_detail(_request(match_info={"fp": FP}))
        )["finding"]
        assert "diff" not in detail
        assert "prBody" not in detail
        assert detail["candidateStatus"] == "kept"


# ── the manual draft path ────────────────────────────────────────────────────


@pytest.fixture()
def queued(data_root: Path) -> Path:
    """A finding with a body and a diff sitting in the PR queue, and a clone configured."""
    clone = data_root / "clone"
    clone.mkdir(parents=True, exist_ok=True)
    _config(data_root, clone=str(clone), branch="origin/work", githubUser="octo")
    _write(store.pr_queue_dir() / f"{FP}.pr.md", "# speed up f()\n\ndetail\n")
    _write(store.pr_queue_dir() / f"{FP}.diff", "--- a\n+++ b\n")
    return clone


@pytest.fixture()
def draft_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the whole git/gh boundary the draft path drives, recording argv."""
    state: dict[str, Any] = {
        "git": [],
        "materialize": {"ok": True, "base": "basesha"},
        "commit": {"ok": True},
        "recorded": [],
    }

    def _git(clone: Path, *args: str, **_kw: Any) -> Any:
        state["git"].append(tuple(args))
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(commit_mod, "_git", _git)
    monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: True)
    monkeypatch.setattr(clone_setup, "_push_disabled", lambda _clone: True)
    monkeypatch.setattr(
        commit_mod, "materialize_queued_diff", lambda **_kw: dict(state["materialize"])
    )
    monkeypatch.setattr(
        commit_mod, "commit_staged_for_draft", lambda **_kw: dict(state["commit"])
    )
    monkeypatch.setattr(clone_setup, "resolve_origin_url", lambda _cfg: "https://github.com/o/r")
    monkeypatch.setattr(
        routes,
        "ledger_admin_record",
        lambda fp, ref: state["recorded"].append((fp, ref)),
    )
    return state


@pytest.mark.asyncio
class TestDraftPr:
    async def test_an_invalid_fingerprint_is_a_400(self) -> None:
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": "a/b"}))
        assert response.status == 400

    async def test_refuses_while_a_run_is_live(self, supervisor: FakeSupervisor) -> None:
        """This route became clone-MUTATING once it materialized its queued diff, so it
        races the loop's own checkout/apply/push on the same branch."""
        supervisor._status = runner.STATUS_RUNNING
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 409
        assert "race the loop" in _json_of(response)["error"]

    async def test_a_run_starting_while_it_waits_is_a_409(
        self, supervisor: FakeSupervisor, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        supervisor.status_queue = [runner.STATUS_IDLE, runner.STATUS_RUNNING]
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 409
        assert _json_of(response)["code"] == "run_in_progress"
        assert draft_stubs["git"] == [], "nothing may touch the clone once a run is live"

    async def test_no_queued_change_is_a_400(self, data_root: Path) -> None:
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 400
        assert _json_of(response)["code"] == "draft_pr_failed"

    async def test_no_repository_configured_is_a_400(self, data_root: Path) -> None:
        _write(store.pr_queue_dir() / f"{FP}.pr.md", "# t\n")
        _write(store.pr_queue_dir() / f"{FP}.diff", "d\n")
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 400

    async def test_a_staging_failure_never_commits(
        self, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        draft_stubs["materialize"] = {"ok": False, "error": "diff does not apply"}
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 400
        assert FakeRecipe.instances == []

    async def test_a_commit_failure_rolls_the_branch_back(
        self, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        """Committing left the change on the configured branch, so the next run would
        treat an unfiled commit as its measurement baseline."""
        draft_stubs["commit"] = {"ok": False, "error": "nothing to commit"}
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 400
        assert ("reset", "--hard", "basesha") in draft_stubs["git"]
        assert FakeRecipe.instances == []

    async def test_a_successful_draft_records_the_reference_and_resets(
        self, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 200
        payload = _json_of(response)
        assert payload["ok"] is True
        assert payload["pr"] == FakeRecipe.ref
        assert draft_stubs["recorded"] == [(FP, FakeRecipe.ref)]
        # Unconditional: a successful draft otherwise leaves its commit checked out.
        assert ("reset", "--hard", "basesha") in draft_stubs["git"]
        recipe = FakeRecipe.instances[0]
        assert recipe.kwargs["user"] == "octo"
        assert recipe.kwargs["base_ref"] == "origin/work"
        assert recipe.draft_calls[0]["summary"] == "speed up f()"
        assert recipe.draft_calls[0]["fingerprint"] == FP

    async def test_a_degraded_reference_is_a_400_and_still_resets(
        self, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        FakeRecipe.ref = "queued://local"
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 400
        assert "still queued locally" in _json_of(response)["error"]
        assert draft_stubs["recorded"] == []
        assert ("reset", "--hard", "basesha") in draft_stubs["git"]

    async def test_a_failed_ledger_append_never_unpublishes_the_pull_request(
        self, queued: Path, draft_stubs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_fp: str, _ref: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(routes, "ledger_admin_record", _boom)
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 200
        assert ("reset", "--hard", "basesha") in draft_stubs["git"]

    async def test_an_unexpected_raise_still_rolls_back(
        self, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        FakeRecipe.raises = RuntimeError("gh exploded")
        with pytest.raises(RuntimeError, match="gh exploded"):
            await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert ("reset", "--hard", "basesha") in draft_stubs["git"]

    async def test_a_body_with_no_heading_falls_back_to_the_fingerprint(
        self, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        _write(store.pr_queue_dir() / f"{FP}.pr.md", "   \n")
        await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert FakeRecipe.instances[0].draft_calls[0]["summary"] == f"auto-improvement: {FP}"

    async def test_a_stage_without_a_base_never_resets_to_nothing(
        self, queued: Path, draft_stubs: dict[str, Any]
    ) -> None:
        """``reset --hard`` with an empty ref would discard the working tree, so the
        rollback is a no-op when the staging step reported no base."""
        draft_stubs["materialize"] = {"ok": True}
        response = await routes._handle_draft_pr(_request("POST", match_info={"fp": FP}))
        assert response.status == 200
        assert draft_stubs["git"] == []


class TestLedgerAdminRecord:
    """The ``filed`` marker must be shaped so ``LedgerEntry(**row)`` accepts it —
    a row spelled ``pr``, or missing ``kind``/``target``, is silently discarded and
    the loop then drafts a SECOND pull request for a change already filed."""

    def test_the_reference_goes_in_cr_and_kind_and_target_are_carried_over(self) -> None:
        _jsonl(
            store.ledger_path(),
            [{"fp": FP, "kind": "perf", "target": "s.py::f"}, {"fp": "other", "kind": "bug"}],
        )
        routes.ledger_admin_record(FP, "https://github.com/o/r/pull/4")
        rows = [
            json.loads(line)
            for line in store.ledger_path().read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows[-1]["cr"] == "https://github.com/o/r/pull/4"
        assert rows[-1]["status"] == "filed"
        assert rows[-1]["kind"] == "perf"
        assert rows[-1]["target"] == "s.py::f"
        assert "pr" not in rows[-1]

    def test_the_required_fields_are_present_even_with_no_prior_row(self) -> None:
        routes.ledger_admin_record(FP, "https://github.com/o/r/pull/5")
        row = json.loads(store.ledger_path().read_text(encoding="utf-8").splitlines()[-1])
        assert row["kind"] == ""
        assert row["target"] == ""

    def test_a_torn_prior_line_is_skipped(self) -> None:
        _write(store.ledger_path(), json.dumps({"fp": FP, "kind": "bug"}) + "\nnot json\n\n")
        routes.ledger_admin_record(FP, "https://github.com/o/r/pull/6")
        row = json.loads(store.ledger_path().read_text(encoding="utf-8").splitlines()[-1])
        assert row["kind"] == "bug"

    def test_an_unreadable_ledger_still_yields_a_well_shaped_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _jsonl(store.ledger_path(), [{"fp": FP, "kind": "perf", "target": "s.py::f"}])
        _unreadable(monkeypatch, "ledger.jsonl")
        routes.ledger_admin_record(FP, "https://github.com/o/r/pull/7")
        # Read back through ``open`` rather than undoing the patch: ``monkeypatch.undo``
        # would also revert the autouse data-root redirect.
        with store.ledger_path().open(encoding="utf-8") as handle:
            row = json.loads(handle.read().splitlines()[-1])
        assert row["cr"] == "https://github.com/o/r/pull/7"
        assert row["kind"] == ""
        assert row["target"] == ""


# ── watchers ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestWatcherListing:
    async def test_promotion_is_opt_in_and_off_by_default(
        self, registry: FakeRegistry
    ) -> None:
        """Starting a watcher runs an agent with an auto-approved shell against
        untrusted pull-request text, so a READ must not do it as a side effect."""
        registry.should = True
        payload = _json_of(await routes._handle_watchers(_request()))
        assert payload["reconcile"]["skipped"] == "watcherAutoStart is off"
        # Disk is still reclaimed: sweeping only deletes unclaimed scratch clones.
        assert payload["reconcile"]["orphanClonesRemoved"] == 3
        assert registry.reconcile_kwargs == {}

    async def test_the_rate_gate_still_sweeps_orphan_clones(
        self, data_root: Path, registry: FakeRegistry
    ) -> None:
        _config(data_root, watcherAutoStart=True)
        registry.should = False
        payload = _json_of(await routes._handle_watchers(_request()))
        assert payload["reconcile"]["skipped"] == "rate-limited"
        assert payload["reconcile"]["orphanClonesRemoved"] == 3

    async def test_the_reconcile_sweep_only_considers_watchable_reconcilable_prs(
        self, data_root: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(data_root, watcherAutoStart="yes")
        registry.should = True
        findings = [
            {"fp": "a", "status": "filed", "pr": "https://github.com/o/r/pull/1"},
            {"fp": "b", "status": "seen", "pr": "https://github.com/o/r/pull/2"},
            {"fp": "c", "status": "committed", "cr": "queued-only"},
        ]
        monkeypatch.setattr(progress, "read_findings", lambda: findings)
        monkeypatch.setattr(
            pr_watchers, "is_watchable_pr", lambda url: url.startswith("https://github.com/")
        )
        fetched: list[str] = []

        async def _fetch(url: str, *, refresh: bool = False) -> dict[str, Any]:
            fetched.append(url)
            return {"ok": True, "url": url}

        monkeypatch.setattr(pr_checks, "fetch_pr_status", _fetch)
        payload = _json_of(await routes._handle_watchers(_request()))
        assert fetched == ["https://github.com/o/r/pull/1"]
        assert registry.reconcile_kwargs["force"] is True
        assert payload["reconcile"]["promoted"] == 0
        assert payload["reconcile"]["orphanClonesRemoved"] == 3

    async def test_a_failed_status_fetch_is_not_actionable(
        self, data_root: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(data_root, watcherAutoStart=True)
        registry.should = True
        monkeypatch.setattr(
            progress,
            "read_findings",
            lambda: [{"fp": "a", "status": "filed", "pr": "https://github.com/o/r/pull/1"}],
        )
        monkeypatch.setattr(pr_watchers, "is_watchable_pr", lambda _url: True)

        async def _fetch(url: str, *, refresh: bool = False) -> dict[str, Any]:
            raise RuntimeError("gh timed out")

        monkeypatch.setattr(pr_checks, "fetch_pr_status", _fetch)
        await routes._handle_watchers(_request())
        status_for = registry.reconcile_kwargs["status_for"]
        assert status_for("https://github.com/o/r/pull/1") == {}

    async def test_a_sweep_failure_is_never_the_callers_problem(
        self, data_root: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config(data_root, watcherAutoStart=True)
        registry.should = True

        def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("ledger unreadable")

        monkeypatch.setattr(progress, "read_findings", _boom)
        payload = _json_of(await routes._handle_watchers(_request()))
        assert payload["reconcile"] == {"skipped": "error"}
        assert payload["sessions"] == []

    async def test_the_session_snapshot_is_redacted(
        self, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured before the fix: a ``target`` carrying a credential-shaped value
        reached the browser verbatim."""
        registry.sessions = [{"fp": "a", "target": "src/m.py::SECRET", "flag": True}]
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        payload = _json_of(await routes._handle_watchers(_request()))
        assert payload["sessions"][0]["target"] == "src/m.py::***"
        assert payload["sessions"][0]["flag"] is True


@pytest.mark.asyncio
class TestWatcherStart:
    async def test_an_invalid_fingerprint_is_a_400(self) -> None:
        response = await routes._handle_watcher_start(_request("POST", match_info={"fp": ".."}))
        assert response.status == 400

    async def test_an_unknown_finding_is_a_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(progress, "read_findings", lambda: [])
        response = await routes._handle_watcher_start(_request("POST", match_info={"fp": FP}))
        assert response.status == 404
        assert _json_of(response)["code"] == "finding_not_found"

    async def test_a_queued_only_change_has_nothing_to_watch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(progress, "read_findings", lambda: [{"fp": FP, "pr": ""}])
        monkeypatch.setattr(pr_watchers, "is_watchable_pr", lambda _url: False)
        response = await routes._handle_watcher_start(_request("POST", match_info={"fp": FP}))
        assert response.status == 409
        payload = _json_of(response)
        assert payload["code"] == "no_pr_to_watch"
        assert "pr=none" in payload["error"]

    async def test_starting_binds_the_gateway_loop_and_passes_the_config(
        self, data_root: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The registry's own fallback uses ``get_running_loop()``, and the start body
        runs in a worker thread where there is none — which would refuse the start."""
        _config(data_root, branch="origin/work", clone="/scratch/repo")
        monkeypatch.setattr(
            progress,
            "read_findings",
            lambda: [
                {
                    "fp": FP,
                    "pr": "https://github.com/o/r/pull/1",
                    "kind": "bug",
                    "target": "s.py::f",
                }
            ],
        )
        monkeypatch.setattr(pr_watchers, "is_watchable_pr", lambda _url: True)
        response = await routes._handle_watcher_start(_request("POST", match_info={"fp": FP}))
        assert response.status == 200
        assert registry.loops == [asyncio.get_running_loop()]
        assert registry.started[0]["base_ref"] == "origin/work"
        assert registry.started[0]["clone"] == "/scratch/repo"
        assert _json_of(response)["session"]["status"] == "nudging"

    async def test_a_registry_without_a_snapshot_falls_back_to_the_state(
        self, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry.status_result = None
        monkeypatch.setattr(
            progress, "read_findings", lambda: [{"fp": FP, "pr": "https://github.com/o/r/pull/1"}]
        )
        monkeypatch.setattr(pr_watchers, "is_watchable_pr", lambda _url: True)
        response = await routes._handle_watcher_start(_request("POST", match_info={"fp": FP}))
        assert _json_of(response)["session"] == {"fp": FP, "status": "starting"}


@pytest.mark.asyncio
class TestWatcherStopAndLog:
    async def test_stopping_reports_the_fingerprint(self, registry: FakeRegistry) -> None:
        response = await routes._handle_watcher_stop(_request("POST", match_info={"fp": FP}))
        assert _json_of(response) == {"stopped": True, "fp": FP}
        assert registry.stopped == [FP]

    async def test_stopping_an_invalid_fingerprint_is_a_400(
        self, registry: FakeRegistry
    ) -> None:
        response = await routes._handle_watcher_stop(_request("POST", match_info={"fp": "a b"}))
        assert response.status == 400
        assert registry.stopped == []

    @pytest.mark.parametrize(
        ("raw", "expected"), [("", 0), ("garbage", 0), ("12", 12), ("-3", -3)]
    )
    async def test_the_since_cursor_defaults_to_zero_when_unparseable(
        self, registry: FakeRegistry, raw: str, expected: int
    ) -> None:
        response = await routes._handle_watcher_log(
            _request("GET", f"/watchers/{FP}/log?since={raw}", match_info={"fp": FP})
        )
        assert response.status == 200
        assert registry.log_calls == [(FP, expected)]

    async def test_an_invalid_fingerprint_never_reaches_the_registry(
        self, registry: FakeRegistry
    ) -> None:
        response = await routes._handle_watcher_log(_request(match_info={"fp": "../x"}))
        assert response.status == 400
        assert registry.log_calls == []


# ── profiles ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestProfiles:
    async def test_the_listing_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            profile_normalize, "list_profiles", lambda: [{"fp": "a", "root": "SECRET"}]
        )
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        payload = _json_of(await routes._handle_profiles(_request()))
        assert payload["profiles"][0]["root"] == "***"

    async def test_an_invalid_fingerprint_is_a_400(self) -> None:
        response = await routes._handle_profile(_request(match_info={"fp": "a/b"}))
        assert response.status == 400

    async def test_a_rejecting_reader_is_a_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _read(_fp: str) -> dict[str, Any]:
            raise ValueError("bad fingerprint")

        monkeypatch.setattr(profile_normalize, "read_profile", _read)
        response = await routes._handle_profile(_request(match_info={"fp": FP}))
        assert response.status == 400
        assert _json_of(response)["code"] == "invalid_request"

    async def test_a_missing_profile_is_a_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(profile_normalize, "read_profile", lambda _fp: None)
        response = await routes._handle_profile(_request(match_info={"fp": FP}))
        assert response.status == 404
        assert _json_of(response)["code"] == "profile_not_found"

    async def test_the_frame_tree_is_scanned_before_it_reaches_the_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            profile_normalize,
            "read_profile",
            lambda _fp: {"name": "SECRET", "children": [{"name": "ok", "self": 0.5}]},
        )
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        payload = _json_of(await routes._handle_profile(_request(match_info={"fp": FP})))
        assert payload["profile"]["name"] == "***"
        assert payload["profile"]["children"][0]["self"] == 0.5


# ── ledger maintenance ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestForgetAndPurge:
    @pytest.mark.parametrize("handler_name", ["_handle_forget", "_handle_purge"])
    async def test_an_invalid_fingerprint_is_a_400(self, handler_name: str) -> None:
        handler = getattr(routes, handler_name)
        response = await handler(_request("POST", match_info={"fp": "../x"}))
        assert response.status == 400

    @pytest.mark.parametrize(
        ("handler_name", "target"), [("_handle_forget", "forget"), ("_handle_purge", "purge")]
    )
    async def test_a_rejecting_helper_is_a_400(
        self, monkeypatch: pytest.MonkeyPatch, handler_name: str, target: str
    ) -> None:
        def _raise(_fp: str) -> dict[str, Any]:
            raise ValueError("fingerprint is not a valid identifier")

        monkeypatch.setattr(ledger_admin, target, _raise)
        response = await getattr(routes, handler_name)(_request("POST", match_info={"fp": FP}))
        assert response.status == 400
        assert _json_of(response)["code"] == "invalid_request"

    @pytest.mark.parametrize(
        ("handler_name", "target"), [("_handle_forget", "forget"), ("_handle_purge", "purge")]
    )
    async def test_success_passes_the_result_through(
        self, monkeypatch: pytest.MonkeyPatch, handler_name: str, target: str
    ) -> None:
        monkeypatch.setattr(ledger_admin, target, lambda fp: {"ok": True, "fp": fp})
        response = await getattr(routes, handler_name)(_request("POST", match_info={"fp": FP}))
        assert response.status == 200
        assert _json_of(response) == {"ok": True, "fp": FP}

    @pytest.mark.parametrize(
        ("handler_name", "target"), [("_handle_forget", "forget"), ("_handle_purge", "purge")]
    )
    async def test_a_missing_finding_is_a_redacted_404(
        self, monkeypatch: pytest.MonkeyPatch, handler_name: str, target: str
    ) -> None:
        monkeypatch.setattr(
            ledger_admin, target, lambda _fp: {"ok": False, "error": "no such SECRET"}
        )
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        response = await getattr(routes, handler_name)(_request("POST", match_info={"fp": FP}))
        assert response.status == 404
        assert _json_of(response)["error"] == "no such ***"

    @pytest.mark.parametrize(
        ("raw", "expected"), [("1", True), ("true", True), ("yes", True), ("0", False)]
    )
    async def test_purge_dead_treats_artifact_removal_as_opt_in(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
    ) -> None:
        seen: list[bool] = []

        def _sweep(*, remove_artifacts: bool = False) -> dict[str, Any]:
            seen.append(remove_artifacts)
            return {"purged": []}

        monkeypatch.setattr(ledger_admin, "purge_dead", _sweep)
        response = await routes._handle_purge_dead(
            _request("POST", f"/findings/purge-dead?artifacts={raw}")
        )
        assert response.status == 200
        assert seen == [expected]

    async def test_purge_dead_defaults_to_keeping_the_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []
        monkeypatch.setattr(
            ledger_admin,
            "purge_dead",
            lambda *, remove_artifacts=False: (seen.append(remove_artifacts), {"purged": []})[1],
        )
        await routes._handle_purge_dead(_request("POST", "/findings/purge-dead"))
        assert seen == [False]


# ── calibrate / commit ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCalibrate:
    async def test_no_repository_configured_is_a_409(self) -> None:
        response = await routes._handle_calibrate(_request("POST"))
        assert response.status == 409
        assert _json_of(response)["code"] == "no_repo_configured"

    async def test_calibration_starts_from_the_config_on_disk(
        self, data_root: Path, supervisor: FakeSupervisor
    ) -> None:
        _config(data_root, clone="/scratch/repo")
        response = await routes._handle_calibrate(_request("POST"))
        assert response.status == 200
        assert _json_of(response)["clone"] == "/scratch/repo"
        assert "calibrate" in supervisor.calls

    async def test_a_watcher_conflict_is_a_409(
        self, data_root: Path, supervisor: FakeSupervisor
    ) -> None:
        _config(data_root, clone="/scratch/repo")
        supervisor.calibrate_raises = RuntimeError("a watcher owns the clone")
        response = await routes._handle_calibrate(_request("POST"))
        assert response.status == 409
        payload = _json_of(response)
        assert payload["code"] == "watcher_conflict"
        assert payload["error"] == "a watcher owns the clone"


@pytest.mark.asyncio
class TestCommit:
    async def test_an_invalid_fingerprint_is_a_400(self) -> None:
        response = await routes._handle_commit(_request("POST", match_info={"fp": "a/b"}))
        assert response.status == 400

    async def test_refuses_while_a_run_is_live(self, supervisor: FakeSupervisor) -> None:
        supervisor._status = runner.STATUS_CALIBRATING
        response = await routes._handle_commit(_request("POST", match_info={"fp": FP}))
        assert response.status == 409
        assert "race the loop" in _json_of(response)["error"]

    async def test_a_run_starting_while_it_waits_is_a_409(
        self, supervisor: FakeSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supervisor.status_queue = [runner.STATUS_IDLE, runner.STATUS_RUNNING]
        called: list[str] = []
        monkeypatch.setattr(
            commit_mod, "commit_finding", lambda fp: called.append(fp) or {"ok": True}
        )
        response = await routes._handle_commit(_request("POST", match_info={"fp": FP}))
        assert response.status == 409
        assert _json_of(response)["code"] == "run_in_progress"
        assert called == []

    async def test_a_landed_commit_supersedes_the_filed_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``filed`` is what drives the watchers and the commit button, so without
        this the operator is invited to commit a change already on the branch."""
        monkeypatch.setattr(
            commit_mod,
            "commit_finding",
            lambda fp: {"ok": True, "fp": fp, "branch": "work", "sha": "deadbee"},
        )
        recorded: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            ledger_admin,
            "record_committed",
            lambda fp, *, branch, sha: recorded.append((fp, branch, sha)),
        )
        response = await routes._handle_commit(_request("POST", match_info={"fp": FP}))
        assert response.status == 200
        assert recorded == [(FP, "work", "deadbee")]

    async def test_a_failed_commit_is_a_redacted_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            commit_mod, "commit_finding", lambda _fp: {"ok": False, "error": "fatal: SECRET"}
        )
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        response = await routes._handle_commit(_request("POST", match_info={"fp": FP}))
        assert response.status == 400
        payload = _json_of(response)
        assert payload["code"] == "request_failed"
        assert payload["error"] == "fatal: ***"


# ── progress / deps / events / health ────────────────────────────────────────


@pytest.mark.asyncio
class TestReadOnlySurface:
    async def test_progress_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(progress, "read_progress", lambda: {"points": [{"pr": "SECRET"}]})
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        payload = _json_of(await routes._handle_progress(_request()))
        assert payload["points"][0]["pr"] == "***"

    async def test_deps_are_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "check_deps", lambda: {"ok": True, "deps": [], "blocking": []})
        assert _json_of(await routes._handle_deps(_request()))["ok"] is True

    async def test_a_successful_install_is_a_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "install_deps", lambda: {"ok": True, "installed": ["ruff"]})
        response = await routes._handle_deps_install(_request("POST"))
        assert response.status == 200
        assert _json_of(response)["installed"] == ["ruff"]

    async def test_a_failed_install_is_a_redacted_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deps, "install_deps", lambda: {"ok": False, "error": "pip SECRET"})
        monkeypatch.setattr(routes, "redact", lambda text: text.replace("SECRET", "***"))
        response = await routes._handle_deps_install(_request("POST"))
        assert response.status == 500
        payload = _json_of(response)
        assert payload["code"] == "operation_failed"
        assert payload["error"] == "pip ***"

    async def test_the_event_stream_is_delegated_to_the_sse_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[web.Request] = []

        async def _stream(request: web.Request) -> web.StreamResponse:
            seen.append(request)
            return web.json_response({"streamed": True})

        monkeypatch.setattr(sse, "stream", _stream)
        request = _request("GET", "/events")
        response = await routes._handle_events(request)
        assert seen == [request]
        assert _json_of(response) == {"streamed": True}

    async def test_health_names_the_app(self) -> None:
        assert _json_of(await routes._handle_health(_request())) == {
            "ok": True,
            "app": store.APP_NAME,
        }


# ── the run engine ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRunEngine:
    async def test_the_body_is_ignored_and_the_config_on_disk_is_used(
        self, data_root: Path, supervisor: FakeSupervisor
    ) -> None:
        """A Start click must not be able to smuggle in a different repo or a wider
        budget than the config endpoints allow."""
        _config(data_root, clone="/scratch/repo", maxCycles=2)
        response = await routes._handle_run_start(
            _request("POST", body={"clone": "/evil", "maxCycles": 999})
        )
        assert response.status == 200
        used = _json_of(response)["config"]
        assert used["clone"] == "/scratch/repo"
        assert used["maxCycles"] == 2

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("a run is already in progress"),
            ValueError("no repository configured"),
            PermissionError("clone push is not disabled"),
        ],
    )
    async def test_every_state_conflict_is_a_409_with_the_actionable_message(
        self, supervisor: FakeSupervisor, exc: BaseException
    ) -> None:
        supervisor.start_raises = exc
        response = await routes._handle_run_start(_request("POST"))
        assert response.status == 409
        payload = _json_of(response)
        assert payload["code"] == "session_conflict"
        assert payload["error"] == str(exc)

    async def test_a_probe_launcher_crash_gets_its_own_code(
        self, supervisor: FakeSupervisor
    ) -> None:
        """A crashed isolation probe is a sandbox failure, not a state conflict:
        a UI branching on `code` must not render the push-isolation guidance
        for it (#8151). It subclasses RuntimeError, so without the dedicated
        clause it would fall into `session_conflict`."""
        supervisor.start_raises = clone_setup.IsolationProbeError(
            "ModuleNotFoundError: No module named 'platform'"
        )
        response = await routes._handle_run_start(_request("POST"))
        assert response.status == 409
        payload = _json_of(response)
        assert payload["code"] == "sandbox_launcher_failed"
        assert "sandbox launcher" in payload["error"]
        assert "push is not disabled" not in payload["error"]

    async def test_status_reads_in_memory_state_only(self, supervisor: FakeSupervisor) -> None:
        supervisor._status = runner.STATUS_RUNNING
        payload = _json_of(await routes._handle_run_status(_request()))
        assert payload == {"status": runner.STATUS_RUNNING}

    async def test_stop_is_delegated_to_the_supervisor(
        self, supervisor: FakeSupervisor
    ) -> None:
        payload = _json_of(await routes._handle_run_stop(_request("POST")))
        assert payload == {"status": runner.STATUS_STOPPING}
        assert "stop" in supervisor.calls


# ── registration ─────────────────────────────────────────────────────────────


class TestRegisterRoutes:
    def test_every_registered_route_is_behind_the_enabled_gate(self) -> None:
        """A route added without the gate would be reachable while the app is
        disabled, and nothing else in the system would notice."""
        app = web.Application()
        routes.register_routes(app)
        resources = [r for r in app.router.routes() if r.resource is not None]
        assert len(resources) >= 30
        for route in resources:
            assert getattr(route.handler, "__wrapped__", None) is not None, (
                f"{route.resource} is not wrapped by _require_enabled"
            )

    def test_the_prefix_is_the_app_scoped_api_path(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        paths = {
            r.resource.canonical for r in app.router.routes() if r.resource is not None
        }
        assert f"/api/apps/{store.APP_NAME}/health" in paths
        for path in paths:
            assert path.startswith(f"/api/apps/{store.APP_NAME}/")

    def test_the_lifecycle_hooks_are_registered_by_name(self) -> None:
        """Selected by NAME, not by list position: the gateway appends its own
        startup entries, which silently repoints an index-based lookup."""
        app = web.Application()
        routes.register_routes(app)
        assert "_bind_watcher_loop" in {getattr(h, "__name__", "") for h in app.on_startup}
        assert "_stop_watchers" in {getattr(h, "__name__", "") for h in app.on_cleanup}

    def test_a_failing_layout_never_breaks_gateway_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(store, "ensure_layout", _boom)
        app = web.Application()
        routes.register_routes(app)
        assert [r for r in app.router.routes() if r.resource is not None]

    @pytest.mark.asyncio
    async def test_the_startup_hook_binds_the_watcher_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = web.Application()
        routes.register_routes(app)
        bound: list[Any] = []
        monkeypatch.setattr(pr_watchers, "attach_loop", lambda loop: bound.append(loop))
        hook = next(h for h in app.on_startup if getattr(h, "__name__", "") == "_bind_watcher_loop")
        await hook(app)
        assert bound == [asyncio.get_running_loop()]

    @pytest.mark.asyncio
    async def test_a_failing_startup_hook_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = web.Application()
        routes.register_routes(app)

        def _boom(_loop: Any) -> None:
            raise RuntimeError("no loop")

        monkeypatch.setattr(pr_watchers, "attach_loop", _boom)
        hook = next(h for h in app.on_startup if getattr(h, "__name__", "") == "_bind_watcher_loop")
        await hook(app)

    @pytest.mark.asyncio
    async def test_the_cleanup_hook_stops_every_watcher(
        self, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = web.Application()
        routes.register_routes(app)
        stopped: list[bool] = []
        registry.stop_all = lambda: stopped.append(True)  # type: ignore[attr-defined]
        hook = next(h for h in app.on_cleanup if getattr(h, "__name__", "") == "_stop_watchers")
        await hook(app)
        assert stopped == [True]

    @pytest.mark.asyncio
    async def test_a_failing_cleanup_hook_never_blocks_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = web.Application()
        routes.register_routes(app)

        def _boom() -> Any:
            raise RuntimeError("registry gone")

        monkeypatch.setattr(pr_watchers, "get_registry", _boom)
        hook = next(h for h in app.on_cleanup if getattr(h, "__name__", "") == "_stop_watchers")
        await hook(app)
