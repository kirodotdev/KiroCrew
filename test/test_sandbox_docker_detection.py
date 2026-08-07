"""Tests for Docker container detection in sandbox.py.

Covers:
- ``is_docker_container()`` probe on all detection paths
- ``wrap_argv()`` Docker-specific error guidance when no user-namespace
  backend is available inside a container

Related issue: https://github.com/kirodotdev/KiroCrew/issues/1617
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, mock_open, patch

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Docker detection is Linux-only")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _clear_cache() -> None:
    from kiro_crew.sandbox import is_docker_container

    is_docker_container.cache_clear()


# ── is_docker_container() ─────────────────────────────────────────────────────


class TestIsDockerContainer:
    """Unit tests for :func:`kiro_crew.sandbox.is_docker_container`."""

    def setup_method(self) -> None:
        _clear_cache()

    def teardown_method(self) -> None:
        _clear_cache()

    def test_dockerenv_file_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``/.dockerenv`` present → primary Docker signal."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda p: p == "/.dockerenv")
        monkeypatch.delenv("CONTAINER", raising=False)
        assert sandbox.is_docker_container() is True

    def test_containerenv_file_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/run/.containerenv present → Podman OCI marker."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda p: p == "/run/.containerenv")
        monkeypatch.delenv("CONTAINER", raising=False)
        assert sandbox.is_docker_container() is True

    def test_container_env_var_oci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CONTAINER=oci`` env var → Podman rootless signal."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: False)
        monkeypatch.setenv("CONTAINER", "oci")
        assert sandbox.is_docker_container() is True

    def test_container_env_var_non_oci_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Other ``CONTAINER`` values don't trigger the fast path."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: False)
        monkeypatch.setenv("CONTAINER", "lxc")
        # Falls through to cgroup check — no docker marker in cgroup → False
        bare_cgroup = "0::/user.slice/user-1000.slice/session-1.scope\n"
        with patch("builtins.open", mock_open(read_data=bare_cgroup)):
            assert sandbox.is_docker_container() is False

    def test_cgroup_contains_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/proc/1/cgroup with 'docker' → fallback detection."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: False)
        monkeypatch.delenv("CONTAINER", raising=False)
        cgroup = "12:blkio:/docker/abc123\n0::/docker/abc123\n"
        with patch("builtins.open", mock_open(read_data=cgroup)):
            assert sandbox.is_docker_container() is True

    def test_cgroup_contains_containerd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/proc/1/cgroup with 'containerd' → containerd-managed container."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: False)
        monkeypatch.delenv("CONTAINER", raising=False)
        cgroup = "0::/system.slice/containerd.service\n"
        with patch("builtins.open", mock_open(read_data=cgroup)):
            assert sandbox.is_docker_container() is True

    def test_cgroup_contains_kubepods(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/proc/1/cgroup with 'kubepods' → Kubernetes pod."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: False)
        monkeypatch.delenv("CONTAINER", raising=False)
        cgroup = "0::/kubepods/burstable/pod123/container456\n"
        with patch("builtins.open", mock_open(read_data=cgroup)):
            assert sandbox.is_docker_container() is True

    def test_bare_metal_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No container markers → False on a normal Linux host."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: False)
        monkeypatch.delenv("CONTAINER", raising=False)
        cgroup = "0::/user.slice/user-1000.slice/session-1.scope\n"
        with patch("builtins.open", mock_open(read_data=cgroup)):
            assert sandbox.is_docker_container() is False

    def test_cgroup_unreadable_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unreadable /proc/1/cgroup → False (fail-safe, not an exception)."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: False)
        monkeypatch.delenv("CONTAINER", raising=False)
        with patch("builtins.open", side_effect=OSError("permission denied")):
            assert sandbox.is_docker_container() is False

    def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """lru_cache means the probe runs only once per process."""
        from kiro_crew import sandbox

        calls: list[str] = []

        def counting_exists(path: str) -> bool:
            calls.append(path)
            return path == "/.dockerenv"

        monkeypatch.setattr(sandbox.os.path, "exists", counting_exists)
        monkeypatch.delenv("CONTAINER", raising=False)

        assert sandbox.is_docker_container() is True
        assert sandbox.is_docker_container() is True  # second call — cached
        # os.path.exists was NOT called a second time for /.dockerenv
        assert calls.count("/.dockerenv") == 1


# ── wrap_argv() Docker guidance ───────────────────────────────────────────────


class TestWrapArgvDockerGuidance:
    """wrap_argv() should produce Docker-specific guidance inside a container."""

    def _patch_no_backend_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch sandbox state: no backend, inside container, not macOS sandbox."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "detect_backend", lambda **_kw: "none")
        monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)
        monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "_inside_macos_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "is_docker_container", lambda: True)
        monkeypatch.setattr(
            sandbox,
            "_last_unshare_failure",
            (False, "unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)", ""),
        )
        # Stub SEL so no real I/O happens.
        fake_sel = MagicMock()
        fake_sel.return_value = fake_sel
        monkeypatch.setattr(sandbox, "sel", fake_sel, raising=False)

    @pytest.mark.asyncio
    async def test_docker_guidance_mentions_seccomp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Error message references the seccomp option."""
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        self._patch_no_backend_docker(monkeypatch)
        with pytest.raises(SandboxUnavailableError) as exc_info:
            wrap_argv(["kiro-cli", "chat"], mode="auto")
        assert "seccomp" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_docker_guidance_mentions_allow_unsandboxed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Error message references KIROCREW_ALLOW_UNSANDBOXED."""
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        self._patch_no_backend_docker(monkeypatch)
        with pytest.raises(SandboxUnavailableError) as exc_info:
            wrap_argv(["kiro-cli", "chat"], mode="auto")
        assert "KIROCREW_ALLOW_UNSANDBOXED" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_docker_guidance_does_not_say_install_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docker guidance must NOT tell users to 'install a supported sandbox backend'."""
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        self._patch_no_backend_docker(monkeypatch)
        with pytest.raises(SandboxUnavailableError) as exc_info:
            wrap_argv(["kiro-cli", "chat"], mode="auto")
        assert "install a supported sandbox backend" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_docker_error_kind_is_no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Error kind is 'no_backend', not 'transient' or 'foreign_sandbox'."""
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        self._patch_no_backend_docker(monkeypatch)
        with pytest.raises(SandboxUnavailableError) as exc_info:
            wrap_argv(["kiro-cli", "chat"], mode="auto")
        assert exc_info.value.kind == "no_backend"

    @pytest.mark.asyncio
    async def test_bare_metal_no_backend_gets_generic_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On bare-metal Linux without user namespaces the generic message is used."""
        from kiro_crew import sandbox
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        monkeypatch.setattr(sandbox, "detect_backend", lambda **_kw: "none")
        monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)
        monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "_inside_macos_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "is_docker_container", lambda: False)
        # The guidance branch is chosen by reading the REAL
        # /proc/sys/kernel/apparmor_restrict_unprivileged_userns. Ubuntu 23.10+
        # ships that as 1 — including the GitHub-hosted runners — so without this
        # stub the AppArmor branch answers instead of the generic one and the
        # assertion below fails on CI while passing on any host that lacks the
        # restriction. "Bare metal, no backend" is a claim about the scenario, not
        # about the machine running the test.
        monkeypatch.setattr(sandbox, "_apparmor_userns_restricted", lambda: False)
        monkeypatch.setattr(
            sandbox,
            "_last_unshare_failure",
            (False, "unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)", ""),
        )
        fake_sel = MagicMock()
        fake_sel.return_value = fake_sel
        monkeypatch.setattr(sandbox, "sel", fake_sel, raising=False)

        with pytest.raises(SandboxUnavailableError) as exc_info:
            wrap_argv(["kiro-cli", "chat"], mode="auto")
        assert "install a supported sandbox backend" in str(exc_info.value)
        assert "KIROCREW_ALLOW_UNSANDBOXED" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_apparmor_restricted_host_gets_profile_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On an AppArmor-restricted host the remedy is the profile, not the opt-out.

        The counterpart of the generic case above. Both branches are now asserted
        against an EXPLICIT host state, so neither is decided by whatever kernel
        happens to run the suite — which is how the generic case came to fail on
        Ubuntu runners while passing locally.
        """
        from kiro_crew import sandbox
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        monkeypatch.setattr(sandbox, "detect_backend", lambda **_kw: "none")
        monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)
        monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "_inside_macos_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "is_docker_container", lambda: False)
        monkeypatch.setattr(sandbox, "_apparmor_userns_restricted", lambda: True)
        monkeypatch.delenv("APPIMAGE", raising=False)
        monkeypatch.setattr(
            sandbox,
            "_last_unshare_failure",
            (False, "unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)", ""),
        )
        fake_sel = MagicMock()
        fake_sel.return_value = fake_sel
        monkeypatch.setattr(sandbox, "sel", fake_sel, raising=False)

        if not sys.platform.startswith("linux"):
            pytest.skip("AppArmor userns restriction is a Linux-only branch")

        with pytest.raises(SandboxUnavailableError) as exc_info:
            wrap_argv(["kiro-cli", "chat"], mode="auto")
        message = str(exc_info.value)
        assert "apparmor_restrict_unprivileged_userns" in message
        # The profile is offered before the opt-out, and the opt-out is still named.
        assert message.index("kirocrew service install") < message.index("last resort")
