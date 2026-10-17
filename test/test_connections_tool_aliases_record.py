"""Contract tests for Connections tool-name collision resolution: the ownership record.

Ownership of an already-written alias is decided by the persisted record in
:mod:`kiro_crew.connections.alias_record`, never by the shape of the name, so that
module's invariants are covered here -- including the GENERATION BINDING that keeps a
record from ever being read as a description of a spec it does not match -- together
with the pending resolution of an interrupted transaction and the record write
failures driven through the real rebuild.

One topic of the ``test_connections_tool_aliases*.py`` family; the builders and autouse
fixtures the family shares come from ``connections_tool_aliases_helpers.py``.
"""

import json
from unittest.mock import patch

import pytest
from connections_tool_aliases_helpers import _isolated_alias_record  # noqa: F401
from connections_tool_aliases_helpers import _pinned_kiro_cli_version  # noqa: F401
from connections_tool_aliases_helpers import (
    _L,
    _MAP_L,
    _MAP_LV,
    _V,
    _fail_nth_record_write,
    _gen,
    _raise_read_only,
    _rebuild_env,
)

from kiro_crew.connections.alias_record import (
    begin_transaction,
    commit_transaction,
    emitted_from_alias_map,
    is_recorded_emission,
    load_claimed,
    record_path,
    spec_fingerprint,
    split_tool_ref,
)

# ── the persisted ownership record ──


@pytest.mark.parametrize(
    "ref,alias,expected",
    [
        # Claimed: the whole triple is in the record.
        ("@linear/list_projects", "linear_list_projects", True),
        # NOT claimed: the record does not hold it, whatever it looks like. The
        # second row is the finding a re-derivation rule could not survive --
        # ``notion`` declares nothing and this pass never emitted for it, yet
        # ``notion_search`` is exactly the name it WOULD derive.
        ("@linear/list_issues", "linear_issues", False),
        ("@notion/search", "notion_search", False),
        ("@vercel/list_projects", "vercel_list_projects", False),
        # NOT claimed: right ref, but the value does not match the recorded
        # form. Membership of the whole triple IS the byte-equality test.
        ("@linear/list_projects", "linear_list_projects ", False),
        ("@linear/list_projects", "Linear_List_Projects", False),
        ("@linear/list_projects", "my_projects", False),
        # NOT claimed: malformed or non-per-tool refs name no single tool.
        ("@linear", "linear_list_projects", False),
        ("linear/list_projects", "linear_list_projects", False),
        ("@/list_projects", "linear_list_projects", False),
        ("@linear/", "linear_list_projects", False),
        ("@linear/list_projects", 42, False),
        (42, "linear_list_projects", False),
        (None, None, False),
    ],
)
def test_ownership_is_decided_by_the_record_not_by_the_name(ref, alias, expected):
    """Shape asks about the present; ownership is a fact about the past. Only a
    recorded emission may be stripped, so a hand-written name that merely looks
    generated is never claimed."""
    record = {("linear", "list_projects", "linear_list_projects")}
    assert is_recorded_emission(record, ref, alias) is expected


def test_an_empty_record_claims_nothing():
    for ref, alias in [
        ("@linear/list_projects", "linear_list_projects"),
        ("@notion/search", "notion_search"),
    ]:
        assert is_recorded_emission(frozenset(), ref, alias) is False


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("@linear/list_projects", ("linear", "list_projects")),
        ("@linear/a/b", ("linear", "a/b")),
        ("@linear", None),
        ("@linear/", None),
        ("@/tool", None),
        ("linear/tool", None),
        ("", None),
        (42, None),
    ],
)
def test_a_tool_ref_splits_only_when_it_names_one_tool(ref, expected):
    assert split_tool_ref(ref) == expected


def test_an_alias_map_converts_to_record_triples():
    assert emitted_from_alias_map(
        {"@linear/list_projects": "linear_list_projects", "@vercel": "ignored"}
    ) == frozenset({("linear", "list_projects", "linear_list_projects")})


def test_a_map_fingerprint_is_canonical_over_key_order():
    """Re-serialising the same map must not look like a new generation."""
    assert spec_fingerprint({"a": "1", "b": "2"}) == spec_fingerprint({"b": "2", "a": "1"})
    assert spec_fingerprint(_MAP_L) != spec_fingerprint(_MAP_LV)


