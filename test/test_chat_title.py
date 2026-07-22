"""Tests for auto-title prompt construction (chat_title._build_title_prompt)."""

from __future__ import annotations

from kiro_crew.dashboard.chat_title import (
    _TITLE_MAX_ATTACHMENT_FILES,
    _TITLE_MAX_ATTACHMENT_PATH_LENGTH,
    _TITLE_SOURCE_SCAN_LIMIT,
    _TITLE_TEXT_LIMIT,
    _build_title_prompt,
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
    assert "quarterly report final.txt" not in prompt


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
    assert "results.csv" not in prompt


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
