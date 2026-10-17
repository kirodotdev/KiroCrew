"""Tests for ``agent_state.lift_and_strip_bookkeeping``.

kiro-cli's ``deny_unknown_fields`` rejects an entire agent spec on any
unrecognized key, so ``model_managed`` / ``cc_model`` must never reach a kiro
JSON file. This is the single helper shared by the PUT handler,
``migrate_agent_specs``, and ``_refresh_dynamic_fields`` — pin its
lift/strip/no-clobber/type-guard contract directly, independent of any caller.
"""

from __future__ import annotations

import logging

from kiro_crew import agent_state


def test_lifts_when_unset():
    config = {"name": "kirocrew", "model_managed": True, "cc_model": "claude-sonnet-4.6"}

    changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "model_managed" not in config
    assert "cc_model" not in config
    assert agent_state.get_model_managed("kirocrew") is True
    assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"


def test_does_not_clobber_existing_sidecar_value():
    agent_state.set_model_managed("kirocrew", False)
    agent_state.set_cc_model("kirocrew", "test-model-stub")
    config = {"model_managed": True, "cc_model": "claude-sonnet-4.6"}

    changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "model_managed" not in config
    assert "cc_model" not in config
    assert agent_state.get_model_managed("kirocrew") is False
    assert agent_state.get_cc_model("kirocrew") == "test-model-stub"


def test_non_bool_model_managed_discarded_not_lifted(caplog):
    config = {"model_managed": "false"}

    with caplog.at_level(logging.WARNING):
        changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "model_managed" not in config
    # Not lifted: bool("false") is True, which would have silently flipped
    # the flag's meaning had the raw value been coerced instead of guarded.
    assert agent_state.get_model_managed("kirocrew") is None
    assert "non-bool model_managed" in caplog.text


def test_non_string_cc_model_discarded_not_lifted(caplog):
    config = {"cc_model": 123}

    with caplog.at_level(logging.WARNING):
        changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "cc_model" not in config
    assert agent_state.get_cc_model("kirocrew") is None
    assert "non-string cc_model" in caplog.text


def test_returns_false_when_neither_key_present():
    config = {"name": "kirocrew", "model": "auto"}

    changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is False
    assert config == {"name": "kirocrew", "model": "auto"}


def test_falsy_cc_model_strips_without_lifting(caplog):
    config = {"cc_model": ""}

    with caplog.at_level(logging.WARNING):
        changed = agent_state.lift_and_strip_bookkeeping(config, "kirocrew")

    assert changed is True
    assert "cc_model" not in config
    assert agent_state.get_cc_model("kirocrew") is None
    assert caplog.text == ""


# --- Fork lineage sidecar (forked_from / private_to) -------------------------
#
# A template spec cannot carry fork lineage (kiro-cli's deny_unknown_fields
# drops the whole agent on any unknown key), so it lives in the same sidecar as
# model_managed / cc_model. Both keys must be non-empty strings to count.


def test_fork_info_roundtrips():
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")

    info = agent_state.get_fork_info("design-crew")
    assert info == {"forked_from": "kirocrew", "private_to": "design-crew"}


def test_get_fork_info_none_when_unset():
    assert agent_state.get_fork_info("never-forked") is None


def test_fork_info_survives_alongside_model_bookkeeping():
    """Fork keys and model keys share one entry and must not clobber each other."""
    agent_state.set_model_managed("copy", True)
    agent_state.set_fork_info("copy", forked_from="kirocrew", private_to="a-crew")

    assert agent_state.get_fork_info("copy") == {
        "forked_from": "kirocrew",
        "private_to": "a-crew",
    }
    assert agent_state.get_model_managed("copy") is True


def test_clear_fork_info_drops_lineage_and_keeps_model_bookkeeping():
    """Clearing lineage flips a copy back to shared without touching model state."""
    agent_state.set_model_managed("copy", True)
    agent_state.set_fork_info("copy", forked_from="kirocrew", private_to="a-crew")

    agent_state.clear_fork_info("copy")

    assert agent_state.get_fork_info("copy") is None
    assert agent_state.get_model_managed("copy") is True


def test_clear_fork_info_removes_an_entry_left_empty_and_tolerates_unknown_names():
    agent_state.set_fork_info("only-lineage", forked_from="kirocrew", private_to="a-crew")

    agent_state.clear_fork_info("only-lineage")
    agent_state.clear_fork_info("never-recorded")

    assert agent_state.get_fork_info("only-lineage") is None
    assert "only-lineage" not in agent_state.all_fork_info()


def test_get_fork_info_ignores_empty_strings():
    """An entry with a blank forked_from or private_to is not a real fork."""
    agent_state.set_fork_info("blank", forked_from="", private_to="crew")
    assert agent_state.get_fork_info("blank") is None

    agent_state.set_fork_info("blank2", forked_from="kirocrew", private_to="")
    assert agent_state.get_fork_info("blank2") is None


