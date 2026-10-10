"""Replayed history carries no image reference into a prompt.

A history row's picture belonged to an earlier turn, and a text vehicle cannot
carry bytes, so the reference is the only thing that would arrive. Both readings
of an arriving reference are wrong, and both are pinned below:

* the file is still readable -- a builder that inlined any readable path it
  found in the text would bring back a picture Kiro Crew's compaction already
  dropped, at full byte cost on every cold start, with the markdown mangled into
  ``![alt]([image: name])``. The builder emits image blocks only from the
  channel's structured attachment list, so a replayed path is text, never a
  block on its own; the strip stays load-bearing for the other reading;
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

The bare-path pass is narrower than the inliner's own ``_PATH_RE``, and each
narrowing is pinned: code spans and URL-embedded paths keep their text, because
rewriting either is corruption rather than scrubbing; and a span holding a space
or tab is replaced only when its last token is a path on its own or its shape
vouches for the whole span, so a sentence that merely mentions a directory and,
later, an image name keeps every word.
"""

from __future__ import annotations

import base64
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import pytest

import kiro_crew
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.context import _recall_rows, build_session_replay
from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER, strip_image_refs
from kiro_crew.prompt_attachments import image_attachments
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
# A directory, and an attachment whose name carries a space -- the shape a
# macOS screenshot lands with -- spelled for the running host for the same reason.
_DIR = "C:\\var\\log\\app" if os.name == "nt" else "/var/log/app"
_SPACED = (
    "C:\\Users\\me\\Screen Shot 2024.png" if os.name == "nt" else "/Users/me/Screen Shot 2024.png"
)
# What a channel appends when its temp directory holds a space: the writer's
# own ``mkstemp`` name, under a profile named "John Smith".
_TEMP = (
    "C:\\Users\\John Smith\\AppData\\Local\\Temp\\tmpab12cd_4.png"
    if os.name == "nt"
    else "/Users/John Smith/tmp/tmpab12cd_4.png"
)


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

    @pytest.mark.parametrize(
        "template,strip",
        [
            ("restore {p}.backup", False),
            ("restore {p}~", False),
            ("see {p}/other", False),
            ("see {p}.", True),
            ("see {p}.Then we moved on", True),
        ],
    )
    def test_bare_path_requires_a_complete_image_name(self, tmp_path, template, strip):
        p = _dest(_png(tmp_path))
        text = template.format(p=p)
        expected = template.format(p=STRIPPED_IMAGE_MARKER) if strip else text
        assert strip_image_refs(text) == expected

    def test_a_non_image_file_inside_a_directory_named_like_a_picture_keeps_its_text(
        self, tmp_path
    ):
        p = _dest(_png(tmp_path))
        text = f"open {p}版本/final.txt"

        assert strip_image_refs(text) == text

    @pytest.mark.parametrize("glued", ["📁", "Ａ"])
    def test_a_symbol_in_a_directory_named_like_a_picture_keeps_its_text(self, tmp_path, glued):
        p = _dest(_png(tmp_path))
        text = f"open {p}{glued}/final.txt"

        assert strip_image_refs(text) == text

    def test_two_paths_glued_by_cjk_text_are_one_reference(self, tmp_path):
        # Nothing in the text tells a conjunction from a directory component, so
        # the glued spelling is one token; a space or fullwidth comma keeps two.
        a = _dest(_png(tmp_path, "a.png"))
        b = _dest(_png(tmp_path, "b.png"))

        assert strip_image_refs(f"看 {a}和{b}") == f"看 {STRIPPED_IMAGE_MARKER}"

    def test_two_windows_paths_glued_by_cjk_text_are_one_reference(self, monkeypatch):
        # Same contract under the Windows grammar: the second path's drive
        # colon must not split the token, or the first picture is scrubbed
        # alone and the second path survives the replay.
        from kiro_crew import image_refs

        monkeypatch.setattr(image_refs, "_PATH_RE", image_refs._WINDOWS_PATH_RE)

        text = "看 C:/Users/me/a.png和C:/Users/me/b.png"

        assert strip_image_refs(text) == f"看 {STRIPPED_IMAGE_MARKER}"

    @pytest.mark.parametrize("glue", ["\u2014", "\u2013", "\u2192", "\u2022", "\u30fb"])
    def test_two_paths_glued_by_punctuation_are_two_references(self, tmp_path, glue):
        # Punctuation separates the two; only the first stands alone (after
        # whitespace), so the bare-path pass strips that one and leaves the
        # second as written, exactly as it did before the glue rules.
        a = _dest(_png(tmp_path, "a.png"))
        b = _dest(_png(tmp_path, "b.png"))

        assert strip_image_refs(f"look {a}{glue}{b}") == f"look {STRIPPED_IMAGE_MARKER}{glue}{b}"

    def test_two_paths_glued_by_an_emoji_keep_their_text(self, tmp_path):
        # No path starts after a symbol and the first is vetoed by the later
        # suffix, so the grammar sees no picture here -- nothing is scrubbed,
        # and the builder inlines nothing for the same text.
        a = _dest(_png(tmp_path, "a.png"))
        b = _dest(_png(tmp_path, "b.png"))
        text = f"look {a}\U0001f4c1{b}"

        assert strip_image_refs(text) == text

    def test_two_paths_glued_by_a_backslash_follow_the_host_grammar(self, tmp_path):
        # A backslash is a symbol to the POSIX grammar (nothing scrubbed) and a
        # path character to the Windows one (the pair is one token, one marker).
        a = _dest(_png(tmp_path, "a.png"))
        b = _dest(_png(tmp_path, "b.png"))
        text = f"look {a}\\{b}"

        expected = f"look {STRIPPED_IMAGE_MARKER}" if os.name == "nt" else text
        assert strip_image_refs(text) == expected

    @pytest.mark.parametrize(
        ("glue", "expected"),
        [
            ("\u2014", f"look {STRIPPED_IMAGE_MARKER}\u2014C:/y/b.png"),
            ("\U0001f4c1", "look C:/x/a.png\U0001f4c1C:/y/b.png"),
            ("\\", f"look {STRIPPED_IMAGE_MARKER}"),
        ],
    )
    def test_windows_glued_pairs_follow_the_same_rules(self, monkeypatch, glue, expected):
        # Punctuation separates; a symbol hides both; a backslash is a Windows
        # path character, so that pair is one token and one marker.
        from kiro_crew import image_refs

        monkeypatch.setattr(image_refs, "_PATH_RE", image_refs._WINDOWS_PATH_RE)

        assert strip_image_refs(f"look C:/x/a.png{glue}C:/y/b.png") == expected

    def test_a_url_after_a_windows_path_is_not_part_of_it(self, monkeypatch):
        # A URL scheme's colon is not a drive colon, so a row naming a document
        # and then a web image keeps every word and the working URL.
        from kiro_crew import image_refs

        monkeypatch.setattr(image_refs, "_PATH_RE", image_refs._WINDOWS_PATH_RE)
        prose = r"C:\Users\me\report.md and the banner is at https://example.com/logo.png"

        assert strip_image_refs(prose) == prose
        assert (
            strip_image_refs(r"see C:\Users\me\shot.png and https://example.com/logo.png")
            == f"see {STRIPPED_IMAGE_MARKER} and https://example.com/logo.png"
        )

    def test_a_url_after_a_posix_path_is_not_part_of_it(self, monkeypatch):
        from kiro_crew import image_refs

        monkeypatch.setattr(image_refs, "_PATH_RE", image_refs._POSIX_PATH_RE)
        prose = "/home/me/report.md and the banner is at https://example.com/logo.png"

        assert strip_image_refs(prose) == prose
        assert (
            strip_image_refs("see /home/me/shot.png and https://example.com/logo.png")
            == f"see {STRIPPED_IMAGE_MARKER} and https://example.com/logo.png"
        )

    def test_a_long_message_of_path_fragments_scans_in_linear_time(self):
        # Space is both a delimiter and a legal path character, so every "/" here
        # opens a path body that would walk to the suffix: the bounded body is
        # what keeps this linear.
        text = (" /a" * 20000) + ".png~"

        started = time.perf_counter()
        out = strip_image_refs(text)
        elapsed = time.perf_counter() - started

        assert out == text
        assert elapsed < 5.0, f"image-reference scan took {elapsed:.3f}s"

    def test_a_path_component_after_an_image_suffix_is_stripped_whole(self, tmp_path):
        p = _dest(_png(tmp_path))

        assert strip_image_refs(f"see {p}版本/final.jpg") == f"see {STRIPPED_IMAGE_MARKER}"

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


