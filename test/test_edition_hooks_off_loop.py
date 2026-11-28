"""Edition hooks may block, so core never calls them on the event loop.

``SkillProvider.is_available`` (through ``provider_available``), an MCP
provider's ``is_available``, and the registry
policy's ``clone_sandbox_mode`` (through ``_context_clone_sandbox_mode``) are
supplied by an edition. An edition's answer can read a file or run a probe, so an
async caller runs them with ``asyncio.to_thread``. Called on the loop, a slow
answer stalls every request and can trip the loop watchdog.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from kiro_crew.skill_providers.base import ProviderRegistry, SkillSearchResult
from kiro_crew.testing.wait import await_until

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: The hooks, and the registry properties that call one for every provider.
_HOOKS = {"_context_clone_sandbox_mode", "provider_available", "_featured_response"}
_HOOK_PROPERTIES = {"available_providers", "available_provider_names"}
#: ``<name>.is_available()`` on these names is a skill or MCP provider's own hook.
_PROVIDER_NAMES = {"p", "provider", "edition"}


class _GatedProvider:
    """A provider whose ``is_available`` waits for the loop to release it."""

    name = "gated"
    display_name = "Gated"
    api_base = "https://gated.example.invalid"

    def __init__(self, loop: asyncio.AbstractEventLoop, entered: asyncio.Event) -> None:
        self._loop = loop
        self._entered = entered
        self.release = threading.Event()
        self.threads: list[int] = []

    def is_available(self) -> bool:
        self.threads.append(threading.get_ident())
        self._loop.call_soon_threadsafe(self._entered.set)
        # The loop sets ``release`` once it sees ``entered``. If this runs on the
        # loop thread the loop cannot get there, so the wait times out: False.
        return self.release.wait(5)

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        return [SkillSearchResult(id="s1", name="s1", description="", provider=self.name)]

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        return None


async def _search_while_the_loop_releases(provider: str | None) -> tuple:
    entered = asyncio.Event()
    gated = _GatedProvider(asyncio.get_running_loop(), entered)
    registry = ProviderRegistry()
    registry.register(gated)

    search = asyncio.create_task(registry.search_with_outcomes("x", provider=provider))
    await await_until(entered, what="the hook was entered")
    gated.release.set()
    response = await await_until(search, what="the search finished")
    return response, gated


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, "gated"], ids=["fan-out", "one-provider"])
async def test_search_asks_is_available_off_the_loop(provider):
    response, gated = await _search_while_the_loop_releases(provider)
    assert [r.id for r in response.results] == ["s1"]
    assert gated.threads and threading.get_ident() not in gated.threads


def _async_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def _direct_hook_calls(fn: ast.AsyncFunctionDef) -> list[str]:
    """Hook calls and hook-property reads made directly inside *fn*'s own body.

    A hook passed to ``asyncio.to_thread`` or read inside a lambda is not a call
    on the loop, and nested functions are judged on their own.
    """
    found: list[str] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                callee = child.func
                name = (
                    callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
                )
                if name in _HOOKS:
                    found.append(f"{name}() at line {child.lineno}")
                elif (
                    name == "is_available"
                    and isinstance(callee, ast.Attribute)
                    and isinstance(callee.value, ast.Name)
                    and callee.value.id in _PROVIDER_NAMES
                ):
                    found.append(f"{callee.value.id}.is_available() at line {child.lineno}")
            elif isinstance(child, ast.Attribute) and child.attr in _HOOK_PROPERTIES:
                found.append(f".{child.attr} at line {child.lineno}")
            visit(child)

    visit(fn)
    return found


def test_no_async_function_calls_an_edition_hook_on_the_loop():
    offenders: list[str] = []
    scanned = 0
    for path in sorted(_SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(h in source for h in _HOOKS | _HOOK_PROPERTIES | {"is_available"}):
            continue
        scanned += 1
        for fn in _async_functions(ast.parse(source)):
            for hit in _direct_hook_calls(fn):
                offenders.append(f"{path.relative_to(_SRC)}: async {fn.name}: {hit}")
    # Anti-vacuity: the scan must have reached the files that use the hooks.
    assert scanned >= 5, f"only {scanned} files mention an edition hook; check the scan"
    assert offenders == [], "call these through asyncio.to_thread:\n" + "\n".join(offenders)
