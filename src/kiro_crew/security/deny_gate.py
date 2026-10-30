"""The command deny gate, and the audit events of the security decisions.

:func:`is_denied` matches a command line against the two deny tiers -- the
effective rule regexes and the operator's glob patterns -- in the two passes its
docstring describes, over the quote- and escape-normalized views of each segment
(``_deny_segment_views``), beside the argv-structural floors of
:mod:`kiro_crew.security.argv_floor`. :func:`is_denied_synthesized_target` judges a
target the gate built itself. Each decision is written to the security event log: a
denial (``_emit_deny_event``), a granted exception (``_emit_deny_exception_event``),
an allowed feature-branch push (``_emit_push_allow_event``, offloaded by
``_schedule_push_allow_audit``) and dropped prompt-injection content
(:func:`audit_injection_dropped`).

The emitters live beside the gate because tests patch ``SecurityEventLog``,
``maintenance_executor``, ``_emit_deny_event``, ``_emit_deny_exception_event`` and
``_deny_segment_views`` on ``kiro_crew.security``: the package's ``_EXPORTS`` table
sends each of those writes here, and every reader of those names resolves them
through this module's globals.

``kiro_crew.security`` is the import path. This module reads the package's
``_submodule`` resolver and ``logger``, so the package imports it at its foot, once
both are bound. New deny-gate logic and new security audit events belong here.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from kiro_crew.executors import maintenance_executor
from kiro_crew.security import _submodule, logger
from kiro_crew.sel import SecurityEvent, SecurityEventLog

from .shell_commands import sharing_views
from .text_redaction import redact_and_truncate

if TYPE_CHECKING:
    from collections.abc import Callable


def _schedule_push_allow_audit(command: str) -> None:
    """Fire-and-forget audit write offloaded to the maintenance executor.

    Avoids blocking the event loop on file I/O (same concern as
    ``_emit_deny_event`` — both should be offloaded per the
    no-blocking-call-on-event-loop guideline).  Falls back to an inline
    synchronous write when no event loop is running (sync/test contexts).
    """
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(maintenance_executor(), _emit_push_allow_event, command)
    except RuntimeError:
        # No running loop (called from a sync test or non-async context) —
        # fall back to inline write (acceptable: no event loop to block).
        _emit_push_allow_event(command)


def _emit_push_allow_event(command: str) -> None:
    """Emit a SEL audit event when a feature-branch push is allowed through.

    Best-effort: an audit failure is logged at WARNING and does not affect the
    allow decision (the push already passed the protected-branch gate).
    """
    try:
        sel = SecurityEventLog()
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="push_allowed",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation="git_push",
                outcome="allowed",
                resources="feature_branch_push",
                metadata={
                    "command": redact_and_truncate(command, 200),
                    "mechanism": "BRANCH_GATE",
                },
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for push_allowed (allow stands)",
            exc_info=True,
        )


def _deny_segment_views(segment: str, emit_self: bool = True) -> tuple[str, ...]:
    """The views of ONE shell segment that the deny tiers are matched against.

    *segment* arrives with its ORIGINAL CASE, and every view returned is
    lowercased.  Case matters for exactly one step: bash's Unicode escape widths
    are case-sensitive (``\\u`` up to 4 hex digits, ``\\U`` up to 8), so decoding
    after a ``lower()`` would read ``$'\\u0072f'`` -- which bash passes as ``rf`` --
    as a single 5-digit code point and miss the rule.  The decode therefore runs
    FIRST, on the text as written, and the lowercasing happens after.

    The first element is always the raw text (lowercased) -- matched exactly as it
    was before this helper existed -- so nothing that was denied can stop being
    denied.  Quote/escape-NORMALIZED re-joins are APPENDED when they differ.

    *emit_self* False walks NESTED PAYLOADS ONLY, emitting no view for *segment*
    itself.  That is how the whole command is inspected without joining across its
    separators: ``_split_segments`` is deliberately quote-unaware, so a newline
    inside a quoted payload (``bash -c 'r\\<newline>m -rf /'``) severs the command
    into pieces before the payload can be extracted from it -- while re-joining the
    whole command would fabricate a command that never ran.  Walking it for
    payloads without emitting its own re-join gets the first without the second.

    ── Why the extra view is needed ──
    Both deny tiers match TEXT, and a shell removes quoting, escaping and
    empty-string splices and collapses whitespace runs before the program ever
    sees its argv.  So every rule authored as a command SHAPE (``rm -rf /``,
    ``dd if=``, ``chmod 777``) was defeated by re-spelling any one token:
    ``rm -rf "/"``, ``"rm" -rf /``, ``'rm' -rf /``, ``rm "-rf" /``,
    ``r''m -rf /`` and ``rm  -rf /`` all run the identical command and none of
    them CONTAINS the pattern's own text.  Of the ~140 built-in rules only the
    six self-protection rules and git-publish had an argv-structural floor
    closing this (see ``_SELF_PROTECTION_FLOOR_PATTERNS``); every other rule was
    spelling-dependent.

    ── Why the tokenizer is ``_shell_tokens`` and not ``normalize_shell_command``
    Both share one tokenizer, but the deny view deliberately stops BEFORE
    ``~``/``$HOME`` expansion, for two reasons.  Expansion is
    platform-dependent, so it would make the view decide differently per host:
    it DELETES the literal ``~`` that ``rm -rf ~.*`` is authored to match, and
    on Windows it yields a drive path (``c:\\users\\…``) that no POSIX-anchored
    rule matches — so ``rm -rf "~"`` would be caught on Linux by the sibling
    ``rm -rf /.*`` rule and missed on Windows.  And a denied view becomes the
    security event log's ``operation`` field, so expanding here would write the
    operator's real home path into the audit trail on every such denial.  Path
    IDENTITY (dot segments, ``..``, ``$HOME`` versus the resolved home) is
    already decided by ``is_sensitive_path`` against the
    sensitive-path keystone, which is the layer that resolves rather than
    matches; this view answers only the narrower question of what the shell
    hands over as argv.

    ── Why this is a per-SEGMENT view, never a whole-command one ──
    Re-joining tokens with single spaces erases the separators a shell uses to
    END a command, so normalizing the whole input would FABRICATE a command that
    was never run: ``echo rm`` + newline + ``-rf /`` is two commands, and a
    whole-input re-join reads as ``echo rm -rf /``.  The heredoc frames pinned by
    ``TestStdinProgramTextScoping.test_benign_neighbour_no_longer_reads_as_a_mint``
    are the concrete case.  Segments come from ``_split_segments``, so no boundary
    is ever crossed — including inside a nested payload, which is split the same
    way before being viewed.

    ── Nested shell payloads ──
    A shell's ``-c`` argument is a COMMAND, and ``shlex`` strips only the OUTER
    quoting level, so ``bash -c 'dd "if=/dev/zero" of=/dev/sda'`` re-joins with
    its inner quotes intact and the ``dd if=`` rule still does not match.
    Each literal payload is
    therefore walked and viewed in its own right, reusing
    :func:`_nested_shell_payloads` — the extractor the self-protection floor
    already uses, so the ``-c`` / ``eval`` / ``env -S`` / herestring /
    ``$SHELL -c`` spellings and the ``bash -c -- <script>`` form are recognized
    here by construction rather than re-enumerated.  Only LITERAL payloads exist
    to walk: ``eval "$CMD"`` carries no visible script and stays the raw tier's
    job.

    The walk takes NO numeric depth cap, for the reason
    :func:`_self_token_frames` records: whatever the number, one more wrapper
    defeats it.  It terminates structurally instead — a payload is carried inside
    ONE token of its parent, so it is strictly shorter than the parent's source
    text, and a chain of strictly shorter strings is finite.

    ── Fail-closed, and it never raises ──
    This only ever ADDS views.  ``_shell_tokens`` already degrades to whitespace
    splitting with quote stripping when ``shlex`` rejects the input (so even an
    unbalanced-quote segment still normalizes), and every window is built inside a
    guard: this runs in the permission gate, where an exception is a crash rather
    than a security decision, so a failure drops that window and leaves the raw
    view standing.  A failure can therefore lose the EXTRA match but never the raw
    one, so it cannot turn a denied command into an allowed one.

    ── Whitespace-only words ──
    A quoted WHITESPACE-ONLY word (``rm -rf " " /home/x``) is a real operand, so
    a fourth view drops it: an argv one operand short, matched against and
    nothing more.  It is additive -- an exception excusing one view's match only
    moves on to the next pattern -- so it can add a denial, never remove one.
    A token with any non-whitespace character stays whole (``"a b"``).

    ── Residual ──
    Two shapes stay outside every view.  A token split by BOTH quoting and a
    separator-shaped glue construct (``"rm"$(echo ' ')-rf /``) is in none of them:
    the raw text is not contiguous and the glue lands on its own segment — the
    whole-string raw pass covers the glue-ONLY spelling (``git$(echo ' ')push``),
    and closing the combination needs a normalizer that models substitution,
    which a re-join is not.  A variable spelling of a path operand
    (``rm -rf $HOME``) is by construction not expanded here, per the note above.
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _shell_normalizer = _submodule("shell_normalizer")

    views: list[str] = [segment.lower()] if emit_self else []
    seen_views: set[str] = set(views)
    # Decode the case-sensitive escapes BEFORE folding case (see the docstring),
    # then work entirely in lowercase from here on -- the tiers compare lowercased
    # text, and the payload extractor recognizes lowercase program names.  Guarded
    # like the walk below: this is the permission gate, so a decoder that raises
    # must cost the extra view, never the decision.
    try:
        start = _shell_normalizer._decode_shell_quoted_literals(segment).lower()
    except Exception:
        logger.debug("deny-view quote decode failed; raw view only", exc_info=True)
        start = segment.lower()
    seen_sources: set[str] = {start}
    # (source, parent_len, is_root, allow_join): a payload lives inside one token of
    # its parent, so it is strictly shorter than the parent's source text — which is
    # what bounds this walk without a numeric cap.  ``is_root`` marks the source the
    # caller handed in, whose own re-join ``emit_self=False`` suppresses.
    # ``allow_join`` carries the same discipline ``_shell_payload_walk`` applies: a
    # frame produced BY the ``eval`` argument join must not join again, or the two
    # walks each build a chain of shrinking suffixes and this one — which re-lexes
    # and re-splits every frame — dominates the cost (measured on ``"eval " * 640``:
    # 12.0 s of a 14.2 s total here, against 0.04 s before the join existed).
    pending: list[tuple[str, int, bool, bool]] = [(start, len(start) + 1, True, True)]
    while pending:
        source, parent_len, is_root, allow_join = pending.pop()
        try:
            tokens = _shell_normalizer._shell_tokens(source)
            if not tokens:
                continue
            # No expansion happens above, so an already-lowercased source stays
            # lowercased through the re-join and needs no second fold.
            #
            # The empty-elided re-join is a THIRD view, ADDED beside the plain one
            # rather than replacing it -- this helper only ever adds views, and
            # substituting here broke that invariant in a measurable way.  An
            # empty-quoted word (``""``, ``''``, ``$''``, or any concatenation of
            # them) is a real argv element the shell does hand over, so
            # ``_shell_tokens`` is right to keep it and the payload walk below
            # still sees argv as it was.  What it cannot survive is the RENDER: a
            # single-space join turns a zero-width element into a spurious extra
            # separator, and every rule authored as a command shape with single
            # separators (``rm -rf /``, ``dd if=``) then stops matching its own
            # target -- ``rm -rf "" /home/x`` rendered as ``rm -rf  /home/x``.
            # The element contributes no text to the shape and cannot name a file
            # or carry a flag, so a view without it renders what the command does
            # rather than fabricating something it does not.
            #
            # Keeping the plain join is not defensive tidiness.  A rule that
            # REQUIRES an intervening token (``rm -rf .* ./data``) matched the
            # double-spaced view and matches neither the elided one nor the
            # command's canonical spelling, so dropping it removed a denial that
            # existed: ``r""m -rf "" ./data`` was refused and became
            # allowed (reproduced against the
            # merge-base).  Emitting both means a rule authored against either
            # whitespace shape still fires, which is the only reading that cannot
            # lose a denial.  Rules whose own pattern already tolerated the extra
            # separator (``chmod.*/etc/.*``) were denying via the plain view all
            # along, which is why the escape was pattern-dependent rather than
            # uniform, and why this belongs here and not in individual rules.
            view = " ".join(tokens)
            candidates = [view]
            elided = " ".join(token for token in tokens if token)
            if elided and elided != view:
                candidates.append(elided)
            # Whitespace-only tokens dropped; appended, never substituted.
            stripped = " ".join(token for token in tokens if token.strip())
            if stripped and stripped not in candidates:
                candidates.append(stripped)
            for candidate in candidates:
                if not (is_root and not emit_self) and candidate not in seen_views:
                    seen_views.add(candidate)
                    views.append(candidate)
            joined_here: set[str] = set()
            payloads = _shell_normalizer._nested_shell_payloads(
                tokens, allow_join=allow_join, joined_out=joined_here
            )
            # A redirect before the ``-c`` operand hides the script from the payloads.
            payloads = [*payloads, *_submodule("shell_commands").redirect_hidden_payloads(source)]
            programs = _shell_normalizer._argv_programs(tokens) if payloads else []
            # Both values below read ONLY ``tokens``, which is fixed for this
            # whole walk, so they are charged ONCE here instead of once per
            # payload.  Asking per payload is what makes this loop quadratic in
            # payload count (18k payloads, ~293s): the exemption's
            # command-level guards sweep the whole argv, and recovering a
            # payload's positions with ``enumerate`` sweeps it again, so N
            # payloads cost N x len(tokens).  Neither hoist can change a verdict:
            # same inputs, same answers, computed once rather than N times.  Both
            # are skipped when there are no payloads so an ordinary command --
            # the common case -- pays nothing new.
            command_disqualified = (
                _shell_normalizer._data_consumer_command_disqualified(tokens) if payloads else False
            )
            token_positions: dict[str, list[int]] = {}
            if payloads:
                for _pos, _tok in enumerate(tokens):
                    token_positions.setdefault(_tok, []).append(_pos)
            for payload in payloads:
                if len(payload) >= parent_len:
                    continue
                # ``echo bash -c '<script>'`` PRINTS the script, so descending into
                # it refuses a command that runs nothing.  The repo's own exemption
                # decides this, rather
                # than a "launcher must be in command position" rule: the launcher
                # is NOT in command position in ``sudo bash -c …``,
                # ``timeout 5 bash -c …``, ``nohup``, ``ssh host``, ``xargs`` or
                # ``env FOO=1 bash -c …``, all of which really do execute, so that
                # rule would trade this false positive for six bypasses.
                # ``_data_consumer_exempt`` is a DENYLIST of consumers with the
                # executing cases already carved out (a piped evaluator, a
                # substitution in program position, an ``awk``/``sed`` script that
                # can execute), so a program it does not know stays walked.
                #
                # A payload is not necessarily a TOKEN.  ``_nested_shell_payloads``
                # also returns SYNTHESIZED text — a ``sed`` ``e``-flag replacement,
                # the tail of a glued herestring (``bash<<<'<script>'``), a glued
                # ``env -S`` argument, an ``alias`` assignment — which is a
                # substring or a re-join, not an element of ``tokens``.  Recovering
                # a position with ``list.index`` therefore raised ``ValueError`` and
                # propagated out of the permission gate on legitimate input
                # (``sed 's/x/y/e' notes.txt``).
                #
                # The exemption is decided per OCCURRENCE and fails closed: it is
                # applied only when the payload appears as a token AND every
                # occurrence sits in the argv of a data consumer.  A payload with no
                # token position cannot be proven inert, so it is DESCENDED into —
                # over-blocking, which is the safe direction here.  Deciding from a
                # single recovered index would not be sound: a short synthesized
                # payload can also be a coincidental substring of an unrelated
                # token, and one wrong position could wrongly exempt a payload that
                # really executes.
                occurrences = token_positions.get(payload, [])
                if occurrences and all(
                    _shell_normalizer._data_consumer_exempt(
                        i,
                        payload,
                        programs,
                        tokens,
                        command_disqualified=command_disqualified,
                    )
                    for i in occurrences
                ):
                    continue
                # A payload is a command LINE, so it gets the same PRE-LEX treatment
                # the top level got: the shell that runs it folds ITS continuations
                # before lexing, so fold before splitting or the split severs them.
                # ``bash -c 'r\<newline>m -rf /'`` otherwise yields the pieces ``r``
                # and ``m -rf /``, and no view holds the command that runs.
                # A view must also not be joined across one
                # of the payload's own separators.  Only the PIECES are recorded as
                # walked — recording the payload itself would filter out the single
                # piece that equals it.
                child_may_join = payload not in joined_here
                for piece in _split_segments(_shell_normalizer._fold_line_continuations(payload)):
                    piece = piece.strip()
                    if piece and piece not in seen_sources:
                        seen_sources.add(piece)
                        pending.append((piece, len(source), False, child_may_join))
        except Exception:
            # This runs INSIDE the permission gate, where an exception is a crash
            # rather than a security decision — the hazard ``_normalize_search_path``
            # documents for the same reason.  Losing one view only costs the EXTRA
            # match; the raw view is already in ``views`` and the raw tier decides
            # exactly as it did before this helper existed, so a failure here can
            # never turn a denied command into an allowed one.
            logger.debug("deny-view construction failed for a window", exc_info=True)
            continue
    return tuple(views)


