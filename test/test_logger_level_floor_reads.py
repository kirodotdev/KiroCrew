"""The logger-level floor's census read restores exactly what its full snapshot restores.

``_restore_logger_levels`` puts every logger's level and ``disabled`` flag back after
each test. It reads them through a census of the live ``Logger`` objects and takes
the per-name restore only when something differs. These tests run generated logger
trees through generated changes twice, once around the floor and once around the
full-snapshot cycle built from the floor's own slow-path pieces, and require the
same final state both times.

Each run uses a private ``logging.Manager`` and root logger, so no real logger is
touched.
"""

from __future__ import annotations

import logging
import random

import conftest_floor_restores as floor
import pytest

_SEEDS = range(40)
_LEVELS = (logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING, logging.CRITICAL)
_NAMES = ("a", "a.b", "a.b.c", "a.x", "d", "d.e.f", "g.h", "kiro_crew", "kiro_crew.cli", "z.y.x.w")


def _full_snapshot_cycle():
    """The floor as a full snapshot on each side, from its own slow-path pieces."""
    before_disable = logging.Logger.manager.disable
    before = floor._logger_levels()
    yield
    floor._put_logger_levels_back(before)
    if logging.Logger.manager.disable != before_disable:
        logging.disable(before_disable)


def _state() -> dict[str, tuple]:
    """Every entry of ``loggerDict``, the root logger and the manager's cut-off."""
    manager = logging.Logger.manager
    state: dict[str, tuple] = {
        name: (
            (type(obj).__name__, obj.level, obj.disabled)
            if isinstance(obj, logging.Logger)
            else (type(obj).__name__,)
        )
        for name, obj in manager.loggerDict.items()
    }
    root = logging.getLogger()
    state[""] = ("RootLogger", root.level, root.disabled)
    state["<disable>"] = (manager.disable,)
    return state


def _populate(rng: random.Random) -> None:
    for name in rng.sample(_NAMES, rng.randrange(0, len(_NAMES))):
        logger = logging.getLogger(name)
        logger.setLevel(rng.choice(_LEVELS))
        logger.disabled = rng.random() < 0.2
    logging.getLogger().setLevel(rng.choice(_LEVELS))


def _change(rng: random.Random) -> str:
    """Change the logging state the way a test can, and say how."""
    manager = logging.Logger.manager
    loggers = [obj for obj in manager.loggerDict.values() if isinstance(obj, logging.Logger)]
    kind = rng.choice(
        ["level", "assign", "disable", "create", "configure", "root", "cutoff", "evict", "nothing"]
    )
    if kind == "create":
        logging.getLogger(rng.choice(_NAMES) + rng.choice(["", ".new", ".new.deeper"]))
    elif kind == "configure":
        logger = logging.getLogger(rng.choice(_NAMES) + ".configured")
        logger.setLevel(rng.choice(_LEVELS))
        logger.disabled = True
    elif kind == "root":
        logging.getLogger().setLevel(rng.choice(_LEVELS))
    elif kind == "cutoff":
        logging.disable(rng.choice(_LEVELS))
    elif kind == "evict" and manager.loggerDict:
        del manager.loggerDict[rng.choice(sorted(manager.loggerDict))]
    elif loggers and kind == "level":
        rng.choice(loggers).setLevel(rng.choice(_LEVELS))
    elif loggers and kind == "assign":
        rng.choice(loggers).level = rng.choice(_LEVELS)
    elif loggers and kind == "disable":
        logger = rng.choice(loggers)
        logger.disabled = not logger.disabled
    return kind


def _run(cycle_factory, seed: int, changes: int) -> tuple[dict, dict, int]:
    """``(state inherited, state after the cycle, per-name restores taken)`` in a private tree."""
    rng = random.Random(seed)
    root = logging.RootLogger(logging.WARNING)
    restores: list[dict] = []
    put_back = floor._put_logger_levels_back
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logging.Logger, "manager", logging.Manager(root))
        patch.setattr(logging.Logger, "root", root)
        patch.setattr(logging, "root", root)
        # The live floor around this test holds the real tree's census; hand it back.
        patch.setattr(floor, "_LOGGER_CENSUS", None)
        patch.setattr(
            floor,
            "_put_logger_levels_back",
            lambda before: (restores.append(before), put_back(before)),
        )
        _populate(rng)
        inherited = _state()
        cycle = cycle_factory()
        next(cycle)
        for _ in range(changes):
            _change(rng)
        with pytest.raises(StopIteration):
            next(cycle)
        return inherited, _state(), len(restores)


