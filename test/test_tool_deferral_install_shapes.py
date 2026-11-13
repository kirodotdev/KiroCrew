"""Which kiro-cli installs let Kiro Crew's own MCP servers defer, and the fallback.

The decision is made on the file the session is about to run:

* the pinned install: unchanged, covered by ``test_acp_tool_search_spawn_env.py``;
* the operator's ``KIROCREW_KIRO_BIN``: the version the pin's probe already gives;
* a kiro-cli found only through ``PATH``: the version read inside the sandbox,
  never waited for;
* anything else (a pod's bundle, a sibling nobody probed): resident, as before.

The floor is empirical, so a refusal above it is remembered: from the first
request refused for a changed tools list, every spawn sends the full list.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from kiro_crew import kiro_cli, kiro_cli_path_probe
from kiro_crew.acp.client import AcpConversationBindingMismatch, AcpError, _raise_acp_error
from kiro_crew.acp.harness._common import MANDATORY_MCPS_ENV
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.harness.kiro import KiroHarness
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.kiro_prerequisite_process import ProcessResult

NEW = (3, 2, 1)
FLOOR = kiro_cli.MANDATORY_MCPS_DROP_MIN_VERSION
OLD = (2, 26, 1)

# The provider's own words for the refusal the never-defer rule exists to
# prevent, as an ACP error frame carries them.
TOOLS_LIST_REFUSAL = {
    "code": -32603,
    "message": "Prompt failed",
    "data": (
        "messages.3.content.0: Invalid `signature` in `thinking` block. "
        "The block is bound to a different conversation. "
        "The `tools` list differs from the one this block was created with."
    ),
}


def _executable(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("executable")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch):
    """Record every version read, by route. A file with no answer fails the test."""
    record: dict = {"pin": [], "path": [], "pin_versions": {}, "path_versions": {}}

    def _pin_probe(binary: str):
        if binary not in record["pin_versions"]:
            pytest.fail(f"unexpected version read on the pin's route: {binary}")
        record["pin"].append(binary)
        return record["pin_versions"][binary]

    def _path_answer(binary: str):
        if binary not in record["path_versions"]:
            pytest.fail(f"unexpected version read on the PATH route: {binary}")
        record["path"].append(binary)
        return record["path_versions"][binary]

    monkeypatch.setattr(kiro_cli, "kiro_cli_version_at", _pin_probe)
    monkeypatch.setattr(kiro_cli_path_probe, "version_if_known", _path_answer)
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    return record


def _path_only(monkeypatch: pytest.MonkeyPatch, binary: str) -> None:
    """No pinned install; the resolver finds *binary* through ``PATH``."""
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, True))
    monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **_kw: binary)


def _override(monkeypatch: pytest.MonkeyPatch, binary: str) -> None:
    """``KIROCREW_KIRO_BIN`` names *binary*, which the pin then is."""
    monkeypatch.setenv("KIROCREW_KIRO_BIN", binary)
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (binary, False))


class TestPathOnlyInstall:
    @pytest.mark.parametrize(
        ("version", "allowed"),
        [(NEW, True), ((3, 0, 0), True), (OLD, False), (None, False)],
        ids=["new", "first-standalone", "old", "unknown-or-not-answered-yet"],
    )
    def test_the_sandboxed_answer_decides(self, version, allowed, tmp_path, probes, monkeypatch):
        binary = _executable(tmp_path / "usr-local-bin" / "kiro-cli")
        _path_only(monkeypatch, binary)
        probes["path_versions"][binary] = version

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is allowed
        assert probes["path"] == [binary]
        assert probes["pin"] == [], "a PATH file is never read on the pin's route"

    def test_a_posix_launcher_below_3_stays_resident(self, tmp_path, probes, monkeypatch):
        """Its own version says nothing about the ``kiro-cli-chat`` it execs."""
        if sys.platform == "win32":
            pytest.skip("Windows ships one self-contained kiro-cli.exe")
        binary = _executable(tmp_path / "bin" / "kiro-cli")
        _executable(tmp_path / "bin" / "kiro-cli-chat")
        _path_only(monkeypatch, binary)
        probes["path_versions"][binary] = (2, 37, 1)

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is False
        assert probes["path"] == [binary], "no sibling is derived or read"

    def test_a_file_the_resolver_did_not_choose_is_not_read(self, tmp_path, probes, monkeypatch):
        """A pod's bundle, or a chat sibling swapped in, is not the PATH install."""
        found = _executable(tmp_path / "bin" / "kiro-cli")
        bundle = _executable(tmp_path / "pod" / "bundle" / "kiro-cli-chat")
        _path_only(monkeypatch, found)
        probes["path_versions"][found] = NEW

        assert kiro_cli.mandatory_mcps_drop_allowed(bundle) is False
        assert (
            kiro_cli.mandatory_mcps_drop_allowed(str(tmp_path / "bin" / "kiro-cli-chat")) is False
        )
        assert probes["path"] == [] and probes["pin"] == []

    def test_no_install_at_all_reads_nothing(self, tmp_path, probes, monkeypatch):
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, False))

        assert kiro_cli.mandatory_mcps_drop_allowed(str(tmp_path / "kiro-cli")) is False
        assert probes["path"] == [] and probes["pin"] == []

    def test_a_file_changed_after_the_read_is_unknown_until_read_again(self, tmp_path, monkeypatch):
        """The answer belongs to a file identity; a swap between read and spawn has none."""
        binary = _executable(tmp_path / "bin" / "kiro-cli")
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        _path_only(monkeypatch, binary)
        started: list = []
        monkeypatch.setattr(
            kiro_cli_path_probe.threading,
            "Thread",
            lambda **kw: type("T", (), {"start": lambda self: started.append(kw)})(),
        )
        key = (binary, os.stat(binary).st_mtime_ns)
        monkeypatch.setitem(kiro_cli_path_probe._versions, key, NEW)
        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is True

        stat = os.stat(binary)
        os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is False
        assert len(started) == 1, "the new file is read in the background, not waited for"


