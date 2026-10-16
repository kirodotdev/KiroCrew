"""``pr_status.py``: the issue-link advisory, its closing-trailer grammar and the issue
declaration grammar.
"""

from __future__ import annotations

from types import ModuleType

from prepare_pr_status_helpers import ROOT, _install_fake_gh, _load_script, _pr_payload

# --- issue-link advisory (closing keyword) ------------------------------------
#
# The advisory exists because finished work merged with only "Related: #n" left
# the issue open forever, with nothing downstream to reconcile it. The host's own
# closingIssuesReferences resolution is the truth; the body regexes only
# classify WHY it resolved to nothing, so the operator is told which of the
# three mistakes they made.


def test_resolved_closing_reference_silences_the_notice() -> None:
    module = _load_script()
    assert module.closing_link_reason("Fixes #7", [{"number": 7}]) is None


def _assert_host_closure_is_unconfirmed(module: ModuleType, body: str, number: int = 7) -> None:
    reason = module.closing_link_reason(body, [{"number": number}])
    assert reason is not None
    assert "no explicit closing trailer" in reason
    assert "#{}".format(number) in reason


def test_backtick_fenced_trailer_does_not_confirm_host_closure() -> None:
    module = _load_script()
    body = "```markdown\nFixes #7\n```\nVisible prose accidentally fixes #7."
    _assert_host_closure_is_unconfirmed(module, body)


def test_tilde_fenced_trailer_does_not_confirm_host_closure() -> None:
    module = _load_script()
    body = "~~~markdown\nFixes #7\n~~~\nVisible prose accidentally fixes #7."
    _assert_host_closure_is_unconfirmed(module, body)


def test_indented_variable_length_fences_mask_their_contents() -> None:
    module = _load_script()
    bodies = (
        "   ````markdown\nFixes #7\n```\n   `````\nVisible prose fixes #7.",
        "  ~~~~~text\nFixes #7\n  ~~~~~~\nVisible prose fixes #7.",
    )

    for body in bodies:
        _assert_host_closure_is_unconfirmed(module, body)


def test_crlf_fenced_trailer_does_not_confirm_host_closure() -> None:
    module = _load_script()
    body = "```markdown\r\nFixes #7\r\n```\r\nVisible prose fixes #7."
    _assert_host_closure_is_unconfirmed(module, body)


def test_multiline_html_commented_trailer_does_not_confirm_host_closure() -> None:
    module = _load_script()
    body = "<!-- example\nFixes #7\n-->\nVisible prose accidentally fixes #7."
    _assert_host_closure_is_unconfirmed(module, body)


def test_single_line_html_commented_trailer_does_not_confirm_host_closure() -> None:
    module = _load_script()
    body = "<!-- Fixes #7 -->\nVisible prose accidentally fixes #7."
    _assert_host_closure_is_unconfirmed(module, body)


def test_visible_trailer_after_fence_still_confirms_host_closure() -> None:
    module = _load_script()
    body = "```markdown\nFixes #99\n```\nFixes #7"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_visible_trailer_with_trailing_html_comment_still_confirms() -> None:
    module = _load_script()
    body = "Fixes #7 <!-- this explanation is not part of the trailer -->"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_fence_markers_inside_html_comment_do_not_hide_visible_trailer() -> None:
    module = _load_script()
    body = "<!--\n```markdown\nFixes #99\n```\n-->\nFixes #7"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_html_comment_markers_inside_fence_do_not_hide_visible_trailer() -> None:
    module = _load_script()
    body = "```markdown\n<!--\nFixes #99\n-->\n```\nFixes #7"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_each_host_closure_requires_a_visible_matching_trailer() -> None:
    module = _load_script()
    body = (
        "Fixes #7\n"
        "```markdown\n"
        "Fixes #3257\n"
        "```\n"
        "Visible prose accidentally fixes #3257."
    )
    reason = module.closing_link_reason(body, [{"number": 7}, {"number": 3257}])
    assert reason is not None
    assert "#3257" in reason
    assert "#7" not in reason


