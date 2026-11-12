"""``max_memory_mb`` caps resident memory only; ``RLIMIT_AS`` has its own opt-in key.

A resident cap must not also cap the address space. ``RLIMIT_AS`` is applied
soft == hard to every ``tool``-profile child through the exec shim, script and
command crons spawn on that profile, and Node/V8 and GraalVM native binaries
reserve far more address space than they touch: under a 5 GiB cap Node's
``fetch()`` cannot allocate its Wasm parser and a native image cannot create its
isolate, so neither starts. These tests pin the split end to end: the resident
cap still reaches the cgroup ``MemoryMax``, no address-space cap follows it onto
the shim payload, ``RLIMIT_AS`` comes only from ``max_address_space_mb``, the
extractor keeps its fixed ceiling, and ``config set`` warns about the new key.

Every test writes under a temp data home; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cli_config import _config_cmd
from kiro_crew.config.loader import _WARNED_RESOURCE_LIMIT_KEYS

_MIB = 1024 * 1024
# The reporter's value: a 5 GiB resident cap on a 31 GB host.
_RESIDENT_MB = 5120


@pytest.fixture(autouse=True)
def _reset_warn_once():
    """The refusal log is once-per-key-per-process, so tests must not inherit it."""
    _WARNED_RESOURCE_LIMIT_KEYS.clear()
    yield
    _WARNED_RESOURCE_LIMIT_KEYS.clear()


def _with_config(raw: dict):
    return patch("kiro_crew.config.loader._raw_config", return_value=raw)


class TestResidentCapSetsNoAddressSpaceLimit:
    def test_max_memory_mb_emits_no_rlimit_as(self):
        from kiro_crew.security import resource_limit_spec

        spec = dict(resource_limit_spec({"resource_limits": {"max_memory_mb": _RESIDENT_MB}}))
        assert "RLIMIT_AS" not in spec
        # The rest of the rlimit policy is unchanged by the key.
        assert spec["RLIMIT_NOFILE"] == 1024

    def test_the_tool_profile_shim_payload_carries_no_address_space_cap(self):
        """The exact seam a script cron child is spawned through."""
        from kiro_crew.sandbox_rlimit import RLIMIT_PROFILE_TOOL, _rlimit_spec

        with _with_config({"resource_limits": {"max_memory_mb": _RESIDENT_MB}}):
            payload = _rlimit_spec(RLIMIT_PROFILE_TOOL)
        assert "RLIMIT_AS" not in payload
        assert "RLIMIT_NOFILE:1024" in payload

    def test_the_resident_cap_still_reaches_the_cgroup_memory_max(self):
        """A fix that dropped the memory cap would pass the two tests above."""
        from kiro_crew.sandbox_cgroup import _cgroup_limits_from_config

        with _with_config({"resource_limits": {"max_memory_mb": _RESIDENT_MB}}):
            _procs, mem_mb, _weight, _quota = _cgroup_limits_from_config()
        assert mem_mb == _RESIDENT_MB


class TestAddressSpaceKey:
    def test_max_address_space_mb_sets_rlimit_as(self):
        from kiro_crew.security import resource_limit_spec

        spec = dict(resource_limit_spec({"resource_limits": {"max_address_space_mb": 65536}}))
        assert spec["RLIMIT_AS"] == 65536 * _MIB

    def test_zero_and_unset_leave_the_address_space_inherited(self):
        from kiro_crew.security import resource_limit_spec

        assert "RLIMIT_AS" not in dict(resource_limit_spec({}))
        assert "RLIMIT_AS" not in dict(
            resource_limit_spec({"resource_limits": {"max_address_space_mb": 0}})
        )

    def test_the_tool_profile_carries_the_opt_in_cap(self):
        from kiro_crew.sandbox_rlimit import RLIMIT_PROFILE_TOOL, _rlimit_spec

        with _with_config({"resource_limits": {"max_address_space_mb": 65536}}):
            payload = _rlimit_spec(RLIMIT_PROFILE_TOOL)
        assert f"RLIMIT_AS:{65536 * _MIB}" in payload

    def test_the_key_is_independent_of_the_cgroup_memory_max(self):
        import kiro_crew.sandbox_cgroup as cg

        with _with_config({"resource_limits": {"max_address_space_mb": 65536}}):
            _procs, mem_mb, _weight, _quota = cg._cgroup_limits_from_config()
        assert mem_mb == cg._default_max_memory_mb()

    @pytest.mark.skipif(os.name != "posix", reason="the extractor preexec is POSIX only")
    def test_the_extractor_fallback_keeps_its_fixed_address_space_ceiling(self, monkeypatch):
        """The shim-less extractor path asks for its 1 GiB ``RLIMIT_AS`` through
        the same config shape, so it has to name the key that still sets it."""
        import kiro_crew.sandbox_rlimit as sr
        from kiro_crew import security
        from kiro_crew.security import resource_limit_spec

        seen: list[dict] = []
        monkeypatch.setattr(sr, "_EXTRACTOR_PREEXEC", sr._UNSET)
        monkeypatch.setattr(
            security, "apply_resource_limits", lambda cfg=None: seen.append(cfg) or (lambda: None)
        )
        sr.extractor_resource_limit_preexec()
        [cfg] = seen
        assert dict(resource_limit_spec(cfg))["RLIMIT_AS"] == sr._EXTRACTOR_MAX_AS_BYTES


def _set(home: Path, key: str, value: str, *, local: bool = False) -> None:
    args = argparse.Namespace(
        config_action="set",
        key=key,
        value=value,
        value_base64=None,
        file=None,
        local=local,
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=home / "config.json"),
        patch("kiro_crew.cli_config.config_local_path", return_value=home / "config.local.json"),
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "crew"
    d.mkdir()
    return d


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestConfigSetWarning:
    def test_setting_the_address_space_cap_warns_that_it_is_virtual(self, home, capsys):
        _set(home, "resource_limits.max_address_space_mb", "16384")
        captured = capsys.readouterr()
        assert _read(home / "config.json")["resource_limits"]["max_address_space_mb"] == 16384
        assert "✅" in captured.out
        assert "VIRTUAL address space" in captured.err
        assert "Node/V8" in captured.err
        assert "resource_limits.max_memory_mb" in captured.err

    def test_the_overlay_write_warns_too(self, home, capsys):
        _set(home, "resource_limits.max_address_space_mb", "16384", local=True)
        captured = capsys.readouterr()
        assert _read(home / "config.local.json")["resource_limits"]["max_address_space_mb"] == 16384
        assert "VIRTUAL address space" in captured.err

    def test_a_whole_section_write_warns(self, home, capsys):
        _set(home, "resource_limits", json.dumps({"max_address_space_mb": 16384}))
        assert "VIRTUAL address space" in capsys.readouterr().err

    def test_zero_turns_the_cap_off_and_does_not_warn(self, home, capsys):
        _set(home, "resource_limits.max_address_space_mb", "0")
        assert "VIRTUAL address space" not in capsys.readouterr().err

    def test_the_resident_cap_does_not_warn(self, home, capsys):
        _set(home, "resource_limits.max_memory_mb", str(_RESIDENT_MB))
        captured = capsys.readouterr()
        assert _read(home / "config.json")["resource_limits"]["max_memory_mb"] == _RESIDENT_MB
        assert "VIRTUAL address space" not in captured.err
