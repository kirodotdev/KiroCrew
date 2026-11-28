from __future__ import annotations

import http.client
import inspect
import json
import unittest
import urllib.request
from unittest.mock import patch

from kiro_byo.providers import chat_completions_common, moonshot_chat, qwen_chat, xiaomi_mimo, zhipu_chat
from kiro_byo.providers.base import ProviderTransportError
from kiro_byo.providers.http_sse import iter_response_lines, read_response_bytes, post_json, safe_request_url
from kiro_byo.providers.qwen_chat import QwenChatInterrupted


class _ClosedResponse:
    def read(self) -> bytes:
        raise ValueError("I/O operation on closed response")




class _IncompleteResponse:
    def read(self) -> bytes:
        raise http.client.IncompleteRead(b"", 8)


class _IncompleteStream:
    def __iter__(self):
        yield b"data: first\n"
        raise http.client.IncompleteRead(b"", 4)


class ProviderSharedTransportTests(unittest.TestCase):
    def test_native_chat_adapters_delegate_wire_engine(self) -> None:
        factory = inspect.getsource(chat_completions_common.make_native_chat_turn)
        self.assertIn("run_native_chat_completion(", factory)
        for module in (zhipu_chat, moonshot_chat, qwen_chat, xiaomi_mimo):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertIn("make_native_chat_turn(", source)
                self.assertIn("make_native_chat_text(", source)
                self.assertNotIn("run_native_chat_completion(", source)
                self.assertNotIn("for raw_line in", source)
                self.assertNotIn("urllib.error", source)
                self.assertNotIn("json.loads", source)

    def test_native_request_policies_preserve_session_configuration(self) -> None:
        from kiro_byo.config import ProviderSpec

        original = {'stream_options': {'custom_key': 'preserve'}, 'vendor_hint': 'keep'}
        options = original['stream_options']
        assert isinstance(options, dict)
        provider = ProviderSpec(
            id='p1', name='Provider', type='qwen-chat-v1',
            base_url='https://example.test/v1', api_key='', models=(),
            stream=True, extra_body=original,
        )
        for module in (moonshot_chat, qwen_chat, xiaomi_mimo, zhipu_chat):
            with self.subTest(provider=module.__name__):
                body = module._body(
                    provider, 'm1', [{'role': 'user', 'content': 'hi'}],
                    reasoning_effort=None, thinking_mode='auto', model=None,
                    tools=[{'type': 'function', 'function': {'name': 'ping'}}],
                )
                self.assertEqual(body['vendor_hint'], 'keep')
                self.assertEqual(body['tool_choice'], 'auto')
                self.assertEqual(body['tools'][0]['function']['name'], 'ping')
                self.assertEqual(original['stream_options'], {'custom_key': 'preserve'})
                if module in (moonshot_chat, qwen_chat):
                    self.assertIsNot(body['stream_options'], options)
                    self.assertTrue(body['stream_options']['include_usage'])

    def test_shared_read_raises_callers_interruption_type(self) -> None:
        with self.assertRaises(QwenChatInterrupted):
            read_response_bytes(
                _ClosedResponse(),
                lambda: True,
                interrupted_type=QwenChatInterrupted,
            )

    def test_incomplete_nonstream_body_is_normalized_as_transport_error(self) -> None:
        with self.assertRaises(ProviderTransportError) as ctx:
            read_response_bytes(_IncompleteResponse(), lambda: False)
        self.assertIn("incomplete read", str(ctx.exception).lower())
        self.assertEqual(ctx.exception.category, "transport")
        self.assertTrue(ctx.exception.retryable)

    def test_incomplete_stream_is_normalized_after_prior_lines(self) -> None:
        stream = iter_response_lines(_IncompleteStream(), lambda: False)
        self.assertEqual(next(stream), b"data: first\n")
        with self.assertRaises(ProviderTransportError):
            next(stream)

    def test_unicode_endpoint_is_ascii_safe_but_post_body_remains_utf8(self) -> None:
        address = "https://例え.テスト/v1/日本語?q=中文&x=%2F"
        normalized = safe_request_url(address)
        self.assertEqual(
            normalized,
            "https://xn--r8jz45g.xn--zckzah/v1/%E6%97%A5%E6%9C%AC%E8%AA%9E?q=%E4%B8%AD%E6%96%87&x=%2F",
        )
        target = urllib.request.Request(normalized).selector
        http.client.HTTPConnection._encode_request(None, f"POST {target} HTTP/1.1")
        requests = []
        def opener(request, *, timeout):
            requests.append(request)
            return object()
        post_json(url=address, headers={"Content-Type": "application/json"},
                  body={"prompt": "中文と日本語"}, timeout_seconds=5,
                  error_type=Exception, opener=opener)
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(requests[0].data.decode("utf-8")), {"prompt": "中文と日本語"})
        self.assertTrue(requests[0].full_url.isascii())

    def test_existing_percent_encoded_path_is_not_double_encoded(self) -> None:
        self.assertEqual(safe_request_url("https://example.com/%E6%97%A5?q=%E4%B8%AD"),
                         "https://example.com/%E6%97%A5?q=%E4%B8%AD")