def test_hidden_issue_examples_do_not_trigger_specific_no_host_warning() -> None:
    module = _load_script()
    bodies = (
        "```markdown\nFixes #7\n```",
        "<!-- Fixes #7 -->",
        "The literal example is `Fixes #7`.",
        "```markdown\n#7\n```",
    )

    for body in bodies:
        reason = module.closing_link_reason(body, [])
        assert reason is not None
        assert "no issue link" in reason


def test_multiline_inline_code_trailer_does_not_confirm_host_closure() -> None:
    module = _load_script()
    body = "`\nFixes #7\n`\nVisible prose accidentally fixes #7."
    _assert_host_closure_is_unconfirmed(module, body)


def test_comment_like_fence_info_does_not_hide_visible_trailer() -> None:
    module = _load_script()
    body = "```text <!-- example\nFixes #99\n```\nFixes #7"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_empty_or_null_body_stays_advisory() -> None:
    module = _load_script()
    for body in (None, ""):
        reason = module.closing_link_reason(body, [])
        assert reason is not None
        assert "no issue link" in reason


def test_unterminated_fence_masks_through_end_of_body() -> None:
    module = _load_script()
    body = "```markdown\nFixes #7"
    _assert_host_closure_is_unconfirmed(module, body)


def test_visible_prose_mask_preserves_offsets_and_line_boundaries() -> None:
    module = _load_script()
    body = "before\r\n```markdown\r\nFixes #7\r\n```\r\nFixes #8"
    masked = module._visible_markdown_prose(body)

    assert len(masked) == len(body)
    assert [i for i, char in enumerate(masked) if char == "\n"] == [
        i for i, char in enumerate(body) if char == "\n"
    ]
    assert "Fixes #7" not in masked
    assert masked.endswith("Fixes #8")


def test_oversized_explicit_trailer_degrades_to_advisory() -> None:
    module = _load_script()
    runtime = __import__("sys")
    get_digit_limit = getattr(runtime, "get_int_max_str_digits", None)
    previous_digit_limit = get_digit_limit() if get_digit_limit is not None else None

    # Python 3.10 has no integer-string digit limit. Disable the 3.11+ limit
    # while exercising this path so the regression cannot pass merely because
    # an interpreter-level ValueError happens to protect the parser.
    if previous_digit_limit is not None:
        runtime.set_int_max_str_digits(0)
    try:
        oversized_number = "9" * 5000
        body = "Fixes #7\nFixes #{}".format(oversized_number)
        reason = module.closing_link_reason(body, [{"number": 7}])
    finally:
        if previous_digit_limit is not None:
            runtime.set_int_max_str_digits(previous_digit_limit)

    assert reason is not None
    assert "malformed explicit closing trailer" in reason


def test_malformed_host_issue_numbers_stay_unconfirmed() -> None:
    module = _load_script()
    malformed_numbers = (None, "not-a-number", " 7 ", 7.5, True, [], {})

    for malformed_number in malformed_numbers:
        reason = module.closing_link_reason(
            "Fixes #7",
            [{"number": malformed_number}],
        )
        assert reason is not None, repr(malformed_number)


def test_bare_reference_without_a_verb_is_reported() -> None:
    """A bare reference with no closing verb is reported and closes nothing.

    Reported, not blocked -- the author decides.
    """
    module = _load_script()
    reason = module.closing_link_reason("Related: #2368, #2375 for context", [])
    assert reason is not None
    assert "no closing keyword" in reason


def test_verb_present_but_host_resolved_nothing_is_reported_distinctly() -> None:
    module = _load_script()
    reason = module.closing_link_reason("Fixes #999999", [])
    assert reason is not None
    assert "resolved no issue" in reason
    # Must NOT be reported as the missing-verb case; the operator needs to know
    # the verb is fine and the NUMBER is the problem.
    assert "no closing keyword" not in reason