@pytest.mark.parametrize("absent", [None, [], "toolAliases", 42])
def test_an_absent_or_non_dict_map_fingerprints_apart_from_an_empty_one(absent):
    """A generation that REMOVED the key must not match one that emptied it, and a
    hand-edited non-dict carries no pair this pass could own either."""
    assert spec_fingerprint(absent) == spec_fingerprint(None)
    assert spec_fingerprint(absent) != spec_fingerprint({})


def test_a_committed_record_round_trips_through_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    target = _gen(_MAP_LV, {_L, _V})
    commit_transaction(target)
    assert load_claimed(target.fingerprint) == frozenset({_L, _V})


def test_a_committed_record_is_fingerprint_gated(tmp_path, monkeypatch):
    """A committed record claims only the generation it actually describes.

    On the normal path the gate is inert: the record is written right after its spec
    write, so the next rebuild fingerprints that same map. It earns its keep when the
    record outlives its spec -- see the orphaned-record tests below, where an ungated
    claim would delete an alias that is by then the user's only copy.

    The accepted cost, chosen deliberately: an edit ELSEWHERE in the map re-fingerprints
    the generation, so this pass's own untouched siblings stop being claimable and
    linger. Lingering generated aliases are invariant 4's degradation (the pre-feature
    shadowing); deleting a user's alias is data loss. The failure direction is what is
    being chosen here, not the failure rate.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    commit_transaction(_gen(_MAP_LV, {_L, _V}))

    # Recorded generation is on disk -> honoured.
    assert load_claimed(spec_fingerprint(_MAP_LV)) == frozenset({_L, _V})
    # Canonical over key order, so an unrelated re-serialisation is the same generation.
    assert load_claimed(spec_fingerprint(dict(reversed(list(_MAP_LV.items()))))) == frozenset(
        {_L, _V}
    )

    # The user edits one alias, so the map is not the recorded generation. The
    # claim is withdrawn wholesale; the siblings linger (the accepted cost above).
    edited = {**_MAP_LV, "@linear/list_projects": "my_projects"}
    assert load_claimed(spec_fingerprint(edited)) == frozenset()
    # The edited pair was never claimable either way -- invariant 3 already spared it.
    assert (
        is_recorded_emission(
            load_claimed(spec_fingerprint(edited)), "@linear/list_projects", "my_projects"
        )
        is False
    )


def test_an_orphaned_committed_record_cannot_delete_a_user_alias(tmp_path, monkeypatch):
    """FIX A, the chain this gate closes: record survives, spec does not.

    The transaction committed, then the spec was deleted or rewritten out of band (a
    restore from backup, a manual edit, a wiped agent home). The record is intact and
    internally consistent, so nothing but the fingerprint can tell that the generation
    it describes is gone. Meanwhile a triple it names is now present from ANOTHER
    source -- the user wrote it by hand. An ungated claim would strip it.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    commit_transaction(_gen(_MAP_LV, {_L, _V}))

    # The spec is GONE: no toolAliases key at all (missing spec, or a spec that never
    # carried the map). Nothing on disk is the recorded generation.
    assert load_claimed(spec_fingerprint(None)) == frozenset()

    # The spec is REPLACED out of band and happens to carry one of the recorded
    # triples byte for byte -- but as the user's own entry, in a map this pass never
    # wrote. It must survive.
    user_written = {"@linear/list_projects": _MAP_LV["@linear/list_projects"]}
    claimed = load_claimed(spec_fingerprint(user_written))
    assert claimed == frozenset()
    assert (
        is_recorded_emission(claimed, "@linear/list_projects", _MAP_LV["@linear/list_projects"])
        is False
    )


