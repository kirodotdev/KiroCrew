"""Credential posture of registry clones.

A clone whose URL an external index chose runs credential-free (``anonymous_git_env``
and the strict sandbox), a clone of the owner's own registry URL keeps the owner's
credentials (the same-repo carve-out), and only the owner's clones ask for the
ssh-agent socket.

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401

# ---------------------------------------------------------------------------
# Install-path confused-deputy defense: credential-free clone for entries whose
# repo URL originates from an owner-configured EXTERNAL registry index.
# ---------------------------------------------------------------------------


class TestInstallPathCredentialPosture:
    """An app entry that came from an external index carries ``_registry`` and
    its ``repo`` URL is index-controlled; installing it must clone
    credential-free (anonymous_git_env + strict sandbox), while a bundled
    (curated) entry keeps the owner's ambient git identity via minimal_env."""

    @pytest.mark.asyncio
    async def test_index_originated_install_propagates_credential_free_flag(self):
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                # Entry carries the external-index provenance marker.
                return_value={
                    "name": "acme-app",
                    "repo": "https://github.com/acme/private-sibling.git",
                    "branch": "main",
                    "_registry": "acme",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "acme-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
        ):
            await install_from_registry("acme-app")

        assert captured.get("index_originated") is True

    @pytest.mark.asyncio
    async def test_bundled_install_keeps_owner_credentials(self):
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                # Bundled/curated entry — no ``_registry`` marker.
                return_value={
                    "name": "bundled-app",
                    "repo": "https://github.com/kirodotdev/bundled-app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "bundled-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
        ):
            await install_from_registry("bundled-app")

        assert captured.get("index_originated") is False

    @pytest.mark.asyncio
    async def test_git_clone_or_pull_index_originated_uses_anonymous_env(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", None)

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        dest = tmp_path / "clone-dest"  # does not exist → fresh-clone path
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
        ):
            err = await reg._git_clone_or_pull(
                "https://github.com/acme/private-sibling.git",
                "main",
                dest,
                [],
                index_originated=True,
            )

        assert err is None
        env = captured["env"]
        # Anonymous / credential-free markers must be present.
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_GLOBAL" in env
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        # No ambient SSH agent handed through.
        assert "SSH_AUTH_SOCK" not in env
        # Strict OS sandbox (~/.ssh hidden) is forced.
        assert captured["mode"] == "strict"

    @pytest.mark.asyncio
    async def test_git_clone_or_pull_owner_designated_uses_minimal_env(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", None)

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        url = "https://github.com/kirodotdev/bundled-app.git"
        dest = tmp_path / "clone-dest"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
        ):
            err = await reg._git_clone_or_pull(url, "main", dest, [], index_originated=False)

        assert err is None
        env = captured["env"]
        # minimal_env carries NONE of the anonymous credential-suppression keys.
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        # Sandbox mode is the host-derived context decision, not forced strict.
        assert captured["mode"] == reg._context_clone_sandbox_mode(url)


# ---------------------------------------------------------------------------
# Same-repo credential carve-out (companion to confused-deputy defense).
# When an index entry's effective clone URL is byte-identical to the owner-
# configured registry repo URL, the clone uses owner credentials instead of
# the anonymous+strict posture. Sibling repos on the same host stay anonymous.
# ---------------------------------------------------------------------------