# --- explicit closing-trailer grammar ----------------------------------------
#
# A trailer must occupy the WHOLE visible line, and the accepted targets are
# same-repo `#123`, qualified `owner/repo#123`, and a full issue URL. Each
# accepted form gets a positive case AND its opposite-failure twin, because the
# two mistakes this classifier can make are symmetric and both mislead: calling
# prose a trailer tells the author to fix a number that is fine, and refusing a
# qualified trailer tells them to add a keyword they already wrote.


def test_prose_mentioning_a_past_close_is_not_a_trailer() -> None:
    """Prose mentioning a past close is not a trailer.

    ``Fixed #123 in an earlier release`` is a sentence, not a declaration. It
    must be reported as the missing-verb (bare-reference) case, never as
    "the keyword is fine, your number is wrong".
    """
    module = _load_script()
    prose = "Fixed #123 in an earlier release; this PR only adds tests."
    assert module._CLOSING_KW_RE.search(prose) is None
    reason = module.closing_link_reason(prose, [])
    assert reason is not None
    assert "no closing keyword" in reason
    assert "resolved no issue" not in reason
    # Same shape mid-paragraph, and with the verb not at the start of the line.
    for line in (
        "This closes #7 only partially, so the issue stays open.",
        "See the note above: resolves #7 was already done upstream.",
    ):
        assert module._CLOSING_KW_RE.search(line) is None, line


def test_whole_line_trailer_forms_are_accepted() -> None:
    """Everything that is still a trailer despite decoration.

    Trailing whitespace, one sentence-ending punctuation mark, a CR from a CRLF
    body, a list bullet, an indented line, a trailing HTML comment, and several
    references on one line all leave the line a declaration.
    """
    module = _load_script()
    accepted = (
        "Fixes #123",
        "fixes: #123",
        "Closed #123.",
        "Resolves #123   ",
        "Fixes #123\r",
        "- Fixes #123",
        "  Fixes #123",
        "Fixes #123 <!-- tracked -->",
        "Fixes #123, closes #124",
        "Fixes #123 and resolves #124",
        "Body prose.\n\nFixes #123\n",
    )
    for body in accepted:
        assert module._CLOSING_KW_RE.search(body) is not None, body
        reason = module.closing_link_reason(body, [])
        assert reason is not None and "resolved no issue" in reason, body


def test_qualified_and_url_targets_are_recognised_as_trailers() -> None:
    """GitHub resolves cross-repo and URL targets, so we must not call them
    verb-less. The classifier is only reached when the host resolved nothing,
    so accepting them needs no reconciliation against this repo's identity --
    "the verb is fine, check the reference" is true either way.
    """
    module = _load_script()
    for body in (
        "Fixes owner/repo#123",
        "Closes my-org/my.repo#123",
        "Resolves https://github.com/owner/repo/issues/123",
        "Fixes https://github.example.com/owner/repo/issues/123",
    ):
        assert module._CLOSING_KW_RE.search(body) is not None, body
        reason = module.closing_link_reason(body, [])
        assert reason is not None, body
        assert "resolved no issue" in reason, body
        assert "no closing keyword" not in reason, body


def test_qualified_reference_without_a_verb_is_the_missing_keyword_case() -> None:
    """The opposite-failure twin: a qualified ref or issue URL with no verb is
    an issue reference, so it must report the missing keyword rather than
    "no issue link at all"."""
    module = _load_script()
    for body in (
        "Related: owner/repo#123",
        "Context: https://github.com/owner/repo/issues/123",
    ):
        reason = module.closing_link_reason(body, [])
        assert reason is not None, body
        assert "no closing keyword" in reason, body


def test_malformed_targets_are_not_trailers() -> None:
    """Opposite-failure cases for the target grammar: no number, no verb,
    a non-closing verb, and a pull-request URL are all rejected."""
    module = _load_script()
    for body in (
        "Fixes #",
        "Fixes issue 123",
        "Fixes#123",
        "Addresses #123",
        "Part of #123",
        "Fixes https://github.com/owner/repo/pull/123",
        "Fixes owner#123",
    ):
        assert module._CLOSING_KW_RE.search(body) is None, body


