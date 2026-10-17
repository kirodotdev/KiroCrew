"""Contract tests for Connections tool-name collision resolution: the emission pass.

Two exposed MCP providers that ship the same tool name leave one of the two
unreachable, because kiro-cli addresses a tool by bare name. The resolver's governing
decision table (EXPOSURE x IDENTITY x DECLARATION LIFECYCLE, see
:mod:`kiro_crew.connections.tool_aliases`) is covered row by row in
``test_connections_tool_aliases_decision_table.py``, and the persisted ownership record
of :mod:`kiro_crew.connections.alias_record` in ``test_connections_tool_aliases_record.py``.
This file drives the emission pass that writes the aliases: its own three invariants,
then the state table in that module's docstring, which names every crash boundary, row
by row -- an interrupted transaction is reclaimed rather than stranded, a lost spec
write rolls back to the claim that still matches the disk, and a record that matches
neither generation claims nothing. Row 9, the orphaned committed record, is pinned in
``test_connections_tool_aliases_record.py``. The mutation checks after it reinstate each
rejected shape rule and each rejected ordering and show it failing on the case that
killed it: the happy path passes under all of them, which is why three rounds of
shape-based fixes each looked correct. The builders and autouse fixtures the family
shares live in ``connections_tool_aliases_helpers.py``.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from connections_tool_aliases_helpers import _isolated_alias_record  # noqa: F401
from connections_tool_aliases_helpers import _pinned_kiro_cli_version  # noqa: F401
from connections_tool_aliases_helpers import (
    _L,
    _MAP_L,
    _MAP_LV,
    _V,
    URLS,
    _fail_nth_record_write,
    _gen,
    _raise_read_only,
    _rebuild_env,
)

from kiro_crew.connections import RegistryValidationError
from kiro_crew.connections.alias_record import (
    AliasGeneration,
    begin_transaction,
    commit_transaction,
    emitted_from_alias_map,
    load_claimed,
    record_path,
    spec_fingerprint,
    split_tool_ref,
)
from kiro_crew.connections.tool_aliases import derived_alias

# ── the emission pass ──


def _spec(*slugs: str, tools: list | None = None) -> dict:
    return {
        "mcpServers": {slug: {"url": URLS[slug]} for slug in slugs},
        "tools": [f"@{slug}" for slug in slugs] if tools is None else tools,
        "allowedTools": ["@linear"],
    }


def _apply(config: dict, *, enabled: bool = True, persist: bool = True) -> dict:
    """Run the pass the way ``rebuild_agent_config`` does, transaction included.

    The CALLER owns the transaction: it resolves the claim against the generation
    *config* currently carries (the in-memory analogue of the durable map the real
    caller re-reads under the lock), opens the transaction before the spec write,
    and commits after. ``persist=False`` models a hard kill in exactly that window,
    which is state-table row 4.
    """
    from kiro_crew import agent

    previous = _gen(config.get("toolAliases"), frozenset())
    previous_claim = load_claimed(previous.fingerprint)
    with patch.object(agent, "_connection_tool_aliases_enabled", return_value=enabled):
        generation = agent._apply_connection_tool_aliases(config, previous_claim)
    if generation is None:
        return config
    target = AliasGeneration(*generation)
    begin_transaction(AliasGeneration(previous.fingerprint, previous_claim), target)
    if persist:
        commit_transaction(target)
    return config


def _claimed(config: dict) -> frozenset:
    """What the record claims against the map *config* now carries.

    Every assertion about the record has to name a generation, because that is what
    resolution is: the same file answers differently for a different map, and a
    fingerprint from a stale snapshot is exactly what invariant 6 forbids.
    """
    return load_claimed(spec_fingerprint(config.get("toolAliases")))


# gate-off inertness


def test_flag_off_leaves_a_colliding_spec_byte_identical():
    baseline = json.dumps(_spec("linear", "vercel"), sort_keys=True)
    after = _apply(_spec("linear", "vercel"), enabled=False)
    assert json.dumps(after, sort_keys=True) == baseline
    assert "toolAliases" not in after


def test_flag_off_does_not_clear_an_existing_key():
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@linear/list_projects": "mine"}
    assert _apply(config, enabled=False)["toolAliases"] == {"@linear/list_projects": "mine"}


def test_flag_on_writes_the_resolved_map():
    after = _apply(_spec("linear", "vercel"))
    assert after["toolAliases"]["@vercel/list_projects"] == "vercel_list_projects"
    assert after["toolAliases"]["@linear/list_projects"] == "linear_list_projects"


def test_flag_on_without_a_collision_writes_no_key():
    assert "toolAliases" not in _apply(_spec("linear"))


def test_flag_on_never_touches_tools_or_allowed_tools():
    after = _apply(_spec("linear", "vercel"))
    assert after["tools"] == ["@linear", "@vercel"]
    assert after["allowedTools"] == ["@linear"]


def test_a_spec_without_mcp_servers_is_left_alone():
    assert _apply({"tools": []}) == {"tools": []}


def test_the_pass_is_idempotent():
    first = _apply(_spec("linear", "vercel"))
    assert _apply(dict(first)) == first


def test_per_tool_exposure_reaches_the_emission_layer():
    """The V1 row end to end: disjoint per-tool mounts emit no key at all."""
    spec = _spec("linear", "github", tools=["@linear/list_issues", "@github/get_issue"])
    assert "toolAliases" not in _apply(spec)


def test_disabling_one_provider_leaves_the_other_natural():
    assert "toolAliases" not in _apply(_spec("linear", "vercel", tools=["@linear"]))


# staleness, including registry drift


def test_disconnecting_a_provider_restores_natural_names():
    aliased = _apply(_spec("linear", "vercel"))
    assert aliased["toolAliases"]
    rebuilt = _apply({**_spec("linear"), "toolAliases": dict(aliased["toolAliases"])})
    assert "toolAliases" not in rebuilt


def test_a_renamed_registry_declaration_replaces_the_stranded_alias():
    """A tool-key rename is the reachable form of registry drift -- an alias-value
    rename is unreachable, because the validator pins each alias to its own
    derivation. Either way the superseded pair must not outlive its declaration."""
    from kiro_crew.connections import tool_aliases as ta

    first = _apply(_spec("linear", "vercel"))
    assert first["toolAliases"]["@linear/list_projects"] == "linear_list_projects"

    renamed = {
        "linear": {"list_all_projects": "linear_list_all_projects"},
        "vercel": {"list_all_projects": "vercel_list_all_projects"},
    }
    with patch.object(ta, "declared_tool_aliases", return_value=renamed):
        second = _apply({**_spec("linear", "vercel"), "toolAliases": dict(first["toolAliases"])})

    assert second["toolAliases"] == {
        "@linear/list_all_projects": "linear_list_all_projects",
        "@vercel/list_all_projects": "vercel_list_all_projects",
    }


def test_a_withdrawn_registry_declaration_drops_its_alias():
    from kiro_crew.connections import tool_aliases as ta

    first = _apply(_spec("linear", "vercel"))
    with patch.object(ta, "declared_tool_aliases", return_value={"vercel": {"x": "vercel_x"}}):
        second = _apply({**_spec("linear", "vercel"), "toolAliases": dict(first["toolAliases"])})

    assert "toolAliases" not in second


def test_a_user_alias_with_a_generated_looking_name_survives_a_rebuild():
    """B1: a prefix-shaped ownership test claims ``linear_issues`` -- it starts with
    ``linear_`` -- and a rebuild deletes or overwrites a deliberate user edit.
    Ownership must be proven by derivation, and ``linear_issues`` is not the name
    this pass would emit for ``@linear/list_issues`` (that is
    ``linear_list_issues``), so it is preserved with the flag ON."""
    config = _spec("linear", "vercel")
    config["toolAliases"] = {
        "@linear/list_issues": "linear_issues",
        "@vercel/get_project": "vercel_proj",
    }

    first = _apply(config)
    assert first["toolAliases"]["@linear/list_issues"] == "linear_issues"
    assert first["toolAliases"]["@vercel/get_project"] == "vercel_proj"

    second = _apply(dict(first))
    assert second["toolAliases"]["@linear/list_issues"] == "linear_issues"
    assert second["toolAliases"]["@vercel/get_project"] == "vercel_proj"


def test_a_user_alias_on_an_undeclared_tool_of_a_registry_provider_survives():
    """The same class one step out: the ref names a real provider but a tool the
    registry never declared, so nothing about it is this pass's output."""
    config = _spec("linear")
    config["toolAliases"] = {"@linear/create_comment": "linear_comment"}

    assert _apply(config)["toolAliases"] == {"@linear/create_comment": "linear_comment"}