def test_a_committed_record_matching_the_spec_is_still_honoured(tmp_path, monkeypatch):
    """The gate must not cost the cleanup the feature exists for.

    A withdrawn declaration strands a pair; the committed record from the generation
    that emitted it is what authorizes clearing it. That path still works, because the
    map on disk IS the recorded generation until this pass changes it.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    target = _gen(_MAP_LV, {_L, _V})
    commit_transaction(target)

    claimed = load_claimed(target.fingerprint)
    assert claimed == frozenset({_L, _V})
    for slug, tool, alias in (_L, _V):
        assert is_recorded_emission(claimed, f"@{slug}/{tool}", alias) is True


def test_the_ownership_record_is_write_protected_from_agent_tools():
    """The record AUTHORIZES deletion, so an agent must not be able to write it.

    Invariant 1 makes this file the only ownership oracle, and invariant 2 makes it the
    grant that lets the pass strip a pair out of the spec. An agent that can write it can
    forge a committed record naming an alias the USER hand-wrote, paired with the
    fingerprint of the spec on disk -- which is readable, so it is computable -- and the
    next rebuild deletes that alias as though it were its own. The fingerprint cannot
    defend this: a forger reads the same spec it does. So the defence is placement, in the
    same platform list that already protects the on-call schedule and the incident index
    for exactly this reason (an input to an authorization decision).

    Derived from the live list and the module's own filename, so moving the record without
    moving its protection fails here.
    """
    from kiro_crew import security
    from kiro_crew.connections.alias_record import _RECORD_FILENAME

    # The entry must name the file the module actually writes, in every crew home root.
    assert record_path().name == _RECORD_FILENAME
    protected = set(security.write_protected_home_paths())
    for prefix in security.crew_home_prefixes():
        assert (
            f"{prefix}/{_RECORD_FILENAME}" in protected
        ), f"the ownership record is agent-writable under {prefix}"


def test_an_absent_record_reads_as_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    assert not record_path().exists()
    assert load_claimed(spec_fingerprint(_MAP_L)) == frozenset()


@pytest.mark.parametrize(
    "payload",
    [
        "{ not json",
        "",
        "null",
        "[]",
        '"a string"',
        "{}",
        '{"status": "committed", "emitted": []}',  # no version
        '{"version": 99, "status": "committed", "emitted": [{"slug": "l", "tool": "t", "alias": "a"}]}',
        '{"version": 2, "status": "committed", "emitted": {}}',
        '{"version": 2, "status": "committed"}',
        '{"version": 2, "emitted": [{"slug": "l", "tool": "t", "alias": "a"}]}',  # no status
        '{"version": 2, "status": "nonsense", "emitted": [{"slug": "l", "tool": "t", "alias": "a"}]}',
        # v1: written before the generation binding existed, so it cannot say which
        # spec it describes. Honouring it would inherit exactly the unverifiable
        # claim this version exists to reject.
        '{"version": 1, "emitted": [{"slug": "l", "tool": "t", "alias": "a"}]}',
    ],
)
def test_a_corrupt_or_unknown_record_reads_as_empty(payload, tmp_path, monkeypatch):
    """Invariant 4: losing the record must degrade to "every pair is the user's",
    never to deleting entries on a bad parse."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    assert load_claimed(spec_fingerprint(_MAP_L)) == frozenset()


def test_a_record_entry_that_is_not_three_strings_is_dropped(tmp_path, monkeypatch):
    """Dropping a malformed entry UNDERSTATES, which is the safe direction."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "status": "committed",
                "fingerprint": spec_fingerprint(_MAP_L),
                "emitted": [
                    {"slug": "linear", "tool": "list_projects", "alias": "linear_list_projects"},
                    {"slug": "vercel", "tool": "list_projects"},
                    {"slug": "vercel", "tool": 7, "alias": "x"},
                    "not a dict",
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_claimed(spec_fingerprint(_MAP_L)) == frozenset({_L})


# ── pending resolution: the interrupted transaction (state-table rows 2-5, 8) ──


def test_a_pending_record_resolves_to_the_target_when_the_spec_write_landed(tmp_path, monkeypatch):
    """Rows 4/5. The map on disk IS the target generation, so the emission it
    describes is real and must be reclaimed rather than abandoned."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    previous, target = _gen(_MAP_LV, {_L, _V}), _gen(_MAP_L, {_L})
    begin_transaction(previous, target)

    assert load_claimed(target.fingerprint) == frozenset({_L})


def test_a_pending_record_resolves_to_the_previous_when_the_spec_write_did_not_land(
    tmp_path, monkeypatch
):
    """Rows 2/3. The map on disk is still the previous generation, so the claim that
    was already valid for it is the only one that may be acted on."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    previous, target = _gen(_MAP_LV, {_L, _V}), _gen(_MAP_L, {_L})
    begin_transaction(previous, target)

    assert load_claimed(previous.fingerprint) == frozenset({_L, _V})


def test_a_pending_record_matching_neither_generation_claims_nothing(tmp_path, monkeypatch):
    """Row 8. Something that is not this pass changed the map, so neither candidate
    describes what is on disk and the record may authorize nothing."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    begin_transaction(_gen(_MAP_LV, {_L, _V}), _gen(_MAP_L, {_L}))

    hand_edited = {"@linear/list_projects": "my_projects"}
    assert load_claimed(spec_fingerprint(hand_edited)) == frozenset()