if __name__ == "__main__":
    unittest.main()


def test_state_replay_stops_when_cancelled_during_stale_state_response(monkeypatch):
    import threading
    import urllib.error
    import pytest
    from kiro_byo.config import ProviderSpec
    from kiro_byo.providers import responses_api, gemini_interactions
    from kiro_byo.providers.capabilities import ResponsesCapabilities

    interrupted = threading.Event()
    provider = ProviderSpec(
        'p', 'Example', 'openai-responses', 'https://example.test/v1', '', (),
    )
    calls = []

    def stale_response(request, *, timeout):
        calls.append(request)
        interrupted.set()
        raise urllib.error.HTTPError('https://example.test', 404, 'expired', {}, None)

    monkeypatch.setattr(responses_api.urllib.request, 'urlopen', stale_response)
    monkeypatch.setattr(responses_api, '_body', lambda *args, **kwargs: {
        'previous_response_id': 'expired' if kwargs.get('allow_previous', True) else None,
    })
    monkeypatch.setattr(
        responses_api, 'responses_capabilities',
        lambda typ: ResponsesCapabilities(previous_response_retry_statuses=frozenset({404})),
    )
    with pytest.raises(responses_api.ResponsesAPIInterrupted):
        responses_api.chat_completion_turn(
            provider, 'm', [{'role': 'user', 'content': 'hi'}], lambda _: None,
            should_interrupt=interrupted.is_set,
        )
    assert len(calls) == 1

    interrupted.clear()
    calls.clear()

    def stale_interaction(*args, **kwargs):
        calls.append(1)
        interrupted.set()
        raise urllib.error.HTTPError('https://example.test', 404, 'expired', {}, None)

    monkeypatch.setattr(gemini_interactions, '_request', stale_interaction)
    monkeypatch.setattr(gemini_interactions, '_body', lambda *args, **kwargs: {
        'previous_interaction_id': 'expired' if not kwargs.get('force_stateless') else None,
    })
    with pytest.raises(gemini_interactions.GeminiInteractionsInterrupted):
        gemini_interactions.chat_completion_turn(
            provider, 'm', [{'role': 'user', 'content': 'hi'}], lambda _: None,
            should_interrupt=interrupted.is_set,
        )
    assert len(calls) == 1


