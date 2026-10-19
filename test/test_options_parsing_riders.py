"""What this grammar does NOT change about EXISTING ``[OPTIONS:]`` parsing.

Measured on the merge base and unchanged here: ``split_trailing_protocol_suffix``
detaches the LAST trailing marker only; the line form stays line-final, so a marker
sharing a line is not parsed; and the temper covers only declared heads, so a label
nesting the singular ``[OPTION:`` parses as it does on base. The one addition is the
action trailer, which the splitter consults beside the content trailer.
"""

import pytest

from kiro_crew.constants import (
    _MARKER_BODY_TEMPER,
    OPTION_ACTIONS_RE_TRAILER,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)


class TestOnlyALineFinalMarkerIsDispatched:
    SHARED = "[OPTIONS: a | b] [OPTIONS: c | d]"
    MID_PROSE = "Choose [OPTIONS: a | b][OPTIONS: c | d] to proceed."

    def test_a_marker_whose_line_continues_in_prose_is_not_dispatched(self):
        # A sibling terminator matches here, excising a mid-sentence marker and
        # raising its labels as live buttons.
        assert OPTIONS_RE_LINE.search(self.MID_PROSE) is None

    def test_the_trailing_marker_of_a_shared_line_is_the_one_found(self):
        # The leading one is passed through verbatim; the remedy is consumer-side
        # (select the last marker, strip them all), not in the line terminator.
        found = [m.group("labels") for m in OPTIONS_RE_LINE.finditer(self.SHARED)]
        assert found == [" c | d"]

    def test_positive_control_a_line_final_marker_still_parses(self):
        m = OPTIONS_RE_LINE.search("Pick one.\n[OPTIONS: a | b]")
        assert m is not None and m.group("labels") == " a | b"


class TestABodyRefusesOnlyTheHeadsThisGrammarDefines:
    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: see [OPTIONS: x] below | Skip]",
            "[OPTIONS: see [OPTION-ACTIONS: close=x] | Skip]",
        ],
    )
    def test_a_label_holding_a_head_is_refused_by_both_forms(self, text):
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_TRAILER.search(text) is None

    @pytest.mark.parametrize(
        "text,labels",
        [
            ("[OPTIONS: fix [x] logging | Skip]", " fix [x] logging | Skip"),
            ("[OPTIONS: check arr[0] | Skip]", " check arr[0] | Skip"),
            (
                "[OPTIONS: an OPTION: without a bracket | Skip]",
                " an OPTION: without a bracket | Skip",
            ),
        ],
    )
    def test_positive_control_ordinary_nesting_still_parses(self, text, labels):
        # The refusal is keyed on a bracket that BEGINS a head, never on the word
        # appearing in prose, so an ordinary nested pair is untouched.
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert match.group("labels") == labels

    def test_the_singular_head_is_not_one_of_them_and_still_parses(self):
        # Refusing it would change shipped [OPTIONS:] parsing.
        text = "[OPTIONS: see [OPTION: x] below | Skip]"
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert match.group("labels") == " see [OPTION: x] below | Skip"

    def test_a_refused_outer_marker_does_not_take_the_text_before_it(self):
        # The nested declared head is the located one, so only the complete inner
        # marker detaches and the refused outer text stays visible.
        visible, suffix = split_trailing_protocol_suffix(
            "prose\n[OPTIONS: see [OPTIONS: x] | Skip]"
        )
        assert visible == "prose\n[OPTIONS: see "
        assert suffix == "[OPTIONS: x] | Skip]"


class TestOnlyTheTrailingMarkerIsDetached:
    """Base takes the last marker only, and this change does not widen that.

    A reply holding two trailing markers needs something that EMITS an action
    marker, and nothing does yet, so detaching a run would be machinery with no
    caller. The values below are measured, not intended.
    """

    def test_an_earlier_marker_stays_in_the_visible_half(self):
        visible, suffix = split_trailing_protocol_suffix(
            "prose\n[OPTIONS: a | b]\n[OPTIONS: c | d]"
        )
        assert visible == "prose\n[OPTIONS: a | b]\n"
        assert suffix == "[OPTIONS: c | d]"

    def test_a_trailing_action_marker_is_the_one_detached(self):
        visible, suffix = split_trailing_protocol_suffix(
            "prose\n[OPTIONS: a | b]\n[OPTION-ACTIONS: close=Close]"
        )
        assert visible == "prose\n[OPTIONS: a | b]\n"
        assert suffix == "[OPTION-ACTIONS: close=Close]"

    def test_a_marker_sharing_its_line_with_prose_is_not_crossed(self):
        visible, suffix = split_trailing_protocol_suffix("prose [OPTIONS: a | b] [OPTIONS: c | d]")
        assert visible == "prose [OPTIONS: a | b] "
        assert suffix == "[OPTIONS: c | d]"

    def test_positive_control_a_lone_trailing_marker_is_detached(self):
        visible, suffix = split_trailing_protocol_suffix("prose\n[OPTIONS: a | b]")
        assert visible == "prose\n"
        assert suffix == "[OPTIONS: a | b]"


