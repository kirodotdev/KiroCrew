"""One session start answers ``may_skip_gate_now`` once per ref.

A skill-view preparation asks it for every agent it projects. Inside one
``build_reuse.one_build`` block the first answer per ref is reused; an answer that
raises is not kept, the next block asks again, and so does every call outside one.
"""

from __future__ import annotations

import pytest

from kiro_crew.build_reuse import one_build
from kiro_crew.platform import governance


@pytest.fixture
def policy(monkeypatch):
    asked: list[str] = []
    answers: dict[str, list[object]] = {}

    def policy_may_skip(ref: str) -> bool:
        asked.append(ref)
        answer = answers[ref].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return bool(answer)

    monkeypatch.setattr(governance, "_policy_may_skip_gate_now", policy_may_skip)
    monkeypatch.setattr(governance, "_operator_requires_confirmation", lambda _ref: False)
    monkeypatch.setattr(governance, "_operator_denies", lambda _ref: False)
    return asked, answers


def test_one_start_answers_each_ref_once(policy):
    asked, answers = policy
    answers.update({"@a": [True, False], "@b": [False]})

    with one_build():
        assert [governance.may_skip_gate_now("@a") for _ in range(3)] == [True] * 3
        assert governance.may_skip_gate_now("@b") is False

    assert asked == ["@a", "@b"]


def test_the_next_start_and_calls_outside_a_start_ask_again(policy):
    asked, answers = policy
    answers["@a"] = [True, False, True, False]

    with one_build():
        assert governance.may_skip_gate_now("@a") is True
    with one_build():
        assert governance.may_skip_gate_now("@a") is False
    assert governance.may_skip_gate_now("@a") is True
    assert governance.may_skip_gate_now("@a") is False

    assert len(asked) == 4


def test_an_answer_that_raises_is_not_kept(policy):
    asked, answers = policy
    answers["@a"] = [RuntimeError("ceiling unreadable"), False]

    with one_build():
        with pytest.raises(RuntimeError):
            governance.may_skip_gate_now("@a")
        assert governance.may_skip_gate_now("@a") is False

    assert len(asked) == 2
