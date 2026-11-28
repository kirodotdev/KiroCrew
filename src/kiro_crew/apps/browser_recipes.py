"""Browser recipes: fixed click steps an app declares, which Crew runs on its agent's tab.

An app whose own agent browses its approved sites (``apps/browser_sites.py``) often
needs the same one or two clicks again and again: press a player's play button,
press "Skip ad" when it shows. Asking the model for each costs a turn and a guessed
wait, and nobody presses "Skip ad" once the agent's turn has ended. So an app names
those clicks in ``permissions.browserRecipes``::

    "browserRecipes": [
      {"name": "play", "site": "https://www.youtube.com/watch",
       "steps": [{"waitFor": "button.ytp-play-button", "timeout": 5},
                 {"click": "button.ytp-play-button"}]},
      {"name": "skip_ad", "site": "https://www.youtube.com/watch", "watch": true,
       "steps": [{"click": "button.ytp-skip-ad-button"}]}
    ]

A step is ``{"click": <CSS selector>}`` or ``{"waitFor": <CSS selector>, "timeout":
<seconds>}``, and nothing else: no script, no typed text, no address. ``site`` is a
``browserSites`` pattern (the same rule, :func:`browser_sites.entry_refusal`); a
recipe runs only on a page that matches it AND the app's sites in force, so it can
never act off the owner's list. A ``watch`` recipe also runs by itself while the
gateway watches the app's media tab (``dashboard/handlers/browser_media_watch.py``),
each time its first click's element is shown.

Each recipe is one entry of the :data:`GRANT_FAMILY` family in the app's
approved-grants set, spelled :func:`recipe_entry` (its whole definition), so an
update that changes a step is a new entry the owner has not approved. An install
never approves it (``manager_records._OWNER_APPROVED_FAMILIES``): the trust prompt
lists the recipes and the owner approves them on the app's page. The family is
explicit-only, so a set stored before this host knew the key approves none. A
``watch`` recipe runs by itself only with a second owner yes, the same entry in
:data:`WATCH_FAMILY`, given on its own on the app's page; approving the recipe alone
lets the app page run it on demand.

Recipes sit behind the ``app_browser_sdk`` Feature Preview (``browser_sdk``): with it
off no recipe runs, on demand or by itself, and an approval stays stored but inert.

:func:`app_browser_recipes` and :func:`app_watch_recipes` are the readers the run
paths use, read fresh on every call so the preview turned off, a disable, an
uninstall, a withdrawn trust grant or a changed recipe ends it at the next run.
Nothing here names an app.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The manifest key under ``permissions``, and the grant family that stores it.
GRANT_FAMILY = "browserRecipes"
#: The owner's second, separate yes for a ``watch`` recipe to run by itself: one
#: :func:`recipe_entry` per watch recipe. Without it an approved watch recipe runs
#: only when the app page asks.
WATCH_FAMILY = "browserRecipeWatch"
BROWSER_RECIPE_GRANT_FAMILIES = (GRANT_FAMILY, WATCH_FAMILY)
#: Most recipes one manifest may declare, and most steps one recipe may hold.
MAX_RECIPES = 16
MAX_STEPS = 8
#: Longest selector, in characters.
MAX_SELECTOR = 200
#: Longest one ``waitFor`` may wait, and all of a recipe's waits together, in
#: seconds: a recipe runs inside one browser op's budget (``budgets.OP_TIMEOUT_S``).
MAX_WAIT_S = 10.0
MAX_TOTAL_WAIT_S = 15.0
#: A recipe name: what the app page asks for (``"play"``, ``"skip_ad"``).
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")

STEP_CLICK = "click"
STEP_WAIT = "waitFor"


@dataclass(frozen=True)
class Step:
    """One step: ``kind`` is :data:`STEP_CLICK` or :data:`STEP_WAIT`."""

    kind: str
    selector: str
    timeout: float = 0.0


@dataclass(frozen=True)
class Recipe:
    name: str
    site: str
    steps: tuple[Step, ...]
    watch: bool = False


def _selector_refusal(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "a selector must be a non-empty string"
    if len(value) > MAX_SELECTOR:
        return f"a selector is at most {MAX_SELECTOR} characters"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "a selector may not hold a control character"
    return None


def _step(raw: Any) -> tuple[Step | None, str | None]:
    """``(step, None)`` for a valid step, else ``(None, why)``."""
    if not isinstance(raw, dict):
        return None, "a step must be an object"
    keys = set(raw)
    if keys == {STEP_CLICK}:
        why = _selector_refusal(raw[STEP_CLICK])
        return (None, why) if why else (Step(STEP_CLICK, raw[STEP_CLICK]), None)
    if keys in ({STEP_WAIT}, {STEP_WAIT, "timeout"}):
        why = _selector_refusal(raw[STEP_WAIT])
        if why:
            return None, why
        timeout = raw.get("timeout", 5)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_WAIT_S
        ):
            return None, f"a waitFor timeout is seconds above 0, at most {MAX_WAIT_S:g}"
        return Step(STEP_WAIT, raw[STEP_WAIT], float(timeout)), None
    return None, 'a step is {"click": selector} or {"waitFor": selector, "timeout": seconds}'


def _recipe(raw: Any) -> tuple[Recipe | None, str | None]:
    """``(recipe, None)`` for a valid recipe, else ``(None, why)``."""
    from kiro_crew.apps.browser_sites import entry_refusal  # noqa: PLC0415

    if not isinstance(raw, dict):
        return None, "a recipe must be an object"
    extra = set(raw) - {"name", "site", "steps", "watch"}
    if extra:
        return None, f"a recipe takes only name, site, steps and watch, not {sorted(extra)}"
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return None, "a recipe name is a lower-case letter, then up to 31 of a-z, 0-9 or _"
    site = raw.get("site")
    if not isinstance(site, str):
        return None, f"recipe {name!r}: site must be a string"
    why = entry_refusal(site)
    if why:
        return None, f"recipe {name!r}: site is {why}"
    watch = raw.get("watch", False)
    if not isinstance(watch, bool):
        return None, f"recipe {name!r}: watch must be true or false"
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not 0 < len(steps_raw) <= MAX_STEPS:
        return None, f"recipe {name!r}: steps must be a list of 1 to {MAX_STEPS} steps"
    steps: list[Step] = []
    for item in steps_raw:
        step, why = _step(item)
        if step is None:
            return None, f"recipe {name!r}: {why}"
        steps.append(step)
    if sum(s.timeout for s in steps) > MAX_TOTAL_WAIT_S:
        return None, f"recipe {name!r}: its waits add up to more than {MAX_TOTAL_WAIT_S:g}s"
    if not any(s.kind == STEP_CLICK for s in steps):
        return None, f"recipe {name!r}: needs at least one click"
    if watch and steps[0].kind != STEP_CLICK:
        # A watch runs when this element shows; a wait first would hold the tab.
        return None, f"recipe {name!r}: a watch recipe starts with a click"
    return Recipe(name, site, tuple(steps), watch), None


def recipe_errors(value: Any) -> list[str]:
    """Validation errors of ``permissions.browserRecipes``; empty when valid or absent."""
    if value is None:
        return []
    if not isinstance(value, list):
        return ["permissions.browserRecipes must be an array of recipes"]
    errors: list[str] = []
    if len(value) > MAX_RECIPES:
        errors.append(
            f"permissions.browserRecipes: {len(value)} recipes exceeds the limit of {MAX_RECIPES}"
        )
    names: list[str] = []
    for raw in value:
        recipe, why = _recipe(raw)
        if recipe is None:
            errors.append(f"permissions.browserRecipes: {why}")
        else:
            names.append(recipe.name)
    if len(set(names)) != len(names):
        errors.append("permissions.browserRecipes repeats a recipe name")
    return errors


def parse_recipes(value: Any) -> list[Recipe]:
    """The recipes of a raw ``permissions.browserRecipes`` value; none unless all are valid.

    ``app.json`` is in the app's own writable tree, so the declaration is checked
    again on every read, and one bad recipe admits none of it.
    """
    if not isinstance(value, list) or not value or recipe_errors(value):
        return []
    return [r for r in (_recipe(raw)[0] for raw in value) if r is not None]


def recipe_entry(recipe: Recipe) -> str:
    """The grant entry of *recipe*: its whole definition, so any change is a new entry."""
    steps: list[dict[str, Any]] = []
    for s in recipe.steps:
        if s.kind == STEP_CLICK:
            steps.append({STEP_CLICK: s.selector})
        else:
            steps.append({STEP_WAIT: s.selector, "timeout": s.timeout})
    body = {"name": recipe.name, "site": recipe.site, "steps": steps, "watch": recipe.watch}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _raw(permissions: Any) -> Any:
    if isinstance(permissions, dict):
        return permissions.get(GRANT_FAMILY)
    return getattr(permissions, GRANT_FAMILY, None)


def _declared(permissions: Any) -> list[Recipe]:
    """The recipes a manifest's ``permissions`` (parsed or a raw dict) declare."""
    return parse_recipes(_raw(permissions))