# An interpreter binds the halves to its OWN variables
# (``n = "<name>"; v = "<verb>"; run([n, v])``) and then uses the names.  Inlining those
# bindings is the interpreter-side twin of the shell assignment resolution, and it is what
# keeps the argv pattern TIGHT: the alternative -- admitting ``;`` into the separator class
# so the two quoted strings may sit in different statements -- would also match
# ``print('<name>'); log('<verb>')``, which mints nothing.
_INTERP_BINDING_RE = re.compile(r"\b([a-z_]\w*)\s*=\s*('[^']*'|\"[^\"]*\")")
_INTERP_IDENT_RE = re.compile(r"\b[a-z_]\w*\b")


# ``"<name> %s" % "<verb>"`` -- printf-style formatting is the same evasion as adjacent
# literal concatenation, one operator along.  The tuple spelling
# (``"%s %s" % ("<name>", "<verb>")``) is covered by consuming the arguments in order.
_PERCENT_FORMAT_RE = re.compile(
    r"""(['"])([^'"]*)\1\s*%\s*\(?\s*((?:['"][^'"]*['"]\s*,?\s*)+)\)?"""
)
_QUOTED_FRAGMENT_RE = re.compile(r"""['"]([^'"]*)['"]""")
_FORMAT_SPEC_RE = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?[sridfge]")


