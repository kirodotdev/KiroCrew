"""Whose git config the update check's fetch reads, and how long the click's verdict lasts.

The boot check, the 12-hour poll and ``GET /api/update/check`` run the isolated check:
no credential helper. The owner's "Check for updates" click is ``POST
/api/update/check``; on a standalone install its fetch reads the operator's own git
config, as their Update already does, so a private HTTPS remote that signs in through
a credential helper can be checked. A governed install always keeps the click isolated.

The helper is a real ``credential.helper=store`` in a global config file the test
points ``GIT_CONFIG_GLOBAL`` at, and the stubbed fetch asks git for a credential under
the env it was handed: the password comes back exactly when the helper ran.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state

from kiro_crew import update_next_start
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request
from kiro_crew.platform import update_governance
from kiro_crew.platform.governance import parse_policy
from kiro_crew.platform.update_capability import (
    AUTO_EFFECT_INSTALL,
    AUTO_ROUTE_GIT,
    MANAGED_BY_GIT,
    MODE_NOTIFY,
    AutoUpdateEffect,
    UpdateCapability,
)
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.preview import PREVIEW_ENV
from kiro_crew.shared_crew.teammate_access import teammate_passes_owner_gate
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until

OWNER = "U0OWNER0000"
HOST = "private.example"
SECRET = "from-the-helper"
HEAD = "a" * 40


@pytest.fixture
def helper_home(monkeypatch, tmp_path):
    """A global git config whose only credential helper knows the private host."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    store = tmp_path / "credentials"
    # LF on every OS: the store helper reads a CRLF line's host as "<host>\r".
    store.write_text(f"https://user:{SECRET}@{HOST}\n", encoding="utf-8", newline="\n")
    cfg = tmp_path / "gitconfig"
    store_path = store.as_posix()
    # The empty value clears helpers from earlier config scopes first (Git for
    # Windows ships a system `credential.helper=manager`), so this store is the
    # only helper git asks.
    cfg.write_text(
        f'[credential]\n\thelper =\n\thelper = "store --file={store_path}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    for name in ("GIT_ASKPASS", "SSH_ASKPASS"):
        monkeypatch.delenv(name, raising=False)
    return cfg


def _helper_answers(env: dict[str, str]) -> tuple[bool, str]:
    """Whether git, run under *env*, gets the password from a credential helper.

    Returns the verdict with git's stderr, which a failing assertion shows.
    """
    probe_env = {**env, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    out = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={HOST}\n\n",
        env=probe_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    return f"password={SECRET}" in out.stdout, out.stderr


@pytest.fixture
def git_checkout(monkeypatch, tmp_path):
    """A git-checkout install whose git reads are scripted and whose fetch is recorded.

    The fetch stub plays the private remote: it answers the checkout's HEAD only
    when the env it was handed lets the credential helper sign in, and fails
    otherwise, as an HTTPS fetch with no credential does.
    """
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    capability = UpdateCapability(
        supported=True,
        managed_by=MANAGED_BY_GIT,
        mode=MODE_NOTIFY,
        can_download=False,
        can_apply=True,
        requires_restart=True,
    )
    monkeypatch.setattr(updates, "resolve_provider", lambda: None)
    monkeypatch.setattr(updates, "derive_capability", lambda: capability)
    monkeypatch.setattr(updates, "upstream_remote_and_branch", lambda _p: ("origin", "main"))
    monkeypatch.setattr(updates.platform_compat, "trusted_git_bin", lambda: "git")
    monkeypatch.setattr(updates, "_can_arm_this_install", _false)
    monkeypatch.setattr(updates, "_local_version", "1.0.0")
    probes: list[bool] = []

    def _probe(_proj: str, *, human_transport: bool = False) -> str:
        probes.append(human_transport)
        return ""

    monkeypatch.setattr(updates, "repo_exec_config_reason", _probe)
    # ``rev_list`` is the commit distance the reads report: ahead, then behind.
    record: dict[str, object] = {
        "fetches": [],
        "probes": probes,
        "stderr": [],
        "rev_list": b"0\t0\n",
    }

    async def _fetch(proj, remote, branch, *, git, env, timeout):
        signed_in, stderr = _helper_answers(dict(env))
        record["fetches"].append(signed_in)  # type: ignore[union-attr]
        record["stderr"].append(stderr)  # type: ignore[union-attr]
        return HEAD if signed_in else None

    monkeypatch.setattr(updates, "fetch_and_pin_upstream", _fetch)

    async def _exec(*args, **kwargs):
        argv = [str(a) for a in args]
        if "rev-parse" in argv:
            out = f"{HEAD}\n".encode()
        elif "rev-list" in argv:
            out = record["rev_list"]  # type: ignore[assignment]
        elif "show" in argv:
            out = b'__version__ = "1.0.0"\n'
        else:
            out = b""

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (out, b"")

        return _Proc()

    monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _exec)
    original = dict(updates._update_info)
    yield record
    updates._update_info.clear()
    updates._update_info.update(original)


async def _false() -> bool:
    return False


def _standalone(monkeypatch) -> None:
    monkeypatch.setattr(updates, "manual_check_operator_transport", lambda: True)


async def _client(
    claims: dict[str, object], seen: Callable[[web.Request], None] | None = None
) -> TestClient:
    @web.middleware
    async def _claims(request: web.Request, handler: Callable) -> web.StreamResponse:
        for key, value in claims.items():
            request[key] = value
        if seen is not None:
            seen(request)
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_get("/api/update/check", updates.api_update_check)
    app.router.add_post("/api/update/check", updates.api_update_check_now)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _no_effect_refresh(monkeypatch) -> None:
    async def _noop() -> None:
        return None

    monkeypatch.setattr(updates, "_refresh_auto_update_effect_once", _noop)


class TestTheClickSignsInThroughTheHelper:
    def test_the_owners_click_reaches_a_helper_authenticated_private_remote(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)
        _no_effect_refresh(monkeypatch)

        async def _run():
            client = await _client({"app": "", "user": OWNER})
            try:
                resp = await client.post("/api/update/check")
                return resp.status, await resp.json()
            finally:
                await client.close()

        status, body = asyncio.run(_run())
        assert status == 200
        assert git_checkout["fetches"] == [True], git_checkout["stderr"]
        assert body["check_status"] == "succeeded"
        assert body.get("error_code") is None
        # The paired probe that refuses the repository's own transport keys ran.
        assert True in git_checkout["probes"]

    def test_the_isolated_check_does_not_run_the_helper(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)
        _no_effect_refresh(monkeypatch)

        async def _run():
            client = await _client({"app": "", "user": OWNER})
            try:
                resp = await client.get("/api/update/check")
                return resp.status, await resp.json()
            finally:
                await client.close()

        status, body = asyncio.run(_run())
        assert status == 200
        assert git_checkout["fetches"] == [False]
        assert body["check_status"] == "failed"
        assert body["error_code"] == "git_fetch_failed"
        assert git_checkout["probes"] == [False]

    def test_the_background_poll_stays_isolated_on_a_standalone_install(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)
        asyncio.run(updates._do_update_check())
        assert git_checkout["fetches"] == [False]
        assert updates.get_update_info()["error_code"] == "git_fetch_failed"

    def test_a_teammates_post_runs_the_isolated_check(self, helper_home, git_checkout, monkeypatch):
        # A Tailscale collaborator on their own session: the claims token auth
        # publishes, which pass the owner gate every other owner route uses.
        monkeypatch.setenv(PREVIEW_ENV, "1")
        _standalone(monkeypatch)
        _no_effect_refresh(monkeypatch)
        principal = Principal(
            id=principal_id_for("tailscale", "tailnet", "carol@example.test"),
            kind=PrincipalKind.HUMAN,
            provider="tailscale",
            issuer="tailnet",
            subject="carol@example.test",
            provenance=Provenance.TAILSCALE_LISTENER,
            display=DisplaySnapshot(name="Carol", login="carol@example.test"),
        )
        claims: dict[str, object] = {
            "app": "",
            "user": "tailnet:uid:42",
            "is_dashboard_user": True,
            "auth_tailnet_direct": True,
            ACTOR_REQUEST_KEY: ActorContext(
                principal=principal, request_id="req", transport=Transport.HTTP
            ),
        }
        gate: list[tuple[bool, bool]] = []

        def _seen(request: web.Request) -> None:
            gate.append(
                (teammate_passes_owner_gate(request), is_host_owner_dashboard_request(request))
            )

        async def _run():
            client = await _client(claims, _seen)
            try:
                resp = await client.post("/api/update/check")
                return resp.status
            finally:
                await client.close()

        assert asyncio.run(_run()) == 200
        assert gate == [(True, False)]
        assert git_checkout["fetches"] == [False], git_checkout["stderr"]
        assert git_checkout["probes"] == [False]

    def test_a_governed_click_stays_isolated(self, helper_home, git_checkout, monkeypatch):
        monkeypatch.setattr(updates, "manual_check_operator_transport", lambda: False)
        asyncio.run(updates._do_update_check(manual=True))
        assert git_checkout["fetches"] == [False]
        assert git_checkout["probes"] == [False]

    def test_a_checkout_the_paired_probe_refuses_falls_back_to_the_isolated_fetch(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)

        def _probe(_proj: str, *, human_transport: bool = False) -> str:
            return "repository declares credential.helper" if human_transport else ""

        monkeypatch.setattr(updates, "repo_exec_config_reason", _probe)
        asyncio.run(updates._do_update_check(manual=True))
        assert git_checkout["fetches"] == [False]
        assert updates._manual_verdict["error_code"] == "git_fetch_failed"  # type: ignore[index]


_LIVE_LIFETIME = 10**12
_EXPIRED_LIFETIME = -1


def _lifetime(monkeypatch, seconds: int) -> None:
    monkeypatch.setattr(updates, "_MANUAL_VERDICT_TTL_S", seconds)


def _click_found_an_update(git_checkout, monkeypatch) -> None:
    """The owner's click on a private remote found a newer build (3 commits behind)."""
    _standalone(monkeypatch)
    # The default TTL is minutes; a test never waits on a clock, so it is widened.
    _lifetime(monkeypatch, _LIVE_LIFETIME)
    git_checkout["rev_list"] = b"0\t3\n"
    asyncio.run(updates._do_update_check(manual=True))


class TestTheClicksVerdictIsScoped:
    def test_a_click_never_writes_the_cache_the_gateway_acts_on(
        self, helper_home, git_checkout, monkeypatch
    ):
        before = dict(updates._update_info)
        stamp = updates.last_checked_at()
        _click_found_an_update(git_checkout, monkeypatch)

        assert git_checkout["fetches"] == [True]
        assert updates._manual_verdict["update_available"] is True  # type: ignore[index]
        assert updates.get_update_info() == before
        assert updates.last_checked_at() == stamp

    def test_the_badge_shows_the_click_at_the_clicks_own_time(
        self, helper_home, git_checkout, monkeypatch
    ):
        _click_found_an_update(git_checkout, monkeypatch)

        fields = updates.status_update_fields(next_start={})
        assert fields["update_available"] is True
        assert fields["update_check_status"] == "succeeded"
        assert fields["update_last_checked_at"] == updates._manual_verdict_at
        assert fields["update_last_checked_at"] != updates.last_checked_at()

    def test_the_gateways_update_cycle_ignores_the_click(
        self, helper_home, git_checkout, monkeypatch
    ):
        # The click found a newer build; the gateway's own isolated check cannot
        # sign in. The cycle reads its own failure, never the click's verdict, so
        # it neither applies nor announces what only the click could see.
        _click_found_an_update(git_checkout, monkeypatch)

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = MagicMock()
        orch.sessions.pause_turn_admission_for_update = AsyncMock(return_value=True)
        orch._auto_apply_wheel_update = AsyncMock()
        effect = AutoUpdateEffect(AUTO_EFFECT_INSTALL, AUTO_ROUTE_GIT)
        cfg = SimpleNamespace(auto_update=True, dashboard=SimpleNamespace(update_nudge={}))
        with (
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.platform.update_governance.update_required", return_value=False),
            patch("kiro_crew.slack.gateway.auto_update_effect", return_value=effect),
        ):
            asyncio.run(orch._check_for_updates())

        assert git_checkout["fetches"] == [True, False]
        assert updates.get_update_info()["error_code"] == "git_fetch_failed"
        orch._auto_apply_wheel_update.assert_not_awaited()
        orch.sessions.pause_turn_admission_for_update.assert_not_awaited()
        assert call("update_available") not in orch.dashboard_state.push_refresh.call_args_list

    def test_the_next_isolated_check_clears_it(self, helper_home, git_checkout, monkeypatch):
        _click_found_an_update(git_checkout, monkeypatch)
        assert updates._manual_verdict is not None

        asyncio.run(updates._do_update_check())
        assert updates._manual_verdict is None
        fields = updates.status_update_fields(next_start={})
        assert fields["update_check_status"] == "failed"
        assert fields["update_available"] is not True
        assert fields["update_last_checked_at"] == updates.last_checked_at()

    def test_the_next_click_replaces_it(self, helper_home, git_checkout, monkeypatch):
        _click_found_an_update(git_checkout, monkeypatch)
        git_checkout["rev_list"] = b"0\t0\n"
        asyncio.run(updates._do_update_check(manual=True))
        assert updates._manual_verdict["update_available"] is False  # type: ignore[index]

    def test_it_expires(self, helper_home, git_checkout, monkeypatch):
        _click_found_an_update(git_checkout, monkeypatch)
        assert updates._live_manual_verdict() is not None

        _lifetime(monkeypatch, _EXPIRED_LIFETIME)
        assert updates._live_manual_verdict() is None
        fields = updates.status_update_fields(next_start={})
        assert fields["update_available"] is not True
        assert fields["update_last_checked_at"] == updates.last_checked_at()

    def test_the_default_lifetime_is_short(self):
        assert 0 < updates._MANUAL_VERDICT_TTL_S <= 900

    def test_a_channel_switch_clears_it(self, monkeypatch):
        monkeypatch.setattr(updates, "_manual_verdict", {"check_status": "succeeded"})
        monkeypatch.setattr(updates, "_check_generation", updates._check_generation)
        monkeypatch.setattr(updates, "_last_update_check", updates._last_update_check)
        original = dict(updates._update_info)
        try:
            updates._invalidate_update_check("stable")
        finally:
            updates._update_info.clear()
            updates._update_info.update(original)
        assert updates._manual_verdict is None

    def test_a_feed_install_click_writes_the_cache_as_before(self, monkeypatch):
        capability = UpdateCapability(
            supported=True,
            managed_by="kirocrew",
            mode=MODE_NOTIFY,
            can_download=True,
            can_apply=False,
            requires_restart=True,
        )
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(updates, "derive_capability", lambda: capability)

        async def _feed(_capability) -> None:
            updates._set_update_info(
                managed_by="kirocrew", check_status="succeeded", update_available=True
            )

        monkeypatch.setattr(updates, "_check_release_feed", _feed)
        original = dict(updates._update_info)
        try:
            asyncio.run(updates._do_update_check(manual=True))
            info = dict(updates.get_update_info())
        finally:
            updates._update_info.clear()
            updates._update_info.update(original)
        assert updates._manual_verdict is None
        assert info["update_available"] is True


class TestTheUpdateTheBadgeOffersArms:
    def test_the_owner_who_saw_the_click_verdict_can_arm_it(
        self, helper_home, git_checkout, monkeypatch, tmp_path
    ):
        # The isolated check cannot sign in, so arming through it would refuse an
        # Update the badge offered. The arm names the verdict it showed.
        from kiro_crew.platform import update_provider

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.derive_capability",
            lambda **_kw: SimpleNamespace(managed_by=MANAGED_BY_GIT),
        )
        monkeypatch.setattr(update_provider, "resolve_provider", lambda: None)
        _click_found_an_update(git_checkout, monkeypatch)
        assert updates.status_update_fields(next_start={})["update_available"] is True
        assert updates.status_update_fields(next_start={})["update_can_apply"] is True

        async def _run():
            app = web.Application(middlewares=[_as_owner])
            app["state"] = SimpleNamespace(owner_id=OWNER, push_refresh=lambda *_a, **_k: None)
            app.router.add_post("/api/update", updates.api_update_apply)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post("/api/update", json={"when": "next-start"})
                return resp.status, await resp.json()
            finally:
                await client.close()

        status, body = asyncio.run(_run())
        assert status == 202, body
        assert git_checkout["fetches"] == [True]
        assert body["version"] == "1.0.0"
        assert update_next_start.read().armed_version == "1.0.0"


