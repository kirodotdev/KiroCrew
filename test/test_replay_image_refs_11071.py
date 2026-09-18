"""Replayed history carries no image reference into a prompt.

A history row's picture belonged to an earlier turn, and a text vehicle cannot
carry bytes, so the reference is the only thing that would arrive. Both readings
of an arriving reference are wrong, and both are pinned below:

* the file is still readable -- ``build_prompt_blocks`` inlines it, so a picture
  Kiro Crew's compaction already dropped rides along at full byte cost on every
  cold start, and the markdown is mangled into ``![alt]([image: name])``;
* the file is absent -- no image block is emitted and the PATH sits in the prose,
  next to the assistant's own description of what the picture showed. That
  dangling reference is what makes a model narrate a screenshot it cannot see.

Kiro Crew's replay is the path an auto-compaction reaches. A failing in-place
``/compact`` sends ``CompactionCoordinator._recycle_held``, which pops the
session while leaving ``_suppress_replay`` alone -- ``reset(clear_conversation=
True)`` arms that flag, a recycle deliberately does not, because a recycle
preserves the conversation -- and the compact callback posts its success notice
either way. The next turn is therefore a cold start that re-injects the
transcript, and a persisted row keeps its markdown image reference by design:
the attachment store rewrites the destination into the transcript's
``.attachments/`` directory so the reference survives.

The three consumers that render persisted rows into prompt text all draw from
``_replay_rows`` / ``_recall_rows``, which is why the strip happens there.

The bare-path pass is narrower than the inliner's own ``_PATH_RE`` in two ways,
and both are pinned: code spans and URL-embedded paths keep their text, because
rewriting either is corruption rather than scrubbing.
"""

from __future__ import annotations

import base64
import os
import pathlib
import subprocess
import sys

import pytest

import kiro_crew
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.context import _recall_rows, build_session_replay
from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER, strip_image_refs
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Absolute image paths spelled for the RUNNING host. The bare-path grammar is
# platform-gated -- backslash and ":" are legal in POSIX filenames, so accepting
# Windows shapes everywhere would make ordinary prose a candidate -- which means
# a hardcoded POSIX path matches nothing on Windows and a test built on one
# would assert the opposite of the contract there. Nothing reads these files;
# the scrubber never touches the filesystem.
_ABS_A = "C:\\tmp\\a.png" if os.name == "nt" else "/tmp/a.png"
_ABS_B = "C:\\tmp\\b.png" if os.name == "nt" else "/tmp/b.png"