def test_no_reference_at_all_is_reported_and_names_the_gate() -> None:
    module = _load_script()
    reason = module.closing_link_reason("A pure refactor with no tracked issue.", [])
    assert reason is not None
    assert "Issue Gate" in reason


def test_there_is_no_opt_out_line_that_silences_the_notice() -> None:
    """The Issue Gate lane rejects an issue-less PR, so this module must not
    accept a body line that the gate rejects."""
    module = _load_script()
    body = "A pure refactor.\n\nno linked issue: no ticket exists for this cleanup."
    assert module.closing_link_reason(body, []) is not None
    assert not hasattr(module, "_NO_ISSUE_RE")


def test_host_closure_without_an_explicit_trailer_is_reported() -> None:
    module = _load_script()
    body = "no issue closed: #3257 is resolved by the release, not this change."
    reason = module.closing_link_reason(body, [{"number": 3257}])
    assert reason is not None
    assert "no explicit closing trailer" in reason


def test_each_host_closure_requires_a_matching_explicit_trailer() -> None:
    module = _load_script()
    body = "Fixes #7\n\n" "no issue closed: #3257 is resolved by the release, not this change."
    reason = module.closing_link_reason(body, [{"number": 7}, {"number": 3257}])
    assert reason is not None
    assert "#3257" in reason
    assert "#7" not in reason

    explicit_body = "Fixes #7\nResolves: #3257"
    assert module.closing_link_reason(explicit_body, [{"number": 7}, {"number": 3257}]) is None


def test_same_number_in_different_repositories_stays_unconfirmed() -> None:
    module = _load_script()
    body = "Fixes #7\n\nThe release fixes other/repo#7, not this change."
    closing_refs = [
        {
            "number": 7,
            "repository": {"name": "repo", "owner": {"login": "example"}},
        },
        {
            "number": 7,
            "repository": {"name": "repo", "owner": {"login": "other"}},
        },
    ]
    reason = module.closing_link_reason(body, closing_refs)
    assert reason is not None
    # One unqualified `Fixes #7` covers ONE closure, so the second repository's
    # #7 -- named only in prose, never in a trailer -- is reported as undeclared.
    # Naming the unaccounted-for closure is both narrower and true than calling
    # the shape ambiguous.
    assert "no explicit closing trailer" in reason
    assert "#7" in reason


def test_two_qualified_trailers_for_one_number_do_not_trigger_a_notice() -> None:
    """Two qualified trailers for one number must not trigger a duplicate notice.

    Repository-aware matching accounts for this body fully --
    `Fixes #7` declares this repository's #7 and `Fixes other/repo#7` declares
    the other one, and the host resolves exactly those two. An advisory that
    fires on a correct body is how authors learn to ignore advisories, so it
    does not fire here: genuine ambiguity is covered by the undeclared-closure
    case.
    """
    module = _load_script()
    body = "Fixes #7\nFixes other/repo#7"
    refs = [_host_ref(7, "example"), _host_ref(7, "other")]
    assert module.closing_link_reason(body, refs, "example/repo") is None


def test_one_wildcard_trailer_cannot_vouch_for_two_repositories() -> None:
    """A bare `#<n>` with no known repository covers exactly ONE reference.

    The complement of the test above: without a caller-supplied repository the
    trailer is a wildcard, and one wildcard honestly accounts for one closure.
    The second is reported rather than silently absorbed.
    """
    module = _load_script()
    refs = [_host_ref(7, "example"), _host_ref(7, "other")]
    reason = module.closing_link_reason("Fixes #7", refs)
    assert reason is not None
    assert "no explicit closing trailer" in reason

    # Repeating the same unqualified trailer does NOT buy a second cover.
    # `Fixes #7` and `Closes #7` name the same issue in the same (unknown)
    # repository, so they are one declaration, not two -- writing the trailer
    # twice cannot account for a closure in a repository the body never names.
    repeated = module.closing_link_reason("Fixes #7\nCloses #7", refs)
    assert repeated is not None
    assert "no explicit closing trailer" in repeated

    # Naming the second repository explicitly is what accounts for it.
    assert module.closing_link_reason("Fixes #7\nCloses other/repo#7", refs, "example/repo") is None