class TestOperatorOverride:
    @pytest.mark.parametrize(
        ("version", "allowed"),
        [(NEW, True), (OLD, False), (None, False)],
        ids=["new", "old", "unreadable"],
    )
    def test_the_pin_probe_answer_decides(self, version, allowed, tmp_path, probes, monkeypatch):
        binary = _executable(tmp_path / "opt" / "kiro" / "kiro-cli")
        _override(monkeypatch, binary)
        probes["pin_versions"][binary] = version

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is allowed
        assert probes["pin"] == [binary]
        assert probes["path"] == []

    def test_a_file_named_otherwise_needs_only_the_floor(self, tmp_path, probes, monkeypatch):
        binary = _executable(tmp_path / "opt" / "kiro" / "kiro-cli-chat")
        _override(monkeypatch, binary)
        probes["pin_versions"][binary] = FLOOR

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is True

    def test_a_posix_launcher_below_3_stays_resident(self, tmp_path, probes, monkeypatch):
        if sys.platform == "win32":
            pytest.skip("Windows ships one self-contained kiro-cli.exe")
        binary = _executable(tmp_path / "opt" / "kiro" / "kiro-cli")
        _executable(tmp_path / "opt" / "kiro" / "kiro-cli-chat")
        _override(monkeypatch, binary)
        probes["pin_versions"][binary] = (2, 37, 1)

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is False
        assert probes["pin"] == [binary], "the sibling beside an override is never probed"

    def test_another_file_spawned_under_an_override_stays_resident(
        self, tmp_path, probes, monkeypatch
    ):
        binary = _executable(tmp_path / "opt" / "kiro" / "kiro-cli")
        other = _executable(tmp_path / "pod" / "kiro-cli")
        _override(monkeypatch, binary)

        assert kiro_cli.mandatory_mcps_drop_allowed(other) is False
        assert probes["pin"] == [] and probes["path"] == []

    def test_an_override_that_is_not_the_pin_stays_resident(self, tmp_path, probes, monkeypatch):
        """A relative or unusable override: the pin is absent or another install."""
        installed = _executable(tmp_path / "home" / ".local" / "bin" / "kiro-cli")
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "kiro-cli")
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, True))
        assert kiro_cli.mandatory_mcps_drop_allowed(installed) is False

        monkeypatch.setenv("KIROCREW_KIRO_BIN", str(tmp_path / "missing" / "kiro-cli"))
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (installed, False))
        assert kiro_cli.mandatory_mcps_drop_allowed(installed) is False
        assert probes["pin"] == [] and probes["path"] == []

    def test_a_replaced_override_is_judged_by_its_new_version(self, tmp_path, monkeypatch):
        """The pin's probe is keyed by path and mtime, so a swap is read again."""
        binary = _executable(tmp_path / "opt" / "kiro" / "kiro-cli")
        _override(monkeypatch, binary)
        monkeypatch.setattr(kiro_cli, "_version_cache", {})
        outputs = iter(["kiro-cli 3.2.1\n", "kiro-cli 2.20.0\n"])
        runs: list = []

        def _probe(probed, args, _timeout_secs):
            runs.append([probed, *args])
            return ProcessResult(ok=True, output=next(outputs), returncode=0)

        monkeypatch.setattr(kiro_cli, "_sandboxed_probe", _probe)
        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is True
        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is True
        assert runs == [[binary, "--version"]]

        stat = os.stat(binary)
        os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is False
        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is False
        assert runs == [[binary, "--version"]] * 2


