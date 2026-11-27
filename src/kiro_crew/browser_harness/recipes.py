"""Run one app browser recipe (``apps/browser_recipes.py``) on the tab *ctx* is bound to.

A recipe is clicks and waits on CSS selectors, nothing else. Each step finds its
element with the walker's ``recipeTarget`` in the isolated world (``ops._page``,
which judges the page against the session's sites first, so a page off the list is
blanked and refused). A click step judges the URL and the recipe's ``site`` again,
then one ``recipeClick`` call finds the element with the same checks and calls
``el.click()`` on it. The click is bound to that element, so a frame or overlay the
page moves under a point cannot receive it. It is a script click
(:data:`CLICK_KIND`): ``isTrusted`` is false, and a site that ignores such clicks
does nothing, so the app checks the effect and falls back to its agent. On top of
the session's sites, the page must match the recipe's own ``site``. No model is asked.

Answers ``{"ok": True, "recipe", "steps", "click"}``, or a refusal naming the step that
failed (``step``, from 0) with the walker's code: ``not_found``, ``hidden``,
``covered`` (an embedded document or an overlay drawn over it),
``wait_timeout``, ``bad_selector``,
``password_field``, or ``wrong_site``. After each click the page is judged again, so
a click that moved it off the session's sites is blanked and refused. The app falls
back to its agent on any of them.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.browser_harness import ops

#: How a recipe click reaches the page: ``el.click()`` from the isolated world,
#: an untrusted event bound to the element. Named in every answer and audit row.
CLICK_KIND = "script"
#: How often a ``waitFor`` step looks again, in seconds.
WAIT_POLL_S = 0.25


def _failed(recipe: Any, index: int, code: str, error: str) -> dict[str, Any]:
    out = ops.refusal(code, f"recipe {recipe.name!r} step {index}: {error}")
    out["recipe"] = recipe.name
    out["step"] = index
    return out


def _at_step(recipe: Any, index: int, refused: dict[str, Any]) -> dict[str, Any]:
    """*refused* (an ops refusal, keeping its ``code`` and ``error``) naming the recipe and step."""
    return {**refused, "recipe": recipe.name, "step": index}


async def _target(ctx: ops.OpContext, selector: str) -> dict[str, Any]:
    found = await ops._page(ctx, "recipeTarget", selector, frame_refs=False)
    if not isinstance(found, dict):
        return {"ok": False, "code": "browser_error"}
    return found


async def _on_site(ctx: ops.OpContext, site: str) -> bool:
    from kiro_crew.apps.browser_sites import url_matches  # noqa: PLC0415

    tab = await ctx.helper("current_tab")
    url = tab.get("url") if isinstance(tab, dict) else None
    return isinstance(url, str) and url_matches((site,), url)


async def first_target_shown(ctx: ops.OpContext, recipe: Any) -> bool:
    """Whether *recipe*'s first step is a click whose element is shown now, on its site.

    The watch's probe: it reads, and clicks nothing.
    """
    if not recipe.steps or recipe.steps[0].kind != "click" or not await _on_site(ctx, recipe.site):
        return False
    try:
        return bool((await _target(ctx, recipe.steps[0].selector)).get("ok"))
    except ops._PageRefused:
        return False


async def run(ctx: ops.OpContext, recipe: Any) -> dict[str, Any]:
    """Run every step of *recipe* in order; stop at the first that fails."""
    # The step being run, so a refusal raised inside a page read names it too.
    index = 0
    try:
        if not await _on_site(ctx, recipe.site):
            return _failed(recipe, 0, "wrong_site", f"this page is not {recipe.site}")
        for index, step in enumerate(recipe.steps):
            if step.kind == "waitFor":
                deadline = ctx.now() + step.timeout
                while True:
                    found = await _target(ctx, step.selector)
                    # Read the clock after the probe: an element seen past the
                    # deadline does not count.
                    expired = ctx.now() >= deadline
                    if found.get("code") in ("bad_selector", "password_field"):
                        break
                    if found.get("ok") and not expired:
                        break
                    if expired:
                        return _failed(
                            recipe,
                            index,
                            "wait_timeout",
                            f"no shown element matched within {step.timeout:g}s",
                        )
                    await ctx.sleep(min(WAIT_POLL_S, max(0.0, deadline - ctx.now())))
                if not found.get("ok"):
                    code = str(found.get("code") or "browser_error")
                    return _failed(recipe, index, code, "the selector cannot be used")
                continue
            # The page may have moved since the last step: judge it right before
            # the click, so no click reaches a page off the session's list.
            refused = await ops._refuse_current_url(ctx)
            if refused is not None:
                return _at_step(recipe, index, refused)
            if not await _on_site(ctx, recipe.site):
                return _failed(recipe, index, "wrong_site", f"this page is not {recipe.site}")
            await ops._step(ctx, f"Recipe {recipe.name}: click")
            # One walker call finds the element and clicks that element itself
            # (``el.click()``), so nothing moved under a point can take the click.
            # The walker judges the recipe's site and the session's sites again in the
            # same call as the click; with no session sites it clicks nothing.
            clicked = await ops._page(
                ctx, "recipeClick", step.selector, recipe.site, list(ctx.sites), frame_refs=False
            )
            if not isinstance(clicked, dict) or not clicked.get("ok"):
                code = (
                    str(clicked.get("code") or "browser_error")
                    if isinstance(clicked, dict)
                    else "browser_error"
                )
                return _failed(recipe, index, code, "no shown element matches the selector")
            # A click can move the page (a link, ``history.pushState``): judge where
            # it landed before the next step or the answer, as ``run_op`` does.
            refused = await ops._refuse_current_url(ctx)
            if refused is not None:
                return _at_step(recipe, index, refused)
        return {"ok": True, "recipe": recipe.name, "steps": len(recipe.steps), "click": CLICK_KIND}
    except ops._PageRefused as exc:
        return _at_step(recipe, index, exc.result)


__all__ = ["CLICK_KIND", "WAIT_POLL_S", "first_target_shown", "run"]