def browser_recipe_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` and :data:`WATCH_FAMILY` entries a manifest's
    ``permissions`` declare."""
    declared = _declared(permissions)
    return {
        GRANT_FAMILY: [recipe_entry(r) for r in declared],
        WATCH_FAMILY: [recipe_entry(r) for r in declared if r.watch],
    }


def _approved(app: str, declared: list[Recipe], family: str = GRANT_FAMILY) -> list[Recipe]:
    """The *declared* recipes whose entry is in *app*'s approved *family*, read fresh."""
    if not declared:
        return []
    entries = [recipe_entry(r) for r in declared]
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    approved = set(staged_app_grants(app, family, lambda: entries))
    return [r for r, e in zip(declared, entries, strict=True) if e in approved]


def preview_on() -> bool:
    """Whether the ``app_browser_sdk`` Feature Preview is on, read fresh. Blocking."""
    # Deferred: ``browser_sdk`` stays off an MCP server's pre-list import budget.
    from kiro_crew.apps.browser_sdk import preview_on as sdk_preview_on  # noqa: PLC0415

    return sdk_preview_on()


def _runnable(app: str, *, watch: bool) -> tuple[Recipe, ...]:
    """The approved recipes of *app*, the watch ones also opted in when *watch*. Fails closed."""
    if not isinstance(app, str) or not app or not preview_on():
        return ()
    try:
        from kiro_crew.apps.execution import third_party_ceiling_closed  # noqa: PLC0415
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled  # noqa: PLC0415

        if not is_app_enabled(app):
            return ()
        manifest = get_app_manifest(app)
        if manifest is None:
            return ()
        approved = _approved(app, _declared(manifest.permissions))
        if watch:
            opted = set(_approved(app, [r for r in approved if r.watch], WATCH_FAMILY))
            approved = [r for r in approved if r in opted]
        if approved and third_party_ceiling_closed(app) is not None:
            return ()
        return tuple(approved)
    except Exception:  # noqa: BLE001 - an unreadable grant grants nothing
        logger.warning("could not resolve the browser recipes of an app", exc_info=True)
        return ()


def app_browser_recipes(app: str) -> tuple[Recipe, ...]:
    """The recipes *app*'s page may run on demand right now. Blocking; fails closed.

    The declared recipes the owner approved, exactly as approved, while the
    ``app_browser_sdk`` preview is on and the app is enabled and trusted to execute.
    Empty on any failure.
    """
    return _runnable(app, watch=False)


def app_watch_recipes(app: str) -> tuple[Recipe, ...]:
    """The ``watch`` recipes that may run by themselves on *app*'s tab right now.

    Those of :func:`app_browser_recipes` the owner also opted in to through
    :data:`WATCH_FAMILY`. Blocking; fails closed.
    """
    return _runnable(app, watch=True)


def withdraw_watch(app: str, entry: str) -> bool:
    """Take back the owner's run-by-itself yes for one watch recipe. Blocking.

    Removes *entry* from *app*'s :data:`WATCH_FAMILY` and nothing else, so the recipe
    stays approved for on-demand runs and the app stays on. ``True`` when the yes
    was held. :class:`LookupError` when *app* is not installed. Callers authorize
    the owner first: nothing here checks who asks. Only ever narrows the set.
    """
    from kiro_crew.apps.manager import (  # noqa: PLC0415
        _check_path_safety,
        _effective_approved_grants,
        _read_installed,
        _write_approved_grants,
        app_record_lock,
    )

    # The name keys file lookups: a traversal names no installed app.
    if not isinstance(app, str) or not app or not _check_path_safety(app):
        raise LookupError(app)
    with app_record_lock(app):
        meta = _read_installed(app)
        if meta is None:
            raise LookupError(app)
        current = _effective_approved_grants(app, meta)
        held = list((current or {}).get(WATCH_FAMILY, ()))
        if current is None or entry not in held:
            return False
        _write_approved_grants(app, {**current, WATCH_FAMILY: [e for e in held if e != entry]})
        return True


def browser_recipes_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``browserRecipesGrant`` key of an app row, absent when nothing is declared.

    ``approved`` and ``staged`` hold :func:`recipe_entry` strings: the declared
    recipes the owner's approved set holds, and the ones the detail page offers.
    ``watchOn`` and ``watchOff`` split the declared ``watch`` recipes by the owner's
    separate :data:`WATCH_FAMILY` yes; the page offers that yes on its own, never in
    the approve-all. ``preview`` says whether the ``app_browser_sdk`` preview is on,
    so the page can say an approved recipe does nothing while it is off.
    """
    permissions = (
        manifest.get("permissions")
        if isinstance(manifest, dict)
        else getattr(manifest, "permissions", None)
    )
    declared = _declared(permissions)
    if not declared:
        return {}
    approved = {recipe_entry(r) for r in _approved(name, declared)}
    entries = [recipe_entry(r) for r in declared]
    watches = [r for r in declared if r.watch]
    watch_on = {recipe_entry(r) for r in _approved(name, watches, WATCH_FAMILY)}
    watch_entries = [recipe_entry(r) for r in watches]
    return {
        "browserRecipesGrant": {
            "approved": [e for e in entries if e in approved],
            "staged": [e for e in entries if e not in approved],
            "watchOn": [e for e in watch_entries if e in watch_on],
            "watchOff": [e for e in watch_entries if e not in watch_on],
            "preview": preview_on(),
        }
    }


__all__ = [
    "BROWSER_RECIPE_GRANT_FAMILIES",
    "GRANT_FAMILY",
    "MAX_RECIPES",
    "MAX_SELECTOR",
    "MAX_STEPS",
    "Recipe",
    "STEP_CLICK",
    "STEP_WAIT",
    "Step",
    "WATCH_FAMILY",
    "app_browser_recipes",
    "app_watch_recipes",
    "browser_recipe_grant_entries",
    "browser_recipes_grant_row",
    "parse_recipes",
    "recipe_entry",
    "recipe_errors",
    "withdraw_watch",
]
