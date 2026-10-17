"""Two seams of ``hook_runtime/script_hooks.py`` that the move from ``hooks.py`` touched.

The skills-only directive ``ScriptHookStore.fire`` writes names each skill by its last
segment, spelled ``s.rpartition("/")[2]``: the cross-platform gate reads a ``.split("/")``
under ``hook_runtime/`` as a hand-joined path, and a skill key is a ``category/name`` key,
not a path. That spelling and ``s.split("/")[-1]`` both answer the text after the last
``/``, or the whole string when there is none.

The store's mutation guard is a ``@contextmanager``, whose generator ``compose`` does not
rebind; ``hooks.py`` rebinds it after ``compose``, so a patch through ``kiro_crew.hooks``
reaches it like every other store method.

The classes the two owners define keep their owner's ``__module__``: ``compose`` rebinds
the methods of exactly the classes whose module is the owner's, so a class reporting
``kiro_crew.hooks`` would keep its methods on the owner's globals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import hooks
from kiro_crew.hook_runtime import hook_types, script_hooks
from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT, ScriptHookStore


@pytest.mark.parametrize("key", ["", "a", "a/b", "/a/", "a//b"])
def test_the_last_segment_spelling_matches_the_split_it_replaces(key: str) -> None:
    assert key.rpartition("/")[2] == key.split("/")[-1]


@pytest.mark.asyncio
async def test_a_skills_only_hook_names_each_skill_by_its_last_segment(tmp_path: Path) -> None:
    store = ScriptHookStore(tmp_path)
    store.create(
        {
            "name": "segments",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "command": "",
            "matcher": "*deploy*",
            "skills": ["kirocrew-dev/kirocrew-prepare-pr", "a//b", "plain"],
        }
    )
    results = await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "please deploy this")
    assert [r.stdout for r in results] == ["Load skills: $kirocrew-prepare-pr $b $plain"]


def test_the_store_mutation_guard_reads_the_facade_globals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``compose`` rebinds functions, not the generator a ``@contextmanager`` wraps, so
    ``hooks.py`` rebinds ``ScriptHookStore._atomic_mutation`` itself: a patch of
    ``hooks.copy`` reaches the snapshot the guard takes, as it did when the store lived
    in ``hooks.py``."""
    snapshots: list[str] = []
    real = hooks.copy

    class _RecordingCopy:
        @staticmethod
        def deepcopy(value: object) -> object:
            snapshots.append(type(value).__name__)
            return real.deepcopy(value)

    monkeypatch.setattr(hooks, "copy", _RecordingCopy)
    store = ScriptHookStore(tmp_path)
    store.create({"name": "guarded", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "true"})
    assert snapshots


@pytest.mark.parametrize("owner", [hook_types, script_hooks], ids=lambda m: m.__name__)
def test_the_moved_classes_keep_the_module_compose_selects_them_by(owner: object) -> None:
    classes = {
        name: value
        for name, value in vars(owner).items()
        if isinstance(value, type)
        and value.__qualname__ == name
        and name in owner.__dict__
        and getattr(value, "__module__", None) in {owner.__name__, hooks.__name__}
    }
    assert classes, "no class was found in the owner, so this case measured nothing"
    assert {name: cls.__module__ for name, cls in classes.items()} == dict.fromkeys(
        classes, owner.__name__
    )
    for cls in classes.values():
        for member in vars(cls).values():
            fn = getattr(member, "__func__", member)
            if callable(fn) and getattr(fn, "__code__", None) is not None:
                if fn.__code__.co_filename == owner.__file__:
                    assert fn.__globals__ is vars(hooks), f"{cls.__name__}.{fn.__name__}"