class TestTheActionHeadIsCaseSensitive:
    """One casing rule for both heads: ``OPTIONS_RE_*`` has always been sensitive.

    Nothing emits an action marker yet, so there is no case-variant emitter for a
    second rule to serve.
    """

    @pytest.mark.parametrize("head", ["Option-Actions:", "option-actions:", "OPTION-actions:"])
    def test_a_non_canonical_casing_is_not_an_action_marker(self, head):
        line = f"[{head} close=Stop]"
        assert OPTION_ACTIONS_RE_TRAILER.search(line) is None

    def test_positive_control_the_canonical_head_parses(self):
        line = "[OPTION-ACTIONS: close=Stop]"
        assert OPTION_ACTIONS_RE_TRAILER.search(line) is not None

    def test_the_temper_refuses_both_heads_in_one_spelling(self):
        for head in ("OPTIONS:", "OPTION-ACTIONS:"):
            assert head in _MARKER_BODY_TEMPER


class TestAnActionMarkerCarriesOneNonemptyCloseEntry:
    """On the content body, a body the action grammar has no meaning for is still
    detached as protocol, dropping the trailing text instead of leaving it as prose.
    """

    @pytest.mark.parametrize(
        "body",
        [
            " close=A | close=B",  # two entries
            " close=A , close=B",
            " open=A",  # a key no consumer implements
            " close=",  # empty label
            " close=   ",  # whitespace-only label, strips to an empty pill
            " close=\t",
            "",
        ],
    )
    def test_a_body_that_is_not_one_nonempty_close_entry_is_refused(self, body):
        line = f"[OPTION-ACTIONS:{body}]"
        assert OPTION_ACTIONS_RE_TRAILER.search(line) is None

    @pytest.mark.parametrize(
        "body",
        [" close=Stop", " close=  Close tab", " close=See [1] later", " close=[x]", "close=x"],
    )
    def test_positive_control_one_nonempty_close_entry_still_parses(self, body):
        line = f"[OPTION-ACTIONS:{body}]"
        match = OPTION_ACTIONS_RE_TRAILER.search(line)
        assert match is not None
        assert match.group("labels") == body

    def test_a_refused_body_is_left_as_prose_rather_than_detached(self):
        text = "prose\n[OPTION-ACTIONS: close=A | close=B]"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert suffix == ""
        assert visible == text

    @pytest.mark.parametrize(
        "tail",
        [
            "[OPTION-ACTIONS: open=Settings",
            "[OPTION-ACTIONS: close=A | close=B",
            "[OPTION-ACTIONS: closet=A",
        ],
    )
    def test_an_unfinished_body_that_can_never_complete_stays_visible(self, tail):
        # Detached as a streaming prefix it is DISCARDED at on_done, silently.
        text = "prose\n" + tail
        visible, suffix = split_trailing_protocol_suffix(text)
        assert suffix == ""
        assert visible == text

    @pytest.mark.parametrize(
        "tail",
        ["[OPTION-ACTIONS", "[OPTION-ACTIONS: clo", "[OPTION-ACTIONS: close=Sto"],
    )
    def test_positive_control_a_completable_prefix_is_still_held(self, tail):
        _, suffix = split_trailing_protocol_suffix("prose\n" + tail)
        assert suffix == tail

    @pytest.mark.parametrize("closer", ["]", "\u3011", "\uff3d", "\u3015"])
    def test_an_unpaired_closer_followed_by_prose_stays_visible(self, closer):
        # The closer already ended the marker, so the rest is prose a channel would drop.
        text = f"Answer.\n[OPTION-ACTIONS: close=Close{closer} This only closes the tab."
        assert split_trailing_protocol_suffix(text) == (text, "")

    @pytest.mark.parametrize(
        "tail",
        [
            # A mismatched pair cannot appear in a complete label, so this tail is
            # prose however it continues -- detaching it DROPS the rest of the reply.
            "[OPTION-ACTIONS: close=Open \u3010docs\uff3d before proceeding",
            "[OPTION-ACTIONS: close=Open \uff3bdocs\u3011 before proceeding",
            # Terminated by a closer with no label: it can never be one nonempty entry.
            "[OPTION-ACTIONS: close=   \u3011",
            "[OPTION-ACTIONS: close= \uff3d",
            "[OPTION-ACTIONS: close=\t\u3015",
            # A pair cannot span a newline in the complete body either.
            "[OPTION-ACTIONS: close=Open \u3010multi\nline\u3011 now",
            "[OPTION-ACTIONS: close=Open [multi\nline] now",
            # An unpaired closer becomes label content only if another closer follows
            # IMMEDIATELY, so any whitespace after it leaves nothing able to complete.
            "[OPTION-ACTIONS: close=Open \u3010docs\uff3d\n\n",
            "[OPTION-ACTIONS: close=Open \u3010docs\uff3d\n",
            "[OPTION-ACTIONS: close=Open \u3010docs\uff3d   ",
            "[OPTION-ACTIONS: close=Open \u3010docs\uff3d\t",
        ],
    )
    def test_a_tail_no_complete_label_could_hold_stays_visible(self, tail):
        text = "prose\n" + tail
        assert split_trailing_protocol_suffix(text) == (text, "")

    @pytest.mark.parametrize(
        "tail",
        [
            "[OPTION-ACTIONS: close=Open \u3010docs\u3011 now",
            "[OPTION-ACTIONS: close=Open \u3010docs",
            "[OPTION-ACTIONS: close=Line one\nline two",
            # Measured completable: + "\u3011]" yields labels " close=Open \u3010docs\uff3d\u3011".
            "[OPTION-ACTIONS: close=Open \u3010docs\uff3d",
            # No label yet, and no closer has arrived to rule one out.
            "[OPTION-ACTIONS: close=   ",
            # Whitespace after a PAIRED closer: a terminator can still arrive.
            "[OPTION-ACTIONS: close=Open \u3010docs\u3011\n\n",
        ],
    )
    def test_positive_control_a_paired_or_open_lookalike_is_still_held(self, tail):
        _, suffix = split_trailing_protocol_suffix("prose\n" + tail)
        assert suffix == tail

    def test_a_malformed_tail_before_a_control_tag_keeps_its_text_visible(self):
        # The tag peels first, so the malformed marker ends up whitespace-terminated.
        # Detaching it there would deliver only "Answer." on a send-once channel.
        text = "Answer.\n[OPTION-ACTIONS: close=Open \u3010docs\uff3d\n\n<!-- keep-visible -->"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible == "Answer.\n[OPTION-ACTIONS: close=Open \u3010docs\uff3d\n"
        assert suffix == "\n<!-- keep-visible -->"


