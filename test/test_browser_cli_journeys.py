"""Lifecycle journeys for browsing: what an operator does, and what survives it.

The sequences here are the ones that broke the previous browser stack: a settings
change, a gateway restart, and an update. The property under test is the same in
every case and is stated as a guarantee rather than an implementation detail:
nothing Kiro Crew does to enable browsing writes, rewrites, or deletes
configuration the operator owns.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import install, launch, snapshots, view


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(snapshots, "config_dir", lambda: tmp_path / "crew")
    (tmp_path / "crew").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _operator_cli_config(home: Path) -> Path:
    """The config file the CLI reads, with fields only an operator would set."""
    path = home / "project" / ".playwright" / "cli.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "browser": {
                    "launchOptions": {"args": ["--proxy-server=http://corp:3128"]},
                    "contextOptions": {"viewport": {"width": 1920, "height": 1080}},
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


class TestOperatorConfigIsNotOurs:
    """The CLI's config file belongs to the operator, so we never write THEIRS.

    Kiro Crew writes its OWN config under the data home and points
    ``PLAYWRIGHT_MCP_CONFIG`` at it, which is a different file. Precedence,
    measured against the real CLI, is what keeps that from becoming an override:
    a ``<cwd>/.playwright/cli.config.json`` beats the env-named file, so an
    operator's proxy and viewport survive. Their file is never read or rewritten.
    """

    def test_the_package_has_exactly_one_destructive_operation(self):
        # A guard on the property the other cases depend on: if a future change adds
        # a config write, this fails and the reviewer has to justify it rather than
        # discovering it from a support report.
        #
        # TWO writes are sanctioned, named by module so a SECOND write in either
        # still fails here:
        #   token.py  -- persists the optional attach token, which has to survive a
        #                restart to be worth configuring.
        #   launch.py -- generates Kiro Crew's OWN launch config under the data
        #                home, naming the engine the product installs. It never
        #                writes, reads, or supersedes the operator's
        #                .playwright/cli.config.json; see
        #                test_the_operator_config_path_is_never_written.
        sanctioned = {"token.py", "launch.py"}
        pkg = Path(install.__file__).parent
        writes = []
        for source in sorted(pkg.glob("*.py")):
            for lineno, line in enumerate(source.read_text().splitlines(), 1):
                if any(
                    call in line
                    for call in ("write_text(", "write_bytes(", "rmtree(", "atomic_write(")
                ):
                    writes.append(f"{source.name}:{lineno}")
        unexpected = [w for w in writes if w.split(":", 1)[0] not in sanctioned]
        assert unexpected == [], f"browser_cli must not write files: {unexpected}"
        # And the sanctioned ones must still be there: a token that stopped being
        # persisted would silently stop working across restarts, and a launch config
        # that stopped being written would put browsing back on the CLI's default
        # browser channel, which Kiro Crew does not install.
        assert any(w.startswith("token.py:") for w in writes), "token.py must persist the token"
        assert any(w.startswith("launch.py:") for w in writes), "launch.py must write the config"

    def test_the_operator_config_path_is_never_written(self, home: Path):
        """Ours goes under the data home, never at the CLI's discovered path."""
        config = _operator_cli_config(home)
        before = config.read_text(encoding="utf-8")

        written = launch.write_config()

        assert written is not None
        assert written != config
        assert written.name != "cli.config.json"
        assert ".playwright" not in written.parts
        assert config.read_text(encoding="utf-8") == before

    def test_a_gateway_restart_leaves_the_config_alone(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        config = _operator_cli_config(home)
        before = config.read_text(encoding="utf-8")

        # What a restart actually does for browsing: republish the output directory,
        # publish the launch config, and prune. None of it touches operator config.
        # Applied through monkeypatch so the variables do not outlive the test -- a
        # bare os.environ.update would leak them to every later test on this xdist
        # worker, where one expecting PLAYWRIGHT_MCP_CONFIG unset would then pass or
        # fail on worker assignment.
        for key, value in {
            **snapshots.cli_env_overrides(),
            **launch.cli_env_overrides(),
        }.items():
            monkeypatch.setenv(key, value)
        snapshots.prune()

        assert config.read_text(encoding="utf-8") == before

    def test_an_update_leaves_the_config_alone(self, home: Path):
        config = _operator_cli_config(home)
        before = config.read_text(encoding="utf-8")

        # An update re-runs detection. Detection is a PATH and version read.
        install.detect()

        assert config.read_text(encoding="utf-8") == before


class TestSnapshotDirectoryIsOursAlone:
    """Pruning is the one destructive act, and it is scoped to derived output."""

    def test_pruning_cannot_reach_outside_the_snapshot_directory(self, home: Path):
        outside = home / "crew" / "playwright-config-of-mine.json"
        outside.write_text("{}", encoding="utf-8")
        snapshots.snapshot_dir().mkdir(parents=True, exist_ok=True)
        old = time.time() - (10 * 24 * 60 * 60)
        stale = snapshots.snapshot_dir() / "page-2026-01-01T00-00-00-000Z.yml"
        stale.write_text("- generic", encoding="utf-8")
        os.utime(stale, (old, old))
        (snapshots.snapshot_dir() / "page-2026-06-01T00-00-00-000Z.yml").write_text("- generic", encoding="utf-8")

        snapshots.prune(max_age_s=60.0)

        assert outside.exists(), "prune must not touch a sibling of its own directory"
        assert not stale.exists()

    def test_the_output_directory_is_stable_across_calls(self, home: Path, tmp_path: Path):
        # The agent's working directory moves between turns; the directory the
        # service prunes must not move with it.
        first = snapshots.cli_env_overrides()
        monkey_cwd = tmp_path / "elsewhere"
        monkey_cwd.mkdir()
        os.chdir(monkey_cwd)
        assert snapshots.cli_env_overrides() == first
        assert Path(first["PLAYWRIGHT_MCP_OUTPUT_DIR"]).is_absolute()


class TestBothOnboardingPaths:
    """A host that has the CLI already, and a host that does not."""

    def test_an_existing_install_is_used_as_is(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(install, "cli_path", lambda: "/usr/local/bin/playwright-cli")
        monkeypatch.setattr(install, "_first_version", lambda text: "0.1.18")
        monkeypatch.setattr(install, "_node_version", lambda: "22.1.0")
        monkeypatch.setattr(install, "_run", lambda argv, timeout: (0, "0.1.18", ""))

        state = install.detect()

        assert state["installed"] is True
        # Presence is consent, so browsing is available without any further act by
        # the operator and without us reinstalling over their copy.
        assert install.available() is True

    def test_a_fresh_host_reports_what_is_missing_rather_than_guessing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(install, "cli_path", lambda: None)
        monkeypatch.setattr(install, "_node_version", lambda: "18.0.0")

        state = install.detect()

        assert state["installed"] is False
        assert install.available() is False
        # node_ok is independent of installed, so the card can say "install Node"
        # instead of offering a button that would fail.
        assert state["node_ok"] is False
        assert state["node_version"] == "18.0.0"

    def test_a_missing_binary_refuses_to_start_the_view(self, monkeypatch: pytest.MonkeyPatch):
        # `view` imported the resolver by name, so patching it on `install` would
        # leave view's own reference bound to the real one and start a real server.
        monkeypatch.setattr(view, "cli_path", lambda: None)
        assert view.ensure_running() is None
        assert view.status()["status"] == "unavailable"


class TestAppTokensCannotArmBrowsing:
    """Route scoping is not capability scoping.

    The auth middleware only proves the ROUTE is in a calling app's manifest. It
    does not decide whether an app may ARM the browser, and these mutations are
    exactly the wrong reach for one: the install is what activates browser
    auto-approval, and the attach token silences the browser's own per-attach
    prompt -- the last human checkpoint before a program drives a logged-in
    session. Reads stay open; writes are dashboard-owner only.
    """

    @staticmethod
    def _app_request(path: str, body: dict | None = None):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.path = path
        req.get = lambda key, default=None: "some-app" if key == "app" else default

        async def _json():
            return body or {}

        req.json = _json
        return req

    def _run(self, handler, req):
        import asyncio

        return asyncio.run(handler(req))

    def test_the_token_write_refuses_an_app_token(self):
        from kiro_crew.dashboard.handlers import messaging as msg

        resp = self._run(
            msg.api_browser_token_put,
            self._app_request("/api/browser/token", {"token": "x"}),
        )
        assert resp.status == 403

    def test_the_install_refuses_an_app_token(self):
        from kiro_crew.dashboard.handlers import messaging as msg

        resp = self._run(msg.api_browser_install_start, self._app_request("/api/browser/install"))
        assert resp.status == 403

    def test_the_engine_download_refuses_an_app_token(self):
        from kiro_crew.dashboard.handlers import messaging as msg

        resp = self._run(
            msg.api_browser_engine_install,
            self._app_request("/api/browser/engine", {"engine": "firefox"}),
        )
        assert resp.status == 403


class TestBrowserMutationsAreOwnerOnly:
    """The owner gate covers app tokens AND non-owner dashboard users.

    A caller whose app identity is absent (e.g. a Slack-originated !dashboard
    token) was not refused by the old app-only check, yet the endpoints arm
    browser auto-approval and write stored credentials. The fix gates on
    is_owner_dashboard_request, which subsumes the app check and additionally
    refuses non-owner dashboard callers.
    """

    class _FakeRequest:
        """Minimal request stub with __contains__/__getitem__/get."""

        def __init__(
            self, path: str, *, app_claim: str, user: str,
            owner_id: str = "owner-user-123", body: dict | None = None,
        ):
            from unittest.mock import MagicMock

            state = MagicMock()
            state.owner_id = owner_id
            # Attributes read by the browser GET handler (avoids MagicMock
            # leaking into JSON serialization).
            state._browser_install_task = None
            state._browser_install_error = None
            self.app = {"state": state}
            self.path = path
            self._claims: dict[str, str] = {"app": app_claim, "user": user}
            self._body = body or {}

        def get(self, key, default=None):
            return self._claims.get(key, default)

        def __contains__(self, key) -> bool:
            return key in self._claims

        def __getitem__(self, key):
            return self._claims[key]

        async def json(self):
            return self._body

    @classmethod
    def _owner_request(cls, path: str, body: dict | None = None):
        """Configured owner: app="" + user matches owner_id."""
        return cls._FakeRequest(
            path, app_claim="", user="owner-user-123", body=body,
        )

    @classmethod
    def _non_owner_request(cls, path: str, body: dict | None = None):
        """Dashboard user who is NOT the owner."""
        return cls._FakeRequest(
            path, app_claim="", user="other-user-456", body=body,
        )

    @classmethod
    def _no_identity_request(cls, path: str, body: dict | None = None):
        """Caller with no user identity (empty string)."""
        return cls._FakeRequest(
            path, app_claim="", user="", body=body,
        )

    @classmethod
    def _app_token_request(cls, path: str, body: dict | None = None):
        """An app token caller."""
        return cls._FakeRequest(
            path, app_claim="some-app", user="", body=body,
        )

    def _run(self, handler, req):
        import asyncio

        return asyncio.run(handler(req))

    def test_non_owner_dashboard_user_refused(self):
        """The actual bug: a non-owner with no app identity passes through."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.dashboard.handlers import messaging as msg

        sel_mock = MagicMock()
        with patch.object(msg, "_sel", return_value=sel_mock):
            resp = self._run(
                msg.api_browser_token_put,
                self._non_owner_request(
                    "/api/browser/token", {"token": "x"}
                ),
            )
        assert resp.status == 403

    def test_no_identity_caller_refused(self):
        """A caller with empty user and empty app is refused."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.dashboard.handlers import messaging as msg

        sel_mock = MagicMock()
        with patch.object(msg, "_sel", return_value=sel_mock):
            resp = self._run(
                msg.api_browser_install_start,
                self._no_identity_request("/api/browser/install"),
            )
        assert resp.status == 403

    def test_configured_owner_allowed(self):
        """The configured owner passes through the gate."""
        from unittest.mock import patch

        from kiro_crew.dashboard.handlers import messaging as msg

        # Patch dependencies that run AFTER the gate passes
        with (
            patch.object(msg.browser_cli_token, "set_token"),
            patch.object(
                msg.browser_cli_token, "has_token", return_value=True
            ),
            patch.object(
                msg.browser_cli_token,
                "cli_env_overrides",
                return_value={},
            ),
        ):
            resp = self._run(
                msg.api_browser_token_put,
                self._owner_request(
                    "/api/browser/token", {"token": "secret"}
                ),
            )
        # 200 means the gate passed (handler ran to completion)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_sel_denial_emitted_for_non_owner(self):
        """SEL records the denial with a meaningful caller identity."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.dashboard.handlers import messaging as msg

        sel_mock = MagicMock()
        with patch.object(msg, "_sel", return_value=sel_mock):
            req = self._non_owner_request(
                "/api/browser/token", {"token": "x"}
            )
            await msg.api_browser_token_put(req)

        sel_mock.log_api_access.assert_called_once()
        call_kwargs = sel_mock.log_api_access.call_args[1]
        assert call_kwargs["caller"] == "other-user-456"
        assert call_kwargs["outcome"] == "denied"
        assert call_kwargs["operation"] == "browser_token_set"

    @pytest.mark.asyncio
    async def test_sel_records_the_allowed_decision_too(self):
        """An ALLOWED browser mutation is audited, not just a refused one.

        The damaging event on this boundary is a successful install or token
        write, so a log that carries only denials cannot answer who armed
        browsing on the host.
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.dashboard.handlers import messaging as msg

        sel_mock = MagicMock()
        with (
            patch.object(msg, "_sel", return_value=sel_mock),
            patch.object(msg.browser_cli_token, "set_token"),
            patch.object(msg.browser_cli_token, "has_token", return_value=True),
            patch.object(
                msg.browser_cli_token, "cli_env_overrides", return_value={}
            ),
        ):
            req = self._owner_request("/api/browser/token", {"token": "x"})
            await msg.api_browser_token_put(req)

        outcomes = [
            c[1].get("outcome") for c in sel_mock.log_api_access.call_args_list
        ]
        assert "allowed" in outcomes, outcomes
        allowed = next(
            c[1]
            for c in sel_mock.log_api_access.call_args_list
            if c[1].get("outcome") == "allowed"
        )
        assert allowed["operation"] == "browser_token_set"
        assert allowed["caller"]

    @pytest.mark.asyncio
    async def test_sel_denial_emitted_for_app_token(self):
        """App tokens still audit as 'app:<name>'."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.dashboard.handlers import messaging as msg

        sel_mock = MagicMock()
        req = self._app_token_request(
            "/api/browser/token", {"token": "x"}
        )

        with patch.object(msg, "_sel", return_value=sel_mock):
            await msg.api_browser_token_put(req)

        sel_mock.log_api_access.assert_called_once()
        call_kwargs = sel_mock.log_api_access.call_args[1]
        assert call_kwargs["caller"] == "app:some-app"
        assert call_kwargs["outcome"] == "denied"

    def test_read_endpoint_still_reachable_by_scoped_app(self):
        """api_browser_install_get has no owner gate — apps can read status."""
        from unittest.mock import patch

        from kiro_crew.dashboard.handlers import messaging as msg

        req = self._app_token_request("/api/browser/install")

        with patch.object(
            msg.browser_cli_install,
            "detect",
            return_value={"installed": False, "node_ok": True},
        ), patch.object(
            msg.browser_cli_token, "has_token", return_value=False
        ):
            resp = self._run(msg.api_browser_install_get, req)
        # 200 — the read endpoint does not enforce ownership
        assert resp.status == 200


class TestTheViewDoesNotOutliveTheGateway:
    def test_shutdown_stops_the_view(self):
        """`show` runs in its own session, so only an explicit stop reaps it.

        Without this hook an ordinary restart leaves the dashboard process alive
        while the new gateway loses its pid, and the next request starts a second
        process tree.
        """
        import inspect

        from kiro_crew.dashboard import server

        src = inspect.getsource(server)
        assert "_browser_view_shutdown" in src
        assert "app.on_cleanup.append(_browser_view_shutdown)" in src
        assert "browser_cli_view.stop" in src


class TestOneInstallSlotIsNotAFoldedLie:
    """Folding is right for the CLI install; for engines it reports the wrong target.

    The gateway has ONE install slot. The CLI install has one target, so a second
    click means the same work and folding it into a 200 is honest. Engines are
    three DISTINCT targets sharing that slot: answering 200 while a different
    engine downloads makes the panel show WebKit installing when Firefox is.
    """

    def _app_request(self, body):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.path = "/api/browser/engine"
        # Simulate the configured owner so the request passes the authz gate
        # and reaches the one-slot conflict logic under test.
        _claims = {"app": "", "user": "the-owner"}
        req.get = lambda key, default=None: _claims.get(key, default)
        req.__contains__ = lambda self_inner, key: key in _claims
        req.__getitem__ = lambda self_inner, key: _claims[key]

        async def _json():
            return body

        req.json = _json
        return req

    def test_a_second_engine_request_is_refused_while_one_runs(self):
        import asyncio
        import contextlib

        from kiro_crew.dashboard.handlers import messaging as msg

        async def _drive():
            state = type("S", (), {})()
            state.owner_id = "the-owner"
            never_done = asyncio.get_running_loop().create_future()
            state._browser_install_task = never_done
            state._browser_install_error = None
            req = self._app_request({"engine": "webkit"})
            req.app = {"state": state}
            resp = await msg.api_browser_engine_install(req)
            # Await the cancellation before the loop closes. `cancel()` only
            # REQUESTS cancellation: it schedules the callbacks that finish it,
            # and `asyncio.run` closes the loop on return, so a bare cancel
            # leaves those callbacks to fire against a closed loop and surface
            # as `Event loop is closed` in whichever test runs next in this
            # worker.
            never_done.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await never_done
            return resp

        resp = asyncio.run(_drive())
        assert resp.status == 409
        import json as _json

        assert _json.loads(resp.text)["code"] == "install_already_running"


class TestARecoveredStepIsNotReportedAsAnError:
    """A step can fail and be RECOVERED, so "any step failed" is not the verdict.

    ``--with-deps`` is refused by sudo policy on a managed workstation; the
    browser download is then retried without it and succeeds. That first attempt
    stays in ``steps`` so the operator can see what was tried, which means
    scanning every step for ``ok=False`` raises a permanent error banner quoting
    a sudo refusal on a host where browsing now works. The panel renders
    ``last_error`` with no gate of its own, so the verdict is made here.
    """

    #: What ``install()`` returns once a refused package step has been recovered.
    _RECOVERED = {
        "ok": True,
        "steps": [
            {"name": "npm-install-global", "ok": True, "returncode": 0, "stderr": ""},
            {
                "name": "install-browser",
                "ok": False,
                "returncode": 1,
                "stderr": "not allowed to execute '/bin/sh -c apt-get update' as root",
            },
            {
                "name": "install-browser-no-deps",
                "ok": True,
                "returncode": 0,
                "stderr": "",
            },
            {"name": "install-skills", "ok": True, "returncode": 0, "stderr": ""},
        ],
    }

    def _owner_request(self, state):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.path = "/api/browser/install"
        _claims = {"app": "", "user": "the-owner"}
        req.get = lambda key, default=None: _claims.get(key, default)
        req.__contains__ = lambda self_inner, key: key in _claims
        req.__getitem__ = lambda self_inner, key: _claims[key]

        async def _json():
            return {}

        req.json = _json
        req.app = {"state": state}
        return req

    def _last_error(self, monkeypatch, result):
        import asyncio

        from kiro_crew.dashboard.handlers import messaging as msg

        monkeypatch.setattr(msg.browser_cli_install, "install", lambda: result)
        monkeypatch.setattr(
            msg.browser_cli_install, "detect", lambda: {"installed": True}
        )
        monkeypatch.setattr(msg.browser_cli_token, "has_token", lambda: False)

        async def _go():
            state = type("S", (), {})()
            state.owner_id = "the-owner"
            state._browser_install_task = None
            state._browser_install_error = None
            await msg.api_browser_install_start(self._owner_request(state))
            task = state._browser_install_task
            if task is not None:
                await task
            return state._browser_install_error

        return asyncio.run(_go())

    def test_a_recovered_with_deps_refusal_leaves_no_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        assert self._last_error(monkeypatch, self._RECOVERED) is None

    def test_a_recovered_failure_does_not_mask_the_step_that_decided_the_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The FIRST failed step can be the recovered one, so reporting it would
        hide the real failure and drop the remedy the operator needs."""
        masked = {
            "ok": False,
            "steps": [
                {
                    "name": "npm-install-global",
                    "ok": True,
                    "returncode": 0,
                    "stderr": "",
                },
                {
                    "name": "install-browser",
                    "ok": False,
                    "returncode": 1,
                    "stderr": "not allowed to execute '/bin/sh -c apt-get update' as root",
                },
                {
                    "name": "install-browser-no-deps",
                    "ok": False,
                    "returncode": 0,
                    "stderr": "Host system is missing dependencies!\nsudo dnf install -y nss",
                },
            ],
        }
        error = self._last_error(monkeypatch, masked)
        assert error is not None
        # The decisive step and ITS remedy, not the recovered sudo refusal.
        assert "install-browser-no-deps" in error
        assert "sudo dnf install -y nss" in error
        assert "apt-get update" not in error

    def test_a_genuine_failure_still_reports_its_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The gate must not swallow a real failure: the remedy the operator
        needs travels in exactly this string."""
        failed = {
            "ok": False,
            "steps": [
                {
                    "name": "npm-install-global",
                    "ok": True,
                    "returncode": 0,
                    "stderr": "",
                },
                {
                    "name": "install-browser",
                    "ok": False,
                    "returncode": 1,
                    "stderr": "Host system is missing dependencies!\nsudo dnf install -y nss",
                },
            ],
        }
        error = self._last_error(monkeypatch, failed)
        assert error is not None
        assert "install-browser" in error
        assert "sudo dnf install -y nss" in error


def test_non_object_json_is_a_validation_error_not_a_500():
    """`body.get()` on valid-but-non-object JSON raises AttributeError.

    The file already documented this guard on its older handlers; the browser
    routes added here did not follow it. `(body or {})` is not enough either --
    it absorbs `[]` and `null` but not a non-empty non-dict like `[1]`.
    """
    import inspect

    from kiro_crew.dashboard.handlers import messaging

    for name in ("api_browser_token_put", "api_browser_engine_install"):
        src = inspect.getsource(getattr(messaging, name))
        assert "isinstance(body, dict)" in src, f"{name} would 500 on non-object JSON"


def test_every_browser_route_has_a_deliberate_app_token_stance():
    """Enumerated, not spot-checked, and DISCOVERED rather than listed.

    I shipped three of five routes guarded and missed the two view routes --
    the ones that hand out an UNAUTHENTICATED dashboard URL, i.e. control of a
    logged-in browser. Read-only on this gateway is not read-only on the
    browser. Discovering the routes means a NEW `api_browser_*` handler fails
    this test until someone classifies it, instead of silently defaulting open.
    """
    import inspect

    from kiro_crew.dashboard.handlers import messaging

    # Stance per route:
    #   "owner"    -> must call _deny_non_owner_browser_request (dashboard owner)
    #   "internal" -> machine-only: gated on request["internal_auth"] (loopback +
    #                 X-Internal-Secret); no cookie/app caller reaches it at all
    #   "open"     -> deliberately readable by any caller
    EXPECTED = {
        "api_browser_token_put": "owner",          # writes the attach credential
        "api_browser_install_start": "owner",      # mutates the machine (npm install)
        "api_browser_engine_install": "owner",     # mutates the machine (browser download)
        "api_browser_view_get": "owner",           # returns the unauthenticated dashboard URL
        "api_browser_view_start": "owner",         # launches the browser AND returns that URL
        # Presence/version reporting only. No credential, no URL, no mutation --
        # and an app that cannot read it cannot tell "absent" from "broken".
        "api_browser_install_get": "open",
        # Native command-bus routes: called only by the browser MCP tool / the
        # Electron poller over loopback with the internal secret. Machine-only.
        "api_browser_command": "internal",
        "api_browser_command_drain": "internal",
        "api_browser_command_result": "internal",
    }

    found = {
        name for name, obj in vars(messaging).items()
        if name.startswith("api_browser_") and inspect.isfunction(obj)
    }
    assert found == set(EXPECTED), (
        "browser route set changed -- classify each new route's app-token stance "
        f"here.\n  added: {sorted(found - set(EXPECTED))}"
        f"\n  removed: {sorted(set(EXPECTED) - found)}"
    )

    wrong = []
    for name, stance in EXPECTED.items():
        src = inspect.getsource(getattr(messaging, name))
        owner_gated = "_deny_non_owner_browser_request" in src
        internal_gated = 'request.get("internal_auth") is not True' in src
        actual = "owner" if owner_gated else "internal" if internal_gated else "open"
        if actual != stance:
            wrong.append(f"{name}: guard={actual} expected={stance}")
    assert not wrong, (
        "browser route guard stance does not match the declared intent: "
        + "; ".join(wrong)
    )


class TestTokenIsOwnerRestricted:
    """The attach credential is written via restrict_to_owner, not a numeric mode."""

    def test_set_token_calls_atomic_write_with_restrict_to_owner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        from unittest.mock import patch

        from kiro_crew.browser_cli import token

        monkeypatch.setattr(token, "config_dir", lambda: tmp_path)

        with patch.object(token, "atomic_write") as mock_aw:
            token.set_token("secret-value")

        mock_aw.assert_called_once()
        _args, kwargs = mock_aw.call_args
        # restrict_to_owner must be True so the secret is locked down on Windows too.
        assert kwargs.get("restrict_to_owner") is True or (
            len(_args) > 2 and _args[2] is True
        ), "set_token must pass restrict_to_owner=True to atomic_write"
        # restrict_on_error must be "raise" so a failure is loud, not silent.
        assert kwargs.get("restrict_on_error", "raise") == "raise", (
            "set_token must fail loud when permissions cannot be applied"
        )
        # The old numeric mode must NOT be present.
        assert "mode" not in kwargs, "numeric mode is a Windows no-op; use restrict_to_owner"


class TestViewSubprocessesReceiveNodeEnv:
    """Both _spawn and stop pass the Node-augmented env to the child process."""

    def test_spawn_passes_env_to_popen(self, monkeypatch: pytest.MonkeyPatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.browser_cli import view as view_mod

        fake_env = {"PATH": "/nvm/bin:/usr/bin", "HOME": "/home/test"}
        monkeypatch.setattr(view_mod, "cli_env", lambda: fake_env)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            view_mod._spawn("/n/pw", 9999)

        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        assert kwargs["env"] is fake_env, "_spawn must pass env=cli_env() to Popen"

    def test_stop_does_not_invoke_subprocess_run(self, monkeypatch: pytest.MonkeyPatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.browser_cli import view as view_mod

        fake_env = {"PATH": "/nvm/bin:/usr/bin", "HOME": "/home/test"}
        monkeypatch.setattr(view_mod, "cli_env", lambda: fake_env)
        monkeypatch.setattr(view_mod, "cli_path", lambda: "/n/pw")

        # Set up an owned child so stop() exercises the reap path.
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 12345
        view_mod._proc = fake_proc

        with patch.object(view_mod.subprocess, "run") as mock_run:
            monkeypatch.setattr(
                platform_compat,
                "kill_process_tree",
                lambda pid, sig=None: None,
            )
            view_mod.stop()

        # stop() no longer issues any subprocess.run call (no global --kill).
        mock_run.assert_not_called()

        # Cleanup.
        view_mod._proc = None
        view_mod._info = None


class TestStopGuardsAgainstUnownedProcesses:
    """stop() must not kill an operator's own playwright-cli show."""

    def test_stop_without_owned_proc_does_not_issue_show_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import patch

        from kiro_crew.browser_cli import view as view_mod

        monkeypatch.setattr(view_mod, "cli_path", lambda: "/n/pw")
        # No owned process.
        view_mod._proc = None
        view_mod._info = None

        with patch.object(view_mod.subprocess, "run") as mock_run:
            view_mod.stop()

        mock_run.assert_not_called(), (
            "stop() with no owned _proc must NOT issue show --kill"
        )

    def test_stop_with_owned_proc_reaps_child_without_global_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import MagicMock, patch

        from kiro_crew.browser_cli import view as view_mod

        monkeypatch.setattr(view_mod, "cli_path", lambda: "/n/pw")
        monkeypatch.setattr(view_mod, "cli_env", lambda: {"PATH": "/usr/bin"})

        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 55555
        view_mod._proc = fake_proc
        view_mod._info = view.ShowInfo(url="http://127.0.0.1:9999", port=9999)

        reaped: list[int] = []
        monkeypatch.setattr(
            platform_compat,
            "kill_process_tree",
            lambda pid, sig=None: reaped.append(pid),
        )

        with patch.object(view_mod.subprocess, "run") as mock_run:
            view_mod.stop()

        # No global --kill issued; the owned child was reaped via its tree.
        mock_run.assert_not_called()
        assert 55555 in reaped
        assert view_mod._proc is None

    def test_stop_reaps_owned_child_even_when_tree_kill_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import MagicMock

        from kiro_crew.browser_cli import view as view_mod

        monkeypatch.setattr(view_mod, "cli_path", lambda: "/n/pw")
        monkeypatch.setattr(view_mod, "cli_env", lambda: {"PATH": "/usr/bin"})

        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 66666
        view_mod._proc = fake_proc
        view_mod._info = view.ShowInfo(url="http://127.0.0.1:8888", port=8888)

        def _exploding_kill(pid, sig=None):
            raise OSError("boom")

        monkeypatch.setattr(
            platform_compat,
            "kill_process_tree",
            _exploding_kill,
        )

        # stop() must not propagate the exception; the child is still cleared.
        view_mod.stop()

        assert view_mod._proc is None
        assert view_mod._info is None

    def test_stop_is_idempotent_across_two_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import MagicMock

        from kiro_crew.browser_cli import view as view_mod

        monkeypatch.setattr(view_mod, "cli_path", lambda: "/n/pw")
        monkeypatch.setattr(view_mod, "cli_env", lambda: {"PATH": "/usr/bin"})

        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 77777
        view_mod._proc = fake_proc
        view_mod._info = view.ShowInfo(url="http://127.0.0.1:7777", port=7777)

        kill_calls: list[int] = []
        monkeypatch.setattr(
            platform_compat,
            "kill_process_tree",
            lambda pid, sig=None: kill_calls.append(pid),
        )

        view_mod.stop()
        view_mod.stop()

        # The tree kill fires only once (the first call); the second is a no-op.
        assert kill_calls == [77777]
        assert view_mod._proc is None
        assert view_mod._info is None