def _collapse_percent_format(text: str) -> str:
    """Apply ``%`` formatting to a quoted template whose arguments are literals.

    Only literal arguments are substituted -- the point is to see the string the
    interpreter will hand to a sink, exactly as the concatenation collapse does.
    """

    def _apply(match: "re.Match[str]") -> str:
        quote, template, arg_blob = match.group(1), match.group(2), match.group(3)
        args = _QUOTED_FRAGMENT_RE.findall(arg_blob)
        if not args:
            return match.group(0)
        remaining = list(args)

        def _one(_spec: "re.Match[str]") -> str:
            return remaining.pop(0) if remaining else _spec.group(0)

        return f"{quote}{_FORMAT_SPEC_RE.sub(_one, template)}{quote}"

    return _PERCENT_FORMAT_RE.sub(_apply, text)


def _inline_interpreter_bindings(text: str) -> str:
    """Replace identifiers bound to a quoted literal in *text* with that literal."""
    bindings: dict[str, str] = {}
    for match in _INTERP_BINDING_RE.finditer(text):
        bindings.setdefault(match.group(1), match.group(2))
    if not bindings:
        return text
    return _INTERP_IDENT_RE.sub(lambda m: bindings.get(m.group(0), m.group(0)), text)


@sharing_views
def is_denied(
    tool_name: str,
    extra_patterns: list[str] | None = None,
    *,
    denied_regexes: list[str] | None = None,
    reason_notes: dict[str, str] | None = None,
) -> str | None:
    """Check tool name against the built-in/effective + extra deny patterns.

    Returns denial reason string, or None if allowed.

    ── Two tiers ──
    * Regex tier (``denied_regexes``): the effective enabled built-in rule
      regexes plus user-added regexes (output of ``compute_effective_denied``),
      matched via ``re.search`` (``re.IGNORECASE``).  When ``None``, FAILS
      CLOSED to all built-ins enabled.
    * Glob tier (``extra_patterns``): legacy ``auto_deny_tools`` + companion
      overlay globs, matched via ``fnmatch`` exactly as before.

    ── Two-pass evaluation ──
    Pass 1 (whole-string): every deny pattern is matched against the
    full input.  If a pattern matches and **no exception pattern also
    matches the full input**, the input is denied immediately.  This
    closes evasion vectors where the deny string spans a separator
    boundary that per-segment splitting would erase, e.g.
    ``git$(echo ' ')push origin main`` (which bash evaluates to
    ``git push origin main``): the whole string contains both ``git`` and
    ``push`` so the broad ``*git*push*`` glob matches, and there is no
    matching exception, so the command is denied at this stage even
    though splitting on ``$(`` / ``)`` would otherwise produce no
    segment containing both substrings.

    Pass 2 (per-segment) only runs if pass 1 found a deny match **and**
    the full input also matched at least one exception for that pattern.
    The input is split on shell command separators (``;``, ``&&``,
    ``||``, ``|``, newlines) and command-substitution boundaries
    (``$(``, ``)``, backticks) into segments, and each segment is
    re-evaluated independently.  This preserves the chaining-bypass
    protection (any embedded real
    publish lives in its own segment and matches the deny pattern in its
    own right) while allowing the legitimate stash-in-pipeline case
    that the prior whole-string design over-blocked.

    Edge cases & limitations:
      - Pass-1 deny is conservative: anything matching a deny glob with
        no exception is blocked, even if the input is structurally
        contorted.
      - Pass-2 splitting is purely textual; quoted strings and escaped
        separators are split anyway (over-blocking is the safer
        direction).
      - Each pass-2 segment is matched in TWO views: the raw text, then a
        quote/escape-normalized re-join of that segment
        (``_deny_segment_views``), so a rule authored as a command shape is
        not defeated by re-quoting a token (``rm -rf "/"``), splicing one
        (``r''m -rf /``) or padding the whitespace.  Strictly additive, and
        never applied across a separator — see that helper.
      - Heredoc bodies, ``eval``, ``bash -c``, etc., are not parsed
        specially.  If those become evasion vectors in practice, add
        explicit deny patterns for them.

    Audit:
      - Every denial path emits a ``deny_event`` SEL event via
        ``_emit_deny_event``.
      - Every granted exception emits a ``deny_exception`` SEL event via
        ``_emit_deny_exception_event`` (fail-closed: if SEL logging
        fails the exception is not granted).

    Args:
        tool_name: The full command line / tool invocation to evaluate.
        extra_patterns: Optional fnmatch glob patterns (glob tier — legacy
            ``auto_deny_tools`` + companion overlay).
        denied_regexes: The effective enabled rule regexes (regex tier).  When
            ``None``, fails closed to all built-in rules enabled.
        reason_notes: Optional ``{pattern: operator note}`` map.  When the pattern
            that matched has a note, the note is appended to the refusal on its
            OWN line.  Presentation only — it never affects whether something is
            denied.

    Returns:
        Denial reason string (mentioning the matched pattern), or
        ``None`` if the input is allowed.
    """
    # The owners of the names this function reads, resolved once per call. Reading
    # a name through its owner is what keeps its value in one place; resolving the
    # owner once rather than per read is what keeps this per-tool-call predicate's
    # cost flat -- one import-system lookup per owner, not one per name read.
    _rules = _submodule("denied_rules")
    _argv = _submodule("argv_floor")
    _diag = _submodule("diagnostics")

    lower = tool_name.lower()

    def _reason(
        matched: str,
        note_override: str = "",
        *,
        rule: str = "",
        component: str = "",
    ) -> str:
        """Refusal text for *matched* -- see :func:`_deny_reason`, the shared producer.

        *note_override* lets the argv-structural floor say why a pattern the input
        does not literally match was still the rule that fired (see
        ``_SELF_PROTECTION_FLOOR_NOTES``).

        *rule* and *component* add the diagnostic line, and only the STRUCTURAL
        floors below pass them. That is the whole distinction: a pattern-tier
        denial's first line already names the pattern that matched the input, so a
        diagnostic would repeat it on every ordinary refusal, while a floor denial
        reports a pattern the input provably cannot match and is the refusal an
        agent cannot diagnose at all. The span is the whole subject because a floor
        decides on the argv's SHAPE rather than at an offset.
        """
        diagnostic = (
            _diag.refusal_diagnostic(rule, component, tool_name) if rule and component else None
        )
        return _rules._deny_reason(
            matched, reason_notes, note_override=note_override, diagnostic=diagnostic
        )

    glob_patterns = list(extra_patterns or [])
    if denied_regexes is None:
        regex_patterns = _rules.compute_effective_denied(
            _rules.BUILTIN_DENIED_RULES, (), False, (), ()
        )
    else:
        regex_patterns = list(denied_regexes)
    # Capture which git-publish rules are still ENABLED *before* the strip below
    # removes their patterns from the regex tier. Computing this afterwards would
    # always yield the empty set and the floor would never fire — a silent, total
    # loss of push protection.
    git_publish_enabled = {p for p in regex_patterns if p in _rules._GIT_PUBLISH_RULE_PATTERNS}
    # Never feed git-publish rule patterns to Python ``re`` — they are ReDoS-prone
    # under backtracking and are already enforced by the ``_is_git_publish`` floor
    # below (see ``_GIT_PUBLISH_RULE_PATTERNS``).
    regex_patterns = [p for p in regex_patterns if p not in _rules._GIT_PUBLISH_RULE_PATTERNS]
    # The two self-protection rules get an ADDITIONAL argv-structural floor
    # below, for the reason documented on ``_SELF_PROTECTION_FLOOR_PATTERNS``:
    # only a tokenized view can tell ``kirocrew "token"`` from
    # ``kirocrew-wt-x/test_token_auth.py``.  The floor is a UNION with the regex
    # tier, never a replacement -- the patterns deliberately stay in
    # ``regex_patterns``.  Two independent reasons:
    #   1. The regex still matches raw text, so a payload the tokenizer cannot
    #      see into (``bash -c "kirocrew token"``, ``eval "$CMD"``) is caught.
    #   2. The tokenizer can fail (unbalanced quotes, or a platform bug like the
    #      one fixed in ``normalize_shell_command`` above), and a floor that
    #      REPLACED the regex would then fail OPEN.
    # A rule the operator has DISABLED must stay disabled, so the floor runs
    # only for patterns still present in the effective set.
    floor_enabled = {p for p in regex_patterns if p in _rules._SELF_PROTECTION_FLOOR_PATTERNS}
    # An interpreter CONCATENATES adjacent string literals, so ``'p'+'kill -f <name>'``
    # is one command by the time it reaches the sink.  The two interpreter rules are
    # therefore also matched against a copy with those joins collapsed.  Scoped to those
    # two patterns on purpose: collapsing text for all the other rules would change
    # inputs they were never measured against.
    joined = _inline_interpreter_bindings(
        _collapse_percent_format(_rules._LITERAL_CONCAT_RE.sub("", lower))
    )
    if joined != lower:
        for interpreter_pattern in regex_patterns:
            if interpreter_pattern not in _rules._INTERPRETER_RULE_PATTERNS:
                continue
            try:
                if re.search(interpreter_pattern, joined, re.IGNORECASE):
                    _emit_deny_event(tool_name, interpreter_pattern, lower)
                    return _reason(interpreter_pattern)
            except re.error:  # pragma: no cover - patterns are validated at load
                continue
    # Ordered (pattern, is_regex) pairs so the two passes share one code path;
    # regex tier first (the effective rule set), then the glob tier.
    all_patterns: list[tuple[str, bool]] = [(p, True) for p in regex_patterns] + [
        (p, False) for p in glob_patterns
    ]

    # ── Git publish (verb-anchored, not a glob) ──
    # Checked on the whole string first so command-substitution glue-evasion
    # (e.g. ``git$(echo ' ')push``) is caught even though splitting on ``$(``
    # / ``)`` would otherwise scatter the ``git``/``push`` tokens across
    # segments.  ``_is_git_publish`` is verb-anchored, so a commit message or
    # branch name merely containing "push" does not match.
    #
    # A push to a PROTECTED branch (or a bare/ambiguous push) is denied here;
    # an explicit FEATURE-branch push is allowed to fall through to the normal
    # glob passes (so any other deny pattern in a compound command still
    # applies), and we record the allow INTENT now — the ``push_allowed`` audit
    # is emitted only at a SUCCESS return path below, so the SEL trail reflects
    # the FINAL outcome (never an allow for a command ultimately denied).
    #
    # Evaluated over the whole string AND the source of every nested shell payload
    # (``_shell_payload_sources``), because this floor is the SOLE enforcement for
    # pushes -- every git-publish rule is stripped from the regex tier just above.
    # A top-level-only text match therefore meant one wrapper was a complete
    # bypass: ``bash -c 'git push origin main'`` and ``eval '<push>'`` reached no
    # check at all, while the self-protection floor beside it was already immune
    # because it re-tokenizes payloads. Same walk, same depth guarantee, so a
    # wrapper cannot buy anything here either.
    push_allow_pending = False
    try:
        payload_sources = _argv._shell_payload_sources(lower)
    except Exception:
        # This runs inside the PreToolUse gate, which must return a DECISION and
        # never raise. Degrade to the top-level reading -- precisely what this
        # floor checked before it learned to descend -- so a broken walk costs
        # the nested coverage and nothing else. Failing closed here instead
        # would refuse ordinary commands on any walk hiccup.
        payload_sources = [lower]
    # Tags are collected across EVERY publish source, not just the top-level
    # string, and gated once afterwards. Reading only ``lower`` made one wrapper a
    # complete bypass of the sole enforcement pushes have; gating once at the end
    # keeps the per-rule opt-out semantics exactly as written -- a rule an operator
    # disabled stays disabled at whatever depth it fires.
    publish_sources = [source for source in payload_sources if _argv._is_git_publish(source)]
    if publish_sources:
        floor_tags: frozenset[str] = frozenset()
        for publish_source in publish_sources:
            floor_tags |= _argv._git_publish_floor_tags(publish_source)
        # The ungated tag denies regardless of opt-out: it marks a command whose
        # target could not be verified at all, which is what keeps the gated
        # rules below non-bypassable. Report it under the brace-expansion rule,
        # whose coverage this branch is, so the refusal still names a catalog row.
        if _rules._GIT_PUBLISH_UNGATED in floor_tags:
            ungated_pattern = _rules._GIT_PUBLISH_FLOOR_BY_ID.get(
                "git-publish-push-brace-expansion-refspec", _argv._GIT_PUBLISH_DENY_LABEL
            )
            _emit_deny_event(tool_name, ungated_pattern, lower)
            return _reason(
                ungated_pattern,
                "Matched structurally on the command's argv, not by the pattern text above: "
                "shell substitution or expansion fuses text into the push target, so the "
                "destination branch cannot be determined before the push runs.",
                rule="git-publish-target-unverifiable",
                component="git-publish-floor",
            )
        for tag in sorted(floor_tags):
            gated_pattern = _rules._GIT_PUBLISH_FLOOR_BY_ID.get(tag)
            if gated_pattern is None:
                # A tag naming no catalog row is a MAINTENANCE error, not a policy
                # choice, and the two must not share a branch: skipping here would
                # turn a renamed rule id or tag literal into a silent allow of a
                # protected-branch push, with the failure direction under
                # refactoring being "publish". Deny instead, under the ungated
                # sentinel's row, so the mistake is loud and fail-closed. The
                # structural guard in test_push_branch_gate.py still catches it at
                # build time; this is what happens if that guard is ever removed.
                fallback = _rules._GIT_PUBLISH_FLOOR_BY_ID.get(
                    "git-publish-push-brace-expansion-refspec", _argv._GIT_PUBLISH_DENY_LABEL
                )
                logger.error(
                    "git-publish floor tag %r resolves to no catalog rule; denying "
                    "fail-closed. This is a code defect: the tag and the rule id "
                    "have drifted apart.",
                    tag,
                )
                _emit_deny_event(tool_name, fallback, lower)
                return _reason(
                    fallback,
                    "A protected-branch push shape was recognised but its rule "
                    "could not be resolved, so it is refused rather than allowed.",
                    rule="git-publish-tag-unresolved",
                    component="git-publish-floor",
                )
            if gated_pattern not in git_publish_enabled:
                continue
            # SEL keeps the PATTERN (that is what maps an event to a catalog row),
            # while the human-facing refusal leads with the rule ID: the chip in
            # the dashboard's RecoveryCard is filled verbatim from this first line,
            # and a ~70-char raw regex there is unreadable on the single most
            # frequent denial an agent user hits. The id is both short and the
            # actual toggle identity, so it tells the operator exactly which row to
            # switch off; the regex stays available on the note line below, which
            # the chip parser deliberately ignores.
            _emit_deny_event(tool_name, gated_pattern, lower)
            note = _rules._GIT_PUBLISH_FLOOR_NOTES.get(tag, "")
            return _reason(
                tag,
                f"{note} (rule pattern: {gated_pattern})".strip(),
                rule=tag,
                component="git-publish-floor",
            )
        push_allow_pending = True

    # ── Self-protection floor (argv-structural, not a glob) ──
    # Runs before the pattern passes and on the WHOLE string, for the same reason
    # the git-publish floor does: the evasions live in shell syntax that textual
    # splitting scatters or mis-reads.  Each predicate here is checked only if its
    # catalog row is still in the effective set, so an operator-disabled rule
    # stays disabled.
    for rule_id, predicate in (
        ("credential-exfil-kirocrew-token", _argv._is_credential_mint),
        ("self-protection-kill", _argv._is_self_kill),
        ("self-protection-dev-mode-out-of-root-confirm", _argv._is_dev_mode_out_of_root_confirm),
        ("sandbox-escape-ssh-self", _argv._is_ssh_to_self),
    ):
        pattern = _rules._SELF_PROTECTION_FLOOR_BY_ID.get(rule_id)
        if pattern is None or pattern not in floor_enabled:
            continue
        # The mint predicate also gets the command AS SUBMITTED: it decodes base64
        # literals to read the name they hide, and base64 does not survive the
        # lower-casing every other predicate reads.
        hit = (
            _argv._is_credential_mint(lower, raw_text=tool_name)
            if predicate is _argv._is_credential_mint
            else predicate(lower)
        )
        if hit:
            # Report the rule's own pattern, exactly as the regex tier does, so
            # the denial reason and the SEL event still map back to the rule id —
            # plus a second line saying the match was STRUCTURAL, because a floor
            # hit routinely occurs on input that pattern cannot match and the
            # bare identifier reads as a false explanation.
            _emit_deny_event(tool_name, pattern, lower)
            return _reason(
                pattern,
                _rules._SELF_PROTECTION_FLOOR_NOTES.get(rule_id, ""),
                rule=rule_id,
                component="argv-floor",
            )
    # The self-management SUBCOMMAND floors have no catalog row (their
    # product-name-anywhere regex rows were deleted, see
    # ``_SELF_PROTECTION_UNGATED_FLOOR_IDS``), so there is no pattern to gate on
    # and nothing to report but the id: they run unconditionally, like the
    # git-publish anti-obfuscation branches.  Gating them on a row lookup was the
    # trap this replaces -- ``.get(rule_id)`` returning None fell through to
    # ``continue``, so deleting the row silently disabled the floor.
    for rule_id, predicate in (
        ("self-protection-restart", _argv._is_self_restart),
        ("self-protection-update", _argv._is_self_update),
        ("self-protection-gateway-restart", _argv._is_self_gateway_restart),
        ("self-protection-cloud", _argv._is_self_cloud_destructive),
        ("self-protection-skills-authority-retire", _argv._is_self_skills_authority_retire),
    ):
        if predicate(lower):
            _emit_deny_event(tool_name, rule_id, lower)
            return _reason(
                rule_id,
                _rules._SELF_PROTECTION_FLOOR_NOTES.get(rule_id, ""),
                rule=rule_id,
                component="argv-floor",
            )

    # ── Pattern passes (shared with the synthesized tier's ``segments=True``) ──
    pattern_denial = _deny_pattern_passes(tool_name, lower, all_patterns, _reason)
    if pattern_denial is not None:
        return pattern_denial
    # ── Recursive-force rm floor (argv-structural, UNION with the regex) ──
    # Runs only after the regex tier cleared every view, so whatever a rm row
    # already refused keeps its refusal text and event. It reads every rm argv
    # for recursive + force in any spelling against the root or home dir itself,
    # only for a row still in the effective set, and only when the text could
    # hold an rm at all, so a command with none is never refused by it.
    _rm = _submodule("rm_floor")
    rm_enabled = {
        kind: rule for kind, rule in _rm._RM_FLOOR_BY_KIND.items() if rule[1] in regex_patterns
    }
    if rm_enabled and _rm._can_fire(tool_name):
        note = _rm._RM_FLOOR_NOTE
        try:
            rm_targets = _rm._rm_wipe_targets_nested(tool_name)
        except Exception as exc:  # incl. RecursionError
            # Fail closed for a command naming rm as a word; letters inside
            # another word (``--format``) are not a command the floor reads.
            logger.warning("rm floor could not read the command: %r", exc)
            rm_targets = set(rm_enabled) if _rm._names_rm_word(tool_name) else set()
            note = _rm._RM_FLOOR_UNREAD_NOTE
        for kind in sorted(rm_targets & rm_enabled.keys()):
            rule_id, pattern = rm_enabled[kind]
            # The raw input, not ``lower``: see the audit note below.
            _emit_deny_event(tool_name, pattern, tool_name)
            return _reason(pattern, note, rule=rule_id, component="argv-floor")
    # All windows cleared the deny passes — the input is allowed.  If it was a
    # feature-branch push, emit the deferred allow audit now (final outcome).
    #
    # The RAW input is audited, never ``lower``.  ``lower`` exists for MATCHING;
    # nothing matched on an allow, so the case fold buys the record nothing and
    # costs it two things.  Faithfulness: branch names and remote URLs are
    # case-sensitive, so folding records a push to ``Feature-ABC`` as a push to
    # ``feature-abc``.  And redaction: the credential scrubber inside
    # ``redact_and_truncate`` matches an AWS key ID case-SENSITIVELY on purpose
    # (widening it would false-positive on ordinary prose — ``asia`` is a word —
    # across every egress surface; see ``credential_patterns``), so a key handed
    # in already case-folded slips past the pre-slice redaction, gets cut by the
    # 200-char clip, and the surviving prefix is short enough to escape SEL's own
    # any-case write-path net too — a partial key persisting in the durable log.
    if push_allow_pending:
        _schedule_push_allow_audit(tool_name)
    return None