def test_a_user_alias_survives_even_when_it_shadows_a_generated_destination():
    """A user alias is preserved AND reserved: the generated pair that would have
    landed on the same name is skipped rather than overwriting it."""
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@mine/thing": "vercel_list_projects"}

    after = _apply(config)

    assert after["toolAliases"]["@mine/thing"] == "vercel_list_projects"
    assert "@vercel/list_projects" not in after["toolAliases"]


def test_a_user_authored_alias_survives_registry_drift():
    from kiro_crew.connections import tool_aliases as ta

    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@linear/list_projects": "issues_from_linear"}
    first = _apply(config)
    assert first["toolAliases"]["@linear/list_projects"] == "issues_from_linear"

    renamed = {"linear": {"list_projects": "linear_projects"}}
    with patch.object(ta, "declared_tool_aliases", return_value=renamed):
        second = _apply({**_spec("linear"), "toolAliases": dict(first["toolAliases"])})

    assert second["toolAliases"] == {"@linear/list_projects": "issues_from_linear"}


# the record as the ownership oracle, end to end


def test_a_hand_written_notion_search_survives_a_rebuild():
    """The round-2 blocking finding, as an end-to-end test. ``notion`` has no
    registry declaration, so this pass can never have emitted for it -- yet
    ``notion_search`` is EXACTLY what re-derivation would generate for
    ``@notion/search``. The record holds no such triple, so the entry stands."""
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@notion/search": "notion_search"}

    first = _apply(config)
    assert first["toolAliases"]["@notion/search"] == "notion_search"

    second = _apply(dict(first))
    assert second["toolAliases"]["@notion/search"] == "notion_search"

    third = _apply(dict(second))
    assert third["toolAliases"]["@notion/search"] == "notion_search"


def test_a_user_edited_generated_alias_survives():
    """Once the user changes the VALUE, the spec's triple stops matching the
    recorded one, so the pair is not claimed. Invariant 3 in the field."""
    first = _apply(_spec("linear", "vercel"))
    assert first["toolAliases"]["@linear/list_projects"] == "linear_list_projects"

    edited = dict(first["toolAliases"])
    edited["@linear/list_projects"] = "my_linear_projects"
    second = _apply({**_spec("linear", "vercel"), "toolAliases": edited})

    assert second["toolAliases"]["@linear/list_projects"] == "my_linear_projects"
    # The generated destination is skipped rather than overwriting the edit, and
    # the other provider's own pair still resolves.
    assert second["toolAliases"]["@vercel/list_projects"] == "vercel_list_projects"

    third = _apply(dict(second))
    assert third["toolAliases"]["@linear/list_projects"] == "my_linear_projects"


def test_the_record_equals_exactly_what_the_pass_emitted():
    after = _apply(_spec("linear", "vercel"))

    assert _claimed(after) == frozenset(
        (ref[1:].split("/", 1)[0], ref[1:].split("/", 1)[1], alias)
        for ref, alias in after["toolAliases"].items()
    )


def test_a_retained_user_alias_is_not_recorded_as_emitted():
    """The record must hold ONLY this pass's own output: recording a user's pair
    would authorize deleting it on the next run."""
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@notion/search": "notion_search"}

    after = _apply(config)

    assert ("notion", "search", "notion_search") not in _claimed(after)


def test_a_skipped_alias_is_not_recorded():
    """A generated pair rejected by the destination guard was never written, so
    recording it would claim a name the spec does not carry."""
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@mine/thing": "vercel_list_projects"}

    after = _apply(config)

    assert "@vercel/list_projects" not in after["toolAliases"]
    assert ("vercel", "list_projects", "vercel_list_projects") not in _claimed(after)


# ── the state table: every crash boundary, both directions ──


def test_row4_an_interrupted_commit_is_reclaimed_not_stranded():
    """Row 4, the boundary the two-file ordering could never close.

    A hard kill between the durable spec write and the commit leaves the PENDING
    record, and its target fingerprint matches the map now on disk -- so the next
    pass resolves to exactly that emission and can still clean it up. Under the old
    record-after-spec ordering nothing claimed these pairs and they were the user's
    forever, which is what made a disconnect unable to remove them.
    """
    crashed = _apply(_spec("linear", "vercel"), persist=False)
    assert crashed["toolAliases"]["@linear/list_projects"] == "linear_list_projects"

    # Reclaimed: the interrupted transaction is still recognised as this pass's.
    assert _claimed(crashed) == emitted_from_alias_map(crashed["toolAliases"])

    # And the consequence that matters: the provider disconnects and the aliases go,
    # instead of being stranded in the spec for good.
    assert "toolAliases" not in _apply(
        {**_spec("linear"), "toolAliases": dict(crashed["toolAliases"])}
    )