def test_auth_and_request_http_errors_never_authorize_model_fallback():
    import urllib.error
    import pytest
    from kiro_byo.providers.base import ProviderError

    def rejected(code):
        def opener(request, *, timeout):
            raise urllib.error.HTTPError('https://example.test', code, 'rejected', {}, None)
        with pytest.raises(ProviderError) as result:
            post_json(
                url='https://example.test/v1/chat/completions', headers={},
                body={'model': 'example'}, timeout_seconds=2,
                error_type=ProviderError, opener=opener,
            )
        return result.value

    for status in (400, 401, 403, 404, 422):
        assert rejected(status).failover_allowed is False
    assert rejected(429).failover_allowed is True
    assert rejected(503).failover_allowed is True


def test_unmapped_explicit_thinking_off_is_hard_failure_not_routing_signal():
    import pytest
    from kiro_byo.config import ModelSpec, ProviderSpec
    from kiro_byo.providers.openai_compatible import OpenAICompatibleError, _body

    provider = ProviderSpec('p', 'Example', 'openai-compatible', 'https://example.test/v1', '', ())
    model = ModelSpec(
        'p', 'Example', 'm', 'M', reasoning_can_disable=True,
        reasoning_disable_value='disabled',
    )
    with pytest.raises(OpenAICompatibleError) as result:
        _body(provider, 'm', [{'role': 'user', 'content': 'hello'}],
              thinking_mode='off', model=model)
    assert result.value.failover_allowed is False


def test_responses_and_gemini_auth_rejections_share_failover_boundary(monkeypatch):
    import urllib.error
    import pytest
    from kiro_byo.config import ProviderSpec
    from kiro_byo.providers import responses_api, gemini_interactions

    provider = ProviderSpec('p', 'Example', 'openai-responses', 'https://example.test/v1', '', ())
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError('https://example.test', 401, 'unauthorized', {}, None)

    monkeypatch.setattr(responses_api.urllib.request, 'urlopen', denied)
    with pytest.raises(responses_api.ResponsesAPIError) as resp_error:
        responses_api.chat_completion_turn(provider, 'm', [], lambda _: None)
    assert resp_error.value.failover_allowed is False

    monkeypatch.setattr(gemini_interactions, '_request', denied)
    with pytest.raises(gemini_interactions.GeminiInteractionsError) as gemini_error:
        gemini_interactions.chat_completion_turn(provider, 'm', [], lambda _: None)
    assert gemini_error.value.failover_allowed is False


def test_legacy_text_adapters_delegate_to_current_turn_entry() -> None:
    """All wire formats share one dynamic text-only/compaction wrapper.

    A patched Turn entry must still be honored, preserving the old API and
    ensuring the text-only path cannot silently fork from normal execution.
    """
    from kiro_byo.config import ProviderSpec
    from kiro_byo.providers import (
        anthropic_messages, cohere_v2, gemini_interactions, mistral_chat,
        openai_compatible, responses_api,
    )
    from kiro_byo.providers.base import ChatTurn

    provider = ProviderSpec(
        'test', 'Test', 'openai-compatible', 'https://example.test/v1', '', (),
    )
    messages = [{'role': 'user', 'content': 'ping'}]
    for module in (
        anthropic_messages, cohere_v2, gemini_interactions, mistral_chat,
        openai_compatible, responses_api,
    ):
        with patch.object(module, 'chat_completion_turn', return_value=ChatTurn(text='ready')) as turn:
            result = module.chat_completion(
                provider, 'model', messages, lambda _: None,
                thinking_mode='off', reasoning_effort=None, cache_key='cache',
            )
        assert result == 'ready', module.__name__
        assert turn.call_count == 1, module.__name__
        assert turn.call_args.kwargs['thinking_mode'] == 'off'
        assert turn.call_args.kwargs['cache_key'] == 'cache'
        assert turn.call_args.kwargs.get('tools') is None
        assert module.chat_completion.__module__ == module.__name__
        source = inspect.getsource(module)
        assert 'chat_completion = make_chat_text(lambda: chat_completion_turn)' in source
        assert 'def chat_completion(' not in source