@web.middleware
async def _as_owner(request: web.Request, handler: Callable) -> web.StreamResponse:
    request["app"] = ""
    request["user"] = OWNER
    return await handler(request)


class TestTheTwoChecksDoNotShareASlot:
    def test_a_click_does_not_ride_on_a_running_isolated_check(self, monkeypatch):
        started: list[bool] = []

        async def _main():
            gate = asyncio.Event()

            async def _run(*, manual: bool = False) -> None:
                started.append(manual)
                if not manual:
                    await gate.wait()

            monkeypatch.setattr(updates, "_run_update_check", _run)
            background = asyncio.create_task(updates._do_update_check())
            await asyncio.sleep(0)
            # The isolated check cannot finish before the gate opens, so the
            # click finishing while it still waits proves the click did not ride it.
            manual = asyncio.create_task(updates._do_update_check(manual=True))
            await await_until(manual, what="the manual check")
            assert not background.done()
            gate.set()
            await await_until(background, what="the isolated check after its gate opens")

        asyncio.run(_main())
        assert started == [False, True]
        assert updates._check_task is None
        assert updates._manual_check_task is None

    def test_a_response_reports_its_own_run_not_the_other_slots(self, monkeypatch):
        # The isolated slot finishing while the click's response is still
        # reading config must not turn the click's failure into its answer.
        async def _run(*, manual: bool = False) -> dict[str, object]:
            updates._set_update_info(check_status="failed", error_code="git_fetch_failed")
            return dict(updates._update_info)

        real_load = updates.KiroCrewConfig.load

        def _load_after_the_other_slot_wrote():
            updates._set_update_info(check_status="succeeded", update_available=False)
            return real_load()

        monkeypatch.setattr(updates, "_run_update_check", _run)
        monkeypatch.setattr(updates.KiroCrewConfig, "load", _load_after_the_other_slot_wrote)
        _no_effect_refresh(monkeypatch)
        original = dict(updates._update_info)

        async def _main():
            client = await _client({"app": "", "user": OWNER})
            try:
                resp = await client.post("/api/update/check")
                return await resp.json()
            finally:
                await client.close()

        try:
            body = asyncio.run(_main())
        finally:
            updates._update_info.clear()
            updates._update_info.update(original)
        assert body["check_status"] == "failed"
        assert body["error_code"] == "git_fetch_failed"