def test_row4_a_hand_written_alias_survives_an_interrupted_remove():
    """Row 4 in the REMOVE direction -- the original server blocker, closed.

    The pass relinquishes ``vercel_list_projects``, the spec write lands, the commit
    is lost. The user then hand-writes exactly that name. The pending record resolves
    to the target generation, which does not contain the relinquished pair, so
    nothing claims the user's entry and it stands.
    """
    first = _apply(_spec("linear", "vercel"))
    relinquished = first["toolAliases"]["@vercel/list_projects"]

    _apply({**_spec("linear"), "toolAliases": dict(first["toolAliases"])}, persist=False)

    hand_written = {"@vercel/list_projects": relinquished}
    rebuilt = _apply({**_spec("linear"), "toolAliases": dict(hand_written)})

    assert rebuilt["toolAliases"] == hand_written


def test_row5_a_commit_failure_leaves_the_emission_recoverable():
    """Row 5. A failed commit is loud but recoverable: the pending record still
    describes the durable map, so the emission is neither stranded nor over-claimed.
    """
    first = _apply(_spec("linear", "vercel"))
    assert _claimed(first)

    # vercel disconnects, so this pass relinquishes its pairs -- the pending write
    # lands and only the commit fails.
    second = {**_spec("linear"), "toolAliases": dict(first["toolAliases"])}
    with patch(
        "kiro_crew.connections.alias_record.atomic_write", _fail_nth_record_write(2)
    ):
        with pytest.raises(OSError):
            _apply(second)

    # The pending write landed before the spec, so what is on disk describes the map
    # the pass produced, and the relinquished pairs are not in it.
    assert ("vercel", "list_projects", "vercel_list_projects") not in _claimed(second)
    assert _claimed(second) == emitted_from_alias_map(second.get("toolAliases", {}))


def test_rows2and3_a_lost_spec_write_keeps_the_claim_that_matches_the_disk():
    """Rows 2/3. The spec write never landed, so the map on disk is still the previous
    generation -- and the pending record resolves to the claim that was already valid
    for it. The pass simply retries; nothing is stranded and nothing new is claimed.
    """
    first = _apply(_spec("linear", "vercel"))
    durable = dict(first["toolAliases"])

    # This pass would relinquish vercel's pair, but its spec write is lost -- so the
    # map the NEXT pass sees is still `durable`.
    _apply({**_spec("linear"), "toolAliases": dict(durable)}, persist=False)

    # Resolution against the generation actually on disk still claims both pairs.
    assert _claimed({"toolAliases": durable}) == emitted_from_alias_map(durable)

    # So the retry cleans them, exactly as the interrupted pass intended.
    assert "toolAliases" not in _apply({**_spec("linear"), "toolAliases": dict(durable)})


def test_row1_a_failed_transaction_open_leaves_the_map_at_the_durable_generation(
    tmp_path, monkeypatch
):
    """Row 1's fail-closed half, now owned by the caller: no transaction, no advance.

    An unwritable data home fails the pending write, and the record then still
    describes the durable generation. That is safe only while the map is also still
    that generation, so the caller restores it from the durable snapshot and writes
    the rest of the spec normally -- an unwritable sidecar must not take down
    agent-spec repair, and the aliases must not advance past a record that cannot
    describe them.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.connections import alias_record

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    durable = {"@notion/search": "my_notion_search"}
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": durable}), encoding="utf-8"
    )
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: True)
    monkeypatch.setattr(alias_record, "begin_transaction", _raise_read_only)

    # clean=True makes the map genuinely diverge (the reset drops it), so the
    # transition is attempted and the fail-closed restore is what decides the write.
    agent_mod.rebuild_agent_config(clean=True)

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert written["toolAliases"] == durable, "the map advanced past the surviving record"
    assert not record_path().exists(), "no transaction should have been recorded"


def test_row1_a_failed_transaction_open_stops_the_pass_before_it_touches_the_spec():
    """The same fail-closed property at the pass level: with the claim resolved from
    a record it cannot replace, the pass must not be the thing that advances the map.
    """
    first = _apply(_spec("linear", "vercel"))
    before = _claimed(first)
    assert before

    config = {**_spec("linear"), "toolAliases": dict(first["toolAliases"])}
    with patch(
        "kiro_crew.connections.alias_record.atomic_write", side_effect=OSError("read-only")
    ):
        with pytest.raises(OSError):
            _apply(config)

    # The pending write never landed, so the record still describes the durable
    # generation and still claims exactly what it did.
    assert _claimed(first) == before


def test_row8_a_hand_edit_under_a_pending_record_claims_nothing():
    """Row 8. Neither candidate generation is on disk, so the record may authorize
    nothing -- a hand-edit between an interrupted pass and the next one is the one
    case where the transaction cannot tell which side it is looking at.
    """
    first = _apply(_spec("linear", "vercel"))
    # Interrupted: pending record on disk, describing either `first` or its successor.
    _apply({**_spec("linear"), "toolAliases": dict(first["toolAliases"])}, persist=False)

    edited = {"@linear/list_projects": "my_projects", "@vercel/list_projects": "mine"}
    assert _claimed({"toolAliases": edited}) == frozenset()

    # So both edits survive the next pass untouched, which is the safe direction.
    rebuilt = _apply({**_spec("linear", "vercel"), "toolAliases": dict(edited)})
    assert rebuilt["toolAliases"]["@linear/list_projects"] == "my_projects"
    assert rebuilt["toolAliases"]["@vercel/list_projects"] == "mine"


def test_row7_a_missing_record_claims_nothing_and_is_the_documented_degradation():
    """Row 7. Deleting the record file outright is the one case that does leave a
    generated pair permanently the user's -- invariant 4's deliberate degradation,
    reachable only by removing or corrupting the file, never by a write failure or a
    kill."""
    first = _apply(_spec("linear", "vercel"))
    record_path().unlink()

    second = _apply({**_spec("linear"), "toolAliases": dict(first["toolAliases"])})

    assert second["toolAliases"] == first["toolAliases"]


def test_the_whole_record_sequence_runs_under_the_lock_that_guards_the_spec(
    tmp_path, monkeypatch
):
    """Where the real rebuild makes these calls -- the whole ordering, one section.

    ``_apply`` above reproduces the ordering; this drives the actual caller, which
    is the only place the ordering can be got wrong. Concurrent rebuilds serialize
    on the spec lock, so every step from the record READ to the record WRITE has
    to sit inside it:

    * A record written after the lock is released can land AFTER the next pass's
      spec write -- a record describing a spec this pass did not write, which
      claims a name it never emitted and deletes it one cycle later.
    * A record READ taken before the lock is acquired is the same race from the
      other end: the overlapping pass reads this one's freshly-written spec before
      this one's record lands, finds no record claiming those aliases, concludes
      they are the user's, retains them, and writes a record that does not name
      them. Only a record authorizes removal, so they are then unclaimable
      forever.

    So the order must be read -> spec -> record, with the lock held throughout.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.apps import bridges
    from kiro_crew.connections import alias_record

    project = tmp_path / "project" / "agents"
    project.mkdir(parents=True)
    (project / "defaults.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    (project / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "project"))

    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    spec_path = kiro_dir / "kirocrew.json"
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-kiro.json")
    monkeypatch.setattr(agent_mod, "_CC_MCP_JSON", tmp_path / "absent-cc.json")
    # Makes the write take the locked branch, as it does in production.
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: spec_path)

    held = {"depth": 0}
    real_lock = bridges._mcp_lock

    @contextmanager
    def _tracking_lock(**kwargs):
        with real_lock(**kwargs):
            held["depth"] += 1
            try:
                yield
            finally:
                held["depth"] -= 1

    events: list[str] = []
    depth_at: dict = {}
    seen: dict = {}
    target = _gen(_MAP_L, {_L})
    real_load = alias_record.load_claimed
    real_write = agent_mod._atomic_json_write

    def _tracked_load(fingerprint):
        events.append("read")
        depth_at["read"] = held["depth"]
        return real_load(fingerprint)

    def _tracked_begin(previous, pending_target):
        events.append("pending")
        depth_at["pending"] = held["depth"]

    def _fake_pass(config, claimed=frozenset()):
        # Stands in for the resolver so the emission is deterministic. The record READ
        # and the transaction OPEN are now the CALLER's, so this only has to return a
        # generation -- what is under test is that the caller performs all four steps
        # inside the one section that writes the spec, and in order.
        return tuple(target)

    def _tracked_write(path_arg, data):
        real_write(path_arg, data)
        if Path(path_arg) == spec_path:
            events.append("spec")
            depth_at["spec"] = held["depth"]

    def _observe(generation):
        events.append("commit")
        depth_at["commit"] = held["depth"]
        seen["pairs"] = set(generation.emitted)

    monkeypatch.setattr(bridges, "_mcp_lock", _tracking_lock)
    monkeypatch.setattr(alias_record, "load_claimed", _tracked_load)
    monkeypatch.setattr(alias_record, "begin_transaction", _tracked_begin)
    monkeypatch.setattr(agent_mod, "_apply_connection_tool_aliases", _fake_pass)
    monkeypatch.setattr(agent_mod, "_atomic_json_write", _tracked_write)
    monkeypatch.setattr(alias_record, "commit_transaction", _observe)

    agent_mod.rebuild_agent_config()

    assert seen.get("pairs") == set(target.emitted), "the commit never ran"
    # One critical section: every step of the transaction sees the lock held.
    assert depth_at.get("read", 0) > 0, "record read before the spec lock was acquired"
    assert depth_at.get("pending", 0) > 0, "transaction opened outside the lock"
    assert depth_at.get("spec", 0) > 0, "spec written outside the lock"
    assert depth_at.get("commit", 0) > 0, "commit landed after the spec lock was released"
    # And in order. The pending write must precede the spec write, or a lost spec
    # write has no recoverable previous generation; the commit must follow it, or a
    # lost commit would leave a record describing a spec that never landed.
    assert events[: events.index("commit") + 1] == ["read", "pending", "spec", "commit"]


