"""What holds up against launchers that rewrite specs and against older builds.

A stable alias identity across relaunches, the sidecar and lock residue sweeps, the
display-text bounds, ``recognise()``, side files under the data home, and the prune a
projection runs when it ends.
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import native_skill_projection_helpers
import pytest
from native_skill_projection_helpers import (
    _alias_file,
    _crew_home_id,
    _hold_the_prune_clock,
    _metadata_file,
    _wait_for_dropped_projection_ends,
)

from kiro_crew.acp import skill_projection as projection
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import wait_until

# The shared fixtures, bound here by name so pytest registers them for this module.
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
native_tree = native_skill_projection_helpers.native_tree
# The exit-hook tests swap ``_COLLECTED_ENDS`` for a private queue. The real reaper
# reads that global on every loop, so one still finishing an earlier test's end
# would follow the swap and take the test's items: drain it after every test.
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)


_REAL_EAGER_PRUNE = projection._eager_prune


# ── a launcher that rewrites every spec on each launch ──


def _launcher_spec(nonce, extra_env=None):
    """An authored spec as a credential-broker launcher leaves it: a fresh nonce
    stamped into one MCP server's ``env`` on every launch."""
    env = {"BROKER_URL": "${BROKER_URL}", "AIM_CREDS_AGENT_INJECTION": nonce, **(extra_env or {})}
    return {
        "name": "custom",
        "tools": ["@broker"],
        "mcpServers": {"broker": {"command": "broker", "args": ["serve"], "env": env}},
    }


def _launcher_reserialize(path, nonce=None):
    """Rewrite *path* the way that launcher does: a different serializer and,
    optionally, its own fresh nonce, published by rename (a new inode)."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    if nonce is not None:
        spec["mcpServers"]["broker"]["env"]["AIM_CREDS_AGENT_INJECTION"] = nonce
    staged = path.with_name(path.name + ".launcher")
    staged.write_text(json.dumps(spec, indent=2).replace('": ', '" : '), encoding="utf-8")
    os.replace(staged, path)


def _aliases_on_disk(agents):
    return sorted(p.name for p in agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))


def test_a_relaunched_env_nonce_keeps_one_alias_name_per_agent(native_tree):
    """Two preparations whose source differs only by a re-stamped ``env`` value
    must yield ONE alias -- a new name per spawn is a name kiro-cli has never
    loaded and a file the directory keeps."""
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(json.dumps(_launcher_spec(str(uuid.uuid4()))), encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    nonce = str(uuid.uuid4())
    source.write_text(json.dumps(_launcher_spec(nonce)), encoding="utf-8")
    second = projection.prepare_native_skill_projection(project)

    assert second.agent("custom") == first.agent("custom")
    assert _aliases_on_disk(agents) == [f"{first.agent('custom')}.json"]
    # The file still carries the value as authored now: only the NAME ignores it.
    view = json.loads(_alias_file(agents, second).read_text(encoding="utf-8"))
    assert view["mcpServers"]["broker"]["env"]["AIM_CREDS_AGENT_INJECTION"] == nonce


def test_an_added_env_variable_is_still_a_different_view(native_tree):
    """Only env VALUES leave the identity; which variables a server gets is part
    of what the agent is."""
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(json.dumps(_launcher_spec("n1")), encoding="utf-8")
    first = projection.prepare_native_skill_projection(project).agent("custom")
    source.write_text(json.dumps(_launcher_spec("n1", {"EXTRA": "1"})), encoding="utf-8")
    second = projection.prepare_native_skill_projection(project).agent("custom")

    assert first != second


def test_a_reserialized_alias_is_current_and_is_not_rewritten(native_tree):
    """The launcher re-serializes every alias too. That changes no part of
    the spec, so the next preparation must keep the file (and its sidecar)
    rather than rewrite every alias under the shared lock."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(json.dumps(_launcher_spec("n1")), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias, meta = _alias_file(agents, prepared), _metadata_file(agents, prepared)
    _launcher_reserialize(alias)
    before = (alias.stat().st_ino, meta.stat().st_ino, alias.read_bytes())

    again = projection.prepare_native_skill_projection(project)

    assert again.agent("custom") == prepared.agent("custom")
    assert (alias.stat().st_ino, meta.stat().st_ino, alias.read_bytes()) == before


def test_a_changed_env_value_is_still_republished(native_tree):
    """The currency check ignores serialization, never values: an operator who
    changes an ``env`` value must see kiro-cli given the new one."""
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(json.dumps(_launcher_spec("n1")), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, prepared)
    _launcher_reserialize(alias)
    source.write_text(json.dumps(_launcher_spec("n2")), encoding="utf-8")

    projection.prepare_native_skill_projection(project)

    env = json.loads(alias.read_text(encoding="utf-8"))["mcpServers"]["broker"]["env"]
    assert env["AIM_CREDS_AGENT_INJECTION"] == "n2"


def test_a_relaunched_alias_stays_owned_and_is_reclaimed_as_a_pair(native_tree, monkeypatch):
    """The sidecar binds the alias's identity, not only its bytes: an alias the
    launcher re-serialized AND re-stamped is still this home's, so the recorded
    prune removes alias and sidecar together, with no legacy age wait."""
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(json.dumps(_launcher_spec("n1", {"V": "1"})), encoding="utf-8")
    old = projection.prepare_native_skill_projection(project)
    alias, meta = _alias_file(agents, old), _metadata_file(agents, old)
    _launcher_reserialize(alias, nonce=str(uuid.uuid4()))
    assert projection._managed_metadata_for_alias(agents, alias, alias.read_bytes()) is not None
    del old
    _wait_for_dropped_projection_ends()
    _hold_the_prune_clock(monkeypatch)
    source.write_text(json.dumps(_launcher_spec("n1", {"V": "1", "W": "2"})), encoding="utf-8")

    projection.prepare_native_skill_projection(project)

    assert not alias.exists() and not meta.exists()


