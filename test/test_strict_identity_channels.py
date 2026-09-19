"""The strict session-identity resolver's channels, and what each one costs.

``_resolve_session_key_strict`` answers "which session is calling" for every tool
that mutates a session's state. It has to be right in two directions at once: a
sandboxed caller must not be able to name a session of its choosing, and a
legitimate caller must not lose its identity because of the platform it runs on.

The gateway peer lookup is what makes the first direction hold -- the kernel
supplies the pid, the gateway supplies the binding, and neither is the caller's to
choose. It cannot be the ONLY channel, because the transport it needs is optional:
``dashboard/server.py`` skips the AF_UNIX site entirely on Windows and degrades to
TCP-only on any bind failure. So a direct read of the same fenced binding sits
below it, and these tests pin both that it is reached and that the sandbox gains
nothing from it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core, platform_compat, session_pid_sig

SESSION_KEY = "dashboard:chat-9-987654"
HOST_PID = 4242


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data home holding one published binding, with every OTHER channel shut.

    The env var, the signed token and the caller context are all removed, so a key
    that comes back can only have come from the gateway lookup or the fenced read.
    """
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.setenv("KIROCREW_HOST_PID", str(HOST_PID))
    with (
        patch.object(session_pid_sig, "config_dir", return_value=tmp_path),
        patch.object(platform_compat, "get_process_start_id", return_value=None),
        patch.object(session_pid_sig, "sel_hmac_key_path", return_value=tmp_path / "sel_hmac.key"),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
        patch.object(mcp_core, "current_caller", return_value=None),
        patch.object(mcp_core, "_session_key_from_token", return_value=""),
        patch(
            "kiro_crew.member_memory_auth.protected_member_session_for_pid",
            return_value=None,
        ),
    ):
        (tmp_path / "sel_hmac.key").write_bytes(b"\x01" * 32)
        session_pid_sig._reported.clear()
        yield tmp_path
        session_pid_sig._reported.clear()
        mcp_core._GATEWAY_PEER_SESSION_KEY = None


@pytest.fixture(autouse=True)
def _no_cached_peer_answer():
    """The gateway answer is cached per process, so a leaked cache would let one
    test's identity satisfy the next one's assertion."""
    mcp_core._GATEWAY_PEER_SESSION_KEY = None
    yield
    mcp_core._GATEWAY_PEER_SESSION_KEY = None


class TestChannelOrder:
    def test_the_gateway_answer_wins_over_the_local_read(self, home):
        """Preferred because it is the one a sandboxed caller cannot influence:
        the kernel names the pid. A disagreement means the local view is stale or
        tampered, so the gateway's answer is the one to keep."""
        session_pid_sig.publish_session_pid(HOST_PID, "dashboard:stale-local")
        with patch.object(mcp_core, "_session_key_from_gateway_peer", return_value=SESSION_KEY):
            assert mcp_core._resolve_session_key_strict() == SESSION_KEY

    def test_the_env_var_wins_over_both(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")
        with patch.object(mcp_core, "_session_key_from_gateway_peer", return_value=""):
            assert mcp_core._resolve_session_key_strict() == "dashboard:from-env"


class TestTheFallbackWhereThereIsNoSocket:
    """The platform shape this branch exists for: no AF_UNIX transport, so the
    gateway cannot be asked at all."""

    def test_identity_still_resolves(self, home):
        session_pid_sig.publish_session_pid(HOST_PID, SESSION_KEY)
        with patch.object(mcp_core, "_session_key_from_gateway_peer", return_value=""):
            assert mcp_core._resolve_session_key_strict() == SESSION_KEY

    def test_it_reads_the_fenced_copy_and_not_the_attribution_copy(self, home):
        """MUTATION. A well-formed binding written only where an agent can reach
        resolves nothing, which is what makes the fallback safe: what denies the
        sandbox is the masked directory, not the absence of this code path. Point
        the read at the data-home copy instead and this test fails."""
        (home / f"session_pid_{HOST_PID}.txt").write_text(SESSION_KEY, encoding="utf-8")
        with patch.object(mcp_core, "_session_key_from_gateway_peer", return_value=""):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_an_unreadable_identity_root_resolves_nothing(self, home):
        """The in-sandbox shape, reproduced by making the root unloadable: the
        mask means the binding and its signing root are both unreadable there, so
        this branch returns "" however it is reached."""
        session_pid_sig.publish_session_pid(HOST_PID, SESSION_KEY)
        session_pid_sig._identity_key_path(home).write_bytes(b"\x01" * 8)
        with patch.object(mcp_core, "_session_key_from_gateway_peer", return_value=""):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_no_declared_host_pid_resolves_nothing(self, home, monkeypatch):
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        session_pid_sig.publish_session_pid(HOST_PID, SESSION_KEY)
        with patch.object(mcp_core, "_session_key_from_gateway_peer", return_value=""):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_a_raising_read_resolves_nothing_rather_than_propagating(self, home):
        """This runs under every strict tool call, so an exception here would turn
        a fail-closed refusal into a crash."""
        with (
            patch.object(mcp_core, "_session_key_from_gateway_peer", return_value=""),
            patch.object(session_pid_sig, "verify_session_pid", side_effect=OSError("boom")),
        ):
            assert mcp_core._resolve_session_key_strict() == ""


class TestTheWalkStaysExcluded:
    def test_the_fallback_is_an_exact_pid_lookup(self) -> None:
        """A ``/proc`` ancestor walk would resolve a ``spawn_run`` subagent to its
        PARENT slot, letting it mutate state on the wrong session. The fallback
        looks up the declared pid and nothing else, so it cannot do that."""
        src = Path(mcp_core.__file__).read_text(encoding="utf-8")
        body = src.split("def _session_key_from_fenced_binding(")[1].split("\ndef ")[0]
        assert "verify_session_pid" in body
        for forbidden in ("_get_ppid", "resolve_peer_identity", "read_session_pid_txt"):
            assert forbidden not in body, f"the fallback must not use {forbidden}"
