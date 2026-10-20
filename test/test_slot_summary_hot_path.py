"""The slot summary's per-tab work is memoized without changing a byte of it.

``serialize_slots`` runs on the event loop for every ``GET /api/chat/slots`` and
every slots broadcast, and projects each open tab's last message: a markdown
strip, the display redaction battery and an OPTIONS parse over the whole text.
These tests pin that the faster path answers exactly as the direct computation
does, that its memo retains only redacted output under a keyed digest, and that
no display cache is read or filled where the battery's answer depends on a scope
the cache key does not carry.
"""

from __future__ import annotations

import base64
import json
import unicodedata

import pytest

from kiro_crew import preview_text
from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard import chat_utils_display as cud
from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard.state_parts import slot_transcript as st
from kiro_crew.security import exfil, redact_credentials, redact_exfiltration_urls
from kiro_crew.security.redaction_switch import code_text, owner_view

_AWS = "AKIAIOSFODNN7EXAMPLE"
_HOST = "tenant.example.com"
_EXFIL_URL = f"https://{_HOST}/view?d={base64.b64encode(bytes(range(200))).decode()}"
#: Redacted as a bare secret outside ``code_text``, spared as an ordered alphabet inside.
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_ZWSP = "\u200b"
_LIMIT = st._LAST_MESSAGE_PREVIEW_CHARS