def test_native_state_context_rejects_malformed_or_cross_provider_identity():
    from kiro_byo.config import ProviderSpec
    from kiro_byo.providers.base import matching_context
    from kiro_byo.providers import anthropic_messages, cohere_v2, gemini_interactions, responses_api, chat_completions_common

    provider = ProviderSpec('123', 'Original', 'openai-responses', 'https://example.test', '', ())
    for module in (anthropic_messages, cohere_v2, gemini_interactions, responses_api, chat_completions_common):
        lookup = getattr(module, '_matching_context', None) or getattr(module, '_context', None) or getattr(module, 'matching_context')
        assert lookup({'_kiro_provider_context': {'provider_id': '123', 'transport': 'openai-responses', 'data': {'response_id': 'valid'}}}, provider) == {'response_id': 'valid'}
        for broken in (
            {'provider_id': 123, 'transport': 'openai-responses', 'data': {'response_id': 'wrong-type'}},
            {'provider_id': 'different', 'transport': 'openai-responses', 'data': {'response_id': 'cross-tenant'}},
            {'provider_id': '123', 'transport': 'anthropic-messages', 'data': {'response_id': 'cross-transport'}},
            {'provider_id': '123', 'transport': 'openai-responses', 'data': 'invalid'},
        ):
            assert lookup({'_kiro_provider_context': broken}, provider) is None
    assert matching_context({}, provider) is None


def test_state_replay_retry_is_single_use_and_never_retries_auth_failures():
    import pytest
    import urllib.error
    from kiro_byo.providers.base import ProviderError, ProviderInterrupted
    from kiro_byo.providers.http_sse import open_with_state_fallback

    def attempt(statuses, initial, *, expired=(404,), cancelled=False):
        calls = []
        fallbacks = []
        def opening(body):
            calls.append(dict(body))
            status = statuses[len(calls)-1]
            if status:
                raise urllib.error.HTTPError('https://example.test', status, 'failure', {}, None)
            return {'done': True}
        def fallback():
            fallbacks.append(True)
            return {'model': 'm', 'history': 'full'}
        args = dict(
            open_request=opening, state_field='previous_response_id',
            retry_statuses=expired, make_stateless=fallback,
            should_interrupt=(lambda: cancelled), interrupted_type=ProviderInterrupted,
            error_type=ProviderError, timeout_seconds=2,
        )
        if initial in ('pass','reject','cancel'):
            if initial == 'pass':
                result = open_with_state_fallback({'previous_response_id':'old'},**args)
                assert result == {'done': True}
            elif initial == 'cancel':
                with pytest.raises(ProviderInterrupted):
                    open_with_state_fallback({'previous_response_id':'old'},**args)
            else:
                with pytest.raises(ProviderError) as exc:
                    open_with_state_fallback({'previous_response_id':'old'},**args)
                if statuses[-1] in (401, 403):
                    assert exc.value.failover_allowed is False
        return calls, fallbacks

    assert attempt((404, None), 'pass') == ([{'previous_response_id': 'old'}, {'model': 'm', 'history': 'full'}], [True])
    assert attempt((401,), 'reject') == ([{'previous_response_id': 'old'}], [])
    assert attempt((404,), 'cancel', cancelled=True) == ([{'previous_response_id': 'old'}], [])
    assert attempt((404, 404), 'reject') == ([{'previous_response_id': 'old'}, {'model': 'm', 'history': 'full'}], [True])

    # Without a stored state anchor, an upstream 404 is not a replay signal.
    calls = []
    with pytest.raises(ProviderError):
        open_with_state_fallback(
            {}, open_request=lambda body: calls.append(body) or (_ for _ in ()).throw(
                urllib.error.HTTPError('https://example.test', 404, 'missing', {}, None)),
            state_field='previous_response_id', retry_statuses=(404,),
            make_stateless=lambda: pytest.fail('unexpected retry'),
            should_interrupt=None, interrupted_type=ProviderInterrupted,
            error_type=ProviderError, timeout_seconds=1,
        )
    assert len(calls) == 1