def test_a_corrupt_record_claims_nothing_on_the_next_pass():
    first = _apply(_spec("linear", "vercel"))
    record_path().write_text("{ not json", encoding="utf-8")

    second = _apply({**_spec("linear"), "toolAliases": dict(first["toolAliases"])})

    assert second["toolAliases"] == first["toolAliases"]


def test_an_unreadable_registry_leaves_the_record_untouched():
    """The pass returns None on a registry failure, and must NOT then open a
    transaction: forgetting a real emission strands those aliases for good, which is
    the "permanently unclearable" failure narrowing produced."""
    from kiro_crew.connections import tool_aliases as ta

    first = _apply(_spec("linear", "vercel"))
    before = _claimed(first)
    assert before

    with patch.object(ta, "declared_tool_aliases", side_effect=RuntimeError("bad registry")):
        assert _apply(dict(first), persist=True)["toolAliases"] == first["toolAliases"]

    assert _claimed(first) == before

    # And with the registry readable again the stale pairs are still clearable.
    assert "toolAliases" not in _apply(
        {**_spec("linear"), "toolAliases": dict(first["toolAliases"])}
    )


def test_the_gate_off_pass_writes_no_record():
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@linear/list_projects": "linear_list_projects"}

    from kiro_crew import agent

    with patch.object(agent, "_connection_tool_aliases_enabled", return_value=False):
        assert agent._apply_connection_tool_aliases(config) is None
    assert not record_path().exists()


def test_a_spec_without_servers_leaves_the_record_alone():
    first = _apply(_spec("linear", "vercel"))
    before = _claimed(first)

    from kiro_crew import agent

    with patch.object(agent, "_connection_tool_aliases_enabled", return_value=True):
        assert agent._apply_connection_tool_aliases({"tools": []}) is None
    assert _claimed(first) == before


def test_the_record_empties_when_the_collision_goes_away():
    """Relinquishing is observable: after a rebuild with no collision the record
    must claim nothing for the new generation, not carry the old claim forward, or a
    user who later hand-writes the old generated name would have it deleted."""
    first = _apply(_spec("linear", "vercel"))
    assert _claimed(first)

    cleared = _apply(_spec("linear"))

    assert _claimed(cleared) == frozenset()

    # Proof of the consequence: that same name is now safe to hand-write.
    config = _spec("linear")
    config["toolAliases"] = {"@linear/list_projects": "linear_list_projects"}
    assert _apply(config)["toolAliases"] == {"@linear/list_projects": "linear_list_projects"}


def test_a_stale_generated_ref_for_a_gone_provider_is_removed():
    """The pair must be RECORDED to be strippable, so the stale state is reached by
    really emitting it and then unmounting the provider -- not by planting it."""
    emitted = _apply(_spec("linear", "vercel"))
    assert emitted["toolAliases"]["@vercel/list_projects"] == "vercel_list_projects"

    rebuilt = _apply({**_spec("linear"), "toolAliases": dict(emitted["toolAliases"])})

    assert "toolAliases" not in rebuilt


def test_a_hand_planted_generated_looking_ref_is_never_claimed():
    """Same bytes, no record: with nothing proving this pass wrote them, the pairs
    are the user's and survive. This is the shape-based rule's failure mode
    inverted -- ``notion_search`` below is the name re-derivation would have
    claimed even though ``notion`` declares nothing at all."""
    config = _spec("linear")
    config["toolAliases"] = {
        "@vercel/list_projects": "vercel_list_projects",
        "@linear/list_projects": "linear_list_projects",
        "@notion/search": "notion_search",
    }

    after = _apply(config)

    assert after["toolAliases"] == {
        "@vercel/list_projects": "vercel_list_projects",
        "@linear/list_projects": "linear_list_projects",
        "@notion/search": "notion_search",
    }


