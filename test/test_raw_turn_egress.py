"""Structural guards for the byte-identical raw provider-text accumulators.

Two turn loops keep the provider's text raw until a boundary redacts it whole
(so a compressed pako fragment can be validated only once complete): the
dashboard's ``assistant_text`` in ``chat_runner._run_chat`` and Slack's
``accumulated`` in ``slack.handler.handle_message``. The dashboard keeps a third
raw buffer beside the first: ``_orch_plan_buf``, the never-reset whole-turn copy a
planning turn falls back to when ``assistant_text`` was reset at a tool boundary.
Every guard parses the owning function and refuses any NEW call that reads a raw
buffer unless it is one of the sanctioned host-aware egress helpers or classified
internal analysis.

THE FROZEN SETS BELOW ARE A SECURITY ALLOWLIST, NOT TEST FIXTURES. Before this
change every chunk was redacted as it arrived, so a buffer that reached a new
consumer carried at most a baseline-redacted copy; now it carries the provider's
bytes verbatim, and these sets are the only thing standing between a new call site
and an unredacted egress. Widening any of them is a security-review change
(``docs/system-specs/modules/security.md``, "Cross-chunk streaming redaction"):
an egress entry must redact the WHOLE text it is handed through the active
policy, and an analysis entry must either return nothing displayable or redact
what it returns -- pinned behaviourally, as
``test_plan_metadata_extraction_redacts_what_it_hands_out`` pins the one entry
whose output does leave the loop. A reshaper entry (below) is neither: its
result is still the raw text and is followed like an alias.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_title import _extract_and_redact_plan_metadata
from kiro_crew.slack import handler as slack_handler

_SANCTIONED_EGRESS = frozenset(
    {
        "_deliver_cross_surface_reply",
        "_flush_segment",
        "redact_with_findings_via_context",
        "redact_via_context",
    }
)
# Calls whose RESULT is not displayable text: a verdict, a tuple of verdicts, a
# scheduling decision, or (for the one extraction) text it has itself redacted.
_INTERNAL_ANALYSIS = frozenset(
    {
        "_extract_and_redact_plan_metadata",
        "_maybe_continue_after_compaction",
        "_maybe_continue_after_terminal_text",
        "_maybe_retry_unfinished_progress",
        "_maybe_schedule_auto_retry_after_text",
        "bool",
        "has_leaked_tool_call",
        "has_unfinished_progress_claim",
        "is_promise_only_terminal",
        "looks_like_plan",
        "should_continue_after_compaction",
        "should_notice_leaked_tool_call",
        "should_notice_mixed_turn_leak",
        "should_recover_promise_only",
        "validate_plan_format",
    }
)
# Calls whose RESULT is still the provider's text with something trimmed or
# stripped off (a notice, plan markers, control comments). A name bound to such a
# result is as raw as the buffer it came from, so the alias census follows it:
# ``x = strip_plan_markers(raw); egress(x)`` is reported exactly like
# ``x = raw; egress(x)``. A census that stopped at the call name would let a
# reshaped copy leave through an unclassified consumer.
_TEXT_RESHAPERS = frozenset(
    {
        "_answer_text_only",
        "_reflow_label_and_audit",
        "ensure_go_all_option",
        "reflow_glued_option_marker",
        "strip_control_comments",
        "strip_plan_markers",
    }
)

# Slack keeps ``accumulated`` raw only until one joined boundary rebinds it to the
# host-aware redaction; everything after reads the redacted value EXCEPT the raw
# aliases enumerated in ``_SLACK_RAW_ALIAS_CONSUMERS_AFTER_BOUNDARY``. Before that
# boundary the raw buffer may reach the live cursor edit (wrapped in the narrow
# pako seam ``redact_pako_via_context`` -- the baseline on ordinary text, the
# active policy on a pako link's decoded state, the same seam the live-wire
# StreamRedactor runs) plus three classified internal transforms: thinking-tag
# splitting, control-comment stripping, and comment-hold resolution. The latter's
# released bytes still flow through ``_append_stream`` and its StreamRedactor.
_SLACK_SANCTIONED_EGRESS = frozenset(
    {"redact_with_findings_via_context", "redact_pako_via_context"}
)
_SLACK_INTERNAL_ANALYSIS = frozenset({"_resolve_comment_hold", "strip_thinking_tags"})
_SLACK_TEXT_RESHAPERS = frozenset({"strip_control_comments"})
# ``_untrimmed`` -- the control-comment-stripped copy taken BEFORE the boundary --
# is the one raw alias that outlives it, on purpose: the options trailer is parsed
# from the untrimmed text so a quoted control tag keeps its indent. Its two readers
# are safe by a downstream redaction, not by this boundary: ``extract_options``
# returns a body the host-aware ``render_one_for_slack`` redacts and options that
# ``build_options_blocks`` redacts choice by choice; ``_resolve_comment_hold``
# releases bytes only into ``_append_stream``'s StreamRedactor. A new reader of
# ``_untrimmed`` after the boundary is therefore an unredacted egress until it is
# classified here with the redaction that covers it.
_SLACK_RAW_ALIAS_CONSUMERS_AFTER_BOUNDARY = frozenset({"_resolve_comment_hold", "extract_options"})

# The dashboard's third raw accumulator. ``_orch_plan_buf`` mirrors every raw
# ``event.text`` of a planning turn and is never reset, so a plan the model
# emitted BEFORE later tool calls survives the tool-boundary reset of
# ``assistant_text``. Nothing displays it: it feeds the plan-format detector and
# the metadata extraction that arms the plan gate, and that extraction redacts
# every title, goal and description it hands out (pinned behaviourally below).
# It has no egress of its own, so unlike ``assistant_text`` no sanctioned
# egress call is REQUIRED to consume it -- only the two analysis calls are.
_ORCH_PLAN_BUF_CONSUMERS = frozenset({"_extract_and_redact_plan_metadata", "validate_plan_format"})

# A synthetic turn loop for the detector's own regression test: one classified
# read, two direct egresses, one shielded egress, one egress through a method
# call on the raw name, and the alias shapes -- a plain rebind, a slice-and-
# concatenate rebind, an augmented rebind and a RESHAPED rebind reach an egress
# (flagged), while a name bound to an analysis call's RESULT and a name bound to
# a shielded read do not (an alias census that tainted those would flag every
# plan-format check).
_SYNTHETIC_RAW_EGRESS = """
async def _run_chat():
    _orch_plan_buf = ""
    validate_plan_format(_orch_plan_buf)
    slot.append("assistant", _orch_plan_buf, "msg msg-a")
    state.broadcast_ws("chat_chunk", {"content": redact_via_context(_orch_plan_buf)})
    await state.slack_client.post_message(chan, body=_orch_plan_buf.strip())
    mirror = _orch_plan_buf
    await state.slack_client.mirror_message(chan, mirror)
    tail = _orch_plan_buf[-500:] + "..."
    tail += "\\n"
    state.persist_tail(tail)
    trimmed = strip_plan_markers(_orch_plan_buf)
    state.persist_trimmed(trimmed)
    state.persist_flag(bool(_orch_plan_buf.strip()))
    has_plan, valid, issues = validate_plan_format(_orch_plan_buf)
    logger.info("plan issues %s", issues)
    safe = redact_via_context(_orch_plan_buf)
    state.persist_safe(safe)