class TestPathOnlyInstallLookup:
    def test_it_is_the_resolvers_answer_made_absolute(self, tmp_path, monkeypatch):
        binary = _executable(tmp_path / "bin" / "kiro-cli")
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        _path_only(monkeypatch, binary)

        assert kiro_cli.path_only_install() == os.path.abspath(binary)

    @pytest.mark.parametrize("shape", ["pinned", "override", "none"])
    def test_every_other_shape_has_none(self, shape, tmp_path, monkeypatch):
        binary = _executable(tmp_path / "bin" / "kiro-cli")
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **_kw: binary)
        if shape == "pinned":
            monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (binary, False))
        elif shape == "override":
            monkeypatch.setenv("KIROCREW_KIRO_BIN", "kiro-cli")
            monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, True))
        else:
            monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, False))

        assert kiro_cli.path_only_install() is None


class TestTheRefusalIsRemembered:
    """The fallback: one refusal turns deferral off for the life of the process."""

    def _pinned_and_new(self, tmp_path, monkeypatch) -> str:
        binary = _executable(tmp_path / "install" / "kiro-cli")
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (binary, False))
        monkeypatch.setattr(kiro_cli, "kiro_cli_version_at", lambda _binary: NEW)
        return binary

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS], ids=["kiro", "kas"])
    def test_the_real_refusal_is_typed_and_remembered(self, backend, tmp_path, monkeypatch):
        binary = self._pinned_and_new(tmp_path, monkeypatch)
        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is True

        with pytest.raises(AcpConversationBindingMismatch) as raised:
            _raise_acp_error(TOOLS_LIST_REFUSAL, backend=backend)

        assert raised.value.transient is False
        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is False

    @pytest.mark.parametrize("harness", [KiroHarness, KasHarness], ids=["kiro", "kas"])
    def test_the_next_spawn_names_every_crew_server(self, harness, tmp_path, monkeypatch):
        """What the rebuilt session's process is started with: the full send."""
        from kiro_crew import agent as agent_mod

        binary = self._pinned_and_new(tmp_path, monkeypatch)
        monkeypatch.delenv(MANDATORY_MCPS_ENV, raising=False)
        monkeypatch.setattr(agent_mod, "crew_owned_mcp_servers", lambda: {"kirocrew-core"})
        monkeypatch.setattr(
            "kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None, raising=True
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None, raising=True
        )
        before: dict[str, str] = {}
        harness().apply_spawn_env(before, spawned_binary=binary)
        assert MANDATORY_MCPS_ENV not in before

        with pytest.raises(AcpConversationBindingMismatch):
            _raise_acp_error(TOOLS_LIST_REFUSAL)

        after: dict[str, str] = {}
        harness().apply_spawn_env(after, spawned_binary=binary)
        assert after[MANDATORY_MCPS_ENV] == "kirocrew-core"

    def test_a_remembered_refusal_reads_no_version(self, tmp_path, probes, monkeypatch):
        binary = _executable(tmp_path / "bin" / "kiro-cli")
        _path_only(monkeypatch, binary)
        kiro_cli.note_tools_list_refusal()

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is False
        assert probes["path"] == [] and probes["pin"] == []

    @pytest.mark.parametrize(
        "frame",
        [
            # Another binding mismatch: the content changed, not the tools list.
            {
                "code": -32603,
                "message": "Prompt failed",
                "data": (
                    "messages.3.content.0: Invalid `signature` in `thinking` block. "
                    "The block is bound to a different conversation. Content before "
                    "this block differs from when it was created."
                ),
            },
            # The reason sits in a field the mismatch itself is not in.
            {
                "code": -32603,
                "message": "The `tools` list differs from the one this block was created with.",
                "data": (
                    "messages.3.content.0: Invalid `signature` in `thinking` block. "
                    "The block is bound to a different conversation."
                ),
            },
            # Not a binding mismatch at all.
            {
                "code": -32603,
                "message": "Prompt failed",
                "data": "The `tools` list differs from the one this block was created with.",
            },
        ],
        ids=["content-changed", "reason-in-another-field", "no-mismatch"],
    )
    def test_any_other_refusal_is_not_remembered(self, frame, tmp_path, monkeypatch):
        binary = self._pinned_and_new(tmp_path, monkeypatch)

        with pytest.raises(AcpError):
            _raise_acp_error(frame)

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is True

    def test_another_harness_refusal_is_not_remembered(self, tmp_path, monkeypatch):
        """Only kiro-cli's own spawns read the never-defer list this decides."""
        binary = self._pinned_and_new(tmp_path, monkeypatch)

        with pytest.raises(AcpConversationBindingMismatch):
            _raise_acp_error(TOOLS_LIST_REFUSAL, backend=ACP_BACKEND_CLAUDE)

        assert kiro_cli.mandatory_mcps_drop_allowed(binary) is True

    def test_only_the_first_refusal_sets_the_time(self):
        kiro_cli.note_tools_list_refusal()
        first = kiro_cli._tools_list_refused_at
        kiro_cli.note_tools_list_refusal()

        assert first is not None and kiro_cli._tools_list_refused_at == first
        assert kiro_cli.spawned_before_tools_list_refusal(first) is True
        assert kiro_cli.spawned_before_tools_list_refusal(first + 0.5) is False

    def test_no_refusal_fences_no_process(self):
        assert kiro_cli.spawned_before_tools_list_refusal(0.0) is False