def _deny_pattern_passes(
    tool_name: str,
    lower: str,
    all_patterns: list[tuple[str, bool]],
    reason: "Callable[[str], str]",
) -> str | None:
    """Run :func:`is_denied`'s whole-string and per-segment passes over *all_patterns*.

    Exceptions and the perm-verb narrowing apply as there; no floor runs here.
    """
    _rules = _submodule("denied_rules")
    _shell = _submodule("shell_normalizer")

    # Memoizes the argv-structural mention walk per view: the same view is asked
    # about once per matching pattern, and every opted-in pattern can match.
    mention_cache: dict[str, bool] = {}

    # ── Pass 1: whole-string deny ──
    # If any pattern matches the full input AND no exception matches the
    # full input, deny outright.  A whole-string match that IS covered by an
    # exception falls through to the per-segment Pass 2 carve-out re-check.
    #
    # The regex tier matches the FULL, untruncated string via ``_DenyMatcher``
    # (linear-time, no length bound — see the ReDoS-mitigation notes above), so
    # a destructive needle at any offset within a single un-separated segment is
    # caught here.  In ``is_denied``, ``_is_git_publish`` / the always-on floors
    # also run on the full string before this call.
    for pattern, is_regex in all_patterns:
        if _rules._deny_pattern_matches(pattern, lower, is_regex):
            whole_string_exception_match = _rules._exception_applies(
                lower, _rules._DENY_EXCEPTIONS.get(pattern, [])
            )
            if not whole_string_exception_match and _perm_verb_mention_narrows(
                pattern, lower, mention_cache
            ):
                # Same shape as the glob exception above: a whole-string carve-out
                # only DEFERS to Pass 2, which re-judges each segment on its own,
                # so an embedded real invocation is still denied there.  The audit
                # is emitted here (and GATES the carve-out) because for a search
                # whose verb and path land in different segments Pass 2 never
                # matches, so this is the only place the decision is recorded.
                if _emit_deny_exception_event(tool_name, pattern, _PERM_VERB_MENTION_MECHANISM):
                    whole_string_exception_match = True
            if not whole_string_exception_match:
                _emit_deny_event(tool_name, pattern, lower)
                return reason(pattern)

    # ── Pass 2: per-segment (re-)evaluation ──
    # Split into segments and check each.  This runs UNCONDITIONALLY: besides
    # the exception-carve-out re-check, splitting isolates an embedded real
    # publish/destructive command (e.g. after ``;`` / ``&&`` / inside
    # ``$(...)``) into its own segment so it matches the deny pattern in its own
    # right (chaining-bypass protection).  Segments that match a deny pattern
    # AND an exception are allowed with a SEL audit event.
    #
    # Each segment is evaluated in every view ``_deny_segment_views`` returns:
    # the RAW text first (identical to what this pass matched before that helper
    # existed), then a quote/escape-normalized re-join of the same segment when
    # it differs.  The second view is what makes a rule authored as a command
    # shape hold under re-spelling — ``rm -rf "/"`` and ``"rm" -rf /`` reach the
    # ``rm -rf /`` rule as the one command they both are.  It is strictly
    # additive: see that helper for why it is per-segment and why a
    # normalization failure cannot widen what is allowed.
    # Segments are split from the ORIGINAL-case input, not from ``lower``, so
    # ``_deny_segment_views`` can decode bash's case-sensitive Unicode escape
    # widths before folding case.  The split is unaffected: ``_split_segments``
    # cuts on ``;`` ``&&`` ``||`` ``|`` newlines and substitution boundaries, none
    # of which any case mapping produces, so splitting-then-lowercasing and
    # lowercasing-then-splitting give the same pieces.
    #
    # Line continuations are folded FIRST, because the split cuts on the newline
    # they contain: without this, ``"r\<newline>m" -rf /`` is severed into two
    # segments and neither contains the command bash actually runs.  The fold is
    # quote-aware (see ``_fold_line_continuations``) -- pass 1 above still matches
    # the completely unfolded text, so this only ever adds reach.
    # The WHOLE command is walked for nested payloads first, with its own re-join
    # suppressed.  ``_split_segments`` is deliberately quote-unaware, so a newline
    # inside a quoted payload severs the command before the payload can be
    # extracted from it -- ``bash -c 'r\<newline>m -rf /'`` arrives as the pieces
    # ``bash -c 'r\`` and ``m -rf /'`` and the ``-c`` script is never seen.
    # Emitting no view for the command itself is what keeps
    # this from fabricating one across its separators.
    folded = _shell._fold_line_continuations(tool_name)
    segments = [seg.strip() for seg in _split_segments(folded)]
    segments = [seg for seg in segments if seg]
    work: list[tuple[str, tuple[str, ...]]] = []
    # The whole-command payload walk is only needed when the split actually SPLIT
    # something.  With a single segment the whole command IS that segment, so
    # walking it twice doubles the payload scan -- which is quadratic in token
    # count inside ``_nested_shell_payloads`` -- for no view the segment walk does
    # not already produce.  Measured: skipping the duplicate halves the cost on a
    # command padded with thousands of interpreter tokens (a stall risk).
    if len(segments) != 1 or segments[0] != folded.strip():
        work.append(("", _deny_segment_views(tool_name, False)))
    for seg_raw in segments:
        work.append((seg_raw.lower(), _deny_segment_views(seg_raw)))
    for seg_lower, segment_views in work:
        for view in segment_views:
            for pattern, is_regex in all_patterns:
                if _rules._deny_pattern_matches(pattern, view, is_regex):
                    if _rules._exception_applies(
                        view, _rules._DENY_EXCEPTIONS.get(pattern, [])
                    ) or _perm_verb_mention_narrows(pattern, lower, mention_cache):
                        # ``lower``, not ``view``: the mention reading is a
                        # WHOLE-COMMAND judgement and a Pass 2 segment is not
                        # always a command.  ``_split_segments`` is deliberately
                        # quote-unaware, so a quoted alternation is severed
                        # mid-literal and the tail arrives looking like an
                        # invocation -- ``rg -n 'chmod|chown' /etc/profile.d``
                        # yields the fragment ``chown' /etc/profile.d``, whose
                        # first word IS the verb.  Judging that fragment refuses
                        # a search that runs nothing.  Nothing is lost by asking
                        # about the whole command instead: the predicate demands
                        # that EVERY occurrence, in the command and in every
                        # nested payload, sit at an argument position, and a
                        # chained real invocation is exactly an occurrence in
                        # program position -- ``_ends_argv`` cuts the argv at
                        # ``;`` ``&&`` ``||`` ``|`` and at a subshell or brace
                        # opener, so the embedded command's own verb leads its
                        # own argv and refuses the whole exemption.  A newline
                        # separator, which ``shlex`` would swallow as
                        # whitespace, is refused outright by the predicate.
                        if not _emit_deny_exception_event(
                            tool_name, pattern, _perm_verb_mechanism_for(pattern)
                        ):
                            _emit_deny_event(tool_name, pattern, view, raw_segment=seg_lower)
                            return reason(pattern)
                        # Exception granted for this pattern on this segment;
                        # continue to evaluate any remaining patterns against
                        # the same segment (a different pattern without an
                        # exception must still cause a deny).
                        continue
                    _emit_deny_event(tool_name, pattern, view, raw_segment=seg_lower)
                    return reason(pattern)
    return None


