"""Tests for auto-title prompt construction (chat_title._build_title_prompt)."""

from __future__ import annotations

import logging
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
