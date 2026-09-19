"""Integration: harnesses.json -> known + selectable + harness_for-resolvable.

The unit suites cover each seam in isolation. This one drives the whole boot-load
path end to end against a temp ``harnesses.json`` with a real (stub) executable on
PATH, so the three registration sides -- vocabulary, runtime, selection -- and the
invalid/unselectable diagnostics are exercised together, exactly as
``bootstrap_context`` runs them.

Every test restores the process-global registry state in a fixture (registered
ids, the operator register, the selectable pair, and the diagnostic maps), so one
test's boot-load cannot leak into another.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from kiro_crew.acp import harness as harness_pkg
from kiro_crew.acp.harness import DescriptorHarness, harness_for
from kiro_crew.acp.harness import operator_registry as reg
from kiro_crew.agent_sdk import backends as b


@pytest.fixture
def clean_boot():
    """Snapshot/restore every registry surface the boot-load writes."""
    baseline = set(b._baseline)
    selectable = set(b._selectable)
    yield
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    reg._reset_operator_diagnostics()
    b._baseline.clear()
    b._baseline.update(baseline)
    b._selectable.clear()
    b._selectable.update(selectable)


def _stub_executable(tmp_path, name="my-acp"):
    """Create an executable stub on a directory placed on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def _write_harnesses(tmp_path, mapping) -> str:
    path = tmp_path / "harnesses.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return str(path)


def test_valid_agent_spec_becomes_known_selectable_and_resolvable(clean_boot, tmp_path):
    """The acceptance case: a routed descriptor is fully served after the load.

    Known (spellable), selectable (offered in the switch), on the runtime path,
    and resolvable through ``harness_for`` as a ``DescriptorHarness`` whose argv
    renders the stub executable.
    """
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "id": "my-acp",
                "display_name": "My ACP",
                "executable": str(exe),
                "argv": ["{executable}", "serve"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )

    reg.load_and_register_operator_descriptors(path=path)

    # Known + routed + labelled + own-namespaced.
    assert "my-acp" in b.ACP_BACKENDS_KNOWN
    assert b.routing_for("my-acp") is b.Routing.AGENT_SPEC
    assert b.provider_label_for("my-acp") == "My ACP"
    assert b.model_registry_namespace("my-acp") == "my-acp"
    # Selectable, and on the shared-runtime serving path (path A).
    assert "my-acp" in b.selectable_backends()
    assert "my-acp" in b.acp_runtime_backends()
    # Resolvable through the one function every caller uses.
    harness = harness_for("my-acp")
    assert isinstance(harness, DescriptorHarness)
    assert harness.backend == "my-acp"
    # No diagnostic rows for a clean, routed descriptor.
    assert "my-acp" not in reg.invalid_operator_harnesses()
    assert "my-acp" not in reg.unselectable_operator_harnesses()


@pytest.mark.asyncio
# The stub is an extensionless file made runnable with chmod, which shutil.which
# cannot find on Windows because it resolves candidates through PATHEXT. The
# resolver is correct there; only this fixture is POSIX-shaped (the same reason
# test_acp_client.py marks its resolution tests _POSIX_EXEC_PATHS_ONLY).
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable-resolution semantics only")
async def test_the_resolved_harness_spawns_the_stub_executable(clean_boot, tmp_path, monkeypatch):
    """End to end: the registered harness renders an argv naming the stub binary.

    The stub is on PATH, so the generic resolver finds it and render_argv puts its
    resolved path first -- proving the descriptor's executable actually drives a
    spawn plan, not just a listing row.
    """
    from pathlib import Path

    from kiro_crew.acp.harness.base import SpawnContext

    exe = _stub_executable(tmp_path)
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + os.environ.get("PATH", ""))
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "executable": "my-acp",
                "argv": ["{executable}", "serve"],
                "routing": "agent_spec",
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    ctx = SpawnContext(
        agent="a", work_dir=str(tmp_path), model=None, environ={}, home=Path(tmp_path)
    )
    plan = await harness_for("my-acp").resolve_spawn(ctx)
    assert plan.argv[0] == str(exe)
    assert plan.argv[1:] == ["serve"]
    # AGENT_SPEC + no agent_args block => nothing selected at spawn to verify.
    assert plan.extra_hidden_dirs == ()


