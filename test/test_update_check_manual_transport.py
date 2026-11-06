"""Whose git config the update check's fetch reads, and how long a manual verdict lasts.

The boot check, the 12-hour poll and ``GET /api/update/check`` run the isolated check:
no credential helper. The owner's "Check for updates" click is ``POST
/api/update/check``; on a standalone install its fetch reads the operator's own git
config, as their Update already does, so a private HTTPS remote that signs in through
a credential helper can be checked. A governed install keeps the click isolated unless
its policy sets ``updates.manual_check_uses_operator_git_config``.

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

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_governance
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import UpdatePins, parse_policy
from kiro_crew.platform.update_capability import MANAGED_BY_GIT, MODE_NOTIFY, UpdateCapability
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until

OWNER = "U0OWNER0000"
HOST = "private.example"
SECRET = "from-the-helper"
HEAD = "a" * 40
PERMITTED_REMOTE = f"https://{HOST}/owner/private.git"
FORBIDDEN_REMOTE = "https://elsewhere.example/owner/private.git"


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
    record: dict[str, object] = {"fetches": [], "probes": probes, "stderr": [], "judged": []}

    def _resolve(_proj: str, *, fetch_env, remote: str = "", branch: str = "") -> str:
        return PERMITTED_REMOTE

    def _blocked(url: str) -> str:
        record["judged"].append(url)  # type: ignore[union-attr]
        return ""

    monkeypatch.setattr(updates, "resolve_remote_url", _resolve)
    monkeypatch.setattr(updates, "update_blocked_reason", _blocked)

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
            out = b"0\t0\n"
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


async def _client(claims: dict[str, str]) -> TestClient:
    @web.middleware
    async def _claims(request: web.Request, handler: Callable) -> web.StreamResponse:
        for key, value in claims.items():
            request[key] = value
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
        _standalone(monkeypatch)
        _no_effect_refresh(monkeypatch)

        async def _run():
            client = await _client({"app": "", "user": "U0TEAMMATE"})
            try:
                resp = await client.post("/api/update/check")
                return resp.status
            finally:
                await client.close()

        assert asyncio.run(_run()) == 200
        assert git_checkout["fetches"] == [False]

    def test_a_governed_click_without_the_opt_in_stays_isolated(
        self, helper_home, git_checkout, monkeypatch
    ):
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
        assert updates.get_update_info()["error_code"] == "git_fetch_failed"

    def test_a_remote_the_source_pin_refuses_gets_the_isolated_fetch(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)

        def _resolve(_proj: str, *, fetch_env, remote: str = "", branch: str = "") -> str:
            assert remote == "origin"
            return FORBIDDEN_REMOTE

        def _blocked(url: str) -> str:
            git_checkout["judged"].append(url)  # type: ignore[union-attr]
            return "remote not permitted" if url == FORBIDDEN_REMOTE else ""

        monkeypatch.setattr(updates, "resolve_remote_url", _resolve)
        monkeypatch.setattr(updates, "update_blocked_reason", _blocked)
        asyncio.run(updates._do_update_check(manual=True))
        assert git_checkout["judged"] == [FORBIDDEN_REMOTE]
        assert git_checkout["fetches"] == [False]
        assert updates.get_update_info()["error_code"] == "git_fetch_failed"

    def test_a_remote_the_source_pin_permits_still_signs_in(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)
        asyncio.run(updates._do_update_check(manual=True))
        assert git_checkout["judged"] == [PERMITTED_REMOTE]
        assert git_checkout["fetches"] == [True], git_checkout["stderr"]

    def test_the_isolated_check_does_not_consult_the_pin_for_operator_transport(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)
        asyncio.run(updates._do_update_check())
        assert git_checkout["judged"] == []
        assert git_checkout["fetches"] == [False]


class TestTheManualVerdictSticks:
    def test_a_later_isolated_failure_keeps_the_clicks_verdict(
        self, helper_home, git_checkout, monkeypatch
    ):
        _standalone(monkeypatch)
        asyncio.run(updates._do_update_check(manual=True))
        assert updates.get_update_info()["check_status"] == "succeeded"

        asyncio.run(updates._do_update_check())
        assert git_checkout["fetches"] == [True, False]
        info = updates.get_update_info()
        assert info["check_status"] == "succeeded"
        assert info.get("error_code") is None

    def test_a_successful_isolated_check_replaces_it(self, git_checkout, monkeypatch):
        monkeypatch.setattr(
            updates, "_manual_verdict", {"check_status": "succeeded", "latest_version": "0.0.1"}
        )
        # A public remote: the isolated fetch signs in without any helper.

        async def _fetch(*_a, **_k):
            return HEAD

        monkeypatch.setattr(updates, "fetch_and_pin_upstream", _fetch)
        asyncio.run(updates._do_update_check())
        assert updates._manual_verdict is None
        assert updates.get_update_info()["latest_version"] == "1.0.0"

    def test_the_next_click_that_fails_clears_it(self, helper_home, git_checkout, monkeypatch):
        _standalone(monkeypatch)
        asyncio.run(updates._do_update_check(manual=True))
        assert updates._manual_verdict is not None

        monkeypatch.setattr(updates, "manual_check_operator_transport", lambda: False)
        asyncio.run(updates._do_update_check(manual=True))
        assert updates._manual_verdict is None
        assert updates.get_update_info()["error_code"] == "git_fetch_failed"

    def test_a_failure_the_click_cannot_cure_is_not_masked(
        self, helper_home, git_checkout, monkeypatch
    ):
        # The cache's other readers (the arm path, the gateway's auto-update)
        # act on it, so only the fetch failure a click can sign past is covered.
        _standalone(monkeypatch)
        asyncio.run(updates._do_update_check(manual=True))
        assert updates._manual_verdict is not None

        def _refuse(_proj: str, *, human_transport: bool = False) -> str:
            return "repository declares filter.x.smudge"

        monkeypatch.setattr(updates, "repo_exec_config_reason", _refuse)
        asyncio.run(updates._do_update_check())
        info = updates.get_update_info()
        assert info["check_status"] == "failed"
        assert info["error_code"] == "git_read_failed"
        assert updates._manual_verdict is None

    def test_a_feed_install_keeps_no_manual_verdict(self, monkeypatch):
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
        results = iter(
            [
                {"check_status": "succeeded", "update_available": True},
                {"check_status": "failed", "error_code": "feed_unreachable"},
            ]
        )

        async def _feed(_capability) -> None:
            updates._set_update_info(managed_by="kirocrew", **next(results))

        monkeypatch.setattr(updates, "_check_release_feed", _feed)
        original = dict(updates._update_info)
        try:
            asyncio.run(updates._do_update_check(manual=True))
            assert updates._manual_verdict is None
            asyncio.run(updates._do_update_check())
            info = dict(updates.get_update_info())
        finally:
            updates._update_info.clear()
            updates._update_info.update(original)
        assert info["check_status"] == "failed"
        assert info["error_code"] == "feed_unreachable"

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
    """``update_governance.manual_check_operator_transport``: standalone yes, governed opt-in."""

    @staticmethod
    def _context(monkeypatch, *, governance=None, enterprise=False, registry=False):
        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: SimpleNamespace(governance=governance, is_enterprise=enterprise),
        )
        monkeypatch.setattr("kiro_crew.edition.is_enterprise", lambda: False)
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
    def test_a_governed_install_stays_isolated_without_the_opt_in(self, monkeypatch, kind):
        policy = parse_policy({"version": 1, "boot": {}})
        self._context(
            monkeypatch,
            governance=policy if kind == "policy" else None,
            enterprise=kind == "enterprise",
            registry=kind == "registry",
        )
        if kind != "policy":
            monkeypatch.setattr(
                "kiro_crew.platform.governance.active_update_pins", lambda: UpdatePins()
            )
        assert update_governance.manual_check_operator_transport() is False

    def test_the_policy_opt_in_lets_a_governed_click_read_it(self, monkeypatch):
        policy = parse_policy(
            {
                "version": 1,
                "boot": {},
                "updates": {"manual_check_uses_operator_git_config": True},
            }
        )
        self._context(monkeypatch, governance=policy)
        assert update_governance.manual_check_operator_transport() is True

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


class TestThePolicyField:
    def test_absent_reads_off(self):
        assert (
            parse_policy({"version": 1, "boot": {}}).updates.manual_check_uses_operator_git_config
            is False
        )

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_taken_as_written(self, value):
        pins = UpdatePins.from_dict({"manual_check_uses_operator_git_config": value})
        assert pins.manual_check_uses_operator_git_config is value

    @pytest.mark.parametrize("bad", ["true", 1, 0, [], {}])
    def test_any_other_value_fails_the_load(self, bad):
        with pytest.raises(PlatformCompositionError, match="must be true or false"):
            UpdatePins.from_dict({"manual_check_uses_operator_git_config": bad})

    def test_a_lower_tier_cannot_supply_it(self):
        from kiro_crew.platform import governance

        authority = parse_policy({"version": 1, "boot": {}})
        subordinate = parse_policy(
            {
                "version": 1,
                "boot": {},
                "updates": {"manual_check_uses_operator_git_config": True},
            }
        )
        merged = governance._intersect_ceilings(authority, subordinate)
        assert merged.updates.manual_check_uses_operator_git_config is False

    def test_the_authoritys_opt_in_stands(self):
        from kiro_crew.platform import governance

        authority = parse_policy(
            {
                "version": 1,
                "boot": {},
                "updates": {"manual_check_uses_operator_git_config": True},
            }
        )
        subordinate = parse_policy({"version": 1, "boot": {}})
        merged = governance._intersect_ceilings(authority, subordinate)
        assert merged.updates.manual_check_uses_operator_git_config is True


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
