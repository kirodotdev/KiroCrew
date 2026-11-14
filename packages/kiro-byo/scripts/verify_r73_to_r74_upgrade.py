#!/usr/bin/env python3
"""Offline R73 -> R74 host upgrade and uninstall, using isolated Crew copies.

This runs the *real* installer in fresh subprocesses to avoid Python module
caching between the prior release and the candidate. It never writes to the
user's installed KiroCrew and cannot prove a running Gateway/Worker permission.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REL_GATE = 'agent_sdk/tool_gate.py'

_SCRIPT = r'''
import json, sys
from pathlib import Path
from unittest.mock import patch
app, crew, home, action = (Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
sys.path.insert(0,str(app/'src'))
from kiro_byo import integration_manager as m
with patch.object(m, 'running_crew', lambda: ('0.12.0', crew, crew.parent)):
    if action == 'install':
        result = m.install_from_app(app, home, version='0.3.2')
    elif action == 'uninstall':
        result = m.uninstall_from_app(app, home)
    else:
        raise ValueError(action)
assert result.get('ok'), result
print(json.dumps({'action': action, 'ok': result['ok'], 'result': result}, default=str))
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--crew-package', required=True, type=Path,
                        help='Pristine Crew package with static/dist from the supplied dashboard')
    parser.add_argument('--r73-app', required=True, type=Path)
    parser.add_argument('--r74-app', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    prior = args.r73_app.resolve()
    current = args.r74_app.resolve()
    if not (prior/'src/kiro_byo/integration_manager.py').is_file():
        raise ValueError('R73 app source directory missing')
    if not (current/'src/kiro_byo/integration_manager.py').is_file():
        raise ValueError('R74 app source directory missing')
    if not (args.crew_package/'static/dist/index.html').is_file():
        raise ValueError('Crew Dashboard static/dist/index.html is required')
    old = json.loads((prior/'integration/kiro_crew/v0.12-plus/compatibility.json').read_text())
    new = json.loads((current/'integration/kiro_crew/v0.12-plus/compatibility.json').read_text())
    assert 'src/kiro_crew/agent_sdk/tool_gate.py' in old['generated_files']
    assert 'src/kiro_crew/agent_sdk/tool_gate.py' not in new['generated_files']
    expected = {p.removeprefix('src/kiro_crew/') for p in old['generated_files']}
    expected.add('static/dist/index.html')
    with tempfile.TemporaryDirectory(prefix='kiro-byo-r73-r74-') as directory:
        root=Path(directory)
        crew=root/'src'/'kiro_crew'
        shutil.copytree(args.crew_package,crew)
        home=root/'byo-data'
        before={rel: digest(crew/rel) for rel in sorted(expected)}
        stage=[]
        for app,action in [(prior,'install'),(current,'install'),(current,'install'),(current,'uninstall')]:
            result=subprocess.run([sys.executable,'-c',_SCRIPT,str(app),str(crew),str(home),action],
                                  capture_output=True,text=True,timeout=100)
            if result.returncode:
                raise RuntimeError(f'{app.name}/{action} failed: {result.stderr[-3500:]}\n{result.stdout[-2000:]}')
            stage.append({'action':action,'app': 'R73' if app==prior else 'R74',
                          'result':json.loads(result.stdout)['result']})
            if len(stage)==1:
                assert digest(crew/REL_GATE)!=before[REL_GATE], 'R73 did not touch tool gate'
            if len(stage)==2:
                assert digest(crew/REL_GATE)==before[REL_GATE], 'R74 did not retire tool gate'
        mismatched=[rel for rel,hash_value in before.items() if digest(crew/rel)!=hash_value]
        if mismatched:
            raise AssertionError(f'Crew files not restored after uninstall: {mismatched}')
        if (crew/'acp/harness/kiro_byo.py').exists():
            raise AssertionError('R74 uninstall left BYO-created harness')
        report={'status':'pass','stages':stage,'priorMutatedSourceFiles':len(old['generated_files']),
                'currentMutatedSourceFiles':len(new['generated_files']),
                'retiredToolGateRestoredOnUpgrade':True,
                'originalFileHashesRestoredOnUninstall':len(before),
                'byoCreatedHarnessRemoved':True,
                'scope':'isolated filesystem and installer; no running Gateway/Worker E2E'}
    output=json.dumps(report,indent=2,ensure_ascii=False)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(output)
    print(output)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
