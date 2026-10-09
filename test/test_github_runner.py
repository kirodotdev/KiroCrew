"""The shared hardened gh runner (``kiro_crew.github_runner``).

One module now owns trusted-binary resolution, the minimal child environment,
and the SEL-audited spawn chokepoint for every ``gh``-spawning surface (the
dashboard PR sidebar, Issue Radar, Code Review Sage). These tests lock in the
properties that would otherwise drift between the three callers:

* resolver precedence (caller override → ``KIROCREW_GH_BIN`` → candidates),
  including the fail-loud rule for an override that is SET but empty or wrong
  — silently ignoring a set override was the weaker of the historical
  behaviors and is pinned OUT here;
* the exact child environment: gh-scoped auth/network/TLS keys pass through,
  nothing else from a polluted gateway environment does (AWS/Slack/SSH), and
  ``pin_host`` pins ``GH_HOST`` for callers whose bare API paths cannot pass
  ``--hostname``;
* a SEL audit event on success, non-zero exit, and timeout for EVERY spawn —
  the property one of the three copies had silently lost;
* the re-export seams that keep the historical import locations working.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import urllib.parse
from unittest import mock

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    # NOT because the hardening is POSIX-only -- it is not. These
    # assertions pin the POSIX policy's own messages ("world-writable", "owned
    # by another user (uid ...)") and build `#!/bin/sh` gh stubs, none of which
    # the Windows branch produces or can execute. The Windows policy has its
    # own suite in test/test_windows_acl.py; porting these assertions to be
    # platform-agnostic is separate work.
    reason="asserts the POSIX branch's messages and fixtures (see test_windows_acl.py)",
)

from kiro_crew import github_runner as runner  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_runner_state(monkeypatch):
    runner.reset_cache()
    monkeypatch.delenv("KIROCREW_GH_BIN", raising=False)
    monkeypatch.delenv("_KIROCREW_GH_PREVALIDATED", raising=False)
    monkeypatch.delenv("KIROCREW_PROVIDER_BIN_STRICT", raising=False)
    monkeypatch.setattr(runner, "agent_writable_roots", lambda: ())
    yield
    runner.reset_cache()


def _fake_gh(directory, name: str = "gh") -> str:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return str(binary)


# ── resolve_gh ───────────────────────────────────────────────────────────────


class TestResolveGh:
    @pytest.fixture(autouse=True)
    def _hermetic_ownership(self, monkeypatch):
        """These tests pin resolution ORDER, caching, and messages — not the
        ownership policy (covered by the moved validation tests). The per-
        component ownership walk depends on who owns the host's tmp ancestry,
        so it is no-opped to keep the suite hermetic on any runner."""
        monkeypatch.setattr(
            runner,
            "check_provider_path_component",
            lambda path, *, label, uid, strict: None,
        )
        # Same reasoning for the Windows walk, which reads a real ACL and so
        # depends on the runner's own tmp ancestry just as much.
        monkeypatch.setattr(
            runner,
            "check_provider_path_component_windows",
            lambda path, *, label, me_sid, strict: None,
        )

    def test_caller_override_wins_over_generic_and_candidates(self, monkeypatch, tmp_path):
        caller = _fake_gh(tmp_path / "caller-bin")
        generic = _fake_gh(tmp_path / "generic-bin")
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("KIROCREW_TEST_GH", caller)
        monkeypatch.setenv("KIROCREW_GH_BIN", generic)
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == caller

    def test_generic_override_wins_when_caller_var_is_unset(self, monkeypatch, tmp_path):
        generic = _fake_gh(tmp_path / "generic-bin")
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.delenv("KIROCREW_TEST_GH", raising=False)
        monkeypatch.setenv("KIROCREW_GH_BIN", generic)
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == generic

    def test_candidates_are_used_without_any_override(self, monkeypatch, tmp_path):
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        assert runner.resolve_gh() == candidate

    def test_a_set_but_empty_override_fails_loudly(self, monkeypatch, tmp_path):
        """D4 lock-in: a SET override — even empty — is validated, never skipped.

        Silently falling through to the candidate scan would run a binary the
        operator was explicitly steering away from.
        """
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("KIROCREW_TEST_GH", "")
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )

        with pytest.raises(runner.SetupError, match="KIROCREW_TEST_GH.*path must be absolute"):
            runner.resolve_gh(override_env="KIROCREW_TEST_GH")

    def test_a_wrong_override_names_the_variable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_TEST_GH", str(tmp_path / "missing-gh"))

        with pytest.raises(runner.SetupError, match="KIROCREW_TEST_GH.*failed validation"):
            runner.resolve_gh(override_env="KIROCREW_TEST_GH")

    def test_strict_mode_refuses_path_hits(self, monkeypatch, tmp_path):
        planted = _fake_gh(tmp_path / "user-bin")
        monkeypatch.setenv("KIROCREW_PROVIDER_BIN_STRICT", "1")
        monkeypatch.setenv("PATH", str(tmp_path / "user-bin"))
        monkeypatch.setattr(
            runner,
            "PROVIDER_EXECUTABLE_CANDIDATES",
            {"gh": ("/nonexistent-kirocrew/gh",), "glab": ()},
        )

        with pytest.raises(runner.SetupError) as excinfo:
            runner.resolve_gh()
        # The user-owned PATH hit was never even considered a candidate.
        assert planted not in str(excinfo.value)

    def test_missing_gh_message_gives_install_guidance(self, monkeypatch):
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            runner,
            "PROVIDER_EXECUTABLE_CANDIDATES",
            {"gh": ("/nonexistent-kirocrew/gh",), "glab": ()},
        )

        with pytest.raises(runner.SetupError) as excinfo:
            runner.resolve_gh(override_env="KIROCREW_TEST_GH")

        message = str(excinfo.value)
        assert "brew install gh" in message
        assert "gh auth login" in message
        assert "KIROCREW_TEST_GH" in message
        # "path does not exist" rejections are noise, not guidance.
        assert "does not exist" not in message

    def test_resolution_is_cached_and_reset_clears_it(self, monkeypatch, tmp_path):
        candidate = _fake_gh(tmp_path / "candidate-bin")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(
            runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (candidate,), "glab": ()}
        )
        first = runner.resolve_gh()
        with mock.patch.object(runner, "validate_provider_executable") as validate:
            assert runner.resolve_gh() == first
            validate.assert_not_called()

        runner.reset_cache()
        with mock.patch.object(
            runner, "validate_provider_executable", return_value=candidate
        ) as validate:
            assert runner.resolve_gh() == candidate
            validate.assert_called()

    def test_a_changed_override_value_is_not_served_from_cache(self, monkeypatch, tmp_path):
        first = _fake_gh(tmp_path / "first-bin")
        second = _fake_gh(tmp_path / "second-bin")
        monkeypatch.setenv("KIROCREW_TEST_GH", first)
        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == first
        monkeypatch.setenv("KIROCREW_TEST_GH", second)
        assert runner.resolve_gh(override_env="KIROCREW_TEST_GH") == second


# ── prevalidated handoff (sandboxed children) ────────────────────────────────


def _prevalidated_value(path: str) -> str:
    st = os.stat(path)
    return f"{path}|{st.st_dev}:{st.st_ino}"


class TestPrevalidatedGh:
    def test_handoff_wins_and_skips_the_ownership_walk(self, monkeypatch, tmp_path):
        """Inside a single-uid userns the ownership walk refuses everything, so
        the handoff must resolve WITHOUT calling it -- an exploding walk proves
        the skip rather than merely coexisting with a permissive one."""
        gh = _fake_gh(tmp_path / "bin")
        monkeypatch.setenv(runner.GH_PREVALIDATED_ENV, _prevalidated_value(gh))

        def _explode(path, *, label, uid, strict):
            raise AssertionError("ownership walk must not run for a prevalidated handoff")

        monkeypatch.setattr(runner, "check_provider_path_component", _explode)
        assert runner.resolve_gh() == gh

    def test_identity_mismatch_is_refused(self, monkeypatch, tmp_path):
        """A binary swapped in after the parent's validation has a different
        inode: the pin closes the validate-then-exec window the skipped
        ownership walk would otherwise reopen. Built from two LIVE files --
        two simultaneously-existing files can never share an inode on one
        filesystem -- because delete-then-recreate can reuse the freed inode
        (observed on CI) and would make the mismatch nondeterministic."""
        target = _fake_gh(tmp_path / "bin")
        other = _fake_gh(tmp_path / "other-bin", name="gh2")
        st = os.stat(other)
        monkeypatch.setenv(runner.GH_PREVALIDATED_ENV, f"{target}|{st.st_dev}:{st.st_ino}")
        with pytest.raises(runner.SetupError, match="identity mismatch"):
            runner.resolve_gh()

    def test_malformed_handoff_fails_loudly(self, monkeypatch):
        for bad in ("", "no-identity", "/x|not-numbers", "|1:2"):
            runner.reset_cache()
            monkeypatch.setenv(runner.GH_PREVALIDATED_ENV, bad)
            with pytest.raises(runner.SetupError):
                runner.resolve_gh()

    def test_world_writable_target_is_refused(self, monkeypatch, tmp_path):
        gh = _fake_gh(tmp_path / "bin")
        # Deliberately world-writable: this test asserts the guard REFUSES it.
        os.chmod(
            gh, 0o757
        )  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        monkeypatch.setenv(runner.GH_PREVALIDATED_ENV, _prevalidated_value(gh))
        with pytest.raises(runner.SetupError, match="world-writable"):
            runner.resolve_gh()

    def test_agent_writable_tree_is_still_refused(self, monkeypatch, tmp_path):
        """The namespace does not destroy this check, so the child keeps it:
        a handoff pointing into the agent's own tree is refused even though
        the parent supposedly validated it."""
        gh = _fake_gh(tmp_path / "bin")
        monkeypatch.setattr(runner, "agent_writable_roots", lambda: (tmp_path,))
        monkeypatch.setenv(runner.GH_PREVALIDATED_ENV, _prevalidated_value(gh))
        with pytest.raises(runner.SetupError, match="agent-writable"):
            runner.resolve_gh()

    def test_changed_handoff_is_not_served_from_cache(self, monkeypatch, tmp_path):
        first = _fake_gh(tmp_path / "first-bin")
        second = _fake_gh(tmp_path / "second-bin")
        monkeypatch.setenv(runner.GH_PREVALIDATED_ENV, _prevalidated_value(first))
        assert runner.resolve_gh() == first
        monkeypatch.setenv(runner.GH_PREVALIDATED_ENV, _prevalidated_value(second))
        assert runner.resolve_gh() == second

    def test_producer_pins_the_resolved_binary(self, monkeypatch, tmp_path):
        gh = _fake_gh(tmp_path / "bin")
        monkeypatch.setattr(runner, "resolve_gh", lambda **kw: gh)
        env = runner.prevalidated_gh_env()
        st = os.stat(gh)
        assert env == {runner.GH_PREVALIDATED_ENV: f"{gh}|{st.st_dev}:{st.st_ino}"}

    def test_producer_is_empty_when_no_gh_resolves(self, monkeypatch):
        def _fail(**kw):
            raise runner.SetupError("no gh")

        monkeypatch.setattr(runner, "resolve_gh", _fail)
        assert runner.prevalidated_gh_env() == {}


# ── gh_env ───────────────────────────────────────────────────────────────────


# ``socket.AF_UNIX`` does not exist on Windows CPython; the resolver tests that bind a
# real socket carry this marker, and the Windows branch is pinned by
# ``test_no_uid_means_no_variable`` instead.
_unix_sockets_only = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX sockets only"
)


@contextlib.contextmanager
def _listening_unix_socket(path):
    """A real AF_UNIX socket at *path* for the duration of the block.

    *path* must be short enough for ``sun_path`` (108 bytes on Linux, ~104 on
    macOS), which is what the ``short_sock_dir`` fixture guarantees.
    """
    path = os.fspath(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(path)
        sock.listen(1)
        yield sock
    finally:
        sock.close()
        with contextlib.suppress(OSError):
            os.unlink(path)


class TestSessionBusAddress:
    """The parent resolves the user bus ONCE, by its standard paths, and hands the
    child an explicit answer it cannot widen: the socket when it exists, an inert
    address otherwise, never a search of its own. Every candidate path is injected;
    nothing here touches the host's real ``/run/user``."""

    @staticmethod
    def _unescaped(address):
        assert address is not None and address.startswith("unix:path=")
        return urllib.parse.unquote(address[len("unix:path=") :])

    @pytest.fixture(autouse=True)
    def _no_host_uid_path(self, _floor_monkeypatch, tmp_path):
        """Pin the uid-derived candidate to an absent path under ``tmp_path`` so no test
        here can observe the host's own ``/run/user/<uid>/bus``; tests that want the
        uid candidate to resolve override it explicitly. On the isolation floor's own
        undo stack (``_floor_monkeypatch``), not the test's shared ``monkeypatch``, so a
        test's overrides unwind before these pins do."""
        _floor_monkeypatch.setattr(runner.platform_compat, "effective_uid", lambda: 4242)
        self.uid_bus = tmp_path / "uid-run" / "bus"
        _floor_monkeypatch.setattr(
            runner,
            "_user_bus_socket_candidates",
            lambda: _candidates(os.environ.get("XDG_RUNTIME_DIR"), self.uid_bus),
        )

    @_unix_sockets_only
    def test_socket_at_the_runtime_dir_is_handed_over_by_path(self, monkeypatch, short_sock_dir):
        monkeypatch.setenv("XDG_RUNTIME_DIR", os.fspath(short_sock_dir))
        # The ambient value is not what gets forwarded: the standard path is.
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/somewhere/else/bus")
        with _listening_unix_socket(short_sock_dir / "bus"):
            address = runner.session_bus_address()
        assert self._unescaped(address) == os.fspath(short_sock_dir / "bus")

    def test_missing_socket_is_inert_not_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", os.fspath(tmp_path / "run"))
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/somewhere/else/bus")
        assert runner.session_bus_address() == runner.INERT_SESSION_BUS_ADDRESS
        # A regular file at the conventional name is not a bus either.
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "bus").write_text("not a socket", encoding="utf-8")
        assert runner.session_bus_address() == runner.INERT_SESSION_BUS_ADDRESS

    @_unix_sockets_only
    def test_uid_path_is_tried_when_the_runtime_dir_has_no_bus(self, monkeypatch, short_sock_dir):
        """godbus derives ``/run/user/<uid>/bus`` from the uid alone and ignores
        ``XDG_RUNTIME_DIR``, so a gateway whose runtime dir holds no ``bus`` socket
        must still hand gh/glab the socket they would have found by uid."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", os.fspath(short_sock_dir / "no-bus-here"))
        (short_sock_dir / "no-bus-here").mkdir()
        self.uid_bus = short_sock_dir / "bus"
        assert runner.session_bus_address() == runner.INERT_SESSION_BUS_ADDRESS
        with _listening_unix_socket(self.uid_bus):
            address = runner.session_bus_address()
        assert self._unescaped(address) == os.fspath(self.uid_bus)

    @_unix_sockets_only
    def test_without_xdg_runtime_dir_the_uid_path_is_used(self, monkeypatch, short_sock_dir):
        """A system-service gateway has no XDG_RUNTIME_DIR; the standard location
        is then ``/run/user/<uid>/bus``, derived from the effective uid through
        platform_compat."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        self.uid_bus = short_sock_dir / "bus"
        assert runner.session_bus_address() == runner.INERT_SESSION_BUS_ADDRESS
        with _listening_unix_socket(self.uid_bus):
            address = runner.session_bus_address()
        assert self._unescaped(address) == os.fspath(self.uid_bus)

    def test_candidate_order_and_shape(self, monkeypatch):
        monkeypatch.setattr(runner, "_user_bus_socket_candidates", _REAL_CANDIDATES)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
        assert runner._user_bus_socket_candidates() == ("/run/user/4242/bus",)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/other-runtime")
        assert runner._user_bus_socket_candidates() == (
            "/tmp/other-runtime/bus",
            "/run/user/4242/bus",
        )
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert runner._user_bus_socket_candidates() == ("/run/user/4242/bus",)

    def test_no_uid_means_no_variable(self, monkeypatch):
        """Windows: no uid, no conventional path, and the child must not get a
        made-up address, so the builder leaves the variable unset."""
        monkeypatch.setattr(runner, "_user_bus_socket_candidates", _REAL_CANDIDATES)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(runner.platform_compat, "effective_uid", lambda: None)
        assert runner._user_bus_socket_candidates() == ()
        assert runner.session_bus_address() is None
        env = runner.apply_session_bus_address(
            {"DBUS_SESSION_BUS_ADDRESS": "stale", "NO_COLOR": "1"}
        )
        assert env == {"NO_COLOR": "1"}

    def test_inert_address_is_a_transport_no_library_registers(self):
        transport, sep, rest = runner.INERT_SESSION_BUS_ADDRESS.partition(":")
        assert sep == ":" and rest == ""
        # Non-empty (every library and cgroup_scope_bus_env read empty as unset),
        # not ``autolaunch`` (the one method that spawns), and none of the
        # transports libdbus, godbus or GDBus implement.
        assert transport not in {
            "",
            "autolaunch",
            "unix",
            "unixexec",
            "tcp",
            "nonce-tcp",
            "launchd",
            "systemd",
        }

    @_unix_sockets_only
    def test_address_value_is_escaped_per_the_dbus_spec(self, monkeypatch, short_sock_dir):
        run = short_sock_dir / "r d,w=o"
        run.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", os.fspath(run))
        with _listening_unix_socket(run / "bus"):
            address = runner.session_bus_address()
        assert address is not None
        value = address[len("unix:path=") :]
        assert " " not in value and "," not in value and "=" not in value
        assert urllib.parse.unquote(value) == os.fspath(run / "bus")

    def test_escaping_survives_an_undecodable_path(self):
        """A surrogate-escaped name (undecodable bytes in a path) is escaped from
        its own bytes rather than raising ``UnicodeEncodeError``."""
        raw = b"/run/user/4242\xff/bus"
        escaped = runner._escape_bus_address_value(os.fsdecode(raw))
        assert escaped == "/run/user/4242%FF/bus"
        assert urllib.parse.unquote_to_bytes(escaped) == raw

    def test_apply_replaces_any_inherited_value(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", os.fspath(tmp_path / "run"))
        env = runner.apply_session_bus_address({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/ambient"})
        assert env["DBUS_SESSION_BUS_ADDRESS"] == runner.INERT_SESSION_BUS_ADDRESS


_REAL_CANDIDATES = runner._user_bus_socket_candidates


def _candidates(runtime_dir, uid_bus):
    """The resolver's candidate tuple with the uid-derived path replaced by *uid_bus*."""
    found: list[str] = []
    if runtime_dir:
        found.append(os.path.join(runtime_dir, "bus"))
    found.append(os.fspath(uid_bus))
    return tuple(dict.fromkeys(found))


class TestGhEnv:
    def test_polluted_gateway_env_never_reaches_the_child(self, monkeypatch):
        """The D3 lock-in: gh-scoped auth/network/TLS keys pass, secrets do not."""
        polluted = {
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AWS_ACCESS_KEY_ID": "AKIAXXXX",
            "SLACK_BOT_TOKEN": "xoxb-secret",
            "SSH_AUTH_SOCK": "/run/agent.sock",
            "SSH_AGENT_PID": "4242",
            "GIT_SSH_COMMAND": "ssh -i /home/user/.ssh/id_rsa",
            "KIROCREW_INTERNAL_TOKEN": "internal",
            "GH_TOKEN": "gho_token",
            "GH_ENTERPRISE_TOKEN": "ghe_token",
            "GITHUB_ENTERPRISE_TOKEN": "ghe_token2",
            "GH_CONFIG_DIR": "/home/user/.config/gh",
            "ALL_PROXY": "socks5://proxy:1080",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/bundle.pem",
            "CURL_CA_BUNDLE": "/etc/ssl/curl.pem",
        }
        for key, value in polluted.items():
            monkeypatch.setenv(key, value)

        env = runner.gh_env()

        for secret_key in (
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "SLACK_BOT_TOKEN",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_SSH_COMMAND",
            "KIROCREW_INTERNAL_TOKEN",
        ):
            assert secret_key not in env, secret_key
        for passthrough_key in (
            "GH_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_CONFIG_DIR",
            "ALL_PROXY",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
        ):
            assert env[passthrough_key] == polluted[passthrough_key]
        # Deterministic output pins, always present.
        assert env["GH_PAGER"] == "cat"
        assert env["NO_COLOR"] == "1"

    def test_every_key_is_allowlisted(self, monkeypatch):
        """Exact-set property: everything in the child env is either the safe
        base, the gh passthrough, or one of the fixed pins — nothing else."""
        monkeypatch.setenv("GH_TOKEN", "gho_token")
        monkeypatch.setenv("SOME_RANDOM_SECRET", "boom")
        from kiro_crew.apps import registry

        allowed = (
            set(registry._SAFE_ENV_KEYS)
            | set(runner.GH_ENV_PASSTHROUGH)
            | {"GH_PAGER", "NO_COLOR", "GH_HOST", "DBUS_SESSION_BUS_ADDRESS"}
        )
        for key in runner.gh_env(pin_host="github.com"):
            assert key in allowed, key

    def test_hands_gh_an_explicit_session_bus_address(self, monkeypatch):
        """gh keeps its token in the OS keyring by default and asks for it over
        D-Bus. The child is never left to find the bus itself: the parent resolves
        it once, by its standard path, and the builder pins whichever value that
        produced. The gateway's own ambient value is never forwarded."""
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/ambient/forwarded/bus")

        for resolved in (runner.INERT_SESSION_BUS_ADDRESS, "unix:path=/run/user/4242/bus"):
            monkeypatch.setattr(runner, "session_bus_address", lambda resolved=resolved: resolved)
            env = runner.gh_env()
            assert env.get("DBUS_SESSION_BUS_ADDRESS"), "gh must not start with no bus address"
            assert env["DBUS_SESSION_BUS_ADDRESS"] == resolved
            assert "/ambient/forwarded/bus" not in env["DBUS_SESSION_BUS_ADDRESS"]

    @_unix_sockets_only
    def test_gh_env_resolves_a_real_user_bus_socket(self, monkeypatch, short_sock_dir, tmp_path):
        """End to end through the real resolver: an unreachable standard path is
        inert rather than an autolaunch, and a listening socket there is handed over."""
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/ambient/forwarded/bus")
        monkeypatch.setenv("XDG_RUNTIME_DIR", os.fspath(short_sock_dir))
        # The uid-derived candidate must not reach the host's own /run/user.
        monkeypatch.setattr(
            runner,
            "_user_bus_socket_candidates",
            lambda: _candidates(os.fspath(short_sock_dir), tmp_path / "uid-run" / "bus"),
        )

        assert runner.gh_env()["DBUS_SESSION_BUS_ADDRESS"] == runner.INERT_SESSION_BUS_ADDRESS
        with _listening_unix_socket(short_sock_dir / "bus"):
            with_bus = runner.gh_env()
        address = with_bus["DBUS_SESSION_BUS_ADDRESS"]
        assert address.startswith("unix:path=")
        assert urllib.parse.unquote(address[len("unix:path=") :]) == os.fspath(
            short_sock_dir / "bus"
        )

    def test_pin_host_sets_gh_host_and_unpinned_does_not(self, monkeypatch):
        monkeypatch.delenv("GH_HOST", raising=False)
        assert "GH_HOST" not in runner.gh_env()
        assert runner.gh_env(pin_host="github.com")["GH_HOST"] == "github.com"

    def test_pin_host_overrides_an_ambient_gh_host(self, monkeypatch):
        """A configured enterprise default cannot survive the pin — this is the
        property the sidebar's bare API paths rely on."""
        monkeypatch.setenv("GH_HOST", "ghe.internal.example")
        assert runner.gh_env(pin_host="github.com")["GH_HOST"] == "github.com"


# ── run_gh ───────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0) -> subprocess.CompletedProcess:
    """A stand-in for what ``subprocess.run`` returns to ``run_gh`` -- BYTES.

    ``run_gh`` captures bytes and decodes them strictly in its own frame, so a
    stub at the subprocess boundary must hand it bytes. Handing it `str` raises
    `AttributeError: 'str' object has no attribute 'decode'`.

    This file is skipped on Windows (see the module-level `pytestmark`), so
    these five tests are the ones a Windows-only local run does NOT execute --
    which is exactly how the `str` version of this helper reached CI green
    locally and red on the Linux shards.
    """
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=b"", stderr=b"")


class TestRunGh:
    def test_refuses_a_non_absolute_binary(self):
        with pytest.raises(runner.SetupError, match="absolute gh path"):
            runner.run_gh(["gh", "api", "user"], timeout=5, audit_caller="core:test")

    def test_spawn_env_is_exactly_gh_env(self, monkeypatch):
        """D1 lock-in: the chokepoint hands the child gh_env(), never the
        gateway's full environment."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        monkeypatch.setenv("GH_TOKEN", "gho_token")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _proc()

        with (
            mock.patch.object(runner.subprocess, "run", side_effect=fake_run),
            mock.patch.object(runner, "_audit_run"),
        ):
            runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test")

        assert captured["kwargs"]["env"] == runner.gh_env()
        assert "AWS_SECRET_ACCESS_KEY" not in captured["kwargs"]["env"]
        assert captured["argv"] == ["/usr/bin/gh", "api", "user"]
        assert captured["kwargs"]["timeout"] == 5
        assert "shell" not in captured["kwargs"]

    def test_pin_host_is_forwarded_to_the_child_env(self, monkeypatch):
        """Issue Radar's bare API paths never pass --hostname, so its run_gh
        calls pin GH_HOST — an ambient enterprise default must not steer them."""
        monkeypatch.setenv("GH_HOST", "ghe.internal.example")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["kwargs"] = kwargs
            return _proc()

        with (
            mock.patch.object(runner.subprocess, "run", side_effect=fake_run),
            mock.patch.object(runner, "_audit_run"),
        ):
            runner.run_gh(
                ["/usr/bin/gh", "api", "user"],
                timeout=5,
                audit_caller="core:test",
                pin_host="github.com",
            )
        assert captured["kwargs"]["env"]["GH_HOST"] == "github.com"

    def test_audits_an_oserror_spawn_failure_then_reraises(self):
        """A cached binary gone bad (chmod'd, replaced with a non-executable)
        must land in the audit trail, not escape as an unaudited failure."""
        with (
            mock.patch.object(runner.subprocess, "run", side_effect=PermissionError("denied")),
            mock.patch.object(runner, "_audit_run") as audit,
        ):
            with pytest.raises(PermissionError):
                runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test")
        assert audit.call_args_list[-1] == mock.call(
            "core:test", "gh api user", "failure", error="PermissionError"
        )

    def test_audits_invoked_before_the_spawn_then_ok(self):
        calls: list[tuple] = []

        def fake_run(argv, **kwargs):
            calls.append(("spawn",))
            return _proc()

        def fake_audit(caller, target, outcome, **kwargs):
            calls.append(("audit", outcome, kwargs.get("critical", False)))

        with (
            mock.patch.object(runner.subprocess, "run", side_effect=fake_run),
            mock.patch.object(runner, "_audit_run", side_effect=fake_audit),
        ):
            runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test")
        # The invoked event is critical and lands BEFORE the child runs.
        assert calls == [("audit", "invoked", True), ("spawn",), ("audit", "ok", False)]

    def test_audits_non_zero_exit(self):
        with (
            mock.patch.object(runner.subprocess, "run", return_value=_proc(returncode=1)),
            mock.patch.object(runner, "_audit_run") as audit,
        ):
            proc = runner.run_gh(
                ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test"
            )
        assert proc.returncode == 1
        assert audit.call_args_list[-1] == mock.call(
            "core:test", "gh api user", "failure", error="exit 1"
        )

    def test_audits_timeout_then_reraises(self):
        with (
            mock.patch.object(
                runner.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 5)
            ),
            mock.patch.object(runner, "_audit_run") as audit,
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test")
        assert audit.call_args_list[-1] == mock.call(
            "core:test", "gh api user", "failure", error="timeout after 5s"
        )

    def test_audit_event_carries_the_caller_namespace(self, monkeypatch):
        """Issue Radar's historical SEL operation identity survives the shared
        emission point."""
        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kwargs):
                events.append(kwargs)

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _FakeSel())
        runner._audit_run("core:issue-radar", "gh api repos/o/r", "ok")

        assert events == [
            {
                "caller": "core:issue-radar",
                "operation": "issue_radar.gh_run",
                "outcome": "ok",
                "source": "builtin-app",
                "resources": "gh api repos/o/r",
                "error": "",
                "critical": False,
            }
        ]

    def test_unavailable_audit_refuses_the_spawn(self, monkeypatch):
        """Audit-or-deny: with SEL storage unusable, gh must NOT run unaudited."""
        monkeypatch.setattr("kiro_crew.sel.sel", mock.Mock(side_effect=RuntimeError("sel down")))
        with mock.patch.object(runner.subprocess, "run", return_value=_proc()) as spawn:
            with pytest.raises(runner.SetupError, match="refusing to run gh unaudited"):
                runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test")
            spawn.assert_not_called()

    def test_outcome_audit_failure_never_breaks_the_call(self, monkeypatch):
        """Once the invoked record landed, a failed OUTCOME write is logged,
        not turned into a feature failure — the spawn already happened."""

        class _FlakySel:
            def __init__(self) -> None:
                self.calls = 0

            def log_api_access(self, **kwargs):
                self.calls += 1
                if kwargs.get("outcome") != "invoked":
                    raise RuntimeError("sel went away mid-call")

        flaky = _FlakySel()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: flaky)
        with mock.patch.object(runner.subprocess, "run", return_value=_proc()):
            proc = runner.run_gh(
                ["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="core:test"
            )
        assert proc.returncode == 0
        assert flaky.calls == 2


# ── re-export seams ──────────────────────────────────────────────────────────


class TestReExports:
    def test_source_providers_validation_is_the_shared_function(self):
        from kiro_crew.dashboard.handlers import source_providers

        assert source_providers._validate_provider_executable is runner.validate_provider_executable
        assert (
            source_providers.provider_executable_candidates is runner.provider_executable_candidates
        )
        assert (
            source_providers._PROVIDER_EXECUTABLE_CANDIDATES
            is runner.PROVIDER_EXECUTABLE_CANDIDATES
        )

    def test_github_client_url_parser_is_the_shared_function(self):
        from kiro_crew.apps.builtins.issue_radar.backend import github_client

        assert github_client.parse_github_repo_url is runner.parse_github_repo_url

    def test_repo_url_error_is_one_class_across_layers(self):
        from kiro_crew.apps.builtins.issue_radar.backend import errors, github_client

        assert errors.RepoUrlError is runner.RepoUrlError
        assert github_client.RepoUrlError is runner.RepoUrlError
        with pytest.raises(errors.RepoUrlError):
            runner.parse_github_repo_url("https://evil.example/o/r")

    def test_source_providers_gh_auth_keys_derive_from_the_canonical_union(self):
        """D3 lock-in: the sidebar's gh key set cannot drift from the
        app-side passthrough — it derives from the runner's canonical list,
        minus the enterprise tokens its github.com-pinned child can never use."""
        from kiro_crew.dashboard.handlers import source_providers

        assert source_providers._PROVIDER_AUTH_ENV_KEYS["gh"] == frozenset(
            runner.GH_ENV_PASSTHROUGH
        ) - {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}


class TestRepoUrlSegmentsAreBoundedInWidth:
    """``parse_github_repo_url`` bounds both halves of each segment it admits: the
    charset AND the width. A charset with no quantifier is satisfied by a segment of
    any size, and this parser's own docstring says those values reach a subprocess
    argv. Every width below is derived from the published limit rather than written
    down, so the bound has exactly one home.
    """

    def test_the_two_limits_are_githubs_own_published_maximums(self):
        """The widths below are all derived from these two constants, so the
        constants themselves are the one thing a derived width cannot check:
        loosening either would leave every other case in this class green. A
        github.com account login -- user or organization -- is 1-39 characters, and
        a repository name is at most 100. Both parsers are pinned to the
        github.com host, so these are the ceilings for every value that reaches
        them.
        """
        assert runner.GITHUB_MAX_OWNER_CHARS == 39
        assert runner.GITHUB_MAX_REPO_CHARS == 100

    def test_an_owner_at_githubs_login_limit_is_accepted(self):
        owner = "o" * runner.GITHUB_MAX_OWNER_CHARS
        parsed_owner, _ = runner.parse_github_repo_url(f"https://github.com/{owner}/repo")

        assert len(parsed_owner) == runner.GITHUB_MAX_OWNER_CHARS

    def test_a_repository_at_githubs_name_limit_is_accepted(self):
        repo = "r" * runner.GITHUB_MAX_REPO_CHARS
        _, parsed_repo = runner.parse_github_repo_url(f"https://github.com/owner/{repo}")

        assert len(parsed_repo) == runner.GITHUB_MAX_REPO_CHARS

    @pytest.mark.parametrize("over", [1, 2, 5000])
    def test_an_owner_wider_than_githubs_login_limit_is_refused(self, over: int):
        owner = "o" * (runner.GITHUB_MAX_OWNER_CHARS + over)

        with pytest.raises(runner.RepoUrlError):
            runner.parse_github_repo_url(f"https://github.com/{owner}/repo")

    @pytest.mark.parametrize("over", [1, 2, 5000])
    def test_a_repository_wider_than_githubs_name_limit_is_refused(self, over: int):
        repo = "r" * (runner.GITHUB_MAX_REPO_CHARS + over)

        with pytest.raises(runner.RepoUrlError):
            runner.parse_github_repo_url(f"https://github.com/owner/{repo}")

    def test_the_dot_git_suffix_is_stripped_before_the_width_is_judged(self):
        """A name at the limit stays legal when the URL spells the ``.git`` clone
        form, because the suffix is removed before the bound is applied."""
        repo = "r" * runner.GITHUB_MAX_REPO_CHARS
        _, parsed_repo = runner.parse_github_repo_url(f"https://github.com/owner/{repo}.git")

        assert len(parsed_repo) == runner.GITHUB_MAX_REPO_CHARS

    @pytest.mark.parametrize(
        ("pattern_name", "limit_name"),
        [
            ("GITHUB_OWNER_SEGMENT_RE", "GITHUB_MAX_OWNER_CHARS"),
            ("GITHUB_REPO_SEGMENT_RE", "GITHUB_MAX_REPO_CHARS"),
        ],
    )
    def test_a_trailing_newline_cannot_escape_the_bound(self, pattern_name: str, limit_name: str):
        """``$`` also matches immediately before a final newline, so a bound
        anchored with it admits one character past the maximum and disagrees with
        itself between ``match`` and ``fullmatch``."""
        pattern = getattr(runner, pattern_name)
        widest = "x" * getattr(runner, limit_name)

        assert pattern.match(widest) is not None
        assert pattern.match(f"{widest}\n") is None
        assert pattern.fullmatch(f"{widest}\n") is None