def is_denied_synthesized_target(
    target: str,
    patterns: list[str] | None = None,
    *,
    extra_patterns: list[str] | None = None,
    reason_notes: dict[str, str] | None = None,
    segments: bool = False,
) -> str | None:
    """Evaluate a SYNTHESIZED target against the patterns that participate in one.

    A synthesized target is not a command line.  It is a ``"<namespace> key=value ..."``
    summary this gate mints from a tool call's structured arguments so a rule can see a
    scope that exists nowhere in text (``hooks._search_deny_target``).  Handing it to
    :func:`is_denied` evaluates it against the WHOLE shared rule set, including the ~140
    command-oriented built-ins -- and those match its path text incidentally: the
    ``mkfs.*`` rule denies a read-only search of a directory named ``mkfs-tests``.  The
    only per-rule remedy is disabling that rule by id, which also stops it protecting
    real shell commands, so the collision costs a real control to clear.

    Which patterns participate: exactly the ones the CALLER passes.  The hooks gate
    passes the operator's own enabled regexes, and the companion overlay is evaluated
    separately and unscoped a layer up (``PolicyAuthority``).  The shipped built-in
    catalogue is NOT passed and takes no part in a synthesized target: a built-in cannot
    express a scope rule for one -- none is authored against the grammar, ratcheted by
    ``test_no_shipped_builtin_is_authored_against_the_grammar`` -- so its only possible
    hit here is the incidental one this tier exists to drop.  A future built-in written
    against the grammar fails that ratchet, which is the signal to give it an explicit
    way in.

    This is deliberately a caller-supplied SET rather than a filter applied here.  An
    earlier revision classified the merged effective set by testing each pattern's text
    against the shipped catalogue, and text cannot answer that question: an operator who
    authors a pattern whose text coincides with a shipped one (``mkfs.*`` is a natural
    thing to type) had their OWN rule read as shipped and dropped -- a silent fail-open on
    an explicit deny.  Pattern text is not provenance.  Passing only what participates
    makes provenance structural: there is nothing left to misclassify.

    What this does NOT run, and why:

    * The argv-structural floors (credential mint, self-kill, restart/update/cloud) and
      the verb-anchored git-publish detector.  Each interprets SHELL SYNTAX, and a
      synthesized target has none: its tokens are the namespace and ``key=value`` pairs,
      values are whitespace-encoded by the synthesizer so one cannot split into two
      tokens, and no such target can name a program.  A search of a tree cannot mint a
      credential or kill a process, so these can only produce false positives here.  A
      real command still reaches them through its own ``command`` target.
    * Per-segment (pass 2) re-evaluation.  Segment splitting exists to isolate a chained
      command inside one shell line; a synthesized target has no chaining semantics, so
      splitting it only manufactures pseudo-commands out of path substrings -- the same
      collision class, one layer down.

    ``segments=True`` is for a shell title kiro-cli cut short, judged as SENT by the
    operator's own rules (``hooks.untruncated_shell_title``).  That text is a command
    line and a rule may be anchored to one chained command of it, so both of
    :func:`is_denied`'s pattern passes run (``_deny_pattern_passes``); the floors,
    which misread the cut end, still do not.

    Args:
        target: The synthesized target, e.g. ``"file-search path=/srv max_depth=3"``.
        patterns: Regex-tier patterns that participate (the operator's own).  ``None``
            or empty means the regex tier contributes nothing -- NOT that it falls back
            to every built-in, which would be the opposite of this tier's contract.
        extra_patterns: Glob-tier patterns that participate (``auto_deny_tools``).
        reason_notes: Optional ``{pattern: operator note}`` map, presentation only.
        segments: Also run the per-segment pass (see above).  Default ``False``.

    Returns:
        Denial reason string (mentioning the matched pattern), or ``None`` if allowed.
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _denied_rules = _submodule("denied_rules")

    lower = target.lower()
    all_patterns: list[tuple[str, bool]] = [(p, True) for p in list(patterns or [])] + [
        (p, False) for p in list(extra_patterns or [])
    ]
    if segments:
        return _deny_pattern_passes(
            target,
            lower,
            all_patterns,
            lambda matched: _denied_rules._deny_reason(matched, reason_notes),
        )
    for pattern, is_regex in all_patterns:
        if not _denied_rules._deny_pattern_matches(pattern, lower, is_regex):
            continue
        # No ``_DENY_EXCEPTIONS`` carve-out here.  That map ships EMPTY and its machinery
        # is retained in ``is_denied`` only for a future scoped exception, so replicating
        # it here would be dead symmetry.  If it ever gains an entry, this tier has to be
        # revisited deliberately -- ``test_the_deny_exception_map_is_still_empty`` reddens
        # then, so the omission cannot become a silent gap.
        _emit_deny_event(target, pattern, lower)
        return _denied_rules._deny_reason(pattern, reason_notes)
    return None


def _split_segments(command_lower: str) -> list[str]:
    """Split a command into independently-evaluatable segments.

    Splits on shell separators and command-substitution boundaries.
    Returns the list of segments (which may include the empty string for
    adjacent separators; callers should skip empties).
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _shell_normalizer = _submodule("shell_normalizer")

    return _shell_normalizer._CMD_SPLIT_RE.split(command_lower)