"""


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ast.unparse(func)


def _is_text_derivation(
    node: ast.AST,
    raw_names: frozenset[str],
    shields: frozenset[str],
    reshapers: frozenset[str],
) -> bool:
    """True when *node* is a raw buffer or a pure reshaping of one.

    A reshaping keeps the provider bytes readable: the name itself, a slice, a
    concatenation or f-string containing it, a conditional between such values,
    a METHOD call on one (``raw.strip()``), or a call to one of *reshapers* with
    a raw argument. A call to anything else is either a sanctioned egress or an
    analysis function and is classified by the consumer census instead, so its
    result is deliberately NOT treated as raw -- that is what keeps
    ``has_plan, valid, issues = validate_plan_format(raw)`` from tainting three
    unrelated names.
    """
    if isinstance(node, ast.Call):
        if _call_name(node) in shields:
            return False
        if _call_name(node) in reshapers:
            return any(
                _is_text_derivation(arg, raw_names, shields, reshapers)
                for arg in [*node.args, *(keyword.value for keyword in node.keywords)]
            )
        return isinstance(node.func, ast.Attribute) and _is_text_derivation(
            node.func.value, raw_names, shields, reshapers
        )
    if isinstance(node, ast.Name):
        return node.id in raw_names
    if isinstance(
        node, (ast.Subscript, ast.BinOp, ast.JoinedStr, ast.FormattedValue, ast.IfExp, ast.BoolOp)
    ):
        return any(
            _is_text_derivation(child, raw_names, shields, reshapers)
            for child in ast.iter_child_nodes(node)
        )
    return False


def _raw_names(
    fn: ast.AST,
    raw_name: str,
    shields: frozenset[str],
    reshapers: frozenset[str] = _TEXT_RESHAPERS,
) -> frozenset[str]:
    """The raw buffer plus every name rebound to a reshaping of it, to a fixpoint.

    This closes the alias seam where ``x = raw`` followed by ``slot.append(x)``
    would otherwise hide the buffer from the consumer census.
    """
    names = {raw_name}
    while True:
        added = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets: list[ast.expr] = node.targets
                value: ast.expr | None = node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets, value = [node.target], node.value
            else:
                continue
            if value is None or not _is_text_derivation(
                value, frozenset(names), shields, reshapers
            ):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    added = True
        if not added:
            return frozenset(names)


def _contains_unshielded_raw(
    node: ast.AST,
    raw_names: frozenset[str],
    shields: frozenset[str],
    opaque: frozenset[str] = frozenset(),
) -> bool:
    """True when *node* hands a raw buffer (or alias) to whatever encloses it.

    A shielded call (a sanctioned redaction) and an *opaque* analysis call both
    stop the walk: their result is redacted text or a verdict, never the buffer.
    ``egress(bool(raw.strip()))`` is therefore not an egress of ``raw`` -- the
    ``bool`` call is censused as a consumer in its own right -- while
    ``egress(strip_plan_markers(raw))`` still is, because a reshaper is in
    neither set.
    """
    if isinstance(node, ast.Call) and _call_name(node) in shields | opaque:
        return False
    if isinstance(node, ast.Name) and node.id in raw_names:
        return True
    return any(
        _contains_unshielded_raw(child, raw_names, shields, opaque)
        for child in ast.iter_child_nodes(node)
    )


def _async_def(module_file: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    return next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _raw_consumers(
    fn: ast.AsyncFunctionDef,
    raw_name: str,
    shields: frozenset[str],
    *,
    reshapers: frozenset[str] = _TEXT_RESHAPERS,
    opaque: frozenset[str] = _INTERNAL_ANALYSIS,
    upto_line: int | None = None,
    after_line: int | None = None,
    exclude_names: frozenset[str] = frozenset(),
) -> set[str]:
    raw_names = _raw_names(fn, raw_name, shields, reshapers) - exclude_names
    consumers: set[str] = set()
    for call in (node for node in ast.walk(fn) if isinstance(node, ast.Call)):
        if upto_line is not None and call.lineno > upto_line:
            continue
        if after_line is not None and call.lineno <= after_line:
            continue
        values = list(call.args) + [keyword.value for keyword in call.keywords]
        if any(_contains_unshielded_raw(value, raw_names, shields, opaque) for value in values):
            consumers.add(_call_name(call))
    return consumers


def _run_chat_raw_consumers() -> set[str]:
    run_chat = _async_def(chat_runner.__file__, "_run_chat")
    return _raw_consumers(run_chat, "assistant_text", _SANCTIONED_EGRESS)


def _assert_every_consumer_is_classified(
    buffer: str, consumers: set[str], *classified: frozenset[str]
) -> None:
    """The guard's one verdict: a raw-buffer reader outside the allowlists FAILS.

    Shared by the production guards and the negative self-test below so the
    self-test exercises the same assertion the guards make, not a look-alike.
    """
    unexpected = consumers.difference(*classified)
    assert not unexpected, (
        f"raw {buffer} reached an unclassified call; either keep the use internal "
        "or route it through the host-aware egress helper: " + ", ".join(sorted(unexpected))
    )


def test_raw_turn_accumulator_has_only_sanctioned_egress_or_internal_consumers() -> None:
    consumers = _run_chat_raw_consumers()
    _assert_every_consumer_is_classified(
        "assistant_text", consumers, _SANCTIONED_EGRESS, _INTERNAL_ANALYSIS, _TEXT_RESHAPERS
    )
    assert _SANCTIONED_EGRESS <= consumers


def test_classification_sets_are_disjoint() -> None:
    """One name, one claim: a call is an egress, an opaque analysis, or a reshaper.

    A name in two sets would let a reshaper's raw result be read as an analysis
    result (not followed) or an analysis call be read as an egress (sanctioned).
    """
    assert _SANCTIONED_EGRESS.isdisjoint(_INTERNAL_ANALYSIS)
    assert _SANCTIONED_EGRESS.isdisjoint(_TEXT_RESHAPERS)
    assert _INTERNAL_ANALYSIS.isdisjoint(_TEXT_RESHAPERS)
    assert _SLACK_SANCTIONED_EGRESS.isdisjoint(_SLACK_INTERNAL_ANALYSIS)
    assert _SLACK_SANCTIONED_EGRESS.isdisjoint(_SLACK_TEXT_RESHAPERS)
    assert _SLACK_INTERNAL_ANALYSIS.isdisjoint(_SLACK_TEXT_RESHAPERS)


def test_orch_plan_buffer_reaches_only_plan_analysis() -> None:
    """The whole-turn plan buffer is fed raw and must never leave as text.

    It receives the same unredacted ``event.text`` as ``assistant_text``; a call
    that appended, broadcast or mirrored it would publish provider bytes that no
    boundary redacted. The only readers allowed are the two plan-analysis calls,
    and the extraction among them redacts what it returns.
    """
    run_chat = _async_def(chat_runner.__file__, "_run_chat")
    consumers = _raw_consumers(run_chat, "_orch_plan_buf", _SANCTIONED_EGRESS)
    _assert_every_consumer_is_classified(
        "_orch_plan_buf", consumers, _SANCTIONED_EGRESS, _INTERNAL_ANALYSIS
    )
    assert _ORCH_PLAN_BUF_CONSUMERS <= consumers, (
        "the plan-gate fallback no longer reads _orch_plan_buf through the classified "
        f"analysis calls: {sorted(consumers)}"
    )
    assert _ORCH_PLAN_BUF_CONSUMERS <= _INTERNAL_ANALYSIS


def test_orch_plan_buffer_is_fed_the_raw_provider_chunk() -> None:
    """Every write to the plan buffer is the raw chunk, not a per-chunk redaction.

    Per-chunk redaction is what shredded a split pako link before either boundary
    could validate it whole; the buffer must accumulate ``event.text`` verbatim so
    the classified extraction sees the same bytes ``assistant_text`` does.
    """
    run_chat = _async_def(chat_runner.__file__, "_run_chat")
    writes = [
        ast.unparse(node.value)
        for node in ast.walk(run_chat)
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_orch_plan_buf"
    ]
    assert writes == ["event.text"], writes


def test_the_guard_flags_a_direct_egress_of_a_raw_buffer() -> None:
    """The detector is not vacuous: a new ``slot.append``/broadcast of a raw buffer
    is reported by name, while a ``redact_via_context``-wrapped read is not."""
    fn = ast.parse(_SYNTHETIC_RAW_EGRESS).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    consumers = _raw_consumers(fn, "_orch_plan_buf", _SANCTIONED_EGRESS)
    unexpected = consumers - _INTERNAL_ANALYSIS - _SANCTIONED_EGRESS - _TEXT_RESHAPERS
    assert {"append", "post_message"} <= unexpected


def test_the_guard_follows_aliases_of_a_raw_buffer_without_tainting_analysis() -> None:
    """``x = raw; egress(x)`` is reported, and so is a slice/concat/augmented
    rebind and a rebind to a RESHAPER's result; a name bound to an analysis
    RESULT or to a shielded read is not."""
    fn = ast.parse(_SYNTHETIC_RAW_EGRESS).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _raw_names(fn, "_orch_plan_buf", _SANCTIONED_EGRESS) == {
        "_orch_plan_buf",
        "mirror",
        "tail",
        "trimmed",
    }
    consumers = _raw_consumers(fn, "_orch_plan_buf", _SANCTIONED_EGRESS)
    unexpected = consumers - _INTERNAL_ANALYSIS - _SANCTIONED_EGRESS - _TEXT_RESHAPERS
    assert {"mirror_message", "persist_tail", "persist_trimmed"} <= unexpected
    # A verdict computed from the buffer is not the buffer: the opaque ``bool``
    # call is censused itself and stops the walk into ``persist_flag``.
    assert unexpected.isdisjoint({"info", "persist_safe", "persist_flag"})
    assert "bool" in consumers


def test_the_guard_fails_rather_than_skips_on_an_unclassified_reader() -> None:
    """Negative self-test: the production assertion RAISES on the synthetic loop.

    The census tests above show the detector *reports* an unclassified direct
    reader, an alias and a reshaped copy; this pins that the guard turns each
    report into an ``AssertionError`` naming the call -- never a skip, never a
    silent pass -- so a new reader of a raw buffer cannot land green.
    """
    fn = ast.parse(_SYNTHETIC_RAW_EGRESS).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    consumers = _raw_consumers(fn, "_orch_plan_buf", _SANCTIONED_EGRESS)
    with pytest.raises(AssertionError) as failure:
        _assert_every_consumer_is_classified(
            "_orch_plan_buf", consumers, _SANCTIONED_EGRESS, _INTERNAL_ANALYSIS, _TEXT_RESHAPERS
        )
    message = str(failure.value)
    # One name per unclassified shape: a direct new reader (``append``), an
    # alias (``mirror_message`` via ``mirror = _orch_plan_buf``) and a reshaped
    # copy (``persist_trimmed`` via ``strip_plan_markers``).
    assert "append" in message and "mirror_message" in message and "persist_trimmed" in message
    # The same assertion passes when every reader is classified.
    _assert_every_consumer_is_classified("_orch_plan_buf", consumers, frozenset(consumers))


def test_a_reshaper_result_is_not_followed_when_reshapers_are_unclassified() -> None:
    """The reshaper taint is what catches ``persist_trimmed``; without it the
    name-based census would have passed that egress. Pins that the guard's
    strength rests on ``_TEXT_RESHAPERS`` being maintained, not on luck."""
    fn = ast.parse(_SYNTHETIC_RAW_EGRESS).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    consumers = _raw_consumers(fn, "_orch_plan_buf", _SANCTIONED_EGRESS, reshapers=frozenset())
    assert "persist_trimmed" not in consumers
    assert "strip_plan_markers" in consumers


def test_plan_metadata_extraction_redacts_what_it_hands_out() -> None:
    """The extraction's displayable outputs use the full active-policy boundary.

    The raw-buffer census classifies this helper as internal analysis only because
    every returned goal, title, and description is safe to hand to user-facing plan
    metadata. Clean fields must remain byte-identical.
    """
    import dataclasses

    from kiro_crew import security
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.context import reset_context, set_context

    companion_token = "COMPANION-PLAN-SECRET"
    clean_title = "verify unchanged metadata"
    clean_description = "- run the focused suite byte-for-byte"
    plan = (
        f"📋 Plan for: rotate {companion_token}\n"
        f"Stage 1: audit {companion_token}\n"
        f"- grep for {companion_token} in config\n"
        f"Stage 2: {clean_title}\n"
        f"{clean_description}\n"
    )

    class _CompanionPolicy:
        def redact(self, text: str) -> str:
            return security.redact(text).replace(
                companion_token, "[REDACTED: companion credential]"
            )

        def exempt_exact_hosts(self) -> frozenset[str]:
            return frozenset()

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, credentials=_CompanionPolicy()))
    try:
        titles, goal, descriptions = _extract_and_redact_plan_metadata(plan)
    finally:
        reset_context()

    handed_out = [goal, *titles, *(line for stage in descriptions for line in stage)]
    assert titles and goal and descriptions[0], handed_out
    assert all(companion_token not in text for text in handed_out), handed_out
    assert sum("[REDACTED: companion credential]" in text for text in handed_out) == 3
    assert titles[1] == clean_title
    assert descriptions[1] == [clean_description]


def _slack_joined_boundary(handle_message: ast.AsyncFunctionDef) -> ast.Assign:
    """The one statement that rebinds Slack's raw ``accumulated`` to redacted text.

    The boundary may live in a top-level ``if`` or in a direct ``if`` inside the
    top-level delivery ``try``. The latter is current main's permit-release and
    exactly-once-verdict architecture. It still may not live inside the provider
    event loop or an exception arm, so the line-number split below remains a real
    execution-order split over every reader.
    """
    candidate_bodies: list[list[ast.stmt]] = []
    for stmt in handle_message.body:
        if isinstance(stmt, ast.If):
            candidate_bodies.append(stmt.body)
        elif isinstance(stmt, ast.Try):
            candidate_bodies.extend(child.body for child in stmt.body if isinstance(child, ast.If))

    for body in candidate_bodies:
        for node in body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _call_name(node.value) == "redact_with_findings_via_context"
                and [ast.unparse(arg) for arg in node.value.args] == ["accumulated"]
                and isinstance(node.targets[0], ast.Tuple)
                and ast.unparse(node.targets[0].elts[0]) == "accumulated"
            ):
                return node
    raise AssertionError(
        "handle_message no longer rebinds `accumulated` through "
        "redact_with_findings_via_context at a direct delivery branch; the Slack "
        "joined redaction boundary is what makes every later consumer safe"
    )


def test_slack_raw_accumulator_reaches_only_sanctioned_egress_before_its_joined_boundary() -> None:
    handle_message = _async_def(slack_handler.__file__, "handle_message")
    boundary = _slack_joined_boundary(handle_message)
    assert boundary.end_lineno is not None
    before = _raw_consumers(
        handle_message,
        "accumulated",
        _SLACK_SANCTIONED_EGRESS,
        reshapers=_SLACK_TEXT_RESHAPERS,
        opaque=_SLACK_INTERNAL_ANALYSIS,
        upto_line=boundary.end_lineno,
    )
    unexpected = (
        before - _SLACK_SANCTIONED_EGRESS - _SLACK_INTERNAL_ANALYSIS - _SLACK_TEXT_RESHAPERS
    )
    assert not unexpected, (
        "raw Slack `accumulated` reached an unclassified call before the joined "
        "redaction boundary; wrap it in the redactor or move it after the "
        "boundary: " + ", ".join(sorted(unexpected))
    )
    assert _SLACK_SANCTIONED_EGRESS <= before


def test_slack_raw_aliases_after_the_boundary_reach_only_classified_readers() -> None:
    """``accumulated`` is redacted at the boundary, but a copy reshaped from it
    BEFORE the boundary is still raw afterwards. Every such alias may reach only
    the readers whose downstream redaction is recorded beside the allowlist."""
    handle_message = _async_def(slack_handler.__file__, "handle_message")
    boundary = _slack_joined_boundary(handle_message)
    assert boundary.end_lineno is not None
    after = _raw_consumers(
        handle_message,
        "accumulated",
        _SLACK_SANCTIONED_EGRESS,
        reshapers=_SLACK_TEXT_RESHAPERS,
        opaque=_SLACK_INTERNAL_ANALYSIS,
        after_line=boundary.end_lineno,
        exclude_names=frozenset({"accumulated"}),
    )
    unexpected = after - _SLACK_RAW_ALIAS_CONSUMERS_AFTER_BOUNDARY
    assert not unexpected, (
        "a raw alias of Slack `accumulated` reached an unclassified call after the "
        "joined boundary, which redacted only `accumulated` itself: "
        + ", ".join(sorted(unexpected))
    )
    assert after == _SLACK_RAW_ALIAS_CONSUMERS_AFTER_BOUNDARY, after


def test_slack_accumulator_is_not_rebound_after_its_joined_boundary() -> None:
    """Nothing re-introduces raw text into ``accumulated`` once it is redacted.

    Every consumer after the boundary (the conversation log, the linked dashboard
    slot) relies on reading the REDACTED value; an assignment after it would
    silently put provider bytes back.
    """
    handle_message = _async_def(slack_handler.__file__, "handle_message")
    boundary = _slack_joined_boundary(handle_message)
    assert boundary.end_lineno is not None
    late_writes = [
        node.lineno
        for node in ast.walk(handle_message)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
        and node.lineno > boundary.end_lineno
        and "accumulated"
        in {
            ast.unparse(target)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        }
    ]
    assert late_writes == [], late_writes
