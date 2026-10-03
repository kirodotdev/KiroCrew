"""Which ``setup`` hooks the apps runtime dispatches, pinned against the source.

``setup.onUpdate`` parses, validates and round-trips through ``SetupConfig``, but no
code path dispatches it. ``docs/system-specs/modules/app-kit-platform.md`` records
the stance as declared-not-wired: an app whose update correctness depends on it is
broken, and the remedy is an idempotent ``onInstall`` -- which a registry update
re-runs -- not a call site added quietly.

The set of hooks the runtime touches OUTSIDE ``manifest.py``'s own serialization is
derived from the AST rather than read off a comment. Wiring ``onUpdate`` (or
dropping a hook) fails here and names the field comment, the manifest-reference
row and the spec paragraph that must change in the same PR. The reference's
example manifest is held to the same set, because an author copies the example.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from kiro_crew.apps.manifest import SetupConfig

_REPO = Path(__file__).resolve().parent.parent
_APPS = _REPO / "src" / "kiro_crew" / "apps"
_MANIFEST_PY = _APPS / "manifest.py"
_REFERENCE_MD = _REPO / "docs" / "app-kit" / "manifest-reference.md"
_SPEC_MD = _REPO / "docs" / "system-specs" / "modules" / "app-kit-platform.md"

#: Every ``setup.on*`` hook some code path actually executes. ``onInstall`` is
#: read by ``registry_pipeline/install.py`` (the install transaction, which a
#: registry update re-enters); the other three are the ``run_lifecycle_script``
#: callers in ``routes.py`` and ``teardown.py``.
DISPATCHED_HOOKS = frozenset({"onInstall", "onUninstall", "onEnable", "onDisable"})

#: Declared on ``SetupConfig`` and round-tripped, but executed by nothing.
DECLARED_NOT_WIRED = frozenset({"onUpdate"})

_HOOK_NAME = re.compile(r"^on[A-Z][A-Za-z]*$")


def _runtime_touched_hooks() -> dict[str, set[str]]:
    """Hook names the apps runtime reads, by AST, excluding ``manifest.py``.

    ``manifest.py`` is where every hook is declared and serialized, so it names
    all of them whether or not anything runs them; it is excluded so that what
    remains is exactly the set of hooks some code path reaches for. Comments and
    docstrings never reach the AST, so a stale mention cannot register as a
    dispatch. Returns ``{hook: {"relative/path.py:line", ...}}``.
    """
    touched: dict[str, set[str]] = {}
    for py in sorted(_APPS.rglob("*.py")):
        rel = py.relative_to(_APPS).as_posix()
        if py == _MANIFEST_PY or rel.startswith("builtins/") or "/tests/" in f"/{rel}":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, ast.Attribute) and _HOOK_NAME.match(node.attr):
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _HOOK_NAME.match(node.value):
                    name = node.value
            if name is not None and not name.endswith("Timeout"):
                touched.setdefault(name, set()).add(f"{rel}:{node.lineno}")
    return touched


def _reference_example_setup() -> dict[str, object]:
    text = _REFERENCE_MD.read_text(encoding="utf-8")
    start = text.index("### `setup`")
    end = text.find("\n### ", start + 1)
    section = text[start : end if end != -1 else len(text)]
    m = re.search(r"```json\n(.*?)\n```", section, re.S)
    assert m, "the manifest reference's `setup` section has no JSON example"
    return json.loads(m.group(1))["setup"]


def test_dispatched_hooks_are_exactly_the_four_the_runtime_reads() -> None:
    """Pin the measured set so a quiet new call site has to update the contract.

    If this fails because ``onUpdate`` now appears, the field comment in
    ``manifest.py``, the ``setup.onUpdate`` row in the manifest reference and the
    declared-not-wired paragraph in the app-kit platform spec all have to change
    in the same PR -- that is the whole point of pinning it.
    """
    touched = _runtime_touched_hooks()
    assert set(touched) == DISPATCHED_HOOKS, (
        f"hooks the apps runtime reads changed: {touched}. Update "
        f"DISPATCHED_HOOKS, SetupConfig's field comments, {_REFERENCE_MD.name}'s "
        f"`setup` table and the declared-not-wired paragraph in {_SPEC_MD.name}."
    )
    assert DECLARED_NOT_WIRED.isdisjoint(touched)


def test_every_declared_hook_is_classified() -> None:
    """A new ``on*`` field must land in one of the two sets, not fall between them."""
    declared = {
        f
        for f in SetupConfig.__dataclass_fields__
        if _HOOK_NAME.match(f) and not f.endswith("Timeout")
    }
    assert declared == DISPATCHED_HOOKS | DECLARED_NOT_WIRED


def test_reference_example_shows_only_hooks_that_run() -> None:
    """An author copies the example; it must show the dispatched hooks and nothing else."""
    shown = {k for k in _reference_example_setup() if _HOOK_NAME.match(k)}
    unwired = shown - DISPATCHED_HOOKS
    missing = DISPATCHED_HOOKS - shown
    assert not unwired, f"example shows unwired hook(s): {unwired}"
    assert not missing, f"example omits dispatched hook(s): {missing}"