class TestSameRepoCredentialCarveOut:
    """The same-repo carve-out: owner-configured registry URL gets credentials;
    sibling repos on the same host remain anonymous+strict; bundled unchanged."""

    @pytest.mark.asyncio
    async def test_same_repo_install_uses_owner_credentials(self):
        """Entry whose clone URL == registry config repo → credentialed install."""
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(
                name="internal", repo="ssh://git.example.com/team/MyRegistry", branch="main"
            )
        ]

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "my-app",
                    "repo": "ssh://git.example.com/team/MyRegistry",
                    "gitUrl": "ssh://git.example.com/team/MyRegistry",
                    "branch": "main",
                    "subdirectory": "apps/my-app",
                    "_registry": "internal",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "my-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock_config,
            ),
        ):
            await install_from_registry("my-app")

        # Same-repo carve-out: index_originated flipped to False → credentialed.
        assert captured.get("index_originated") is False

    @pytest.mark.asyncio
    async def test_sibling_repo_same_host_stays_anonymous(self):
        """Entry pointing at a DIFFERENT repo on the same host → still anonymous."""
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(
                name="internal", repo="ssh://git.example.com/team/MyRegistry", branch="main"
            )
        ]

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                # repo points at a DIFFERENT package on the same host — the exact
                # confused-deputy scenario the defense exists for.
                return_value={
                    "name": "sibling-app",
                    "repo": "ssh://git.example.com/team/OtherRepo",
                    "gitUrl": "ssh://git.example.com/team/OtherRepo",
                    "branch": "main",
                    "_registry": "internal",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "sibling-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock_config,
            ),
        ):
            await install_from_registry("sibling-app")

        # Sibling repo on the same host: confused-deputy defense applies.
        assert captured.get("index_originated") is True

    @pytest.mark.asyncio
    async def test_bundled_entry_unchanged_by_carve_out(self):
        """Bundled entry (no _registry marker) → still owner-designated."""
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        mock_config = MagicMock()
        mock_config.registries = []

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "bundled-app",
                    "repo": "https://github.com/kirodotdev/bundled-app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "bundled-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock_config,
            ),
        ):
            await install_from_registry("bundled-app")

        # Bundled entries have no _registry → index_originated stays False.
        assert captured.get("index_originated") is False

    @pytest.mark.asyncio
    async def test_same_repo_manifest_fetch_uses_owner_credentials(self):
        """_fetch_app_manifest with owner_designated=True uses minimal_env."""
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=MagicMock(is_file=MagicMock(return_value=False)),
            ),
        ):
            await reg._fetch_app_manifest(
                "ssh://git.example.com/team/MyRegistry",
                "main",
                "",
                app_name="",
                git_url="ssh://git.example.com/team/MyRegistry",
                owner_designated=True,
            )

        # Owner-designated: minimal_env (no credential suppression) + context sandbox.
        env = captured["env"]
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        # SSH host is trusted → context mode is standard (not forced strict).
        assert captured["mode"] == reg._context_clone_sandbox_mode(
            "ssh://git.example.com/team/MyRegistry"
        )

    @pytest.mark.asyncio
    async def test_default_manifest_fetch_stays_anonymous(self):
        """_fetch_app_manifest without owner_designated uses anonymous+strict."""
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=MagicMock(is_file=MagicMock(return_value=False)),
            ),
        ):
            await reg._fetch_app_manifest(
                "ssh://git.example.com/team/SiblingRepo",
                "main",
                "",
                app_name="",
                git_url="ssh://git.example.com/team/SiblingRepo",
                owner_designated=False,
            )

        # Default (not owner-designated): anonymous env + strict sandbox.
        env = captured["env"]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_GLOBAL" in env
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        assert "SSH_AUTH_SOCK" not in env
        assert captured["mode"] == "strict"

    @pytest.mark.asyncio
    async def test_same_repo_clone_or_pull_uses_owner_credentials(self, tmp_path):
        """_git_clone_or_pull with index_originated=False (same-repo carve-out)
        uses minimal_env + context sandbox — matching the existing
        test_git_clone_or_pull_owner_designated_uses_minimal_env test."""
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", None)

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        url = "ssh://git.example.com/team/MyRegistry"
        dest = tmp_path / "clone-dest"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
        ):
            err = await reg._git_clone_or_pull(url, "main", dest, [], index_originated=False)

        assert err is None
        env = captured["env"]
        # Owner-designated: minimal_env without credential suppression.
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        # Context sandbox mode for trusted SSH host → standard.
        assert captured["mode"] == reg._context_clone_sandbox_mode(url)

    def test_credential_grant_is_sel_audited(self):
        """The owner-designated credential escalation leaves an SEL record.

        Escalating from anonymous+strict to owner credentials is a
        permission decision; it must be auditable like the index fetch.
        """
        from kiro_crew.apps import registry as reg

        sel = MagicMock()
        with patch.object(reg, "_sel_fn", return_value=sel):
            reg._sel_credential_grant("fetch_app_manifest", "ssh://git.example.com/team/MyRegistry")

        sel.log_api_access.assert_called_once()
        kwargs = sel.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "registry"
        assert kwargs["operation"] == "fetch_app_manifest"
        assert kwargs["outcome"] == "granted"
        assert "ssh://git.example.com/team/MyRegistry" in kwargs["resources"]

    def test_credential_grant_audit_is_best_effort(self):
        """A failing (or absent) SEL backend never breaks the clone path."""
        from kiro_crew.apps import registry as reg

        broken = MagicMock()
        broken.log_api_access.side_effect = RuntimeError("sel down")
        with patch.object(reg, "_sel_fn", return_value=broken):
            reg._sel_credential_grant("install_from_registry", "ssh://git.example.com/team/X")
        with patch.object(reg, "_sel_fn", None):
            reg._sel_credential_grant("install_from_registry", "ssh://git.example.com/team/X")

    def test_is_owner_designated_repo_exact_match(self):
        """Predicate returns True only for byte-identical match to config repo."""
        from kiro_crew.apps.registry import _is_owner_designated_repo

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(
                name="internal", repo="ssh://git.example.com/team/MyRegistry", branch="main"
            )
        ]

        entry_same = {
            "name": "app",
            "repo": "ssh://git.example.com/team/MyRegistry",
            "gitUrl": "ssh://git.example.com/team/MyRegistry",
            "_registry": "internal",
        }
        entry_sibling = {
            "name": "app",
            "repo": "ssh://git.example.com/team/OtherRepo",
            "gitUrl": "ssh://git.example.com/team/OtherRepo",
            "_registry": "internal",
        }
        entry_bundled = {
            "name": "app",
            "repo": "https://github.com/kirodotdev/app.git",
        }

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_config):
            assert _is_owner_designated_repo(entry_same) is True
            # Different repo on the same host → NOT owner-designated.
            assert _is_owner_designated_repo(entry_sibling) is False
            # Bundled (no _registry) → returns False (but irrelevant since
            # bundled entries never set index_originated=True).
            assert _is_owner_designated_repo(entry_bundled) is False

    def test_is_owner_designated_repo_no_normalization(self):
        """No URL normalization — trailing slash difference is NOT a match."""
        from kiro_crew.apps.registry import _is_owner_designated_repo

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(name="r", repo="ssh://git.example.com/team/Repo", branch="main")
        ]

        # Trailing slash → not byte-identical → not owner-designated.
        entry = {
            "name": "app",
            "gitUrl": "ssh://git.example.com/team/Repo/",
            "_registry": "r",
        }
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_config):
            assert _is_owner_designated_repo(entry) is False


