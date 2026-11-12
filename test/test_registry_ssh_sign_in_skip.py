"""The store listing stops asking an ssh destination that refused the anonymous sign-in.

No network: the clone is a fake process with chosen stderr, or ``_fetch_app_manifest``
is replaced at the facade, and the sandbox wrappers are identity.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from apps_registry_coverage_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps import registry
from kiro_crew.apps.registry_pipeline import manifests as manifests_module
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import await_until

_HOST = "git.example.com"
_REFUSED = b"git@git.example.com: Permission denied (publickey).\r\nfatal: Could not read from remote repository.\n"
_REPO_DENIED = b"You do not have sufficient permissions to read from this repository (pkg/x)\n"


def _row(i: int, url: str | None = None) -> dict[str, Any]:
    return {
        "name": f"app-{i}",
        "gitUrl": url or f"ssh://{_HOST}/pkg/app-{i}",
        "branch": "main",
        "_registry": "labs",
    }


class _Fetch:
    """Stand-in for ``_fetch_app_manifest``: records each call and answers *answer*.

    Like the real fetch, it reads an app named in ``installed`` from its local copy
    first, and asks ``before_remote`` only on the way to the remote.
    """

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.remote: list[str] = []
        self.local: list[str] = []
        self.installed: set[str] = set()
        self.fail_first = False

    async def __call__(self, repo, branch, subdirectory="", app_name="", git_url="", **kw):
        if app_name in self.installed:
            kw["provenance"]["source"] = "installed"
            return {"name": app_name}
        before_remote = kw.get("before_remote")
        if not kw.get("allow_remote", True) or (
            before_remote is not None and not await before_remote()
        ):
            self.local.append(app_name)
            return None
        self.remote.append(app_name)
        await asyncio.sleep(0)
        if self.fail_first and len(self.remote) == 1:
            raise RuntimeError("the probe's fetch blew up")
        outcome = kw.get("outcome")
        if outcome is not None and self.answer is not None:
            outcome["ssh_sign_in"] = self.answer
        return None if self.answer == "refused" else {"name": app_name}


@pytest.fixture()
def anonymous(monkeypatch):
    """Every row is an anonymous (non-owner) fetch."""
    monkeypatch.setattr(registry, "_owner_designated_repo_target", lambda entry: "")


def _install_fetch(monkeypatch, answer: str | None) -> _Fetch:
    fetch = _Fetch(answer)
    monkeypatch.setattr(registry, "_fetch_app_manifest", fetch)
    return fetch


class TestDestination:
    @pytest.mark.parametrize(
        ("url", "want"),
        [
            ("ssh://git.example.com/pkg/x", "git.example.com"),
            ("ssh://git@github.com/a/b", "git@github.com"),
            ("git@github.com:a/b.git", "git@github.com"),
            ("ssh://bob:hunter2@git.example.com:2222/x", "bob@git.example.com:2222"),
            ("git@[fd00::1]:a/b.git", "git@[fd00::1]"),
            ("git@[fd00::2]:a/b.git", "git@[fd00::2]"),
            ("https://github.com/a/b", ""),
            ("", ""),
        ],
    )
    def test_the_key_is_the_ssh_authority_without_a_password(self, url, want):
        assert registry._anonymous_ssh_destination(url) == want

    def test_an_overlong_destination_is_never_a_key(self):
        user = "u" * registry._SSH_DESTINATION_MAX_CHARS

        assert registry._anonymous_ssh_destination(f"ssh://{user}@{_HOST}/pkg/x") == ""

    @pytest.mark.parametrize(
        "url",
        [
            f"ssh://git@{_HOST}/Permission%20denied%20(publickey)",
            f"ssh://git@{_HOST}/a b",
            f"ssh://git@{_HOST}/a\nb",
            f"git@{_HOST}:caf\u00e9.git",
        ],
    )
    def test_a_url_git_could_echo_as_another_line_is_never_a_key(self, url):
        assert registry._anonymous_ssh_destination(url) == ""


class TestTheListing:
    @pytest.mark.asyncio
    async def test_a_cold_listing_asks_a_refusing_destination_once(
        self, cache_dir, anonymous, monkeypatch
    ):
        fetch = _install_fetch(monkeypatch, "refused")
        rows = [_row(i) for i in range(20)]

        await asyncio.gather(*[registry._resolve_manifest(dict(r)) for r in rows])

        assert len(fetch.remote) == 1
        assert len(fetch.local) == 19
        # A skipped row records a failed fetch, exactly as a fetch that failed would.
        assert all(registry._read_manifest_negative_cache(r) for r in rows)

    @pytest.mark.asyncio
    async def test_a_destination_that_answers_fans_out(self, cache_dir, anonymous, monkeypatch):
        fetch = _install_fetch(monkeypatch, "attempted")

        await asyncio.gather(*[registry._resolve_manifest(_row(i)) for i in range(20)])

        assert len(fetch.remote) == 20
        assert fetch.local == []

    @pytest.mark.asyncio
    async def test_an_owner_designated_fetch_is_never_skipped(self, cache_dir, monkeypatch):
        registry._note_ssh_sign_in(_HOST, "refused")
        monkeypatch.setattr(registry, "_owner_designated_repo_target", lambda e: e["gitUrl"])
        fetch = _install_fetch(monkeypatch, "attempted")

        await registry._resolve_manifest(_row(1))

        assert fetch.remote == ["app-1"]

    @pytest.mark.asyncio
    async def test_another_user_at_the_same_host_is_still_asked(
        self, cache_dir, anonymous, monkeypatch
    ):
        registry._note_ssh_sign_in(_HOST, "refused")
        fetch = _install_fetch(monkeypatch, "attempted")

        await registry._resolve_manifest(_row(1, f"ssh://bob@{_HOST}/pkg/app-1"))

        assert fetch.remote == ["app-1"]

    @pytest.mark.asyncio
    async def test_an_https_row_is_never_skipped(self, cache_dir, anonymous, monkeypatch):
        fetch = _install_fetch(monkeypatch, "refused")

        await asyncio.gather(
            *[registry._resolve_manifest(_row(i, f"https://{_HOST}/pkg/app-{i}")) for i in range(5)]
        )

        assert len(fetch.remote) == 5

    @pytest.mark.asyncio
    async def test_a_probe_that_raises_releases_the_rows_waiting_on_it(
        self, cache_dir, anonymous, monkeypatch
    ):
        fetch = _install_fetch(monkeypatch, "refused")
        fetch.fail_first = True

        results = await await_until(
            asyncio.gather(
                *[registry._resolve_manifest(_row(i)) for i in range(3)], return_exceptions=True
            )
        )

        assert sum(isinstance(r, RuntimeError) for r in results) == 1
        # The next row probed in turn, was refused, and the last one was skipped.
        assert len(fetch.remote) == 2
        assert len(fetch.local) == 1

    @pytest.mark.asyncio
    async def test_an_installed_row_never_waits_for_the_probe(
        self, cache_dir, anonymous, monkeypatch
    ):
        monkeypatch.setattr(manifests_module, "_SSH_SIGN_IN_PROBE_WAIT", 3600.0)
        fetch = _install_fetch(monkeypatch, "attempted")
        fetch.installed = {"app-1"}
        _refused, held = await registry._ssh_sign_in_refused(_HOST)
        try:
            row = await await_until(asyncio.ensure_future(registry._resolve_manifest(_row(1))))
        finally:
            registry._release_ssh_probe(_HOST, held)

        assert row["name"] == "app-1"
        assert fetch.remote == [] and fetch.local == []

    @pytest.mark.asyncio
    async def test_a_refresh_asks_a_refused_destination_again(self, cache_dir, monkeypatch):
        registry._note_ssh_sign_in(_HOST, "refused")
        monkeypatch.setattr(registry, "_effective_registries", lambda: [])

        async def _no_listing():
            return []

        monkeypatch.setattr(registry, "list_registry", _no_listing)

        await registry.refresh_registries()

        assert await registry._ssh_sign_in_refused(_HOST) != (True, None)


class TestTheMemory:
    @pytest.mark.asyncio
    async def test_the_rows_after_the_first_wait_for_its_answer(self):
        refused, probe = await registry._ssh_sign_in_refused(_HOST)
        assert (refused, probe is not None) == (False, True)
        waiter = asyncio.ensure_future(registry._ssh_sign_in_refused(_HOST))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not waiter.done()

        registry._note_ssh_sign_in(_HOST, "refused")
        registry._release_ssh_probe(_HOST, probe)

        assert await await_until(waiter) == (True, None)

    @pytest.mark.asyncio
    async def test_a_probe_that_learned_nothing_hands_over_to_the_next_row(self):
        _refused, first = await registry._ssh_sign_in_refused(_HOST)
        waiter = asyncio.ensure_future(registry._ssh_sign_in_refused(_HOST))
        await asyncio.sleep(0)

        registry._note_ssh_sign_in(_HOST, None)
        registry._release_ssh_probe(_HOST, first)

        refused, second = await await_until(waiter)
        assert refused is False
        assert second is not None and second is not first
        registry._release_ssh_probe(_HOST, second)

    @pytest.mark.asyncio
    async def test_a_row_stops_waiting_for_a_probe_that_hangs(self, monkeypatch):
        monkeypatch.setattr(manifests_module, "_SSH_SIGN_IN_PROBE_WAIT", 0.01)
        _refused, held = await registry._ssh_sign_in_refused(_HOST)
        try:
            answer = await await_until(asyncio.ensure_future(registry._ssh_sign_in_refused(_HOST)))
        finally:
            registry._release_ssh_probe(_HOST, held)

        # The row fetches without the probe's answer, and the probe stays the probe.
        assert answer == (False, None)
        assert not held.cancelled()

    @pytest.mark.asyncio
    async def test_a_refusal_lapses_after_its_window(self, monkeypatch):
        clock = ManualClock()
        clock.install(monkeypatch, manifests_module)
        registry._note_ssh_sign_in(_HOST, "refused")
        assert await registry._ssh_sign_in_refused(_HOST) == (True, None)

        clock.advance(registry._SSH_SIGN_IN_REFUSED_TTL + 1)

        refused, probe = await registry._ssh_sign_in_refused(_HOST)
        assert refused is False and probe is not None
        registry._release_ssh_probe(_HOST, probe)

    def test_a_refusal_after_an_answer_takes_over(self):
        registry._note_ssh_sign_in(_HOST, "attempted")
        registry._note_ssh_sign_in(_HOST, "refused")

        assert _HOST not in registry._SSH_SIGN_IN_ANSWERED
        assert _HOST in registry._SSH_SIGN_IN_REFUSED

    def test_the_memory_is_bounded(self, monkeypatch):
        monkeypatch.setattr(manifests_module, "_SSH_SIGN_IN_MEMORY_MAX", 3)
        for i in range(3):
            registry._note_ssh_sign_in(f"u{i}@{_HOST}", "attempted")

        registry._note_ssh_sign_in(f"u9@{_HOST}", "refused")

        assert registry._SSH_SIGN_IN_ANSWERED == set()
        assert list(registry._SSH_SIGN_IN_REFUSED) == [f"u9@{_HOST}"]

    @pytest.mark.asyncio
    async def test_in_flight_probes_count_toward_the_cap(self, monkeypatch, caplog):
        monkeypatch.setattr(manifests_module, "_SSH_SIGN_IN_MEMORY_MAX", 2)
        held = [(await registry._ssh_sign_in_refused(f"u{i}@{_HOST}"))[1] for i in range(2)]
        late = f"u2@{_HOST}"

        with caplog.at_level("WARNING"):
            assert await registry._ssh_sign_in_refused(late) == (False, None)
            assert await registry._ssh_sign_in_refused(f"u3@{_HOST}") == (False, None)
        registry._note_ssh_sign_in(late, "refused")

        # Turned away: no row in any store, and one log line for the two turned away.
        assert late not in registry._SSH_SIGN_IN_PROBES
        assert late not in registry._SSH_SIGN_IN_REFUSED
        assert late not in registry._SSH_SIGN_IN_ANSWERED
        assert [r.levelname for r in caplog.records if "sign-in memory" in r.message] == ["WARNING"]

        registry._release_ssh_probe(f"u0@{_HOST}", held[0])
        refused, probe = await registry._ssh_sign_in_refused(late)
        assert refused is False and probe is not None
        for i, done in ((1, held[1]), (2, probe)):
            registry._release_ssh_probe(f"u{i}@{_HOST}", done)


class TestTheRefusalLine:
    @pytest.mark.parametrize(
        "stderr",
        [
            _REFUSED.decode(),
            "kieser@git.example.com: Permission denied (keyboard-interactive,publickey).",
            "Permission denied (publickey,password).",
            "git@[fd00::1]: Permission denied (publickey)",
        ],
    )
    def test_ssh_s_own_line_is_a_refusal(self, stderr):
        assert registry._is_ssh_sign_in_refusal(stderr)

    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: '/Permission denied (publickey)' does not appear to be a git repository",
            "fatal: '/x/git@h: Permission denied (publickey).' does not appear to be a git repository",
            "git@git.example.com: Permission denied (publickey). and more",
            "remote: git@git.example.com: Permission denied (publickey).",
            "fatal: could not create work tree dir 'x': Permission denied",
            "git@git.example.com: Permission denied ().",
        ],
    )
    def test_the_phrase_inside_another_line_is_not(self, stderr):
        assert not registry._is_ssh_sign_in_refusal(stderr)


class _CloneProc:
    """A spawned ``git clone`` that exits *rc* with *stderr*."""

    def __init__(self, rc: int, stderr: bytes) -> None:
        self.returncode = rc
        self.pid = UNALLOCATABLE_PID
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


class TestTheFetchReportsTheSignIn:
    @pytest.fixture()
    def clone(self, monkeypatch, tmp_path):
        state: dict[str, Any] = {"rc": 128, "stderr": _REFUSED, "spawned": 0}

        async def _spawn(*argv, **kwargs):
            state["spawned"] += 1
            return _CloneProc(state["rc"], state["stderr"])

        monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **k: (list(cmd), None))
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
        monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / "no-such-clone")
        return state

    async def _fetch(self, before_remote=None) -> dict[str, str]:
        outcome: dict[str, str] = {}
        url = f"ssh://{_HOST}/pkg/demo"
        manifest = await registry._fetch_app_manifest(
            url, "main", git_url=url, outcome=outcome, before_remote=before_remote
        )
        assert manifest is None
        return outcome

    @pytest.mark.asyncio
    async def test_an_ssh_refusal_is_reported(self, clone):
        assert await self._fetch() == {"ssh_sign_in": "refused"}

    @pytest.mark.asyncio
    async def test_a_repository_level_denial_is_not_a_refusal(self, clone):
        clone["stderr"] = _REPO_DENIED

        assert await self._fetch() == {"ssh_sign_in": "attempted"}

    @pytest.mark.asyncio
    async def test_a_repository_path_that_spells_the_refusal_is_not_a_refusal(self, clone):
        # An index chooses the path, and git echoes it inside its own error.
        clone["stderr"] = (
            b"fatal: '/Permission denied (publickey)' does not appear to be a git repository\n"
            b"fatal: Could not read from remote repository.\n"
        )

        assert await self._fetch() == {"ssh_sign_in": "attempted"}

    @pytest.mark.asyncio
    async def test_a_closed_gate_skips_the_remote(self, clone):
        async def _closed() -> bool:
            return False

        assert await self._fetch(_closed) == {}
        assert clone["spawned"] == 0

    @pytest.mark.asyncio
    async def test_an_installed_copy_answers_before_the_gate_is_asked(
        self, clone, monkeypatch, tmp_path
    ):
        installed = tmp_path / "installed"
        installed.mkdir()
        (installed / "app.json").write_text('{"name": "demo"}', encoding="utf-8")

        async def _matches(*args, **kwargs) -> bool:
            return True

        monkeypatch.setattr(registry, "app_source_dir", lambda name: installed)
        monkeypatch.setattr(registry, "_clone_branch_matches", _matches)
        monkeypatch.setattr(registry, "_clone_origin_matches", _matches)
        asked: list[bool] = []

        async def _open() -> bool:
            asked.append(True)
            return True

        outcome: dict[str, str] = {}
        url = f"ssh://{_HOST}/pkg/demo"
        manifest = await registry._fetch_app_manifest(
            url, "main", app_name="demo", git_url=url, outcome=outcome, before_remote=_open
        )

        assert manifest == {"name": "demo"}
        assert asked == [] and outcome == {}
        assert clone["spawned"] == 0

    @pytest.mark.asyncio
    async def test_a_host_the_ssrf_gate_refuses_reports_nothing(self, clone, monkeypatch):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: False)
        asked: list[bool] = []

        async def _open() -> bool:
            asked.append(True)
            return True

        assert await self._fetch(_open) == {}
        assert clone["spawned"] == 0
        assert asked == []