def test_get_fork_info_ignores_non_string_values():
    """A non-string forked_from/private_to (e.g. hand-edited JSON) is not a fork.

    Written straight to the sidecar so the type guard is exercised without
    set_fork_info's str() coercion masking it.
    """
    from kiro_crew import agent_state as _st

    _st._write({"weird": {_st._FORKED_FROM: 123, _st._PRIVATE_TO: ["c"]}})
    assert agent_state.get_fork_info("weird") is None
    assert "weird" not in agent_state.all_fork_info()


def test_all_fork_info_returns_only_valid_entries():
    agent_state.set_fork_info("f1", forked_from="kirocrew", private_to="c1")
    agent_state.set_fork_info("f2", forked_from="kirocrew-lite", private_to="c2")
    # A model-only entry carries no fork keys and must not appear.
    agent_state.set_model_managed("plain", False)

    allf = agent_state.all_fork_info()
    assert allf == {
        "f1": {"forked_from": "kirocrew", "private_to": "c1"},
        "f2": {"forked_from": "kirocrew-lite", "private_to": "c2"},
    }


def test_mutation_refuses_an_unreadable_existing_sidecar(monkeypatch, tmp_path):
    """a mutation must never collapse an EXISTING-but-unreadable
    sidecar to {} and write it back — that silently erases every agent's
    model and lineage state. Only a genuinely missing file reads as empty."""
    import pytest

    sidecar = tmp_path / "agent_model_state.json"
    sidecar.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(agent_state, "_state_path", lambda: sidecar)

    with pytest.raises(ValueError):
        agent_state.set_fork_info("a-crew", forked_from="kirocrew", private_to="a-crew")
    with pytest.raises(ValueError):
        agent_state.set_model_managed("a-crew", True)
    with pytest.raises(ValueError):
        agent_state.prune("a-crew")
    # The corrupt bytes are still there for a human to recover — not replaced.
    assert sidecar.read_text(encoding="utf-8") == "{not json"

    # A MISSING file stays the ordinary empty case: mutations create it.
    sidecar.unlink()
    agent_state.set_fork_info("a-crew", forked_from="kirocrew", private_to="a-crew")
    assert agent_state.get_fork_info("a-crew") == {
        "forked_from": "kirocrew",
        "private_to": "a-crew",
    }


def test_strict_get_fork_info_surfaces_corruption_while_lenient_degrades(monkeypatch, tmp_path):
    """Opus round-24 advisory: the spawn gate's fail-closed branch needs the
    strict read to be reachable; display callers keep the lenient default."""
    import pytest

    sidecar = tmp_path / "agent_model_state.json"
    sidecar.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(agent_state, "_state_path", lambda: sidecar)

    assert agent_state.get_fork_info("any") is None  # lenient default
    with pytest.raises(ValueError):
        agent_state.get_fork_info("any", strict=True)


# --------------------------------------------------------------------------- #
# managed_digest -- the installer-recorded ownership record (GPT 6.1 F1).
# --------------------------------------------------------------------------- #


def test_spec_digest_matches_the_bytes_the_installer_writes():
    """``spec_digest`` hashes the CANONICAL bytes the atomic writer lands --
    ``json.dumps(indent=2)`` + a trailing newline -- so a spec read back and re-serialized
    the same way reproduces the recorded value. No ``sort_keys`` (the writer does not sort)."""
    import hashlib
    import json

    config = {"name": "kirocrew-dashboard-author", "mcpServers": {"kirocrew-core": {}}}
    expected = hashlib.sha256((json.dumps(config, indent=2) + "\n").encode("utf-8")).hexdigest()
    assert agent_state.spec_digest(config) == expected


def test_managed_digest_roundtrips_and_clears():
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None
    agent_state.set_managed_digest("kirocrew-dashboard-author", "deadbeef")
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") == "deadbeef"
    agent_state.set_managed_digest("kirocrew-dashboard-author", None)
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None


def test_managed_digest_survives_alongside_model_bookkeeping():
    agent_state.set_model_managed("kirocrew-dashboard-author", True)
    agent_state.set_managed_digest("kirocrew-dashboard-author", "cafe1234")
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") == "cafe1234"
    assert agent_state.get_model_managed("kirocrew-dashboard-author") is True


def test_prune_drops_the_managed_digest():
    agent_state.set_managed_digest("x", "feed0000")
    agent_state.prune("x")
    assert agent_state.get_managed_digest("x") is None


def test_strict_get_managed_digest_surfaces_corruption_while_lenient_degrades(
    monkeypatch, tmp_path
):
    """A caller whose answer decides an OVERWRITE reads strict: an unreadable sidecar must
    raise (fail closed), not degrade to 'no ownership' and let the file be rewritten. Display
    callers keep the lenient default."""
    import pytest

    sidecar = tmp_path / "agent_model_state.json"
    sidecar.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(agent_state, "_state_path", lambda: sidecar)

    assert agent_state.get_managed_digest("any") is None  # lenient default
    with pytest.raises((ValueError, OSError)):
        agent_state.get_managed_digest("any", strict=True)


