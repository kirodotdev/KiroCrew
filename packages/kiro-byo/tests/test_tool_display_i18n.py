"""ACP tool title localization is presentation only: the tool contract is unchanged."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.i18n import LOCALES, locale_messages
from kiro_byo.tool_display import display_locale, tool_display_title


@pytest.mark.parametrize('lang,want', [
    ('zh-CN','读取文件'), ('zh-TW','讀取檔案'), ('en','Read file'),
    ('ja','ファイルを読み取る'), ('ko','파일 읽기'),
])
def test_native_read_title_localized_in_five_supported_ui_languages(lang, want):
    assert tool_display_title('read', ui_language=lang, native_name='read') == want


@pytest.mark.parametrize('lang', LOCALES)
def test_all_native_title_keys_and_mcp_placeholder_exist(lang):
    keys = [
        'read','read_files','list_directory','glob','grep','write','edit',
        'delete_file','web_fetch','web_search','shell','control_shell_process',
        'code_symbols','code_query','update_plan','mcp',
    ]
    msgs = locale_messages(lang)
    assert all(msgs.get('tool.title.'+name) for name in keys)
    assert msgs['tool.title.mcp'].count('{v0}') == 1


def test_ui_language_is_independent_of_user_response_language():
    session = Session('s1', '.', 'model')
    session.ui_language_preference = 'ja'
    # The selected model and reasoning policy must never affect the display locale.
    session.response_language_preference = 'zh-CN'
    session.reasoning_language_preference = 'en'
    session.native = SimpleNamespace(
        has_tool=lambda name: name == 'read',
        canonical_name=lambda name: name,
        kind_for=lambda name: 'read',
    )
    server = object.__new__(AcpServer)
    result = server._tool_call_payload(session, 'one', 'read', {'path':'hello.txt'}, status='pending')
    assert result['title'] == 'ファイルを読み取る'
    assert result['toolCallId'] == 'one'
    assert result['kind'] == 'read'
    assert result['rawInput'] == {'path':'hello.txt'}
    assert result['status'] == 'pending'
    assert result['_meta']['kiro']['toolId'] == 'read'


def test_mcp_title_five_languages_but_preserves_identity_and_raw_arguments():
    public = 'mcp__core__ping'
    binding = SimpleNamespace(server_name='core', tool_name='ping', annotations={})
    session = Session('s2', '.', 'model')
    session.mcp = SimpleNamespace(bindings={public:binding})
    server = object.__new__(AcpServer)
    for lang in LOCALES:
        session.ui_language_preference = lang
        result = server._tool_call_payload(session,'call',public,{'anything':'value'})
        assert result['title'] == locale_messages(lang)['tool.title.mcp'].replace('{v0}','ping')
        assert result['kind'] == 'other'
        assert result['rawInput'] == {'anything':'value'}
        assert result['_meta']['kiro']['mcpServerName'] == 'core'
        assert result['_meta']['kiro']['toolName'] == 'ping'
        assert result['_meta']['kiro']['mcpTool']['identity'] == {'serverName':'core','toolName':'ping'}


@pytest.mark.parametrize('system,want', [
    ('zh_CN.UTF-8','zh-CN'), ('zh_HK.UTF-8','zh-TW'), ('ja_JP.UTF-8','ja'),
    ('ko_KR','ko'), ('en_US','en'), ('C.UTF-8','en'), ('xx_YY','en'),
])
def test_auto_locale_matches_system_when_crew_language_unavailable(system,want):
    assert display_locale('auto',system_language=system) == want


def test_unknown_tool_and_preference_fail_soft_and_never_translate_arguments():
    assert tool_display_title('unrecognized_tool',ui_language='zh-CN',native_name='unrecognized_tool') == 'unrecognized_tool'
    assert tool_display_title('custom_unknown',ui_language='ja') == 'custom_unknown'
    assert display_locale('not-a-language',system_language='en_US') == 'en'
    args = {'command':'echo Error: do not translate this'}
    session = Session('s3', '.', 'model',ui_language_preference='zh-CN')
    session.native = SimpleNamespace(has_tool=lambda name: name == 'shell',canonical_name=lambda name: name,kind_for=lambda name: 'execute')
    result = object.__new__(AcpServer)._tool_call_payload(session,'call','shell',args)
    assert result['title'] == '执行终端命令'
    assert result['rawInput'] is args


def test_missing_locale_assets_never_block_tool_dispatch(monkeypatch):
    def broken(_locale):
        raise OSError("asset unavailable")
    monkeypatch.setattr('kiro_byo.tool_display.locale_messages', broken)
    assert tool_display_title('read', ui_language='ja', native_name='read') == 'read'
