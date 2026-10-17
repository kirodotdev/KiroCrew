"""``gateway_id`` is readable inside the sandbox and never writable from it.

The id is what the Remote Crew cycle guard compares to refuse a chain that loops back
on itself, so a sandboxed process that can replace the file chooses whether a loop is
detected. The read path already judges the descriptor it opens; these tests pin the
WRITE half: one READONLY sandbox disposition, the write-only tool-gate tier, and a
pre-created stub so the Linux seal has a name to bind before the first mint.
"""

from __future__ import annotations

import os
import sys

import pytest
from test_sandbox_launcher_program import RecordingLibc, launch, refusal

from kiro_crew import gateway_identity, sandbox, sandbox_launcher_program, sandbox_plan, security

_LEAF = gateway_identity.GATEWAY_ID_FILE
_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")

_MS_BIND = 4096
_MS_REMOUNT = 32
_MS_RDONLY = 1


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(_floor_monkeypatch):
    """A namespace plan asks the host's ``ssh -V``; the answer does not move any mask."""
    _floor_monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_cache():
    gateway_identity._CACHED_IDS.clear()
    yield
    gateway_identity._CACHED_IDS.clear()


def _crew_path(prefix: str, leaf: str) -> str:
    return os.path.join(os.path.expanduser("~"), f"{prefix}/{leaf}")


def test_the_leaf_name_is_the_one_the_identity_module_writes() -> None:
    # The sandbox spells the leaf as a literal; the pin keeps the two from drifting.
    assert _LEAF == "gateway_id"
    assert _LEAF in sandbox._CREW_READONLY_LEAVES
    assert _LEAF not in sandbox._CREW_HIDDEN_LEAVES
    assert _LEAF not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES


def test_a_foreign_child_may_read_it() -> None:
    # A random id, already published on /api/health: nothing to withhold.
    assert _LEAF in sandbox._CREW_CHILD_READABLE_LEAVES
    assert _LEAF not in sandbox._CREW_CHILD_WITHHELD_LEAVES


@_POSIX_ONLY
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("prefix", _CREW_PREFIXES)
def test_linux_seals_it_read_only_and_does_not_mask_it(mode: str, prefix: str) -> None:
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, mode)
    target = _crew_path(prefix, _LEAF)
    assert target in set(plan.readonly), f"gateway_id is writable through the {mode} sandbox"
    assert target not in set(plan.sensitive_dirs)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("prefix", _CREW_PREFIXES)
def test_macos_denies_writes_but_not_reads(mode: str, prefix: str) -> None:
    profile = sandbox._build_seatbelt_profile(mode)
    target = _crew_path(prefix, _LEAF)
    assert f'(deny file-write* (literal "{target}"))' in profile
    assert f'(deny file-write* (subpath "{target}"))' in profile
    assert f'(deny file-link (subpath "{target}"))' in profile
    assert f'(deny file-read* (subpath "{target}"))' not in profile


@pytest.mark.parametrize("prefix", _CREW_PREFIXES)
def test_the_agent_file_tools_may_read_it_but_not_write_it(prefix: str) -> None:
    target = os.path.join("~", prefix, _LEAF)
    assert security.is_sensitive_write_path(target)
    assert not security.is_sensitive_path(target)


@_LINUX_ONLY
@pytest.mark.parametrize("mode", _MODES)
def test_an_in_sandbox_write_is_refused_by_a_read_only_bind(
    mode: str, tmp_path, monkeypatch
) -> None:
    """Drive the launcher's seal stage: the id file is bound over itself, then remounted RO."""
    monkeypatch.setenv("HOME", str(tmp_path))
    id_file = tmp_path / ".kiro" / "crew" / _LEAF
    id_file.parent.mkdir(parents=True)
    id_file.write_text("0" * 32, encoding="utf-8")
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, mode)
    assert str(id_file) in plan.readonly
    libc = RecordingLibc()
    run = launch(tmp_path, sandbox_plan.namespace_payload(plan), libc=libc)

    assert refusal(sandbox_launcher_program.seal_readonly, run) is None
    on_leaf = [c for c in libc.calls if c.target_path == os.path.realpath(id_file)]
    assert len(on_leaf) == 2, "gateway_id was not bound and sealed"
    bind, seal = on_leaf
    assert bind.target.startswith(b"/proc/self/fd/")
    assert bind.flags == _MS_BIND
    sealing = _MS_REMOUNT | _MS_BIND | _MS_RDONLY
    assert seal.flags & sealing == sealing


class TestTheSpawnMintsTheIdBeforeSealing:
    """``mount(2)`` cannot seal an absent name, and the id is minted only on first read."""

    def test_the_leaf_is_not_precreated_as_a_stub(self) -> None:
        # A stub the gateway later replaces would detach the bind in a running sandbox.
        assert _LEAF not in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES

    def test_the_spawn_mints_the_real_id_before_sealing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sandbox._materialize_sealable_ceilings()
        minted = (tmp_path / _LEAF).read_text(encoding="utf-8")
        assert gateway_identity._ID_RE.match(minted)
        assert gateway_identity.gateway_id(create=False) == minted

    def test_an_existing_id_is_never_overwritten(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / _LEAF).write_text("a" * 32, encoding="utf-8")
        sandbox._materialize_sealable_ceilings()
        assert (tmp_path / _LEAF).read_text(encoding="utf-8") == "a" * 32

    def test_an_absent_data_home_is_not_scaffolded(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "absent-home"
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        sandbox._mint_gateway_id_before_seal()
        assert not home.exists()
