"""The floor's patcher leaves the environment exactly as two redundant writes did.

``_FloorMonkeyPatch`` skips an environment write that changes nothing and still
leaves the undo record the write would have left. These tests run generated
programs twice, once through it and once through a plain ``MonkeyPatch`` (which
takes the floor's old two-write path), on a private environment that counts its
writes, and require the same environment at every point a test, a fixture's
teardown or the next test could read it.

No real environment variable is touched: each run swaps ``os.environ`` for a
private mapping for its own duration.
"""

from __future__ import annotations

import os
import random

import conftest_floor_home as floor
import pytest

_SEEDS = range(60)
#: The eight variables the home floor clears and sweeps.
_SWEPT = (
    "KIROCREW_PROJECT_DIR",
    "KIROCREW_PORT",
    "KIROCREW_BOUND_PORT",
    "KIROCREW_SANDBOX_ACTIVE",
    "KIROCREW_SANDBOX_LEVEL",
    "KIROCREW_DEV_MODE",
    "KIROCREW_STRICT_ON_LOOP_PERSIST",
    "KIROCREW_SCRATCH_ROOT",
)
_CONSTANTS = {
    "KIROCREW_TELEMETRY": "0",
    "KIROCREW_PROFILE": "standalone",
    "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
    "GIT_AUTHOR_NAME": "Test",
    "XDG_CONFIG_HOME": "/session/xdg",
}
_PER_TEST = ("KIROCREW_HOME", "KIROCREW_WORKSPACE", "OLLAMA_MODELS")
#: Deleted by the floor and normally absent; on Windows the two spellings are one key.
_NORMALLY_ABSENT = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "KIROCREW_VENV")
_VALUES = ("", "0", "1", "standalone", "Test", "/some/where", "left-by-the-test")


def _expect_absent(patcher, name: str) -> None:
    """Remove *name* for the test, and at undo whatever the test left in it.

    The floor's patcher has a method for it. A plain ``MonkeyPatch`` needs two
    writes for the same record: ``delenv`` on an absent variable records nothing,
    so the variable is set to ``""`` first, which leaves the record "was absent".
    """
    if isinstance(patcher, floor._FloorMonkeyPatch):
        patcher.expect_absent(name)
    elif name in os.environ:
        patcher.delenv(name)
    else:
        patcher.setenv(name, "")
        patcher.delenv(name)


#: What the worker held in each swept variable when this module was imported, at
#: collection, before any floor ran.
_INHERITED = {name: os.environ.get(name) for name in _SWEPT}


class _Env(dict):
    """A private ``os.environ`` that counts the writes made to it."""

    writes = 0

    def __setitem__(self, name, value):
        self.writes += 1
        super().__setitem__(self._key(name), value)

    def __delitem__(self, name):
        self.writes += 1  # ``os.environ`` unsets before it looks, so this is a write too
        super().__delitem__(self._key(name))

    def __getitem__(self, name):
        return super().__getitem__(self._key(name))

    def __contains__(self, name):
        return super().__contains__(self._key(name))

    def get(self, name, default=None):
        return super().get(self._key(name), default)

    def pop(self, name, *default):
        if name in self:
            value = self[name]
            del self[name]
            return value
        if default:
            return default[0]
        raise KeyError(name)

    @staticmethod
    def _key(name):
        return name


class _FoldingEnv(_Env):
    """The same, with keys folded to upper case as ``os.environ`` folds them on Windows."""

    @staticmethod
    def _key(name):
        return name.upper()