class TestBarePathPassAgreesWithTheUncPredicate:
    r"""Both passes answer "is this ``//`` destination remote?" the same way.

    ``iter_local_refs`` classifies a destination through
    ``is_remote_destination``, which on Windows reclassifies a roaming
    profile's own UNC attachment (``//fileserver/home/me/.kiro/crew/...``) as
    local -- the fix this contract pins. The bare-path pass must call the SAME
    predicate: testing the raw ``REMOTE_PREFIXES`` tuple instead reads that
    stored attachment as a remote URL, so a replayed row keeps a dangling UNC
    path the builder itself would have inlined -- the exact divergence the
    predicate exists to close.

    The fixture mirrors ``TestUncDestinationIsNotARemoteUrl`` in
    ``test_outbound_files.py``: every spelling is forward-slash (the only
    spelling a markdown destination can carry) and the gate is purely lexical,
    so the simulated-Windows form answers the same on a POSIX CI box.
    """

    _UNC_HOME = "//fileserver/home/me/.kiro/crew"
    _STORED = f"{_UNC_HOME}/sessions/chat-1.attachments/{'0' * 16}-shot.png"

    @pytest.fixture
    def windows_with_a_unc_data_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.messaging import outbound_files as module

        monkeypatch.setattr(module, "os", type("OS", (), {"name": "nt"})(), raising=False)
        monkeypatch.setattr(
            "kiro_crew.config.paths.peek_data_home", lambda: pathlib.Path(self._UNC_HOME)
        )

    def test_a_stored_unc_attachment_is_stripped_by_both_passes(
        self, windows_with_a_unc_data_home: None
    ) -> None:
        # Markdown form (pass one) and bare form (pass two) of the same stored
        # attachment: both are local to the predicate, so both are scrubbed --
        # neither reading of a dangling reference survives a replay.
        out = strip_image_refs(f"![shot]({self._STORED}) and bare:\n{self._STORED}")

        assert self._STORED not in out
        assert out.count(STRIPPED_IMAGE_MARKER) == 2

    def test_a_share_outside_the_gateways_own_directories_keeps_its_text(
        self, windows_with_a_unc_data_home: None
    ) -> None:
        # The predicate borrows the filesystem gate's allowlist, so an
        # attacker-chosen host stays remote here exactly as it does in the
        # builder -- left alone by both passes.
        text = "see //evil/share/x.png and //fileserver/other/x.png here"
        assert strip_image_refs(text) == text


