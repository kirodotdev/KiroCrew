"""Regression coverage for context-authorized pako output downstream passes."""

from __future__ import annotations

import base64
import dataclasses
import json
import zlib
from collections.abc import Callable

import pytest

from kiro_crew import security
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat_handlers import _redact_history_rows
from kiro_crew.dashboard.chat_utils import (
    _build_stream_chunk,
    _prepare_messages,
    redact_display_content,
)
from kiro_crew.discord.renderer import _redact_transformed
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.feishu.renderer import FeishuRenderer
from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
from kiro_crew.imessage.renderer import IMessageRenderer
from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.driver import _redact as turn_driver_redact
from kiro_crew.messaging.renderer import (
    apply_options_cap,
    display_safe_for,
    format_overflow,
    render_options_as_text,
)
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.platform import (
    build_default_context,
    redact_pako_via_context,
    reset_context,
    set_context,
)
from kiro_crew.slack.format import build_options_blocks, render_for_slack
from kiro_crew.slack.handler import _display_redactor
from kiro_crew.slack.renderer import _display_safe as slack_renderer_display_safe
from kiro_crew.teams.renderer import _display_safe as teams_display_safe
from kiro_crew.telegram.renderer import build_inline_keyboard, md_to_telegram_html_safe
from kiro_crew.webex.renderer import _authorized_display_safe, webex_display_safe
from kiro_crew.wecom.renderer import WeComRenderer
from kiro_crew.wecom.transport import WECOM_CAPABILITIES
from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES
from kiro_crew.weixin.turn_renderer import WeixinRenderer
from kiro_crew.whatsapp.renderer import to_whatsapp_text

_BASELINE_CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"
_COMPANION_CREDENTIAL = "HOST-PRIVATE-VALUE"
_EXFIL_URL = "https://example.invalid/?blob=" + "A" * 220


def _pako_url(code: str) -> tuple[str, str]:
    state = json.dumps({"code": code}, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(zlib.compress(state, 9)).decode().rstrip("=")
    return f"https://mermaid.live/edit#pako:{payload}", payload


@pytest.fixture
def active_companion_policy():
    """Install one host-only marker that the public baseline does not know."""

    class _Policy:
        def redact(self, text: str) -> str:
            return security.redact(text).replace(
                _COMPANION_CREDENTIAL,
                "[REDACTED: host credential]",
            )

        def exempt_exact_hosts(self) -> frozenset[str]:
            return frozenset()

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, credentials=_Policy()))
    try:
        yield
    finally:
        reset_context()


def _imessage_transform(text: str) -> str:
    renderer = IMessageRenderer(object(), "+15550100000", IMESSAGE_CAPABILITIES)  # type: ignore[arg-type]
    renderer._buf = [text]
    return renderer.delivery_text()


def _feishu_transform(text: str) -> str:
    renderer = FeishuRenderer(object(), "message", FEISHU_CAPABILITIES)  # type: ignore[arg-type]
    renderer._buf = [text]
    return renderer.text()


def _wecom_transform(text: str) -> str:
    renderer = WeComRenderer(object(), "request", "", WECOM_CAPABILITIES)  # type: ignore[arg-type]
    return renderer._render_slice(text, final=True)


def _weixin_transform(text: str) -> str:
    renderer = WeixinRenderer(  # type: ignore[arg-type]
        object(),
        "user",
        WEIXIN_CAPABILITIES,
        ctx_store=object(),
        account_id="account",
    )
    return renderer.redact_for_target(text)


def _slack_native_transform(text: str) -> str:
    return redact_for_display(text, _display_redactor)[0]


def _slack_principal_transform(text: str) -> str:
    return "".join(render_for_slack(text))


def _whatsapp_transform(text: str) -> str:
    return to_whatsapp_text(text, redactor=redact_pako_via_context)


_AUTHORIZED_TRANSFORMS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("slack principal", _slack_principal_transform),
    ("slack transport sibling", slack_renderer_display_safe),
    ("slack native fallback", _slack_native_transform),
    ("discord", _redact_transformed),
    ("telegram", md_to_telegram_html_safe),
    ("teams", teams_display_safe),
    ("webex", _authorized_display_safe),
    ("whatsapp", _whatsapp_transform),
    ("imessage", _imessage_transform),
    ("wecom", _wecom_transform),
    ("weixin", _weixin_transform),
    ("feishu", _feishu_transform),
)