# --- Owner registry clones keep the ssh-agent socket; index-chosen clones do not ---
#
# On a host whose git server takes only agent-held SSH keys, a registry clone run
# with ``SSH_AUTH_SOCK`` unset can never authenticate. The owner's own clones (the
# configured registry index, an owner-designated install) ask ``wrap_argv`` to
# forward the socket; ``wrap_argv`` still re-judges it per tier. A clone whose URL
# came from an index entry (``index_originated``) must never ask -- that is the
# confused-deputy guard.

_SSH_REGISTRY_URL = "git@code.example.com:team/apps-index.git"


class _SshFakeProc:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        # Above every pid_max, so a cleanup signal reaches no live process.
        self.pid = 99_999_999_999
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr

    def kill(self) -> None:  # pragma: no cover - only the timeout path calls this
        return None


def _ssh_capture(monkeypatch, module, calls: list[dict], stderr: bytes = b"") -> None:
    """Record each ``wrap_argv_async`` call's argv + kwargs; every network spawn fails."""
    from kiro_crew.apps.registry_pipeline import checkout

    async def _wrap(argv: list[str], **kwargs: object) -> tuple[list[str], None]:
        calls.append({"argv": list(argv), **kwargs})
        return list(argv), None

    async def _spawn(*argv: str, **kwargs: object) -> _SshFakeProc:
        # init / remote add succeed so the pinned path reaches its network fetch.
        ok = any(tok in ("init", "remote") for tok in argv[:12]) and "fetch" not in argv
        return _SshFakeProc(0 if ok else 128, stderr)

    async def _noop(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr(module, "wrap_argv_async", _wrap)
    monkeypatch.setattr(module, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(module, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(checkout, "_git_transport_env", lambda *a, **k: {})
    monkeypatch.setattr(checkout, "_rmtree_force_settled", _noop)
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True)


def _ssh_calls_with(calls: list[dict], sub: str) -> list[dict]:
    return [c for c in calls if sub in c["argv"]]


@pytest.mark.asyncio
async def test_owner_configured_index_clone_forwards_the_agent_socket(monkeypatch):
    from kiro_crew.apps.registry_pipeline import indexes

    calls: list[dict] = []
    _ssh_capture(monkeypatch, indexes, calls)
    monkeypatch.setattr(indexes, "_context_clone_sandbox_mode", lambda url: "standard")
    assert await indexes._fetch_external_registry_index(_SSH_REGISTRY_URL, "main") is None
    (clone,) = _ssh_calls_with(calls, "clone")
    assert clone["mode"] == "standard"
    assert clone["forward_ssh_auth_sock"] is True


@pytest.mark.asyncio
async def test_index_clone_failure_logs_a_bounded_stderr_tail(monkeypatch, caplog):
    from kiro_crew.apps.registry_pipeline import indexes

    calls: list[dict] = []
    stderr = b"x" * 2000 + b"Permission denied (publickey)."
    _ssh_capture(monkeypatch, indexes, calls, stderr=stderr)
    monkeypatch.setattr(indexes, "_context_clone_sandbox_mode", lambda url: "standard")
    with caplog.at_level(logging.WARNING, logger=indexes.logger.name):
        await indexes._fetch_external_registry_index(_SSH_REGISTRY_URL, "main")
    (record,) = [r for r in caplog.records if "registry clone failed" in r.getMessage()]
    assert "Permission denied (publickey)." in record.getMessage()
    assert len(record.args[0]) <= 400


@pytest.mark.parametrize("pinned", [False, True], ids=["branch", "pinned-commit"])
@pytest.mark.parametrize("index_originated", [False, True], ids=["owner", "index-entry"])
@pytest.mark.asyncio
async def test_install_clone_forwards_the_agent_socket_only_for_an_owner_install(
    monkeypatch, tmp_path: Path, index_originated: bool, pinned: bool
):
    from kiro_crew.apps.registry_pipeline import checkout

    calls: list[dict] = []
    _ssh_capture(monkeypatch, checkout, calls)
    monkeypatch.setattr(checkout, "_context_clone_sandbox_mode", lambda url: "standard")
    dest = tmp_path / "apps" / "demo"
    commit = "a" * 40 if pinned else ""
    result = await checkout._git_clone_or_pull(
        _SSH_REGISTRY_URL, "main", dest, [], index_originated=index_originated, commit=commit
    )
    assert result is not None and result["ok"] is False
    network = _ssh_calls_with(calls, "fetch" if pinned else "clone")
    assert network, calls
    assert all(c["forward_ssh_auth_sock"] is (not index_originated) for c in network)
    # The socket is for the network step only: local init/checkout never get it.
    local = [c for c in calls if c not in network]
    assert all(c.get("forward_ssh_auth_sock", False) is False for c in local)


# --- The rest of the owner posture asks for the socket the same way ---
#
# A same-repo manifest fetch, the browse-time blob proxy and the owner-tier art
# prewarm run in the owner posture (``minimal_env`` plus the context sandbox mode),
# so on a host that only takes agent-held SSH keys they need the socket too. A
# fetch for any other URL stays anonymous and never asks.


@pytest.mark.parametrize("pinned", [False, True], ids=["branch", "pinned-commit"])
@pytest.mark.parametrize("owner_designated", [False, True], ids=["default", "owner"])
@pytest.mark.asyncio
async def test_manifest_fetch_asks_for_the_agent_socket_only_for_the_owners_url(
    monkeypatch, owner_designated: bool, pinned: bool
):
    from kiro_crew.apps.registry_pipeline import checkout, manifests

    calls: list[dict] = []
    _ssh_capture(monkeypatch, manifests, calls)
    _ssh_capture(monkeypatch, checkout, calls)
    monkeypatch.setattr(manifests, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(manifests, "_context_clone_sandbox_mode", lambda url: "standard")
    monkeypatch.setattr(manifests, "_sel_credential_grant", lambda operation, url: None)
    commit = "a" * 40 if pinned else ""
    manifest = await manifests._fetch_app_manifest(
        _SSH_REGISTRY_URL,
        "main",
        git_url=_SSH_REGISTRY_URL,
        owner_designated=owner_designated,
        commit=commit,
    )
    assert manifest is None
    network = _ssh_calls_with(calls, "fetch" if pinned else "clone")
    assert network, calls
    asked = [c.get("forward_ssh_auth_sock", False) for c in network]
    assert asked == [owner_designated] * len(network)
    # The socket is for the network step only: local init/remote steps never get it.
    local = [c for c in calls if c not in network]
    assert all(c.get("forward_ssh_auth_sock", False) is False for c in local)


@pytest.mark.parametrize("forward", [False, True], ids=["default", "forwarded"])
@pytest.mark.asyncio
async def test_branch_fetch_asks_for_the_agent_socket_only_when_told_to(
    monkeypatch, tmp_path: Path, forward: bool
):
    from kiro_crew.apps.registry_pipeline import checkout

    calls: list[dict] = []
    _ssh_capture(monkeypatch, checkout, calls)
    result = await checkout._git_fetch_branch(
        _SSH_REGISTRY_URL,
        "main",
        tmp_path / "branch",
        [],
        clone_env={},
        sandbox_mode="standard",
        forward_ssh_auth_sock=forward,
    )
    assert result is not None and result["ok"] is False
    (fetch,) = _ssh_calls_with(calls, "fetch")
    assert fetch["forward_ssh_auth_sock"] is forward
    # The socket is for the network step only: local init/remote steps never get it.
    local = [c for c in calls if c is not fetch]
    assert all(c.get("forward_ssh_auth_sock", False) is False for c in local)