class TestBarePathPassKeepsProse:
    """``_PATH_CHARS`` admits a space or tab, so the grammar alone reads a
    sentence that holds a directory and, later, an image name as ONE path. The
    builder can afford that -- it inlines only after ``is_file()`` -- but a
    substitution has no such gate, and replacing that span deletes the words
    between the two from replayed history.

    A spaced span is replaced when its last token is a path on its own, or when
    its shape vouches for it: a channel's attachment line (alone on its line
    AND ending in the name the channel writer mints), or a markdown link's
    angle-form destination. Every rule is lexical -- the scrubber consults no file, because
    it runs inside the history build on the event loop while the builder's own
    probes are offloaded (``acp.client`` calls it through ``asyncio.to_thread``).
    """

    @pytest.mark.parametrize(
        "text",
        [
            # The reported row, verbatim: POSIX spelling on every host.
            "Please check /var/log/app and tell me why logo.png is broken",
            f"Please check {_DIR} and tell me why logo.png is broken",
            f"check {_DIR}\tthen logo.png",
            # A directory the scrubber could not stat even if it wanted to.
            "I checked /root/notes and the new logo.png is fine",
            # Parentheses and brackets wrap an aside far more often than a path.
            f"See the logs ({_DIR} and the new logo.png) first",
            f"see [{_SPACED}] here",
            # A quote pair is not proof of a single path: a person types
            # "/var/log/app has the broken logo.png" as one sentence.
            '"/var/log/app has the broken logo.png"',
            f'"{_DIR} has the broken logo.png"',
            f"'{_DIR} has the broken logo.png'",
            f"<{_DIR} has the broken logo.png>",
            # ... so a quoted spaced path is residue too, not a covered shape,
            # and so is a BARE link destination: this repo's own markdown reader
            # ends one at its first space, so the span is not a path to the code
            # that consumes it.
            f'see "{_SPACED}" here',
            f"see [the shot]({_SPACED}) here",
            # The documented residue: a spaced path written mid-sentence with no
            # shape to vouch for it keeps its text rather than risking the prose.
            f"I saved it to {_SPACED} for you.",
            # ... and so does one a person typed alone on its line: its name is
            # not the channel writer's, so nothing tells it from the prose below.
            f"look at this\n{_SPACED}",
            # A whole line a person types that opens with a directory and ends
            # at an image name: alone on its line, but not an attachment line.
            "/var/log/app has the broken logo.png.",
            f"{_DIR} has the broken logo.png",
            f"1. {_DIR} needs the new favicon.png",
            f"- {_DIR} shows the error in logo.png",
            f"> {_DIR} has images{os.sep}logo.png",
        ],
    )
    def test_prose_keeps_every_word(self, text):
        assert strip_image_refs(text) == text

    def test_a_code_span_ends_a_candidate(self):
        # The mask blanks code with a sentinel no path class holds, so a
        # candidate cannot run through a code span and swallow the prose
        # around it. The code itself stays readable.
        text = f"I ran /usr/bin/python `fix.py` on {_SPACED}"

        assert strip_image_refs(text) == text
        # ... and a path after the code is still judged on its own.
        bare = f"I ran `fix.py` on {_ABS_A}"
        assert strip_image_refs(bare) == f"I ran `fix.py` on {STRIPPED_IMAGE_MARKER}"

    @pytest.mark.parametrize("after", ["/other", _ABS_B])
    def test_a_code_span_ends_the_run_after_a_path_too(self, after):
        # The sentinel is a boundary on both sides: a slash or a second picture
        # past the code span opens a new run, so the picture before the code is
        # still complete and is replaced; the code stays readable.
        text = f"{_ABS_A}`x`{after}"

        assert strip_image_refs(text) == f"{STRIPPED_IMAGE_MARKER}`x`{after}"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # The grammar's span runs from the directory to the image, but its
            # last token is a reference on its own.
            (f"Check {_DIR} and {_ABS_A}", f"Check {_DIR} and {STRIPPED_IMAGE_MARKER}"),
            (f"{_DIR} and {_ABS_A}", f"{_DIR} and {STRIPPED_IMAGE_MARKER}"),
            (
                f"check {_DIR} and tell me why ({_ABS_A}) is broken",
                f"check {_DIR} and tell me why ({STRIPPED_IMAGE_MARKER}) is broken",
            ),
            (f"{_ABS_A}--that one", f"{STRIPPED_IMAGE_MARKER}--that one"),
            (f"here: {_ABS_A}.", f"here: {STRIPPED_IMAGE_MARKER}."),
        ],
    )
    def test_the_reference_inside_prose_is_still_stripped(self, text, expected):
        assert strip_image_refs(text) == expected

    def test_a_token_that_only_tails_a_path_does_not_narrow_the_span(self):
        # "(1)/img.png" matches the grammar from "/img.png", which does not
        # stand alone -- so the span stays the whole path and is judged on its
        # own shape, here the channel append shape.
        spaced = (
            "C:\\Users\\me\\photos (1)\\tmpab12cd_4.png"
            if os.name == "nt"
            else "/Users/me/photos (1)/tmpab12cd_4.png"
        )
        assert strip_image_refs(f"look\n{spaced}") == f"look\n{STRIPPED_IMAGE_MARKER}"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # The channel append shape, the whole row, and the list and quote
            # markers a reply sets a file list in.
            (f"look at this\n{_TEMP}", f"look at this\n{STRIPPED_IMAGE_MARKER}"),
            (_TEMP, STRIPPED_IMAGE_MARKER),
            (f"  {_TEMP}  \nthanks", f"  {STRIPPED_IMAGE_MARKER}  \nthanks"),
            (
                f"files:\n- {_TEMP}\n- {_ABS_B}",
                f"files:\n- {STRIPPED_IMAGE_MARKER}\n- {STRIPPED_IMAGE_MARKER}",
            ),
            (f"> {_TEMP}", f"> {STRIPPED_IMAGE_MARKER}"),
            (f"1. {_TEMP}", f"1. {STRIPPED_IMAGE_MARKER}"),
            # A markdown destination in ANGLE form holds nothing but a path --
            # the CommonMark spelling for one with a space in it. An ordinary
            # link is not an image, so `iter_local_refs` leaves it to this pass.
            (
                f"see [the shot](<{_SPACED}>) here",
                f"see [the shot](<{STRIPPED_IMAGE_MARKER}>) here",
            ),
        ],
    )
    def test_a_spaced_path_whose_shape_vouches_for_it_is_stripped(self, text, expected):
        # These name no file on disk: a swept temp upload is the usual case, and
        # its dangling path is the reading this scrubber exists to remove.
        assert strip_image_refs(text) == expected

    def test_a_run_on_name_is_a_longer_name_on_both_sides(self):
        # "a.png.bak" is one file whose name merely contains a picture's: the
        # builder refuses to read "a.png" out of it, so the scrubber leaves the
        # same token alone and the sentence keeps every word.
        text = f"{_ABS_A}.bak is the backup"

        assert strip_image_refs(text) == text

    @pytest.mark.parametrize(
        "row",
        [
            # The reported sentence, where the path OPENS the line: only the
            # trailing-text check keeps it, and nothing else in this class does.
            "/var/log/app and tell me why logo.png is broken",
            "- /var/log/app and tell me why logo.png is broken",
            "> /var/log/app and tell me why logo.png is broken",
        ],
    )
    def test_prose_that_opens_its_line_with_a_path_keeps_every_word(self, row):
        assert strip_image_refs(f"two things:\n{row}\nthat is all") == (
            f"two things:\n{row}\nthat is all"
        )

    def test_a_code_span_on_its_own_line_is_not_swallowed(self):
        # The line is vouched by shape, so only the sentinel keeps the candidate
        # from running through the code span and taking the whole line with it.
        row = f"{_DIR}{os.sep}py `fix.py` on logo.png"
        assert strip_image_refs(f"ran it:\n{row}") == f"ran it:\n{row}"

    @pytest.mark.parametrize("tail", [".", ",", ":", "!", "?", "  ", ""])
    def test_sentence_punctuation_after_a_vouched_line_still_strips(self, tail):
        assert strip_image_refs(f"here it is:\n{_TEMP}{tail}") == (
            f"here it is:\n{STRIPPED_IMAGE_MARKER}{tail}"
        )

    @pytest.mark.parametrize("lead", ["- ", "* ", "+ ", "1. ", "2) ", "> ", "- [ ] ", "  "])
    def test_every_list_and_quote_lead_still_leaves_the_path_alone_on_its_line(self, lead):
        # Each lead ends in whitespace: `_STANDALONE_LEAD_RE`, base behaviour
        # this pass does not widen, rejects a path glued straight onto a marker.
        assert strip_image_refs(f"files:\n{lead}{_TEMP}") == (
            f"files:\n{lead}{STRIPPED_IMAGE_MARKER}"
        )

    def test_a_long_lead_with_many_spaced_spans_is_read_once_per_line(self, monkeypatch):
        # A 248 KB row with a long quote lead and thousands of channel-shaped
        # spans must not re-read that lead (`_LINE_LEAD_RE.fullmatch`) once per
        # candidate. The lead is matched once per line, and a candidate the
        # greedy lead does not reach is settled by one comparison -- so the
        # work is counted (fullmatch calls), and the clock is thread CPU read
        # once, with a bound a wall-clock stall cannot reach.
        import kiro_crew.image_refs as image_refs

        class _CountingLead:
            def __init__(self, real):
                self._real = real
                self.fullmatch_calls = 0

            def fullmatch(self, *args):
                self.fullmatch_calls += 1
                return self._real.fullmatch(*args)

            def match(self, *args):
                return self._real.match(*args)

        lead = _CountingLead(image_refs._LINE_LEAD_RE)
        monkeypatch.setattr(image_refs, "_LINE_LEAD_RE", lead)
        # The small equivalent: spans sharing a line stand alone to no rule, so
        # the line keeps every character; the channel line after it strips.
        small = f"> > {_TEMP} {_TEMP} {_TEMP}\n{_TEMP}"
        assert strip_image_refs(small) == f"> > {_TEMP} {_TEMP} {_TEMP}\n{STRIPPED_IMAGE_MARKER}"
        lead.fullmatch_calls = 0

        row = "> " * 60_000 + f"{_TEMP} " * 6_000 + f"{_TEMP}\n{_TEMP}"
        assert len(row) > 250_000
        cpu = time.thread_time()
        out = strip_image_refs(row)
        cpu = time.thread_time() - cpu

        assert out == row[: -len(_TEMP)] + STRIPPED_IMAGE_MARKER
        # One lead read per line that held a candidate the lead reached, not one
        # per candidate: 6 001 candidates on the first line share a single read.
        assert lead.fullmatch_calls == 2
        assert cpu < 2.0, f"scrub spent {cpu:.1f}s of CPU"

    def test_no_file_is_consulted(self):
        # Lexical by contract: a probe here would run on the event loop, and the
        # data home behind it can be a network share.
        # A BLANKET guard, not one keyed on the text's own paths: the rule that
        # was removed resolved the DATA HOME, which names none of them.
        def boom(target, *args, **kwargs):
            raise AssertionError(f"the scrubber stat-ed {target}")

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "stat", boom)
            patched.setattr(os, "lstat", boom)

            assert strip_image_refs(f"look\n{_TEMP}") == f"look\n{STRIPPED_IMAGE_MARKER}"
            assert strip_image_refs(f"check {_DIR} and why logo.png broke") == (
                f"check {_DIR} and why logo.png broke"
            )

    def test_prose_survives_the_replay_boundary(self):
        row = "Please check /var/log/app and tell me why logo.png is broken"
        replay = build_session_replay(_Log([{"role": "user", "content": row}]), "k")

        assert row in replay
        assert STRIPPED_IMAGE_MARKER not in replay

    def test_the_builder_still_inlines_a_spaced_current_turn_attachment(self, tmp_path):
        # The narrowing is the scrubber's alone: a spaced attachment handed in
        # the list is inlined, and marked where the text names it mid-sentence.
        p = _png(tmp_path, "Screen Shot 2024.png")
        blocks = build_prompt_blocks(
            f"look at {p} please", attachments=image_attachments([str(p)]), allow_image=True
        )

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"look at [image: {p.name}] please"

    def test_the_channel_writer_mints_the_name_the_scrubber_vouches_for(
        self, tmp_path, monkeypatch
    ):
        # The writer and the scrubber agree by test, not by a shared constant:
        # a real channel ingest temp file, under a temp directory holding a
        # space, is stripped from the line the channel appends it on.
        from kiro_crew.messaging.attachments import (
            IngestResult,
            _make_temp,
            append_attachment_context,
        )

        spaced_dir = tmp_path / "John Smith"
        spaced_dir.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(spaced_dir))
        dest = _make_temp(".png")
        row = append_attachment_context("look at this", IngestResult(image_paths=[dest]))

        # Spaced, or this pins nothing: an unspaced path strips by another rule.
        assert " " in dest
        assert os.path.dirname(dest) == str(spaced_dir)
        assert row == f"look at this\n{dest}"
        assert strip_image_refs(row) == f"look at this\n{STRIPPED_IMAGE_MARKER}"