def test_a_no_op_transaction_resolves_to_its_target(tmp_path, monkeypatch):
    """Both candidates share a fingerprint when the map did not change, so the tie
    must go to the target: it describes that same map and is the newer emission."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    begin_transaction(_gen(_MAP_L, frozenset()), _gen(_MAP_L, {_L}))

    assert load_claimed(spec_fingerprint(_MAP_L)) == frozenset({_L})


def test_committing_replaces_the_pending_record(tmp_path, monkeypatch):
    """After a clean pass the previous generation is not recoverable, and must
    not be: it does not describe anything on disk."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    previous, target = _gen(_MAP_LV, {_L, _V}), _gen(_MAP_L, {_L})
    begin_transaction(previous, target)
    commit_transaction(target)

    assert load_claimed(target.fingerprint) == frozenset({_L})
    # Querying with the retired generation's fingerprint claims NOTHING, not the
    # previous claim and not the new one: a committed record answers only for the
    # generation it describes. That is the stronger form of "not recoverable" --
    # the rollback candidate is gone, and the record does not answer for a map that
    # is not the one it was committed for either.
    assert load_claimed(previous.fingerprint) == frozenset()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "abc123",
        "0" * 63,
        "0" * 65,
        "Z" * 64,
        "0123456789ABCDEF" * 4,
        42,
        ["0" * 64],
    ],
)
@pytest.mark.parametrize("status", ["committed", "pending"])
def test_a_record_without_a_valid_fingerprint_claims_nothing(bad, status, tmp_path, monkeypatch):
    """A payload that parses as v2 but names no generation is MALFORMED.

    A missing, truncated, non-hex or uppercase fingerprint cannot describe any map,
    so trusting the emissions beside it would authorize deletion on the word of a
    record this writer cannot have produced. It reads as empty (invariant 4) --
    including on the committed branch, whose freedom from an EQUALITY gate is only
    sound while the record is demonstrably ours.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    payload = {
        "version": 2,
        "status": status,
        "emitted": [{"slug": "linear", "tool": "list_projects", "alias": "linear_list_projects"}],
    }
    if bad is not None:
        payload["fingerprint"] = bad
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_claimed(spec_fingerprint(_MAP_L)) == frozenset()


def test_a_pending_record_with_a_malformed_previous_generation_ignores_it(tmp_path, monkeypatch):
    """An unusable `previous` fingerprint cannot be matched, so it authorizes nothing.

    No separate shape check is needed on the rollback candidate: the queried
    fingerprint is always a real :func:`spec_fingerprint` value, so anything that is
    not one can never equal it. The target still resolves normally.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    target = _gen(_MAP_L, {_L})
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "status": "pending",
                "fingerprint": target.fingerprint,
                "emitted": [
                    {"slug": s, "tool": t, "alias": a} for s, t, a in sorted(target.emitted)
                ],
                "previous": {
                    "fingerprint": "not-a-hash",
                    "emitted": [
                        {"slug": "vercel", "tool": "list_projects", "alias": "vercel_list_projects"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_claimed(target.fingerprint) == frozenset({_L})
    assert load_claimed(spec_fingerprint(_MAP_LV)) == frozenset()


@pytest.mark.parametrize("phase", ["pending", "committed"])
def test_both_record_writes_raise_instead_of_swallowing(phase, tmp_path, monkeypatch):
    """Invariant 5: neither write may fail silently.

    A pending failure means the spec must not advance, and a commit failure means
    bookkeeping is behind over a correct spec. The writer cannot repair either, so
    an unwritable data home is reported when it happens rather than surfacing later
    as aliases nothing explains.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    target = _gen(_MAP_L, {_L})
    with patch("kiro_crew.connections.alias_record.atomic_write", side_effect=OSError("read-only")):
        with pytest.raises(OSError, match="read-only"):
            if phase == "pending":
                begin_transaction(_gen(_MAP_LV, {_L, _V}), target)
            else:
                commit_transaction(target)


def test_a_failed_pending_write_leaves_the_earlier_record_intact(tmp_path, monkeypatch):
    """Row 1's state. ``atomic_write`` leaves the destination alone on failure, so
    the record still describes the PREVIOUS generation -- which is safe only because
    the pass then refuses to advance the spec past it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    previous = _gen(_MAP_LV, {_L, _V})
    commit_transaction(previous)

    with patch("kiro_crew.connections.alias_record.atomic_write", side_effect=OSError("read-only")):
        with pytest.raises(OSError):
            begin_transaction(previous, _gen(_MAP_L, {_L}))

    assert load_claimed(previous.fingerprint) == frozenset({_L, _V})


def test_a_commit_failure_surfaces_out_of_the_real_rebuild_and_stays_recoverable(
    tmp_path, monkeypatch
):
    """Row 5, driven through the real caller.

    The spec write lands and the commit write fails. Two properties: the failure
    propagates rather than dying inside the pass, and the pending record left behind
    describes the map that is now durable -- so the emission is RECONCILED by the
    next pass instead of being stranded, which is what the two-file ordering could
    never offer at this boundary.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.connections import alias_record

    spec_path = _rebuild_env(tmp_path, monkeypatch)

    target = _gen(_MAP_L, {_L})
    monkeypatch.setattr(
        agent_mod,
        "_apply_connection_tool_aliases",
        lambda config, claimed=frozenset(): tuple(target),
    )
    # The caller opens the transaction, so the pending write is the FIRST record
    # write and the commit is the second. Fail only the second, leaving the pending
    # record on disk -- the state a real interrupted rebuild leaves. The spec goes
    # through agent._atomic_json_write, a different binding, so the failure is
    # isolated to bookkeeping.
    monkeypatch.setattr(alias_record, "atomic_write", _fail_nth_record_write(2))

    with pytest.raises(OSError, match="read-only"):
        agent_mod.rebuild_agent_config()

    assert spec_path.exists(), "the spec must be durable before the commit is attempted"
    # The pending record survives and still describes the durable generation, so the
    # next pass reclaims exactly this emission rather than treating it as the user's.
    assert alias_record.load_claimed(target.fingerprint) == frozenset({_L})


def test_a_pending_write_failure_skips_the_pass_without_failing_the_rebuild(tmp_path, monkeypatch):
    """Row 1, driven through the real caller: fail closed, and only the pass.

    ``rebuild_agent_config`` installs and repairs the agent spec, so the fail-closed
    branch is caught inside the pass: the spec is still written and only alias
    maintenance stands down. Letting the OSError out would let an unwritable sidecar
    take down spec repair.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.connections import alias_record

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_mod, "_connection_tool_aliases_enabled", lambda: True)
    monkeypatch.setattr(alias_record, "begin_transaction", _raise_read_only)

    agent_mod.rebuild_agent_config()

    assert spec_path.exists(), "a failed transaction must not stop the spec being written"
    assert not record_path().exists(), "the pass stood down, so it wrote no record"


def test_a_broken_registry_does_not_abort_the_rebuild_at_import_time(tmp_path, monkeypatch):
    """FIX B: the alias import is a submodule import, so it runs the package __init__.

    ``kiro_crew.connections.__init__`` imports the registry, which validates
    registry.json EAGERLY at module level (``_PROVIDERS = _load_registry()``). So a
    corrupt or newly-invalid registry raises at the IMPORT, which is upstream of every
    fail-closed guard inside the pass -- an optional feature would take down agent-spec
    repair, the one thing a user runs a rebuild to get. The import must therefore be
    survivable: the spec is written, the on-disk map is kept, and only the ownership
    pass stands down.
    """
    import builtins

    from kiro_crew import agent as agent_mod
    from kiro_crew.connections.registry import RegistryValidationError

    spec_path = _rebuild_env(tmp_path, monkeypatch)
    user_map = {"@notion/search": "notion_search"}
    spec_path.write_text(
        json.dumps({"name": "kirocrew", "toolAliases": dict(user_map)}), encoding="utf-8"
    )

    real_import = builtins.__import__

    def _registry_is_broken(name, *args, **kwargs):
        # The real exception from the real chain: __init__ -> registry -> _load_registry.
        if name == "kiro_crew.connections.alias_record":
            raise RegistryValidationError("registry.json entry 3: slug is not a string")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _registry_is_broken)

    # No exception: a broken optional registry must not propagate out of a rebuild.
    agent_mod.rebuild_agent_config()

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert written["toolAliases"] == user_map, "the on-disk alias map was not preserved"
    assert written["name"] == "kirocrew", "the rest of the spec was written normally"
    # No ownership transition was opened: with no record module there is no claim to
    # retire, and an absent record reads as empty, so the pairs on disk stay the user's.
    assert not record_path().exists(), "the pass stood down, so it wrote no record"


def test_an_empty_emission_relinquishes_every_earlier_claim(tmp_path, monkeypatch):
    """Committing an EMPTY emission is not a no-op: it is how the pass gives up pairs
    it does not write. Skipping it would leave a superseded triple claimable."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    commit_transaction(_gen(_MAP_L, {_L}))
    relinquished = _gen(None, frozenset())
    commit_transaction(relinquished)

    assert load_claimed(relinquished.fingerprint) == frozenset()
    assert record_path().exists()
