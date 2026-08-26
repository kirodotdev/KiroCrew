"""Tests for auto-title prompt construction (chat_title._build_title_prompt)."""

from __future__ import annotations

import logging
import threading
import unicodedata
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_MAX_ATTACHMENT_FILES,
    _TITLE_MAX_ATTACHMENT_PATH_LENGTH,
    _TITLE_SOURCE_SCAN_LIMIT,
    _TITLE_TEXT_LIMIT,
    _build_title_prompt,
    _looks_like_prose,
    _message_attachment_paths,
    _title_text,
)


def test_prompt_isolates_and_delimits_transcript():
    """The title prompt must instruct the model to name ONLY the delimited
    transcript and ignore residual session history — the shared _bg session
    retains a sibling session's context between recycles, which previously
    bled into titles."""
    msgs = [
        {"role": "user", "content": "Update the doc refs to bullseye Set a goal"},
        {"role": "assistant", "content": "Done — the icon is the lucide Goal component."},
    ]
    prompt = _build_title_prompt(msgs)
    assert prompt is not None

    # Isolation instruction present.
    assert "ignore any earlier conversation" in prompt

    # Transcript is fenced and lands strictly between the delimiters.
    assert "===== CONVERSATION TO NAME =====" in prompt
    assert "===== END CONVERSATION =====" in prompt
    body = prompt.split("===== CONVERSATION TO NAME =====", 1)[1].split(
        "===== END CONVERSATION =====", 1
    )[0]
    assert "Update the doc refs" in body
    assert "lucide Goal component" in body


def test_prompt_none_when_no_usable_messages():
    """Contract preserved: empty or non-user/assistant messages yield None."""
    assert _build_title_prompt([]) is None
    assert _build_title_prompt([{"role": "system", "content": "x"}]) is None


def test_prompt_strips_image_attachment_before_truncation():
    """A long upload path must not crowd the user's request out of the prompt."""
    attachment = f"![image](/Users/example/.kirocrew/uploads/{'a' * 240}.jpg)"
    prompt = _build_title_prompt(
        [{"role": "user", "content": f"{attachment}\n\ncreating titles is failing"}]
    )

    assert prompt is not None
    assert "creating titles is failing" in prompt
    assert "![image]" not in prompt
    assert "/uploads/" not in prompt


def test_prompt_strips_image_attachment_with_parentheses():
    prompt = _build_title_prompt(
        [
            {
                "role": "user",
                "content": "![image](/tmp/screenshot(1).jpg)\n\nfix title generation",
            }
        ]
    )

    assert prompt is not None
    assert "fix title generation" in prompt
    assert "screenshot" not in prompt
    assert ".jpg)" not in prompt


def test_prompt_strips_non_image_attachment_before_truncation():
    attachment = f"[attached_file 1] /Users/example/uploads/{'a' * 240}.txt"
    prompt = _build_title_prompt([{"role": "user", "content": f"{attachment}\nreview this config"}])

    assert prompt is not None
    assert "review this config" in prompt
    assert "attached_file" not in prompt
    assert "/uploads/" not in prompt


def test_prompt_substitutes_attachment_name_from_metadata():
    """The NAME survives; the directory path does not.

    Previously the marker and path were replaced by a bare space, so an
    attachment-only or attachment-dominated message lost its topic entirely and
    the titling model answered SKIP. The basename is the topic, so it is kept --
    the full path is still stripped.
    """
    path = "/Users/example/uploads/quarterly report final.txt"
    prompt = _build_title_prompt(
        [
            {
                "role": "user",
                "content": f"[attached_file 1] {path}\nsummarize the findings",
                "meta": {"files": [path]},
            }
        ]
    )

    assert prompt is not None
    assert "summarize the findings" in prompt
    assert "quarterly report final.txt" in prompt, "the attachment name is the topic"
    assert "/Users/example/uploads" not in prompt, "the directory path must not leak"
    assert "attached_file" not in prompt


def test_multi_attachment_message_keeps_a_titleable_sentence():
    """`compare A and B` must not collapse to `compare and`."""
    prompt = _build_title_prompt(
        [
            {
                "role": "user",
                "content": "compare [attached_file 1] /a/x.txt and [attached_file 2] /b/y.txt",
                "meta": {"files": ["/a/x.txt", "/b/y.txt"]},
            }
        ]
    )

    assert prompt is not None
    assert "x.txt" in prompt and "y.txt" in prompt
    assert "compare" in prompt