class TestWhoseGitConfigAClickMayRead:
    """``update_governance.manual_check_operator_transport``: standalone yes, governed never."""

    @staticmethod
    def _context(monkeypatch, *, governance=None, enterprise=False, registry=False):
        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: SimpleNamespace(governance=governance, is_enterprise=enterprise),
        )
        monkeypatch.setattr("kiro_crew.edition.is_lts", lambda: False)
        cfg = SimpleNamespace(
            _base_unreadable=False,
            _overlay_unreadable=False,
            agent=SimpleNamespace(mcp_registry_mode=registry),
        )
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: cfg)

    def test_a_standalone_install_reads_the_operators_config(self, monkeypatch):
        self._context(monkeypatch)
        assert update_governance.manual_check_operator_transport() is True

    @pytest.mark.parametrize(
        "kind", ["policy", "enterprise", "registry"], ids=["policy", "enterprise", "registry"]
    )
    def test_a_governed_install_stays_isolated(self, monkeypatch, kind):
        # A policy that pins the update source is the case the operator's config
        # must never reach: a governed click reads no transport opt-in at all.
        policy = parse_policy(
            {"version": 1, "boot": {}, "updates": {"source": "https://allowed.example/*"}}
        )
        self._context(
            monkeypatch,
            governance=policy if kind == "policy" else None,
            enterprise=kind == "enterprise",
            registry=kind == "registry",
        )
        assert update_governance.manual_check_operator_transport() is False

    def test_an_unreadable_context_is_isolated(self, monkeypatch):
        def _boom():
            raise RuntimeError("no context")

        monkeypatch.setattr("kiro_crew.platform.context.current_context", _boom)
        assert update_governance.manual_check_operator_transport() is False

    def test_an_unreadable_config_is_isolated(self, monkeypatch):
        self._context(monkeypatch)
        broken = SimpleNamespace(
            _base_unreadable=True,
            _overlay_unreadable=False,
            agent=SimpleNamespace(mcp_registry_mode=False),
        )
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: broken)
        assert update_governance.manual_check_operator_transport() is False


