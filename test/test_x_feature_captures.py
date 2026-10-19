"""The X-capture rule in ``scripts/check_harness_parity.py``.

A non-standard capability set must cite, per member, a live capture under
``test/fixtures/acp_frames/<backend>/`` that a test loads; KAS cites ``kas/``.
Today's gaps sit in a shrink-only allowlist. Contract:
``docs/ci/harness-parity-gate.md``.
"""

from __future__ import annotations

import importlib.util
import os
import sys

_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "check_harness_parity.py"
)
_spec = importlib.util.spec_from_file_location("check_harness_parity_x", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

# The ceiling only moves down. Raising it is the change this test exists to stop.
X_CAPTURE_CEILING = 18
KIRO_KAS_PAIR_CEILING = 3


def test_tree_passes() -> None:
    assert gate.enforce_x_captures(None) == 0


def test_allowlists_stay_under_their_ceiling() -> None:
    assert len(gate.X_CAPTURE_ALLOWLIST) <= X_CAPTURE_CEILING
    assert len(gate.KIRO_KAS_PAIR_ALLOWLIST) <= KIRO_KAS_PAIR_CEILING
    assert all(why.strip() for why in gate.X_CAPTURE_ALLOWLIST.values())


def test_pi_joining_steering_request_without_a_capture_fails() -> None:
    """pi added to STEERING_REQUEST with no pi capture cited."""
    sets = gate.live_sets()
    name = "ACP_BACKENDS_STEERING_REQUEST"
    sets[name] = sets[name] | {"pi"}
    gaps = gate.check_x_captures(sets, gate.set_comments())
    assert (name, "pi") in gaps
    assert (name, "pi") not in gate.X_CAPTURE_ALLOWLIST


def test_kas_citing_a_kiro_capture_fails() -> None:
    comments = gate.set_comments()
    name = "ACP_BACKENDS_STEER"
    assert "acp_frames/kas/steering-live.jsonl" in comments[name]
    comments[name] = comments[name].replace("acp_frames/kas/", "acp_frames/kiro/")
    gaps = gate.check_x_captures(gate.live_sets(), comments)
    assert "kiro-cli is not KAS" in gaps[(name, "kas")]


def test_a_capture_is_read_with_its_replay_snapshot() -> None:
    meta, name = gate.read_capture(gate.REPO_ROOT, "kas/steering-live.jsonl")
    assert meta["recorded"] == "live" and meta["backend"] == "kas" and meta["replayed"]
    assert gate.read_capture(gate.REPO_ROOT, "pi/steer-live.jsonl") is None
    _, name = gate.read_capture(gate.REPO_ROOT, "pi/session-live.jsonl")
    assert name == "pi-acp"  # from ``initialize.agentInfo`` when the header has none


def test_the_self_test_passes() -> None:
    assert gate._x_capture_self_test() == 0


def test_an_added_allowlist_entry_is_refused(monkeypatch) -> None:
    entries = set(gate.X_CAPTURE_ALLOWLIST)
    entries.discard(("ACP_BACKENDS_STEER", "kiro"))
    pairs, x_sets = set(gate.KIRO_KAS_PAIR_ALLOWLIST), dict(gate.X_FEATURE_SETS)
    monkeypatch.setattr(gate, "_base_allowlists", lambda base: (entries, pairs, x_sets))
    assert gate.enforce_x_captures("base") == 1


def test_dropping_an_x_set_is_refused(monkeypatch) -> None:
    prior = (
        set(gate.X_CAPTURE_ALLOWLIST),
        set(gate.KIRO_KAS_PAIR_ALLOWLIST),
        dict(gate.X_FEATURE_SETS),
    )
    monkeypatch.setattr(gate, "_base_allowlists", lambda base: prior)
    assert gate.enforce_x_captures("base") == 0
    monkeypatch.delitem(gate.X_FEATURE_SETS, "ACP_BACKENDS_MARKDOWN_AGENT_SPECS")
    monkeypatch.delitem(gate.X_CAPTURE_ALLOWLIST, ("ACP_BACKENDS_MARKDOWN_AGENT_SPECS", "kas"))
    assert gate.enforce_x_captures("base") == 1


def test_a_proven_member_left_on_the_allowlist_is_refused(monkeypatch) -> None:
    monkeypatch.setitem(gate.X_CAPTURE_ALLOWLIST, ("ACP_BACKENDS_STEER", "kas"), "stale")
    assert gate.enforce_x_captures(None) == 1


def test_base_allowlists_are_read_back_from_the_gate_source(monkeypatch) -> None:
    with open(_GATE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    monkeypatch.setattr(gate, "git", lambda args: source)
    x, pairs, x_sets = gate._base_allowlists("base")
    assert x == set(gate.X_CAPTURE_ALLOWLIST)
    assert pairs == set(gate.KIRO_KAS_PAIR_ALLOWLIST)
    assert x_sets == {k: (None if v is None else set(v)) for k, v in gate.X_FEATURE_SETS.items()}


def test_narrowing_an_x_set_scope_is_refused(monkeypatch) -> None:
    """Scoping pi out of STEERING_REQUEST would let it join with no capture."""
    prior = (
        set(gate.X_CAPTURE_ALLOWLIST),
        set(gate.KIRO_KAS_PAIR_ALLOWLIST),
        dict(gate.X_FEATURE_SETS),
    )
    monkeypatch.setattr(gate, "_base_allowlists", lambda base: prior)
    name = "ACP_BACKENDS_STEERING_REQUEST"
    monkeypatch.setitem(gate.X_FEATURE_SETS, name, frozenset({"claude", "codex", "goose"}))
    assert gate.enforce_x_captures("base") == 1
    monkeypatch.setitem(gate.X_FEATURE_SETS, name, None)
    monkeypatch.setitem(gate.X_FEATURE_SETS, "ACP_BACKENDS_MEMBER_DISPATCH", None)
    assert gate._narrowed(prior[2]) == []  # widening a scope is always allowed


def test_an_emptied_allowlist_is_read_back_as_empty(monkeypatch) -> None:
    with open(_GATE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("KIRO_KAS_PAIR_ALLOWLIST: frozenset[str] = frozenset(")
    end = source.index("\n)\n", start) + 3
    source = (
        source[:start] + "KIRO_KAS_PAIR_ALLOWLIST: frozenset[str] = frozenset()\n" + source[end:]
    )
    monkeypatch.setattr(gate, "git", lambda args: source)
    _, pairs, _ = gate._base_allowlists("base")
    assert pairs == set()


def test_the_shrink_check_reads_the_merge_base_not_the_base_tip(monkeypatch) -> None:
    seen = []
    monkeypatch.setenv("HARNESS_BASE_REF", "origin/main")
    monkeypatch.setattr(gate, "diff_base", lambda base: "forkpoint")
    monkeypatch.setattr(gate, "enforce_diff", lambda base: 0)
    monkeypatch.setattr(gate, "enforce_x_captures", lambda base: seen.append(base) or 0)
    assert gate.main([]) == 0
    assert seen == ["forkpoint"]