class TestAReplyExplainingTheMarkerKeepsItsText:
    """The detach is end-anchored, so writing ABOUT the marker is not writing one.

    This is what keeps the grammar safe to land before its emitter: an agent
    answering a question about the feature loses nothing. Only a reply whose final
    line IS a well-formed marker is detached, which is the grammar's whole purpose.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "The [OPTION-ACTIONS: close=Stop] marker closes a tab with no LLM turn.",
            "Write `[OPTION-ACTIONS: close=Stop]` at the end of the reply.",
            "A close button is spelled [OPTION-ACTIONS: close=Stop], and labels are free text.",
            "To close the tab, emit [OPTION-ACTIONS: close=Stop] as the final line.",
            "Here is the grammar:\n\n    [OPTION-ACTIONS: close=Stop]\n\nThe label is free text.",
            "The head is `[OPTION-ACTIONS:` and the body is one `close=<label>` entry.",
        ],
    )
    def test_quoting_or_explaining_the_marker_keeps_the_whole_reply(self, text):
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_positive_control_a_reply_ending_in_the_marker_is_detached(self):
        visible, suffix = split_trailing_protocol_suffix(
            "You can close it like this:\n[OPTION-ACTIONS: close=Stop]"
        )
        assert visible == "You can close it like this:\n"
        assert suffix == "[OPTION-ACTIONS: close=Stop]"

    def test_many_unpaired_openers_stay_linear(self):
        tail = "[OPTION-ACTIONS: close=" + "\u3010a" * 20000
        _, suffix = split_trailing_protocol_suffix("prose\n" + tail)
        assert suffix == tail