def test_the_fetch_url_carries_no_rewrite_the_fetch_will_apply_again(monkeypatch, tmp_path):
    """The URL handed to the fetch is the configured one, so the fetch rewrites it once.

    A manual fetch reads the operator's global ``url.<base>.insteadOf``, and git
    applies it to the URL on the command line. Resolving that URL under the same
    config first would apply a chained pair of rewrites twice.
    """
    from kiro_crew.platform import update_fetch

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    monkeypatch.setattr(update_governance.platform_compat, "trusted_git_bin", lambda: git)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        '[url "https://example.invalid/"]\n\tinsteadOf = gh:\n'
        '[url "ssh://git@example.invalid/"]\n\tinsteadOf = https://example.invalid/\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (["init", "-q"], ["remote", "add", "origin", "gh:owner/private"]):
        subprocess.run(
            [git, *argv], cwd=repo, check=True, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS
        )

    # Under the operator's config the resolver applies the first rewrite...
    operator_env = update_governance.git_command_env(human_transport=True)
    resolved = update_governance.resolve_remote_url(
        str(repo), fetch_env=operator_env, remote="origin"
    )
    assert resolved == "https://example.invalid/owner/private"
    # ...so the fetch is handed the configured URL instead.
    prepared = update_fetch._prepare_isolated_fetch(str(repo), "origin", "main")
    assert prepared is not None
    url, _objects, scratch, _pairs = prepared
    shutil.rmtree(scratch, ignore_errors=True)
    assert url == "gh:owner/private"