# ── mutation checks ──
#
# Each rejected ownership rule is reinstated here and shown FAILING on the exact
# case that killed it. Without these the record looks like unnecessary machinery:
# the happy-path tests pass under a shape rule too, which is how three rounds of
# shape-based fixes each looked correct until the next case arrived.


def _prefix_ownership(record, ref, alias):
    """Rejected rule 1: ``alias.startswith(f"{slug}_")``."""
    parts = split_tool_ref(ref)
    return parts is not None and isinstance(alias, str) and alias.startswith(f"{parts[0]}_")


def _derivational_ownership(record, ref, alias):
    """Rejected rule 2: ``alias == f"{slug}_{tool}"`` (the round-2 blocker)."""
    parts = split_tool_ref(ref)
    return parts is not None and alias == derived_alias(parts[0], parts[1])


def _declared_only_derivational_ownership(record, ref, alias):
    """Rejected rule 3: rule 2, narrowed to providers that currently declare."""
    from kiro_crew.connections import tool_aliases as ta

    parts = split_tool_ref(ref)
    if parts is None or alias != derived_alias(parts[0], parts[1]):
        return False
    return parts[1] in ta.declared_tool_aliases().get(parts[0], {})


def _alias_blind_ownership(record, ref, alias):
    """Rejected rule 4: record the ref but not the VALUE (drops invariant 3)."""
    parts = split_tool_ref(ref)
    if parts is None:
        return False
    return any(slug == parts[0] and tool == parts[1] for slug, tool, _ in record)


def _with_ownership_rule(rule):
    return patch("kiro_crew.connections.alias_record.is_recorded_emission", new=rule)


def test_reverting_to_prefix_ownership_deletes_a_hand_written_alias():
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@linear/list_issues": "linear_issues"}

    with _with_ownership_rule(_prefix_ownership):
        broken = _apply(config)
    assert "@linear/list_issues" not in broken["toolAliases"]

    # The shipped rule keeps it.
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@linear/list_issues": "linear_issues"}
    assert _apply(config)["toolAliases"]["@linear/list_issues"] == "linear_issues"


def test_reverting_to_derivational_ownership_deletes_a_hand_written_notion_search():
    """The round-2 BLOCKING finding, pinned. ``notion`` carries no declaration, so
    no emission for it can exist -- but its hand-written alias is byte-identical to
    the name re-derivation would produce, and re-derivation deletes it."""
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@notion/search": "notion_search"}

    with _with_ownership_rule(_derivational_ownership):
        broken = _apply(config)
    assert "@notion/search" not in broken["toolAliases"]

    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@notion/search": "notion_search"}
    assert _apply(config)["toolAliases"]["@notion/search"] == "notion_search"


def test_narrowing_ownership_to_declared_providers_strands_the_pair_forever():
    """A rejected fix: withdrawing a declaration takes
    its slug out of the test, so the pair that declaration stranded can never be
    recognised again -- permanently unclearable, on every future rebuild."""
    from kiro_crew.connections import tool_aliases as ta

    stale = dict(_apply(_spec("linear", "vercel"))["toolAliases"])

    # The record clears it, because the record remembers the emission the
    # withdrawn declaration does not describe.
    with patch.object(ta, "declared_tool_aliases", return_value={}):
        fixed = _apply({**_spec("linear", "vercel"), "toolAliases": dict(stale)})
    assert "toolAliases" not in fixed

    # Same state, narrowed shape rule: the pair survives every rebuild.
    assert dict(_apply(_spec("linear", "vercel"))["toolAliases"]) == stale
    with _with_ownership_rule(_declared_only_derivational_ownership):
        with patch.object(ta, "declared_tool_aliases", return_value={}):
            broken = _apply({**_spec("linear", "vercel"), "toolAliases": dict(stale)})
            still_broken = _apply(dict(broken))
    assert still_broken["toolAliases"] == stale


def test_dropping_the_alias_from_the_record_key_deletes_a_user_edit():
    """Invariant 3 is load-bearing: keying on ``(slug, tool)`` alone claims the ref
    whatever its current value, so a user's edit of a generated alias is deleted.

    The record is written by hand here so that its fingerprint IS the map on disk,
    which keeps the committed branch's equality gate INERT and leaves the ownership
    rule as the only thing deciding. This boundary is defended twice -- the gate also
    refuses a record that does not describe the map -- and a mutation test has to
    isolate the defence it is probing or it stops probing anything.
    """
    first = _apply(_spec("linear", "vercel"))
    original = dict(first["toolAliases"])
    edited = dict(original)
    edited["@linear/list_projects"] = "my_linear_projects"

    # A committed record FOR THE EDITED GENERATION that still names the original
    # triples: the exact state invariant 3 adjudicates -- the record holds a pair
    # whose alias is not the value the map carries.
    record_path().write_text(
        json.dumps(
            {
                "version": 2,
                "status": "committed",
                "fingerprint": spec_fingerprint(edited),
                "emitted": [
                    {"slug": s, "tool": t, "alias": a}
                    for s, t, a in sorted(emitted_from_alias_map(original))
                ],
            }
        ),
        encoding="utf-8",
    )

    with _with_ownership_rule(_alias_blind_ownership):
        broken = _apply({**_spec("linear"), "toolAliases": dict(edited)})
    assert "@linear/list_projects" not in broken.get("toolAliases", {})

    # The shipped rule, same state: the alias is part of the key, so the edit is not
    # this pass's own and survives.
    record_path().write_text(
        json.dumps(
            {
                "version": 2,
                "status": "committed",
                "fingerprint": spec_fingerprint(edited),
                "emitted": [
                    {"slug": s, "tool": t, "alias": a}
                    for s, t, a in sorted(emitted_from_alias_map(original))
                ],
            }
        ),
        encoding="utf-8",
    )
    kept = _apply({**_spec("linear"), "toolAliases": dict(edited)})
    assert kept["toolAliases"]["@linear/list_projects"] == "my_linear_projects"


