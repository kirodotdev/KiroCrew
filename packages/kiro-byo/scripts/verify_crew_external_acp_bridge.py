#!/usr/bin/env python3
"""R76: verify *actual Crew source* experimental generic ACP transport with BYO.

No Crew production registry mutation, no prompts, no model API calls, no MCP.
Reports separately what works on the wire and what still requires upstream.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for piece in iter(lambda: f.read(1024 * 1024), b''):
            h.update(piece)
    return h.hexdigest()


def _load_actual_crew_module(source: Path):
    path = source / 'src' / 'kiro_crew' / 'agent_sdk' / 'external_acp_probe.py'
    if not path.is_file():
        raise FileNotFoundError('Crew upstream prototype module not present: ' + str(path))
    spec = importlib.util.spec_from_file_location('crew_external_acp_probe_for_verification', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mode_run(module, mode: str, base: Path) -> dict:
    expected = {'tiga': 'multi', 'dyna': 'flash'}[mode]
    home = base / mode / 'byo-home'
    (home / 'config').mkdir(parents=True)
    (home / 'config' / 'providers.json').write_text(json.dumps({
        'credential_schema_version': 1,
        'mode': {'id': mode, 'type': expected},
        'providers': [
            {'id': 'mock-fast', 'name': 'Fast', 'type': 'openai-compatible',
             'base_url': 'https://example.invalid/v1',
             'models': [{'id': 'wire-fast', 'name': 'WireFast', 'tier': 'fast', 'tool_use': True}]},
            {'id': 'mock-strong', 'name': 'Strong', 'type': 'openai-compatible',
             'base_url': 'https://example.invalid/v1',
             'models': [{'id': 'wire-strong', 'name': 'WireStrong', 'tier': 'strong', 'tool_use': True}]},
        ],
    }), encoding='utf8')
    exe = Path(sys.executable).resolve(strict=True)
    workspace = base / mode / 'workspace'
    workspace.mkdir(parents=True)
    env = {
        'PYTHONPATH': str(ROOT / 'src'),
        'PATH': os.environ.get('PATH', ''),
        'KIRO_BYO_HOME': str(home),
        'KIROCREW_HOME': str(base / mode / 'crew-home'),
    }
    if os.name == 'nt' and 'SYSTEMROOT' in os.environ:
        env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
    spec = module.ApprovedAcpProcess(
        backend_id='kiro-byo', label='Kiro BYO', executable=exe,
        executable_sha256=_sha(exe), argv=('-m', 'kiro_byo', 'acp'),
        approval_root=exe.parent, cwd=workspace, env=env,
    )
    with module.AcpProbeSwitch({'kiro-byo': spec}, reserved_ids=frozenset({
        'kas', 'codex', 'claude', 'opencode', 'pi', 'goose', 'deepseek',
    })) as chooser:
        child = chooser.select('kiro-byo')
        new = child.new_session()
        sid = new['sessionId']
        model_ids = child._model_ids(new)
        key = f'kiro-byo-type:{mode}:{expected}'
        if new.get('models', {}).get('currentModelId') != key or key not in model_ids:
            raise AssertionError('wrong default Type or advertised model list')
        if 'WireStrong' not in model_ids:
            raise AssertionError('model not advertised')
        switched = child.select_model(sid, 'WireStrong')
        if not any(isinstance(row, dict) and row.get('id') == 'model'
                   and row.get('currentValue') == 'WireStrong'
                   for row in switched.get('configOptions', [])):
            raise AssertionError('model config did not switch')
        try:
            child.select_model(sid, 'kiro-byo-type:other:unknown')
        except module.ProbeRefused:
            rejected_cross_mode = True
        else:
            rejected_cross_mode = False
        if not rejected_cross_mode:
            raise AssertionError('undisclosed cross-mode model not rejected')
        child.close_session(sid)
        loaded = child.load_session(sid)
        if loaded.get('models', {}).get('currentModelId') != 'WireStrong':
            raise AssertionError('model did not survive session/load')
        try:
            child._request('session/prompt', {'sessionId': sid, 'prompt': []})
        except module.ProbeRefused:
            prompt_refused = True
        else:
            prompt_refused = False
        if not prompt_refused:
            raise AssertionError('protocol-only boundary allowed prompt')
        return {
            'mode': mode, 'backend_id': chooser.selected_backend,
            'protocol': 'ACP v1', 'initialized': True, 'session_created': True,
            'default_type': key, 'model_switched': True,
            'session_loaded': True, 'cross_mode_rejected': rejected_cross_mode,
            'prompt_refused': prompt_refused, 'mcp_servers_sent': 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--crew-source', required=True, type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = {
        'ok': False, 'scope': 'real Crew source external ACP transport (not Crew runtime)',
        'production_registration': 'not integrated',
        'worker_mcp_permission_enforcement': 'not verified',
        'desktop_end_to_end': 'not tested',
        'official_kiro_cli_switch': 'not tested (actual kiro-cli binary unavailable)',
    }
    try:
        module = _load_actual_crew_module(args.crew_source)
        with tempfile.TemporaryDirectory(prefix='crew-r76-acp-') as temp:
            report['modes'] = [_mode_run(module, mode, Path(temp)) for mode in ('tiga', 'dyna')]
        report['ok'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + '\n', encoding='utf8')
    print(payload)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