def test_unroutable_descriptor_is_known_but_not_selectable(clean_boot, tmp_path):
    """A descriptor with no routing registers as known-but-unselectable, with a reason."""
    exe = _stub_executable(tmp_path, name="no-route")
    path = _write_harnesses(
        tmp_path,
        {
            "no-route": {
                "executable": str(exe),
                "argv": ["{executable}"],
                # routing omitted -> valid but unselectable
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    assert "no-route" in b.ACP_BACKENDS_KNOWN  # spellable + nameable
    assert "no-route" not in b.selectable_backends()  # not offered
    assert "no-route" in reg.unselectable_operator_harnesses()
    assert "no-route" not in reg.invalid_operator_harnesses()
    # It IS resolvable as a harness (known + on the runtime set), just not selectable.
    assert isinstance(harness_for("no-route"), DescriptorHarness)


def test_malformed_descriptor_lands_in_invalid_and_costs_only_its_row(clean_boot, tmp_path):
    """A malformed entry is recorded in invalid() and never registered; siblings survive."""
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "executable": str(exe),
                "argv": ["{executable}", "serve"],
                "routing": "agent_spec",
            },
            "bad-one": {
                # argv[0] is not {executable} -> validation failure
                "executable": "x",
                "argv": ["x", "run"],
                "routing": "agent_spec",
            },
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    # The good one is fully served.
    assert "my-acp" in b.selectable_backends()
    assert isinstance(harness_for("my-acp"), DescriptorHarness)
    # The bad one costs only its own row: recorded invalid, registered nowhere.
    assert "bad-one" in reg.invalid_operator_harnesses()
    assert "bad-one" not in b.ACP_BACKENDS_KNOWN
    with pytest.raises(ValueError, match="no ACP harness"):
        harness_for("bad-one")


def test_session_config_descriptor_records_permission_config(clean_boot, tmp_path):
    """A session_config descriptor is enforced: its (option, value) is recorded, selectable."""
    exe = _stub_executable(tmp_path, name="wire-host")
    path = _write_harnesses(
        tmp_path,
        {
            "wire-host": {
                "executable": str(exe),
                "argv": ["{executable}"],
                "routing": "session_config",
                "permission_config": {"option": "mode", "value": "read-only"},
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    assert b.routing_for("wire-host") is b.Routing.SESSION_CONFIG
    assert b.permission_config_for("wire-host") == ("mode", "read-only")
    assert "wire-host" in b.selectable_backends()


def test_a_missing_file_registers_nothing(clean_boot, tmp_path):
    """No harnesses.json is the normal case: nothing registered, no diagnostics."""
    known_before = set(b.ACP_BACKENDS_KNOWN)
    reg.load_and_register_operator_descriptors(path=str(tmp_path / "absent.json"))
    assert set(b.ACP_BACKENDS_KNOWN) == known_before
    assert reg.invalid_operator_harnesses() == {}


def test_the_load_is_idempotent(clean_boot, tmp_path):
    """A second boot-load pass is a no-op, not a re-registration crash.

    bootstrap_context can run twice in one process; re-registering a known id
    raises, so the second pass must skip an already-registered id.
    """
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {"my-acp": {"executable": str(exe), "argv": ["{executable}"], "routing": "agent_spec"}},
    )
    reg.load_and_register_operator_descriptors(path=path)
    # Must not raise on the second pass.
    reg.load_and_register_operator_descriptors(path=path)
    assert "my-acp" in b.selectable_backends()


def test_full_teardown_restores_the_builtin_state(tmp_path):
    """After reset, the registry is back to builtins with no operator harness.

    Does its own before/after bookkeeping rather than using the fixture, to prove
    the resets -- not the fixture -- are what clean up.
    """
    baseline_before = set(b._baseline)
    selectable_before = set(b._selectable)
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {"my-acp": {"executable": str(exe), "argv": ["{executable}"], "routing": "agent_spec"}},
    )
    try:
        reg.load_and_register_operator_descriptors(path=path)
        assert "my-acp" in b.ACP_BACKENDS_KNOWN

        b._reset_registered_backends()
        harness_pkg._reset_operator_register()
        reg._reset_operator_diagnostics()

        assert "my-acp" not in b.ACP_BACKENDS_KNOWN
        assert "my-acp" not in b.acp_runtime_backends()
        with pytest.raises(ValueError, match="no ACP harness"):
            harness_for("my-acp")
        assert reg.invalid_operator_harnesses() == {}
        assert reg.unselectable_operator_harnesses() == {}
    finally:
        b._selectable.clear()
        b._selectable.update(selectable_before)
        b._baseline.clear()
        b._baseline.update(baseline_before)