def test_committing_before_the_spec_write_strands_the_emission_it_should_recover():
    """The ordering mutation the PENDING phase exists to defeat.

    Committing before the spec write, then a crash before the spec reaches disk, leaves
    a record describing a generation that never existed anywhere. Two consequences, and
    only one of them is now harmful:

    * The DELETION chain is closed. The committed record's fingerprint does not match
      the map on disk, so it claims nothing -- a user who hand-writes one of those
      exact names keeps it. That is the equality gate on the committed branch doing
      the work, independently of the phase ordering.
    * The STRAND remains, and it is what the pending phase is for. A record naming a
      generation that never landed can never be resolved against disk, so the pairs it
      holds are unreclaimable; the real pass writes a PENDING record here, whose
      ``previous`` candidate is exactly what a lost spec write resolves to (rows 2/3).
    """
    from kiro_crew import agent

    reversed_order = _spec("linear", "vercel")
    with patch.object(agent, "_connection_tool_aliases_enabled", return_value=True):
        generation = agent._apply_connection_tool_aliases(reversed_order)
    # <- committed BEFORE the spec write; then "crash".
    commit_transaction(AliasGeneration(*generation))

    # The user, on a spec that never got those aliases, writes one of the names
    # themselves. It survives: the record does not describe this map.
    hand_written = _spec("linear")
    hand_written["toolAliases"] = {"@linear/list_projects": "linear_list_projects"}
    assert _apply(hand_written)["toolAliases"] == {
        "@linear/list_projects": "linear_list_projects"
    }

    # But the emission is gone for good: no map on disk is the generation the record
    # names, and the mutation kept no previous candidate to fall back to.
    assert load_claimed(spec_fingerprint(reversed_order.get("toolAliases"))) == frozenset()
    assert load_claimed(spec_fingerprint(None)) == frozenset()


def test_a_pending_record_without_its_previous_generation_cannot_be_rolled_back():
    """Why the pending record carries BOTH generations.

    Dropping ``previous`` leaves a lost spec write with nothing that describes the map
    still on disk, so the record claims nothing and every generated pair it was
    tracking becomes permanently the user's -- the strand this design exists to close,
    reintroduced by storing one generation instead of two.
    """
    durable = dict(_apply(_spec("linear", "vercel"))["toolAliases"])
    previous, target = _gen(durable, emitted_from_alias_map(durable)), _gen(_MAP_L, {_L})

    # The mutation: a pending record that names only where it was going.
    record_path().write_text(
        json.dumps(
            {
                "version": 2,
                "status": "pending",
                "fingerprint": target.fingerprint,
                "emitted": [
                    {"slug": s, "tool": t, "alias": a} for s, t, a in sorted(target.emitted)
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_claimed(previous.fingerprint) == frozenset()

    # With `previous` present, the same lost spec write is fully recoverable.
    begin_transaction(previous, target)
    assert load_claimed(previous.fingerprint) == previous.emitted


def test_relinquishing_does_not_depend_on_the_commit_landing():
    """Relinquishing a claim does not rest on the commit reaching disk.

    ``persist=False`` is a hard kill in the spec-write -> commit window. Under a
    plain two-file ordering that left the previous pass's claim on disk, where it ate
    a name the user is now entitled to write. The pending record written before the
    spec is what defeats it: its target fingerprint matches the map now on disk, so
    resolution returns the new emission and the relinquished pair is not claimed.
    """
    first = _apply(_spec("linear", "vercel"))
    assert _claimed(first)

    cleared = _apply(_spec("linear"), persist=False)
    assert _claimed(cleared) == frozenset()

    config = _spec("linear")
    config["toolAliases"] = {"@linear/list_projects": "linear_list_projects"}
    assert _apply(config)["toolAliases"] == {"@linear/list_projects": "linear_list_projects"}


# ── the authoritative under-lock spec snapshot (overlapping rebuilds) ──


def test_the_alias_map_is_reconciled_from_the_spec_on_disk(tmp_path):
    """``config`` carries a PRE-LOCK spec read, so the map in it can be a generation
    behind by the time the write happens. The value that counts is the one on disk."""
    from kiro_crew import agent

    spec = tmp_path / "kirocrew.json"
    spec.write_text(json.dumps({"toolAliases": _MAP_L}), encoding="utf-8")

    config = {"toolAliases": {"@vercel/list_projects": "stale"}}
    agent._reconcile_tool_aliases_from_disk(spec, config)

    assert config["toolAliases"] == _MAP_L


@pytest.mark.parametrize(
    "on_disk",
    [
        '{"toolAliases": null}',
        '{"toolAliases": []}',
        '{"toolAliases": "linear_list_projects"}',
        "{}",
        "{ not json",
        '"a string"',
    ],
)
def test_a_present_spec_with_no_usable_alias_map_reconciles_to_absent(on_disk, tmp_path):
    """A spec that EXISTS decides the map, and an unusable value clears the key.

    kiro-cli rejects the whole spec over a non-dict ``toolAliases``, so re-importing
    a hand-edited ``[]`` would carry the broken file forward and cost the user every
    tool. Dropping it means a rebuild repairs it even when the alias pass never runs.
    """
    from kiro_crew import agent

    spec = tmp_path / "kirocrew.json"
    spec.write_text(on_disk, encoding="utf-8")

    config = {"toolAliases": {"@vercel/list_projects": "stale"}}
    assert agent._reconcile_tool_aliases_from_disk(spec, config) is True

    assert "toolAliases" not in config


def test_a_missing_spec_preserves_the_assembled_alias_map(tmp_path):
    """A MISSING spec is not an invalid one -- there is no durable generation to
    reconcile against, so what the build assembled stands.

    The reachable case: a first install whose ``agent.json`` carries a hand-written
    ``toolAliases``. Treating "no file" like "file with a broken value" would erase
    that override before it was ever written.
    """
    from kiro_crew import agent

    assembled = {"@notion/search": "my_notion_search"}
    config = {"toolAliases": dict(assembled)}
    assert agent._reconcile_tool_aliases_from_disk(tmp_path / "absent.json", config) is False

    assert config["toolAliases"] == assembled


def test_a_clean_rebuild_does_not_reimport_an_invalid_alias_map(tmp_path, monkeypatch):
    """A clean rebuild regenerates from defaults, so it must DISCARD the old spec's
    aliases rather than reconcile them back in.

    The reachable case: a hand-edited ``"toolAliases": []`` plus a clean rebuild with
    the gate off. Re-importing it would write the invalid value straight back and
    kiro-cli would keep rejecting the spec -- the repair the user asked for would do
    nothing.
    """
    from kiro_crew import agent as agent_mod

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": []}), encoding="utf-8"
    )
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: False)

    agent_mod.rebuild_agent_config(clean=True)

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert "toolAliases" not in written, "the invalid value was carried forward"


def test_a_clean_rebuild_discards_the_old_specs_alias_map(tmp_path, monkeypatch):
    """Clean means regenerate from defaults, so a VALID on-disk map goes too.

    ``rebuild_agent_config`` documents that user customizations are preserved only
    when *clean* is False; reconciling the old spec's aliases back in on a clean
    rebuild would make this one key the exception, silently surviving the reset the
    user asked for.
    """
    from kiro_crew import agent as agent_mod

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": {"@notion/search": "notion_search"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: False)

    agent_mod.rebuild_agent_config(clean=True)

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert "toolAliases" not in written, "a clean rebuild re-imported the old map"


# ── the transition runs for every map-changing write, not only for the pass ──


def test_a_gate_off_clean_rebuild_retires_the_stale_claim(tmp_path, monkeypatch):
    """A clean write changes the map WITHOUT the pass, so it must still transition.

    Left alone, the old committed record would go on claiming a generation that no
    longer exists anywhere -- and a user who then re-adds one of those exact names
    has it deleted by the next rebuild. That is the deletion hazard the generation
    binding exists to remove, reached through the clean path instead of a crash.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.connections import alias_record

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": _MAP_LV}), encoding="utf-8"
    )
    alias_record.commit_transaction(_gen(_MAP_LV, {_L, _V}))
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: False)

    agent_mod.rebuild_agent_config(clean=True)

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert "toolAliases" not in written
    # The claim is retired against the generation actually written...
    assert alias_record.load_claimed(spec_fingerprint(None)) == frozenset()
    # ...so re-adding one of those exact names is safe rather than fatal.
    assert alias_record.load_claimed(spec_fingerprint(_MAP_LV)) == frozenset()


def test_a_gate_off_rebuild_that_changes_nothing_leaves_the_record_alone(
    tmp_path, monkeypatch
):
    """The opposite direction at the same boundary: when the map does NOT change, the
    transition must not run.

    Rewriting the record empty there would forget a real emission, and every
    generated pair it named would become permanently the user's -- unclaimable, so a
    later disconnect could never remove it.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.connections import alias_record

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": _MAP_LV}), encoding="utf-8"
    )
    alias_record.commit_transaction(_gen(_MAP_LV, {_L, _V}))
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: False)

    agent_mod.rebuild_agent_config()

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert written["toolAliases"] == _MAP_LV, "the unchanged map was not preserved"
    assert alias_record.load_claimed(spec_fingerprint(_MAP_LV)) == frozenset({_L, _V})


