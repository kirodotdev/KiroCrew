"""A view already proved current is not read again on every spawn.

Every Kiro CLI (v2) spawn re-checks every agent's published view under the shared
alias lock. Each full check reads the view and its sidecar through the
sensitive-path screen, so a burst of spawns queued for minutes behind each other.
These tests pin that an unchanged view is skipped, and that any change to either
file still gets the full check and a republish.
"""

from __future__ import annotations

import json

import native_skill_projection_helpers
import pytest
from native_skill_projection_helpers import _alias_file, _metadata_file

from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.skill_projection_parts import view_ownership

_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
native_tree = native_skill_projection_helpers.native_tree
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)


@pytest.fixture
def reads(monkeypatch):
    """Track currency-check reads with fingerprint memoization enabled on every OS.

    The age floor is lifted so a file this test just wrote can be remembered;
    ``test_a_just_written_view_is_not_remembered`` puts it back.
    """
    monkeypatch.setattr(view_ownership, "_fingerprints_show_writes", lambda: True, raising=False)
    monkeypatch.setattr(view_ownership, "_PROVED_CURRENT_MIN_AGE_NS", 0, raising=False)
    seen: list[str] = []
    real = projection.safe_read_file_bytes

    def counting(path, *args, **kwargs):
        seen.append(str(path))
        return real(path, *args, **kwargs)

    # Through the facade: it forwards the patch to every part that holds the name.
    monkeypatch.setattr(projection, "safe_read_file_bytes", counting)
    return seen


def _published(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps({"name": "custom", "tools": ["read"]}), encoding="utf-8"
    )
    # The first preparation publishes; the second proves the published view current.
    projection.prepare_native_skill_projection(project)
    return agents, project, projection.prepare_native_skill_projection(project)


def test_an_unchanged_view_is_not_read_again(native_tree, reads):
    agents, project, proved = _published(native_tree)
    alias = _alias_file(agents, proved)
    reads.clear()

    again = projection.prepare_native_skill_projection(project)

    assert again.agent("custom") == proved.agent("custom")
    assert str(alias) not in reads
    assert str(_metadata_file(agents, proved)) not in reads


def test_a_view_edited_in_place_is_checked_and_republished(native_tree, reads):
    agents, project, proved = _published(native_tree)
    alias = _alias_file(agents, proved)
    good = alias.read_text(encoding="utf-8")
    with alias.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"name": alias.stem, "tools": ["*"], "resources": []}))
    reads.clear()

    projection.prepare_native_skill_projection(project)

    assert str(alias) in reads
    assert json.loads(alias.read_text(encoding="utf-8")) == json.loads(good)


def test_a_sidecar_edited_in_place_is_checked_again(native_tree, reads):
    agents, project, proved = _published(native_tree)
    sidecar = _metadata_file(agents, proved)
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record[view_ownership._MANAGED_CREW_HOME] = "/some/other/home"
    with sidecar.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(record))
    reads.clear()

    projection.prepare_native_skill_projection(project)

    assert str(_alias_file(agents, proved)) in reads
    rewritten = json.loads(sidecar.read_text(encoding="utf-8"))
    assert rewritten[view_ownership._MANAGED_CREW_HOME] != "/some/other/home"


def test_windows_always_runs_the_full_check(native_tree, reads, monkeypatch):
    """Windows reports creation time as ctime, so the fingerprint cannot see an edit."""
    monkeypatch.setattr(view_ownership, "_fingerprints_show_writes", lambda: False, raising=False)
    agents, project, proved = _published(native_tree)
    reads.clear()

    projection.prepare_native_skill_projection(project)

    assert str(_alias_file(agents, proved)) in reads


def test_a_just_written_view_is_not_remembered(native_tree, reads, monkeypatch):
    """A coarse timestamp tick can hide a second write, so a fresh file is read again."""
    monkeypatch.setattr(view_ownership, "_PROVED_CURRENT_MIN_AGE_NS", 3_600 * 10**9, raising=False)
    agents, project, proved = _published(native_tree)
    reads.clear()

    projection.prepare_native_skill_projection(project)

    assert str(_alias_file(agents, proved)) in reads