def _emit_deny_event(
    tool_name: str, deny_pattern: str, segment: str, raw_segment: str = ""
) -> None:
    """Emit a SEL audit event when a command is denied.

    Records the operation, matched pattern, and (for pass-2 denials) the
    specific segment that triggered the block.  This satisfies the
    security-controls guideline that every permission decision — both
    grants and denials — must produce an audit trail.

    *raw_segment* is the segment's UNNORMALIZED text, recorded as a separate
    ``raw_segment`` field when it differs from *segment*.  A pass-2 match can now
    come from a quote-normalized view (``_deny_segment_views``), and the view is
    the more useful thing to show — it names the command that would have run —
    but the evasion is only visible in the spelling the caller actually
    submitted, so forensics needs both.  The full raw input is already carried in
    ``operation``; this pins WHICH segment of it normalized into the match, which
    a multi-segment command otherwise leaves the reader to re-derive.  Omitted
    when the two are equal, so an ordinary denial's event does not grow.

    Best-effort: SEL logging failures are logged at WARNING and do not
    affect the deny decision (denials are inherently fail-closed; the
    block stands regardless of audit success).
    """
    try:
        sel = SecurityEventLog()
        # ``redact_and_truncate``, never a bare slice: it redacts over the FULL text
        # BEFORE cutting, which is the rule that function exists to enforce -- a
        # credential straddling the 200-char boundary would otherwise be cut in half,
        # and the fragment fails to match the credential pattern, so SEL's own
        # write-path redaction cannot catch it and the partial secret persists in a
        # dashboard-readable log.  Both fields take it: a bare slice carries the
        # same hazard in either one.
        metadata = {
            "deny_pattern": deny_pattern,
            "segment": redact_and_truncate(segment, 200) if segment else "",
            "mechanism": "BUILTIN_DENY_PATTERNS",
        }
        if raw_segment and raw_segment != segment:
            metadata["raw_segment"] = redact_and_truncate(raw_segment, 200)
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="deny_event",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation=tool_name,
                outcome="denied",
                resources=f"deny_pattern={deny_pattern}",
                metadata=metadata,
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for deny_event on %r (deny stands)",
            tool_name,
            exc_info=True,
        )


