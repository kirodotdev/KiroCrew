"""Crew 0.11 modular turn clock contract, independent of OS or ACP provider."""
from __future__ import annotations

import asyncio
import ast
from dataclasses import dataclass
from types import SimpleNamespace
import pytest

from kiro_byo import crew11_patch

DISPATCH = '''from __future__ import annotations
import time
async def _turn_dispatch_gates(turn):
    client = turn.client
    _turn_t0 = turn._turn_t0
    # preparation was already done here
    _turn_t0 = time.monotonic()
    turn._turn_t0 = _turn_t0
'''

STATS = '''from __future__ import annotations
import time
async def _on_complete(turn):
    client = turn.client
    _u = turn.usage
    _turn_t0 = turn._turn_t0
    try:
        _turn_elapsed_ms = int(_u.duration_ms or (time.monotonic() - _turn_t0) * 1000)
        turn._turn_elapsed_ms = _turn_elapsed_ms
    except (TypeError, ValueError):
        turn._turn_elapsed_ms = int((time.monotonic() - _turn_t0) * 1000)
'''


def _load_patched_function(source: str, name: str, *, seconds: float):
    """Execute just the patched function in isolation, not synthetic import mocks."""
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    code = compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), '<owner>', 'exec')
    class Monotonic:
        @staticmethod
        def monotonic() -> float:
            return seconds
    ns = {
        'time': Monotonic,
        'capabilities_of': lambda client: SimpleNamespace(backend=client.backend),
    }
    exec(code, ns)
    return ns[name]


@pytest.mark.parametrize('backend', ['kiro-byo', 'kas', 'kiro', 'claude_code'])
def test_crew_011_clock_keeps_early_start_only_for_byo(backend):
    patched = crew11_patch.patch_text(crew11_patch._DISPATCH_CLOCK, DISPATCH)
    assert crew11_patch.inspect(crew11_patch._DISPATCH_CLOCK, patched).state == 'patched'
    assert crew11_patch.patch_text(crew11_patch._DISPATCH_CLOCK, patched) == patched
    turn = SimpleNamespace(client=SimpleNamespace(backend=backend), _turn_t0=100.0)
    asyncio.run(_load_patched_function(patched, '_turn_dispatch_gates', seconds=107.0)(turn))
    assert turn._turn_t0 == (100.0 if backend == 'kiro-byo' else 107.0)


@pytest.mark.parametrize('backend,expected_ms', [
    ('kiro-byo', 15000),
    ('kas', 3000),
    ('kiro', 3000),
    ('claude_code', 3000),
])
def test_crew_011_completion_uses_true_turn_wall_clock_for_byo(backend, expected_ms):
    patched = crew11_patch.patch_text(crew11_patch._TURN_FOOTER, STATS)
    assert crew11_patch.inspect(crew11_patch._TURN_FOOTER, patched).state == 'patched'
    assert crew11_patch.patch_text(crew11_patch._TURN_FOOTER, patched) == patched
    turn = SimpleNamespace(client=SimpleNamespace(backend=backend),
                           _turn_t0=100.0, usage=SimpleNamespace(duration_ms=3000))
    asyncio.run(_load_patched_function(patched, '_on_complete', seconds=115.0)(turn))
    assert turn._turn_elapsed_ms == expected_ms


def test_crew_011_acp_zero_duration_uses_full_clock():
    patched = crew11_patch.patch_text(crew11_patch._TURN_FOOTER, STATS)
    turn = SimpleNamespace(client=SimpleNamespace(backend='kiro-byo'),
                           _turn_t0=100.0, usage=SimpleNamespace(duration_ms=0))
    asyncio.run(_load_patched_function(patched, '_on_complete', seconds=113.42)(turn))
    assert turn._turn_elapsed_ms == 13420


@pytest.mark.parametrize('relative,source', [
    (crew11_patch._DISPATCH_CLOCK, DISPATCH.replace('turn._turn_t0 = _turn_t0', 'turn._turn_t0 = 0')),
    (crew11_patch._TURN_FOOTER, STATS.replace('_u.duration_ms or ', '')),
])
def test_crew_011_changed_owner_contract_fails_closed(relative,source):
    with pytest.raises(crew11_patch.PatchError):
        crew11_patch.patch_text(relative,source)


def test_manifest_contains_both_owner_seams():
    import json
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    adapter=root/'integration/kiro_crew/v0.11-rc1'
    manifest=json.loads((adapter/'manifest.json').read_text())
    compat=json.loads((adapter/'compatibility.json').read_text())
    seams={crew11_patch._DISPATCH_CLOCK,crew11_patch._TURN_FOOTER}
    assert seams <= set(manifest['files'])
    assert seams <= set(compat['generated_files'])
    assert seams == set(compat['semantic_clock_seams_unpinned'])
    assert seams <= set(crew11_patch.TARGET_FILES)