def _program(rng: random.Random):
    """``(inherited environment, the floor's setup steps, what the test does)``."""
    names = [*_SWEPT, *_CONSTANTS, *_PER_TEST, *_NORMALLY_ABSENT]
    inherited = {name: rng.choice(_VALUES) for name in rng.sample(names, rng.randrange(0, 9))}
    for name, value in _CONSTANTS.items():
        if rng.random() < 0.5:
            inherited[name] = value  # already held, as a session fixture leaves it
    setup = [("absent", name, None) for name in _SWEPT]
    setup += [("setenv", name, value) for name, value in _CONSTANTS.items()]
    setup += [("setenv", name, f"/per-test/{rng.randrange(10**6)}") for name in _PER_TEST]
    setup += [("delenv", name, None) for name in _NORMALLY_ABSENT]
    rng.shuffle(setup)
    actions = [
        (
            rng.choice(["raw_set", "raw_del", "setenv", "delenv", "undo", "nothing"]),
            rng.choice(names),
            rng.choice(_VALUES),
        )
        for _ in range(rng.randrange(0, 14))
    ]
    return inherited, setup, actions


def _run(patcher_type, env_type, program) -> tuple[list[dict], int]:
    """The environment after setup, after each action, after the test's own undo and
    after the floor's undo; and the number of writes the floor itself made."""
    inherited, setup, actions = program
    env = env_type()
    for name, value in inherited.items():
        dict.__setitem__(env, env._key(name), value)
    seen: list[dict] = []
    with pytest.MonkeyPatch.context() as outer:
        outer.setattr(os, "environ", env)
        patcher = patcher_type()
        for kind, name, value in setup:
            if kind == "absent":
                _expect_absent(patcher, name)
            elif kind == "setenv":
                patcher.setenv(name, value)
            else:
                patcher.delenv(name, raising=False)
        floor_writes = env.writes
        seen.append(dict(env))
        own = pytest.MonkeyPatch()
        for kind, name, value in actions:
            env.writes = 0
            if kind == "raw_set":
                os.environ[name] = value  # flake-ok: the raw write under test, on a private mapping
            elif kind == "raw_del":
                os.environ.pop(name, None)
            elif kind == "setenv":
                own.setenv(name, value)
            elif kind == "delenv":
                own.delenv(name, raising=False)
            elif kind == "undo":
                own.undo()
            seen.append(dict(env))
        own.undo()
        seen.append(dict(env))  # what a fixture torn down after the test's patcher reads
        env.writes = 0
        patcher.undo()
        floor_writes += env.writes
        seen.append(dict(env))  # what the next test inherits
    return seen, floor_writes


@pytest.mark.parametrize("env_type", [_Env, _FoldingEnv])
@pytest.mark.parametrize("seed", _SEEDS)
def test_the_environment_is_the_same_at_every_point_with_fewer_writes(seed, env_type):
    program = _program(random.Random(seed))
    plain, plain_writes = _run(pytest.MonkeyPatch, env_type, program)
    ours, our_writes = _run(floor._FloorMonkeyPatch, env_type, program)
    assert (
        ours == plain
    ), f"seed {seed}: the environment differs at step {_first_difference(ours, plain)}"
    assert our_writes <= plain_writes


def _first_difference(ours, plain) -> int:
    return next(i for i, (a, b) in enumerate(zip(ours, plain)) if a != b)


@pytest.mark.parametrize("env_type", [_Env, _FoldingEnv])
@pytest.mark.parametrize("seed", _SEEDS)
def test_the_next_test_inherits_every_variable_the_floor_answers_for(seed, env_type):
    """Whatever the test did, raw or through its own patcher, and wherever it stopped.

    The floor answers for the variables it sweeps, the ones it sets, and the ones it
    found present and deleted. A variable it deleted while absent is outside that,
    with either patcher: ``delenv`` on an absent variable records nothing.
    """
    program = _program(random.Random(seed))
    inherited = env_type()
    for name, value in program[0].items():
        dict.__setitem__(inherited, env_type._key(name), value)
    seen, _ = _run(floor._FloorMonkeyPatch, env_type, program)
    answered = {env_type._key(name) for name in (*_SWEPT, *_CONSTANTS, *_PER_TEST)}
    answered |= {env_type._key(name) for name in _NORMALLY_ABSENT if name in inherited}
    for key in answered:
        assert seen[-1].get(key) == dict.get(inherited, key), key
    for name in _SWEPT:
        assert env_type._key(name) not in seen[0], "a swept variable was visible to the test"