def test_colliding_attachment_names_are_disambiguated():
    """Three `report.pdf` would read as `report.pdf and report.pdf`."""
    files = ["/q3/report.pdf", "/q4/report.pdf"]
    prompt = _build_title_prompt(
        [
            {
                "role": "user",
                "content": "diff [attached_file 1] /q3/report.pdf vs [attached_file 2] /q4/report.pdf",
                "meta": {"files": files},
            }
        ]
    )

    assert prompt is not None
    assert "q3/report.pdf" in prompt
    assert "q4/report.pdf" in prompt


def test_attachment_label_budget_is_bounded():
    """Many deep attachments must not crowd out the user's own words."""
    files = [f"/very/deep/directory/tree/file-number-{i:02d}.txt" for i in range(20)]
    markers = " ".join(f"[attached_file {i + 1}] {p}" for i, p in enumerate(files))
    prompt = _build_title_prompt(
        [
            {
                "role": "user",
                "content": f"{markers} please summarize everything",
                "meta": {"files": files},
            }
        ]
    )

    assert prompt is not None
    assert "please summarize everything" in prompt, "user text must survive the labels"
    # Budget is 80 chars total; 20 labels of ~22 chars each would be ~440.
    assert prompt.count("file-number-") <= 4


def test_prompt_strips_non_image_attachment_path_with_spaces_from_metadata():
    path = "/Users/example/uploads/quarterly report final.txt"
    prompt = _build_title_prompt(
        [
            {
                "role": "user",
                "content": f"[attached_file 1] {path}\nsummarize the findings",
                "meta": {"files": [path]},
            }
        ]
    )

    assert prompt is not None
    assert "summarize the findings" in prompt
    assert "/Users/example/uploads" not in prompt


def test_title_text_bounds_source_scanning():
    content = "describe this " + ("x" * _TITLE_SOURCE_SCAN_LIMIT) + " SECRET_TAIL"

    sanitized = _title_text(content)

    assert "describe this" in sanitized
    assert "SECRET_TAIL" not in sanitized
    assert len(sanitized) <= _TITLE_TEXT_LIMIT


def test_prompt_strips_multiple_near_limit_attachments_before_text_cap():
    paths = [
        f"/tmp/{index}-" + ("x" * (_TITLE_MAX_ATTACHMENT_PATH_LENGTH - 12)) + ".txt"
        for index in range(1, 6)
    ]
    content = "\n".join(
        [
            *(f"[attached_file {index}] {path}" for index, path in enumerate(paths, 1)),
            "summarize the quarterly findings",
        ]
    )

    prompt = _build_title_prompt([{"role": "user", "content": content, "meta": {"files": paths}}])

    assert prompt is not None
    assert "summarize the quarterly findings" in prompt
    assert "attached_file" not in prompt
    assert "/tmp/" not in prompt


def test_attachment_metadata_is_bounded_without_shifting_indices():
    files: list[object] = [
        "/tmp/first.txt",
        42,
        "x" * (_TITLE_MAX_ATTACHMENT_PATH_LENGTH + 1),
        *(f"/tmp/{index}.txt" for index in range(_TITLE_MAX_ATTACHMENT_FILES)),
    ]

    paths = _message_attachment_paths({"meta": {"files": files}})

    assert len(paths) == _TITLE_MAX_ATTACHMENT_FILES
    assert paths[:4] == ("/tmp/first.txt", "", "", "/tmp/0.txt")


def test_prompt_strips_mixed_attachments_and_keeps_caption():
    prompt = _build_title_prompt(
        [
            {
                "role": "user",
                "content": (
                    "![image](/tmp/screenshot.png)\n\n"
                    "compare these outputs\n"
                    "[attached_file 1] /tmp/results.csv"
                ),
            }
        ]
    )

    assert prompt is not None
    assert "compare these outputs" in prompt
    assert "screenshot.png" not in prompt
    # No `meta` on this message, so the label comes from the whitespace-scan
    # fallback. The name is kept (it is the topic); images stay dropped because
    # they carry no textual topic.
    assert "results.csv" in prompt
    assert "/tmp/results.csv" not in prompt