def test_a_clean_rebuild_preserves_a_user_alias_it_cannot_prove_is_generated(
    tmp_path, monkeypatch
):
    """The user-data direction on the clean path: with no record naming it, a map
    entry is the user's, and the transition retires nothing it did not own."""
    from kiro_crew import agent as agent_mod
    from kiro_crew.connections import alias_record

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    hand_written = {"@notion/search": "notion_search"}
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": hand_written}), encoding="utf-8"
    )
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: False)

    agent_mod.rebuild_agent_config(clean=True)

    # The clean reset removes the key, but nothing was ever CLAIMED, so the record
    # cannot authorize deleting that name if the user writes it again.
    assert alias_record.load_claimed(spec_fingerprint(hand_written)) == frozenset()


def test_a_commit_failure_on_a_gate_off_clean_write_stays_recoverable(
    tmp_path, monkeypatch
):
    """The clean transition's own interruption boundary (row 5).

    The spec write lands and the commit is lost, so the PENDING record is what
    survives -- and because its target fingerprint matches the clean map now on disk,
    the retirement still resolves instead of leaving the old claim in force.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.connections import alias_record

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": _MAP_LV}), encoding="utf-8"
    )
    alias_record.commit_transaction(_gen(_MAP_LV, {_L, _V}))
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: False)
    # The pending write lands; only the commit fails.
    monkeypatch.setattr(alias_record, "atomic_write", _fail_nth_record_write(2))

    with pytest.raises(OSError, match="read-only"):
        agent_mod.rebuild_agent_config(clean=True)

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert "toolAliases" not in written
    assert alias_record.load_claimed(spec_fingerprint(None)) == frozenset()


def test_an_overlapping_rebuild_cannot_resurrect_a_removed_alias(tmp_path):
    """The race the reconcile closes, driven end to end.

    Two rebuilds overlap. The first removes vercel's alias and writes the spec. The
    second is still holding the alias map it read BEFORE the lock, so without the
    reconcile it writes that stale map back and the removed alias returns -- and its
    fingerprint would certify a generation that is not on disk.
    """
    from kiro_crew import agent

    # First rebuild: both providers mounted, then vercel disconnects and is cleaned.
    first = _apply(_spec("linear", "vercel"))
    stale_snapshot = dict(first["toolAliases"])
    second = _apply({**_spec("linear"), "toolAliases": dict(stale_snapshot)})
    assert "toolAliases" not in second

    spec = tmp_path / "kirocrew.json"
    spec.write_text(json.dumps(second), encoding="utf-8")

    # Third rebuild, assembled from the PRE-LOCK read that still has both aliases.
    overlapping = {**_spec("linear"), "toolAliases": dict(stale_snapshot)}
    agent._reconcile_tool_aliases_from_disk(spec, overlapping)
    assert "toolAliases" not in overlapping, "the pre-lock snapshot was written back"

    assert "toolAliases" not in _apply(overlapping)


def test_a_gate_off_rebuild_does_not_write_back_a_pre_lock_snapshot(tmp_path):
    """The resurrection does not need the pass to run, which is why the reconcile is
    unconditional: a gate-off or fail-closed rebuild would write the stale map too."""
    from kiro_crew import agent

    spec = tmp_path / "kirocrew.json"
    spec.write_text(json.dumps({"tools": []}), encoding="utf-8")

    config = {**_spec("linear"), "toolAliases": dict(_MAP_L)}
    agent._reconcile_tool_aliases_from_disk(spec, config)
    with patch.object(agent, "_connection_tool_aliases_enabled", return_value=False):
        assert agent._apply_connection_tool_aliases(config) is None

    assert "toolAliases" not in config


# destination safety


def test_a_generated_alias_colliding_with_a_user_alias_target_is_skipped():
    config = _spec("linear", "vercel")
    config["toolAliases"] = {"@custom/thing": "vercel_list_projects"}
    after = _apply(config)
    assert after["toolAliases"]["@custom/thing"] == "vercel_list_projects"
    assert "@vercel/list_projects" not in after["toolAliases"]
    assert after["toolAliases"]["@linear/list_projects"] == "linear_list_projects"


def test_a_generated_alias_colliding_with_a_custom_servers_visible_tool_is_skipped():
    """V3: a custom mount's per-tool ref names a real tool, and a name it occupies
    is occupied whoever owns the server."""
    spec = _spec(
        "linear",
        "vercel",
        tools=["@linear", "@vercel", "@mycustom/vercel_list_projects"],
    )
    after = _apply(spec)
    assert "@vercel/list_projects" not in after["toolAliases"]
    assert after["toolAliases"]["@linear/list_projects"] == "linear_list_projects"


def test_a_whole_server_custom_mount_cannot_block_a_destination():
    """The OUT OF SCOPE row, asserted as a boundary rather than left implicit: a
    whole custom mount publishes no static names, so it degrades to shadowing."""
    spec = _spec("linear", "vercel", tools=["@linear", "@vercel", "@mycustom"])
    after = _apply(spec)
    assert after["toolAliases"]["@vercel/list_projects"] == "vercel_list_projects"


def test_a_custom_per_tool_ref_beside_a_whole_server_ref_still_reserves():
    """Exposure precedence must not leak into reservation: the custom server is
    mounted whole AND names one tool explicitly, and that name is occupied."""
    spec = _spec(
        "linear",
        "vercel",
        tools=["@linear", "@vercel", "@mycustom", "@mycustom/vercel_list_projects"],
    )
    after = _apply(spec)

    assert "@vercel/list_projects" not in after["toolAliases"]
    assert after["toolAliases"]["@linear/list_projects"] == "linear_list_projects"


def test_a_generated_alias_colliding_with_a_natural_tool_name_is_skipped():
    """A destination equal to a real tool name on an exposed provider would
    recreate the shadowing. Reachable because a natural name may itself carry the
    slug prefix that destinations use."""
    from kiro_crew.connections import tool_aliases as ta

    declarations = {
        "linear": {"list_projects": "linear_teams", "linear_teams": "linear_teams_alt"},
        "vercel": {"list_projects": "vercel_list_projects"},
    }
    with patch.object(ta, "declared_tool_aliases", return_value=declarations):
        after = _apply(_spec("linear", "vercel"))

    assert "@linear/list_projects" not in after.get("toolAliases", {})
    assert after["toolAliases"]["@vercel/list_projects"] == "vercel_list_projects"


def test_a_generated_alias_colliding_with_a_builtin_is_skipped():
    from kiro_crew.connections import tool_aliases as ta

    declarations = {
        "linear": {"list_projects": "linear_fs_read"},
        "vercel": {"list_projects": "vercel_list_projects"},
    }
    spec = _spec("linear", "vercel", tools=["linear_fs_read", "@linear", "@vercel"])
    with patch.object(ta, "declared_tool_aliases", return_value=declarations):
        after = _apply(spec)

    assert "@linear/list_projects" not in after.get("toolAliases", {})
    assert after["toolAliases"]["@vercel/list_projects"] == "vercel_list_projects"


# endpoint-upgrade continuity (the adjudicated row)


def test_a_registry_endpoint_change_degrades_to_shadowing_not_worse():
    """A retired ``mcp_url`` makes an existing install's entry stop matching, so
    its tools keep their natural names -- the pre-feature behaviour, and the
    fail-safe direction. The old generated refs go with it, so no dead ref is
    left pointing at a rename that is not applied."""
    aliased = _apply(_spec("linear", "vercel"))
    assert aliased["toolAliases"]

    moved = {**_spec("linear", "vercel"), "toolAliases": dict(aliased["toolAliases"])}
    moved["mcpServers"]["linear"] = {"url": "https://mcp.linear.app/mcp/v2"}
    after = _apply(moved)

    assert "toolAliases" not in after


def test_reconnecting_after_an_endpoint_change_restores_the_aliases():
    """Self-heal: Connect rewrites the entry from the registry, so the URL matches
    again and the aliases come back without any historical-URL bookkeeping."""
    moved = _spec("linear", "vercel")
    moved["mcpServers"]["linear"] = {"url": "https://mcp.linear.app/mcp/v2"}
    stripped = _apply(moved)
    assert "toolAliases" not in stripped

    reconnected = _apply(_spec("linear", "vercel"))
    assert reconnected["toolAliases"]["@linear/list_projects"] == "linear_list_projects"


# self-heal + failure containment


def test_a_non_dict_tool_aliases_value_is_replaced():
    config = _spec("linear", "vercel")
    config["toolAliases"] = ["not", "a", "map"]
    assert _apply(config)["toolAliases"]["@linear/list_projects"] == "linear_list_projects"


def test_a_non_string_alias_value_is_dropped():
    config = _spec("linear")
    config["toolAliases"] = {"@custom/thing": 42}
    assert "toolAliases" not in _apply(config)


def test_a_broken_registry_does_not_fail_the_rebuild():
    from kiro_crew import agent
    from kiro_crew.connections import tool_aliases as ta

    config = _spec("linear", "vercel")
    with patch.object(agent, "_connection_tool_aliases_enabled", return_value=True), patch.object(
        ta, "exposed_declared_tools", side_effect=RegistryValidationError("boom")
    ):
        agent._apply_connection_tool_aliases(config)
    assert "toolAliases" not in config


def test_a_broken_registry_does_not_clear_existing_aliases():
    """Failure must not be indistinguishable from 'no collisions', which clears."""
    from kiro_crew import agent
    from kiro_crew.connections import tool_aliases as ta

    config = _spec("linear")
    config["toolAliases"] = {"@custom/thing": "mine"}
    with patch.object(agent, "_connection_tool_aliases_enabled", return_value=True), patch.object(
        ta, "exposed_declared_tools", side_effect=OSError("no registry")
    ):
        agent._apply_connection_tool_aliases(config)
    assert config["toolAliases"] == {"@custom/thing": "mine"}


# ── the gate ──


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, False),
        ({"connections": {}}, False),
        ({"connections": {"tool_aliases": False}}, False),
        ({"connections": {"tool_aliases": "yes"}}, False),
        ({"connections": {"tool_aliases": 1}}, False),
        ({"connections": True}, False),
        ({"connections": {"tool_aliases": True}}, True),
    ],
)
def test_the_gate_is_off_unless_explicitly_true(raw, expected):
    from kiro_crew import agent

    with patch.object(agent, "_load_json", return_value=raw):
        assert agent._connection_tool_aliases_enabled() is expected


# ── the boot invariant ──


def test_importing_agent_does_not_eagerly_load_the_registry(tmp_path):
    """The registry validates at MODULE level, so an eager import would make a
    malformed registry.json break `import kiro_crew.agent` -- the module that
    installs and repairs the agent spec -- before any guard runs.

    Checked in a SUBPROCESS against real sys.modules, not by reading agent.py's
    import block: the module could be pulled in transitively by anything agent.py
    imports, which source inspection of one file cannot see."""
    import subprocess
    import sys

    probe = (
        "import sys, kiro_crew.agent; "
        "assert 'kiro_crew.connections.registry' not in sys.modules, "
        "sorted(m for m in sys.modules if m.startswith('kiro_crew.connections'))"
    )
    # ``-B`` + a throwaway cwd keep the child's imports side-effect free: without
    # them it writes ``__pycache__`` bytecode into the source tree it imports.
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