@pytest.mark.parametrize("env_type", [_Env, _FoldingEnv])
def test_a_test_that_touches_nothing_costs_no_write_for_the_two_redundant_shapes(env_type):
    """Eight absent variables to sweep and three constants already held: no write at all,
    where the two-write path makes four per swept variable and two per held constant."""
    held = {
        "KIROCREW_TELEMETRY": "0",
        "KIROCREW_PROFILE": "standalone",
        "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
    }
    setup = [("absent", name, None) for name in _SWEPT]
    setup += [("setenv", name, value) for name, value in held.items()]
    program = (held, setup, [])
    plain, plain_writes = _run(pytest.MonkeyPatch, env_type, program)
    ours, our_writes = _run(floor._FloorMonkeyPatch, env_type, program)
    assert ours == plain
    assert plain_writes == 4 * len(_SWEPT) + 2 * len(held)
    assert our_writes == 0


@pytest.mark.parametrize("left", ["", "8080"])
def test_a_value_the_test_leaves_in_a_swept_variable_is_gone_after_the_floor(left):
    program = (
        {},
        [("absent", "KIROCREW_BOUND_PORT", None)],
        [("raw_set", "KIROCREW_BOUND_PORT", left)],
    )
    seen, _ = _run(floor._FloorMonkeyPatch, _Env, program)
    assert seen[1] == {"KIROCREW_BOUND_PORT": left} and seen[2] == {"KIROCREW_BOUND_PORT": left}
    assert seen[-1] == {}


def test_an_inherited_value_of_a_swept_variable_is_hidden_and_then_restored():
    program = (
        {"KIROCREW_PORT": "7000"},
        [("absent", "KIROCREW_PORT", None)],
        [("raw_set", "KIROCREW_PORT", "9000")],
    )
    seen, _ = _run(floor._FloorMonkeyPatch, _Env, program)
    assert seen[0] == {} and seen[-1] == {"KIROCREW_PORT": "7000"}


@pytest.mark.parametrize("action", ["raw_set", "raw_del"])
def test_a_held_constant_the_test_changes_or_deletes_is_put_back(action):
    program = (
        {"KIROCREW_PROFILE": "standalone"},
        [("setenv", "KIROCREW_PROFILE", "standalone")],
        [(action, "KIROCREW_PROFILE", "enterprise")],
    )
    seen, writes = _run(floor._FloorMonkeyPatch, _Env, program)
    assert seen[1] != seen[0]
    assert seen[-1] == {"KIROCREW_PROFILE": "standalone"}
    assert writes == 1, "the restore is the one write this cycle needs"


def test_the_undo_writes_to_the_environment_the_record_was_made_against(monkeypatch):
    """As a record on ``os.environ`` does, even if ``os.environ`` is another object by then."""
    first, second = _Env(), _Env()
    monkeypatch.setattr(os, "environ", first)
    patcher = floor._FloorMonkeyPatch()
    patcher.expect_absent("KIROCREW_PORT")
    first["KIROCREW_PORT"] = "left"
    monkeypatch.setattr(os, "environ", second)
    second["KIROCREW_PORT"] = "in another environment"
    patcher.undo()
    assert "KIROCREW_PORT" not in first
    assert second == {"KIROCREW_PORT": "in another environment"}


def test_a_prepending_setenv_is_never_skipped(monkeypatch):
    env = _Env({"PATH_LIKE": "a"})
    monkeypatch.setattr(os, "environ", env)
    patcher = floor._FloorMonkeyPatch()
    patcher.setenv("PATH_LIKE", "a", prepend=":")
    assert env["PATH_LIKE"] == "a:a"
    patcher.undo()
    assert env == {"PATH_LIKE": "a"}


def _records(patcher) -> tuple[set[str], dict[str, str]]:
    """``(variables with a "was absent" record, variables with an inherited value to restore)``.

    The two records the floor leaves on the real environment: the write-free one
    for a variable that was absent, and the ordinary ``delenv`` record, which
    carries the value, for one that was present.
    """
    absent = {
        name
        for target, name, value in patcher._setitem
        if isinstance(target, floor._EnvUndo)
        and target.environ is os.environ
        and value is floor._ABSENT
    }
    present = {
        name: value
        for target, name, value in patcher._setitem
        if target is os.environ and isinstance(value, str)
    }
    return absent, present