# --------------------------------------------------------------------------- #
# Two-phase managed-write digest (GPT 6.1): the file reproduces one of the two
# recorded digests at every instant, so a crash never freezes a managed spec.
# --------------------------------------------------------------------------- #


def test_managed_digest_matches_finalized_or_pending():
    name = "kirocrew-dashboard-author"
    old, new = "oldoldold", "newnewnew"
    agent_state.set_managed_digest(name, old)
    # Before any write: only the finalized digest matches.
    assert agent_state.managed_digest_matches(name, old) is True
    assert agent_state.managed_digest_matches(name, new) is False
    # begin: pending recorded, finalized still in place -> BOTH match.
    agent_state.begin_managed_write(name, new)
    assert agent_state.managed_digest_matches(name, old) is True  # file still old bytes
    assert agent_state.managed_digest_matches(name, new) is True  # or already new bytes
    # finalize: new promoted, pending cleared -> only new matches.
    agent_state.finalize_managed_write(name)
    assert agent_state.managed_digest_matches(name, new) is True
    assert agent_state.managed_digest_matches(name, old) is False


def test_crash_after_begin_before_write_leaves_old_bytes_confirmable():
    """Process death between begin and the file replace: the file still holds the OLD bytes,
    which reproduce the finalized digest -> still confirmed, so a rebuild refreshes it."""
    name = "kirocrew-dashboard-author"
    old_bytes_digest = "oldbytes"
    agent_state.set_managed_digest(name, old_bytes_digest)
    agent_state.begin_managed_write(name, "newbytes")  # crash here, no file write, no finalize
    assert agent_state.managed_digest_matches(name, old_bytes_digest) is True


def test_crash_after_write_before_finalize_leaves_new_bytes_confirmable():
    """Process death between the file replace and finalize: the file holds the NEW bytes,
    which reproduce the pending digest -> still confirmed, so a rebuild refreshes it."""
    name = "kirocrew-dashboard-author"
    agent_state.set_managed_digest(name, "oldbytes")
    agent_state.begin_managed_write(name, "newbytes")  # file now written to new bytes...
    # ...crash before finalize_managed_write(name)
    assert agent_state.managed_digest_matches(name, "newbytes") is True


def test_finalize_is_a_noop_without_a_pending():
    name = "kirocrew-dashboard-author"
    agent_state.set_managed_digest(name, "keepme")
    agent_state.finalize_managed_write(name)  # nothing pending
    assert agent_state.get_managed_digest(name) == "keepme"


def test_prune_drops_both_digest_slots():
    name = "x"
    agent_state.set_managed_digest(name, "fin")
    agent_state.begin_managed_write(name, "pend")
    agent_state.prune(name)
    assert agent_state.get_managed_digest(name) is None
    assert agent_state.managed_digest_matches(name, "fin") is False
    assert agent_state.managed_digest_matches(name, "pend") is False


def test_begin_carries_the_current_on_disk_digest_so_a_double_interruption_recovers():
    """GPT 6.1 (on 8965bd31bf): a first write finalize-interrupts, leaving finalized=A,
    pending=B, FILE=B. The next write's begin must keep the file (B) confirmable even if its
    own replace then fails. ``begin`` writes the CURRENT on-disk digest (B) to finalized
    before setting the new pending (C), so the worst case is finalized=B, pending=C, file=B
    -- B still matches, so the spec is recoverable rather than frozen."""
    name = "kirocrew-dashboard-author"
    A, B, C = "digestA", "digestB", "digestC"
    # State after a finalize-interrupted first write: finalized=A, pending=B, file=B.
    agent_state.set_managed_digest(name, A)
    agent_state.begin_managed_write(name, B)  # file then written to B, finalize interrupted
    assert agent_state.managed_digest_matches(name, B) is True  # file=B confirms (pending)

    # Second write begins, passing the CURRENT on-disk digest (B) as `current`...
    agent_state.begin_managed_write(name, C, current=B)
    # ...and its replace FAILS before finalize. The file is still B.
    assert agent_state.managed_digest_matches(name, B) is True  # recoverable: B is finalized now
    assert agent_state.managed_digest_matches(name, C) is True  # or the new bytes, if written


def test_begin_without_current_only_sets_pending():
    """A first install (nothing on disk) passes no ``current``: only the pending slot is set,
    no spurious finalized digest is invented for bytes that are not there yet."""
    name = "kirocrew-dashboard-author"
    agent_state.begin_managed_write(name, "firstbytes")
    assert agent_state.managed_digest_matches(name, "firstbytes") is True
    assert agent_state.get_managed_digest(name) is None  # finalized still unset
