"""Cross-channel contract: ``max_buttons`` is ENFORCED, per channel.

The capability ledger (``test_capability_ledger.py``) says the field is
enforced; THIS file is what makes that claim unforgeable. For every channel
declaring ``max_buttons > 0`` it drives the real options path with an
over-cap list and pins:

1. exactly ``max_buttons`` choices render interactively, and
2. the overflow degrades to a numbered text list (numbering continues after
   the widget slots) instead of being silently dropped — the pre-enforcement
   behavior lost choices without any user-visible signal.

A channel declaring ``max_buttons == 0`` renders no widget, and the same helper
answers it with zero widget slots: EVERY choice becomes a numbered line. That
half is pinned here too, because dropping the list deletes the answers to a
question the agent just asked and the user is left with a prompt and no way to
see what it offered.

Two ratchets keep both halves exhaustive: a channel that starts declaring
``max_buttons > 0`` without a pin in this file fails
``test_every_widget_channel_is_pinned_here``, and a zero-widget channel absent
from ``ZERO_WIDGET_RENDERERS`` fails
``test_every_zero_widget_channel_is_pinned_here``. The second is keyed on a
renderer FACTORY rather than a name, because ``text()`` is not on the ``Renderer``
ABC — nothing in code forces a zero-widget renderer to call the helper, so the
ratchet has to demand something it can actually drive.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import pytest

from kiro_crew.messaging.renderer import (
    apply_options_cap,
    cap_choices,
    render_options_as_text,
    split_options_trailer,
)
from kiro_crew.messaging.transport import TransportCapabilities

#: channel_type -> the test class below that pins its enforcement.
PINNED_WIDGET_CHANNELS = {"slack", "discord", "telegram", "teams", "webex"}


def _wecom_renderer() -> Any:
    from kiro_crew.wecom.renderer import WeComRenderer
    from kiro_crew.wecom.transport import WECOM_CAPABILITIES

    return WeComRenderer(object(), "rq1", "https://r", WECOM_CAPABILITIES)


def _weixin_renderer() -> Any:
    from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES
    from kiro_crew.weixin.turn_renderer import WeixinRenderer

    return WeixinRenderer(
        object(), "peer", WEIXIN_CAPABILITIES, ctx_store=object(), account_id="acct"
    )


def _imessage_renderer() -> Any:
    from kiro_crew.imessage.renderer import IMessageRenderer
    from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES

    return IMessageRenderer(object(), "+61400000000", IMESSAGE_CAPABILITIES)


def _feishu_renderer() -> Any:
    from kiro_crew.feishu.renderer import FeishuRenderer
    from kiro_crew.feishu.transport import FEISHU_CAPABILITIES

    return FeishuRenderer(object(), "om_msg", FEISHU_CAPABILITIES)


#: Channels rendering no widget, each with a factory driving its REAL renderer
#: against its REAL capabilities. Keyed this way rather than as a set of names so
#: the ratchet below cannot be satisfied by adding a string: a new zero-widget
#: channel has to supply something this file can actually drive.
ZERO_WIDGET_RENDERERS: dict[str, Callable[[], Any]] = {
    "wecom": _wecom_renderer,
    "weixin": _weixin_renderer,
    "imessage": _imessage_renderer,
    "feishu": _feishu_renderer,
}


def _all_channel_capabilities() -> dict[str, TransportCapabilities]:
    from kiro_crew.discord.transport import DISCORD_CAPABILITIES
    from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
    from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES
    from kiro_crew.slack.transport import SLACK_CAPABILITIES
    from kiro_crew.teams.transport import TEAMS_CAPABILITIES
    from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
    from kiro_crew.webex.transport import WEBEX_CAPABILITIES
    from kiro_crew.wecom.transport import WECOM_CAPABILITIES
    from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES

    return {
        "slack": SLACK_CAPABILITIES,
        "discord": DISCORD_CAPABILITIES,
        "telegram": TELEGRAM_CAPABILITIES,
        "teams": TEAMS_CAPABILITIES,
        "webex": WEBEX_CAPABILITIES,
        "wecom": WECOM_CAPABILITIES,
        "weixin": WEIXIN_CAPABILITIES,
        "imessage": IMESSAGE_CAPABILITIES,
        "feishu": FEISHU_CAPABILITIES,
    }


class TestRatchet:
    def test_every_widget_channel_is_pinned_here(self) -> None:
        widget_channels = {
            name for name, caps in _all_channel_capabilities().items() if caps.max_buttons > 0
        }
        assert widget_channels == PINNED_WIDGET_CHANNELS, (
            "A channel's max_buttons declaration changed. Every channel "
            "declaring max_buttons > 0 must have an enforcement pin in this "
            f"file. unpinned={widget_channels - PINNED_WIDGET_CHANNELS} "
            f"stale={PINNED_WIDGET_CHANNELS - widget_channels}"
        )

    def test_every_zero_widget_channel_is_pinned_here(self) -> None:
        # Keyed on the FACTORY map, not a set of names: a name could be added to
        # a bare set to make this green, which would leave the channel with no
        # actual pin -- nothing in code forces a renderer to call the helper.
        zero_widget = {
            name for name, caps in _all_channel_capabilities().items() if caps.max_buttons == 0
        }
        assert zero_widget == set(ZERO_WIDGET_RENDERERS), (
            "A channel's max_buttons declaration changed. Every channel "
            "declaring max_buttons == 0 needs a renderer factory in "
            "ZERO_WIDGET_RENDERERS so its numbered-text fallback is driven here. "
            f"unpinned={zero_widget - set(ZERO_WIDGET_RENDERERS)} "
            f"stale={set(ZERO_WIDGET_RENDERERS) - zero_widget}"
        )

    def test_the_two_pinned_sets_cover_every_channel(self) -> None:
        # A channel is widget-capable or not, so the union must be the whole
        # shipped set. A NEGATIVE max_buttons would land in neither and is the
        # only way to sit in the gap between the two ratchets above.
        assert set(_all_channel_capabilities()) == (
            PINNED_WIDGET_CHANNELS | set(ZERO_WIDGET_RENDERERS)
        )


class TestSharedHelper:
    def test_under_cap_is_byte_identical(self) -> None:
        caps = TransportCapabilities(max_buttons=3)
        body, kept = apply_options_cap("Choose.", ["A", "B"], caps)
        assert body == "Choose."
        assert kept == ["A", "B"]

    def test_overflow_degrades_to_numbered_text_continuing_the_widget_slots(self) -> None:
        caps = TransportCapabilities(max_buttons=2)
        body, kept = apply_options_cap("Pick one.", ["A", "B", "C", "D"], caps)
        assert kept == ["A", "B"]
        assert body == "Pick one.\n\n3. C\n4. D"

    def test_zero_cap_keeps_nothing_and_numbers_every_choice(self) -> None:
        # A button-less channel is the overflow case with zero widget slots, not
        # a channel with nothing to say. Returning the body alone deleted the
        # answers to the question the body just asked.
        caps = TransportCapabilities(max_buttons=0)
        body, kept = apply_options_cap("Text.", ["A", "B"], caps)
        assert body == "Text.\n\n1. A\n2. B"
        assert kept == []

    def test_zero_cap_with_no_choices_is_byte_identical(self) -> None:
        caps = TransportCapabilities(max_buttons=0)
        body, kept = apply_options_cap("Text.", [], caps)
        assert body == "Text."
        assert kept == []

    def test_zero_cap_renders_one_blank_line_however_the_body_ends(self) -> None:
        # A body that already ends in a newline needs one fewer, so both spellings
        # render as exactly one blank line between the prompt and the list.
        caps = TransportCapabilities(max_buttons=0)
        assert apply_options_cap("Pick.", ["A"], caps)[0] == "Pick.\n\n1. A"
        assert apply_options_cap("Pick.\n", ["A"], caps)[0] == "Pick.\n\n1. A"

    def test_zero_cap_with_empty_body_is_just_the_list(self) -> None:
        caps = TransportCapabilities(max_buttons=0)
        body, _ = apply_options_cap("", ["A", "B"], caps)
        assert body == "1. A\n2. B"

    def test_cap_choices_splits_without_formatting(self) -> None:
        caps = TransportCapabilities(max_buttons=1)
        kept, overflow = cap_choices(["A", "B", "C"], caps)
        assert kept == ["A"]
        assert overflow == ["B", "C"]

    def test_overflow_neutralizes_mass_mention_syntax(self) -> None:
        # Regression (review round 2): overflow lands in the message BODY
        # where platforms parse mentions — unlike widget labels, which render
        # as plain text. A prompt-injected choice must not mass-notify.
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["ping @everyone now", "or <!channel> maybe"], start=1)
        assert "@everyone" not in out
        assert "<!channel>" not in out
        # The text stays human-readable — only the trigger syntax is broken.
        assert "everyone" in out and "channel" in out

    def test_overflow_redacts_credentials_in_their_DISPLAY_form(self) -> None:
        # Regression (review round 5): overflow lands in the markdown-parsed
        # BODY, so a key split by a code span or emphasis is broken to every
        # byte-level scan (the driver's stream redactor included) and WHOLE on
        # screen once the platform drops the delimiters. Slack's widget path
        # already routes choices through the display redactor for exactly this
        # reason; the shared sink has to close the same hole for telegram and
        # discord, which have no display-state pass of their own.
        from kiro_crew.messaging.renderer import format_overflow

        split = "AKIA`" + "`IOSFODNN7EXAMPLE"
        out = format_overflow([f"Retry with {split}"], start=1)
        assert "IOSFODNN7EXAMPLE" not in out, (
            "a backtick-split key survived the overflow sink — the platform "
            "strips the delimiters and shows the reader an intact credential"
        )

    def test_overflow_redaction_runs_before_mention_defanging(self) -> None:
        # Both sanitisations transform the text; if the ZWSP went in first it
        # could split a key so the regex stops matching while the platform
        # still renders it whole. Pin the order with a choice that needs both.
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["@everyone use AKIA*IOSFODNN7EXAMPLE*"], start=0)
        assert "@everyone" not in out
        assert "IOSFODNN7EXAMPLE" not in out

    def test_overflow_redacts_a_spoiler_split_key(self) -> None:
        # Regression (review round 6): ``||…||`` is Discord's spoiler. The
        # reader clicks it, the delimiters vanish and the halves join — the
        # same splitter property as ``**``, but it was missing from the
        # canonicaliser's delimiter run, so round 5's fix had a hole exactly
        # one delimiter family wide.
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["Retry with AKIA||IOSFODNN7EXAMPLE||"], start=0)
        assert "IOSFODNN7EXAMPLE" not in out, (
            "a spoiler-split key survived — Discord joins the halves when the "
            "reader reveals the spoiler"
        )

    def test_overflow_redacts_an_invisible_character_split_key(self) -> None:
        # The invisible half of the same hazard, and worse than the markup half:
        # a zero-width character renders as NOTHING, so the reader sees an
        # intact key with no click and no markup while every literal scan sees
        # it broken. Pre-existing in the display redactor; closed here because
        # this sink is what puts LLM-authored choice text into the body.
        from kiro_crew.messaging.renderer import format_overflow

        for name, ch in (
            ("ZWSP", "\u200b"),
            ("ZWNJ", "\u200c"),
            ("word joiner", "\u2060"),
            ("BOM", "\ufeff"),
            ("soft hyphen", "\u00ad"),
        ):
            out = format_overflow([f"Retry with AKIA{ch}IOSFODNN7EXAMPLE"], start=0)
            assert "IOSFODNN7EXAMPLE" not in out, f"{name} split the key past the scan"

    def test_non_ascii_text_is_not_mangled(self) -> None:
        """The format-character filter must not touch visible non-ASCII text."""
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["重新部署到主分支", "café — naïve"], start=0)
        assert out == "1. 重新部署到主分支\n2. café — naïve"

    def test_a_lone_pipe_is_left_alone(self) -> None:
        """The pipe counts only in pairs — pinned so the boundary is deliberate.

        A single ``|`` is literal on every channel here, so collapsing it would
        widen the canonical form with no rendering that matches it. This also
        keeps ordinary table-ish text intact.
        """
        from kiro_crew.messaging.display_safety import canonicalize_display

        assert canonicalize_display("a|b") == "a|b"
        assert canonicalize_display("a||b") == "ab"

    def test_clean_choices_are_untouched_by_the_redactor(self) -> None:
        """The sink must not mangle ordinary text — no false-positive damage."""
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["Rebase onto main", "Skip the `--force` flag"], start=2)
        assert out == "3. Rebase onto main\n4. Skip the `--force` flag"


class TestSplitOptionsTrailer:
    """The ONE parse of the ``[OPTIONS:]`` marker, both halves and both policies.

    Six channels carried this parse before it was hoisted -- three of them
    (Discord, Telegram, Teams) identical down to the comment. The reason to pin it
    here rather than per channel is that a duplicated parse drifts silently: each
    copy reads correctly in isolation, so nothing goes red when one of them stops
    agreeing with the others.
    """

    def test_a_complete_trailer_yields_body_and_choices(self) -> None:
        body, choices = split_options_trailer("Pick one.\n\n[OPTIONS: A | B | C]")
        assert body == "Pick one."
        assert choices == ["A", "B", "C"]

    def test_choices_are_stripped_and_blanks_dropped(self) -> None:
        _, choices = split_options_trailer("q\n\n[OPTIONS:  A  |  | B ]")
        assert choices == ["A", "B"]

    def test_no_marker_is_an_untouched_passthrough(self) -> None:
        text = "Just an answer, no trailer."
        assert split_options_trailer(text) == (text, [])

    def test_a_matched_but_empty_trailer_still_strips_the_marker(self) -> None:
        """Otherwise reserved protocol ships as visible text.

        Distinct from the no-match case, which must NOT strip -- the two are only
        distinguishable by comparing the body, which is why the zero-widget path
        routes through ``apply_options_cap`` unconditionally rather than branching
        on an empty choice list.
        """
        body, choices = split_options_trailer("Body here.\n\n[OPTIONS: ]")
        assert body == "Body here."
        assert choices == []

    def test_a_quoted_marker_mid_answer_cannot_swallow_the_body(self) -> None:
        """The end-of-buffer anchor is what prevents this."""
        text = "See [OPTIONS: in the docs] for the list, then decide."
        assert split_options_trailer(text) == (text, [])

    def test_a_partial_marker_is_kept_by_default(self) -> None:
        """The BUFFERED reading: cutting the assistant's prose is permanent.

        A sealed reply ending ``see the [OPTIONS section`` must keep its last four
        words, so the default cannot be the destructive one.
        """
        text = "Read the docs, see the [OPTIONS section"
        assert split_options_trailer(text) == (text, [])

    def test_a_partial_marker_is_hidden_when_asked(self) -> None:
        """The STREAMING reading: the fragment may be a marker mid-flight."""
        body, choices = split_options_trailer("Working on it. [OPTIONS: A | B", hide_partial=True)
        assert body == "Working on it."
        assert choices == []

    def test_hide_partial_does_not_touch_a_closed_bracket_elsewhere(self) -> None:
        """Only an UNCLOSED fragment is a fragment.

        ``[OPTIONS: …]`` that failed the end anchor is prose, not a live marker, so
        the streaming reading must not cut it either.
        """
        text = "See [OPTIONS: in the docs] for the list."
        assert split_options_trailer(text, hide_partial=True) == (text, [])

    def test_the_default_is_the_non_destructive_one(self) -> None:
        """Pins the DIRECTION of the default, not just its value.

        A caller that forgets to state a policy must degrade toward a cosmetic
        failure (reserved markup visible for one frame), never toward deleting
        text nobody can recover.
        """
        import inspect

        param = inspect.signature(split_options_trailer).parameters["hide_partial"]
        assert param.default is False
        assert param.kind is inspect.Parameter.KEYWORD_ONLY


class TestOnlyOneTrailerParseExists:
    """Ratchet: no channel may re-derive the trailer parse.

    Greps the tree rather than trusting review. The two allowed sites are the
    shared helper itself and ``constants.split_trailing_protocol_suffix``, which
    answers a different question (where does the protocol suffix begin, for a
    length splitter). ``slack/format.py`` is deliberately NOT in scope: it parses
    the LINE grammar (``OPTIONS_RE_LINE``, end-of-line, MULTILINE), not the
    end-of-buffer TRAILER, so converging it would silently stop matching a marker
    mid-message.
    """

    _ALLOWED_PARTIAL = {"messaging/renderer.py", "constants.py"}

    def _hits(self, needle: str) -> set[str]:
        from pathlib import Path

        import kiro_crew as pkg

        root = Path(pkg.__file__).parent
        found = set()
        for path in root.rglob("*.py"):
            if "_vendor" in path.parts or "static" in path.parts:
                continue
            if needle in path.read_text(encoding="utf-8"):
                # ``as_posix``, not ``str``: on Windows the latter yields
                # ``messaging\renderer.py``, which matches no entry in the
                # forward-slash allow-lists below -- so the exemptions silently
                # stop applying and the ratchet reports its own allowed sites as
                # offenders. A path used as a KEY has to have one spelling.
                found.add(path.relative_to(root).as_posix())
        return found

    def test_no_channel_hand_rolls_the_partial_marker_scan(self) -> None:
        """The pre-generalisation literal must not come back anywhere.

        The scan used to be spelled ``rfind("[OPTIONS")`` in the allowed sites
        themselves, so this check subtracted them. It no longer is: the scan now
        iterates :data:`MARKER_PREFIXES`, because a second marker head was added
        and a one-literal scan cannot see it. That makes the expected hit count
        ZERO rather than "only the allowed sites", so the allow-list is gone from
        this check on purpose -- a hit is a re-derivation wherever it appears,
        including in the shared sites, where it would mean the generalisation was
        reverted for one head.

        Zero-hit checks cannot tell success from a broken grep, so non-vacuity is
        established by ``test_the_ratchet_is_not_vacuous`` below, which anchors on
        needles that MUST exist.
        """
        offenders = self._hits('rfind("[OPTION')
        assert not offenders, (
            "these spell the unfinished-marker scan as a single literal, which "
            "cannot see every marker head -- iterate constants.MARKER_PREFIXES, "
            "or pass hide_partial= to messaging.renderer.split_options_trailer: "
            f"{sorted(offenders)}"
        )

    def test_no_channel_iterates_the_marker_prefixes_itself(self) -> None:
        """The generalised spelling is ratcheted exactly as the literal was.

        Two sites are allowed because they answer a DIFFERENT question, the same
        grounds on which ``slack/format.py`` is exempt from the choice-split check
        below:

        * ``slack/handler.py`` — ``_filter_options_brackets`` is a
          character-by-character filter over a live stream, so it has no buffer to
          run a regex against and must test the head as a string prefix.
        * ``messaging/tables.py`` — classifies whether ONE line is a protocol
          trailer rather than a GFM table row, so it needs a per-line
          ``startswith``, not a scan for where an unfinished marker begins. It
          consumes the shared tuple precisely so the heads live in one place; a
          literal list there previously knew only ``"[OPTIONS"`` and let an action
          marker's ``close=a | close=b`` body be absorbed as a table body row.
        """
        allowed = self._ALLOWED_PARTIAL | {"slack/handler.py", "messaging/tables.py"}
        offenders = self._hits("MARKER_PREFIXES") - allowed
        assert not offenders, (
            "these re-derive the marker-head scan instead of passing hide_partial= "
            f"to messaging.renderer.split_options_trailer: {sorted(offenders)}"
        )

    def test_only_the_shared_helper_and_slack_split_the_choice_group(self) -> None:
        # Slack keeps its own because its GRAMMAR differs (OPTIONS_RE_LINE).
        allowed = {"messaging/renderer.py", "slack/format.py"}
        offenders = self._hits('group(1).split("|")') - allowed
        assert not offenders, (
            "these re-derive the choice split instead of calling "
            f"messaging.renderer.split_options_trailer: {sorted(offenders)}"
        )

    def test_the_ratchet_is_not_vacuous(self) -> None:
        """A grep that matches nothing would make the checks above pass forever.

        Anchored on the spelling the tree ACTUALLY uses. The previous anchor was
        the ``rfind("[OPTIONS")`` literal; generalising the scan removed that
        literal from every site, which took both ratchets vacuous in one commit
        and is exactly the failure this test exists to catch.

        Re-anchored a second time for the same reason: centralising the head scan
        on ``constants.rfind_marker_head`` removed ``MARKER_PREFIXES`` from
        ``messaging/renderer.py``, so anchoring on that name there went vacuous.
        The helper's own name is what renderer.py now spells.
        """
        assert "constants.py" in self._hits("MARKER_PREFIXES")
        assert "messaging/renderer.py" in self._hits("rfind_marker_head")
        assert "messaging/tables.py" in self._hits("starts_with_marker_head")
        assert "messaging/renderer.py" in self._hits('group(1).split("|")')

    def test_no_site_re_derives_a_case_sensitive_head_scan(self) -> None:
        """The head scan must be CASE-AWARE, and only one place decides that.

        The action patterns carry ``re.IGNORECASE`` while the content ones do not,
        so a bare ``rfind``/``startswith`` on a head literal is wrong by
        construction: it misses a mixed-case fragment that the parser WILL strip,
        leaving raw protocol text sealed into a sent message. Both helpers live in
        ``constants.py``; every other module must call one of them.

        Parsed with ``ast``, NOT grepped. Two modules DESCRIBE this bug in a
        docstring, quoting the very literal a substring scan looks for, so a grep
        reports the documentation as the offender — a check that fires on its own
        explanation. Only a real call node counts.
        """
        import ast
        from pathlib import Path

        import kiro_crew as pkg

        root = Path(pkg.__file__).parent
        offenders: set[str] = set()
        for path in root.rglob("*.py"):
            if "_vendor" in path.parts or "static" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            if rel == "constants.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - not a defect of this ratchet
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in ("rfind", "find", "startswith"):
                    continue
                for arg in node.args:
                    literals = arg.elts if isinstance(arg, (ast.Tuple, ast.List)) else [arg]
                    for lit in literals:
                        if (
                            isinstance(lit, ast.Constant)
                            and isinstance(lit.value, str)
                            and lit.value.startswith("[OPTION")
                        ):
                            offenders.add(rel)
        assert not offenders, (
            "these scan for a marker head with a case-SENSITIVE match instead of "
            f"calling constants.rfind_marker_head / starts_with_marker_head: {sorted(offenders)}"
        )


class TestRenderOptionsAsText:
    """The whole trailer path for a zero-widget channel, in one place.

    Left to themselves the channels drifted: several carried a byte-identical
    ``_strip_options``, Weixin a looser ``sub()`` variant that suppressed nothing,
    and each pinned these properties in its own file — including its own copy of
    the ReDoS regression. They are pinned once here, against the helper they all
    call, so a new zero-widget channel inherits the properties instead of
    re-deriving them.
    """

    CAPS = TransportCapabilities(max_buttons=0)

    def test_a_complete_trailer_becomes_a_numbered_list(self) -> None:
        out = render_options_as_text("Pick one.\n\n[OPTIONS: a | b | c]", self.CAPS)
        assert out == "Pick one.\n\n1. a\n2. b\n3. c"

    def test_an_unfinished_marker_is_left_alone(self) -> None:
        # It LOOKS like a marker still arriving, but this helper cannot tell a live
        # frame from a sealed answer, and the channels calling it buffer a whole turn
        # and send once — so for them such a tail is the assistant's prose, and
        # cutting it is permanent data loss. The one zero-widget channel that does
        # stream (WeCom) trades the other way in its own helper, where the cost is a
        # transient flash whose next frame replaces the bubble anyway.
        assert (
            render_options_as_text("answer [OPTIONS: a | b", self.CAPS) == "answer [OPTIONS: a | b"
        )

    def test_prose_ending_in_a_bare_marker_word_keeps_its_last_words(self) -> None:
        text = "see the [OPTIONS section"
        assert render_options_as_text(text, self.CAPS) == text

    def test_plain_text_is_returned_unchanged(self) -> None:
        assert render_options_as_text("just an answer", self.CAPS) == "just an answer"

    def test_empty_text_is_returned_unchanged(self) -> None:
        assert render_options_as_text("", self.CAPS) == ""

    def test_prose_that_merely_MENTIONS_a_marker_is_never_deleted(self) -> None:
        # Only a COMPLETE trailer at the very END is ours. Anything else is the
        # assistant's answer: deleting it to be tidy about protocol would lose the
        # user's content, which is worse than leaving a marker visible.
        assert (
            render_options_as_text("See the [STEERING design doc", self.CAPS)
            == "See the [STEERING design doc"
        )
        # A steering frame reaching a renderer at all means TurnDriver did not
        # strip it; this sink is not the place to guess. Left intact.
        raw = "answer\n[OPTIONS: a | b]\n[STEERING steer-1234"
        assert render_options_as_text(raw, self.CAPS) == raw

    def test_choice_whitespace_is_stripped_and_blanks_dropped(self) -> None:
        out = render_options_as_text("Q\n[OPTIONS:  a  |   | b ]", self.CAPS)
        assert out == "Q\n\n1. a\n2. b"

    def test_body_text_before_the_trailer_keeps_its_own_newlines(self) -> None:
        out = render_options_as_text("line one\nline two\n[OPTIONS: a]", self.CAPS)
        assert out == "line one\nline two\n\n1. a"

    #: Samples per size, taking the MINIMUM. Even in CPU time a single sample can
    #: absorb a GC pause; the fastest of a few is the machine's best effort, which
    #: is the quantity that reflects the algorithm rather than the host.
    _SAMPLES = 3

    #: Calls per timed batch. ONE call is single-digit milliseconds, and Windows'
    #: ``process_time`` granularity is ~15.6 ms — so a single call measures as
    #: exactly 0.0 there and any ratio built from it is noise, not signal (a
    #: Windows shard produced "ratio 15625.0x" against a provably linear regex,
    #: which is 1/1e-6, i.e. the divide-by-zero floor rather than a measurement).
    #: 20 calls puts the batch 5-11x above that tick on measured hardware.
    _REPS = 20

    #: A batch must clear this to be a measurement at all. Belt-and-braces against
    #: the failure above recurring on a platform whose clock is coarser still, or a
    #: machine fast enough to drop back under the tick: fail LOUDLY asking for more
    #: reps rather than silently comparing two zeroes.
    _MIN_BATCH_SECONDS = 0.02

    def _growth_ratio(self, build: Callable[[int], str], n: int) -> float:
        """CPU-time ratio for *build* at ``n`` and ``2n``, min-of-N batches.

        Three choices make this a COMPLEXITY assertion rather than a performance
        one, which is what keeps it from false-reddening a loaded shard:

        * **The ratio, not a duration.** An absolute budget passes or fails on how
          busy the host is and on whether coverage is enabled. Linear matching
          stays near 2x per doubling on any machine; polynomial backtracking blows
          past it on every machine.
        * **``process_time``, not ``perf_counter``.** Wall clock counts the time
          this process spent DESCHEDULED, so under the CPU oversubscription an
          ``-n auto`` shard creates, one sample can absorb another worker's slice
          and invent a 6x ratio out of a linear regex.
        * **A batch, not one call.** CPU time is scheduler-immune but COARSE on
          Windows; see ``_REPS``.
        """

        def best(size: int) -> float:
            text = build(size)
            render_options_as_text(text, self.CAPS)  # warm: exclude the first call
            return min(self._cpu_per_call(text) for _ in range(self._SAMPLES))

        # Smaller size FIRST: a cold cache or a page fault charged to whichever
        # size runs first must not be charged to the numerator.
        small = best(n)
        return best(2 * n) / small

    def _cpu_per_call(self, text: str) -> float:
        """Mean CPU seconds per call, measured over a batch above the clock's tick."""
        start = time.process_time()
        for _ in range(self._REPS):
            render_options_as_text(text, self.CAPS)
        batch = time.process_time() - start
        assert batch >= self._MIN_BATCH_SECONDS, (
            f"batch of {self._REPS} measured {batch:.4f}s, under the "
            f"{self._MIN_BATCH_SECONDS}s floor — the clock cannot resolve it, so "
            "any ratio would be noise. Raise _REPS."
        )
        return batch / self._REPS

    def test_an_unterminated_options_tag_is_not_redos(self) -> None:
        # Regression (py/polynomial-redos), consolidated from the wecom, webex
        # and teams renderer suites: a greedy ``.*`` body could consume a "["
        # that ALSO starts the outer "[OPTIONS:" literal, so over text with many
        # "[OPTIONS:" prefixes search() re-explored the body from each position —
        # polynomial. The tempered body in OPTIONS_RE_TRAILER forbids only a
        # re-occurring "[OPTIONS:", so the match is linear.
        # Returned unchanged (no complete trailer), which is the point of the
        # timing check below: the regex must REJECT this in linear time, not
        # backtrack over it.
        evil = "[OPTIONS:" + ("\t" * 200_000) + "x"
        assert render_options_as_text(evil, self.CAPS) == evil
        ratio = self._growth_ratio(lambda n: "[OPTIONS:" + ("\t" * n) + "x", 100_000)
        assert ratio < 8.0, f"superlinear in input length (ratio {ratio:.1f}x)"

    def test_many_repeated_options_prefixes_are_not_redos(self) -> None:
        # The real polynomial pump: each "[OPTIONS:" is another position the body
        # could be re-explored from.
        ratio = self._growth_ratio(lambda n: "[OPTIONS:" * n + "x", 50_000)
        assert ratio < 8.0, f"superlinear in prefix count (ratio {ratio:.1f}x)"


class TestZeroWidgetChannelEnforcement:
    """Each zero-widget renderer's own text path, driven through its real
    capabilities object.

    ``TestRenderOptionsAsText`` pins the helper; this pins that each channel
    actually routes through it — the thing a renderer can silently stop doing,
    since ``text()`` is not on the ``Renderer`` ABC and nothing forces the call.
    """

    TRAILER = "Deploy now?\n\n[OPTIONS: yes | no]"
    EXPECTED = "Deploy now?\n\n1. yes\n2. no"

    @pytest.mark.parametrize("channel", sorted(ZERO_WIDGET_RENDERERS))
    def test_the_trailer_becomes_numbered_text(self, channel: str) -> None:
        renderer = ZERO_WIDGET_RENDERERS[channel]()
        renderer._buf = [self.TRAILER]
        assert renderer.text() == self.EXPECTED

    #: WeCom keeps a LOCAL variant of the helper and hides an unfinished marker
    #: instead of keeping it. That is a defensible choice for the one zero-widget
    #: channel that STREAMS — its next frame replaces the bubble, so a partial
    #: marker is a transient flash rather than the permanent loss it would be for
    #: a channel that only ever sends the sealed answer. The shared helper keeps
    #: the tail for exactly that reason, so this assertion covers the callers of
    #: the shared helper and names WeCom's divergence rather than hiding it.
    _KEEPS_AN_UNFINISHED_TAIL = sorted(set(ZERO_WIDGET_RENDERERS) - {"wecom"})

    @pytest.mark.parametrize("channel", _KEEPS_AN_UNFINISHED_TAIL)
    def test_an_unfinished_marker_is_left_alone(self, channel: str) -> None:
        # No channel may delete authored text to tidy up an incomplete marker.
        renderer = ZERO_WIDGET_RENDERERS[channel]()
        renderer._buf = ["Deploy now? [OPTIONS: yes | n"]
        assert renderer.text() == "Deploy now? [OPTIONS: yes | n"


class TestSlackEnforcement:
    def _choices(self, n: int) -> list[str]:
        return [f"Choice {i}" for i in range(1, n + 1)]

    def test_widget_caps_at_declared_and_overflow_is_visible(self) -> None:
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        blocks = build_options_blocks(self._choices(n + 3))
        actions = next(b for b in blocks if b["type"] == "actions")
        opts = actions["elements"][0]["options"]
        assert len(opts) == n
        overflow = next(b for b in blocks if b["type"] == "context")
        text = overflow["elements"][0]["text"]
        # Numbering continues after the widget slots; every dropped choice shows.
        assert f"{n + 1}. Choice {n + 1}" in text
        assert f"{n + 3}. Choice {n + 3}" in text

    def test_under_cap_emits_no_overflow_block(self) -> None:
        from kiro_crew.slack.format import build_options_blocks

        blocks = build_options_blocks(self._choices(2))
        assert [b["type"] for b in blocks] == ["actions"]

    def test_huge_overflow_is_chunked_not_sliced(self) -> None:
        # Regression (review round 1): a single [:2900] slice re-created the
        # silent data loss the cap exists to remove. Every overflow choice
        # must reach the wire, across as many context blocks as needed.
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        long = [f"Choice {i} " + "x" * 140 for i in range(1, n + 41)]
        blocks = build_options_blocks(long)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert len(ctx) >= 2, "one sliced block would drop tail choices"
        joined = "".join(b["elements"][0]["text"] for b in ctx)
        assert f"{n + 40}." in joined, "the LAST overflow choice must survive"

    def test_pathological_overflow_is_bounded_with_visible_truncation(self) -> None:
        # Regression (review round 3): unbounded context blocks blow Slack's
        # 50-block message limit — the API rejects the WHOLE message and every
        # choice disappears. The block budget is capped and the tail drop is
        # VISIBLE (counted marker), never silent.
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        huge = [f"Choice {i} " + "x" * 140 for i in range(1, n + 201)]
        blocks = build_options_blocks(huge)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert len(ctx) <= 4, "block budget must be bounded"
        assert len(blocks) <= 5
        marker = ctx[-1]["elements"][0]["text"]
        assert "omitted" in marker
        # The marker counts what was dropped — no silent loss.
        assert any(ch.isdigit() for ch in marker)

    def test_single_oversized_choice_truncates_with_visible_marker(self) -> None:
        # Regression (review round 4): one absurd >2900-char choice was
        # sliced with no signal. The cut must be visible.
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        choices = [f"Choice {i}" for i in range(1, n + 1)] + ["y" * 4000]
        blocks = build_options_blocks(choices)
        ctx = [b for b in blocks if b["type"] == "context"]
        text = ctx[0]["elements"][0]["text"]
        assert len(text) <= 2900
        assert text.endswith("…"), "truncation must be visible, not silent"


class TestTelegramEnforcement:
    def test_steer_seal_near_limit_with_overflow_stays_under_transport_cap(self) -> None:
        # Regression (review round 1): on_steer_consumed ran _rotate_on_length
        # BEFORE apply_options_cap expanded the body with numbered overflow, so
        # a near-limit pre-steer answer sealed past the transport cap.
        from test_telegram import FakeClient

        from kiro_crew.messaging.renderer import STEER_CONSUMED, TEXT_CHUNK, OutputEvent
        from kiro_crew.telegram.client import TELEGRAM_CHUNK_LIMIT
        from kiro_crew.telegram.renderer import TelegramRenderer
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        n = TELEGRAM_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice number {i} with a long label" for i in range(1, n + 9))
        near_limit = "x" * (TELEGRAM_CHUNK_LIMIT - 60)
        cli = FakeClient()
        r = TelegramRenderer(  # type: ignore[arg-type]
            cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(
                OutputEvent(kind=TEXT_CHUNK, text=f"{near_limit}\n\n[OPTIONS: {trailer}]")
            )
            await r.dispatch(OutputEvent(kind=STEER_CONSUMED, text="steered"))

        asyncio.run(_go())
        for text, _ in cli.sent:
            assert len(text) <= TELEGRAM_CHUNK_LIMIT
        for _, text, _ in cli.edits:
            assert len(text) <= TELEGRAM_CHUNK_LIMIT

    def test_keyboard_caps_at_declared_and_overflow_is_visible(self) -> None:
        from test_telegram import FakeClient

        from kiro_crew.messaging.renderer import DONE, TEXT_CHUNK, OutputEvent
        from kiro_crew.telegram.renderer import TelegramRenderer
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        n = TELEGRAM_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice {i}" for i in range(1, n + 4))
        cli = FakeClient()
        r = TelegramRenderer(  # type: ignore[arg-type]
            cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text=f"Pick.\n\n[OPTIONS: {trailer}]"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        kb = cli.final_markup()
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        assert len(labels) == n, "telegram keyboard was uncapped before enforcement"
        assert labels == [f"Choice {i}" for i in range(1, n + 1)]
        final = cli.final_text()
        assert f"{n + 1}. Choice {n + 1}" in final
        assert f"{n + 3}. Choice {n + 3}" in final


class TestDiscordEnforcement:
    def test_buttons_cap_at_declared_and_overflow_is_visible(self) -> None:
        import pytest_asyncio  # noqa: F401  (asyncio runner parity with test_discord)
        from test_discord import FakeClient

        from kiro_crew.discord.renderer import DiscordRenderer
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES

        n = DISCORD_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice {i}" for i in range(1, n + 4))
        cli = FakeClient()
        r = DiscordRenderer(  # type: ignore[arg-type]
            cli, "chan1", DISCORD_CAPABILITIES, session_key="discord:u:c"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(f"Pick.\n\n[OPTIONS: {trailer}]")
            await r.on_done()

        asyncio.run(_go())
        comps = cli.final_components()
        labels = [b["label"] for row in comps for b in row["components"]]
        assert len(labels) == n
        final = cli.final_text()
        assert f"{n + 1}. Choice {n + 1}" in final
        assert f"{n + 3}. Choice {n + 3}" in final

    def test_overflow_credential_is_redacted_on_the_real_render_path(self) -> None:
        """End-to-end: discord has no display-state pass of its own.

        Before enforcement the 26th+ choices were dropped entirely, so there
        was no exposure; routing them into the parsed body is what opened the
        surface this closes.
        """
        import pytest_asyncio  # noqa: F401  (asyncio runner parity with test_discord)
        from test_discord import FakeClient

        from kiro_crew.discord.renderer import DiscordRenderer
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES

        n = DISCORD_CAPABILITIES.max_buttons
        leaked = "AKIA`" + "`IOSFODNN7EXAMPLE"
        choices = [f"Choice {i}" for i in range(1, n + 1)] + [f"Retry with {leaked}"]
        cli = FakeClient()
        r = DiscordRenderer(  # type: ignore[arg-type]
            cli, "chan1", DISCORD_CAPABILITIES, session_key="discord:u:c"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("Pick.\n\n[OPTIONS: " + " | ".join(choices) + "]")
            await r.on_done()

        asyncio.run(_go())
        assert "IOSFODNN7EXAMPLE" not in cli.final_text()


class TestTeamsEnforcement:
    """Teams renders choices as Adaptive Card ``Action.Submit`` actions.

    The cap matters more here than the widget count suggests: a chip's label is
    later resolved from what the renderer recorded, so a choice that was capped
    out of the card must reach the user as text or it is unreachable entirely.
    """

    def test_chips_cap_at_declared_and_overflow_is_visible(self) -> None:
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        n = TEAMS_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice {i}" for i in range(1, n + 4))

        class _Client:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.cards: list[dict] = []

            async def send_message(self, conversation_id, content, service_url):
                self.sent.append(content)
                return f"mid-{len(self.sent)}"

            async def send_card(self, conversation_id, card, service_url):
                self.cards.append(card)
                return f"card-{len(self.cards)}"

            async def update_message(self, conversation_id, activity_id, content, service_url):
                return True

            async def send_typing(self, conversation_id, service_url) -> None:
                return None

        cli = _Client()
        r = TeamsRenderer(cli, "CONV", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(f"Pick.\n\n[OPTIONS: {trailer}]")
            await r.on_done()

        asyncio.run(_go())

        labels = [a["title"] for a in cli.cards[-1]["content"]["actions"]]
        assert len(labels) == n, "the card must carry exactly max_buttons chips"
        body = "\n".join(cli.sent)
        # Overflow continues the SAME numbering the widget slots started, so the
        # user can answer an un-chipped choice by typing it.
        assert f"{n + 1}. Choice {n + 1}" in body
        assert f"{n + 3}. Choice {n + 3}" in body

    def test_an_overflow_credential_is_redacted_in_the_body(self) -> None:
        """Overflow lands in the message body, which Teams markdown-renders."""
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        n = TEAMS_CAPABILITIES.max_buttons
        choices = [f"Choice {i}" for i in range(1, n + 1)] + ["AKIAIOSFODNN7EXAMPLE"]
        trailer = " | ".join(choices)

        class _Client:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.cards: list[dict] = []

            async def send_message(self, conversation_id, content, service_url):
                self.sent.append(content)
                return f"mid-{len(self.sent)}"

            async def send_card(self, conversation_id, card, service_url):
                self.cards.append(card)
                return f"card-{len(self.cards)}"

            async def update_message(self, conversation_id, activity_id, content, service_url):
                return True

            async def send_typing(self, conversation_id, service_url) -> None:
                return None

        cli = _Client()
        r = TeamsRenderer(cli, "CONV", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)

        async def _go() -> None:
            await r.on_text_chunk(f"Pick.\n\n[OPTIONS: {trailer}]")
            await r.on_done()

        asyncio.run(_go())

        assert "AKIAIOSFODNN7EXAMPLE" not in "\n".join(cli.sent)


class TestKeptChoicesAreDisplayRedacted:
    """A widget label is LLM-authored text rendered into a channel.

    ``apply_options_cap`` redacted only the OVERFLOW list, so the same string was
    sanitized when it landed as numbered text and intact when it landed on a button
    — and again in the press echo, which quotes the label back. On a forum Topic
    that is every allow-listed participant. Slack redacts at this same point; doing
    it in the shared helper closes it for every widget channel at once, so a channel
    added later cannot miss it.
    """

    @staticmethod
    def _caps(max_buttons: int = 5):
        from kiro_crew.messaging.transport import TransportCapabilities

        return TransportCapabilities(max_buttons=max_buttons)

    def test_a_credential_in_a_kept_label_is_redacted(self) -> None:
        from kiro_crew.messaging.renderer import apply_options_cap

        key = "AKIA" + "IOSFODNN7EXAMPLE"
        _body, kept = apply_options_cap("pick one", [key, "plain"], self._caps())
        assert key not in kept[0], "a credential must not reach a button label"
        assert "REDACTED" in kept[0]
        assert kept[1] == "plain", "an innocuous label must survive untouched"

    def test_a_markup_split_credential_is_caught_on_the_canonical_form(self) -> None:
        # The byte-level pass alone misses this: the contiguous key only exists once
        # the markup is flattened, which is exactly what display_safe does first.
        from kiro_crew.messaging.renderer import apply_options_cap

        head, tail = "AKIA", "IOSFODNN7EXAMPLE"
        _body, kept = apply_options_cap("pick", [f"{head}**{tail}**"], self._caps())
        assert "REDACTED" in kept[0]
        assert head + tail not in kept[0]
        assert tail not in kept[0], "the second half must not survive either"

    def test_mentions_in_a_kept_label_are_defanged(self) -> None:
        # Labels render as plain text, but the press echo puts the label back into a
        # message body — where the platform DOES parse mentions.
        from kiro_crew.messaging.renderer import apply_options_cap

        _body, kept = apply_options_cap("pick", ["@everyone"], self._caps())
        assert kept[0] != "@everyone"
        assert "​" in kept[0], "the mention must be broken with a ZWSP"

    def test_the_overflow_list_is_still_redacted_too(self) -> None:
        # The half that already worked, so a regression cannot trade one for the other.
        from kiro_crew.messaging.renderer import apply_options_cap

        key = "AKIA" + "IOSFODNN7EXAMPLE"
        body, kept = apply_options_cap("pick", ["a", "b", key], self._caps(max_buttons=2))
        assert len(kept) == 2
        assert key not in body and "REDACTED" in body

    def test_a_zero_widget_channel_takes_the_all_overflow_path(self) -> None:
        # `max_buttons <= 0` has no branch of its own: it keeps nothing and overflows
        # everything, so a button-less channel gets every choice as a numbered line
        # through the same sanitising sink rather than losing the answers to a
        # question the agent just asked.
        from kiro_crew.messaging.renderer import apply_options_cap

        key = "AKIA" + "IOSFODNN7EXAMPLE"
        body, kept = apply_options_cap("pick", [key], self._caps(max_buttons=0))
        assert kept == [], "a zero-widget channel keeps no choice for a widget"
        assert "1. " in body, "the choice must still reach the user as text"
        assert key not in body and "REDACTED" in body


class TestWebexEnforcement:
    def test_card_actions_cap_at_declared_and_overflow_is_visible(self) -> None:
        """Drive the REAL render path, not the helper.

        The ratchet exists because a renderer can call the shared cap and then
        build its widget from the uncapped list, so the only assertion worth
        making is against what the client was actually asked to send.
        """
        from test_webex_renderer import FakeClient

        from kiro_crew.messaging.renderer import DONE, TEXT_CHUNK, OutputEvent
        from kiro_crew.webex.renderer import WebexRenderer
        from kiro_crew.webex.transport import WEBEX_CAPABILITIES

        n = WEBEX_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice {i}" for i in range(1, n + 4))
        cli = FakeClient()
        # A card is rendered only when its press can be resolved, so the real path
        # needs the dispatcher's choice store -- the card is the last thing a turn
        # sends, and a renderer-owned map is gone before any press arrives.
        r = WebexRenderer(
            cli,
            "ROOM",
            WEBEX_CAPABILITIES,  # type: ignore[arg-type]
            publish_choices=lambda _nonce, _choices: None,
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text=f"Pick.\n\n[OPTIONS: {trailer}]"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())

        card = next(kw for (_, _, kw) in cli.sent_full if kw.get("attachments"))
        actions = card["attachments"][0]["content"]["actions"]
        labels = [a["title"] for a in actions]
        assert len(labels) == n, "webex card actions were uncapped"
        assert labels == [f"Choice {i}" for i in range(1, n + 1)]
        # Overflow is numbered CONTINUING the widget slots, never dropped.
        final = cli.edits[-1][2]
        assert f"{n + 1}. Choice {n + 1}" in final
        assert f"{n + 3}. Choice {n + 3}" in final

    def test_an_email_choice_is_not_defanged_but_a_credential_still_goes(self) -> None:
        """Both halves of what ``mention_grammars=False`` claims, on the real path.

        Webex parses no broadcast grammar and its allow-list IS email addresses, so
        a ZWSP after every ``@`` makes an offered address uncopyable — the exact cost
        the capability was added to avoid. Redaction is NOT capability-gated, so the
        credential half must survive the same call: this pins that the declaration
        relaxes the defang alone, on the widget label and the numbered overflow
        together, since a fix applied to only one is how the two drifted before.
        """
        from test_webex_renderer import FakeClient

        from kiro_crew.messaging.renderer import DONE, TEXT_CHUNK, OutputEvent
        from kiro_crew.webex.renderer import WebexRenderer
        from kiro_crew.webex.transport import WEBEX_CAPABILITIES

        n = WEBEX_CAPABILITIES.max_buttons
        # One address on a kept (widget) choice and one past the cap, so the
        # assertion covers the numbered-overflow sink too.
        choices = [
            "ask kyle@example.com",
            *[f"Choice {i}" for i in range(n)],
            "AKIAIOSFODNN7EXAMPLE",
        ]
        cli = FakeClient()
        r = WebexRenderer(
            cli,
            "ROOM",
            WEBEX_CAPABILITIES,  # type: ignore[arg-type]
            publish_choices=lambda _nonce, _choices: None,
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(
                OutputEvent(kind=TEXT_CHUNK, text=f"Pick.\n\n[OPTIONS: {' | '.join(choices)}]")
            )
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())

        card = next(kw for (_, _, kw) in cli.sent_full if kw.get("attachments"))
        labels = [a["title"] for a in card["attachments"][0]["content"]["actions"]]
        final = cli.edits[-1][2]
        assert "ask kyle@example.com" in labels, f"the address was defanged: {labels}"
        assert "​" not in "".join(labels)
        assert "​" not in final
        # The credential rides the OVERFLOW half, past the widget cap.
        assert "AKIAIOSFODNN7EXAMPLE" not in final
