#!/usr/bin/env python3
"""Read-only, fresh-process Crew 0.12 native-registration + routing evidence.

No synthetic registry mutation or monkeypatch: imports the actual Crew backend
owner and tool gate and reports their *runtime* answers. A selectable backend
is never reported as permission-safe merely because routing_verdict says ROUTED.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_CHILD = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from kiro_crew.agent_sdk import backends, tool_gate
from kiro_crew.agent_sdk.backends_launch import ACP_BACKEND_LAUNCH
backend='kiro-byo'
known=backend in backends.ACP_BACKENDS_KNOWN
before=backend in backends.selectable_backends()
try:
    backends.register_selectable_backend(backend)
    registration={'allowed':True,'reason':''}
except (ValueError,KeyError) as exc:
    registration={'allowed':False,'reason':str(exc)}
routing=backends.routing_for(backend)
verdict,why=tool_gate.routing_verdict(backend)
result={
 'backend':backend,'known':known,'selectedBeforeRegistration':before,
 'registration':registration,'selectable':backend in backends.selectable_backends(),
 'route':routing.value,'hostRoutingVerdict':verdict.value,'hostRoutingExplanation':why,
 'hostNonRoutedRefusalEnforced':tool_gate.is_enforced(backend),
 'hostLaunchDescriptionPresent':backend in ACP_BACKEND_LAUNCH,
 'toolAuthorizationProof':'not_verified',
 'gatewayWorkerMcpE2E':'not_verified',
}
result['state']=('blocked_unknown' if not known else
    'registered_without_host_enforcement' if result['selectable'] and not result['hostNonRoutedRefusalEnforced'] else
    'registry_only_requires_runtime_gate_test')
print(json.dumps(result,ensure_ascii=False))
'''


def probe(src: Path) -> dict:
    src = src.resolve()
    if not (src / 'kiro_crew' / 'agent_sdk' / 'backends.py').is_file():
        raise ValueError('expected a Crew src directory containing kiro_crew/agent_sdk/backends.py')
    result = subprocess.run([sys.executable, '-c', _CHILD, str(src)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Crew runtime imports failed: '+result.stderr[-3000:])
    return json.loads(result.stdout)


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--crew-src',type=Path,required=True)
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    report=probe(a.crew_src)
    data=json.dumps(report,indent=2,ensure_ascii=False)+'\n'
    if a.output:
        a.output.write_text(data,encoding='utf8')
    print(data,end='')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