def test_the_live_floor_runs_on_the_floors_own_patcher(_floor_monkeypatch):
    """This test's own floor: the eight variables are absent, and each has its undo record.

    Which record depends on what the worker inherited. A shell that exports one of
    them (an agent sandbox exports its markers) makes the floor record that value to
    restore; a variable the worker did not have gets the "was absent" record.
    """
    assert isinstance(_floor_monkeypatch, floor._FloorMonkeyPatch)
    absent, present = _records(_floor_monkeypatch)
    for name in _SWEPT:
        assert name not in os.environ
        if _INHERITED[name] is None:
            assert name in absent, f"{name} would not be swept after this test"
        else:
            assert present.get(name) == _INHERITED[name], f"{name} would not be restored"


@pytest.mark.parametrize(
    "inherited",
    [
        (),
        ("KIROCREW_SANDBOX_ACTIVE",),
        ("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL", "KIROCREW_PORT"),
        _SWEPT,
    ],
    ids=["none", "one", "three", "all eight"],
)
def test_the_floor_answers_for_all_eight_whichever_of_them_the_worker_inherited(inherited):
    """The real environment, with some of the eight set before the floor runs.

    Each of the eight gets a record whether it was inherited or not, is hidden from
    the test, and afterwards is back at its inherited value or absent again, with
    whatever the test left in it gone.
    """
    with pytest.MonkeyPatch.context() as worker:
        for name in _SWEPT:
            worker.delenv(name, raising=False)
        for name in inherited:
            worker.setenv(name, f"inherited-{name}")
        patcher = floor._FloorMonkeyPatch()
        own = pytest.MonkeyPatch()
        try:
            for name in _SWEPT:
                _expect_absent(patcher, name)
            absent, present = _records(patcher)
            assert absent == set(_SWEPT) - set(inherited)
            assert {name: present.get(name) for name in inherited} == {
                name: f"inherited-{name}" for name in inherited
            }
            for name in _SWEPT:
                assert name not in os.environ
                own.setenv(name, "left-by-the-test")
        finally:
            own.undo()  # the test's own patcher first, as pytest undoes it
            patcher.undo()
        for name in _SWEPT:
            assert os.environ.get(name) == (f"inherited-{name}" if name in inherited else None)


_OUTER = ("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL", "PYTHONUTF8", "PYTHONIOENCODING")


def _floor_patcher_cycle():
    """One cycle of the real ``_floor_monkeypatch`` fixture, as a generator."""
    return getattr(floor._floor_monkeypatch, "__wrapped__", floor._floor_monkeypatch)()


@pytest.mark.parametrize("env_type", [_Env, _FoldingEnv])
def test_the_workers_outer_variables_cost_no_write_when_the_test_left_them_alone(
    monkeypatch, env_type
):
    """The fixture puts four of the worker's own variables back after its undo.

    Two are set and two are absent here. A test that changed none of them costs no
    write, and one that changed a set one and created an absent one costs exactly
    the two writes that put them right.
    """
    env = env_type()
    dict.__setitem__(env, "PYTHONUTF8", "1")
    dict.__setitem__(env, "PYTHONIOENCODING", "utf-8")
    monkeypatch.setattr(os, "environ", env)
    held = dict(env)

    cycle = _floor_patcher_cycle()
    next(cycle)
    env.writes = 0
    with pytest.raises(StopIteration):
        next(cycle)
    assert dict(env) == held
    assert env.writes == 0, "an untouched variable was written back"

    cycle = _floor_patcher_cycle()
    next(cycle)
    dict.__setitem__(env, "PYTHONUTF8", "0")
    dict.__setitem__(env, "KIROCREW_SANDBOX_ACTIVE", "1")
    env.writes = 0
    with pytest.raises(StopIteration):
        next(cycle)
    assert dict(env) == held
    assert env.writes == 2, "one restore of a changed value and one removal"
