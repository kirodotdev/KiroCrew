"""Gateway port resolution for MCP callbacks.

``mcp_core`` resolves lazily via ``port_resolution.resolve_client_port_ex`` (env →
config port → live run-marker → default), patched through ``cli_server``'s namespace.
The dashboard exports ``KIROCREW_BOUND_PORT`` once its TCP site listens.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew
import kiro_crew.mcp_core as mcp_core
from kiro_crew.dashboard.server import _export_bound_port

#: Source root of the tree under test, pinned onto the probe subprocess's
#: PYTHONPATH. Without it the child interpreter resolves whatever kiro_crew
#: happens to be installed for it (pytest's ``pythonpath = src`` does not
#: propagate to subprocesses) — a stale editable install would make the
#: leaf-purity assertion pass vacuously against old code, and a non-editable
#: install would fail it spuriously. Same pattern as test_perf_boot_path._probe.
_SRC = str(Path(kiro_crew.__file__).resolve().parents[1])


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch: pytest.MonkeyPatch):
    """Reset the lazy caches and neutralise ambient env for every test.

    ``_API`` / ``_API_UNIX_SOCKET`` memoise per process. ``KIROCREW_PORT`` is deleted
    because a dev box may carry it and it outranks every other step.
    """
    monkeypatch.setattr(mcp_core, "_API_PORT", None)
    monkeypatch.setattr(mcp_core, "_API", None)
    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", None)
    monkeypatch.delenv("KIROCREW_PORT", raising=False)
    monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)


def _cfg(url: object):
    cfg = MagicMock()
    cfg.dashboard.url = url
    return patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=cfg)


def _markers(ports: list[int]):
    return patch("kiro_crew.cli_server.run_marker.marker_ports", return_value=ports)


def _owned(ports: list[int]):
    """Pretend a verified Kiro Crew gateway listens on each of *ports*."""
    return patch("kiro_crew.cli_server._gateway_owns_port", side_effect=lambda p: p in set(ports))


class TestApiBaseResolution:
    """``mcp_core._api_base`` — the resolver every gateway callback uses."""

    def test_portless_url_prefers_live_marker(self):
        """The bug: portless ``dashboard.url``, no env, gateway live on 6776.

        ``parse_dashboard_url`` substitutes the default port for a portless
        URL; taking that as the answer aims every MCP callback at a port the
        gateway never bound. The live run-marker is the better evidence.
        """
        with _cfg("http://my.host.example"), _markers([6776]), _owned([6776]):
            assert mcp_core._api_base() == "http://127.0.0.1:6776"

    def test_explicit_config_port_beats_marker(self):
        """A port the user wrote down is a decision; discovery must yield."""
        with _cfg("http://127.0.0.1:7778"), _markers([6776]), _owned([6776]):
            assert mcp_core._api_base() == "http://127.0.0.1:7778"

    def test_dead_pid_marker_is_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """A marker whose recorded pid does not hold the port is discarded.

        A crashed gateway leaves its marker behind. Runs the real ``_gateway_owns_port``
        chain; only the marker reads and the listener lookup are stubbed.
        """
        monkeypatch.setattr("kiro_crew.cli_server.run_marker.read_pid", lambda port: 999999999)
        # A dead pid holds no sockets — the listener set for the port is empty.
        monkeypatch.setattr(
            "kiro_crew.cli_server.platform_compat.find_listening_pids", lambda port: []
        )
        with _cfg(""), _markers([6776]):
            assert mcp_core._api_base() == "http://127.0.0.1:5476"

    def test_env_var_beats_marker(self, monkeypatch: pytest.MonkeyPatch):
        """The exported ``KIROCREW_PORT`` (gateway truth) sits above discovery."""
        monkeypatch.setenv("KIROCREW_PORT", "6777")
        with _cfg(""), _markers([6776]), _owned([6776]):
            assert mcp_core._api_base() == "http://127.0.0.1:6777"

    def test_default_fallthrough_is_not_pinned(self):
        """A resolution with no evidence behind it must not be cached.

        During boot a server can resolve before the run-marker lands; caching that
        default would freeze the wrong port for the process lifetime.
        """
        with _cfg(""), _markers([]):
            assert mcp_core._api_base() == "http://127.0.0.1:5476"
        assert mcp_core._API_PORT is None  # nothing pinned
        assert mcp_core._API is None
        # The marker appears (gateway finished booting) — same process now
        # resolves the real port without a restart.
        with _cfg(""), _markers([6776]), _owned([6776]):
            assert mcp_core._api_base() == "http://127.0.0.1:6776"
        # A marker-discovered port is served but never pinned either: its
        # ownership proof holds only for the instant it was made, and a freed
        # port can be rebound by any local process. Requests carrying the
        # internal secret must re-verify per call (see _api_port).
        assert mcp_core._API_PORT is None

    def test_marker_port_is_reverified_on_every_call(self):
        """A marker-discovered port must be re-resolved (re-proving ownership)
        on every call, not trusted from a cache.

        A freed port may be rebound by another local user, and a cached answer would
        keep sending secret-bearing requests there.
        """
        with _cfg(""), _markers([6776]) as markers, _owned([6776]):
            assert mcp_core._api_base() == "http://127.0.0.1:6776"
            assert mcp_core._api_base() == "http://127.0.0.1:6776"
            assert markers.call_count == 2  # re-resolved, not cached
        # The gateway moved: ownership of 6776 stops verifying and the new
        # marker names 7788 — the very next call follows it.
        with _cfg(""), _markers([7788]), _owned([7788]):
            assert mcp_core._api_base() == "http://127.0.0.1:7788"

    def test_unix_socket_not_pinned_on_default_fallthrough(self):
        """The socket path follows the same no-evidence rule as the URL."""
        with _cfg(""), _markers([]):
            assert mcp_core._api_unix_socket().endswith("dashboard-5476.sock")
        assert mcp_core._API_UNIX_SOCKET is None
        with _cfg(""), _markers([6776]), _owned([6776]):
            assert mcp_core._api_unix_socket().endswith("dashboard-6776.sock")

    def test_resolution_is_lazy_and_cached(self, monkeypatch: pytest.MonkeyPatch):
        """First call resolves, later calls reuse the cache (no re-discovery).

        Caching applies to STABLE sources only — here the env var, a user
        decision that holds for the process lifetime. (A marker-discovered
        port deliberately re-resolves per call; see
        ``test_marker_port_is_reverified_on_every_call``.)
        """
        monkeypatch.setenv("KIROCREW_PORT", "6777")
        with _cfg(""), _markers([]) as markers:
            assert mcp_core._api_base() == "http://127.0.0.1:6777"
            assert mcp_core._api_base() == "http://127.0.0.1:6777"
            markers.assert_not_called()
        assert mcp_core._API_PORT == 6777  # stable evidence IS pinned

    def test_preseeded_cache_is_respected(self, monkeypatch: pytest.MonkeyPatch):
        """A pre-seeded ``_API`` (the test seam) short-circuits resolution."""
        monkeypatch.setattr(mcp_core, "_API", "http://127.0.0.1:1")
        with _markers([6776]) as markers:
            assert mcp_core._api_base() == "http://127.0.0.1:1"
            markers.assert_not_called()

    def test_unix_socket_path_follows_the_same_port(self):
        """Both transports must aim at the same gateway.

        The unix-socket path is derived from the same resolution as the TCP
        base; deriving it from raw config again would reintroduce the split
        this fix removes (TCP at the marker port, socket at the default).
        """
        with _cfg("http://my.host.example"), _markers([6776]), _owned([6776]):
            assert mcp_core._api_unix_socket().endswith("dashboard-6776.sock")

    def test_bound_port_beats_marker_and_config_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The parent gateway's exported bound port is observed truth."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7891")
        with _cfg("http://my.host.example"), _markers([6776]), _owned([6776]):
            assert mcp_core._api_base() == "http://127.0.0.1:7891"

    def test_operator_port_beats_bound_port(self, monkeypatch: pytest.MonkeyPatch):
        """An explicit KIROCREW_PORT is how a caller retargets a child at a
        DIFFERENT gateway: pod exec builds a client env with
        KIROCREW_PORT=<pod-port> while the inherited KIROCREW_BOUND_PORT still
        names the spawning LIVE gateway. If the bound value outranked it, pod
        token/status/logout would walk their credentials into the live
        gateway — a cross-plane isolation break."""
        monkeypatch.setenv("KIROCREW_PORT", "7891")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "5476")
        with _cfg(""), _markers([]):
            assert mcp_core._api_base() == "http://127.0.0.1:7891"


class _StubRunner:
    def __init__(self, addresses: list[object]):
        self.addresses = addresses


class TestExportBoundPort:
    """``dashboard.server._export_bound_port`` — the gateway-side half."""

    def test_explicit_port_is_exported(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        _export_bound_port(_StubRunner([("127.0.0.1", 6776)]), 6776)
        assert os.environ["KIROCREW_BOUND_PORT"] == "6776"
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)

    def test_ephemeral_bind_reads_port_back_from_runner(self, monkeypatch: pytest.MonkeyPatch):
        """``--port auto`` requests port 0; the OS-assigned port is the truth."""
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        _export_bound_port(_StubRunner([("127.0.0.1", 54321)]), 0)
        assert os.environ["KIROCREW_BOUND_PORT"] == "54321"
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)

    def test_no_readable_address_leaves_env_untouched(self, monkeypatch: pytest.MonkeyPatch):
        """Best-effort: an unreadable address list must not export garbage."""
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        _export_bound_port(_StubRunner(["/tmp/some.sock"]), 0)
        assert "KIROCREW_BOUND_PORT" not in os.environ

    def test_export_overwrites_stale_env(self, monkeypatch: pytest.MonkeyPatch):
        """The bound port is the truth even when an older value is inherited."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "1111")
        _export_bound_port(_StubRunner([]), 6776)
        assert os.environ["KIROCREW_BOUND_PORT"] == "6776"


class TestPortResolutionStaysLeaf:
    """Importing ``port_resolution`` or ``mcp_core`` must not pull in ``cli_server``.

    Checked in a fresh interpreter: this suite itself imports ``cli_server``.
    """

    @pytest.mark.parametrize(
        "module", ["kiro_crew.port_resolution", "kiro_crew.mcp_core"]
    )
    def test_import_does_not_pull_cli_server(self, module: str) -> None:
        code = (
            "import importlib, sys; "
            f"importlib.import_module({module!r}); "
            "assert 'kiro_crew.cli_server' not in sys.modules, "
            f"'importing {module} pulled in the heavy cli_server graph'"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