def _publish_pre_view_digest(agents, name):
    """The pair as a build before this change wrote it: the sidecar binds the
    alias BYTES only, the shape the launcher incident left on disk."""
    alias = agents / f"{name}.json"
    raw = json.dumps(
        {"name": name, "resources": [], "mcpServers": {"kirocrew-core": {"command": "x"}}}
    )
    alias.write_text(raw, encoding="utf-8")
    md = projection._projection_metadata_dir()
    md.mkdir(parents=True, exist_ok=True)
    sidecar = md / f"{name}.json"
    sidecar.write_text(
        json.dumps(
            {
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: _crew_home_id(),
                projection._MANAGED_ALIAS_SHA256: hashlib.sha256(raw.encode()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return alias, sidecar


def _backdate(path, secs=3600.0):
    old = time.time() - secs
    os.utime(path, (old, old))


def test_a_reserialized_byte_bound_pair_is_removed_together(native_tree):
    """Re-serializing an alias whose sidecar binds only its bytes sends the prune
    down the moved-bytes path, which removes the alias and the sidecar together
    instead of stranding the sidecar."""
    _home, agents, _project = native_tree
    control_alias, control_sidecar = _publish_pre_view_digest(
        agents, projection.NATIVE_SKILL_ALIAS_PREFIX + "a" * 24
    )
    _backdate(control_alias)
    assert projection._reclaim_prune_candidate(
        agents,
        control_alias,
        _crew_home_id(),
        projection._ViewLedgerWrites(agents, _crew_home_id()),
    )
    assert not control_alias.exists() and not control_sidecar.exists()

    alias, sidecar = _publish_pre_view_digest(
        agents, projection.NATIVE_SKILL_ALIAS_PREFIX + "b" * 24
    )
    alias.write_text(
        json.dumps(json.loads(alias.read_text()), indent=2).replace('": ', '" : '),
        encoding="utf-8",
    )
    _backdate(alias)

    assert projection._reclaim_prune_candidate(
        agents, alias, _crew_home_id(), projection._ViewLedgerWrites(agents, _crew_home_id())
    )
    assert not alias.exists()
    assert not sidecar.exists(), "the ownership sidecar was stranded"


def test_a_reserialized_alias_whose_sidecar_names_another_home_is_kept(native_tree):
    """The pair removal is ownership-checked: another data home's record is that
    home's to retire, bytes moved or not."""
    _home, agents, _project = native_tree
    alias, sidecar = _publish_pre_view_digest(
        agents, projection.NATIVE_SKILL_ALIAS_PREFIX + "c" * 24
    )
    record = json.loads(sidecar.read_text())
    record[projection._MANAGED_CREW_HOME] = "/some/other/crew-home"
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    alias.write_text(json.dumps(json.loads(alias.read_text()), indent=2), encoding="utf-8")
    _backdate(alias)

    assert not projection._reclaim_prune_candidate(
        agents, alias, _crew_home_id(), projection._ViewLedgerWrites(agents, _crew_home_id())
    )
    assert alias.exists() and sidecar.exists()


def _orphan_sidecar(agents, digest, home=None):
    stem = projection.NATIVE_SKILL_ALIAS_PREFIX + digest
    md = projection._projection_metadata_dir()
    md.mkdir(parents=True, exist_ok=True)
    path = md / f"{stem}.json"
    path.write_text(
        json.dumps(
            {
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: home or _crew_home_id(),
                projection._MANAGED_ALIAS_SHA256: "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    return stem, path


def test_the_prune_sweeps_this_homes_sidecars_whose_alias_is_gone(native_tree, monkeypatch):
    """A backlog of sidecars with no alias drains:
    an orphan of THIS home goes; another home's orphan, a sidecar whose alias is
    still there, and a file that is not a sidecar stay."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    _hold_the_prune_clock(monkeypatch)
    _stem, ours = _orphan_sidecar(agents, "1" * 24)
    _stem, foreign = _orphan_sidecar(agents, "2" * 24, home="/some/other/crew-home")
    kept_stem, kept = _orphan_sidecar(agents, "3" * 24)
    (agents / f"{kept_stem}.json").write_text('{"name":"operator-owned"}', encoding="utf-8")
    md = projection._projection_metadata_dir()
    stray = md / "notes.json"
    stray.write_text("{}", encoding="utf-8")

    live = projection.prepare_native_skill_projection(project)

    assert not ours.exists()
    assert foreign.exists() and kept.exists() and stray.exists()
    assert _metadata_file(agents, live).exists(), "the live pair lost its record"


def test_the_boot_drain_retires_an_orphaned_sidecar_backlog_in_bounded_batches(
    native_tree, monkeypatch
):
    """Sidecar-only batches count as progress, so the drain does not stop after
    the first per-batch cap while orphans remain; the return value stays the
    alias count it always was."""
    _home, agents, _project = native_tree
    monkeypatch.setattr(projection, "_SIDECAR_SWEEP_MAX_PER_RUN", 3)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    _hold_the_prune_clock(monkeypatch)
    orphans = [_orphan_sidecar(agents, f"{n:024x}")[1] for n in range(10)]

    assert projection.drain_stale_aliases() == 0

    assert not any(p.exists() for p in orphans)


def test_an_alias_publication_is_announced_by_a_same_bytes_in_place_write(native_tree, monkeypatch):
    """kiro-cli reloads its agents directory on a data write and never on the
    rename ``atomic_write`` publishes with, so the publication must end
    in a write to the published inode that leaves its bytes unchanged."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    seen = []
    real_announce = projection._announce_publication

    def observed(path, data):
        backdated = path.stat().st_mtime_ns - 10**9
        os.utime(path, ns=(backdated, backdated))
        inode = path.stat().st_ino
        real_announce(path, data)
        after = path.stat()
        seen.append(
            (
                path.name,
                after.st_ino == inode,
                path.read_bytes() == data,
                after.st_mtime_ns > backdated,
            )
        )

    monkeypatch.setattr(projection, "_announce_publication", observed)
    prepared = projection.prepare_native_skill_projection(project)

    assert seen == [(f"{prepared.agent('custom')}.json", True, True, True)]


def test_the_announcement_never_writes_bytes_the_file_does_not_already_hold(tmp_path):
    """Racing a foreign rewrite, the announcement must leave the file alone: it
    only ever rewrites bytes identical to the ones already there."""
    path = tmp_path / "alias.json"
    path.write_bytes(b'{"name":"theirs"}')
    old = time.time() - 3600
    os.utime(path, (old, old))

    projection._announce_publication(path, b'{"name":"mine!!"}')

    assert path.read_bytes() == b'{"name":"theirs"}'
    assert abs(path.stat().st_mtime - old) < 1


def _alias_lock(agents, digest, *, age_secs=3600.0, content=b""):
    path = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{digest}.lock"
    path.write_bytes(content)
    if age_secs:
        old = time.time() - age_secs
        os.utime(path, (old, old))
    return path


def test_leftover_alias_lock_files_are_swept_only_when_provably_residue(native_tree, monkeypatch):
    """An external rewriter leaves an empty ``<alias>.lock`` beside every spec it
    locks (1,661 beside 23 aliases on one host). One whose alias is gone and that
    is older than the legacy age is residue; a fresh one, one beside a live
    alias, a non-empty one, and any other ``.lock`` stay."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    _hold_the_prune_clock(monkeypatch)
    residue = _alias_lock(agents, "4" * 24)
    fresh = _alias_lock(agents, "5" * 24, age_secs=0)
    non_empty = _alias_lock(agents, "6" * 24, content=b"pid 1")
    beside_alias = _alias_lock(agents, "7" * 24)
    (agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{'7' * 24}.json").write_text(
        '{"name":"x"}', encoding="utf-8"
    )
    authored_lock = agents / "custom.lock"
    authored_lock.write_bytes(b"")
    old = time.time() - 3600
    os.utime(authored_lock, (old, old))

    projection.prepare_native_skill_projection(project)

    assert not residue.exists()
    assert fresh.exists() and non_empty.exists() and beside_alias.exists()
    assert authored_lock.exists()


def test_the_residue_census_counts_orphans_locks_and_external_rewrites(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert projection.census_projection_residue(agents) == {
        "sidecars": 1,
        "orphan_sidecars": 0,
        "alias_locks": 0,
        "rewritten": 0,
        "truncated": 0,
    }
    _orphan_sidecar(agents, "8" * 24)
    _alias_lock(agents, "9" * 24)
    # Not residue by the sweeper's rule: too young, not empty, or beside an alias.
    _alias_lock(agents, "a" * 24, age_secs=0)
    _alias_lock(agents, "b" * 24, content=b"held")
    (agents / f"{prepared.agent('custom')}.lock").write_bytes(b"")
    alias = _alias_file(agents, prepared)
    alias.write_text(json.dumps(json.loads(alias.read_text()), indent=2), encoding="utf-8")

    assert projection.census_projection_residue(agents) == {
        "sidecars": 2,
        "orphan_sidecars": 1,
        "alias_locks": 1,
        "rewritten": 1,
        "truncated": 0,
    }


# ── display-only text kiro-cli copies into every session reply ──


def test_an_alias_view_leaves_out_the_welcome_message_and_keeps_a_short_description(
    native_tree, caplog
):
    """kiro-cli ships every agent's description and welcomeMessage in each
    session/new reply; an alias is a second copy of an agent the host already
    lists, so it must not double that text. The authored spec keeps it (Kiro Crew
    renders the welcomeMessage from there), and the source is named in a warning."""
    _home, agents, project = native_tree
    source = agents / "custom.json"
    welcome = "release notes\n" * 9000  # ~126 KB, the shape of an agent package's notes
    spec = {
        "name": "custom",
        "tools": ["read"],
        "description": "d" * 5000,
        "welcomeMessage": welcome,
    }
    source.write_text(json.dumps(spec), encoding="utf-8")
    projection._DISPLAY_TEXT_WARNED.clear()

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        prepared = projection.prepare_native_skill_projection(project)

    view = json.loads(_alias_file(agents, prepared).read_text(encoding="utf-8"))
    assert "welcomeMessage" not in view
    # The authored text is not copied at all: the view carries the short
    # managed description instead, well under the bound the strip enforces.
    assert "d" * 100 not in view["description"]
    assert len(view["description"]) < projection._ALIAS_DESCRIPTION_MAX_CHARS
    assert json.loads(source.read_text(encoding="utf-8"))["welcomeMessage"] == welcome
    warnings = [r.getMessage() for r in caplog.records if "welcomeMessage" in r.getMessage()]
    assert warnings and "'custom'" in warnings[0] and source.as_posix() in warnings[0]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        projection.prepare_native_skill_projection(project)
    assert not [r for r in caplog.records if "welcomeMessage" in r.getMessage()]


def test_a_view_describes_itself_as_managed_and_names_the_agent_to_pick(native_tree):
    """The Kiro IDE lists every file in the agents directory as an agent, with
    its description. A view's description is the one place it can say what it is
    and which agent the user should pick instead; the authored description is
    not copied (the authored agent is listed beside it with its own)."""
    _home, agents, project = native_tree
    spec = {"name": "custom", "tools": ["read"], "description": "Reviews pull requests."}
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads(_alias_file(agents, prepared).read_text(encoding="utf-8"))
    description = view["description"]
    assert description.startswith(projection._MANAGED_VIEW_DESCRIPTION_PREFIX + "custom [")
    assert "Use custom instead." in description
    assert "Reviews pull requests." not in description
    assert projection._view_home_tag(view) == projection._home_tag(_crew_home_id())
    # An edit to the authored description alone changes nothing kiro-cli runs,
    # so it mints no new view.
    spec["description"] = "Reviews pull requests carefully."
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    assert projection.prepare_native_skill_projection(project).agent("custom") == prepared.agent(
        "custom"
    )


@pytest.mark.parametrize("length", [1, 100, 10_000])
def test_a_views_managed_description_keeps_the_alias_bound_for_any_agent_name(length):
    """The managed description replaces the authored one AFTER the alias bound
    was applied, so it must keep that bound itself: a spec on disk may declare a
    name of any length. The name is shortened, the home tag the prune reads is
    not, and the description still parses as a tagged managed view."""
    name = "a" * length
    home_id = "/some/home"
    description = projection._managed_view_description(name, home_id)
    assert len(description) <= projection._ALIAS_DESCRIPTION_MAX_CHARS
    assert description.startswith(projection._MANAGED_VIEW_DESCRIPTION_PREFIX + "a")
    assert description.endswith(" instead.")
    assert projection._view_home_tag({"description": description}) == projection._home_tag(home_id)
    if length <= 100:
        assert f" of {name} [" in description and f"Use {name} instead." in description
    else:
        assert "\u2026 [" in description and name not in description


def test_a_view_tagged_by_another_home_is_never_reclaimed_as_a_legacy_view(
    native_tree, monkeypatch, tmp_path
):
    """Two homes share one agents directory when they share ``~/.kiro``. Each
    keeps its ownership records under its OWN data home, so the other home's
    view has no sidecar this home can see -- exactly what a view from a build
    before any sidecar looks like. The home tag in the view's description keeps
    the legacy reclaim off it; a view tagged by THIS home with its record lost
    still drains through that path."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-b")
    theirs = projection.prepare_native_skill_projection(project)
    their_alias = _alias_file(agents, theirs)
    theirs.release()
    assert their_alias.exists()
    # Their record is under their data home: take it away so this home cannot
    # even find a legacy-dir record for it, then age the view past the floor.
    shutil.rmtree(tmp_path / "crew-b")
    _backdate(their_alias)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew")
    mine = projection.prepare_native_skill_projection(project)
    my_alias = _alias_file(agents, mine)
    assert their_alias.exists(), "another home's live view was reclaimed as a legacy one"
    assert my_alias.exists() and my_alias != their_alias

    # The same view, tagged by THIS home with its sidecar gone, is this home's
    # to reclaim: every legacy gate still applies (aged, unleased, inactive).
    mine.release()
    _metadata_file(agents, mine).unlink()
    _backdate(my_alias)
    with projection._projection_alias_lock(agents):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    assert not my_alias.exists()
    assert their_alias.exists()


def test_the_display_budget_names_the_readers_frame_limit():
    from kiro_crew.acp import runtime

    assert projection._STDOUT_FRAME_LIMIT_BYTES == runtime._STDOUT_BUFFER_LIMIT


def test_the_display_text_warning_escapes_what_it_read_from_disk(caplog, monkeypatch):
    """Agent names and paths come from files another program wrote; a newline in
    one must not forge a second log record, and a huge one stays bounded."""
    projection._DISPLAY_TEXT_WARNED.clear()
    name = "evil\nWARNING forged record" + "x" * 1000
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        projection._warn_on_display_text({name: (64 * 1024, "/p/evil\n.json")})
    messages = [r.getMessage() for r in caplog.records]
    assert messages and all("\n" not in m for m in messages)
    assert all(len(m) < 2000 for m in messages)


def test_the_display_text_warning_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(projection, "_DISPLAY_TEXT_WARNED_MAX", 4)
    projection._DISPLAY_TEXT_WARNED.clear()
    for n in range(20):
        projection._warn_on_display_text({f"a{n}": (64 * 1024 + n, f"/p/a{n}.json")})
    assert len(projection._DISPLAY_TEXT_WARNED) <= 4


def test_the_display_text_warning_cap_holds_per_insertion(caplog, monkeypatch):
    """A single batch larger than the bound (257 large specs against 256) must not
    insert past it; the rest are counted in one line instead of named."""
    projection._DISPLAY_TEXT_WARNED.clear()
    sizes = {f"a{n}": (64 * 1024, f"/p/a{n}.json") for n in range(257)}
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        projection._warn_on_display_text(sizes)
    assert len(projection._DISPLAY_TEXT_WARNED) == projection._DISPLAY_TEXT_WARNED_MAX
    assert any("1 more agent(s)" in r.getMessage() for r in caplog.records)
    projection._DISPLAY_TEXT_WARNED.clear()


def _credential_spec(token, nonce):
    return {
        "name": "custom",
        "tools": ["@broker"],
        "mcpServers": {
            "broker": {
                "command": "broker",
                "env": {"TOKEN": token, "AIM_CREDS_AGENT_INJECTION": nonce},
            }
        },
    }


def test_a_rotated_credential_names_a_new_alias_but_a_launch_nonce_does_not(
    native_tree, monkeypatch
):
    """A rotated credential must name an alias kiro-cli has not loaded -- the old
    name would activate its loaded copy, old credential and all -- and two launch
    contexts never share one; only the volatile nonce changing reuses the alias."""
    monkeypatch.delenv("KIROCREW_SKILL_VIEW_VOLATILE_ENV", raising=False)
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(json.dumps(_credential_spec("t1", "n1")), encoding="utf-8")
    first = projection.prepare_native_skill_projection(project).agent("custom")
    source.write_text(json.dumps(_credential_spec("t1", "n2")), encoding="utf-8")
    assert projection.prepare_native_skill_projection(project).agent("custom") == first
    source.write_text(json.dumps(_credential_spec("t2", "n2")), encoding="utf-8")
    assert projection.prepare_native_skill_projection(project).agent("custom") != first


def test_recognise_does_not_carry_the_launch_name_exemption():
    """recognise()'s only callers are the SHARED RUNTIME, whose own contract is
    that it must NEVER set spawn_agent_name (it activates the launch agent through
    _activate_mode_bracketed instead; setting the field would make request()
    tolerate the launch agent on a mid-session switch too, reactivating a cached
    unprojected spec). So a refresh through recognise() must NOT propagate the
    earlier projection's spawn_agent_name onto the fresh one -- only the direct
    spawn caller sets it."""
    earlier = projection.NativeSkillProjection({}, spawn_agent_name="kirocrew")
    fresh = projection.NativeSkillProjection({})
    assert fresh.spawn_agent_name == ""
    fresh.recognise(earlier)
    assert fresh.spawn_agent_name == ""

    # A projection that already knows its own launch name is likewise untouched by
    # recognise() -- the field is owned by the spawn caller, not this seam.
    already = projection.NativeSkillProjection({}, spawn_agent_name="kirocrew")
    already.recognise(projection.NativeSkillProjection({}, spawn_agent_name="other"))
    assert already.spawn_agent_name == "kirocrew"


def test_recognise_carries_advertised_launch_name_but_not_the_request_exemption():
    """A projection refresh runs frame() on the fresh object, so the launch
    identity's ADVERTISING must survive recognise() -- otherwise a no-view launch
    agent's mode is dropped from availableModes after the refresh. This is carried
    (advertising only), unlike ``spawn_agent_name`` (the mid-session request
    tolerance), which recognise() must never propagate."""
    earlier = projection.NativeSkillProjection(
        {}, spawn_agent_name="kirocrew", advertised_launch_name="kirocrew"
    )
    fresh = projection.NativeSkillProjection({})
    fresh.recognise(earlier)
    # Advertising carried; the request exemption NOT carried.
    assert fresh.advertised_launch_name == "kirocrew"
    assert fresh.spawn_agent_name == ""
    # A fresh projection that already recorded its own launch name is untouched.
    already = projection.NativeSkillProjection({}, advertised_launch_name="own")
    already.recognise(projection.NativeSkillProjection({}, advertised_launch_name="other"))
    assert already.advertised_launch_name == "own"


def test_recognise_admits_only_alias_names_and_registered_agent_names():
    earlier = projection.NativeSkillProjection({})
    earlier._recognised.update(
        {
            projection.NATIVE_SKILL_ALIAS_PREFIX + "a" * 24: "ops",
            projection.NATIVE_SKILL_ALIAS_PREFIX + "b" * 24: "evil\x1b]0;x\x07" + "y" * 5000,
            "not-an-alias": "ops",
        }
    )
    current = projection.NativeSkillProjection({})
    current.recognise(earlier)
    assert current._recognised == {projection.NATIVE_SKILL_ALIAS_PREFIX + "a" * 24: "ops"}


def test_the_census_leaves_an_alias_too_large_to_hash_whole_unjudged(native_tree, monkeypatch):
    """A partial read's digest proves nothing: an unchanged alias past the read
    bound must not be reported as rewritten by another program."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    monkeypatch.setattr(projection, "_CENSUS_ALIAS_MAX_BYTES", 4)
    assert projection.census_projection_residue(agents)["rewritten"] == 0


def test_the_churning_census_is_bounded_at_insertion():
    """A spec with an enormous env map, or enormous key names, costs a bounded
    amount: labels are cut, and a group keeps a fixed number of them."""
    churning: dict = {}
    view = {"mcpServers": {"s": {"env": {f"{n:04d}" + "K" * 5000: str(n) for n in range(500)}}}}
    projection._note_env_values(churning, ("ops", "/a.json"), view)
    labels = churning[("ops", "/a.json")]
    assert len(labels) == projection._CHURNING_KEYS_PER_GROUP_MAX
    assert all(len(label) <= projection._CHURNING_LABEL_MAX_CHARS for label in labels)
    for n in range(projection._CHURNING_GROUPS_MAX + 10):
        projection._note_env_values(churning, (f"a{n}", "/x.json"), view)
    assert len(churning) == projection._CHURNING_GROUPS_MAX


def test_a_lock_file_a_launcher_still_holds_is_never_swept(native_tree, tmp_path):
    """An old, empty ``<stem>.lock`` with no alias beside it is residue only while
    nobody holds it: a launcher holding it would otherwise lose its lock inode to
    the sweep, and the next launcher's rewrites would race its own."""
    _home, agents, _project = native_tree
    held = _alias_lock(agents, "7" * 24)
    free = _alias_lock(agents, "8" * 24)
    fd = os.open(held, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        assert projection.platform_compat.try_acquire_lock(fd, exclusive=True)
        removed = projection._sweep_orphan_alias_locks(agents, deadline=time.monotonic() + 30)
    finally:
        os.close(fd)
    assert held.exists() and not free.exists()
    assert removed == 1


def test_the_churning_census_bounds_the_group_key_it_retains(native_tree):
    """The group key comes from a sidecar another program may have written; its
    agent and source strings are cut like the labels are."""
    _home, agents, project = native_tree
    spec = {"name": "custom", "mcpServers": {"s": {"command": "s", "env": {"K": "v"}}}}
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    sidecar = projection._projection_metadata_dir() / f"{prepared.agent('custom')}.json"
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record[projection._MANAGED_AGENT] = "x" * 5000
    record[projection._MANAGED_SOURCE] = "/" + "y" * 5000
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    churning: dict = {}
    projection.census_projection_residue(agents, churning)
    assert churning
    assert all(
        len(part) <= projection._CHURNING_LABEL_MAX_CHARS for key in churning for part in key
    )


# ── side files live under the data home, not in the agents directory ──


def test_a_preparation_writes_only_views_into_the_agents_directory(native_tree):
    """The Kiro IDE lists every entry of ``~/.kiro/agents/`` as an agent. The
    lock, the lease records and the ownership sidecars are Kiro Crew's own and
    belong under its data home; only the views kiro-cli resolves by name may
    land beside the authored specs."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = prepared.agent("custom")

    assert sorted(p.name for p in agents.iterdir()) == sorted(["custom.json", f"{alias}.json"])
    side = projection._projection_side_dir()
    assert side == projection.data_home() / "skill-projection"
    assert (side / "publication.lock").is_file()
    assert (side / "metadata" / f"{alias}.json").is_file()
    assert (side / "metadata" / projection._VIEW_LEDGER_NAME).is_file()
    records = list((side / "leases").glob("*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text())["aliases"] == [alias]


def test_the_view_ceiling_is_sized_for_the_ide_drop_down():
    """Every view is a row the Kiro IDE shows. The ceiling is a backstop above
    the measured steady state of a heavily used host (35 views for 34 authored
    agents), not a startup-cost net in the thousands."""
    assert projection.SKILL_VIEW_PROJECTION_CEILING == 128


# ── a projection that ends prunes its own views at once ──


def test_releasing_a_projection_prunes_this_homes_unleased_views_at_once(native_tree, monkeypatch):
    """Without this a session's views wait for the NEXT spawn's prune, and on a
    host that is winding down they sit in the Kiro IDE's agent list indefinitely.
    ``release()`` drops the lease and prunes right away, under the same lock and
    gates as the per-spawn prune."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, prepared)
    sidecar = _metadata_file(agents, prepared)
    assert alias.exists() and sidecar.exists()

    prepared.release()

    assert not alias.exists(), "the released view waited for the next spawn's prune"
    assert not sidecar.exists()
    assert not list(projection._projection_lease_dir().iterdir()), "the lease was not released"
    assert projection.source_agent_name(alias.stem) == "custom", "the ledger lost the view"
    # Idempotent, and the projection still translates what it published.
    prepared.release()
    assert prepared.agent("custom") == alias.stem


def test_a_dropped_projection_prunes_too_and_spares_what_another_projection_holds(
    native_tree, monkeypatch, tmp_path
):
    """The finalizer path: a projection that is simply dropped prunes as
    ``release()`` does, and neither touches a view a live projection in this
    process still holds."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","prompt":"v1"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    live_alias = _alias_file(agents, live)
    source.write_text('{"name":"custom","prompt":"v2"}', encoding="utf-8")
    ended = projection.prepare_native_skill_projection(tmp_path / "other-run")
    ended_alias = _alias_file(agents, ended)
    assert live_alias != ended_alias and live_alias.exists() and ended_alias.exists()

    del ended
    gc.collect()
    # The drop's end is handed to the reaper thread (never run inline on the
    # dropping thread, which may hold a non-reentrant lock -- see
    # ``_end_projection``). Wait for the reaper to reach it before asserting.
    _reached = threading.Event()
    projection._COLLECTED_ENDS.put(_reached.set)
    assert _reached.wait(10), "the reaper never reached the dropped projection's end"

    assert not ended_alias.exists(), "a dropped projection's view waited for the next spawn"
    assert live_alias.exists(), "a view a live projection holds was pruned"
    assert live.agent("custom") == live_alias.stem


def test_a_projection_the_cyclic_collector_frees_ends_on_the_reaper_not_the_collector(
    native_tree, monkeypatch
):
    """A cyclic collection runs finalizers on whichever thread allocated at that
    moment, inside whatever that thread holds -- in the Windows hang, a stdlib
    executor's ``submit`` lock. The end takes locks and calls back into that same
    executor, so run there it waits on a lock its own thread holds, forever. A
    lock this test holds across ``gc.collect()`` stands in for that one: the end
    must run on the reaper thread, which gets it once the collection returns."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, prepared)
    lease_dir = projection._projection_lease_dir()
    held_by_collector = threading.Lock()
    ran: list[tuple[bool, bool]] = []
    done = threading.Event()
    real_end = projection._end_projection_off_loop

    def end(*args):
        on_collector = threading.get_ident() == collector
        # Bounded so a regression fails here instead of hanging the worker.
        got = held_by_collector.acquire(timeout=5)
        try:
            ran.append((on_collector, got))
            real_end(*args)
        finally:
            if got:
                held_by_collector.release()
            done.set()

    monkeypatch.setattr(projection, "_end_projection_off_loop", end)
    collector = threading.get_ident()
    # Only the cyclic collector can free it: a list that holds itself and it.
    cycle: list[object] = [prepared]
    cycle.append(cycle)
    del prepared
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        del cycle
        with held_by_collector:
            gc.collect()
    finally:
        if was_enabled:
            gc.enable()

    assert done.wait(10), "the collected projection was never ended"
    assert ran == [(False, True)], "the end ran on the collecting thread, under its lock"
    assert not list(lease_dir.iterdir()), "the lease was not released"
    assert not alias.exists(), "the collected projection's view was not pruned"


def test_a_projection_dropped_off_the_loop_ends_on_the_reaper_not_the_dropping_thread(
    native_tree, monkeypatch
):
    """A refcount-zero drop runs the finalizer on the dropping thread. In the
    Windows hang that thread was inside a ``ThreadPoolExecutor.submit`` holding
    ``_global_shutdown_lock`` (constructing the ``Future`` whose allocation
    dropped this projection's last reference), and running the end there
    re-enters that same executor through the path-resolution pool, waiting on a
    lock its own thread holds until the test timeout kills the xdist worker. A
    lock this test holds across the drop stands in for that one: the end must NOT
    run on the dropping thread -- off the loop a finalizer is queued for the
    reaper, which holds none of those locks. This is the refcount companion to
    the cyclic-collector case above; one rule covers both."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    lease_dir = projection._projection_lease_dir()
    held_by_dropper = threading.Lock()
    ran: list[tuple[bool, bool]] = []
    done = threading.Event()
    real_end = projection._end_projection_off_loop

    def end(*args):
        on_dropper = threading.get_ident() == dropper["id"]
        # Bounded so a regression (the end running inline on the dropping thread,
        # which holds the lock) fails here instead of hanging the worker.
        got = held_by_dropper.acquire(timeout=5)
        try:
            ran.append((on_dropper, got))
            real_end(*args)
        finally:
            if got:
                held_by_dropper.release()
            done.set()

    monkeypatch.setattr(projection, "_end_projection_off_loop", end)
    dropper: dict[str, int] = {}
    alias_box: dict[str, Path] = {}

    def drop_under_lock() -> None:
        dropper["id"] = threading.get_ident()
        prepared = projection.prepare_native_skill_projection(project)
        assert prepared is not None
        alias_box["path"] = _alias_file(agents, prepared)
        was_enabled = gc.isenabled()
        gc.disable()  # force a pure refcount drop, never a cyclic collection
        try:
            # Hold a lock the end would deadlock on if it ran inline here, then
            # drop the last reference: the finalizer fires on THIS thread.
            with held_by_dropper:
                del prepared
        finally:
            if was_enabled:
                gc.enable()

    t = threading.Thread(target=drop_under_lock, name="dropper", daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the dropping thread hung -- the end ran inline under its lock"

    assert done.wait(10), "the dropped projection was never ended"
    # (False, True): the end ran on the reaper (not the dropper) and took the
    # lock freely, because the dropping thread had already released it.
    assert ran == [(False, True)], "the end ran inline on the dropping thread, under its lock"
    assert not list(lease_dir.iterdir()), "the lease was not released"
    assert not alias_box["path"].exists(), "the dropped projection's view was not pruned"


def test_an_off_loop_drop_releases_the_lease_inline_when_the_reaper_is_gone(
    native_tree, monkeypatch
):
    """The ``atexit`` weakref sweep can fire a finalizer for a projection the
    shared runtime never explicitly released, after the daemon reaper is already
    gone. A queued end would never run then, leaving the lock file behind to
    block the next process's view of this home. So off the loop with the reaper
    not alive the finalizer path releases the lease INLINE instead of queueing it
    -- the prune (which re-enters the path-resolution executor) is dropped, the
    next boot drain covers it, but the lease must not be lost. Forcing
    ``_reaper_alive`` False stands in for interpreter shutdown; the lease
    directory must be empty after the drop without anything landing on the
    reaper queue."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    monkeypatch.setattr(projection, "_reaper_alive", lambda: False)
    # A fresh queue no reaper drains: anything queued here stays queued, so an
    # empty queue after the drop proves the end did NOT take the reaper path.
    sink: "queue.SimpleQueue" = queue.SimpleQueue()
    monkeypatch.setattr(projection, "_COLLECTED_ENDS", sink)
    # No real reaper may start on the private queue: it would take this test's
    # items, then stay blocked on a queue nothing else ever feeds.
    monkeypatch.setattr(projection, "_ensure_projection_reaper", lambda: None)

    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    lease_dir = projection._projection_lease_dir()
    assert len(list(lease_dir.glob("*.json"))) == 1

    was_enabled = gc.isenabled()
    gc.disable()  # a pure refcount drop off the loop, never a cyclic collection
    try:
        del prepared
    finally:
        if was_enabled:
            gc.enable()

    assert sink.empty(), "the end was queued for a reaper that is gone instead of run inline"
    assert list(lease_dir.glob("*.json")) == [], "the lease was not released inline at exit"


def test_an_off_loop_drop_once_exit_began_releases_the_lease_inline_with_the_reaper_alive(
    native_tree, monkeypatch
):
    """While ``atexit`` handlers run the daemon reaper is still alive, yet
    finalization can stop it before it reaches a queued end. Once the exit hook
    has set ``_EXITING``, a dropped projection must release its lease inline even
    though ``_reaper_alive`` still answers True, and queue nothing."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    monkeypatch.setattr(projection, "_reaper_alive", lambda: True)
    exiting = threading.Event()
    exiting.set()
    monkeypatch.setattr(projection, "_EXITING", exiting)
    sink: "queue.SimpleQueue" = queue.SimpleQueue()
    monkeypatch.setattr(projection, "_COLLECTED_ENDS", sink)
    # No real reaper may start on the private queue: it would take this test's
    # items, then stay blocked on a queue nothing else ever feeds.
    monkeypatch.setattr(projection, "_ensure_projection_reaper", lambda: None)

    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    lease_dir = projection._projection_lease_dir()
    assert len(list(lease_dir.glob("*.json"))) == 1

    was_enabled = gc.isenabled()
    gc.disable()  # a pure refcount drop off the loop, never a cyclic collection
    try:
        del prepared
    finally:
        if was_enabled:
            gc.enable()

    assert sink.empty(), "the end was queued for a reaper that exit may stop first"
    assert list(lease_dir.glob("*.json")) == [], "the lease was not released inline at exit"


def test_the_exit_hook_releases_the_lease_of_an_end_still_queued(native_tree, monkeypatch):
    """An end queued before the exit began (the reaper alive but not yet at it)
    must not lose its lease when the process exits: the ``atexit`` hook takes it
    off the queue and releases its lease on the exiting thread, sets
    ``_EXITING``, and leaves a queued item that is not a projection end uncalled."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    monkeypatch.setattr(projection, "_reaper_alive", lambda: True)
    exiting = threading.Event()
    monkeypatch.setattr(projection, "_EXITING", exiting)
    sink: "queue.SimpleQueue" = queue.SimpleQueue()
    monkeypatch.setattr(projection, "_COLLECTED_ENDS", sink)
    # No real reaper may start on the private queue: it would take this test's
    # items, then stay blocked on a queue nothing else ever feeds.
    monkeypatch.setattr(projection, "_ensure_projection_reaper", lambda: None)
    foreign_calls: list[int] = []
    sink.put(lambda: foreign_calls.append(1))

    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    lease_dir = projection._projection_lease_dir()

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        del prepared
    finally:
        if was_enabled:
            gc.enable()
    assert not sink.empty(), "the drop did not queue its end for the reaper"
    assert len(list(lease_dir.glob("*.json"))) == 1, "the queued end released its lease early"

    # The reaper is gone by now in this stand-in, so the hook has nothing to wait for.
    monkeypatch.setattr(projection, "_reaper_alive", lambda: False)
    projection._release_queued_leases_at_exit()

    assert exiting.is_set(), "the exit hook did not mark the exit as begun"
    assert sink.empty(), "the exit hook left an end on the queue"
    assert list(lease_dir.glob("*.json")) == [], "the queued end's lease was not released at exit"
    assert foreign_calls == [], "the exit hook ran a queued item that is not a projection end"


def test_the_exit_hook_waits_for_the_end_the_reaper_already_took(monkeypatch):
    """With the reaper alive the hook hands it a marker and waits for it, so an
    end the reaper took off the queue just before the exit gets to release its
    lease. A stand-in reaper takes the marker but holds it until released; the
    hook must still be waiting while it is held, and return once it runs."""
    monkeypatch.setattr(projection, "_EXITING", threading.Event())
    sink: "queue.SimpleQueue" = queue.SimpleQueue()
    monkeypatch.setattr(projection, "_COLLECTED_ENDS", sink)
    # No real reaper may start on the private queue: it would take this test's
    # items, then stay blocked on a queue nothing else ever feeds.
    monkeypatch.setattr(projection, "_ensure_projection_reaper", lambda: None)
    monkeypatch.setattr(projection, "_reaper_alive", lambda: True)
    monkeypatch.setattr(projection, "_EXIT_REAPER_WAIT_SECS", 60.0)
    took_marker = threading.Event()
    let_it_run = threading.Event()

    def stand_in_reaper() -> None:
        end = sink.get(timeout=30)
        took_marker.set()
        let_it_run.wait(30)
        end()

    reaper = threading.Thread(target=stand_in_reaper, name="stand-in-reaper", daemon=True)
    reaper.start()
    hook_returned = threading.Event()

    def run_hook() -> None:
        projection._release_queued_leases_at_exit()
        hook_returned.set()

    hook = threading.Thread(target=run_hook, name="exit-hook", daemon=True)
    hook.start()
    try:
        assert took_marker.wait(10), "the exit hook queued no marker for the reaper"
        assert not hook_returned.wait(
            0.5
        ), "the exit hook returned before the reaper reached its marker"
    finally:
        let_it_run.set()
    assert hook_returned.wait(10), "the exit hook kept waiting after the reaper ran its marker"
    hook.join(10)
    reaper.join(10)


_EXIT_WITH_A_BUSY_REAPER = """
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import kiro_crew.agent as agent_module
from kiro_crew.acp import skill_projection as projection

root = Path(sys.argv[1])
home = root / "kiro"
agents = home / "agents"
projection.kiro_home = lambda: home
projection.data_home = lambda: root / "crew"
projection.kiro_agents_dir = lambda: agents
projection.platform_compat.path_volume_is_remote = lambda path: False
projection.platform_compat.first_linked_ancestor = lambda path: None
agent_module.managed_mcp_spec_entry = lambda name, **_kw: {"command": "test-core", "args": []}
agent_module._KIRO_MCP_JSON = home / "settings" / "mcp.json"
projection.list_agents = lambda **kw: [
    SimpleNamespace(name="custom", filename="custom.json", scope="global")
]

# Held until the interpreter exits, so only the atexit weakref sweep ends it.
HELD = projection.prepare_native_skill_projection(root / "project")
assert HELD is not None
assert projection._reaper_alive()
# The reaper stays alive but busy for the rest of the process, so an end
# queued at exit would never be reached.
projection._COLLECTED_ENDS.put(threading.Event().wait)
print(len(list(projection._projection_lease_dir().glob("*.json"))))
"""


def test_a_projection_held_until_exit_releases_its_lease_with_the_reaper_alive(
    native_tree, tmp_path
):
    """End to end, in a real interpreter: a projection still referenced when the
    process exits is ended by the ``atexit`` weakref sweep while the daemon
    reaper is alive but busy. Its lease must be released before the process
    ends; queued for the reaper, it never would be."""
    _home, agents, _project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_NATIVE_SKILL_PROJECTION"}
    result = subprocess.run(
        [sys.executable, "-c", _EXIT_WITH_A_BUSY_REAPER, str(tmp_path)],
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
        timeout=120,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, f"child failed: {result.stderr[-2000:]}"
    assert result.stdout.strip().splitlines()[-1] == "1", "the child held no lease to release"
    lease_dir = projection._projection_lease_dir()
    assert list(lease_dir.glob("*.json")) == [], "the lease outlived the exiting process"


def test_two_gateways_still_cannot_both_own_a_view(native_tree, monkeypatch):
    """The lease protocol, after the lease moved under the data home: while
    ANOTHER process of this home holds a lease naming a view, this process
    ending its own projection of the same view must not reclaim it -- neither
    eagerly nor on its next spawn -- and once that lease is released the view is
    reclaimable again. The other gateway is stood in for by a second lease
    record with its own held lock, which is all a reclaim can see of it."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    ours = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, ours)
    other_gateway = projection._acquire_projection_lease(agents, {alias.stem})
    assert len(list(projection._projection_lease_dir().glob("*.json"))) == 2
    _hold_the_prune_clock(monkeypatch)

    ours.release()
    assert alias.exists(), "a view another gateway's lease names was reclaimed on release"
    with projection._projection_alias_lock(agents):
        assert projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set()) == 0
    assert alias.exists(), "a view another gateway's lease names was reclaimed by a prune"
    assert len(list(projection._projection_lease_dir().glob("*.json"))) == 1

    other_gateway.close()
    with projection._projection_alias_lock(agents):
        assert projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set()) == 1
    assert not alias.exists()
    assert not list(projection._projection_lease_dir().iterdir())


def test_a_projection_ended_on_the_event_loop_releases_and_prunes_off_it(native_tree, monkeypatch):
    """``release()`` runs on the event-loop thread at session shutdown (and a
    finalizer can run there too, when the shared runtime drops an old
    projection). Nothing of the end runs on that thread: the lease release --
    unlock, close, two stats and two unlinks on a data home that may be slow --
    and the prune, which waits on a file lock, both go to one daemon thread, so
    no session the gateway serves is stalled behind this one's end."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, prepared)
    lease_dir = projection._projection_lease_dir()
    assert len(list(lease_dir.iterdir())) == 2
    ran_on: list[tuple[str, bool]] = []
    lease_present_at_prune: list[bool] = []
    done = threading.Event()
    gate = threading.Event()

    def recording_prune(directory, crew_home_id, *, force=False):
        current = threading.current_thread()
        ran_on.append((current.name, current.daemon))
        # The release ran before the prune, on this same thread.
        lease_present_at_prune.append(any(lease_dir.iterdir()))
        gate.wait(10)
        try:
            _REAL_EAGER_PRUNE(directory, crew_home_id, force=force)
        finally:
            done.set()

    monkeypatch.setattr(projection, "_eager_prune", recording_prune)
    monkeypatch.setattr(projection, "on_event_loop", lambda: True)

    prepared.release()

    # The caller returned while the thread holds at ``gate``: the only work it
    # did itself was to start that thread.
    assert not done.is_set(), "the end ran inline on the loop thread"
    gate.set()
    assert done.wait(10), "the prune never ran"
    assert ran_on == [("kirocrew-skill-view-prune", True)]
    assert lease_present_at_prune == [False], "the lease outlived its release"
    assert not list(lease_dir.iterdir())
    assert not alias.exists()


def test_a_projection_ended_on_the_event_loop_releases_inline_when_no_thread_can_start(
    native_tree, monkeypatch
):
    """Interpreter shutdown: a thread cannot start. The lease still goes -- the
    two unlinks the finalizer always did -- and only the prune is dropped, for
    the next spawn's prune to cover."""
    pruned: list[Path] = []
    monkeypatch.setattr(
        projection,
        "_eager_prune",
        lambda directory, _home_id, *, force=False: pruned.append(directory),
    )
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, prepared)

    class _NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(projection.threading, "Thread", _NoThread)
    monkeypatch.setattr(projection, "on_event_loop", lambda: True)

    prepared.release()

    assert not list(projection._projection_lease_dir().iterdir()), "the lease was not released"
    assert pruned == []
    assert alias.exists(), "a prune ran with no thread to run it on"


def test_the_per_spawn_eager_prune_skips_rather_than_queues_but_an_end_waits(
    native_tree, monkeypatch
):
    """Two different contracts on the in-process lock. A per-spawn eager prune
    must NOT wait the full in-process budget: a start that holds the lock runs
    its own prune, so a busy queue means the work is covered. A terminal END has
    no other party covering it, so it QUEUES for the lock rather than skipping --
    otherwise a second end that arrives while a first is walking would drop its
    own prune and leave its view on disk."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    waits: list[float | None] = []
    real_lock = projection._projection_alias_lock

    def recording_lock(directory, *, in_process_wait=None):
        waits.append(in_process_wait)
        return real_lock(directory, in_process_wait=in_process_wait)

    monkeypatch.setattr(projection, "_projection_alias_lock", recording_lock)

    # Per-spawn (force=False): asked for with no wait, so a busy queue is skipped.
    projection._eager_prune(agents, _crew_home_id(), force=False)
    assert waits == [0.0]

    # An end (force=True): asked for with the default in-process budget, so it
    # queues behind a concurrent end instead of dropping its prune.
    waits.clear()
    projection._eager_prune(agents, _crew_home_id(), force=True)
    assert waits == [None], "an end skipped the lock instead of queueing for it"


def test_ends_that_arrive_while_one_prunes_share_its_next_pass(native_tree, monkeypatch):
    """A subagent fan-out ends dozens of projections together, on the same
    in-process alias lock that session starts wait on. One forced prune per end
    would hold that lock long enough for the starts behind it to run out their
    wait and be refused. Ends that arrive while one end's prune runs return at
    once and are covered by ONE more pass, not one pass each."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, _project = native_tree
    calls: list[str] = []
    entered = threading.Event()
    gate = threading.Event()
    follow_up_gate = threading.Event()
    follow_up_done = threading.Event()

    def recording_prune(directory, crew_home_id, *, force=False):
        calls.append(threading.current_thread().name)
        if len(calls) == 1:
            entered.set()
            gate.wait(10)
        elif len(calls) == 2:
            follow_up_gate.wait(10)
        _REAL_EAGER_PRUNE(directory, crew_home_id, force=force)
        if len(calls) == 2:
            follow_up_done.set()

    monkeypatch.setattr(projection, "_eager_prune", recording_prune)
    side_dir = projection._projection_side_dir()
    first = threading.Thread(
        target=projection._end_projection_off_loop,
        args=(ExitStack(), side_dir, agents, _crew_home_id()),
        name="first-end",
    )
    first.start()
    assert entered.wait(10)

    late = [
        threading.Thread(
            target=projection._end_projection_off_loop,
            args=(ExitStack(), side_dir, agents, _crew_home_id()),
            name=f"late-end-{index}",
        )
        for index in range(10)
    ]
    for thread in late:
        thread.start()
    for thread in late:
        thread.join(5)
        assert not thread.is_alive(), "an end queued behind the running prune"
    gate.set()
    first.join(10)
    # The follow-up pass is still held: the end that ran the first pass was
    # not kept for the passes the later ends asked for.
    assert not first.is_alive(), "the first end was held for the follow-up pass"
    follow_up_gate.set()
    assert follow_up_done.wait(10), "the later ends got no follow-up pass"

    assert calls == ["first-end", "kirocrew-skill-view-prune"], "each end ran a prune pass"


def test_an_end_covered_by_a_running_prune_still_loses_its_view(native_tree, monkeypatch, tmp_path):
    """The coalescing must not drop an end: a view whose lease is released while
    another end's prune is already past its lease scan is reclaimed by the extra
    pass that prune makes, not left for the next spawn or boot."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","prompt":"v1"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    first_alias = _alias_file(agents, first)
    source.write_text('{"name":"custom","prompt":"v2"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(tmp_path / "other-run")
    second_alias = _alias_file(agents, second)
    assert first_alias != second_alias
    passes: list[bool] = []
    first_pass_done = threading.Event()
    second_pass_done = threading.Event()
    gate = threading.Event()

    def recording_prune(directory, crew_home_id, *, force=False):
        _REAL_EAGER_PRUNE(directory, crew_home_id, force=force)
        passes.append(second_alias.exists())
        if len(passes) == 1:
            first_pass_done.set()
            gate.wait(10)
        else:
            second_pass_done.set()

    monkeypatch.setattr(projection, "_eager_prune", recording_prune)
    ender = threading.Thread(target=first.release, name="first-end")
    ender.start()
    assert first_pass_done.wait(10)
    assert not first_alias.exists()
    assert second_alias.exists(), "the second view went before its projection ended"

    second.release()  # returns at once: the running end owes it one more pass
    gate.set()
    ender.join(10)
    assert not ender.is_alive()
    assert second_pass_done.wait(10), "the covered end got no follow-up pass"

    assert passes == [True, False]
    assert not second_alias.exists(), "an end covered by a running prune kept its view"
    assert not list(projection._projection_lease_dir().iterdir())


def test_a_projection_ending_later_prunes_the_home_it_published_into(
    native_tree, monkeypatch, tmp_path
):
    """A finalizer fires at an arbitrary later moment. It prunes the side directory
    its projection was prepared under, and never asks which data home is current."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, prepared)
    published_under = projection._projection_side_dir()

    def refuse_to_resolve():
        raise AssertionError("the ending projection asked which data home is current")

    monkeypatch.setattr(projection, "data_home", refuse_to_resolve)

    prepared.release()

    assert not alias.exists()
    assert not list((published_under / "leases").iterdir())


def test_the_census_counts_another_homes_untracked_view_as_foreign(native_tree, monkeypatch):
    """A view with no record here carries its publishing home's tag; the prune keeps
    it, so the census must not promise this gateway will reclaim it."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    mine = projection.prepare_native_skill_projection(project)
    mine_alias = _alias_file(agents, mine)
    monkeypatch.setattr(projection, "data_home", lambda: _home.parent / "crew-b")
    theirs = projection.prepare_native_skill_projection(project)
    their_alias = _alias_file(agents, theirs)
    # Their lease holder is an open file under their home; close it before the
    # home goes (Windows refuses to unlink an open file), as a gateway that has
    # exited would have. The view itself is fresh, so the release keeps it.
    theirs.release()
    shutil.rmtree(_home.parent / "crew-b")  # their records live under their home
    monkeypatch.setattr(projection, "data_home", lambda: _home.parent / "crew")
    assert mine_alias.exists() and their_alias.exists()

    counts = projection.census_projected_aliases(agents)

    assert counts["total"] == 2
    assert counts["foreign_leased"] + counts["foreign_home"] == 1


def test_a_burst_of_releases_prunes_every_home_despite_the_per_spawn_spacing(
    native_tree, monkeypatch, tmp_path
):
    """A gateway stop ends many sessions at once. The per-spawn spacing throttles
    the prune WALK so a burst of spawns does not pay for one walk each, but a
    release is a terminal event and must always reclaim: under the production
    spacing, two projections released back to back must leave neither view on
    disk, not just the first one's. Without the release path bypassing the
    spacing, the second release is skipped by ``_per_spawn_prune_due`` and its
    view lingers in the Kiro IDE's agent list until the next spawn or boot drain."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    # Restore the real spacing the autouse fixture zeroes, so the burst is real.
    monkeypatch.setattr(projection, "_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS", 5.0)
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})
    _home, agents, project = native_tree
    source = agents / "custom.json"
    # Two distinct views of this home: a content rewrite between the prepares
    # gives each projection its own alias, as two separate sessions would have.
    source.write_text('{"name":"custom","prompt":"v1"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    source.write_text('{"name":"custom","prompt":"v2"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(tmp_path / "second-run")
    first_alias = _alias_file(agents, first)
    second_alias = _alias_file(agents, second)
    assert first_alias != second_alias
    assert first_alias.exists() and second_alias.exists()

    # Both views are leased and on disk. Clear the per-spawn clock so the FIRST
    # release's prune is due -- the burst bug is that the SECOND release, within
    # the spacing window the first one just opened, is the one that gets skipped.
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})

    first.release()
    second.release()

    assert not first_alias.exists(), "the first release's view lingered"
    assert not second_alias.exists(), (
        "the second release in the burst was throttled by the per-spawn spacing "
        "and its view waited for the next spawn's prune"
    )
    assert not list(projection._projection_lease_dir().iterdir()), "a lease outlived its release"


def test_client_reset_state_releases_the_projection_and_prunes_its_view(native_tree, monkeypatch):
    """The client's shutdown reset is the explicit release at session end: without
    it a session's views wait for garbage collection or the next spawn's prune,
    and on a host winding down they sit in the Kiro IDE's agent list. Removing the
    ``projection.release()`` call from ``AcpClient`` shutdown reds this test."""
    from kiro_crew.acp import client as acp_client

    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    client = acp_client.AcpClient(agent="custom", work_dir=project)
    client._native_skill_projection = projection.prepare_native_skill_projection(
        project, per_session_element=False
    )
    alias = _alias_file(agents, client._native_skill_projection)
    assert alias.exists()
    assert list(projection._projection_lease_dir().iterdir()), "no lease was taken"
    # No live child: _reset_state's PID bookkeeping is not what this pins. Off the
    # event loop the release's prune runs inline, so its effect is observable here.
    client._process = None
    client._pid = None
    client._child_pids = {}

    client._reset_state()

    assert not alias.exists(), "the view outlived the session's shutdown reset"
    assert not list(
        projection._projection_lease_dir().iterdir()
    ), "the lease outlived the session's shutdown reset"


def test_two_concurrent_releases_each_prune_their_own_view(native_tree, monkeypatch, tmp_path):
    """A gateway stop ends sessions concurrently: client shutdown runs each
    ``release()`` on the event loop, which hands each end to its own
    ``kirocrew-skill-view-prune`` daemon thread. If the second end skipped its
    prune on the in-process lock the first still holds, its view would linger
    until the next spawn or boot drain -- the gateway-stop case the issue names.
    An end that arrives while the other's pass runs is covered by a follow-up
    pass on a third prune thread, so the test waits for every owed pass
    (``_settle_end_prunes``), not just the two release threads."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    monkeypatch.setattr(projection, "_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS", 5.0)
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})
    monkeypatch.setattr(projection, "on_event_loop", lambda: True)
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","prompt":"v1"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    source.write_text('{"name":"custom","prompt":"v2"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(tmp_path / "second-run")
    first_alias = _alias_file(agents, first)
    second_alias = _alias_file(agents, second)
    assert first_alias != second_alias
    assert first_alias.exists() and second_alias.exists()
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})

    # on_event_loop() is patched True, so each release spawns its own prune
    # thread; the threads race for the in-process lock.
    threads_before = set(threading.enumerate())
    first.release()
    second.release()
    _settle_end_prunes(agents, _new_prune_threads(threads_before))

    assert not first_alias.exists(), "the first release's view lingered"
    assert not second_alias.exists(), (
        "the second concurrent release skipped its prune on the busy in-process "
        "lock and its view lingered"
    )
    assert not list(projection._projection_lease_dir().iterdir()), "a lease outlived its release"


def _new_prune_threads(before):
    """The ``kirocrew-skill-view-prune`` threads started since *before* was taken."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "kirocrew-skill-view-prune" and thread not in before
    ]


def _settle_end_prunes(agents, release_threads, *, on_poll=lambda: None):
    """Wait until every prune the ends on *release_threads* owe has run.

    Joining the release threads is not enough. An end that arrives while another
    end's pass runs only records a request (``_coalesced_end_prune``), and the
    running end hands the follow-up pass to a NEW ``kirocrew-skill-view-prune``
    thread before it exits, so that pass can still be walking when the release
    threads are gone. The follow-up is owed for as long as this agents directory
    is in ``_END_PRUNE_RUNNING`` (a pending request always has a running key), so
    the wait ends when no end prune is running or requested for it. *on_poll* runs
    on each read of that state.
    """
    for thread in release_threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "an end's release thread never finished"
    target = agents.absolute().as_posix()

    def owed():
        with projection._END_PRUNE_GUARD:
            return sorted(
                key
                for key in projection._END_PRUNE_RUNNING | projection._END_PRUNE_REQUESTED
                if key[1] == target
            )

    def settled():
        on_poll()
        return not owed()

    wait_until(settled, describe=lambda: f"end prunes still owed: {owed()}")


@pytest.mark.parametrize("leader", ["first", "second"])
def test_a_release_handed_to_the_follow_up_pass_is_waited_for(
    native_tree, monkeypatch, tmp_path, leader
):
    """The order that leaves ``test_two_concurrent_releases_each_prune_their_own_view``
    a follow-up pass to wait for, forced. One end's pass walks while the other
    projection still holds its lease. The other end then arrives, records a
    request and returns, and the follow-up pass that removes its view runs on a
    third prune thread, held here until the settle wait is already polling.
    Joining only the two release threads would assert while that view is still
    on disk. ``leader`` picks which release takes the first pass, so either
    view can be the one the follow-up pass owes."""
    monkeypatch.setattr(projection, "_eager_prune", _REAL_EAGER_PRUNE)
    monkeypatch.setattr(projection, "_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS", 5.0)
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})
    monkeypatch.setattr(projection, "on_event_loop", lambda: True)
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","prompt":"v1"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    source.write_text('{"name":"custom","prompt":"v2"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(tmp_path / "second-run")
    first_alias = _alias_file(agents, first)
    second_alias = _alias_file(agents, second)
    assert first_alias != second_alias
    lead, follow = (first, second) if leader == "first" else (second, first)
    lead_alias, follow_alias = (
        (first_alias, second_alias) if leader == "first" else (second_alias, first_alias)
    )
    pass_threads: list[threading.Thread] = []
    lead_walked = threading.Event()
    follower_asked = threading.Event()
    settle_polled = threading.Event()

    def recording_prune(directory, crew_home_id, *, force=False):
        pass_threads.append(threading.current_thread())
        if len(pass_threads) == 2:
            settle_polled.wait(30)
        _REAL_EAGER_PRUNE(directory, crew_home_id, force=force)
        if len(pass_threads) == 1:
            lead_walked.set()
            follower_asked.wait(30)

    monkeypatch.setattr(projection, "_eager_prune", recording_prune)
    threads_before = set(threading.enumerate())
    try:
        lead.release()
        assert lead_walked.wait(30), "the leading end never ran its prune pass"
        assert not lead_alias.exists()
        assert follow_alias.exists(), "the following view went before its projection ended"

        follow.release()
        release_threads = _new_prune_threads(threads_before)
        for thread in release_threads:
            if thread is not pass_threads[0]:
                thread.join(timeout=30)  # it only records a request and returns
        follower_asked.set()
        _settle_end_prunes(agents, release_threads, on_poll=settle_polled.set)
    finally:
        follower_asked.set()
        settle_polled.set()

    assert len(pass_threads) == 2, "the two ends did not share one follow-up pass"
    assert pass_threads[1] not in release_threads, "the follow-up ran on a release thread"
    assert not follow_alias.exists(), "the view handed to the follow-up pass lingered"
    assert not lead_alias.exists()
    assert not list(projection._projection_lease_dir().iterdir()), "a lease outlived its release"
