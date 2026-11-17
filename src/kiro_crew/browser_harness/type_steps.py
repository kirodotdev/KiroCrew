"""``type {ref?, steps}``: ordered text and key steps in ONE ``type`` op.

One op cannot be reordered, so a table or form row (text, Tab, text) lands in the
cells it names. Every step runs through the handler that already guards it --
``ops._type`` for a text step, ``ops._press_key`` for a key step -- so no guard is
copied here. The whole list is shape-checked before any step runs; the first step
refused while running ends the op, its 0-based index in ``step`` and at the front
of its ``error``. ``ops._type`` reaches this module by a call-time import.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.browser_harness import ops as _ops
from kiro_crew.browser_harness.supervisor import HelperError


def _steps_refusal(error: str, index: int | None = None) -> dict[str, Any]:
    if index is None:
        return _ops.refusal("bad_steps", error)
    out = _ops.refusal("bad_steps", f"step {index} (counting from 0): {error}; nothing was typed")
    out["step"] = index
    return out


def _ran_steps_refusal(result: dict[str, Any], index: int) -> dict[str, Any]:
    """A step's own refusal, its index in front of its error and as ``step``."""
    prefix = f"step {index} (counting from 0; the steps before it ran)"
    error = result.get("error")
    return {**result, "error": f"{prefix}: {error}" if error else prefix, "step": index}


def _step_shape_error(step: Any) -> str | None:
    """Why one ``steps`` item is malformed, or ``None`` for ``{text}`` / ``{key, modifiers?}``."""
    if not isinstance(step, dict):
        return "each step is an object: {text} or {key, modifiers?}"
    keys = set(step)
    if keys == {"text"}:
        return None if isinstance(step["text"], str) else "a text step needs a `text` string"
    if "key" in keys and keys <= {"key", "modifiers"}:
        if _ops._known_key(step["key"]):
            return None
        return f"unknown key: {step['key']!r} (named keys or a single character)"
    return "each step is exactly {text} or {key, modifiers?}"


async def run(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """Run ``args["steps"]`` in order, each with its own guards."""
    steps = args.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= _ops.STEPS_CAP:
        return _steps_refusal(f"`steps` is a list of 1 to {_ops.STEPS_CAP} steps")
    if args.get("text") is not None or args.get("submit"):
        return _steps_refusal(
            "with `steps`, put the text and the Enter key in the steps, "
            "not in `text` or `submit`"
        )
    for index, step in enumerate(steps):
        error = _step_shape_error(step)
        if error is not None:
            return _steps_refusal(error, index)
    chars = sum(len(step["text"]) for step in steps if "text" in step)
    if chars > _ops.TEXT_CAP:
        return _ops.refusal("bad_text", f"text longer than {_ops.TEXT_CAP} characters")
    ref = args.get("ref")
    if ref is not None:
        refused = await _ops._focus_ref(ctx, ref)
        if refused is not None:
            return refused
    for index, step in enumerate(steps):
        try:
            if "text" in step:
                result = await _ops._type(ctx, {"text": step["text"]})
            else:
                result = await _ops._press_key(
                    ctx, {"key": step["key"], "modifiers": step.get("modifiers")}
                )
        except _ops._PageRefused as exc:
            return _ran_steps_refusal(exc.result, index)
        except HelperError:
            # A dialog a step opened holds the page, so this step's helper call failed:
            # ``run_op`` would say so too, but without the step the row stopped at.
            shown = await _ops._open_dialog(ctx)
            if shown is None:
                raise
            return _ran_steps_refusal(_ops._dialog_open(shown), index)
        if not result.get("ok"):
            return _ran_steps_refusal(result, index)
    return {"ok": True, "ref": ref, "steps": len(steps), "chars": chars}