class _Log:
    """Conversation-log stand-in; the row builders read one method each."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def read_messages_chained(self, session_key: str) -> list[dict]:
        return list(self._rows)

    def read_messages(self, session_key: str) -> list[dict]:
        return list(self._rows)


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


def _dest(p):
    """The spelling the dashboard composer serializes into a markdown destination.

    Forward slashes on both hosts: a destination cannot carry raw backslashes
    (CommonMark eats a backslash before punctuation) and Windows file APIs
    accept the forward-slash form verbatim, so this is the shape the product
    actually persists -- and the one that makes a single assertion true on
    either platform.
    """
    return p.as_posix()


class TestStripImageRefs:
    def test_markdown_reference_loses_its_path(self, tmp_path):
        p = _png(tmp_path)
        out = strip_image_refs(f"here is the failing screen ![shot]({_dest(p)})")

        assert out == f"here is the failing screen {STRIPPED_IMAGE_MARKER}"
        assert _dest(p) not in out

    def test_alt_text_does_not_survive(self, tmp_path):
        # A caption is indistinguishable from a description: left in, it is
        # prose asserting what a picture the model cannot see contained.
        p = _png(tmp_path)
        out = strip_image_refs(f"![the login page error]({_dest(p)})")

        assert "login page error" not in out
        assert out == STRIPPED_IMAGE_MARKER

    def test_bare_path_loses_its_path(self, tmp_path):
        # The shape a Slack or Telegram inbound message appends: a native path,
        # not a markdown destination.
        p = _png(tmp_path)
        out = strip_image_refs(f"look at this\n{p}")

        assert out == f"look at this\n{STRIPPED_IMAGE_MARKER}"

    def test_every_reference_in_one_row_is_replaced(self, tmp_path):
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.jpg")
        out = strip_image_refs(f"first ![a]({_dest(a)}) then ![b]({_dest(b)}) done")

        assert _dest(a) not in out
        assert _dest(b) not in out
        assert out.count(STRIPPED_IMAGE_MARKER) == 2
        # Right-to-left replacement keeps the earlier span valid, so the
        # surrounding prose is not shifted into the wrong place.
        assert out == f"first {STRIPPED_IMAGE_MARKER} then {STRIPPED_IMAGE_MARKER} done"

    def test_marker_is_not_the_attached_image_spelling(self):
        # build_prompt_blocks writes "[image: <name>]" to mean the OPPOSITE --
        # that the picture rides this very request. The two must not collide.
        assert not STRIPPED_IMAGE_MARKER.startswith("[image:")
        assert "not carried" in STRIPPED_IMAGE_MARKER

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "just words, no attachment",
            "a sentence mentioning png and jpeg as words",
        ],
    )
    def test_text_with_nothing_to_strip_is_returned_unchanged(self, text):
        assert strip_image_refs(text) is text

    def test_remote_reference_is_left_alone(self):
        # Not a local path: it is not inlined here either, and it stays usable
        # to a tool-capable agent.
        text = "see ![chart](https://example.com/chart.png)"
        assert strip_image_refs(text) == text

    def test_non_string_content_is_passed_through(self):
        assert strip_image_refs(None) is None


class TestBarePathPassIsNarrowerThanTheInliner:
    """A substitution has no "only after a file was read" condition, so it must
    not touch text where rewriting is corruption rather than scrubbing."""

    @pytest.mark.parametrize(
        "text",
        [
            # Inside a URL query. The grammar's own guard admits this ("=" is
            # not [\w:/]), and rewriting it breaks the URL.
            f"see https://host/render?src={_ABS_A} for the chart",
            f"https://host/a?x=1&src={_ABS_A}&y=2",
            # Inline code and a fenced block are documentation.
            f"run `open {_ABS_A}` to view it",
            f"how to:\n```\nopen {_ABS_A}\n```\nthat is all",
            # Protocol-relative: a path shape to both grammars ("/" opens the
            # POSIX one, "//" also spells a UNC share) but REMOTE to
            # iter_local_refs, so pass one declines it and pass two must too --
            # otherwise a working remote image URL is destroyed.
            "see ![chart](//cdn.example.com/x.png) here",
            "bare protocol-relative: //cdn.example.com/x.png",
        ],
    )
    def test_code_and_url_spans_keep_their_text(self, text):
        assert strip_image_refs(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            f"look at this\n{_ABS_A}",  # the Slack/Telegram append shape
            _ABS_A,  # the whole row
            f"look at ({_ABS_A}) there",  # opening delimiter
        ],
    )
    def test_a_standalone_path_is_still_stripped(self, text):
        out = strip_image_refs(text)
        assert _ABS_A not in out
        assert STRIPPED_IMAGE_MARKER in out

    def test_a_masked_span_does_not_shift_a_real_strip(self):
        # The mask is length-preserving, so a path AFTER a code span is still
        # replaced at the right offset.
        text = f"docs say `open {_ABS_A}`, and here it is:\n{_ABS_B}"
        out = strip_image_refs(text)

        assert out == f"docs say `open {_ABS_A}`, and here it is:\n{STRIPPED_IMAGE_MARKER}"


class TestReplayedHistoryCarriesNoImage:
    """Both readings of an arriving reference, at the boundary that produces them."""

    def _rows(self, dest) -> list[dict]:
        return [
            {"role": "user", "content": f"here is the failing screen ![shot]({dest})"},
            {"role": "assistant", "content": "I see a red panel with a stack trace."},
            {"role": "user", "content": "and now the second one"},
            {"role": "assistant", "content": "Understood."},
        ]

    def test_present_attachment_is_not_re_inlined(self, tmp_path):
        # Reading one: the compaction dropped this picture, and replaying its
        # path puts the bytes straight back while mangling the markup.
        p = _png(tmp_path)
        replay = build_session_replay(_Log(self._rows(_dest(p))), "k")
        blocks = build_prompt_blocks(replay, allow_image=True)

        assert [b["type"] for b in blocks] == ["text"]
        assert _dest(p) not in blocks[0]["text"]
        assert f"[image: {p.name}]" not in blocks[0]["text"]
        assert STRIPPED_IMAGE_MARKER in blocks[0]["text"]

    def test_missing_attachment_leaves_no_dangling_path(self, tmp_path):
        # Reading two: a swept temp upload leaves the path in the prose with no
        # picture behind it.
        p = _png(tmp_path)
        gone = _dest(p)
        p.unlink()
        replay = build_session_replay(_Log(self._rows(gone)), "k")
        blocks = build_prompt_blocks(replay, allow_image=True)

        assert [b["type"] for b in blocks] == ["text"]
        assert gone not in blocks[0]["text"]
        assert STRIPPED_IMAGE_MARKER in blocks[0]["text"]

    def test_replay_is_otherwise_intact(self, tmp_path):
        p = _png(tmp_path)
        replay = build_session_replay(_Log(self._rows(_dest(p))), "k")

        assert "Assistant: I see a red panel with a stack trace." in replay
        assert "User: and now the second one" in replay
        assert replay.startswith("User: here is the failing screen ")

    def test_recall_rows_strip_too(self, tmp_path):
        # The other row builder. It feeds the thread-history fallback AND the
        # transcript compress_thread_history hands to the LLM compressor, which
        # returns it verbatim under the cap -- so a path here reaches a model
        # on a warm turn, not only after a compaction.
        p = _png(tmp_path)
        rows = _recall_rows(_Log(self._rows(_dest(p))), "k", conv_max=10)

        assert rows
        joined = "\n".join(r["content"] for r in rows)
        assert _dest(p) not in joined
        assert STRIPPED_IMAGE_MARKER in joined

    def test_the_current_turn_still_gets_its_picture(self, tmp_path):
        # The strip must not reach the live path: an image the user just
        # attached has to keep becoming a real image block.
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"look at {p} please", allow_image=True)

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert base64.b64decode(blocks[1]["data"]) == _PNG
        assert blocks[0]["text"] == f"look at [image: {p.name}] please"


class TestImportSafety:
    """``kiro_crew.image_refs`` must import with nothing else loaded.

    ``acp.prompt_blocks`` imports it at module scope, so anything it reaches that
    leads back to the ACP package closes a cycle -- and the order that breaks is
    the one where ``image_refs`` is FIRST into the cluster, which importing
    ``prompt_blocks`` or ``context`` never exercises. Only a cold interpreter
    can measure it, hence the subprocess.
    """

    @pytest.mark.parametrize(
        "statement",
        [
            "import kiro_crew.image_refs",
            "from kiro_crew.image_refs import strip_image_refs",
        ],
    )
    def test_cold_import_of_the_leaf_succeeds(self, statement):
        src = str(pathlib.Path(kiro_crew.__file__).resolve().parent.parent)
        code = f"import sys; sys.path.insert(0, {src!r})\n{statement}\n"
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=120, **UTF8_TEXT
        )

        assert done.returncode == 0, done.stderr[-800:]

    def test_module_scope_reaches_no_acp_module(self):
        src = str(pathlib.Path(kiro_crew.__file__).resolve().parent.parent)
        code = (
            f"import sys; sys.path.insert(0, {src!r})\n"
            "import kiro_crew.image_refs\n"
            "bad = sorted(m for m in sys.modules if m.startswith('kiro_crew.acp'))\n"
            "print('\\n'.join(bad))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=120, **UTF8_TEXT
        )

        assert done.returncode == 0, done.stderr[-800:]
        assert done.stdout.strip() == "", f"module scope pulled in: {done.stdout}"
