"""Every variable-resolution site must run off the event loop.

This anchor was raised four rounds running, each time naming a different site, because
the fix was applied one site at a time. This enumerates the class instead: the four
helpers that resolve variables all do a ``KiroCrewConfig.load()`` (a synchronous read,
parse and validate of config.json) plus a variables-store read, and every dispatch path
reaching them is on the gateway event loop.

Asserted on the CALL SITES rather than by timing, because the property is structural:
the helper is handed to an offloader instead of being invoked inline.
"""

from __future__ import annotations

import ast
import pathlib

import kiro_crew.config.loader as loader_mod

SRC = pathlib.Path(loader_mod.__file__).resolve().parents[1]

# helper -> the file whose async dispatch paths call it.
RESOLVERS = {
    "_expand_message_variables": "dashboard/chat_runner.py",
    "build_cron_session_context": "slack/gateway.py",
    "render_nudge_message": "slack/gateway.py",
}

# Recognised ways of getting off the loop in this codebase.
OFFLOADERS = {"to_thread", "run_in_embed_pool", "run_config_write"}


def _inline_calls(rel: str, helper: str) -> list[int]:
    """Line numbers where *helper* is CALLED inline (not handed to an offloader)."""
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    offloaded_names: set[int] = set()

    # First pass: names passed as arguments to an offloader are fine -- that is the
    # to_thread(fn, *args) form, where the helper appears as a bare Name.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fname not in OFFLOADERS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id == helper:
                offloaded_names.add(arg.lineno)
            elif isinstance(arg, ast.Attribute) and arg.attr == helper:
                offloaded_names.add(arg.lineno)

    inline: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fname != helper:
            continue
        if node.lineno in offloaded_names:
            continue
        inline.append(node.lineno)
    return inline


class TestResolutionIsOffLoop:
    def test_no_resolver_is_called_inline_on_a_dispatch_path(self):
        offenders: list[str] = []
        for helper, rel in RESOLVERS.items():
            for lineno in _inline_calls(rel, helper):
                offenders.append(f"{rel}:{lineno} {helper}()")
        assert not offenders, (
            "these variable-resolution calls run on the event loop; hand the helper to "
            f"asyncio.to_thread (or another offloader) instead: {offenders}"
        )

    def test_the_offload_forms_are_actually_present(self):
        """Positive control.

        Without this, the guard above passes on a build where the helpers were simply
        deleted or renamed -- an absence assertion proving nothing.
        """
        gateway = (SRC / "slack/gateway.py").read_text(encoding="utf-8")
        runner = (SRC / "dashboard/chat_runner.py").read_text(encoding="utf-8")
        assert "asyncio.to_thread(build_cron_session_context" in gateway
        assert "asyncio.to_thread(\n            render_nudge_message" in gateway
        assert "asyncio.to_thread(_expand_message_variables" in runner