class TestTheTitlePromptKeepsItsOwnImageHandling:
    """The title prompt builder does not call the scrubber, and why it cannot.

    ``run_bg_oneliner`` scrubs the composed prompt and sends the turn text-only
    (``test_run_bg_oneliner``), so no path that survives in a one-line prompt
    becomes an image block. ``chat_title._title_text`` strips markdown images
    and attachment markers its own way, because it keeps an escaped or
    code-quoted ``![x](...)`` readable where the scrubber's bare-path pass
    would replace the escaped one's destination.
    """

    _ROW = f"look at this\n{_TEMP}"

    def test_the_title_builder_keeps_a_bare_path_as_text(self):
        # NOT scrubbed here: `_title_text` keeps an escaped or code-quoted
        # `![x](...)` readable in a title prompt, and `strip_image_refs`
        # replaces the escaped one's destination (pinned below), so the title
        # path strips markdown images and attachment markers its own way. A
        # channel's bare path is neither, and stays in the title prompt as text.
        from kiro_crew.dashboard.chat_title import _build_title_prompt

        prompt = _build_title_prompt([{"role": "user", "content": self._ROW}])

        assert prompt and _TEMP in prompt
        assert STRIPPED_IMAGE_MARKER not in prompt

    def test_the_scrubber_replaces_the_escaped_destination_the_title_keeps(self):
        # Why the title path cannot share the scrubber: the bare-path pass
        # reaches inside an escaped marker, while a code-quoted one is masked
        # from both passes and survives.
        escaped = f"\\![image]({_ABS_A})"
        quoted = f"`![image]({_ABS_B})`"

        out = strip_image_refs(f"{escaped} and {quoted}")

        assert out == f"\\![image]({STRIPPED_IMAGE_MARKER}) and {quoted}"


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
        # The other row builder. It feeds the thread-history fallback, so a
        # path here reaches a model on a warm turn, not only after a compaction.
        p = _png(tmp_path)
        rows = _recall_rows(_Log(self._rows(_dest(p))), "k", conv_max=10)

        assert rows
        joined = "\n".join(r["content"] for r in rows)
        assert _dest(p) not in joined
        assert STRIPPED_IMAGE_MARKER in joined

    def test_the_current_turn_still_gets_its_picture(self, tmp_path):
        # The strip must not reach the live path: an image the user just
        # attached -- named in the text AND handed over as the channel's
        # structured attachment, which is what makes it an upload rather than
        # a mention -- has to keep becoming a real image block.
        p = _png(tmp_path)
        blocks = build_prompt_blocks(
            f"look at {p} please",
            attachments=image_attachments([str(p)]),
            allow_image=True,
        )

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