def test_prompt_preserves_escaped_and_code_quoted_markdown_images():
    content = r"\![image](/tmp/literal.png) and `![image](/tmp/example.png)`"
    prompt = _build_title_prompt([{"role": "user", "content": content}])

    assert prompt is not None
    assert r"\![image](/tmp/literal.png)" in prompt
    assert "`![image](/tmp/example.png)`" in prompt


def test_prompt_none_for_attachment_only_message():
    assert (
        _build_title_prompt([{"role": "user", "content": "![image](/tmp/screenshot.jpg)"}]) is None
    )


# ── Prose/refusal rejection (pasted URL made the model narrate its denial) ──


def test_prompt_forbids_fetching_and_forbids_explaining():
    """The naming agent must be told the transcript is data, links are not to be
    opened, and a refusal sentence is never an acceptable reply."""
    prompt = _build_title_prompt(
        [{"role": "user", "content": "this is the launch blog https://example.com/Intro-Kiro-Crew"}]
    )
    assert prompt is not None
    lowered = prompt.lower()
    assert "do not use any tool" in lowered
    assert "fetch" in lowered
    assert "never explain" in lowered


def test_refusal_reply_is_rejected_as_prose():
    """The exact observed failure: a pasted Quip URL produced a refusal sentence
    that was persisted as the session name."""
    assert _looks_like_prose(
        "I cannot access external URLs like Quip documents. Based solely on the message content"
    )


@pytest.mark.parametrize(
    "reply",
    [
        "I can't fetch that link",
        "I'm unable to open the document",
        "Sorry, I don't have access to that page",
        "Unfortunately the URL is not reachable",
        "As an AI I cannot browse",
        "Unable to retrieve the blog post",
        "Here's a title for your conversation",
        "It looks like you shared a link",
        "Kiro Crew launch blog. Review requested",
        "A very long reply that keeps going and going well past any real session title length",
    ],
)
def test_prose_replies_rejected(reply):
    assert _looks_like_prose(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Kiro Crew launch blog",
        "Node.js upgrade plan",
        "Ship v1.2 to prod",
        "Fix title generation bug",
        "Quip doc review",
        "Ideas for the roadmap",
        "SKIP",
    ],
)
def test_real_titles_accepted(reply):
    assert not _looks_like_prose(reply)


def test_prompt_windows_the_opening_head():
    """Initial titling reads the OPENING turns — the head slice is the other
    half of the invariant _TITLE_PROMPT_WINDOW asserts (the refresh prompt and
    the manual regenerate endpoint read the tail)."""
    messages = [{"role": "user", "content": f"topic-{i} discussion"} for i in range(30)]
    prompt = _build_title_prompt(messages)
    assert prompt is not None
    assert "topic-0 " in prompt
    assert "topic-9 " in prompt
    assert "topic-29" not in prompt, "recent tail must be windowed out"


def test_empty_reply_is_not_treated_as_prose():
    """Empty/whitespace replies are already handled by the SKIP branch; the
    prose guard must not claim them."""
    assert not _looks_like_prose("")
    assert not _looks_like_prose("   ")


@pytest.mark.asyncio
async def test_generate_title_discards_prose_reply(monkeypatch):
    """End-to-end on the generation path: a refusal reply must surface as "" so
    the caller falls back instead of persisting the sentence as a title."""

    async def _fake_oneliner(*_a, **_kw):
        return "I cannot access external URLs like Quip documents. Based solely on the message"

    monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
    title = await chat_title._generate_title_via_kiro(
        SimpleNamespace(sessions=SimpleNamespace()),
        [{"role": "user", "content": "this is the launch blog https://example.com/Intro"}],
    )
    assert title == ""


@pytest.mark.asyncio
async def test_generate_title_keeps_real_reply(monkeypatch):
    """Revert guard: the same path must still return a well-formed title."""

    async def _fake_oneliner(*_a, **_kw):
        return "Kiro Crew launch blog"

    monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
    title = await chat_title._generate_title_via_kiro(
        SimpleNamespace(sessions=SimpleNamespace()),
        [{"role": "user", "content": "this is the launch blog https://example.com/Intro"}],
    )
    assert title == "Kiro Crew launch blog"