def test_full_trailer_grammar_satisfies_the_host_closure_confirmation() -> None:
    """Any form the accept path calls a trailer must also COUNT as declared.

    One grammar governs both directions. If the confirmation path recognised a
    narrower set than ``_CLOSING_KW_RE`` accepts, every legitimate bulleted,
    qualified, URL or multi-reference trailer would be reported as a missing
    declaration -- an advisory that fires on correct bodies teaches authors to
    ignore advisories.
    """
    module = _load_script()
    for body in (
        "Fixes #7",
        "- Fixes #7",
        "  * Resolves: #7",
        "Closes example/repo#7",
        "Fixes https://github.com/example/repo/issues/7",
        "Fixes #7.",
        "Fixes #7 <!-- tracked -->",
    ):
        assert module.closing_link_reason(body, [{"number": 7}]) is None, body

    multi = "Fixes #7 and Closes example/repo#8"
    assert module.closing_link_reason(multi, [{"number": 7}, {"number": 8}]) is None, multi


def _host_ref(number: int, owner: str, name: str = "repo") -> dict:
    return {"number": number, "repository": {"name": name, "owner": {"login": owner}}}


def test_stale_qualified_trailer_does_not_vouch_for_a_local_closure() -> None:
    """A trailer for ANOTHER repository must not cover this repository's close.

    The expensive shape: the body carries a stale `Fixes other/repo#7` (which
    resolves to nothing — wrong or deleted issue) while separate prose forms a
    close-on-merge trigger for THIS repository's own #7. Matching on the bare
    number alone let the stale trailer vouch for the resolved closure, so the
    notice was suppressed and an unrelated issue closed on merge — precisely
    the failure this advisory exists to catch.
    """
    module = _load_script()
    body = "Fixes other/repo#7\n\nThis also fixes #7 in passing."
    reason = module.closing_link_reason(body, [_host_ref(7, "example")], "example/repo")
    assert reason is not None
    assert "no explicit closing trailer" in reason
    assert "#7" in reason


def test_qualified_trailer_covers_its_own_repository_closure() -> None:
    """The same tightening must not fire when the repositories AGREE."""
    module = _load_script()
    assert (
        module.closing_link_reason("Fixes other/repo#7", [_host_ref(7, "other")], "example/repo")
        is None
    )
    assert (
        module.closing_link_reason(
            "Fixes https://github.com/other/repo/issues/7",
            [_host_ref(7, "other")],
            "example/repo",
        )
        is None
    )


def test_unqualified_trailer_resolves_to_the_prs_own_repository() -> None:
    """A bare `#<n>` means THIS repository — it covers a local closure and not
    a foreign one."""
    module = _load_script()
    assert module.closing_link_reason("Fixes #7", [_host_ref(7, "example")], "example/repo") is None
    foreign = module.closing_link_reason("Fixes #7", [_host_ref(7, "other")], "example/repo")
    assert foreign is not None
    assert "no explicit closing trailer" in foreign


def test_repository_matching_is_case_insensitive() -> None:
    """GitHub owner/repo names are case-insensitive, so the match must be too —
    otherwise a correctly-cased trailer reads as a foreign repository."""
    module = _load_script()
    assert (
        module.closing_link_reason(
            "Fixes OTHER/Repo#7", [_host_ref(7, "other", "repo")], "Example/Repo"
        )
        is None
    )


def test_unknown_repository_on_either_side_stays_a_wildcard() -> None:
    """An unknown repository must not manufacture a notice on a correct body.

    A caller that passes no ``repo`` cannot know what a bare `#<n>` means, and a
    host payload with no ``repository`` object cannot be reconciled — both must
    keep matching, so the tightening only ever fires on a known disagreement.
    """
    module = _load_script()
    assert module.closing_link_reason("Fixes #7", [{"number": 7}]) is None
    assert module.closing_link_reason("Fixes other/repo#7", [{"number": 7}]) is None
    assert module.closing_link_reason("Fixes #7", [_host_ref(7, "other")], None) is None


