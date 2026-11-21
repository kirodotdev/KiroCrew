"""R76: isolated Crew upstream external ACP bridge contract and real BYO wire."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import textwrap

import pytest

# experiments/ holds local-only upstream-interface prototypes and is excluded
# from the public repo (see .gitignore). When it is absent, skip this whole
# module rather than erroring on import.
pytest.importorskip("experiments.crew_external_acp_probe")
from experiments.crew_external_acp_probe import (
    AcpProbeSwitch, ApprovedAcpProcess, ExternalAcpProbe, ProbeRefused,
)

PROJECT = Path(__file__).resolve().parents[1]


def _digest(exe: Path) -> str:
    sha = hashlib.sha256()
    with exe.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            sha.update(b)
    return sha.hexdigest()


def _python_exe() -> Path:
    return Path(sys.executable).resolve(strict=True)


def _approval(tmp_path: Path, backend='kiro-byo', argv=None, env=None) -> ApprovedAcpProcess:
    exe = _python_exe()
    tmp_path.mkdir(parents=True, exist_ok=True)
    return ApprovedAcpProcess(
        backend_id=backend, label=backend, executable=exe,
        executable_sha256=_digest(exe),
        argv=tuple(argv or ('-m', 'kiro_byo', 'acp')),
        approval_root=exe.parent, cwd=tmp_path,
        env=dict(env or {}),
    )


def _byo_env(tmp_path: Path, mode: str = 'tiga') -> dict[str, str]:
    home = tmp_path / 'byo-home'
    config = home / 'config'
    config.mkdir(parents=True, exist_ok=True)
    types = {'tiga': 'multi', 'dyna': 'flash'}
    (config / 'providers.json').write_text(json.dumps({
        'credential_schema_version': 1,
        'mode': {'id': mode, 'type': types[mode]},
        'providers': [
            {'id': 'a', 'name': 'A', 'type': 'openai-compatible',
             'base_url': 'https://example.invalid/v1',
             'models': [{'id': 'fast-1', 'name': 'Fast', 'tier': 'fast', 'tool_use': True}]},
            {'id': 'b', 'name': 'B', 'type': 'openai-compatible',
             'base_url': 'https://example.invalid/v1',
             'models': [{'id': 'strong-2', 'name': 'Strong', 'tier': 'strong', 'tool_use': True}]},
        ]
    }), encoding='utf8')
    return {
        'PYTHONPATH': str(PROJECT / 'src'),
        'KIRO_BYO_HOME': str(home),
        'KIROCREW_HOME': str(tmp_path / 'crew'),
        'PATH': os.environ.get('PATH', ''),
        **({'SYSTEMROOT': os.environ['SYSTEMROOT']} if os.name == 'nt' and 'SYSTEMROOT' in os.environ else {}),
    }


@pytest.mark.parametrize('mode,expected', [
    ('tiga', 'kiro-byo-type:tiga:multi'),
    ('dyna', 'kiro-byo-type:dyna:flash'),
])
def test_real_byo_handshake_session_type_model_and_reload(tmp_path, mode, expected):
    root = tmp_path / 'work'
    spec = _approval(root, env=_byo_env(tmp_path, mode))
    with ExternalAcpProbe(spec, reserved_ids=frozenset({'kas', 'codex'})) as client:
        info = client.initialize()
        assert info['protocolVersion'] == 1
        created = client.new_session()
        sid = created['sessionId']
        assert created['models']['currentModelId'] == expected
        assert expected in {row['modelId'] for row in created['models']['availableModels']}
        model_selected = client.select_model(sid, 'Strong')
        assert any(row.get('currentValue') == 'Strong' for row in model_selected['configOptions'])
        client.close_session(sid)
        restored = client.load_session(sid)
        assert restored['models']['currentModelId'] == 'Strong'
        assert client.select_model(sid, expected, v3=False) == {}
        with pytest.raises(ProbeRefused, match='method is not permitted'):
            client._request('session/prompt', {'sessionId': sid, 'prompt': []})


def test_reserved_and_unknown_selection_rejected(tmp_path):
    spec = _approval(tmp_path)
    with pytest.raises(ProbeRefused, match='reserved'):
        replace(spec, backend_id='kas').validate(frozenset({'kas'}))
    with AcpProbeSwitch({'kiro-byo': spec}, reserved_ids=frozenset({'kas'})) as selector:
        with pytest.raises(ProbeRefused, match='unknown'):
            selector.select('kas')
        assert selector.selected_backend is None


def test_backend_ids_are_exact_and_not_aliases(tmp_path):
    spec = _approval(tmp_path)
    for bid in ('KIRO-BYO', '../kiro-byo', 'kiro byo', ' kiro-byo'):
        with pytest.raises(ProbeRefused, match='invalid'):
            replace(spec, backend_id=bid).validate(frozenset())


def test_unapproved_executable_fails_closed(tmp_path):
    spec = _approval(tmp_path)
    with pytest.raises(ProbeRefused, match='changed'):
        replace(spec, executable_sha256='0' * 64).validate(frozenset())
    with pytest.raises(ProbeRefused, match='outside'):
        replace(spec, approval_root=tmp_path).validate(frozenset())
    if os.name != 'nt':
        alias = tmp_path / 'python-link'
        alias.symlink_to(spec.executable)
        with pytest.raises(ProbeRefused, match='non-symlink'):
            replace(spec, executable=alias).validate(frozenset())


def test_approval_env_explicit_and_validated(tmp_path):
    spec = _approval(tmp_path)
    with pytest.raises(ProbeRefused, match='environment'):
        replace(spec, env={'BAD=NAME': 'x'}).validate(frozenset())
    with pytest.raises(ProbeRefused, match='environment'):
        replace(spec, env={'SECRET': 'x\0y'}).validate(frozenset())


def test_invalid_model_and_session_ids_fails_before_request(tmp_path):
    spec = _approval(tmp_path, env=_byo_env(tmp_path / 'env'))
    with ExternalAcpProbe(spec, reserved_ids=frozenset()) as child:
        child.initialize()
        with pytest.raises(ProbeRefused, match='model ID'):
            child.select_model('abc', '')
        with pytest.raises(ProbeRefused, match='session ID'):
            child.close_session('')


def _mock_acp_script(path: Path, *, send_permission=False, endless_notifications=False):
    path.write_text(textwrap.dedent('''\
        import json, sys
        for raw in sys.stdin:
            msg=json.loads(raw)
            if 'method' not in msg: continue
            if msg['method']=='initialize':
                result={'protocolVersion':1}
            elif msg['method']=='session/new':
                if PERMISSION:
                    print(json.dumps({'jsonrpc':'2.0','id':'ask','method':'session/request_permission', 'params':{}}), flush=True)
                    denied=json.loads(sys.stdin.readline())
                    assert denied['error']['code'] == -32601
                if FLOOD:
                    for _ in range(150):
                        print(json.dumps({'jsonrpc':'2.0','method':'notification/progress','params':{}}), flush=True)
                result={'sessionId':'mock-123','models':{'availableModels':[]}}
            else:
                result={}
            print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)
    '''.replace('PERMISSION', str(send_permission)).replace('FLOOD', str(endless_notifications))), encoding='utf8')


def test_two_independent_acp_backends_switch_and_deny_agent_request(tmp_path):
    script = tmp_path / 'mock.py'
    _mock_acp_script(script, send_permission=True)
    byo = _approval(tmp_path / 'byo-work', env=_byo_env(tmp_path / 'isolated'))
    mock = _approval(tmp_path / 'mock-work', backend='mock-cli',
                     argv=('-u', str(script)), env={'PYTHONUNBUFFERED': '1'})
    with AcpProbeSwitch({'kiro-byo': byo, 'mock-cli': mock},
                        reserved_ids=frozenset({'kas', 'codex'})) as controller:
        first = controller.select('kiro-byo')
        first_sid = first.new_session()['sessionId']
        assert first_sid.startswith('kiro-byo-')
        second = controller.select('mock-cli')
        assert second.new_session()['sessionId'] == 'mock-123'
        assert controller.selected_backend == 'mock-cli'
        assert first._closed
        with pytest.raises(ProbeRefused, match='closed'):
            first.new_session()


def test_notification_flood_refused(tmp_path):
    script = tmp_path / 'flood.py'
    _mock_acp_script(script, endless_notifications=True)
    spec = _approval(tmp_path / 'work', backend='mock-cli', argv=('-u', str(script)))
    with ExternalAcpProbe(spec, reserved_ids=frozenset(), timeout=3) as child:
        child.initialize()
        with pytest.raises(ProbeRefused, match='flood'):
            child.new_session()


def test_upstream_crew_copy_is_byte_identical():
    crew = Path(os.environ.get('KIROCREW_SOURCE_ROOT', ''))
    if not crew.is_dir() or not (crew / 'src/kiro_crew/agent_sdk/external_acp_probe.py').exists():
        pytest.skip('no Crew R76 reference source supplied')
    assert (PROJECT / 'experiments/crew_external_acp_probe.py').read_bytes() == (
        crew / 'src/kiro_crew/agent_sdk/external_acp_probe.py').read_bytes()


def test_failed_switch_preserves_existing_acp_process(tmp_path):
    mock_path = tmp_path / 'mock.py'
    _mock_acp_script(mock_path)
    mock = _approval(tmp_path / 'mock-work', backend='mock-cli',
                     argv=('-u', str(mock_path)))
    untrusted = _approval(tmp_path / 'other-work', backend='other-cli',
                          argv=('-u', str(mock_path)))
    # Hash is well-formed but does not match the installed executable.
    untrusted = replace(untrusted, executable_sha256='0' * 64)
    with pytest.raises(ProbeRefused, match='changed'):
        AcpProbeSwitch({'mock-cli': mock, 'other-cli': untrusted}, reserved_ids=frozenset())
    # A previously trusted but now tampered binary is refused on selection, and
    # the active adapter is still alive (atomic switch).
    with AcpProbeSwitch({'mock-cli': mock}, reserved_ids=frozenset()) as selector:
        active = selector.select('mock-cli')
        assert active.new_session()['sessionId'] == 'mock-123'
        selector._approved['other-cli'] = untrusted
        with pytest.raises(ProbeRefused, match='changed'):
            selector.select('other-cli')
        assert selector.selected_backend == 'mock-cli'
        assert not active._closed
        assert active.new_session()['sessionId'] == 'mock-123'


def test_probe_does_not_inherit_an_unapproved_environment_secret(tmp_path, monkeypatch):
    script = tmp_path / 'env.py'
    script.write_text('''import json, os, sys
for raw in sys.stdin:
    msg = json.loads(raw)
    result = {'protocolVersion': 1, 'leaked': os.environ.get('BYO_TEST_SECRET_NEVER_INHERIT')}
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)
''', encoding='utf8')
    monkeypatch.setenv('BYO_TEST_SECRET_NEVER_INHERIT', 'top-secret-parent-only')
    spec = _approval(tmp_path / 'work', backend='mock-cli', argv=('-u', str(script)))
    with ExternalAcpProbe(spec, reserved_ids=frozenset()) as child:
        assert child.initialize()['leaked'] is None


def test_cancel_is_notification_and_privileged_request_params_fail_closed(tmp_path):
    spec = _approval(tmp_path / 'work', env=_byo_env(tmp_path / 'env'))
    with ExternalAcpProbe(spec, reserved_ids=frozenset()) as client:
        client.initialize()
        sid = client.new_session()['sessionId']
        assert client.cancel_session(sid) is None  # no JSON-RPC id, no reply expected
        # A request with an injected MCP server or non-model config cannot bypass
        # the public API through a direct call to the internal request helper.
        with pytest.raises(ProbeRefused, match='forbids MCP'):
            client._request('session/new', {'cwd': str(spec.cwd),
                        'mcpServers': [{'name': 'evil', 'command': 'sh'}]})
        with pytest.raises(ProbeRefused, match='allowlist'):
            client._request('session/set_config_option', {'sessionId': sid,
                        'configId': 'allow_all', 'value': 'on'})


def test_real_deepseek_effort_is_negotiated_per_session_not_guessed(tmp_path):
    """Real BYO ACP stdio child: DS fixed model -> effort -> Type -> deny.

    No provider call or Crew UI is involved; this proves only the ACP control
    plane, not host permission enforcement or the Dashboard endpoint.
    """
    root = tmp_path / 'work'
    env = _byo_env(tmp_path / 'isolated', 'tiga')
    config_path = Path(env['KIRO_BYO_HOME']) / 'config' / 'providers.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['providers'].append({
        'id': 'deepseek', 'name': 'DeepSeek', 'preset_id': 'deepseek',
        'type': 'deepseek-responses', 'base_url': 'https://api.deepseek.com',
        'models': [
            {'id': 'deepseek-v4.1-flash', 'name': 'DeepSeek-V4.1-Flash'},
            {'id': 'deepseek-v4-pro', 'name': 'DeepSeek-V4-Pro'},
        ],
    })
    config_path.write_text(json.dumps(config), encoding='utf-8')
    spec = _approval(root, env=env)
    with ExternalAcpProbe(spec, reserved_ids=frozenset({'kas', 'codex'})) as client:
        assert client.initialize()['protocolVersion'] == 1
        created = client.new_session()
        sid = created['sessionId']
        assert created['models']['currentModelId'] == 'kiro-byo-type:tiga:multi'
        with pytest.raises(ProbeRefused, match='not advertised'):
            client.select_effort(sid, 'max')
        updated = client.select_model(sid, 'DeepSeek-V4.1-Flash')
        effort = next(x for x in updated['configOptions'] if x['id'] == 'effortLevel')
        assert {row['value'] for row in effort['options']} == {'low', 'high', 'max'}
        assert effort['currentValue'] == 'high'
        selected = client.select_effort(sid, 'max')
        assert next(x for x in selected['configOptions'] if x['id'] == 'effortLevel')['currentValue'] == 'max'
        with pytest.raises(ProbeRefused, match='not advertised'):
            client.select_effort(sid, 'invalid-effort')
        with pytest.raises(ProbeRefused, match='outside .* allowlist'):
            client._request('session/set_config_option', {
                'sessionId': sid, 'configId': 'allow_all', 'value': 'true',
            })
        client.select_model(sid, 'kiro-byo-type:tiga:multi')
        with pytest.raises(ProbeRefused, match='not advertised'):
            client.select_effort(sid, 'max')
        client.close_session(sid)
        restored = client.load_session(sid)
        assert restored['models']['currentModelId'] == 'kiro-byo-type:tiga:multi'
        with pytest.raises(ProbeRefused, match='not advertised'):
            client.select_effort(sid, 'high')


def test_unadvertised_effort_cannot_be_reused_after_model_switch_without_new_options(tmp_path):
    """ACP v1 model change may return {}, which must clear previous effort grants."""
    spec = _approval(tmp_path / 'work', env=_byo_env(tmp_path / 'home'))
    with ExternalAcpProbe(spec, reserved_ids=frozenset()) as client:
        client.initialize()
        sid = client.new_session()['sessionId']
        client._advertised_options[sid] = {'effortLevel': frozenset({'max'})}
        assert 'max' in client._advertised_options[sid]['effortLevel']
        # v1 session/set_model in BYO emits config_option_update but responds {}.
        assert client.select_model(sid, 'Strong', v3=False) == {}
        assert 'effortLevel' not in client._advertised_options.get(sid, {})
        with pytest.raises(ProbeRefused, match='not advertised'):
            client.select_effort(sid, 'max')