#: SEL ``mechanism`` value for the argv-structural inert-mention narrowing.
#: Distinct from the glob carve-out map's ``_DENY_EXCEPTIONS`` so the audit trail
#: can tell the two apart -- they share one emitter and nothing else.
_PERM_VERB_MENTION_MECHANISM = "_PERM_VERB_MENTION"


def _perm_verb_mechanism_for(pattern: str) -> str:
    """The SEL mechanism name for the exception about to be granted.

    Pass 2 reaches the emitter from a condition that is an OR of the glob
    carve-out and the mention reading, so the branch alone cannot say which one
    fired.  The mention reading is the narrower of the two -- it applies only to
    the opted-in patterns -- so membership in that set decides the label.
    """
    if pattern in _submodule("denied_rules")._PERM_VERB_MENTION_PATTERNS:
        return _PERM_VERB_MENTION_MECHANISM
    return "_DENY_EXCEPTIONS"


def _perm_verb_mention_narrows(
    pattern: str,
    view: str,
    cache: dict[str, bool],
) -> bool:
    """Whether *pattern* is narrowed away on *view* by an inert-mention reading.

    A thin adapter over :func:`~.perm_verb_mention._perm_verb_mention_only`: it confines
    the narrowing to the catalog patterns that opt into it, and memoizes the
    argv walk per view.  The walk descends every nested payload, so it is the
    expensive half of this check while the membership test is a set lookup --
    and ``is_denied`` asks the same question once per matching pattern.

    Returns False for every other pattern, which is what keeps this from
    touching any rule outside the derived set.  The set's size is deliberately
    not written down here: it is derived from the catalog, so a count in prose
    goes stale the next time a row is added or renamed.
    """
    if pattern not in _submodule("denied_rules")._PERM_VERB_MENTION_PATTERNS:
        return False
    verdict = cache.get(view)
    if verdict is None:
        verdict = _submodule("perm_verb_mention")._perm_verb_mention_only(view)
        cache[view] = verdict
    return verdict