def test_space_indented_example_does_not_confirm_host_closure() -> None:
    """A four-space-indented example is CODE — GitHub resolves nothing from it.

    Companion to the fenced cases: this is Markdown's other code block, and it
    is how a body written without fences shows an author what a trailer looks
    like. Crediting it as a declaration suppresses the unrelated-closure notice
    for a closure that came from somewhere else entirely.
    """
    module = _load_script()
    body = "Write the trailer like this:\n\n    Fixes #7\n\nThis also fixes #7."
    reason = module.closing_link_reason(body, [{"number": 7}])
    assert reason is not None
    assert "no explicit closing trailer" in reason


def test_tab_indented_example_does_not_confirm_host_closure() -> None:
    """A tab reaches the four-column stop, so one tab of indent is refused."""
    module = _load_script()
    body = "Example:\n\n\tFixes #7\n\nSeparately this fixes #7 in prose."
    reason = module.closing_link_reason(body, [{"number": 7}])
    assert reason is not None
    assert "no explicit closing trailer" in reason


def test_indented_example_spanning_a_blank_line_is_still_refused() -> None:
    """A blank line inside an indented example changes nothing.

    Under the old block-state approach this pinned "interior blank lines do not
    end the block". The cap makes that question irrelevant: the trailer's own
    indentation is what disqualifies it, so no surrounding state has to be
    modelled correctly for this body to be safe.
    """
    module = _load_script()
    body = "Example:\n\n    first\n\n    Fixes #7\n\nAnd this fixes #7."
    reason = module.closing_link_reason(body, [{"number": 7}])
    assert reason is not None
    assert "no explicit closing trailer" in reason


def test_visible_trailer_after_an_indented_example_still_confirms() -> None:
    """A real trailer that merely FOLLOWS an indented example is still credited --
    the cap disqualifies the indented line, not everything after it."""
    module = _load_script()
    body = "Example:\n\n    Fixes #999\n\nFixes #7"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_a_code_indented_trailer_is_never_a_declaration() -> None:
    """Four columns of indent is refused REGARDLESS of what precedes it.

    An earlier revision credited this, on the reasoning that a line continuing an
    open paragraph is lazy continuation which GitHub does resolve. Keeping that
    carve-out required knowing whether a paragraph was open, and that question is
    a Markdown parser's job — the approximation was wrong for every block type
    that closes itself (see the sibling test). The bound replaces the state: a
    trailer at four or more columns is not a declaration, full stop.

    The cost is this body not being credited, which prints an advisory
    notice on an odd shape. The benefit is that no block type can smuggle an
    EXAMPLE through as a declaration, which silently suppresses a real warning.
    """
    module = _load_script()
    reason = module.closing_link_reason("Some sentence\n    Fixes #7", [{"number": 7}])
    assert reason is not None
    assert "no explicit closing trailer" in reason

    # A tab reaches the same four-column stop, so it is refused identically.
    tabbed = module.closing_link_reason("Some sentence\n\tFixes #7", [{"number": 7}])
    assert tabbed is not None


def test_self_closing_blocks_cannot_smuggle_an_indented_example() -> None:
    """The four block types that leaked while paragraph state was tracked.

    An ATX heading, a blockquote, a thematic break and a setext underline all
    CLOSE their block, so the indented line after them is code — but a tracker
    that only asked "was the previous line non-blank?" judged each one an open
    paragraph and let the example through as a declaration. Each was found by
    probing the shipped function, not by review, which is why they are pinned
    together: they are one defect, not four.
    """
    module = _load_script()
    for label, body in (
        ("atx heading", "## Example\n    Fixes #7\n\nSeparately this fixes #7."),
        ("blockquote", "> Example\n    Fixes #7\n\nSeparately this fixes #7."),
        ("thematic break", "---\n    Fixes #7\n\nSeparately this fixes #7."),
        ("setext", "Example\n=======\n    Fixes #7\n\nSeparately this fixes #7."),
    ):
        reason = module.closing_link_reason(body, [{"number": 7}])
        assert reason is not None, label
        assert "no explicit closing trailer" in reason, label


