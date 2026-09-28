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