@pytest.mark.asyncio
async def test_discarded_prose_is_redacted_before_it_is_logged(monkeypatch, caplog):
    """A refusal can quote the user's message back, credentials included.

    The discard path logs what it threw away, so both redactors must run BEFORE
    that log line -- otherwise a pasted secret reaches the gateway log through
    the model's own narration. Stubbing the redactors to a sentinel proves the
    ordering rather than the redactors' own patterns.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"

    async def _fake_oneliner(*_a, **_kw):
        return f"I cannot access that URL. The message contained {secret} so I stopped"

    monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
    monkeypatch.setattr(
        chat_title, "redact_exfiltration_urls", lambda s: (s.replace(secret, "<URLRED>"), False)
    )
    monkeypatch.setattr(
        chat_title, "redact_credentials", lambda s: (s.replace(secret, "<CREDRED>"), False)
    )

    with caplog.at_level(logging.INFO, logger=chat_title.logger.name):
        title = await chat_title._generate_title_via_kiro(
            SimpleNamespace(sessions=SimpleNamespace()),
            [{"role": "user", "content": f"look at https://example.com/?k={secret}"}],
        )

    assert title == ""
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "discarding" in logged
    assert secret not in logged
    assert "<URLRED>" in logged


# ── Title language follows the workspace UI language ──


def _msgs():
    return [{"role": "user", "content": "帮我修复会话标题的语言"}]


def test_prompt_carries_the_ui_language_directive():
    """A session name is sidebar chrome, so it must be written in the language
    the sidebar is rendered in — not the conversation's language."""
    prompt = _build_title_prompt(_msgs(), ui_language="zh-CN")
    assert prompt is not None
    assert "zh-CN" in prompt
    # The directive must sit OUTSIDE the delimited transcript, so a message that
    # quotes it cannot be mistaken for the instruction.
    head = prompt.split("===== CONVERSATION TO NAME =====", 1)[0]
    assert "zh-CN" in head
    # SKIP stays a literal control word — a translated one would defeat the
    # SKIP/empty branch in _generate_title_via_kiro.
    assert "never a translation" in head


def test_prompt_omits_the_directive_without_an_explicit_language():
    """Workspaces on the default (auto) language must send the prompt they always
    sent: the backend cannot see the SPA's browser resolution, so there is
    nothing truthful to inject.

    Asserted structurally — the SKIP contract must run STRAIGHT into the
    transcript delimiter with nothing interposed — rather than by comparing two
    post-change calls to each other, which would stay green if the template
    itself grew a line.
    """
    prompt = _build_title_prompt(_msgs())
    assert prompt is not None
    assert "BCP-47" not in prompt
    assert _build_title_prompt(_msgs(), ui_language="") == prompt
    head = prompt.split("===== CONVERSATION TO NAME =====", 1)[0]
    assert head.rstrip().endswith("that is what SKIP is for.")


