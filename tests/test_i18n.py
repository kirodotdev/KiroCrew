"""Five-locale resource, preference, ACP and chart integration contracts."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from kiro_byo.i18n import LOCALES, locale_messages, normalize_ui_language, error_code
from kiro_byo.paths import use_app_home
from kiro_byo.provider_admin import (
    ui_language_settings, set_ui_language_preference,
    response_language_settings, set_response_language_preference,
)
from kiro_byo.reasoning_language import (
    resolve_response_language, resolve_reasoning_language, classify_text_language,
)
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import load_catalog_config

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'src' / 'kiro_byo' / 'locales'
UI = (ROOT / 'ui' / 'index.mjs').read_text(encoding='utf-8')


def parse_no_duplicate_keys(text):
    def check(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f'duplicate key: {key}')
            out[key] = value
        return out
    return json.loads(text, object_pairs_hook=check)


def test_all_locale_assets_are_complete_nonduplicated_and_used():
    assert len(LOCALES) == 5
    resources = {loc:parse_no_duplicate_keys((ASSETS / f'{loc}.json').read_text(encoding='utf-8')) for loc in LOCALES}
    expected = set(resources['en'])
    assert len(expected) >= 500
    assert all(re.fullmatch(r'[a-zA-Z0-9_.-]+', key) for key in expected)
    placeholders = lambda value: set(re.findall(r'\{v\d+\}', value))
    for locale, rows in resources.items():
        assert set(rows) == expected, locale
        assert all(isinstance(v,str) and v.strip() for v in rows.values()), locale
        assert all(placeholders(rows[k]) == placeholders(resources['en'][k]) for k in expected), locale
        assert locale_messages(locale)['settings.ui_language'] == rows['settings.ui_language']
    ui_keys = set(re.findall(r'\btr\([\'\"](ui\.[a-z0-9]+)[\'\"]', UI))
    assert ui_keys, 'UI must use translated keys'
    assert ui_keys <= expected
    assert '测试连接' not in UI
    assert '清除请求记录' not in UI


@pytest.mark.parametrize('configured,want',[('ja','ja'),('zh-tw','zh-TW'),('zh-hant','zh-TW'),('en-US','en'),('zh-Hans','zh-CN'),('auto','auto')])
def test_ui_locale_normalization(configured, want):
    assert normalize_ui_language(configured) == want


def test_locale_invalid_rejected_and_auto_falls_back_to_english():
    with pytest.raises(ValueError): normalize_ui_language('invalid/../loc')
    assert locale_messages('auto') == locale_messages('en')


def test_ui_and_response_preferences_persist_separately_from_providers_and_type():
    with tempfile.TemporaryDirectory() as tmp, use_app_home(Path(tmp)):
        config_dir=Path(tmp)/'config'; config_dir.mkdir(parents=True)
        file = config_dir/'providers.json'
        file.write_text(json.dumps({'providers':[], 'mode':{'pack':'tiga', 'policy':'sky'}}))
        set_ui_language_preference('ko')
        set_response_language_preference('auto')
        data=json.loads(file.read_text())
        assert data['providers'] == []
        assert data['mode']['pack'] == 'tiga'
        assert data['mode']['policy'] == 'sky'
        assert data['preferences']['ui_language'] == 'ko'
        assert data['preferences']['response_language'] == 'auto'
        assert ui_language_settings()['value']=='ko'
        assert response_language_settings()['value']=='auto'
        assert load_catalog_config().response_language=='auto'
        assert load_catalog_config().ui_language=='ko'
        set_response_language_preference('ja')
        assert response_language_settings()['value']=='ja'
        assert ui_language_settings()['value']=='ko'


def test_auto_classification_ignores_code_and_follows_current_message():
    code = '''请帮我看这里：
```python
import os
http_request = "This is a lot of English source code"
```
中文逻辑是什么？'''
    assert classify_text_language(code) == 'zh-CN'
    hist=[{'role':'user','content':'我们继续处理中文的任务。'}]
    assert resolve_response_language('auto','Can you explain this API now?',hist)[0]=='en'
    assert resolve_response_language('auto','日本語で説明してください',hist)[0]=='ja'
    assert resolve_response_language('auto','설명을 부탁드립니다',hist)[0]=='ko'
    assert resolve_response_language('auto','請檢查這個設定，顯示有問題',hist)[0]=='zh-TW'
    assert resolve_response_language('auto','ACP?',hist)[0]=='zh-CN'


def test_explicit_language_request_takes_priority_without_changing_reasoning():
    response = resolve_response_language('zh-CN','Please respond in English and explain the circuit.',[])
    assert response[:2] == ('en','explicit-user-request')
    reasoning = resolve_reasoning_language('ja','Please respond in English.',[])
    assert reasoning.language == 'ja'
    assert resolve_response_language('auto','请用日语回答。',[])[0] == 'ja'


def test_session_injection_ephemeral_no_acp_stream_or_thinking_change():
    server=object.__new__(AcpServer)
    server.runtime_trace=None
    s=Session('test','/tmp','demo')
    s.response_language_preference='en'
    server._apply_reasoning_language_policy(s,'ja','中文问题')
    assert 'Japanese (ja)' in s.language_instruction
    assert 'English (en)' in s.language_instruction
    assert s.response_language=='en'
    assert s.reasoning_language=='ja'
    assert 'hidden/private chain-of-thought' in s.language_instruction
    assert not hasattr(s,'translated_reasoning')
    server._apply_reasoning_language_policy(s,'auto','Now switch the language to English.')
    assert s.reasoning_language=='en'


def test_provider_best_effort_and_absent_summary_are_explicit():
    assert 'settings.provider_best_effort' in locale_messages('en')
    assert 'settings.visible_reasoning_help' in locale_messages('en')
    assert 'provider' in locale_messages('en')['settings.visible_reasoning_help'].lower()
    assert error_code('') is None


def test_ui_switch_in_place_no_host_reload_no_fetch_override():
    assert "'/preferences/ui-language'" in UI
    assert 'setUiLocale(target)' in UI
    assert 'setStatus(s)' in UI
    assert 'window.location.reload' not in UI
    assert 'window.fetch=' not in UI and 'globalThis.fetch=' not in UI
    assert 'document.location=' not in UI
    assert 'languageOptions=LOCALE_OPTIONS' in UI


def test_latency_chart_uses_actual_hourly_stats_without_fabrication():
    assert 'function LatencyTrend({hourly=[]})' in UI
    assert 'avg_ms' in UI
    assert 'polyline' in UI
    assert 'const valid=samples.filter(' in UI
    assert 'if(!valid.length)' in UI
    assert 'ka-overview-hourbar' not in UI


def test_host_route_and_package_data_integration():
    backend=(ROOT/'backend/routes.py').read_text(encoding='utf-8')
    assert 'AppRoute("GET", "/locales/{locale}", get_locale)' in backend
    for endpoint in ('/preferences/ui-language','/preferences/response-language'):
        assert endpoint in backend
    project=(ROOT/'pyproject.toml').read_text(encoding='utf-8')
    assert 'locales/*.json' in project
    assert all((ASSETS/f'{loc}.json').is_file() for loc in LOCALES)


def test_error_code_is_additive_and_unknown_diagnostic_is_preserved():
    assert error_code('Session is busy')=='SESSION_BUSY'
    assert error_code('401 unauthorized')=='PROVIDER_AUTH_FAILED'
    assert error_code('Model foo is not available')=='MODEL_NOT_AVAILABLE'
    assert error_code('This unexpected trace must be shown verbatim') is None


def test_language_auto_uses_configured_ui_as_ambiguity_fallback():
    assert resolve_response_language('auto', 'ACP?', [], fallback_language='zh-CN')[0]=='zh-CN'
    assert resolve_response_language('auto', '請檢查設定', [], fallback_language='zh-CN')[0]=='zh-TW'
    response = resolve_response_language('auto', '检查一下模型', [], fallback_language='en')
    assert response[0]=='zh-CN'
    assert 'Use Simplified Chinese characters throughout' in response[2]

def test_explicit_user_language_overrides_setting_for_both_auto_and_response():
    assert resolve_response_language('zh-CN', '请用繁体中文回答。')[0]=='zh-TW'
    assert resolve_reasoning_language('auto', '请用繁体中文回答。').language=='zh-TW'
