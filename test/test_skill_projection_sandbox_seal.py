"""The skill projection's side directory is readable inside the sandbox and never writable from it.

``<data_home>/skill-projection/`` holds the ownership sidecars and the view ledger
that ``source_agent_name`` maps a stored ``kirocrew-skill-view-*`` name back through
-- the agent a resumed conversation then runs as, with that agent's grants -- plus
the lease records and the publication lock that decide which views the prune keeps.
A sandboxed process that could write there would pick the agent a later resume runs
as. These tests pin the WRITE half: one READONLY sandbox disposition on both data-home
spellings and both backends, the write-only tool-gate tier, a pre-created directory so
the Linux seal has a name to bind before the first spawn, and the no-follow rule so a
planted link cannot stand in for the directory.
"""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew import sandbox, sandbox_plan, security
from kiro_crew.acp import skill_projection

_LEAF = skill_projection._PROJECTION_SIDE_DIR_NAME
_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(_floor_monkeypatch):
    """A namespace plan asks the host's ``ssh -V``; the answer does not move any mask."""
    _floor_monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


def _crew_path(prefix: str, leaf: str) -> str:
    return os.path.join(os.path.expanduser("~"), f"{prefix}/{leaf}")


def test_the_leaf_name_is_the_one_the_projection_writes_under() -> None:
    # The sandbox spells the leaf as a literal; the pin keeps the two from drifting.
    assert _LEAF == "skill-projection"
    assert skill_projection._projection_side_dir().name == _LEAF
    assert _LEAF in sandbox._CREW_READONLY_LEAVES
    assert _LEAF not in sandbox._CREW_HIDDEN_LEAVES
    assert _LEAF not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES


def test_a_foreign_child_may_read_it() -> None:
    # View names, agent names, paths and digests: nothing to withhold, and an
    # in-sandbox resolver may need to read a record.
    assert _LEAF in sandbox._CREW_CHILD_READABLE_LEAVES
    assert _LEAF not in sandbox._CREW_CHILD_WITHHELD_LEAVES


def test_the_seal_has_a_name_to_bind_before_the_first_spawn_and_follows_no_link() -> None:
    # Absent until a spawn prepares a view, so it is materialised before every
    # sandbox; a link at the name would seal the referent and leave the name free.
    assert _LEAF in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert _LEAF in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert _LEAF in sandbox._DELEGATED_OVERLAP_LEAF_REASONS


@_POSIX_ONLY
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("prefix", _CREW_PREFIXES)
def test_linux_seals_it_read_only_and_does_not_mask_it(mode: str, prefix: str) -> None:
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, mode)
    target = _crew_path(prefix, _LEAF)
    assert target in set(plan.readonly), f"{_LEAF} is writable through the {mode} sandbox"
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
@pytest.mark.parametrize("inside", ("", "/metadata/kirocrew-skill-view-" + "a" * 24 + ".json"))
def test_the_agent_file_tools_may_read_it_but_not_write_it(prefix: str, inside: str) -> None:
    target = os.path.join("~", prefix, _LEAF) + inside
    assert security.is_sensitive_write_path(target)
    assert not security.is_sensitive_path(target)


def test_the_precreated_directory_is_what_the_projection_then_publishes_into(
    tmp_path, monkeypatch
) -> None:
    """An empty pre-created directory reads as no record, no lease and no ledger."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    sandbox._materialize_sealable_ceilings()
    side = tmp_path / _LEAF
    assert side.is_dir() and not any(side.iterdir())
    monkeypatch.setattr(skill_projection, "data_home", lambda: tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    assert skill_projection._scan_projection_leases(agents) == (set(), False)
    assert skill_projection._sidecar_record(agents, "kirocrew-skill-view-" + "a" * 24) == (
        None,
        side / "metadata" / ("kirocrew-skill-view-" + "a" * 24 + ".json"),
        None,
    )