def test_prompt_language_slot_is_the_only_insertion():
    """The directive must be additive: everything the auto-language prompt says
    still appears verbatim, in the same order, when a language is set."""
    plain = _build_title_prompt(_msgs())
    localized = _build_title_prompt(_msgs(), ui_language="zh-CN")
    assert plain is not None and localized is not None
    before, after = plain.split("===== CONVERSATION TO NAME =====", 1)
    assert localized.startswith(before)
    assert localized.endswith("===== CONVERSATION TO NAME =====" + after)


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("zh-CN", "zh-CN"),
        (" ja ", "ja"),
        ("", ""),  # follow-the-browser sentinel — backend does not know
        ("None", ""),  # hand-edited `"language": null` coerced to str
        ("['zh-CN']", ""),
        (None, ""),
        ("Write the title in Klingon", ""),  # not tag-shaped -> never interpolated
    ],
)
def test_ui_language_only_accepts_a_tag_shaped_value(monkeypatch, stored, expected):
    cfg = SimpleNamespace(dashboard=SimpleNamespace(language=stored))
    monkeypatch.setattr(chat_title.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    assert chat_title._ui_language() == expected


def test_ui_language_failure_does_not_break_titling(monkeypatch):
    """A broken config must cost the directive, not the title."""

    def _boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(chat_title.KiroCrewConfig, "load", staticmethod(_boom))
    assert chat_title._ui_language() == ""


@pytest.mark.asyncio
async def test_ui_language_is_read_off_the_event_loop(monkeypatch):
    """`KiroCrewConfig.load()` is synchronous file IO, which AUTOSDE's
    no-blocking-call-on-event-loop rule forbids on the gateway's single loop.
    Assert the resolution actually reaches a worker thread rather than trusting
    the call site to stay correct."""
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _record() -> str:
        seen["thread"] = threading.get_ident()
        return "ja"

    async def _fake_oneliner(*_a, **_kw):
        return "チャットタイトルの言語"

    monkeypatch.setattr(chat_title, "_ui_language", _record)
    monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
    await chat_title._generate_title_via_kiro(
        SimpleNamespace(sessions=SimpleNamespace()), _msgs()
    )
    assert seen["thread"] != loop_thread


@pytest.mark.asyncio
async def test_generate_title_applies_the_configured_language(monkeypatch):
    """End-to-end on the generation path: the tag reaches the prompt."""
    seen: dict[str, str] = {}

    async def _fake_oneliner(_sessions, prompt, **_kw):
        seen["prompt"] = prompt
        return "修复会话标题语言"

    monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
    monkeypatch.setattr(chat_title, "_ui_language", lambda: "zh-CN")
    title = await chat_title._generate_title_via_kiro(
        SimpleNamespace(sessions=SimpleNamespace()), _msgs()
    )
    assert title == "修复会话标题语言"
    assert "zh-CN" in seen["prompt"]


@pytest.mark.parametrize(
    "reply",
    [
        # A refusal in an unspaced script is ONE word by str.split, so only the
        # character ceiling / wide terminator can catch it.
        "抱歉，我无法访问这个外部链接，因此无法根据它来命名这个会话",
        "我无法打开该文档。请提供更多上下文信息",
        "申し訳ありませんが、外部のURLにはアクセスできません。会話の名前を付けられません",
        "这是一个会话标题。另外还有一句话",
    ],
)
def test_unspaced_script_prose_rejected(reply):
    assert _looks_like_prose(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "修复会话标题语言",
        "升级 Node 到 24",
        "修复 PrivacyPanel 的动态键",
        "チャットタイトルの言語設定",
        "แก้ไขภาษาของชื่อแชท",
    ],
)
def test_unspaced_script_titles_accepted(reply):
    assert not _looks_like_prose(reply)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"Kiro Crew launch blog"', "Kiro Crew launch blog"),
        ("「修复会话标题语言」", "修复会话标题语言"),
        ("“修复会话标题语言”", "修复会话标题语言"),
        ("修复会话标题语言。", "修复会话标题语言"),
        ("『チャットタイトル』", "チャットタイトル"),
    ],
)
def test_clean_title_strips_full_width_wrappers(raw, expected):
    assert chat_title._clean_title(raw) == expected


def test_reveal_prefixes_unchanged_for_spaced_titles():
    """Revert guard on the animation: latin titles still step one word at a
    time, and the caller still owns the final push."""
    assert chat_title._title_reveal_prefixes("Kiro Crew launch blog") == [
        "Kiro",
        "Kiro Crew",
        "Kiro Crew launch",
    ]
    assert chat_title._title_reveal_prefixes("Standalone") == []


def test_reveal_prefixes_step_characters_for_unspaced_titles():
    """A zh/ja title is a single token, which skipped the reveal entirely."""
    prefixes = chat_title._title_reveal_prefixes("修复会话标题语言")
    assert prefixes == ["修复", "修复会话", "修复会话标题"]
    # Never emits the full title — the caller pushes that one.
    assert all(p != "修复会话标题语言" for p in prefixes)


def test_reveal_prefixes_never_split_a_combining_mark():
    """A Thai cut must not land between a consonant and its tone mark, or the
    mark visibly pops onto an already-drawn glyph one frame later."""
    title = "แก้ไขภาษาของชื่อแชท"
    prefixes = chat_title._title_reveal_prefixes(title)
    assert prefixes, "expected an animated reveal for a Thai title"
    for p in prefixes:
        assert title.startswith(p)
        assert p != title
        # The character that comes next must not be a mark belonging to the
        # last character we just revealed.
        assert not unicodedata.combining(title[len(p)])
