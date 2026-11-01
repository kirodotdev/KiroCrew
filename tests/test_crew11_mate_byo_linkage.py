from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

from kiro_byo import crew11_patch
from kiro_byo.crew_linkage import linkage_snapshot

ROOT = Path(__file__).resolve().parents[1]


def _patched_loader_source() -> str:
    # Minimal source-shaped fixture for Crew 0.11's one backend-choice gate.
    # The exact uploaded RC1 checkout is verified separately by the source
    # preflight script and is intentionally not bundled into BYO's tests.
    source = r'''
def _read_backend_choice(section, agent_data, key):
    from kiro_crew.acp_backends import (
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        backend_from_stored,
        selectable_backends,
    )
    value = backend_from_stored(agent_data.get(key))
    if value is None:
        default = section.default(key)
        value = default if default in selectable_backends() else None
    elif key == _MEMBER_HARNESS_KEY and value == ACP_BACKEND_KIRO:
        value = ACP_BACKEND_KAS
        _note_member_remap(agent_data.get(key), value)
    return _normalize_acp_backend(value)

_MEMBER_HARNESS_KEY = "member_acp_backend"
'''
    return crew11_patch.patch_text('src/kiro_crew/config/loader_builders.py', source)


def _load_read_backend_choice():
    source = _patched_loader_source()
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_read_backend_choice')
    module = ast.Module(body=[fn], type_ignores=[])

    fake_parent = types.ModuleType('kiro_crew')
    fake_backends = types.ModuleType('kiro_crew.acp_backends')
    fake_backends.ACP_BACKEND_KAS = 'kas'
    fake_backends.ACP_BACKEND_KIRO = 'kiro'
    fake_backends.ACP_BACKEND_KIRO_BYO = 'kiro-byo'
    fake_backends.backend_from_stored = lambda value: None if value in (None, '') else str(value)
    fake_backends.selectable_backends = lambda: {'kiro', 'kas', 'kiro-byo'}
    fake_parent.acp_backends = fake_backends
    old_parent = sys.modules.get('kiro_crew')
    old_backends = sys.modules.get('kiro_crew.acp_backends')
    sys.modules['kiro_crew'] = fake_parent
    sys.modules['kiro_crew.acp_backends'] = fake_backends
    ns = {
        '_MEMBER_HARNESS_KEY': 'member_acp_backend',
        '_normalize_acp_backend': lambda value: value or 'kiro',
        '_note_member_remap': lambda *_: None,
    }
    try:
        exec(compile(module, 'loader_builders.py', 'exec'), ns)
        return ns['_read_backend_choice'], (old_parent, old_backends)
    except Exception:
        if old_parent is None:
            sys.modules.pop('kiro_crew', None)
        else:
            sys.modules['kiro_crew'] = old_parent
        if old_backends is None:
            sys.modules.pop('kiro_crew.acp_backends', None)
        else:
            sys.modules['kiro_crew.acp_backends'] = old_backends
        raise


class _Section:
    def default(self, key: str):
        return 'kas' if key == 'member_acp_backend' else 'kiro'


def test_member_backend_is_byo_while_active_even_with_saved_official_choice() -> None:
    read, old = _load_read_backend_choice()
    try:
        section = _Section()
        assert read(section, {'acp_backend': 'kiro-byo'}, 'member_acp_backend') == 'kiro-byo'
        assert read(section, {'acp_backend': 'kiro-byo', 'member_acp_backend': ''}, 'member_acp_backend') == 'kiro-byo'
        assert read(section, {'acp_backend': 'kiro-byo', 'member_acp_backend': 'kas'}, 'member_acp_backend') == 'kiro-byo'
        assert read(section, {'acp_backend': 'kiro', 'member_acp_backend': 'kas'}, 'member_acp_backend') == 'kas'
        assert read(section, {'acp_backend': 'kiro', 'member_acp_backend': ''}, 'member_acp_backend') == 'kas'
        assert read(section, {'acp_backend': 'kas'}, 'member_acp_backend') == 'kas'
    finally:
        old_parent, old_backends = old
        if old_parent is None:
            sys.modules.pop('kiro_crew', None)
        else:
            sys.modules['kiro_crew'] = old_parent
        if old_backends is None:
            sys.modules.pop('kiro_crew.acp_backends', None)
        else:
            sys.modules['kiro_crew.acp_backends'] = old_backends


def test_rc1_byo_declares_mate_worker_kit_only_with_worker_projection() -> None:
    assert 'ACP_BACKENDS_MATE_WORKER_KIT' in crew11_patch._CAPABILITIES['backends_members.py']
    harness = (ROOT / 'integration/kiro_crew/v0.11-rc1/overlay/src/kiro_crew/acp/harness/kiro_byo.py').read_text(encoding='utf-8')
    for marker in ('resolve_projected_spec', 'mate_worker_projection', 'MATE_WORKER_AGENT'):
        assert marker in harness
    assert harness.index('mate_worker_projection') < harness.index('descriptor: dict[str, object]')


def test_linkage_snapshot_reports_host_defaults_for_schedule_and_background() -> None:
    class Agent:
        acp_backend = 'kiro-byo'
        member_acp_backend = 'kiro-byo'
        role_models = {'background': 'auto', 'subagent': 'auto'}
        def resolve_model(self, role):
            return 'auto'
    snap = linkage_snapshot(Agent())
    assert snap['cron']['runtime_backend'] == 'kiro-byo'
    assert snap['background']['runtime_backend'] == 'kiro-byo'
    assert snap['subagent']['runtime_backend'] == 'kiro-byo'
    assert snap['members']['configured_backend'] == 'kiro-byo'


def test_task_linkage_is_not_blanket_disabled_for_verified_rc1() -> None:
    routes = (ROOT / 'backend/routes.py').read_text(encoding='utf-8')
    assert '_reject_crew11_mate_linkage_write' not in routes
    ui = (ROOT / 'ui/index.mjs').read_text(encoding='utf-8')
    # Model editing is independent of Mate 2.0 member-launch capability.
    assert "data.main_backend" in ui
    assert "linkResult.value.linkage_write_supported" not in ui