def _battery(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _cut(text: str) -> str:
    return text[:_LIMIT] + "…" if len(text) > _LIMIT else text


def _direct_preview(text: str) -> str:
    return _cut(_battery(preview_text.strip_markdown_preview(text)))


def _walk_drop_format_chars(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _scan_parse_options(text: str) -> list[str]:
    matches = list(st._OPTIONS_RE.finditer(text))
    if not matches:
        return []
    parts = [p.strip() for p in matches[-1].group("labels").split("|")]
    return [p for p in parts if p]


@pytest.fixture(autouse=True)
def _cold_caches():
    cud._clear_display_redaction_cache()
    yield
    cud._clear_display_redaction_cache()


def test_the_cf_drop_matches_a_per_character_walk_over_every_code_point():
    # Interleaved with ASCII so every chunk takes the non-ASCII path; lone
    # surrogates included, since a transcript string can carry them.
    for start in range(0, 0x110000, 0x10000):
        text = "a".join(chr(cp) for cp in range(start, start + 0x10000))
        assert preview_text.drop_format_chars(text) == _walk_drop_format_chars(text)


@pytest.mark.parametrize(
    "text",
    [
        "no marker at all",
        "pick one\n[OPTIONS: yes | no | later]",
        "first\n[OPTIONS: a | b]\nsecond\n[OPTIONS: c | d]",
        "lower case [options: a | b] is not a marker",
        "inline [OPTIONS: a | b] mid-line text",
        "**[OPTIONS: wrapped | bold]**",
        "[OPTIONS:]",
    ],
)
def test_the_options_guard_answers_like_the_regex_scan(text):
    assert st._parse_options(text) == _scan_parse_options(text)
    assert st._strip_options(text) == st._OPTIONS_RE.sub("", text).strip()


_PREVIEW_SAMPLES = [
    "",
    "plain ascii line",
    "**bold** `code` [link](https://example.com/x) and ~~gone~~",
    "```diff\n- a\n+ b\n```\nafter the fence",
    "# heading\n> quote\n- bullet\n1. item\n---\ntail",
    f"zero{_ZWSP}width joined café 漢字 \U0001f600",
    _ZWSP,
    "ask [OPTIONS: yes | no]\nrest",
    f"key **{_AWS[:8]}**{_AWS[8:]} split by markdown",
    f"{_AWS} and {_EXFIL_URL}",
    "word " * 40,
]


@pytest.mark.parametrize("text", _PREVIEW_SAMPLES)
def test_the_preview_memo_returns_the_direct_composition_cold_and_warm(text):
    expected = _direct_preview(text)
    assert st._last_message_preview(text) == expected  # cold: computed and filed
    assert st._last_message_preview(text) == expected  # warm: served from the memo


def test_the_preview_memo_retains_no_raw_text():
    secret = f"the key is {_AWS} and the link {_EXFIL_URL}"
    st._last_message_preview(secret)
    assert cud._display_preview_cache_info()[0] == 1
    for (digest, length, limit), (preview, _cost) in cud._display_preview_cache.items():
        # The key is a 32-byte MAC plus two lengths; the value is the redacted preview.
        assert isinstance(digest, bytes) and len(digest) == 32
        assert (length, limit) == (len(secret.encode()), _LIMIT)
        assert preview == _direct_preview(secret)
        assert _AWS not in preview


def test_the_preview_memo_is_bounded_by_entries_and_bytes(monkeypatch):
    monkeypatch.setattr(cud, "_DISPLAY_PREVIEW_CACHE_MAX_ENTRIES", 2)
    for word in ("alpha", "bravo", "charlie"):
        st._last_message_preview(word)
    entries, charged = cud._display_preview_cache_info()
    assert entries == 2
    assert charged == sum(
        len(k[0]) + len(v[0].encode()) for k, v in cud._display_preview_cache.items()
    )
    assert [v[0] for v in cud._display_preview_cache.values()] == ["bravo", "charlie"]
    monkeypatch.setattr(cud, "_DISPLAY_PREVIEW_CACHE_MAX_BYTES", 32 + len("delta"))
    st._last_message_preview("delta")
    assert [v[0] for v in cud._display_preview_cache.values()] == ["delta"]


def test_clearing_the_display_caches_clears_the_preview_memo():
    st._last_message_preview("some text")
    assert cud._display_preview_cache_info()[0] == 1
    cud._clear_display_redaction_cache()
    assert cud._display_preview_cache_info() == (0, 0)


@pytest.mark.parametrize(
    "text",
    ["nothing to redact", f"key {_AWS} here", f"link {_EXFIL_URL} end", f"{_AWS} and {_EXFIL_URL}"],
)
def test_the_slot_redaction_is_the_battery_cold_and_warm(text):
    expected = _battery(text)
    assert st._redact(text) == expected
    assert st._redact(text) == expected


def test_an_exempt_host_change_is_not_answered_from_either_memo(monkeypatch):
    text = f"see {_EXFIL_URL} end"
    exempt = {"hosts": frozenset()}
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: exempt["hosts"])
    monkeypatch.setattr(cu, "_exempt_exact_hosts", lambda: exempt["hosts"])
    blocked, blocked_preview = st._redact(text), st._last_message_preview(text)
    assert blocked == _battery(text) and "suspicious URL" in blocked
    assert blocked_preview == _direct_preview(text)
    exempt["hosts"] = frozenset({_HOST})
    assert st._redact(text) == _battery(text) != blocked
    assert st._last_message_preview(text) == _direct_preview(text) != blocked_preview
    exempt["hosts"] = frozenset()
    assert st._redact(text) == blocked
    assert st._last_message_preview(text) == blocked_preview
    with exfil.scoped_exempt_hosts(frozenset({_HOST})):
        assert st._redact(text) == _battery(text) != blocked
        assert st._last_message_preview(text) == _direct_preview(text) != blocked_preview
    assert st._last_message_preview(text) == blocked_preview


_SCOPED = [(owner_view, f"key {_AWS} here"), (code_text, f"run {_ALPHABET} ok")]


@pytest.mark.parametrize("scope,text", _SCOPED, ids=["owner_view", "code_text"])
def test_redact_for_display_neither_reads_nor_fills_its_cache_inside_the_scope(scope, text):
    outside = _battery(text)
    with scope():
        inside = _battery(text)
    assert inside != outside  # the scope changes the battery's answer for this text
    # Not filled: the scoped answer, computed first, never reaches a later ordinary read.
    with scope():
        assert cud._redact_for_display(text) == inside
    assert cud._display_redaction_cache_info() == (0, 0)
    assert cud._redact_for_display(text) == outside
    # Not read: with the ordinary answer cached, the scope still gets its own.
    with scope():
        assert cud._redact_for_display(text) == inside


@pytest.mark.parametrize("scope,text", _SCOPED, ids=["owner_view", "code_text"])
def test_the_preview_memo_neither_reads_nor_fills_inside_the_scope(scope, text):
    outside = _direct_preview(text)
    with scope():
        inside = _direct_preview(text)
        assert st._last_message_preview(text) == inside
    assert inside != outside
    assert cud._display_preview_cache_info() == (0, 0)
    assert st._last_message_preview(text) == outside
    assert cud._display_preview_cache_info()[0] == 1
    with scope():
        assert st._last_message_preview(text) == inside


def _reference(monkeypatch):
    """Route the projection through the direct, uncached computations.

    Names the ``dashboard.state`` facade mirrors are patched through it, so the
    patch and its undo reach every owner that binds them.
    """
    monkeypatch.setattr(preview_text, "drop_format_chars", _walk_drop_format_chars)
    monkeypatch.setattr(dashboard_state, "_redact", _battery)
    monkeypatch.setattr(dashboard_state, "_parse_options", _scan_parse_options)
    monkeypatch.setattr(st, "_last_message_preview", _direct_preview)
    monkeypatch.setattr(st, "_strip_options", lambda t: st._OPTIONS_RE.sub("", t).strip())
    assert st._redact is _battery and st._parse_options is _scan_parse_options


def test_the_slot_payload_is_byte_identical_to_the_direct_computation(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    state = _make_state(tmp_path)
    contents = [
        f"**done** with `step` — café {_ZWSP}",
        f"here is the key {_AWS} and {_EXFIL_URL}",
        "Choose one:\n[OPTIONS: ship it | wait | revert]",
        "```python\nprint(1)\n```\n" + "漢字 " * 400,
        _ZWSP,
    ]
    for i, content in enumerate(contents):
        slot = state.get_or_create_slot(f"chat-{i}")
        slot.messages.append(
            {"role": "user", "content": f"ask {i}", "ts": f"2026-10-18T10:0{i}:00Z"}
        )
        slot.messages.append(
            {"role": "assistant", "content": content, "ts": f"2026-10-18T10:0{i}:30Z"}
        )
    cold = json.dumps(state.serialize_slots())
    warm = json.dumps(state.serialize_slots())
    _reference(monkeypatch)
    direct = json.dumps(state.serialize_slots())
    assert cold == warm == direct
    assert _AWS not in direct
