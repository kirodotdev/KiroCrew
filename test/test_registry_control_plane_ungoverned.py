"""The control plane survives a registry declaration no admin registry can enforce.

kiro-cli applies an admin MCP registry only to a sign-in governance reaches
(Identity Center or an API key). On any other sign-in the client is not in
registry mode and drops every entry marked ``"type": "registry"``, so a marked
``kirocrew-core`` / ``kirocrew-cron`` takes ``spawn_run``, ``cron_add`` and
``learn_add`` out of every session. When that sign-in is KNOWN, the control plane
stays a plain local server; any doubt keeps the marker a governed account needs.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import kiro_cli
from kiro_crew.kiro_cli import registry_governance_known_unreachable as real_probe

CONTROL_PLANE = ("kirocrew-core", "kirocrew-cron")
OTHER_MANAGED = ("kirocrew-guide", "kirocrew-mate")
NO_GATES: frozenset[str] = frozenset()
PERSONAL = {"account_type": "BuilderId"}
PINNED = "/opt/kiro/bin/kiro-cli"


def _registry_declared(monkeypatch, *, unreachable: bool) -> None:
    monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: True)
    monkeypatch.setattr(kiro_cli, "registry_governance_known_unreachable", lambda: unreachable)


class TestSpecMarker:
    def test_fresh_spec_keeps_the_control_plane_unmarked(self, monkeypatch):
        _registry_declared(monkeypatch, unreachable=True)
        servers = agent_mod.build_agent_config(gated_off=NO_GATES)["mcpServers"]
        for name in CONTROL_PLANE:
            assert "type" not in servers[name], name
        for name in OTHER_MANAGED:
            assert servers[name]["type"] == "registry", name

    def test_doubt_about_the_sign_in_keeps_every_marker(self, monkeypatch):
        _registry_declared(monkeypatch, unreachable=False)
        servers = agent_mod.build_agent_config(gated_off=NO_GATES)["mcpServers"]
        for name in CONTROL_PLANE + OTHER_MANAGED:
            assert servers[name]["type"] == "registry", name

    def test_refresh_removes_the_control_plane_marker(self, monkeypatch):
        _registry_declared(monkeypatch, unreachable=True)
        config = {
            "mcpServers": {
                name: {"command": "x", "args": [], "type": "registry"}
                for name in CONTROL_PLANE + OTHER_MANAGED
            }
        }
        agent_mod._refresh_dynamic_fields(config, gated_off=NO_GATES)
        for name in CONTROL_PLANE:
            assert "type" not in config["mcpServers"][name], name
        for name in OTHER_MANAGED:
            assert config["mcpServers"][name]["type"] == "registry", name

    def test_member_transport_keeps_the_control_plane_unmarked(self, monkeypatch):
        _registry_declared(monkeypatch, unreachable=True)
        entry = agent_mod.managed_member_transport("kirocrew-core")
        assert entry is not None
        assert "type" not in entry

    def test_template_declaration_keeps_the_control_plane_unmarked(self, monkeypatch):
        from kiro_crew.dashboard.handlers.agent_templates import control_plane_declarations

        _registry_declared(monkeypatch, unreachable=True)
        out = control_plane_declarations(["@kirocrew-core", "@kirocrew-cron"], {})
        assert sorted(out) == sorted(CONTROL_PLANE)
        for name in CONTROL_PLANE:
            assert "type" not in out[name], name


class TestSessionElement:
    """kiro-cli mounts the control plane from the spec; its per-session element is
    what carries the session identity, so an unmarked entry needs one."""

    def _mount(self, tmp_path, monkeypatch, *, unreachable: bool) -> list[str]:
        from kiro_crew.acp import session_mcp

        managed = {"command": "test-crew", "args": ["mcp"]}
        names = (*CONTROL_PLANE, "kirocrew-dashboard")
        spec = {
            "tools": [f"@{name}" for name in names],
            "mcpServers": {name: dict(managed) for name in names},
        }
        monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
        monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
        monkeypatch.setattr(session_mcp, "_registry_mode", lambda: True)
        monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: managed)
        monkeypatch.setattr(kiro_cli, "registry_governance_known_unreachable", lambda: unreachable)
        mount = session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path)
        return sorted(element["name"] for element in mount.elements)

    def test_ungoverned_sign_in_mounts_only_the_control_plane(self, tmp_path, monkeypatch):
        assert self._mount(tmp_path, monkeypatch, unreachable=True) == sorted(CONTROL_PLANE)

    def test_governed_or_unknown_sign_in_mounts_nothing(self, tmp_path, monkeypatch):
        assert self._mount(tmp_path, monkeypatch, unreachable=False) == []


class TestReachabilityProbe:
    def _probe(self, monkeypatch, *, may_apply, whoami) -> tuple[bool, list[int]]:
        from kiro_crew.cloud import login_target

        calls: list[int] = []

        def fake_whoami(kiro_bin=None):
            assert kiro_bin == PINNED, "only the pinned kiro-cli may be asked"
            calls.append(1)
            if isinstance(whoami, Exception):
                raise whoami
            return whoami

        monkeypatch.setattr(kiro_cli, "mcp_governance_may_apply", lambda: may_apply)
        if not self.real_env_file:
            monkeypatch.setattr(kiro_cli, "_data_home_api_key_may_be_set", lambda: False)
        monkeypatch.setattr(login_target, "pin_kiro_cli", lambda: (self.pin[0], False))
        monkeypatch.setattr(login_target, "discover_local_identity", fake_whoami)
        monkeypatch.setattr(kiro_cli, "_sign_in_fingerprint", lambda: self.fingerprint[0])
        return real_probe(), calls

    @pytest.fixture(autouse=True)
    def _fixed_fingerprint(self):
        self.fingerprint = ["store-v1"]
        self.pin = [PINNED]
        self.real_env_file = False

    def _scrubbed_env_file(self, tmp_path, monkeypatch, text: str | None):
        """The Docker shape: the key is only in the data home's ``.env``."""
        from kiro_crew.config import loader

        env = tmp_path / ".env"
        if text is None:
            env.mkdir()  # present but unreadable as a file
        else:
            env.write_text(text, encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        monkeypatch.setenv("_KIROCREW_CREDS_SCRUBBED", "1")
        monkeypatch.delenv("KIRO_API_KEY", raising=False)
        self.real_env_file = True

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("KIRO_API_KEY=ksk-fixture\n", id="key-only-in-env-file"),
            pytest.param(None, id="unreadable-env-file"),
        ],
    )
    def test_a_key_kept_only_in_the_env_file_keeps_the_marker(self, tmp_path, monkeypatch, text):
        """Sessions sign in with the ``.env`` key the scrub hid from the process,
        so a personal store and ``whoami`` must not decide it; a file that cannot
        be read is doubt, and doubt keeps the marker."""
        self._scrubbed_env_file(tmp_path, monkeypatch, text)
        verdict, calls = self._probe(monkeypatch, may_apply=False, whoami=PERSONAL)
        assert verdict is False
        assert calls == []

    def test_an_env_file_without_the_key_still_asks_whoami(self, tmp_path, monkeypatch):
        self._scrubbed_env_file(tmp_path, monkeypatch, "OTHER=1\n")
        verdict, calls = self._probe(monkeypatch, may_apply=False, whoami=PERSONAL)
        assert verdict is True
        assert calls == [1]

    def test_the_fingerprint_follows_the_env_file_key(self, tmp_path, monkeypatch):
        self._scrubbed_env_file(tmp_path, monkeypatch, "OTHER=1\n")
        before = kiro_cli._sign_in_fingerprint(tmp_path, {})
        (tmp_path / ".env").write_text("KIRO_API_KEY=ksk-fixture\n", encoding="utf-8")
        after = kiro_cli._sign_in_fingerprint(tmp_path, {})
        assert after != before
        assert after[2] is True

    def test_no_pinned_kiro_cli_keeps_the_marker_and_runs_nothing(self, monkeypatch):
        """An unattended probe never runs a kiro-cli found only on PATH, and the
        KAS vault's sign-in is not the one kiro-cli applies a registry to."""
        self.pin[0] = None
        verdict, calls = self._probe(monkeypatch, may_apply=False, whoami=PERSONAL)
        assert verdict is False
        assert calls == []

    @pytest.mark.parametrize(
        ("may_apply", "whoami", "expected"),
        [
            pytest.param(False, PERSONAL, True, id="store-and-whoami-say-personal"),
            pytest.param(False, {"account_type": "SocialGoogle"}, True, id="social-is-personal"),
            pytest.param(False, None, False, id="whoami-unknown-keeps-the-marker"),
            pytest.param(False, {}, False, id="no-account-type-keeps-the-marker"),
            pytest.param(False, {"account_type": "IamIdentityCenter"}, False, id="whoami-says-idc"),
            pytest.param(False, {"account_type": "ApiKey"}, False, id="whoami-says-api-key"),
            pytest.param(False, {"account_type": "SomethingNew"}, False, id="unknown-type"),
            pytest.param(True, PERSONAL, False, id="store-says-governed"),
            pytest.param(False, RuntimeError("boom"), False, id="probe-error-keeps-the-marker"),
        ],
    )
    def test_only_two_agreeing_signals_say_unreachable(
        self, monkeypatch, may_apply, whoami, expected
    ):
        verdict, _calls = self._probe(monkeypatch, may_apply=may_apply, whoami=whoami)
        assert verdict is expected

    def test_a_governed_store_never_runs_whoami(self, monkeypatch):
        _verdict, calls = self._probe(monkeypatch, may_apply=True, whoami=PERSONAL)
        assert calls == []

    def test_the_answer_is_reused_within_the_window(self, monkeypatch):
        first, calls = self._probe(monkeypatch, may_apply=False, whoami=PERSONAL)
        assert first is True
        assert real_probe() is True
        assert calls == [1]

    def test_the_answer_is_asked_again_after_the_window(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(kiro_cli, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        _first, calls = self._probe(monkeypatch, may_apply=False, whoami=PERSONAL)
        clock[0] += kiro_cli._GOVERNANCE_UNREACHABLE_TTL_SECS + 1
        assert real_probe() is True
        assert calls == [1, 1]

    def test_a_sign_in_change_is_seen_inside_the_window(self, monkeypatch):
        """A sign-in writes kiro-cli's store, so a spec rebuilt right after it must
        not reuse the answer from before it."""
        _first, calls = self._probe(monkeypatch, may_apply=False, whoami=PERSONAL)
        monkeypatch.setattr(kiro_cli, "mcp_governance_may_apply", lambda: True)
        self.fingerprint[0] = "store-v2"
        assert real_probe() is False
        assert calls == [1]

    def test_the_fingerprint_follows_the_store_and_the_key(self, tmp_path):
        before = kiro_cli._sign_in_fingerprint(tmp_path, {})
        db = kiro_cli.kiro_cli_state_dbs(kiro_cli.sys.platform, tmp_path, {})[0]
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"x")
        assert kiro_cli._sign_in_fingerprint(tmp_path, {}) != before
        assert kiro_cli._sign_in_fingerprint(tmp_path, {"KIRO_API_KEY": "set"})[1] is True


class TestProbeOnEventLoop:
    """``whoami`` can take seconds under a process-wide lock, and spec writers are
    reached from coroutines through many synchronous helpers. On a thread running
    an event loop the probe must never run it, never wait for that lock, and never
    answer anything but a cached verdict or doubt."""

    WARM_THREAD = "kirocrew-registry-probe"

    @pytest.fixture(autouse=True)
    def _signals(self, _floor_monkeypatch):
        from kiro_crew.atomic_write import on_event_loop
        from kiro_crew.cloud import login_target

        self.fingerprint = ["store-v1"]
        self.whoami_threads: list[bool] = []
        self.asked = threading.Event()

        def fake_whoami(kiro_bin=None):
            self.whoami_threads.append(on_event_loop())
            self.asked.set()
            return PERSONAL

        _floor_monkeypatch.setattr(kiro_cli, "_governance_unreachable_cache", None)
        _floor_monkeypatch.setattr(kiro_cli, "_governance_warm_running", False)
        _floor_monkeypatch.setattr(kiro_cli, "mcp_governance_may_apply", lambda: False)
        _floor_monkeypatch.setattr(kiro_cli, "_data_home_api_key_may_be_set", lambda: False)
        _floor_monkeypatch.setattr(kiro_cli, "_sign_in_fingerprint", lambda: self.fingerprint[0])
        _floor_monkeypatch.setattr(login_target, "pin_kiro_cli", lambda: (PINNED, False))
        _floor_monkeypatch.setattr(login_target, "discover_local_identity", fake_whoami)
        yield
        self._join_warm()

    @staticmethod
    def _on_loop(fn):
        async def _call():
            return fn()

        return asyncio.run(_call())

    def _join_warm(self) -> None:
        for thread in threading.enumerate():
            if thread.name == self.WARM_THREAD:
                thread.join(timeout=10)
                assert not thread.is_alive(), "the warm-up thread did not finish"

    def _wait_for_warm(self) -> None:
        assert self.asked.wait(timeout=10), "the warm-up never asked whoami"
        self._join_warm()

    def test_whoami_never_runs_on_a_running_event_loop(self):
        assert self._on_loop(real_probe) is False, "a cold probe on the loop is doubt"
        self._wait_for_warm()
        assert self.whoami_threads == [False], "whoami ran on the event loop thread"
        assert self._on_loop(real_probe) is True
        assert self.whoami_threads == [False]

    def test_the_loop_never_takes_the_probe_lock(self, monkeypatch):
        """An off-loop probe holds this lock for the whole ``whoami``."""

        class _UntouchableLock:
            def __enter__(self):
                raise AssertionError("the probe lock was taken on the event loop thread")

            def __exit__(self, *exc):
                return False

            acquire = __enter__

        monkeypatch.setattr(kiro_cli, "_governance_unreachable_lock", _UntouchableLock())
        monkeypatch.setattr(
            kiro_cli, "_governance_unreachable_cache", (time.monotonic(), "store-v1", True)
        )
        assert self._on_loop(real_probe) is True
        assert self.whoami_threads == []

    def test_a_sign_in_change_on_the_loop_is_doubt(self, monkeypatch):
        monkeypatch.setattr(
            kiro_cli, "_governance_unreachable_cache", (time.monotonic(), "store-v1", True)
        )
        self.fingerprint[0] = "store-v2"
        assert self._on_loop(real_probe) is False
        self._wait_for_warm()
        assert self.whoami_threads == [False]

    def test_an_aged_answer_is_reused_and_refreshed_off_the_loop(self, monkeypatch):
        aged = time.monotonic() - kiro_cli._GOVERNANCE_UNREACHABLE_TTL_SECS - 1
        monkeypatch.setattr(kiro_cli, "_governance_unreachable_cache", (aged, "store-v1", True))
        assert self._on_loop(real_probe) is True
        self._wait_for_warm()
        assert self.whoami_threads == [False]
        assert kiro_cli._governance_unreachable_cache[0] > aged

    def test_a_spec_write_on_the_loop_keeps_the_marker_until_known(self, monkeypatch):
        from kiro_crew.agent_materialization import managed_mcp

        monkeypatch.setattr(kiro_cli, "registry_governance_known_unreachable", real_probe)
        assert self._on_loop(lambda: managed_mcp._registry_marker_for("kirocrew-core", True))
        self._wait_for_warm()
        assert self.whoami_threads == [False]
        assert not self._on_loop(lambda: managed_mcp._registry_marker_for("kirocrew-core", True))

    def test_off_the_loop_the_probe_still_asks_inline(self):
        assert real_probe() is True
        assert self.whoami_threads == [False]
        assert not any(t.name == self.WARM_THREAD for t in threading.enumerate())