@pytest.mark.parametrize(("branch", "transform"), _AUTHORIZED_TRANSFORMS)
def test_turn_driver_authorization_survives_every_downstream_transform(
    branch: str,
    transform: Callable[[str], str],
    active_companion_policy: None,
) -> None:
    """Safe pako survives; unsafe pako and ordinary egress hazards do not."""
    safe_url, _safe_payload = _pako_url("flowchart TD\n  A --> B")
    unsafe_url, unsafe_payload = _pako_url(f"flowchart TD\n  A[{_COMPANION_CREDENTIAL}] --> B")
    raw = "\n".join((safe_url, unsafe_url, _BASELINE_CREDENTIAL, _EXFIL_URL))

    authorized = turn_driver_redact(raw)
    transformed = transform(authorized)

    assert safe_url in transformed, branch
    assert unsafe_url not in transformed, branch
    assert unsafe_payload not in transformed, branch
    assert "[REDACTED: encoded credential]" in transformed, branch
    assert _BASELINE_CREDENTIAL not in transformed, branch
    assert "[REDACTED: credential]" in transformed, branch
    assert _EXFIL_URL not in transformed, branch
    assert security.EXFILTRATION_REDACTION_TAG_PREFIX in transformed, branch


def test_shared_option_helpers_preserve_only_explicitly_authorized_pako(
    active_companion_policy: None,
) -> None:
    safe_url, _ = _pako_url("flowchart TD\n  A --> B")
    button_caps = dataclasses.replace(DISCORD_CAPABILITIES, max_buttons=1)
    text_caps = dataclasses.replace(DISCORD_CAPABILITIES, max_buttons=0)

    raw_kept = apply_options_cap("", [safe_url], button_caps)[1]
    assert safe_url not in raw_kept[0]
    assert "[REDACTED: encoded credential]" in raw_kept[0]
    assert safe_url not in format_overflow([safe_url], 0)
    assert safe_url not in render_options_as_text(f"[OPTIONS: {safe_url}]", text_caps)

    authorized_kept = apply_options_cap(
        "",
        [safe_url],
        button_caps,
        redactor=redact_pako_via_context,
    )[1]
    authorized_text = render_options_as_text(
        f"[OPTIONS: {safe_url}]",
        text_caps,
        redactor=redact_pako_via_context,
    )
    assert authorized_kept == [safe_url]
    assert safe_url in authorized_text


def test_channel_option_sinks_keep_authorized_pako(active_companion_policy: None) -> None:
    safe_url, _ = _pako_url("flowchart TD\n  A --> B")
    telegram = build_inline_keyboard([safe_url], "telegram:session")
    assert telegram is not None
    assert telegram["inline_keyboard"][0][0]["text"] == safe_url[:64]

    blocks = build_options_blocks([safe_url])
    assert blocks[0]["elements"][0]["options"][0]["value"] == safe_url[:150]

    for renderer in (
        IMessageRenderer(object(), "+15550100000", IMESSAGE_CAPABILITIES),  # type: ignore[arg-type]
        FeishuRenderer(object(), "message", FEISHU_CAPABILITIES),  # type: ignore[arg-type]
    ):
        renderer._buf = [f"[OPTIONS: {safe_url}]"]
        assert safe_url in renderer.text()


def test_raw_and_proactive_defaults_remain_fail_closed(active_companion_policy: None) -> None:
    """No public or raw helper acquires pako restoration by default."""
    safe_url, _ = _pako_url("flowchart TD\n  A --> B")
    caps = TransportCapabilities(mention_grammars=False)

    for output in (
        security.redact(safe_url),
        display_safe_for(safe_url, caps),
        format_overflow([safe_url], 0, caps),
        webex_display_safe(safe_url),
        to_whatsapp_text(safe_url),
    ):
        assert safe_url not in output
        assert "[REDACTED: encoded credential]" in output


def test_dashboard_history_and_live_readback_keep_authorized_pako(
    active_companion_policy: None,
) -> None:
    safe_url, _ = _pako_url("flowchart TD\n  A --> B")
    unsafe_url, unsafe_payload = _pako_url(f"flowchart TD\n  A[{_COMPANION_CREDENTIAL}] --> B")
    authorized = turn_driver_redact(
        "\n".join((safe_url, unsafe_url, _BASELINE_CREDENTIAL, _EXFIL_URL))
    )

    rows = _redact_history_rows([{"role": "assistant", "content": authorized}])
    prepared = _prepare_messages(rows, running=False, live_child="")
    streamed = json.loads(
        _build_stream_chunk({"role": "assistant", "content": authorized, "cls": ""})
    )

    for output in (rows[0]["content"], prepared[0]["content"], streamed["content"]):
        assert safe_url in output
        assert unsafe_url not in output
        assert unsafe_payload not in output
        assert _BASELINE_CREDENTIAL not in output
        assert _EXFIL_URL not in output


def test_dashboard_structured_legacy_content_cannot_gain_restoration(
    active_companion_policy: None,
) -> None:
    safe_url, _ = _pako_url("flowchart TD\n  A --> B")

    output = redact_display_content({"legacy": [safe_url]})

    assert safe_url not in output
    assert "[REDACTED: encoded credential]" in output
