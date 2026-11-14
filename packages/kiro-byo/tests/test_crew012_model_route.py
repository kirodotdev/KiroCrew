"""The mate-aware 0.12 models_route contract must preserve backend rejection."""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from _crew_semantic_fixtures import _minimal_agents_source_for_member_backend
from kiro_byo import crew12_patch

REL = "src/kiro_crew/dashboard/handlers/agents.py"


def _mate_route_sample() -> str:
    source = _minimal_agents_source_for_member_backend()
    return source.replace(
        '''    backend = "kiro"
    cfg = object()
''',
        '''    cfg = object()
    from kiro_crew.crewmate_harness import models_route
    route = await asyncio.to_thread(models_route, cfg, request.query)
    if route is None:
        return web.json_response({"code": "invalid_acp_backend"}, status=400)
    backend, bundle_route = route
''',
        1,
    )


def test_models_route_is_guarded_and_insertion_follows_unpack() -> None:
    source = _mate_route_sample()
    assert crew12_patch.inspect(REL, source).state == "clean"
    updated = crew12_patch.patch_text(REL, source)
    block = updated.split("async def api_models(request):", 1)[1]
    assert block.index("if route is None:") < block.index("backend, bundle_route = route")
    assert block.index("backend, bundle_route = route") < block.index("if backend == ACP_BACKEND_KIRO_BYO:")
    assert updated == crew12_patch.patch_text(REL, updated)
    assert crew12_patch.inspect(REL, updated).state == "patched"
    ast.parse(updated)


@pytest.mark.parametrize("broken", [
    lambda s: s.replace('    if route is None:\n', '    if False:\n', 1),
    lambda s: s.replace('    backend, bundle_route = route\n', '    backend = "kiro"\n', 1),
    lambda s: s.replace('asyncio.to_thread(models_route, cfg, request.query)',
                        'asyncio.to_thread(other_route, cfg, request.query)', 1),
    lambda s: s.replace('    if route is None:\n        return web.json_response({"code": "invalid_acp_backend"}, status=400)\n', '', 1),
])
def test_models_route_missing_security_edge_is_rejected(broken) -> None:
    source = broken(_mate_route_sample())
    assert crew12_patch.inspect(REL, source).state == "incompatible"


def test_exact_crew_source_when_available() -> None:
    root = os.environ.get("KIRO_CREW_012_SOURCE")
    if not root:
        pytest.skip("set KIRO_CREW_012_SOURCE to test provided official 0.12.0 sources")
    path = Path(root) / REL
    source = path.read_text(encoding="utf-8-sig")
    assert crew12_patch.inspect(REL, source).state == "clean"
    patched = crew12_patch.patch_text(REL, source)
    assert crew12_patch.inspect(REL, patched).state == "patched"
    assert patched == crew12_patch.patch_text(REL, patched)
    compile(patched, str(path), "exec")