def _emit_deny_exception_event(
    tool_name: str,
    deny_pattern: str,
    mechanism: str = "_DENY_EXCEPTIONS",
) -> bool:
    """Emit an SEL audit event when a deny exception is applied.

    Returns True if the event was logged successfully, False otherwise.
    The caller must NOT grant the exception if this returns False.

    *mechanism* names WHICH narrowing granted the exception.  Two unrelated ones
    reach this emitter -- the glob carve-out map and the argv-structural
    inert-mention reading -- and recording both under one value makes the audit
    trail unable to answer "why was this allowed", which is the only question it
    exists for.  The default keeps the glob path's historical value.
    """
    try:
        sel = SecurityEventLog()
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="deny_exception",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation=tool_name,
                outcome="allowed",
                resources=f"deny_pattern={deny_pattern}",
                metadata={"deny_pattern": deny_pattern, "mechanism": mechanism},
            )
        )
        return True
    except Exception:
        logger.warning(
            "SEL audit failed for deny_exception — denying %r (fail-closed)",
            tool_name,
            exc_info=True,
        )
        return False


def audit_injection_dropped(
    *,
    surface: str,
    session_key: str = "",
    channel_id: str = "",
    thread_ts: str = "",
    agent: str = "kirocrew",
    sample: str = "",
) -> None:
    """Emit an SEL audit event when injection-screened content is dropped.

    Called when :func:`contains_injection` flags untrusted external content
    (e.g. a Slack thread-parent message or thread metadata authored by a
    non-owner) and the content is dropped before reaching the LLM prompt
    Recording the attempt keeps prompt-injection attempts
    visible in the audit trail rather than silently discarded.

    Best-effort: an SEL logging failure is logged at WARNING and never
    propagates — the content is dropped regardless of audit success, so this
    cannot break prompt building.
    """
    try:
        SecurityEventLog().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="prompt_injection_dropped",
                caller_identity=session_key,
                agent=agent,
                source="context",
                operation=surface,
                outcome="dropped",
                resources=f"channel_id={channel_id} thread_ts={thread_ts}",
                metadata={
                    "surface": surface,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "sample": redact_and_truncate(sample, 200),
                    "mechanism": "contains_injection",
                },
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for prompt_injection_dropped on %r (content still dropped)",
            surface,
            exc_info=True,
        )
