"""``build_reuse``: an answer is kept only inside one block, only once computed, and
only within the block's budget.

Each test imports the module itself, so a tree without it fails these tests
alone rather than the whole run at collection.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent as agent_mod


def _counter():
    calls: list[int] = []

    def compute():
        calls.append(1)
        return len(calls)

    return calls, compute


def _one(_value) -> int:
    return 1


def test_outside_a_block_every_ask_computes():
    from kiro_crew import build_reuse

    calls, compute = _counter()
    assert [build_reuse.reuse("k", compute, weight=_one) for _ in range(3)] == [1, 2, 3]


def test_inside_a_block_the_first_answer_is_kept_and_dropped_at_the_end():
    from kiro_crew import build_reuse

    calls, compute = _counter()
    with build_reuse.one_build():
        assert [build_reuse.reuse("k", compute, weight=_one) for _ in range(3)] == [1, 1, 1]
        with build_reuse.one_build():
            assert build_reuse.reuse("k", compute, weight=_one) == 1, "nested keeps the outer's"
    with build_reuse.one_build():
        assert build_reuse.reuse("k", compute, weight=_one) == 2, "the next block computes again"


def test_a_computation_that_raises_keeps_nothing():
    from kiro_crew import build_reuse

    calls: list[int] = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient")
        return "read"

    with build_reuse.one_build():
        with pytest.raises(OSError):
            build_reuse.reuse("k", flaky, weight=_one)
        assert build_reuse.reuse("k", flaky, weight=_one) == "read"


def test_an_answer_past_the_budget_is_handed_back_and_not_kept(monkeypatch):
    from kiro_crew import build_reuse

    monkeypatch.setattr(build_reuse, "MAX_KEPT", 2)
    monkeypatch.setattr(build_reuse, "MAX_KEPT_WEIGHT", 10)
    heavy_calls, heavy = _counter()
    calls, compute = _counter()
    with build_reuse.one_build():
        assert build_reuse.reuse("heavy", heavy, weight=lambda _: 11) == 1
        assert build_reuse.reuse("heavy", heavy, weight=lambda _: 11) == 2, "over the weight"
        build_reuse.reuse("a", compute, weight=_one)
        build_reuse.reuse("b", compute, weight=_one)
        assert build_reuse.reuse("c", compute, weight=_one) == 3
        assert build_reuse.reuse("c", compute, weight=_one) == 4, "over the count"
        assert build_reuse.reuse("a", compute, weight=_one) == 1, "what fit is still kept"


def test_the_spec_scan_keeps_only_the_name_and_rereads_a_rewritten_spec(tmp_path):
    from kiro_crew import build_reuse

    path = tmp_path / "a.json"
    path.write_text(json.dumps({"name": "a", "prompt": "x" * 4096}), encoding="utf-8")
    with build_reuse.one_build():
        assert agent_mod._scanned_name(path) == ("a",)
        path.write_text(json.dumps({"name": "renamed"}), encoding="utf-8")
        assert agent_mod._scanned_name(path) == ("renamed",)
        path.write_text(json.dumps({"name": 7}), encoding="utf-8")
        assert agent_mod._scanned_name(path) == (None,)