def test_trailer_indent_up_to_three_columns_is_still_accepted() -> None:
    """The cap is at FOUR — three columns is still prose, and a bulleted trailer
    indented under the cap must keep working."""
    module = _load_script()
    for body in ("Fixes #7", " Fixes #7", "   Fixes #7", "   - Fixes #7"):
        assert module.closing_link_reason(body, [{"number": 7}]) is None, body


def test_list_nested_fenced_example_does_not_confirm_host_closure() -> None:
    """CommonMark measures fence indent RELATIVE to the container, so a fence
    inside a list item legitimately sits four or more columns in."""
    module = _load_script()
    body = (
        "- Example:\n"
        "\n"
        "      ```\n"
        "      Fixes #7\n"
        "      ```\n"
        "\n"
        "Separately this fixes #7 in prose."
    )
    reason = module.closing_link_reason(body, [{"number": 7}])
    assert reason is not None
    assert "no explicit closing trailer" in reason


def test_nested_fence_closes_at_its_own_indent() -> None:
    """A nested block must END at its own closing fence — otherwise the mask
    runs to the end of the body and swallows a real trailer after it."""
    module = _load_script()
    body = "- Example:\n" "\n" "      ```\n" "      Fixes #999\n" "      ```\n" "\n" "Fixes #7"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_fence_closer_indent_is_capped_at_three_columns() -> None:
    """A closer indented four columns is not a closer -- CommonMark's own bound,
    and the same four-column line the trailer cap draws."""
    module = _load_script()
    assert module._is_closing_fence("```", "`", 3) is True
    assert module._is_closing_fence("   ```", "`", 3) is True
    assert module._is_closing_fence("    ```", "`", 3) is False


def test_list_nested_tilde_fence_example_is_refused() -> None:
    """A tilde fence nested in a list, with no blank line before it.

    Worth pinning separately because it defeats every mechanism EXCEPT the cap.
    The fence sits past the three-column fence bound so it is not recognised as
    a fence; a tilde run has no `_mask_inline_code` equivalent (the backtick
    version of this body is masked by backtick pairing, which is why it is not
    the interesting case); and modelling it as code would need the list's own
    content column. The trailer's indentation settles it without any of that.
    """
    module = _load_script()
    body = (
        "- Example:\n"
        "      ~~~\n"
        "      Fixes #7\n"
        "      ~~~\n"
        "\n"
        "Separately this fixes #7 in prose."
    )
    reason = module.closing_link_reason(body, [{"number": 7}])
    assert reason is not None
    assert "no explicit closing trailer" in reason


def test_an_unterminated_nested_fence_does_not_swallow_a_real_trailer() -> None:
    """Guards the false-positive an earlier revision introduced.

    Recognising fences at ANY indent meant an unterminated indented fence-looking
    line masked the rest of the body, so a genuine column-0 `Fixes #7` after it
    stopped being credited and the notice fired on a correct body. Keeping the
    three-column fence bound is what prevents that; the trailer cap covers the
    example case the widening was reaching for, so nothing is lost.
    """
    module = _load_script()
    body = "- Example:\n\n      ~~~\n      stuff\n\nFixes #7"
    assert module.closing_link_reason(body, [{"number": 7}]) is None


def test_shipped_body_template_does_not_read_as_a_declaration() -> None:
    """An author who copies the template and skips the Issue link section must
    still see the notice -- the leftover instruction text must not read as a
    declaration.

    This runs the real regexes against the repo's PR template (the single
    source of truth), so the template and the check cannot drift back into
    agreeing. The template contains no column-0 opt-out declaration and no
    closing keyword that the host would resolve, so `closing_link_reason`
    must return a non-None advisory reason.
    """
    module = _load_script()
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    reason = module.closing_link_reason(template, [])
    assert reason is not None, "unfilled template reads as an issue-link declaration"


def test_markdown_headings_are_not_mistaken_for_issue_references() -> None:
    """`# Problem` must not read as a bare `#n` ref, or every PR reports the
    wrong reason."""
    module = _load_script()
    reason = module.closing_link_reason("# Problem\n\n## Why it matters\n", [])
    assert reason is not None
    assert "no issue link" in reason