@pytest.mark.parametrize("seed", _SEEDS)
@pytest.mark.parametrize("changes", [0, 1, 4, 12])
def test_the_census_read_ends_where_the_full_snapshot_ends(seed, changes):
    inherited, after_floor, _ = _run(floor._restore_logger_levels, seed, changes)
    inherited_again, after_full, _ = _run(_full_snapshot_cycle, seed, changes)
    assert inherited_again == inherited, "the two runs did not start from the same tree"
    assert after_floor == after_full


@pytest.mark.parametrize("seed", _SEEDS)
def test_every_inherited_logger_is_back_and_every_new_one_is_pristine(seed):
    inherited, after, _ = _run(floor._restore_logger_levels, seed, 12)
    for name, entry in after.items():
        if entry[0] not in ("Logger", "RootLogger"):
            continue
        if name in inherited and inherited[name][0] == entry[0]:
            assert entry == inherited[name], name
        else:
            assert entry[1:] == floor._PRISTINE_LOGGER, name
    assert after["<disable>"] == inherited["<disable>"]


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_test_that_changes_nothing_takes_no_per_name_restore(seed):
    inherited, after, restores = _run(floor._restore_logger_levels, seed, 0)
    assert after == inherited
    assert restores == 0


def test_a_changed_level_takes_the_per_name_restore():
    def one_level_change():
        cycle = floor._restore_logger_levels()
        next(cycle)
        logging.getLogger("a.b").setLevel(logging.CRITICAL)
        yield
        with pytest.raises(StopIteration):
            next(cycle)

    def as_cycle():
        inner = one_level_change()
        next(inner)
        yield
        with pytest.raises(StopIteration):
            next(inner)

    inherited, after, restores = _run(as_cycle, 3, 0)
    assert restores == 1
    assert inherited["a.b"] == ("PlaceHolder",), "seed 3 no longer leaves a.b uninstantiated"
    assert after["a.b"] == ("Logger", *floor._PRISTINE_LOGGER)


def test_a_census_is_dropped_when_a_placeholder_becomes_a_logger():
    """The entry count and the names stay the same; only the object under one name changes."""
    root = logging.RootLogger(logging.WARNING)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logging.Logger, "manager", logging.Manager(root))
        patch.setattr(logging.Logger, "root", root)
        patch.setattr(logging, "root", root)
        patch.setattr(floor, "_LOGGER_CENSUS", None)
        logging.getLogger("p.q")
        census = floor._logger_census()
        assert census.names == ["p.q"] and floor._logger_census() is census
        logging.getLogger("p")
        assert not census.current()
        assert floor._logger_census().names == ["p.q", "p"]


class _EqualToAnyLogger(logging.Logger):
    """A logger whose ``==`` says yes to every other logger."""

    def __eq__(self, other):
        return isinstance(other, logging.Logger)

    __hash__ = logging.Logger.__hash__


def test_a_logger_replaced_by_one_that_compares_equal_is_still_read():
    """The census is kept by identity: the replacement's level is the one restored."""
    root = logging.RootLogger(logging.WARNING)
    with pytest.MonkeyPatch.context() as patch:
        manager = logging.Manager(root)
        patch.setattr(logging.Logger, "manager", manager)
        patch.setattr(logging.Logger, "root", root)
        patch.setattr(logging, "root", root)
        patch.setattr(floor, "_LOGGER_CENSUS", None)
        logging.getLogger("first")
        logging.getLogger("swapped").setLevel(logging.INFO)
        cycle = floor._restore_logger_levels()
        next(cycle)
        replacement = _EqualToAnyLogger("swapped")
        replacement.setLevel(logging.CRITICAL)
        manager.loggerDict["swapped"] = replacement
        with pytest.raises(StopIteration):
            next(cycle)
        assert replacement.level == logging.INFO