class TestWarmPoolAfterARefusal:
    """A pre-started process read its never-defer list before the refusal."""

    @staticmethod
    def _manager():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        discarded: list = []

        async def _discard(provider, context: str) -> None:
            discarded.append((provider, context))

        smap._discard_pool_provider = _discard  # type: ignore[method-assign]
        smap._schedule_replenish = lambda: None  # type: ignore[method-assign]
        return smap, discarded

    @staticmethod
    def _provider():
        return type("Provider", (), {"is_process_alive": lambda self: True, "exit_code": None})()

    @pytest.mark.asyncio
    async def test_a_provider_started_before_the_refusal_is_discarded(self, monkeypatch) -> None:
        smap, discarded = self._manager()
        stale = self._provider()
        started = time.monotonic()
        smap._warm_pool.put_nowait((stale, started))
        monkeypatch.setattr(kiro_cli, "_tools_list_refused_at", started + 1.0)

        assert await smap._drain_and_claim(None) is None
        assert discarded == [(stale, "Warm pool deferral discard")]

    @pytest.mark.asyncio
    async def test_a_provider_started_after_it_is_claimed(self, monkeypatch) -> None:
        smap, discarded = self._manager()
        fresh = self._provider()
        started = time.monotonic()
        monkeypatch.setattr(kiro_cli, "_tools_list_refused_at", started - 1.0)
        smap._warm_pool.put_nowait((fresh, started))

        assert await smap._drain_and_claim(None) is fresh
        assert discarded == []

    @pytest.mark.asyncio
    async def test_no_refusal_claims_normally(self) -> None:
        smap, discarded = self._manager()
        provider = self._provider()
        smap._warm_pool.put_nowait((provider, time.monotonic()))

        assert await smap._drain_and_claim(None) is provider
        assert discarded == []