def test_missing_body_is_treated_as_no_link_not_a_crash() -> None:
    module = _load_script()
    assert module.closing_link_reason(None, []) is not None


def test_gh_query_requests_the_issue_link_fields() -> None:
    """The fake gh injects a payload directly, so no other test would notice the
    real ``--json`` field list dropping these two names -- the advisory would
    then always see an absent body and mis-report on every live PR."""
    module = _load_script()
    seen: list[str] = []

    def capture(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            seen.append(args[args.index("--json") + 1])
            return 1, "", "stop here"
        raise AssertionError("unexpected command: {}".format(args))

    module.run = capture
    module.main(["pr_status.py", "42"])
    assert seen, "gh pr view was never called"
    assert "body" in seen[0].split(","), seen[0]
    assert "closingIssuesReferences" in seen[0].split(","), seen[0]


def test_missing_issue_link_is_reported_but_does_not_block(capsys) -> None:
    """The advisory must be VISIBLE and must NOT change the verdict.

    Both halves matter. Printing without asserting CLEAN would let the check
    silently regain gate power; asserting CLEAN without reading the output
    would pass even if the notice were deleted.
    """
    module = _load_script()
    checks = [
        {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    _install_fake_gh(module, _pr_payload(checks, body="Related: #7", closingIssuesReferences=[]))
    assert module.main(["pr_status.py", "42"]) == 0
    out = capsys.readouterr().out
    assert "STATUS: CLEAN" in out, out
    assert "closes on merge: nothing" in out, out
    assert "NOTICE:" in out and "no closing keyword" in out, out


def test_resolved_issue_link_reports_the_number_and_no_notice(capsys) -> None:
    module = _load_script()
    checks = [
        {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    _install_fake_gh(
        module,
        _pr_payload(checks, body="Fixes #7", closingIssuesReferences=[{"number": 7}]),
    )
    assert module.main(["pr_status.py", "42"]) == 0
    out = capsys.readouterr().out
    assert "closes on merge: #7" in out, out
    assert "NOTICE:" not in out, out


# --- declared_issue_numbers: the one public declaration grammar -----------------
#
# `.github/scripts/issue_gate_refs.py` (the Issue Gate) reads bodies through
# this function, so the gate never re-derives masking or targets. It answers a
# different question from closing_link_reason (which issue is this work FOR,
# not why the host resolved no closure), hence its own line-start shape and
# the non-closing `Refs` / `Part of` verbs.


def test_declared_issue_numbers_line_start_reading_frees_the_rest_of_the_line() -> None:
    module = _load_script()
    body = (
        "Fixes #7 (the Windows half), closes #8\n- Scope:\n    - Closes #9\n"
        "> Closes #10\nDiscloses #11 prefixes #12\nThis PR Fixes #13 partially.\n"
    )
    assert module.declared_issue_numbers(body, "example/repo") == (["7", "8"], True)


def test_declared_issue_numbers_tail_references_need_a_word_start() -> None:
    module = _load_script()
    body = "Fixes #1; the crash is unresolved: #2 and prefixes #3\n"
    assert module.declared_issue_numbers(body, "example/repo") == (["1"], True)


def test_declared_issue_numbers_accepts_non_closing_declarations() -> None:
    module = _load_script()
    body = "Part of #3\n- Refs #4 and ref #5\nRelated to #6\n"
    assert module.declared_issue_numbers(body, "example/repo") == (["3", "4", "5"], True)


def test_declared_issue_numbers_masks_and_filters_like_the_notice_path() -> None:
    module = _load_script()
    body = (
        "<!-- Fixes #1 -->\n```\nCloses #2\n```\nsee `Closes #3`\n"
        "Closes other/repo#4\nFixes Example/Repo#5\nResolves #99999999999\n"
    )
    numbers, well_formed = module.declared_issue_numbers(body, "example/repo")
    assert numbers == ["5"]
    assert well_formed is False
