"""Argv-structural floor for recursive-force ``rm`` deletion of root / home.

Split out of ``argv_floor.py`` as a cohesive sibling: this module owns the
recursive-force ``rm`` deny floor and nothing else. ``argv_floor.is_denied``'s
caller reaches it through :func:`_recursive_force_rm_targets` (and the
fail-closed fallback), which read only the ``rm`` command's OWN argv and return
the catastrophic target set ``{"root", "home"}`` a command deletes.

This floor is a UNION with the two ``rm`` catalog regexes, which stay LIVE in
the ``re`` deny tier as a fail-closed net: the regex catches a quoted payload
this floor's own-argv model cannot reach (``su -c "rm -rf /"``, ``eval``), while
this floor adds the structural flag/target-spelling coverage. The regex's one
false positive — a ``grep``-family search that merely names the literal — is
narrowed by the ``_DENY_EXCEPTIONS`` grep inert-search carve-out.
"""

from __future__ import annotations

import os as _os
import posixpath as _posixpath
import re
import sys as _sys

from . import shell_normalizer as _shell_normalizer
from .shell_normalizer import (
    _DATA_CONSUMER_PROGRAMS,
    _data_consumer_exempt,
    _decode_shell_quoted_literals,
    _iter_shell_chars,
    _opens_comment,
    _program_basename,
    _shell_payload_walk,
    _ShellChar,
    _split_shell_words,
    _substitution_bodies,
)

# ── Recursive-force ``rm`` deletion floor ──
# ``rm`` recursively force-deleting ROOT or HOME is catastrophic; a path UNDER
# either stays allowed. Catalog literals matched one flag spelling; a REGEX
# widening fails (can't see flags after the operand; fires on a SUBSTRING). The
# sound closure is argv-STRUCTURAL and EXACT: read the ``rm``'s OWN argv, deny only
# when a resolved operand IS root or home itself. UNION with the two catalog
# regexes (kept LIVE). Tokens RAW/ENV-UNEXPANDED, so home is by spelling (expanding
# reads ``$HOME`` as ROOT). PROGRAM ``rm`` only.


#: ``rm``'s long options, so an abbreviation can be tested for ambiguity. GNU
#: ``getopt_long`` accepts any UNAMBIGUOUS prefix, so ``rm --rec …`` / ``rm --for
#: …`` run the identical recursive/force delete a fixed string compare would miss.
#: A prefix is honoured only when it matches exactly ONE option — ``--r`` ->
#: ``--recursive``, ``--f`` -> ``--force`` — never a prefix shared by two.
_RM_LONG_OPTIONS: tuple[str, ...] = (
    "--recursive",
    "--force",
    "--dir",
    "--interactive",
    "--no-preserve-root",
    "--one-file-system",
    "--preserve-root",
    "--verbose",
    "--help",
    "--version",
)


def _rm_long_option_resolves_to(tok: str, target: str) -> bool:
    """Whether *tok* is an unambiguous long-option abbreviation of *target*.

    *tok* must be ``--`` followed by a NON-EMPTY prefix (``--`` alone is the
    end-of-options marker, handled elsewhere), and among ``rm``'s long options
    exactly one must start with that prefix, and it must be *target*. An exact
    spelling is trivially unambiguous. GNU stops at the first ``=`` (``--rec=…``),
    so the option name is taken up to it.
    """
    if not tok.startswith("--") or tok == "--":
        return False
    name = tok[: tok.index("=")] if "=" in tok else tok
    matches = [opt for opt in _RM_LONG_OPTIONS if opt.startswith(name)]
    return matches == [target] or (target in matches and name == target)


#: Whether an ``rm`` argument token carries the recursive flag: the long option
#: ``--recursive`` (or an unambiguous prefix of it), or a single-dash short
#: cluster containing ``r`` (``-r`` / ``-rf`` / ``-fr`` / ``-rfv`` …). A ``--``
#: long option is never read as a short cluster, so ``--force`` is not recursive.
#: The cluster must consist ONLY of rm's real short-option letters (``r f i v d``;
#: ``R``/``I`` fold to ``r``/``i`` in the lowercased view) -- otherwise a FOREIGN
#: single-dash predicate that merely contains ``r`` (find's ``-newer``, ``-regex``,
#: a flattened substitution body) is not misread as recursive, so a legit
#: ``rm -f $(find ~ -newer …)`` stays allowed.
def _rm_is_recursive_flag(tok: str) -> bool:
    if tok.startswith("--"):
        return _rm_long_option_resolves_to(tok, "--recursive")
    return bool(re.fullmatch(r"-[rfivd]*r[rfivd]*", tok))


#: Whether an ``rm`` argument token carries the force flag (``--force`` or an
#: unambiguous prefix of it, or a single-dash short cluster containing ``f``).
def _rm_is_force_flag(tok: str) -> bool:
    if tok.startswith("--"):
        return _rm_long_option_resolves_to(tok, "--force")
    return bool(re.fullmatch(r"-[rfivd]*f[rfivd]*", tok))


def _rm_ends_argv_structural(token: str) -> bool:
    """``_ends_argv``'s STRUCTURAL boundaries only -- a ``#`` comment or a bare
    ``(`` / ``{`` / ``x()`` function-body opener -- WITHOUT its quote-unaware
    ``";"/"|" in token`` substring test.

    The flag precheck already finds an unquoted control operator with the
    quote-aware :func:`_rm_unescaped_boundary`, so the only extra boundary this
    fallback must add is a structural opener. Delegating to ``_ends_argv`` instead
    re-introduced the quote-unaware test, which broke the span at a QUOTED ``;``
    (``rm 'a;b' -fr $HOME``) before the recursive-force flag and failed open.
    A real structural opener never appears inside a quoted
    operand, so this stays safe where ``_ends_argv`` did not.
    """
    if token.startswith("#"):
        return True
    return token.rstrip("{") in {"", "("} or token.rstrip("{").endswith("()")


def _rm_token_ends_argv(token: str) -> bool:
    """The ONE quote-aware command-boundary test for every rm-floor scan and
    fallback. A token ends the current ``rm`` argv when it carries an UNQUOTED
    control operator (``;`` / ``&`` / ``|`` / newline / ``)``), via the quote-aware
    :func:`_rm_unescaped_boundary`, OR is a structural opener (``#`` comment, bare
    ``(`` / ``{`` / ``x()``), via :func:`_rm_ends_argv_structural`.

    ``shell_normalizer._ends_argv`` tests ``";"/"|" in token`` as a raw SUBSTRING,
    so a QUOTED ``'a;b'`` operand ended the scan before a later home/root target and
    failed open -- the same quote-boundary bug fixed at three sites. Routing every
    boundary test through this helper closes it everywhere at once.

    A ``)`` that closes a command SUBSTITUTION in the token (``$(…)`` / backtick) is
    NOT an argv boundary: the substitution is one outer word whose body is a
    separate frame, and the ``rm`` argv continues after it (``rm -fr $(true) ~``
    still reaches ``~`` -- treating the ``)`` as a boundary ended the argv and let a
    trailing home/root operand slip, GPT 6.1 fail-open). A BARE subshell closer
    (``(rm -fr /)`` -> word ``/)`` with no ``$(`` opener) still ends the argv, so
    root in a bare subshell cannot hide."""
    word_has_cmdsub = "$(" in token or "`" in token
    return _rm_unescaped_boundary(
        token, treat_subshell_closer=not word_has_cmdsub
    ) is not None or _rm_ends_argv_structural(token)


def _rm_program_basename(token: str) -> str:
    """Program basename that also resolves ADJACENT-QUOTE concatenation.

    Bash fuses adjacent quoted/unquoted runs within one word, so ``r''m`` /
    ``r""m`` / ``"r"m`` is the program ``rm`` -- a split spelling of it. The shared
    ``_program_basename`` strips only SURROUNDING quotes (``"rm"`` -> ``rm``) and
    leaves ``r''m`` intact, so a split program word slipped the executed-``rm``
    classification and a home/root wipe written ``r''m -fr "$HOME"`` was allowed
    (GPT 6.1, security-class). When the shared basename is not already ``rm``, also
    read the fully quote-concatenated spelling and prefer it when it reforms to the
    program we gate on. The substitution/redirect peeling the shared helper does on
    the raw token is kept (so ``$(which rm)`` still resolves) by trying it first.
    """
    base = _program_basename(token)
    if base == "rm":
        return base
    concatenated = _program_basename(_rm_strip_all_quotes(token))
    return concatenated if concatenated == "rm" else base


def _rm_argv_programs(tokens: "list[str]") -> "list[str]":
    """Quote-aware twin of ``shell_normalizer._argv_programs``: for each token, the
    program name of the command it belongs to, with command boundaries read by the
    single quote-aware :func:`_rm_token_ends_argv`.

    The shared helper ends a command at ``_ends_argv``'s quote-UNAWARE ``;``/``|``
    substring test, so a quoted separator in a PRINTED argument (``echo 'cleanup;
    then' sudo rm -fr /``) was read as a command boundary -- the following ``sudo``
    became a program word and ``rm``'s parent, so an ``echo`` that only prints text
    was refused as a wipe (GPT 6.1, security-class over-refusal). Routing program
    ownership through the floor's own boundary keeps a quoted ``;`` as data.
    """
    programs: list[str] = []
    current = ""
    expect_program = True
    for token in tokens:
        if expect_program and token and not _shell_normalizer.ENV_ASSIGNMENT_RE.match(token):
            current = _rm_program_basename(token)
            expect_program = False
        programs.append(current)
        if _rm_token_ends_argv(token):
            current = ""
            expect_program = True
    return programs


def _rm_substitution_depth_delta(token: str) -> int:
    """Quote-aware net change in command-substitution nesting for *token*.

    Like ``shell_normalizer._substitution_depth_delta`` but counts a ``$(`` /
    backtick / ``)`` ONLY where it is UNQUOTED, so a literal paren inside a quoted
    operand (``'a)b'``) does not look like a substitution closer and end the ``rm``
    argv scan before a later target (GPT 6.1: ``rm -fr 'a)b' ~`` was allowed because
    the quoted ``)`` dropped the span depth below zero). The shared helper counts
    raw characters and its own docstring notes it cannot tell a quoted paren apart;
    this one walks the token's quote state. Mirrors the shared formula
    ``count("$(") + count("`")//2 - count(")")`` over UNQUOTED characters only."""
    dollar_paren = backtick = close_paren = 0
    i = 0
    n = len(token)
    in_single = in_double = False
    while i < n:
        ch = token[i]
        if ch == "\\" and not in_single and i + 1 < n:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if in_single or in_double:
            i += 1
            continue
        if ch == "$" and i + 1 < n and token[i + 1] == "(":
            dollar_paren += 1
            i += 2
            continue
        if ch == "`":
            backtick += 1
        elif ch == ")":
            close_paren += 1
        i += 1
    return dollar_paren + backtick // 2 - close_paren


def _rm_span_is_recursive_force(tokens: "list[str]", rm_index: int) -> bool:
    """Cheap pre-check: does the ``rm`` span starting after *rm_index* carry a
    RECURSIVE and a FORCE flag (any spelling/order), or ``--no-preserve-root``?

    Only such a span can be a catastrophic wipe AND is the shape whose per-span
    suffix re-scan is expensive, so the per-argv span cap counts only
    these — a long chain of benign ``rm <file>`` commands (no ``-rf``) neither
    charges the budget nor is failed-closed past it.
    Scans only this one span's flag words (until the next command
    boundary); flags are tested on the de-quoted spelling bash acts on, matching
    the structural parse below. Does not resolve brace expansion — a brace-grouped
    flag word is a catastrophic candidate, so an unparsed brace token conservatively
    counts as recursive-force to stay fail-closed on it.
    """
    has_rec = has_force = False
    j = rm_index + 1
    n = len(tokens)
    while j < n:
        raw = tokens[j]
        glued_prefix = ""
        # A GLUED separator (``-fr;`` / ``-fr&&``) carries the flag BEFORE the
        # operator; classify that prefix, then stop -- otherwise ``rm ~ -fr; true``
        # breaks before the recursive-force flag is seen and the wipe fails open.
        # ``_rm_unescaped_boundary`` finds an UNQUOTED ``;`` / ``&`` /
        # ``|`` glued mid-token (quote-aware), which ``_ends_argv`` misses for a
        # glued ``&&``. Both boundary tests must be quote-aware: ``_ends_argv``'s
        # ``";"/"|" in token`` substring test fires on a QUOTED separator (``'a;b'``)
        # and breaks the span before a later ``-fr`` + ``$HOME``, failing open.
        # ``_rm_unescaped_boundary`` already covers the operators quote-aware,
        # so the fallback here is only ``_ends_argv``'s STRUCTURAL openers (a ``#``
        # comment, a bare ``(`` / ``{`` / ``x()`` function body) -- none of which a
        # quoted operand produces.
        # A ``)`` closing a command SUBSTITUTION in this word (``$(…)`` / backtick)
        # is NOT an argv boundary: ``rm $(true) -fr "$HOME"`` must keep scanning past
        # the substitution to see ``-fr`` (treating the ``)`` as a boundary stopped
        # the precheck before the recursive-force flag and the wipe failed open --
        # GPT 6.1). A bare subshell closer still ends the scan.
        _raw_has_cmdsub = "$(" in raw or "`" in raw
        bidx = _rm_unescaped_boundary(raw, treat_subshell_closer=not _raw_has_cmdsub)
        if bidx is not None:
            if bidx > 0:
                glued_prefix = raw[:bidx]
            if not glued_prefix:
                break
        elif _rm_ends_argv_structural(raw):
            break
        tok = _rm_strip_all_quotes(glued_prefix or raw)
        if tok == "--":
            break
        if tok == "--no-preserve-root":
            return True
        if "{" in tok and "," in tok:
            return True  # a brace-grouped flag word — classify it (fail-closed)
        if _rm_is_recursive_flag(tok):
            has_rec = True
            if _rm_is_force_flag(tok):
                has_force = True
        elif _rm_is_force_flag(tok):
            has_force = True
        if has_rec and has_force:
            return True
        if glued_prefix:
            break  # classified the prefix; the separator ends the span
        j += 1
    return has_rec and has_force


#: The filesystem ROOT ITSELF — ``/`` (a run of slashes) or ``/*``, optional
#: trailing slash, NOTHING under it. For an ``rm`` reached through an EXEC WRAPPER
#: (``setsid rm -rf /``, ``sudo …``): base caught a wrapper-reached descendant only
#: incidentally, so denying those newly refuses benign work (``docker exec kc-ci rm
#: -fr /tmp/build-cache`` — base ALLOWED it). Wrapper denies root
#: ITSELF only. The child glob accepts a RUN of stars (``/**`` / ``/***``), which
#: bash expands over the same children as ``/*`` (GPT 6.1).
_RM_ROOT_ITSELF_RE = re.compile(r"/+(?:\*+/*)?")
#: The HOME dir ITSELF — ``~`` / ``$HOME`` / ``${HOME}``, bare or with trailing
#: slashes (``~//``) or the ``~/*`` glob. The ``${home}`` form also admits a
#: slash-only suffix removal (``${HOME%/}``), the ``:?`` check (``${HOME:?msg}``),
#: the identity substring ``${HOME:0}``, and a
#: DEFAULT-VALUE form ``${HOME:-x}`` / ``${HOME:=x}`` / ``${HOME-x}`` / ``${HOME=x}``
#: — HOME is always set, so the default never fires and the shell supplies the real
#: home. Other value-CHANGING operators (``:+``, non-zero substring)
#: stay out. Wrapper only.
_RM_HOME_ITSELF_RE = re.compile(
    r"(?:~|\$\{home(?:%%?/*|:\?[^}]*|:[ \t]*0+[ \t]*|:?[-=][^}]*)?\}|\$home(?![a-z0-9_]))(?:/+(?:\*+/*)?)?",
    re.IGNORECASE,
)
#: Escape / quote / substitution characters that can reconstruct the ``rm``
#: program name from text that does not contain the literal ``rm`` (a folded
#: ``"r\<nl>m"``, an octal ``$'r\555'``). The cheap pre-filter admits a command
#: carrying any of these so the walk gets a chance to decode it.
_RM_OBFUSCATION_MACHINERY_RE = re.compile(r"[\\$`'\"]")
#: A brace group ``{…}`` (one level, no nested ``{``/``}``). Strips groups from a
#: word so the surviving literal frame can be tested for a root/home prefix.
_RM_BRACE_GROUP_RE = re.compile(r"\{[^{}]*\}")
#: A numeric / single-char brace RANGE body (``000..511``, ``a..z``, ``1..9..2``):
#: it expands only to digits/letters, never a ``/`` / ``~`` / empty member.
_RM_BRACE_RANGE_RE = re.compile(
    r"\A(-?[0-9]+|[A-Za-z])\.\.(-?[0-9]+|[A-Za-z])(?:\.\.(-?[0-9]+))?\Z"
)

#: Ceiling on nested-frame descents per classification. Each ``find -exec`` /
#: ``sh -c`` / interpreter span recurses into :func:`_rm_targets_in_argv`, so a
#: crafted nest would fan out and hang the gate. A mutable cell decremented
#: per descent; at zero no span opens, so work is linear. Fails SAFE: a real
#: ``rm`` at any reachable depth is classified before the cap bites.
_RM_DESCENT_BUDGET = 64

#: How many ``rm`` command spans one argv classifies before it stops. Each
#: leading ``rm`` re-scans its operand suffix, so an argv padded with thousands of
#: ``rm`` words is quadratic (measured 44 s, past the 25 s gate deadline).
#: A real command has a handful, so this never trims a legitimate one; past
#: it the scan FAILS CLOSED (returns both targets) because the whole-text net
#: catches only the contiguous spelling.
_RM_CLASSIFY_SPAN_CAP = 64

#: Ceiling on how many operands ONE ``rm`` span's structural pass classifies. The
#: pass runs several O(operands) candidate scans, so a single span with hundreds of
#: operands (a bare-``rm`` flood) is linear per scan and stalls the gate under
#: coverage instrumentation. Past this the cheap per-span root/home-shape verdict is
#: used instead -- it reads the operands once with no candidate machinery. Set well
#: above any real recursive-force ``rm`` (a handful of targets); only a flood hits it.
_RM_SPAN_OPERAND_CAP = 128

#: Shared ceiling on tokens classified across all frames PAST the frame budget. A
#: deep nested wipe (few tokens per frame) is still reached; a wide flood of long
#: suffix frames stops once spent. Bounds the past-budget flat scan to O(1) total.
_RM_FLAT_TOKEN_BUDGET = 4096

#: Cumulative byte ceiling on brace members materialized across ALL ``rm`` spans
#: of one argv. The per-WORD ``_BRACE_EXPANSION_CAP`` bounds the member COUNT of a
#: single word and ``_RM_CLASSIFY_SPAN_CAP`` bounds how many spans are classified,
#: but neither bounds the BYTES a word's members carry: a word AT the count cap
#: whose members are each large (``"a"*16000 + "{a,b}"*8`` -> 256 members x ~16 KB)
#: re-materialized once per span still costs ~116 CPU s across 64 spans, past the
#: 25 s gate watchdog. Bound the total expanded bytes BEFORE
#: materialization; once exhausted a span stops expanding and falls back to the
#: CHEAP per-span root/home-shape verdict (``span_overflow_targets``), already
#: proven not to newly refuse a legitimate descendant cleanup.
_RM_EXPANSION_BYTE_BUDGET = 1_000_000

#: Max substitution openers (``$(`` / backtick) the structural walk runs on. Each
#: seeds a frame, so a chain (``"$(true" * 199``) makes the eager frame walk
#: O(openers²) and ran ~24s on the gate -- past the 25s loop-stall watchdog. Beyond
#: this the expensive recursive descent is skipped: the heavy path classifies the
#: top-level per-command argv AND does ONE bounded level of descent into the trailing
#: unbalanced ``$(`` (``_rm_last_unbalanced_substitution_tail``), so an ``rm`` wipe
#: hidden after a long opener run is still caught -- so the lower cap stays fail
#: closed. Set below any real command's substitution count (a few dozen); deeper
#: nesting past the one descent level is a residual left to the whole-text regex net
#: and the sandbox.
_RM_SUBSTITUTION_OPENER_CAP = 48

#: Look-ahead window for rejoining a ``${…}`` parameter expansion that spans
#: shlex tokens. A real multi-token ``${HOME}`` closes within a word or two; a long
#: run of UNBALANCED ``'${x'`` tokens never closes, and scanning to the end from
#: every opener is O(tokens**2) -- enough text starves the tool gate. Bounding the
#: look-ahead keeps the rejoin linear; an opener that does not close within the
#: window is left as a single token, which only UNDER-joins (an unbalanced ``${``
#: is not a root/home target), so the bound is fail closed.
_RM_PARAM_REJOIN_WINDOW = 64

# Innermost ``$(...)`` or backtick body -- a span containing NO further opener, so
# a flat regex sees it without the quote-aware paren walk (which a dense
# ``"$(true)"`` run defeats, leaving the real ``$(rm -fr ~)`` unparsed and a home
# wipe failing open). Peeling innermost spans repeatedly
# reaches nested ones too, under a bounded budget.
_RM_INNERMOST_SUBST_RE = re.compile(r"\$\(([^()`]*)\)|`([^`]*)`")

# How many innermost substitution bodies the heavy path classifies. Unlike the
# recursive frame walk (whose cost is O(openers²), hence the small
# ``_RM_DESCENT_BUDGET``), this scan is a single linear ``finditer`` + a cheap argv
# check per body, so the cap can be generous: the real wipe can sit past hundreds
# of decoy ``"$(true)"`` spans. The cap only bounds a
# truly pathological input; a wipe past it is still caught by the fail-closed
# whole-text regex.
_RM_SUBST_BODY_SCAN_CAP = 20000


def _rm_last_unbalanced_substitution_tail(text: str) -> "str | None":
    """The text after the LAST ``$(`` opener that is never closed, or ``None``.

    A dense run of unbalanced ``$(`` openers (``echo "$(true" * 199 rm -fr ~``)
    opens nested command substitutions bash runs the trailing command inside; the
    innermost-body regex matches only BALANCED ``$(…)`` and so misses it. This finds
    the deepest still-open ``$(`` by a single left-to-right paren-depth pass
    (single-quoted spans are skipped, since ``$(`` is literal there) and returns the
    tail from just after it, so the caller can classify that tail as ONE command.
    One level only, O(len(text)); deeper nesting is a residual left to the whole-text
    regex net and the sandbox."""
    depth = 0
    stack: "list[int]" = []
    i = 0
    n = len(text)
    in_single = False
    while i < n:
        ch = text[i]
        if ch == "'" and not in_single:
            in_single = True
            i += 1
            continue
        if ch == "'" and in_single:
            in_single = False
            i += 1
            continue
        if in_single:
            i += 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            depth += 1
            stack.append(i + 2)
            i += 2
            continue
        if ch == ")" and depth > 0:
            depth -= 1
            stack.pop()
            i += 1
            continue
        i += 1
    if not stack:
        return None
    return text[stack[-1] :]


def _rm_operand_before_boundary(operand: str) -> "tuple[str, bool]":
    """The operand text up to its first unquoted control-operator boundary.

    Returns ``(head, ended)``: *head* drops everything from the first ``;`` / ``&``
    / ``|`` / newline onward; *ended* is True when one was present. Quotes are
    already resolved by the time tokens reach here, so a remaining operator is a
    real separator — ``rm -rf /;reboot`` is one operand ``/;reboot`` whose target
    is ``/``; splitting classifies ``/`` and ends the argv so a glued command
    cannot hide it.
    """
    match = _rm_unescaped_boundary(operand)
    if match is None:
        return operand, False
    return operand[:match], True


def _rm_amp_is_redirection(text: str, idx: int) -> bool:
    """True if the ``&`` at *idx* is part of a REDIRECTION, not a command separator.

    Bash's ``&`` backgrounds / separates a command, but inside a redirection it
    duplicates a file descriptor and does NOT end the command:

    * ``N>&M`` / ``N<&M`` (``2>&1``, ``1>&2``, ``<&3``) -- the ``&`` follows a
      ``>`` / ``<`` (after an optional fd digit), so the char immediately before it
      is ``>`` or ``<``.
    * ``&>`` / ``&>>`` (``&>/dev/null``) -- the ``&`` is immediately followed by
      ``>``.

    Treating such an ``&`` as a separator split ``rm -fr 2>&1 ~`` at the ``&`` and
    dropped the trailing ``~`` home operand (GPT 6.1, security-class). A real
    backgrounding ``&`` (``rm -fr x & rm -fr ~``) has neither neighbour and still
    ends the command. The ``&&`` AND-operator is a separator too and is not a
    redirection: its neighbour is another ``&``, not ``>`` / ``<``.

    The other redirection forms the shell writes -- ``>``, ``>>``, ``<``, ``<<``,
    ``2>``, ``1>`` -- contain no ``;`` / ``&`` / ``|`` character, so they never look
    like a separator to the single boundary scan and need no special case here; only
    the fd-duplicating ``&`` is ambiguous.
    """
    prev_ch = text[idx - 1] if idx > 0 else ""
    next_ch = text[idx + 1] if idx + 1 < len(text) else ""
    return prev_ch in (">", "<") or next_ch == ">"


#: A standalone shell REDIRECTION operator word: ``<`` ``>`` ``>>`` ``<<`` ``<<<``
#: ``<>`` ``>|``, optionally fd-prefixed (``2>``, ``1>>``, ``2>&1``, ``<&3``). The
#: word that FOLLOWS a bare one is a redirect source/target the shell opens, not an
#: operand ``rm`` deletes, so the floor must skip it (``rm -fr x < /`` reads ``/``).
_RM_REDIRECT_OPERATOR_RE = re.compile(r"\A\d*(?:<<<|<<|<>|<&|>&|>>|>\||<|>)")


def _rm_is_redirect_operator(token: str) -> bool:
    """True when *token* begins with a shell redirection operator (optionally
    fd-prefixed) and carries no other operand text beyond an fd-dup target, so the
    whole word is a redirection, not a path."""
    m = _RM_REDIRECT_OPERATOR_RE.match(token)
    if m is None:
        return False
    rest = token[m.end() :]
    # A pure operator (``<``, ``2>``) or an fd-dup with the target glued
    # (``2>&1``, ``<&3``, ``>&-``) is a redirection word. Anything else glued on is
    # the target path (``>/dev/null``), handled by the glued-target check.
    return rest == "" or rest.isdigit() or rest == "-" or ("&" in m.group())


def _rm_redirect_has_glued_target(token: str) -> bool:
    """True when a redirect operator word already carries its target glued on
    (``>/dev/null``, ``2>&1``, ``<&3``), so the NEXT word is not its target."""
    m = _RM_REDIRECT_OPERATOR_RE.match(token)
    if m is None:
        return False
    rest = token[m.end() :]
    return rest != "" or "&" in m.group()


def _rm_rejoin_param_expansions(tokens: "list[str]") -> "list[str]":
    """Re-join tokens that word-splitting tore out of a balanced ``${...}``.

    A ``${HOME:?word}`` whose body has interior whitespace (``${HOME:?must be set}``)
    is split by ``_split_shell_words`` into several tokens, so the home operand is no
    longer one word and the home matcher misses it -- a home wipe written
    ``rm -fr ${HOME:?a b}`` was allowed (GPT 6.1, security-class). The shell keeps a
    parameter expansion as part of ONE word, so re-join a token that opens an
    unbalanced ``${`` with the following tokens (space-joined, the splitter's own
    separator) until the braces balance. An expansion that never closes is left as
    the single opening token, so this cannot swallow the rest of the argv.
    """

    def _net_brace_delta(tok: str) -> int:
        depth = 0
        i = 0
        n = len(tok)
        while i < n:
            c = tok[i]
            if c == "\\":
                i += 2
                continue
            if c == "{" and i > 0 and tok[i - 1] == "$":
                depth += 1
            elif c == "}" and depth:
                depth -= 1
            i += 1
        return depth

    out: "list[str]" = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if "${" in tok and _net_brace_delta(tok) > 0:
            # Re-join following tokens until the ``${`` braces balance, but bound the
            # look-ahead: a real ``${…}`` closes within a word or two, while a long
            # run of UNBALANCED ``'${x'`` tokens never closes and re-scanning the
            # whole growing join from every opener is O(n**2) -- enough text starves
            # the tool gate. Scanning a bounded window is linear in n overall; an
            # opener that does not balance within the window is left as one token
            # (fail closed -- an unbalanced ``${`` is not a root/home target). The
            # join uses the running window, not a per-token delta sum: a ``}`` that
            # closes a brace opened in an EARLIER token contributes nothing when a
            # token is measured in isolation, so only the joined view is correct.
            limit = min(n, i + 1 + _RM_PARAM_REJOIN_WINDOW)
            parts = [tok]
            j = i + 1
            while j < limit and _net_brace_delta(" ".join(parts)) > 0:
                parts.append(tokens[j])
                j += 1
            if _net_brace_delta(" ".join(parts)) == 0:
                out.append(" ".join(parts))
                i = j
                continue
        out.append(tok)
        i += 1
    return out


def _rm_unescaped_boundary(operand: str, *, treat_subshell_closer: bool = True) -> "int | None":
    """Index of the first command SEPARATOR in *operand* that is neither
    backslash-escaped, quoted, nor part of a redirection, or ``None``.

    This is the SINGLE command-boundary scan for the whole floor. Every caller --
    the span-ender :func:`_rm_token_ends_argv`, the recursive-force flag precheck,
    the operand-before-boundary split, the operand-loop terminator, and the
    glued-token splitter -- routes through it so one definition of "where does this
    command end" is applied everywhere. A second helper (the former
    ``_rm_boundary_outside_quotes``) drifted from this one and split ``2>&1`` at the
    ``&`` where this one would too; merging them means a redirection-``&`` fix (or
    any future boundary rule) lands once and holds at every scan.

    Boundaries recognised: ``;`` ``&`` ``|`` newline, plus the bare-subshell closer
    ``)`` when *treat_subshell_closer* is True. NOT boundaries:

    * a backslash-escaped operator (``a\\;b`` is the file ``a;b``) -- the escape and
      its target are both skipped;
    * an operator INSIDE a quoted span (``'a;b'`` / ``"a;b"`` is a literal filename),
      so splitting there cannot end the argv before a later recursive-force flag and
      a home/root operand (``rm 'a;b' -fr $HOME``);
    * a redirection ``&`` (``2>&1``, ``1>&2``, ``&>/dev/null``, ``>&2``), which
      duplicates a file descriptor rather than ending the command (``rm -fr 2>&1 ~``
      must keep ``~``), via :func:`_rm_amp_is_redirection`.

    *treat_subshell_closer* is the ONLY behavioural knob, for the one caller that
    must not treat ``)`` as a boundary: the glued-token splitter leaves a ``)``
    closing a ``$(…)`` to the depth-tracking classifier. ``)`` closes a bare subshell
    for the default callers (``(rm -fr /)`` arrives as the operand ``/)`` whose
    target is ``/``; splitting there classifies ``/`` and ends the argv so a widened
    ``-fr`` in a bare subshell cannot hide root). ``}`` is NEVER a boundary -- it
    closes a ``${HOME}`` expansion far more often than a bare group, and treating it
    as one truncated ``${HOME}/../x`` at the brace.
    """
    separators = ";&|\n)" if treat_subshell_closer else ";&|\n"
    k = 0
    n = len(operand)
    quote = ""  # "'" or '"' while inside that quote span, else ""
    # First index at/after which NO ``}`` closes a ``${``; once an unbalanced ``${``
    # walk reaches the end, every later ``${`` is skipped so the scan stays linear.
    no_closer_from = n
    while k < n:
        ch = operand[k]
        if ch == "\\":
            k += 2  # the backslash escapes the next char; neither is a boundary
            continue
        if quote:
            # Inside a quoted span: only the matching close quote ends it; a
            # control-operator here is literal data, never a boundary.
            if ch == quote:
                quote = ""
            k += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            k += 1
            continue
        # A ``${...}`` parameter expansion is part of ONE word: its ``:?word`` /
        # ``:-word`` body can carry a literal ``;`` / ``&`` / ``|`` and interior
        # whitespace (``${HOME:?a;b}``, ``${HOME:?must be set}``) that the shell does
        # NOT read as a command separator. Skip to the matching ``}`` so the interior
        # operator does not end the ``rm`` argv before a home target (GPT 6.1,
        # security-class: ``rm -fr ${HOME:?a;b}`` was torn and allowed). Nested
        # ``${...}`` is tracked by depth; an UNBALANCED ``${`` (no closer) falls
        # through unchanged so it cannot swallow the rest of the operand.
        if ch == "$" and k + 1 < n and operand[k + 1] == "{" and k < no_closer_from:
            depth = 1
            j = k + 2
            while j < n and depth:
                cj = operand[j]
                if cj == "\\":
                    j += 2
                    continue
                if cj == "{" and j > 0 and operand[j - 1] == "$":
                    depth += 1
                elif cj == "}":
                    depth -= 1
                j += 1
            if depth == 0:
                k = j  # the whole balanced ``${...}`` is one word, skip past it
                continue
            # The ``${`` did not close before the end of the operand. No ``}`` lies
            # at or after ``k``, so every LATER ``${`` opener will not close either;
            # record that and short-circuit it, else a long unbalanced run
            # (``"${HOME" * 3300``) rescans to the end from each opener -- O(n**2),
            # 52s on a 20 KB operand, past the gateway's loop-stall watchdog (Opus
            # 5.5). The unbalanced ``${`` falls through unchanged (it cannot swallow
            # the rest of the operand).
            no_closer_from = k
        if ch in separators:
            # A redirection ``&`` (``2>&1``, ``&>/dev/null``) duplicates a file
            # descriptor and does NOT end the command, so it is not a boundary.
            if ch == "&" and _rm_amp_is_redirection(operand, k):
                k += 1
                continue
            return k
        k += 1
    return None


def _rm_strip_all_quotes(token: str) -> str:
    """Remove every unescaped shell quote character from an operand.

    A shell removes quoting during word expansion, so ``"$HOME"/`` and
    ``$HOME/`` are the SAME path, as are ``"${HOME}"/x`` and ``${HOME}/x`` and a
    split ``"$HO"ME``. ``_strip_outer_quotes`` only peels a BALANCED
    surrounding pair, so a PARTIALLY quoted operand keeps a leading ``"`` that
    defeats the ``~`` / ``$HOME`` anchor of the home/root matchers (e.g.
    ``setsid rm -fr "$HOME"/`` bypassed the enabled home rule).
    This yields the de-quoted spelling the matchers are anchored on; a backslash
    escape keeps the quote it escapes (``\\"`` is a literal quote char in the
    filename, not a quoting delimiter).

    A removed quote still TERMINATES a bare ``$name`` VARIABLE reference: ``"$HO"ME``
    reads the variable ``HO`` and appends the literal ``ME`` to its VALUE -- it is
    NOT ``$HOME`` (bash reads the name up to the quote). So a dropped delimiter that
    ends a live ``$name`` run with a word character after it inserts a non-word
    sentinel, keeping the name from absorbing the text past the quote (GPT 6.1:
    ``"$HO"ME`` must not fabricate ``$HOME``). This is scoped to a ``$name`` run, so
    a split WORD or program spelling (``r''m`` -> ``rm``, ``"$HOME"/x`` ->
    ``$HOME/x``) still fuses -- only a variable NAME is bounded.
    """
    out: list[str] = []
    i = 0
    n = len(token)
    dropped_delimiter = False
    in_var_name = False  # inside a bare ``$name`` run (a ``$`` then word chars)

    def _boundary(nxt: str) -> None:
        # The dropped quote ended a ``$name`` run and a word char follows: the quote
        # terminated the variable name, so keep the name from eating ``nxt``.
        if dropped_delimiter and in_var_name and (nxt.isalnum() or nxt == "_"):
            out.append("\x00")

    while i < n:
        ch = token[i]
        if ch == "\\" and i + 1 < n:
            _boundary(token[i + 1])
            dropped_delimiter = False
            in_var_name = False
            out.append(token[i + 1])
            i += 2
            continue
        if ch in "\"'":
            dropped_delimiter = True
            i += 1
            continue
        _boundary(ch)
        dropped_delimiter = False
        if ch == "$":
            in_var_name = True
        elif in_var_name and not (ch.isalnum() or ch == "_"):
            in_var_name = False
        out.append(ch)
        i += 1
    return "".join(out)


#: grep-FAMILY data consumers: they take a ``-e PATTERN`` option whose value is a
#: search pattern, and they NEVER execute an argument (unlike ``sed``'s ``e`` flag
#: or ``awk``'s ``system()``). The shared ``_SCRIPT_EXECUTES_RE`` matches a bare
#: ``-e`` token with its ``\be\s*$`` arm, so ``grep -e rm -e -fr /`` was wrongly
#: read as script execution and lost its data-consumer exemption (GPT 6.1
#: over-refusal). This set lets the floor keep the exemption for a grep-family
#: search while still disqualifying a real evaluator pipeline.
_RM_GREP_FAMILY_PROGRAMS: frozenset[str] = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})


def _rm_command_disqualified(tokens: "list[str]", programs: "list[str]") -> bool:
    """grep-aware twin of ``shell_normalizer._data_consumer_command_disqualified``.

    Identical to the shared helper EXCEPT it does not withdraw the data-consumer
    exemption on a sed/awk script-execution marker (``_SCRIPT_EXECUTES_RE``) when
    the command's program is a grep-FAMILY consumer, whose ``-e`` is a pattern flag
    rather than a script (``grep -e rm -e -fr /`` is a read-only search base
    allows). The evaluator-pipeline and program-position-substitution guards are
    retained verbatim, so a real ``… | sh`` / ``$(…) …`` is still disqualified.
    """
    disq = _shell_normalizer._data_consumer_command_disqualified(tokens)
    if not disq:
        return False
    program = _program_basename(programs[0]) if programs else ""
    if program not in _RM_GREP_FAMILY_PROGRAMS:
        return disq
    # The command IS a grep-family search. Keep the two guards that are real for
    # it; drop only the sed/awk script-marker disqualification.
    if _shell_normalizer._pipes_into_evaluator(tokens):
        return True
    first = tokens[0].lstrip("\"'") if tokens else ""
    if first.startswith("$(") or first.startswith("`"):
        return True
    return False


#: Sentinel that replaces a LITERAL (quoted/escaped, non-parameter-expansion)
#: brace/glob metacharacter so the brace expander and the ``*``-glob root/home
#: matchers do not read it as shell-active. A control char no path matches as
#: root/home, keeping the operand's length and its other characters intact.
_RM_LITERAL_METACHAR_SENTINEL = "\x01"


def _rm_neutralize_literal_metachars(token: str) -> str:
    """De-quote *token*, replacing a brace/glob metacharacter (``{`` ``}`` ``*``)
    with a sentinel ONLY when it is a LITERAL filename character -- quoted or
    backslash-escaped AND not part of a ``${...}`` parameter expansion.

    Bash expands an UNQUOTED ``{a,b}`` / ``*``, and expands ``${HOME}`` whose braces
    delimit a PARAMETER EXPANSION (not a brace list). So ``rm -fr /*`` /
    ``rm -fr /{,tmp}`` are real root wipes and ``rm -fr "${HOME}"`` a real home wipe,
    while a QUOTED ``'/*'`` / ``'/{,tmp}'`` names a literal file (GPT 6.1
    over-refusal). This keeps:

    * an UNQUOTED ``{`` / ``}`` / ``*`` verbatim (still expands);
    * the ``${`` and its matching ``}`` of a parameter expansion verbatim (so a
      quoted ``"${HOME}"`` / ``"${HOME:?}"`` still reads as a home reference);

    and neutralizes only a quoted/escaped brace/glob char that is NEITHER of those.
    The ``$`` is kept so the home-ref matchers still see ``$home``.

    Used ONLY on the raw ``strip_quotes=True`` classification (a direct operand such
    as ``rm -fr '/*'``). The decoded / substitution-body view has already lost its
    quotes to the payload walk, so a quoted-metachar literal inside a ``$(...)``
    body is a documented residual, not covered here.
    """
    states = _rm_quote_states(token)  # 0 unquoted, 1 single, 2 double, per index
    # Positions that are the ``{`` / ``}`` delimiters of a ``${...}`` parameter
    # expansion -- a ``$`` directly before a ``{`` opens one; depth tracks nesting.
    param_brace: set[int] = set()
    stack: list[int] = []
    i = 0
    n = len(token)
    while i < n:
        if token[i] == "$" and i + 1 < n and token[i + 1] == "{":
            param_brace.add(i + 1)
            stack.append(i + 1)
            i += 2
            continue
        if token[i] == "}" and stack:
            stack.pop()
            param_brace.add(i)
        i += 1
    out: list[str] = []
    i = 0
    while i < n:
        ch = token[i]
        if ch == "\\" and i + 1 < n:
            nxt = token[i + 1]
            # A backslash-escaped metachar is a literal (``\*`` / ``\{``).
            out.append(_RM_LITERAL_METACHAR_SENTINEL if nxt in "{}*" else nxt)
            i += 2
            continue
        if ch in "'\"":
            i += 1  # drop the quote delimiter (de-quote)
            continue
        quoted = states[i] != 0
        if ch in "{}*" and quoted and i not in param_brace:
            out.append(_RM_LITERAL_METACHAR_SENTINEL)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _rm_substitution_output_quote_kind(body: str) -> "str | None":
    """How the substitution BODY's resolved operand was QUOTED: ``"single"``,
    ``"double"``, or ``None`` (unquoted).

    A command substitution captures its inner command's output and the OUTER shell
    expands it no further. What the output contains as ACTIVE text depends on how
    the INNER operand was quoted: ``$(echo ~)`` (unquoted) tilde-expands ``~`` to
    the home PATH (a real home wipe -- deny); ``$(echo '$HOME')`` (single-quoted)
    prints ``$HOME`` verbatim, a literal filename with no live ``~`` / ``$`` / glob
    (allow); ``$(echo "$HOME")`` (double-quoted) still expands ``$HOME`` but not
    ``~`` / glob. The shared resolver strips quotes and returns the bare operand,
    losing this, so re-derive it: mirror the resolver's operand pick
    (``echo``/``printf``, skip flags and ``%`` format words) and report its quoting.
    """
    words = body.split()
    if not words or _program_basename(words[0]) not in {"echo", "printf"}:
        return None
    for arg in words[1:]:
        operand = _shell_normalizer._normalize_operand(arg)
        if operand.startswith("-") or "%" in operand:
            continue
        # The operand the resolver returned; its quoting fixes which expansions the
        # inner shell suppressed.
        if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] == "'":
            return "single"
        if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] == '"':
            return "double"
        if "\\" in arg:
            return "single"  # a backslash-escaped operand is literal like single-q
        return None
    return None


def _rm_neutralize_substitution_output(output: str, quote_kind: str) -> str:
    """Blank the metachars a LITERAL substitution output does not expand.

    Called with the operand's quote kind (:func:`_rm_substitution_output_quote_kind`):

    * ``single`` -- the inner shell suppressed EVERY expansion, so ``~`` / glob AND
      a ``$HOME`` / ``$home`` variable spelling are all literal (``$(echo '$HOME')``
      prints ``$HOME`` verbatim); blank the ``$`` of a home-variable run too.
    * ``double`` -- ``~`` and glob are literal but ``$HOME`` still expands, so blank
      only ``~`` / brace / glob and leave a ``$HOME`` spelling for the home matcher.

    An exact ``/`` survives either way, so ``$(printf '/')`` (root itself) still
    denies, while ``$(printf '/*')`` loses its glob ``*`` and reads as the literal
    filename it is.
    """
    chars = list(output)
    for k, ch in enumerate(chars):
        if ch in "{}*" or (k == 0 and ch == "~"):
            chars[k] = _RM_LITERAL_METACHAR_SENTINEL
    if quote_kind == "single":
        # A ``$HOME`` / ``$home`` / ``${home}`` the operand carried is literal text,
        # not a variable; blank its ``$`` so the home-ref matchers do not read it.
        text = "".join(chars)
        for m in _RM_HOME_REF_RE.finditer(text):
            chars[m.start()] = _RM_LITERAL_METACHAR_SENTINEL
    return "".join(chars)


def _rm_token_is_flag_word(token: str) -> bool:
    """True if *token* is an OPTION word (``-rf``, ``--force``, ``--``), not an
    operand. A flag starts with ``-`` on its de-quoted spelling (``-f$'r'`` ->
    ``-fr``); an operand that merely CONTAINS a dash (``./a-b``, ``-``) is not one.
    """
    stripped = _rm_strip_all_quotes(token)
    return len(stripped) >= 2 and stripped[0] == "-"


def _rm_decoded_argv_preserving_operands(
    raw_tokens: "list[str]", norm_tokens: "list[str]"
) -> "list[str]":
    """Merge the quote-preserving *raw_tokens* with the decoded *norm_tokens* so a
    PROGRAM or FLAG word carries its decoded spelling while an OPERAND keeps its
    literal, quote-preserving source.

    The decoded view (the payload walk's own tokens) resolves ``r''m`` -> ``rm`` and
    a split flag ``-f$'r'`` -> ``-fr``, which the classifier needs. But it also
    strips an OPERAND's surrounding quotes, so a literal ``'a;b'`` filename collapses
    to ``a;b`` and the one quote-aware command-boundary split then reads the ``;`` as
    a boundary, drops the trailing ``~``, and fails a home wipe open. Decoding only
    the program and flag words — operands kept literal — routes every scan through
    the single quote-aware boundary helper with operand punctuation intact.

    The two lists are positionally aligned (both come from the same word split; the
    decode changes a word's CONTENT, not the word COUNT), so when their lengths
    differ we cannot map safely and fall back to the decoded view unchanged. An
    operand's own ANSI-C / env decode (``$'\\u002f'`` -> ``/``) is recovered by the
    walk's separate repaired frame and the raw ``strip_quotes=True`` pass, and the
    root/home matchers read ``$HOME`` / ``~`` by spelling, so keeping an operand raw
    loses no coverage.
    """
    if len(raw_tokens) != len(norm_tokens):
        return norm_tokens
    merged: "list[str]" = []
    expect_program = True
    for raw, norm in zip(raw_tokens, norm_tokens):
        # An assignment prefix (``X=1 r''m …``) and a one-word exec wrapper
        # (``env X=1 r''m …``) precede the real program, so they do NOT consume the
        # program position -- otherwise the split spelling ``r''m`` is kept raw
        # (undecoded) and never reforms to ``rm`` (GPT 6.1: a home wipe allowed).
        # Keep the prefix/wrapper word itself raw and leave ``expect_program`` set
        # so the NEXT word is decoded as the program.
        is_assignment = bool(_shell_normalizer.ENV_ASSIGNMENT_RE.match(raw))
        is_wrapper = (
            expect_program
            and not is_assignment
            and _program_basename(_rm_strip_all_quotes(raw)) in _RM_SPAN_EXEC_WRAPPERS
        )
        is_program = expect_program and bool(raw) and not is_assignment and not is_wrapper
        if is_program:
            expect_program = False
        # Keep the decoded spelling for the program word and for flag words; an
        # operand keeps its literal, quote-preserving raw form. A prefix/wrapper word
        # keeps its raw form and does not spend the program slot.
        merged.append(norm if (is_program or _rm_token_is_flag_word(raw)) else raw)
        # A glued or standalone command boundary resets the program expectation so
        # the NEXT command's program word is decoded too (``echo hi; r''m -fr ~``).
        if _rm_token_ends_argv(raw):
            expect_program = True
    return merged


def _strip_outer_quotes(token: str) -> str:
    """Remove only a MATCHING pair of surrounding quotes, keeping interior chars.

    Unlike :func:`_rm_strip_all_quotes` (which collapses ALL quotes and
    backslashes), this peels only a balanced surrounding ``'...'`` / ``"..."`` pair
    and preserves interior characters. ``'~'`` -> ``~``, ``"$HOME"`` -> ``$HOME``.
    """
    t = token
    while len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        t = t[1:-1]
    return t


def _rm_operand_is_single_quoted(token: str) -> bool:
    """True if *token* is wholly wrapped in one balanced single-quote pair.

    Bash passes a single-quoted word verbatim with no expansion, so a single-quoted
    ``'~'`` / ``'$HOME'`` names a literal cwd file, never the home dir — unlike a
    double-quoted ``"$HOME"`` which DOES expand. The whole token must be one
    balanced single-quoted span (leading-and-trailing ``'`` with no interior
    unescaped ``'``), so a partially-quoted or concatenated operand does not qualify.
    """
    if len(token) < 2 or token[0] != "'" or token[-1] != "'":
        return False
    return "'" not in token[1:-1]


def _rm_brace_word_could_be_catastrophic(word: str) -> bool:
    """True if a brace word MIGHT expand to the root/home dir ITSELF, so an overflow
    past the expansion cap must fail CLOSED.

    A member is ``head + <alt_1> + mid + <alt_2> … + tail``. It can be the root/home
    dir itself only when the LITERAL frame (the text left after the brace groups are
    removed) is empty or itself root/home (``/``, ``/*``, ``~`` …) AND some group
    offers an alternative that completes the frame to the target — an EMPTY
    alternative (``/{,bin}`` -> ``/``) or a ``/`` / ``~`` spelling. A numeric or
    single-char RANGE (``{000..511}``, ``{1..99}``) only yields digits/letters, so a
    bare ``rm -rf {000..511}`` (relative dir names, base-allowed) is NOT catastrophic
    and must not fail closed. A non-root literal frame (``/tmp/kc-shard-{000..511}``,
    ``./bench-out/run-{1..500}``) can only expand to descendants either way.
    """
    residual = _RM_BRACE_GROUP_RE.sub("", word)
    stripped = _rm_strip_all_quotes(residual).strip()
    norm = _rm_normalize_dot_segments(stripped)
    frame_is_root_or_empty = (
        stripped == ""
        or residual == ""
        or bool(_RM_ROOT_ITSELF_RE.fullmatch(stripped))
        or bool(_RM_HOME_ITSELF_RE.fullmatch(stripped))
        or bool(_RM_ROOT_ITSELF_RE.fullmatch(norm))
        or bool(_RM_HOME_ITSELF_RE.fullmatch(norm))
    )
    if not frame_is_root_or_empty:
        return False
    # A numeric/char RANGE group (``{000..511}``, ``{a..z}``) ALWAYS contributes a
    # non-empty digit/letter to EVERY member, and a bare root/home target
    # (``/``, ``~``, ``/*``, ``~/*``) carries only ``/`` / ``~`` / ``*`` -- no
    # alphanumeric. So once ANY group is a mandatory range, no member can equal the
    # bare target, whatever the literal frame: ``{000..511}{,.log}`` (empty frame)
    # expands to relative descendants, and ``~/{,cache/}{1..300}`` (home-rooted
    # frame) to ``~/1`` / ``~/cache/1`` -- descendants of home, never home ITSELF.
    # Both are bulk numbered cleanups that must stay allowed; failing them closed
    # over-refuses a base-allowed command (GPT 6.1 F2, security-class false denial).
    if any(_RM_BRACE_RANGE_RE.match(g.group()[1:-1]) for g in _RM_BRACE_GROUP_RE.finditer(word)):
        return False
    # The frame could reach root/home — a group must OFFER a completing member.
    for group in _RM_BRACE_GROUP_RE.finditer(word):
        body = group.group()[1:-1]
        if _RM_BRACE_RANGE_RE.match(body):
            # A numeric / single-char RANGE (``000..511``, ``a..z``) yields only
            # digits/letters — never ``/`` / ``~`` / empty. Not catastrophic.
            continue
        if "," not in body:
            continue  # single-member body: not a brace expansion, no new member
        for alt in body.split(","):
            a = alt.strip()
            if a == "" or a.startswith("/") or a.startswith("~"):
                return True
    return False


def _rm_normalize_dot_segments(operand: str) -> str:
    """Collapse ``.`` / ``..`` path segments in an rm operand, LEXICALLY.

    The kernel resolves dot segments, so ``/./`` / ``/tmp/../`` are ROOT and
    ``~/./`` is home — yet the exact matchers see a non-``/`` string and miss it
    (e.g. ``setsid rm -fr /./`` bypassed the wrapped-root guard). Resolves like
    ``os.path.normpath`` WITHOUT filesystem access, preserving a leading ``~`` /
    ``$HOME`` marker and a trailing ``*`` glob so the matchers still fire.
    """
    if "." not in operand:
        return operand
    # The payload walk expands ``$HOME`` with no variable-name boundary (shared
    # ``main`` behavior), so ``$HOME_BAK`` / ``$HOMEDIR`` become the real home dir
    # with a GLUED identifier suffix (``$HOME`` + ``_bak``). That names a DIFFERENT
    # variable's target, not the home dir, yet a trailing ``..`` would then collapse
    # the fabricated ``<home>_bak/../..`` to ``/`` and the root matcher would
    # falsely refuse it (base allowed it). When the operand is the real home path
    # followed by an identifier char (NOT a ``/`` separator), leave it unresolved so
    # no dot-traversal escapes it to root/home.
    _home_real = _rm_expanded_home_path()
    if _home_real and _rm_fold_home_case(operand).startswith(_home_real):
        _after = operand[len(_home_real) : len(_home_real) + 1]
        if _after and (_after.isalnum() or _after == "_"):
            return operand
    # Preserve a leading home marker and a trailing ``*`` glob across normpath,
    # which would otherwise mangle ``~`` or drop the glob.
    prefix = ""
    for marker in ("~", "${home}", "$home"):
        if operand[: len(marker)].lower() == marker:
            # A bare ``$HOME`` variable ends at a non-identifier char: ``$HOME_BAK``
            # and ``$homedir`` are DIFFERENT variables, so the bare ``$home`` marker
            # needs a name boundary -- without it ``rm -fr $HOME_BAK/../..`` resolved
            # against the real home and was falsely refused. ``${home}`` is
            # already brace-delimited (``${HOME}bak`` is home + literal ``bak``), and
            # ``~x`` is a username handled elsewhere -- neither needs the boundary.
            if marker == "$home":
                nxt = operand[len(marker) : len(marker) + 1]
                if nxt and (nxt.isalnum() or nxt == "_"):
                    continue
            prefix = operand[: len(marker)]
            operand = operand[len(marker) :] or "/"
            break
    glob_tail = ""
    if operand.endswith("/*"):
        operand, glob_tail = operand[:-1], "*"
    if prefix:
        # A ``..`` after the home marker collapses against HOME, not ``/``:
        # ``$HOME/../alice`` with ``HOME=/home/alice`` IS home. Resolve against
        # the REAL expanded home, normalize the joined path (lexical), map back:
        # exactly home -> home; under -> descendant; above -> bare absolute. Fold
        # separators + lowercase (Windows CI); cannot widen POSIX.
        home_real = _posixpath.normpath(_os.path.expanduser("~").replace("\\", "/")).lower()
        joined = home_real + ("" if operand == "/" else operand)
        try:
            collapsed_full = _posixpath.normpath(joined)
        except (TypeError, ValueError):
            return prefix + operand + glob_tail
        # The home MATCHER admits a glob only after a ``/`` separator
        # (``_RM_HOME_ITSELF_RE`` is ``~(?:/+(?:\*/*)?)?``), so a bare ``~*`` /
        # ``~/foo*`` with the separator dropped would never fullmatch and the wipe
        # would fail open (e.g. ``rm -fr ~/./*`` collapsed to
        # ``~*``). Re-emit the ``/`` with the glob tail.
        glob_suffix = "/" + glob_tail if glob_tail else ""
        if collapsed_full == home_real:
            return prefix + glob_suffix
        if collapsed_full.startswith(home_real + "/"):
            return prefix + collapsed_full[len(home_real) :] + glob_suffix
        return collapsed_full + glob_suffix
    try:
        collapsed = _posixpath.normpath(operand)
    except (TypeError, ValueError):
        return prefix + operand + glob_tail
    if prefix and collapsed == ".":
        collapsed = ""
    # ``~`` tilde-expands ONLY as a word's first char. ``./~`` is a cwd directory
    # literally named ``~`` (not expanded) — base's ``rm -rf ~.*`` never matched its
    # leading ``./``. When no home prefix was present yet normpath
    # collapsed a leading ``./`` to a bare ``~`` segment, that ``~`` is a literal
    # filename: restore the dot anchor so it does not re-read as home.
    if not prefix and collapsed[:1] == "~":
        collapsed = "./" + collapsed
    return prefix + collapsed + glob_tail


#: The real expanded home path (``/home/<user>``), computed once. ``expanduser`` is
#: platform-native (a backslash path on Windows), so the separators are folded to
#: ``/`` and the result lowercased to match the lowercased operands the walk
#: produces — the same spelling ``_rm_normalize_dot_segments`` compares against.
_RM_EXPANDED_HOME_CACHE: "list[str] | None" = None

#: True when this platform's filesystem is case-INSENSITIVE for path equality
#: (Windows always; macOS by default). On such a platform ``/Users/Alice`` and
#: ``/users/alice`` name the SAME directory, so the home comparison must fold
#: case; on a case-SENSITIVE platform (ordinary Linux) ``/home/alice`` and
#: ``/home/ALICE`` are DISTINCT directories and folding them would newly refuse a
#: cleanup of a case-variant sibling (GPT 6.1 over-refusal). ``os.path.normcase``
#: lowercases on Windows and is identity on posix, so a non-``nt`` platform whose
#: fs is nonetheless case-insensitive (macOS) is handled explicitly.
_RM_FS_CASE_INSENSITIVE = _os.name == "nt" or _sys.platform == "darwin"


def _rm_fold_home_case(path: str) -> str:
    """``path`` lowercased, so the home-equality comparison always matches operand
    against stored home case-insensitively -- BOTH sides come through here, so a
    home whose own path has an uppercase letter (``/home/Alice``) still compares
    equal to the lowercased operand view and a real wipe of it denies. (Comparing a
    lowercased operand against a case-preserved home was a fail-open: the two never
    matched and ``rm -fr /home/Alice`` was allowed.) A case-variant SIBLING
    (``/home/ALICE`` vs home ``/home/Alice``), which IS a distinct directory on a
    case-sensitive filesystem, is told apart separately and earlier, by
    ``_rm_neutralize_case_variant_home_paths`` on the raw-case text."""
    return path.lower()


_RM_HOME_RAW_CASE_CACHE: "list[str] | None" = None


def _rm_expanded_home_raw_case() -> str:
    """The real home directory ``/``-separated in its ORIGINAL case (never
    lowercased), cached per process. Used only on a case-SENSITIVE filesystem to
    tell the real home apart from a case-variant sibling (``/home/alice`` vs
    ``/home/ALICE``), which the lowercased classification view cannot (GPT 6.1 F3).
    A degenerate ``~`` resolving to ``/`` or ``.`` returns empty, matching
    ``_rm_expanded_home_path``."""
    global _RM_HOME_RAW_CASE_CACHE
    if _RM_HOME_RAW_CASE_CACHE is None:
        try:
            resolved = _posixpath.normpath(_os.path.expanduser("~").replace("\\", "/"))
        except (TypeError, ValueError):
            resolved = ""
        _RM_HOME_RAW_CASE_CACHE = [resolved if resolved not in ("", "/", ".") else ""]
    return _RM_HOME_RAW_CASE_CACHE[0]


def _rm_expanded_home_path() -> str:
    """The real home directory, ``/``-separated and case-folded for the platform's
    filesystem (lowercased on a case-insensitive fs, original case otherwise),
    cached per process."""
    global _RM_EXPANDED_HOME_CACHE
    if _RM_EXPANDED_HOME_CACHE is None:
        try:
            resolved = _rm_fold_home_case(
                _posixpath.normpath(_os.path.expanduser("~").replace("\\", "/"))
            )
        except (TypeError, ValueError):
            resolved = ""
        # A degenerate ``~`` resolving to ``/`` or ``.`` is NOT a usable home anchor
        # (it would misclassify every path), so store empty and the F3 check no-ops.
        _RM_EXPANDED_HOME_CACHE = [resolved if resolved not in ("", "/", ".") else ""]
    return _RM_EXPANDED_HOME_CACHE[0]


#: Mirror of the ``(?:/+(?:\*/*)?)?`` tail ``_RM_HOME_ITSELF_RE`` gives ``~``/
#: ``$HOME``: trailing ``/`` with an OPTIONAL whole-remainder ``*`` glob. Strips
#: that tail so an expanded-home operand carrying it (``/home/<user>/``,
#: ``/home/<user>/*``) compares equal to the bare home, while a real descendant
#: (``/home/<user>/.cache``) keeps a segment and does NOT reduce to home.
_RM_HOME_ITSELF_TAIL_RE = re.compile(r"/+(?:\*/*)?$")


def _rm_strip_home_itself_tail(path: str) -> str:
    """``path`` with a single home-itself tail (trailing slashes / ``/*`` glob) removed."""
    return _RM_HOME_ITSELF_TAIL_RE.sub("", path, count=1)


def _rm_walk_frames(text_lower: str, raw_text: "str | None") -> "list[tuple[str, list[str], bool]]":
    """``(source, norm_tokens, repaired)`` frames for the rm floor to classify.

    Block 1 is ``_shell_payload_walk(text_lower)``. When *raw_text* carries an
    ANSI-C span, block 2 walks that text with its ``$'…'`` spans decoded
    (case-preserved) then lowercased, so the width-sensitive ``\\U`` escape
    resolves (``is_denied`` lowercases first, truncating ``\\U`` at 4 digits).
    ``repaired`` marks block 2; the caller classifies it ONLY via its decoded
    ``norm_tokens`` — a raw split would strip both the shlex-added quotes and the
    LITERAL quotes the decode produced (``$'\\"/\\"'`` -> ``'"/"'`` read as root).
    The block-1 walk is where a raw ``$HOME`` / ``~`` operand is classified.
    """
    frames: "list[tuple[str, list[str], bool]]" = [
        (source, toks, False) for source, toks in _shell_payload_walk(text_lower)
    ]
    if raw_text is not None and "$'" in raw_text:
        repaired = _decode_shell_quoted_literals(raw_text).lower()
        if repaired != text_lower:
            frames.extend((source, toks, True) for source, toks in _shell_payload_walk(repaired))
    # Drop a DESCENDED substitution frame whose opener is SINGLE-QUOTED: single
    # quotes suppress expansion, so a backtick / ``$(`` inside them is literal (``git
    # commit -m 'the `rm -fr /` step'`` is prose). The first frame
    # (whole command) is always kept; a single-quoted ``-c`` payload (``bash -c 'rm
    # -rf /'``) still executes as a ``-c`` descent, so only a bare body is dropped.
    single = _rm_single_quoted_positions(text_lower)
    kept: "list[tuple[str, list[str], bool]]" = []
    for idx, frame in enumerate(frames):
        if idx == 0 or not _rm_frame_is_single_quoted_substitution(text_lower, frame[0], single):
            kept.append(frame)
    return kept


def _rm_frame_is_single_quoted_substitution(
    text_lower: str, src: str, single: "list[bool]"
) -> bool:
    """True if descended frame *src* is a backtick / ``$(`` body inside a
    SINGLE-QUOTED span of *text_lower* — a literal, not an executed command. Finds
    *src* as a substring whose opener char (``\\`` or the ``(`` of ``$(``) is
    single-quoted; conservative, so a genuinely executed payload is never dropped.
    """
    body = src.strip()
    if not body:
        return False
    start = 0
    while True:
        at = text_lower.find(body, start)
        if at < 0:
            return False
        opener = at - 1
        if opener >= 0 and opener < len(single) and single[opener]:
            ch = text_lower[opener]
            if ch == "`" or (ch == "(" and opener > 0 and text_lower[opener - 1] == "$"):
                return True
        start = at + 1


#: An UNQUOTED ``~`` at a word boundary — bash tilde-expands it to the home dir.
#: A quoted (``'~'`` / ``"~"``) or escaped (``\~``) tilde is a LITERAL filename.
#: The boundary classes include ``{`` / ``,`` / ``}`` so a brace-expansion member
#: (``rm -fr {~,/tmp/x}`` -> bash expands ``~``) counts as a live tilde too.
_RM_LIVE_TILDE_RE = re.compile(r"(?:^|[\s;&|({,=])~(?=$|[\s;&|),}/])")
#: A ``$HOME`` / ``${HOME}`` reference — expands when unquoted OR double-quoted.
_RM_HOME_REF_RE = re.compile(r"\$\{?home\b\}?", re.IGNORECASE)


def _rm_has_live_home_expansion(text_lower: str) -> bool:
    """True if *text_lower* contains a home expansion the shell actually performs
    — an UNQUOTED, unescaped ``~`` at a word boundary, or a ``$HOME``/``${HOME}``
    that is unquoted or DOUBLE-quoted (single quotes suppress ``$``).

    A QUOTED tilde (``'~'`` / ``"~"``), a backslash-escaped ``\\~``, and a
    SINGLE-quoted ``'$HOME'`` are literal filenames the shell never expands to the
    home dir, so a command whose only home-shaped token is one of those performs
    NO home expansion — base allowed deleting such a cwd file.
    The caller drops a ``home`` verdict the payload walk produces by expanding a
    quoted tilde regardless of its quoting.
    """
    single = _rm_single_quoted_positions(text_lower)
    for m in _RM_LIVE_TILDE_RE.finditer(text_lower):
        pos = m.end() - 1  # index of the ``~``
        if pos < len(single) and single[pos]:
            continue  # single-quoted tilde (``'~'``) — literal
        # A ``"~"`` double-quoted tilde is literal too; a tilde is only live when
        # unquoted. The live-tilde regex already requires a word-boundary before
        # ``~``; reject it when the preceding char is a quote.
        prev = text_lower[m.start()] if m.start() < pos else ""
        if prev in ("'", '"'):
            continue
        return True
    for m in _RM_HOME_REF_RE.finditer(text_lower):
        pos = m.start()
        if pos < len(single) and single[pos]:
            continue  # single-quoted ``'$HOME'`` — literal, no expansion
        return True
    # A PARTIALLY-quoted ``$HOME`` (``"$HO"ME`` / ``$HO"ME"``) expands to the home
    # dir — the double quotes only group text and bash removes them during word
    # expansion. Scan a view with the DOUBLE quotes removed (single-quoted spans,
    # where ``$`` does not expand, replaced with a sentinel) and re-test for a live
    # ``$HOME`` the raw scan split across a quote.
    #
    # A removed quote delimiter still TERMINATES a bare ``$name`` reference:
    # ``"$HO"ME`` reads variable ``HO`` and appends literal ``ME`` to its VALUE --
    # it is NOT ``$HOME`` (bash reads the name up to the quote). So gluing the spans
    # must not let a ``$name`` run absorb characters past the quote: when a dropped
    # delimiter ends a live ``$name`` run with a word char after it, insert a
    # non-word sentinel. Scoped to a ``$name`` run so a split word still fuses; a
    # quote after the complete name (``"$HOME"/x``) adds none and still reads as
    # home (GPT 6.1: ``HO=./build/; rm -fr "$HO"ME`` fabricated ``$HOME``).
    joined: list[str] = []
    prev_state = 0
    dropped_delimiter = False
    in_var_name = False
    for step in _iter_shell_chars(text_lower):
        before, prev_state = prev_state, step.state
        if step.trailing_escape or _rm_quote_delimiter(step, before):
            dropped_delimiter = True  # drop a quote delimiter, joining the spans
            continue
        ch = step.char
        if len(step.text) != 2 and before == 1 and step.char == "$":
            ch = "\x00"
        if dropped_delimiter and in_var_name and (ch.isalnum() or ch == "_"):
            # The removed quote ended a ``$name`` run; preserve that boundary.
            joined.append("\x00")
        dropped_delimiter = False
        if ch == "$":
            in_var_name = True
        elif in_var_name and not (ch.isalnum() or ch == "_"):
            in_var_name = False
        joined.append(ch)
    if _RM_HOME_REF_RE.search("".join(joined)):
        return True
    return False


def _rm_split_unquoted_newlines(source: str) -> "list[str]":
    """Split *source* into command lines at UNQUOTED, unescaped newlines.

    A shell runs each line of a multi-line command as its own command, but
    ``_split_shell_words`` treats a newline as ordinary whitespace, so a frame
    like ``rm -f x\\nls -ltr ~`` would otherwise fuse ``ls -ltr ~`` into ``rm``'s
    argv — ``ls``'s packed ``-ltr`` donates a spurious ``-r`` and ``~`` becomes
    the operand, misreading a non-recursive ``rm -f`` as a recursive-force home
    wipe. Splitting on the unquoted newline first
    keeps each line its own argv. A newline inside quotes or escaped is literal
    and does not split. Returns the single source unchanged when it has no
    unquoted newline (the common case), so a one-line command is untouched.
    """
    if "\n" not in source and "\r" not in source:
        return [source]
    lines: list[str] = []
    buf: list[str] = []
    for step in _iter_shell_chars(source):
        if step.active and step.char in ("\n", "\r"):
            lines.append("".join(buf))
            buf = []
            continue
        buf.append(step.text)
    lines.append("".join(buf))
    return [ln for ln in lines if ln.strip()] or [source]


def _rm_split_top_level_semicolons(source: str) -> "list[str]":
    """Split *source* at TOP-LEVEL, unquoted command separators (``;`` / ``&`` /
    ``|``), used only by the heavy-substitution fast path: each segment is
    classified as its own argv so a real ``…; rm -fr ~`` after a flood of
    ``"$(true)"`` operands is still seen. A separator inside quotes,
    an escape, or a ``$(…)`` / backtick / ``${…}`` body does not split.
    """
    depth = _rm_substitution_depth(source)
    single = _rm_single_quoted_positions(source)
    both = _rm_quoted_positions(source)
    segs: list[str] = []
    buf: list[str] = []
    for idx, ch in enumerate(source):
        boundary = (
            ch in ";&|"
            and not single[idx]
            and not (idx < len(both) and both[idx])
            and (idx >= len(depth) or depth[idx] <= 0)
        )
        if boundary:
            if buf:
                segs.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        segs.append("".join(buf))
    return [s for s in segs if s.strip()] or [source]


def _rm_absolute_home_operand(text_lower: str) -> bool:
    """True if the DE-QUOTED command names a LITERAL absolute path EQUAL to the
    real home dir (``rm -fr /home/alice`` where ``$HOME`` is ``/home/alice``).

    Such an equality carries NO ``$HOME`` / ``~`` token, so the tilde-liveness
    suppression would wrongly discard it and allow an irreversible home wipe.
    Uses the SAME separator-folding + home-itself-tail
    stripping the operand classifier uses, so ``/home/alice/`` and ``/home/alice/*``
    count while a DESCENDANT (``/home/alice/.cache``) does not.
    """
    home_real = _rm_expanded_home_path()
    if not home_real:
        return False
    for tok in _split_shell_words(text_lower):
        op = _rm_strip_all_quotes(tok)
        for spelled in (op, _rm_normalize_dot_segments(op)):
            if (
                _rm_strip_home_itself_tail(_rm_fold_home_case(spelled.replace("\\", "/")))
                == home_real
            ):
                return True
    return False


#: A heredoc opener: ``<<`` or ``<<-`` followed by its delimiter word, which may
#: be quoted (``<<'EOF'`` / ``<<"EOF"``) or bare (``<<EOF``). The captured name is
#: the delimiter; a leading ``-`` (``<<-``) lets the terminator be indented with
#: tabs. A ``<<<`` here-string (not a heredoc — its single operand IS the data,
#: inline) is excluded by the negative lookahead so it is left for the ordinary
#: argv walk. Only the FIRST opener on a line is handled per pass.
#:
#: The delimiter is a shell WORD: it ends at the first unquoted control operator or
#: whitespace, so ``cat <<EOF;`` has delimiter ``EOF`` not ``EOF;`` (a
#: ``\S+`` capture grabbed ``EOF;`` and ran the body strip past the real ``EOF``
#: terminator, dropping an executable ``rm`` line). The character class therefore
#: excludes ``; & | < > ( ) `` and whitespace.
#: A heredoc opener ``<<``/``<<-`` (not ``<<<``, not a shifted ``$(( a << b ))``)
#: followed by its delimiter WORD. The delimiter may be single- or double-quoted
#: with INTERIOR SPACES (``<<'END OF TEXT'``), partly quoted (``<<E'OF'``), or a
#: bare unquoted run -- so capture a sequence of quoted spans and/or unquoted
#: non-separator chars, NOT just ``[^\s…]+`` which stopped at the first space inside
#: quotes and truncated ``'END OF TEXT'`` to ``END`` (its terminator then never
#: matched, the body incl. a trailing ``rm -fr ~`` was dropped, and the wipe passed
#: the home floor). ``_rm_strip_all_quotes`` dequotes the
#: captured word before it is matched against each body line as the terminator.
_RM_HEREDOC_OPEN_RE = re.compile(r"(?<!<)<<-?(?!<)\s*((?:'[^']*'|\"[^\"]*\"|[^\s;&|<>()'\"])+)")


def _rm_strip_heredoc_bodies(source: str) -> str:
    """Drop heredoc BODIES from *source* — stdin DATA, never argv.

    ``cat > notes.md <<'EOF'`` feeds the following lines to the command on STDIN,
    never parsed as commands. Yet ``_split_shell_words`` flattens a newline to
    whitespace, so a body line ``rm -fr /`` (prose) reads as an ``rm`` command and
    the floor refuses what base allowed. An EVALUATOR heredoc
    (``bash <<'EOF'``) that runs its stdin is still caught by the whole-text regex.
    An UNQUOTED delimiter (``<<EOF``) runs ``$(…)`` / backticks in its body, so those
    executed substitutions are kept for classification. The opener must
    be a REAL ``<<`` — unquoted, unescaped, not ``<<<``, not ``$((… << …))``.
    """
    if "<<" not in source:
        return source
    # Fold BACKSLASH LINE-CONTINUATIONS before anything else: a ``\`` at a physical
    # line end splices the next line onto this one in the shell, so a ``<<`` or a
    # delimiter split across a continuation must be read as one logical line.
    # Folding also keeps the quote scan below from treating the spliced
    # remainder as a fresh unquoted line.
    source = source.replace("\\\n", "")
    lines = source.split("\n")
    # Quote and command-substitution state is computed over the WHOLE source in ONE
    # pass and indexed by ABSOLUTE offset, so a quote opened on an earlier physical
    # line is still seen as open here. The per-LINE masks reset state at each line
    # start, so a quoted multi-line string (``printf '%s\n' 'note<newline><<EOF'``)
    # made ``<<EOF`` on the later line look unquoted, the stripper took it as an
    # unterminated heredoc, and the real ``rm -fr "$HOME"`` after it was discarded
    # -> a home-wipe bypass.
    g_single = _rm_single_quoted_positions(source)
    g_quoted = _rm_quoted_positions(source)
    g_cmdsub_open = _rm_cmdsub_open_mask(source)
    # Absolute offset of each physical line's first char in ``source`` (the ``\n``
    # separators are one char each).
    line_start: "list[int]" = []
    acc = 0
    for ln in lines:
        line_start.append(acc)
        acc += len(ln) + 1
    out: "list[str]" = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out.append(line)
        base = line_start[i]
        # Find a REAL heredoc opener. State comes from the WHOLE-source masks indexed
        # at ``base + local`` (cross-line), not a fresh per-line scan. An opener
        # single-quoted, backslash-escaped, or in ``$((… << …))`` arithmetic is
        # literal and opens no heredoc.
        delim = None
        delim_dash = False

        def _single(k: int, _b: int = base) -> bool:
            idx = _b + k
            return idx < len(g_single) and g_single[idx]

        def _quoted(k: int, _b: int = base) -> bool:
            idx = _b + k
            return idx < len(g_quoted) and g_quoted[idx]

        def _cmdsub(k: int, _b: int = base) -> bool:
            idx = _b + k
            return idx < len(g_cmdsub_open) and g_cmdsub_open[idx]

        # First UNQUOTED ``#`` starting a comment — a ``<<`` after it is commented
        # out, not an opener.
        comment_at = None
        for ci, cch in enumerate(line):
            if cch == "#" and not _quoted(ci) and (ci == 0 or line[ci - 1] in " \t"):
                comment_at = ci
                break
        for m in _RM_HEREDOC_OPEN_RE.finditer(line):
            s = m.start()
            if _single(s):
                continue  # single-quoted (possibly opened on an earlier line) → literal
            if s > 0 and line[s - 1] == "\\":
                continue  # backslash-escaped → literal
            # A DOUBLE-quoted ``<<`` is literal UNLESS inside an open ``$(…)`` /
            # backtick (``--body "$(cat <<EOF …)"``).
            if _quoted(s) and not _cmdsub(s):
                continue
            # Arithmetic shift ``$(( a << b ))``: a ``((`` opened before with no
            # closing ``))`` yet means ``<<`` is an operator, not a heredoc.
            if line[:s].count("((") > line[:s].count("))"):
                continue
            # A ``<<`` INSIDE a parameter expansion ``${…}`` is expansion text, not a
            # redirection (``echo ${v:-<<'x'}``). An unclosed ``${`` before this
            # position (more ``${`` than ``}``) means ``<<`` opens no heredoc -- so
            # the stripper does not take the ``${v:-<<'x'}`` as a quoted heredoc and
            # discard a real trailing ``rm -rf "$HOME"`` (GPT 6.1).
            if line[:s].count("${") > line[:s].count("}"):
                continue
            # A ``<<`` after an unquoted ``#`` is a COMMENT — opens no heredoc, so a
            # following ``rm`` line is a REAL command.
            if comment_at is not None and s > comment_at:
                continue
            # Take the WHOLE delimiter word and strip surrounding quotes, so
            # ``<<'EOF'`` / ``<<EOF'X'`` delimit on the dequoted word and a partial
            # ``EOF`` is not mistaken for the terminator.
            raw_delim = m.group(1)
            delim = _rm_strip_all_quotes(raw_delim)
            # A QUOTED delimiter (``<<'EOF'`` / ``<<"EOF"``) suppresses ALL expansion
            # in the body — it is pure data. An UNQUOTED delimiter (``<<EOF``) lets
            # the shell RUN ``$(…)`` / backticks in the body before feeding stdin to
            # the command, so those executed substitutions must still be
            # classified rather than silently stripped.
            delim_quoted = raw_delim != delim
            # ``<<-`` (dash) strips LEADING TABS from body lines and the terminator;
            # a plain ``<<`` matches the terminator EXACTLY. Record which so the
            # terminator test below strips tabs only for ``<<-`` and never trims a
            # trailing space (``EOF `` is body, not the terminator -- GPT 6.1).
            delim_dash = m.group(0).lstrip()[:3] == "<<-"
            break
        if delim is None:
            i += 1
            continue
        # Drop the body up to the terminator (line == dequoted delimiter; ``<<-``
        # allows leading tabs). If NO terminator exists before EOF the ``<<`` runs
        # its body to EOF, so STOP: every remaining line is that heredoc's data, and
        # continuing to probe each later line as its own opener is O(lines**2) and
        # froze the gate on a ``:<<x`` flood. Terminator indices for
        # a delimiter are found by one forward pass, each line visited once overall.
        j = i + 1
        term = -1
        while j < n:
            body = lines[j]
            # POSIX: the terminator is the delimiter word ALONE on a line, matched
            # EXACTLY. ``<<-`` (dash) strips LEADING TABS from the terminator; a
            # plain ``<<`` does not, and NEITHER trims a trailing space -- ``EOF ``
            # is heredoc DATA, not the terminator, so a following ``rm -fr /`` stays
            # inside the body (GPT 6.1 over-refusal).
            candidate = body.lstrip("\t") if delim_dash else body
            if candidate == delim:
                term = j
                break
            j += 1
        if term < 0:
            # Unterminated opener. A QUOTED delimiter (``<<'EOF'``) is unambiguously
            # a real heredoc -- its body runs to EOF as pure data with no expansion,
            # so DROP it (``cat <<'EOF'\nrm -fr /`` is prose, allowed). An UNQUOTED
            # ``<<x`` is often NOT a heredoc at all but a ``<<`` bash never reads as
            # one (``echo ${v:-<<x}``, ``echo $[1<<2]``, ``"$(echo "<<x")"``)
            # mis-detected as an opener; dropping the rest would silently discard a
            # real trailing ``rm -rf $HOME``. For the unquoted case FAIL SAFE: KEEP
            # every remaining line for classification. Either way STOP probing
            # further openers here -- the kept lines are emitted as data, not
            # re-scanned as their own openers, so this stays O(1), not the
            # O(lines**2) re-probe that froze the gate.
            if not delim_quoted:
                out.extend(lines[i + 1 :])
            break
        # An UNQUOTED heredoc runs ``$(…)`` / backticks in its body before the
        # command reads stdin, so KEEP those executed substitutions (drop only the
        # literal prose around them); a QUOTED delimiter suppresses expansion, so
        # its whole body is pure data and is dropped.
        if not delim_quoted:
            body_text = "\n".join(lines[i + 1 : term])
            kept = _substitution_bodies(body_text)
            if kept:
                out.append(" ; ".join(kept))
        out.append(lines[term])  # keep the terminator word, drop the body between
        i = term + 1
    return "\n".join(out)


def _rm_strip_comments(source: str) -> str:
    """*source* with every shell COMMENT removed (from an unquoted word-initial
    ``#`` up to, not including, the next newline). A comment is never executed, so
    ``make clean # safer than rm -fr ~`` runs no ``rm``. Quote
    state comes from the shared ``_iter_shell_chars`` machine and restarts unquoted
    after each comment, so a quote inside comment text cannot open a span that
    hides the next line."""
    if "#" not in source:
        return source
    out: "list[str]" = []
    start = 0
    n = len(source)
    while start < n:
        cut = None
        for step in _iter_shell_chars(source[start:]):
            if step.active and step.char == "#" and _opens_comment(source, start + step.offset):
                cut = start + step.offset
                break
        if cut is None:
            out.append(source[start:])
            break
        out.append(source[start:cut])
        newline = source.find("\n", cut)
        if newline < 0:
            break
        start = newline
    return "".join(out)


#: A ``$home`` / ``${home…}`` / ``home=`` variable spelling in the LOWERCASED view,
#: for finding candidate home-variable references whose ORIGINAL case must be
#: checked against the raw command. ``~`` carries no case and is never a candidate.
_RM_HOME_VAR_CANDIDATE_RE = re.compile(r"\$\{?home|(?:^|[\s;&|(])home=", re.IGNORECASE)


def _rm_neutralize_lowercase_home_vars(text_lower: str, raw_text: "str | None") -> str:
    """Blank a ``$home`` / ``home=`` reference whose RAW spelling is not ``$HOME``.

    The whole floor classifies ``text_lower``, and the home matchers spell ``home``
    case-insensitively, so a user's own lowercase variable (``home=./build; rm -fr
    "$home"``) reads as the home directory and a relative cleanup is refused. A
    shell variable name is CASE-SENSITIVE -- ``$home`` is a different, usually-unset
    variable from ``$HOME`` -- so a home spelling is the home dir only when the
    original command wrote the all-caps ``HOME``. ``raw_text`` is ``text_lower``'s
    source (``tool_name.lower()``) and ASCII ``.lower()`` preserves length, so the
    two align by offset; any ``home`` candidate whose raw characters are not exactly
    ``HOME`` is replaced with a non-matching sentinel of equal length (lengths and
    all other offsets are preserved, so the aligned ``raw_text`` the caller strips
    in lockstep stays aligned). A bare ``~`` is untouched -- it has no case and IS
    the home dir in every spelling.
    """
    if raw_text is None or "home" not in text_lower:
        return text_lower
    if len(raw_text) != len(text_lower):
        # A length-changing lowercase (e.g. Turkish ``İ`` -> two chars) breaks the
        # offset alignment the per-candidate case read relies on. If the ORIGINAL
        # text contains no literal ``HOME`` at all, then every ``home`` spelling in
        # the lowercased view came from a non-``HOME`` variable, so neutralize them
        # all -- a lowercase ``home=./build`` is a different variable and its
        # cleanup must stay allowed (GPT 6.1 over-refusal). When the raw text DOES
        # carry ``HOME``, offsets cannot be trusted here, so leave it to the matcher
        # (fail CLOSED: a real ``$HOME`` wipe still denies).
        if "HOME" in raw_text:
            return text_lower
        out = list(text_lower)
        for m in _RM_HOME_VAR_CANDIDATE_RE.finditer(text_lower):
            h = text_lower.find("home", m.start())
            if h < 0:
                continue
            for k in range(h, h + 4):
                out[k] = _RM_LITERAL_METACHAR_SENTINEL
        return "".join(out)
    out = list(text_lower)
    for m in _RM_HOME_VAR_CANDIDATE_RE.finditer(text_lower):
        # Locate the 4-letter ``home`` run inside the match and read its raw case.
        h = text_lower.find("home", m.start())
        if h < 0 or h + 4 > len(raw_text):
            continue
        if raw_text[h : h + 4] != "HOME":
            for k in range(h, h + 4):
                out[k] = _RM_LITERAL_METACHAR_SENTINEL
    return "".join(out)


def _rm_neutralize_case_variant_home_paths(text_lower: str, raw_text: "str | None") -> str:
    """Blank a literal home-path spelling whose RAW case is NOT the real home's.

    The floor classifies ``text_lower``, so an absolute path is already lowercased
    and the home-equality check cannot tell the real home (``/home/alice``) from a
    case-variant SIBLING (``/home/ALICE``) that, on a case-SENSITIVE filesystem, is
    a different directory -- so a cleanup of the sibling is refused as a home wipe
    (GPT 6.1 F3). ``raw_text`` is ``text_lower``'s source and ASCII ``.lower()``
    preserves length, so the two align by offset: for each occurrence of the
    lowercased home substring whose offset-aligned RAW characters are not exactly
    the original-case home, replace that run with an equal-length sentinel so the
    home matchers do not read it as the home dir. On a case-INSENSITIVE platform
    (``/Users/Alice`` IS ``/users/alice``) this is a no-op, so folding still
    governs there. A bare ``~`` / ``$HOME`` carries no literal case and is untouched
    -- it is the home dir in every spelling and must still deny."""
    if _RM_FS_CASE_INSENSITIVE or raw_text is None or len(raw_text) != len(text_lower):
        return text_lower
    home_raw = _rm_expanded_home_raw_case()
    if not home_raw:
        return text_lower
    home_lower = home_raw.lower()
    if home_lower not in text_lower:
        # The lowercased home never appears: nothing a case-variant could be
        # confused with here.
        return text_lower
    out = list(text_lower)
    width = len(home_lower)
    start = 0
    while True:
        h = text_lower.find(home_lower, start)
        if h < 0:
            break
        start = h + 1
        if h + width > len(raw_text):
            continue
        # The real home, case-sensitively, is untouched; only a different-case
        # spelling of the same lowercased path is blanked.
        if raw_text[h : h + width] != home_raw:
            for k in range(h, h + width):
                out[k] = _RM_LITERAL_METACHAR_SENTINEL
    return "".join(out)


def _recursive_force_rm_targets(
    text_lower: str, *, raw_text: "str | None" = None
) -> "frozenset[str]":
    """Which catastrophic target(s) a top-level ``rm`` recursively force-deletes.

    Returns a subset of ``{"root", "home"}`` — ``root`` when a resolved operand IS
    the filesystem root, ``home`` when one IS the home dir (``~`` or ``$HOME``).
    Empty when it is not a recursive-force ``rm`` against such an EXACT target; a
    descendant (``/tmp/x``, ``$HOME/.cache``) and a mere mention both return empty.

    ``--no-preserve-root`` is a trigger on its own; otherwise BOTH a recursive and a
    force flag must be present, in any position. A ``--`` stops flag parsing.

    Every command FRAME is inspected — the top-level argv and every nested shell
    payload (``bash -c '…'``, ``$(…)``, here-string, chained segment) — re-split
    from its RAW source (quote-resolved but ENV-UNEXPANDED), so ``$HOME`` is read by
    spelling, not the expanded path. Frame scoping keeps a string that is merely an
    argument to another program (a ``git commit -m`` message) from reading as ``rm``.

    *raw_text* is the ORIGINAL-case command when the caller has it. Bash's ANSI-C
    unicode escapes are width-case-sensitive (``\\u`` 4 digits, ``\\U`` 8), so a
    ``$'\\U…'`` spelling decodes correctly only from case-preserved text; when
    supplied, its ANSI-C spans are decoded then lowercased and walked as an extra
    frame. It also carries the uppercase ``HOME=`` the reassignment check needs.
    """
    # Cheap necessary condition. A plain ``rm`` invocation contains the literal
    # ``rm``; an OBFUSCATED one (``"r\<nl>m"``, ``$'r\555'``) does not — its ``rm``
    # is built by escape/quote/substitution machinery whose decoded output can be
    # any character, so the only sound cheap gate is "contains ``rm`` OR contains
    # such machinery". When neither is present the walk cannot yield an ``rm``.
    if "rm" not in text_lower and not _RM_OBFUSCATION_MACHINERY_RE.search(text_lower):
        return frozenset()
    # A ``$home`` / ``home=`` the user wrote in lowercase is a DIFFERENT variable
    # from ``$HOME`` (shell names are case-sensitive); blank it before the home
    # matchers -- which spell ``home`` case-insensitively over this lowercased view
    # -- read it as the home dir and refuse a relative cleanup. Done while
    # ``text_lower`` and ``raw_text`` still align by offset (before the strips),
    # with an equal-length sentinel so the lockstep stripping below stays aligned.
    text_lower = _rm_neutralize_lowercase_home_vars(text_lower, raw_text)
    # A literal path that case-FOLDS to the real home but whose RAW case differs
    # (``/home/ALICE`` vs home ``/home/alice``) is a DIFFERENT directory on a
    # case-sensitive filesystem; blank it before the home matchers -- which compare
    # over this lowercased view -- read it as the home dir and refuse a sibling's
    # cleanup (GPT 6.1 F3). Done while ``text_lower`` and ``raw_text`` still align
    # by offset, with an equal-length sentinel so the lockstep stripping stays
    # aligned. A no-op on a case-insensitive platform.
    text_lower = _rm_neutralize_case_variant_home_paths(text_lower, raw_text)
    # A pathological opener chain (``"$( " * 1000``) makes the walk emit a frame
    # per opener and reprocess O(openers²). Far past any real command
    # we must NOT skip classification — that fails OPEN: ``: `` + 201×``"$(true)"``
    # + ``; rm -fr ~`` has its wipe OUTSIDE every substitution, invisible to the
    # whole-text ``-rf`` regex. Disable only the expensive descent;
    # the top-level per-command argv classification below still runs.
    heavy_substitution = (
        text_lower.count("$(") + text_lower.count("`") > _RM_SUBSTITUTION_OPENER_CAP
    )
    # A heredoc body is stdin DATA, never parsed as commands — strip it from BOTH
    # views BEFORE the walk, so the walk never descends a ``$(…)``/backtick that is
    # really a markdown span in a commit/PR/doc heredoc naming ``rm -fr /`` as prose.
    # An evaluator heredoc (``bash <<EOF``) that executes its
    # stdin is still caught by the whole-text regex on the contiguous literal.
    text_lower = _rm_strip_heredoc_bodies(text_lower)
    if raw_text is not None:
        raw_text = _rm_strip_heredoc_bodies(raw_text)
    # A trailing ``# …`` comment is never executed, so an ``rm`` named in it is not
    # a command (e.g. ``make clean # safer than rm -fr ~``).
    text_lower = _rm_strip_comments(text_lower)
    if raw_text is not None:
        raw_text = _rm_strip_comments(raw_text)
    # A ``bash -c '<payload>'`` whose shell is an operand of a data consumer
    # (``echo bash -c '…'`` / ``cat sh -c '…'``) PRINTS the payload — it is never
    # executed, so the frame the walk descends from it must not be classified
    # (base allowed these mentions). Collect those payload strings
    # from the top-level argv so the matching descended frame can be skipped.
    shell_c_data = _rm_shell_c_data_payloads(_split_shell_words(text_lower))
    found: set[str] = set()
    # Under a pathological opener chain, skip the O(openers²) walk but STILL
    # classify the top-level command: split on unquoted newlines + top-level ``;``
    # and classify each simple command's argv (raw + decoded), so ``…; rm -fr ~``
    # outside the openers is caught — linear, no substitution descent
    # (budget exhaustion must not fail open).
    if heavy_substitution:
        heavy_confirmed_home = False
        for line in _rm_split_unquoted_newlines(text_lower):
            for seg in _rm_split_top_level_semicolons(line):
                toks = _split_shell_words(seg)
                found |= _rm_targets_in_argv(toks, strip_quotes=False)
                found |= _rm_targets_in_argv(toks, strip_quotes=True)
                # Also run the de-quoted exact root/home scan so a SPLIT spelling
                # (``r''m``) whose program word only resolves after quote removal is
                # caught on the heavy path too -- matching the substitution-body and
                # tail scans (49 ``"$(true)"`` decoys then ``; r''m -fr ~`` trips the
                # heavy path at the opener cap, so the top-level segment is where the
                # wipe lands). Run it on the quote-preserving ``seg`` so an operand
                # keeps its literal ``;`` rather than splitting the span.
                found |= _rm_frame_overflow_targets([], raw_source=seg)
                # The outer ``rm``'s own ``$(…)`` operand resolves to its OUTPUT:
                # ``rm -rf <decoys> $(echo ~)`` keeps the unresolved ``$(echo ~)``
                # in this split and misses home, so a wipe whose home target is the
                # STATIC output of a benign producer escapes the per-segment pass
                # (budget exhaustion must not fail open). Reuse
                # the narrow ``echo``/``printf`` resolver the non-heavy walk runs; a
                # dynamic generator resolves to a non-matching sentinel, so this
                # only ADDS coverage.
                resolved_seg = _rm_resolve_substitution_operands(toks)
                if resolved_seg is not None:
                    resolved_targets = _rm_targets_in_argv(resolved_seg, strip_quotes=True)
                    found |= resolved_targets
                    # A ``home`` from the RESOLVED operand is CONFIRMED (the shell's
                    # own producer output is home), so it is exempt from the final
                    # quoted-home suppression -- which re-reads only ``text_lower``,
                    # where the home spelling lives inside the UNRESOLVED ``$(…)`` and
                    # so is invisible. Without this the confirmed wipe failed open
                    # (e.g. ``rm -rf <decoys> $(echo ~)``).
                    if "home" in resolved_targets:
                        heavy_confirmed_home = True
            if {"root", "home"} <= found:
                break
        # A wipe can live INSIDE a substitution body (``echo "$(true)"*201
        # "$(rm -fr ~)"``), invisible to the top-level per-command split above and
        # to the whole-text regex (no contiguous ``rm -rf`` run). The expensive
        # part was the recursive FRAME WALK (O(openers²)); classify each INNERMOST
        # substitution body with a flat regex (the quote-aware paren walk is
        # defeated by a dense ``"$(true)"`` run), bounded by ``_RM_DESCENT_BUDGET``
        # bodies -- a wipe past the cap is still caught by the fail-closed
        # whole-text regex.
        if not ({"root", "home"} <= found):
            seen = 0
            # A ``$(`` / backtick inside a SINGLE-quoted span is LITERAL text, never
            # executed, so its body is not a command: ``echo '$(rm -fr /)'`` only
            # prints (GPT 6.1 over-refusal in the heavy fallback). Skip a match whose
            # opener offset is single-quoted.
            single_positions = _rm_single_quoted_positions(text_lower)
            for m in _RM_INNERMOST_SUBST_RE.finditer(text_lower):
                if seen >= _RM_SUBST_BODY_SCAN_CAP:
                    break
                seen += 1
                if m.start() < len(single_positions) and single_positions[m.start()]:
                    continue  # literal ``$(…)`` inside single quotes -- printed, not run
                body = m.group(1) if m.group(1) is not None else (m.group(2) or "")
                body_toks = _split_shell_words(body)
                found |= _rm_targets_in_argv(body_toks, strip_quotes=True)
                # Also run the cheap de-quoted exact root/home scan so a SPLIT
                # spelling (``r''m``) whose program word only resolves after
                # quote removal is caught here too, matching the frame walk. Scan
                # the quote-preserving ``body`` so a literal ``;`` in an operand
                # does not split the span.
                found |= _rm_frame_overflow_targets([], raw_source=body)
                if {"root", "home"} <= found:
                    break
        # A dense run of UNBALANCED openers (``echo "$(true" * 199 rm -fr ~``) has
        # no closing ``)``, so the innermost-body regex above matches nothing and
        # the trailing ``rm -fr ~`` -- which bash runs INSIDE the last open
        # substitution -- is read by the top-level split as the data consumer
        # ``echo``'s argument, not an executed command. Do ONE bounded level of
        # descent: take the text after the LAST unbalanced ``$(`` / backtick opener
        # and classify it as its own command. Fixed single level, no recursion, O(n)
        # over the text -- the recursive frame walk (its O(openers**2) cost is why
        # this heavy path exists) is NOT re-entered. Deeper nesting past this one
        # level is a residual left to the whole-text regex net and the sandbox.
        if not ({"root", "home"} <= found):
            tail = _rm_last_unbalanced_substitution_tail(text_lower)
            if tail is not None:
                # The tail begins mid-way through the opener's own body
                # (``true"  rm -rf /``) and carries the opener's dangling quote, which
                # would fuse the whole tail into one unterminated-quote token. Drop
                # bare ``"`` / ``'`` chars so the trailing command's words split
                # normally, then classify the tail as one command.
                # Run the de-quoted exact scan on the RAW tail first (it removes
                # quotes internally, so a split ``r''m`` program word resolves);
                # then drop stray quote chars for the plain argv pass, which needs
                # the trailing command's words to split normally.
                found |= _rm_frame_overflow_targets([], raw_source=tail)
                tail = tail.replace('"', " ").replace("'", " ")
                found |= _rm_targets_in_argv(_split_shell_words(tail), strip_quotes=True)
        # The heavy fallback skips the recursive frame walk, which is where an
        # EXECUTED shell ``-c`` payload (``bash -c 'rm -fr "$HOME"'``) is normally
        # classified, so a ``bash -c`` / ``sh -c`` home/root wipe buried in a
        # heavy-substitution input failed OPEN. Run the same
        # executed-``-c``-payload pass the non-heavy path runs: classify each
        # payload's tokens in its OWN quote context, and a home verdict with a LIVE
        # home expansion in the payload is CONFIRMED (exempt from the suppression).
        for payload in _rm_shell_c_executed_payloads(_split_shell_words(text_lower)):
            # Split the payload into its command LINES first: a shell runs each line
            # of a multi-line ``-c`` body as its own command, but a single
            # ``_split_shell_words`` flattens the newline to whitespace, so a later
            # line's ``~`` (``rm -rf ./dist\ncd ~\npwd``) was read as an operand of
            # the ``rm`` on the first line -- a false home wipe (Security Scope).
            payload_targets: frozenset[str] = frozenset()
            for _pline in _rm_split_unquoted_newlines(payload):
                payload_targets = payload_targets | _rm_targets_in_argv(
                    _split_shell_words(_pline), strip_quotes=True
                )
            found |= payload_targets - {"home"}
            # The payload is EXECUTED, so its home is confirmed on a live
            # ``$HOME``/``~`` expansion OR an absolute path equal to the real home
            # (``rm -fr /home/alice`` where that IS $HOME) -- both are real wipes the
            # top-level ``text_lower`` carries only inside the quoted ``-c`` body.
            if "home" in payload_targets and (
                _rm_has_live_home_expansion(payload.lower()) or _rm_absolute_home_operand(payload)
            ):
                found.add("home")
                heavy_confirmed_home = True
        if (
            "home" in found
            and not heavy_confirmed_home
            and not _rm_has_live_home_expansion(text_lower)
            and not _rm_absolute_home_operand(text_lower)
        ):
            found.discard("home")
        return frozenset(found)
    # A ``home`` verdict confirmed from a RESOLVED command-substitution operand
    # (``rm -rf $(printf %s/me /home)`` = a real ``/home/me`` wipe) is likewise
    # exempt from the final suppression: the home spelling lives in the resolved
    # OUTPUT, which the original text does not carry as a live token, so the
    # suppression's ``text_lower`` re-check cannot see it and would fail the wipe
    # open. Set by the resolved-substitution pass; false otherwise.
    subst_confirmed_home = False
    # A ``home`` verdict confirmed inside an EXECUTED NESTED frame -- a ``bash -c``
    # / ``sh -c`` payload body, or a ``$(…)`` / backtick execution body the shell
    # actually RUNS -- is a real wipe (``bash -c 'rm -fr /home/alice'``,
    # ``echo "$(rm -fr /home/alice)"``). Its home operand lives in the nested
    # frame's source, not as a live token of the top-level text, so the final
    # quoted-home suppression's ``text_lower`` re-check cannot see it and would
    # fail the wipe open. The walk's
    # first frame is the whole top-level command; every LATER frame is such an
    # executed descent, so a ``home`` that a later frame introduces is confirmed.
    frame_confirmed_home = False
    # Shared TOKEN budget across all budget-exhausted frames. A short deep frame
    # (``echo ~ | xargs rm -fr`` at nesting depth 65) costs a handful of tokens and
    # is still reached; a flood of long suffix frames (200 ``;``-joined commands)
    # spends it fast and stops. Bounds the past-budget flat classification to O(1)
    # total rather than O(frames x tokens).
    flat_token_budget = _RM_FLAT_TOKEN_BUDGET
    # Cap how many frames are CLASSIFIED. A pathological opener chain
    # (``"$( " * 1000``) makes the shell walk emit a frame per opener; classifying
    # every one is O(frames × span) and ran for minutes on the synchronous gate.
    # The cap fails CLOSED — base's contiguous
    # ``rm -rf /`` / ``rm -rf ~`` literal still runs on the whole text — so a wipe
    # hidden past the cap is caught by base, never allowed.
    frames_left = _RM_DESCENT_BUDGET
    for frame_index, (source, norm_tokens, repaired) in enumerate(
        _rm_walk_frames(text_lower, raw_text)
    ):
        home_before_frame = "home" in found
        if frames_left <= 0:
            # Budget exhausted. Do NOT fail open (``rm -fr ~`` behind 65 nested
            # ``$(`` must still deny). The remaining frames are overlapping suffixes
            # OR deep nested bodies; classifying each in FULL is O(frames x tokens)
            # (a 200-command line stalled the gate under coverage). Two tiers:
            #
            # (1) ALWAYS run the CHEAP exact root/home scan on EVERY remaining frame
            # (``_rm_frame_overflow_targets`` -- one linear pass, no brace/candidate
            # machinery, matches ``rm`` on its de-quoted spelling so ``r''m`` is
            # caught). It cannot be exhausted, so a wipe hidden past the shared token
            # budget (120 padded substitutions then ``echo "$(r''m -fr ~)"``) is
            # still classified rather than silently skipped.
            # The whole-text deny-net regex does NOT cover a split
            # spelling, so this scan is the net that does.
            #
            # (2) The EXPENSIVE full classification (brace/dot/candidate expansion)
            # stays gated on the shared token budget, bounding total work.
            #
            # Run the cheap scan on the QUOTE-PRESERVING source (per unquoted line),
            # not the de-quoted ``norm_tokens`` -- an operand's literal ``'a;b'``
            # must keep its quotes so the glued-boundary split does not read the
            # ``;`` as a command boundary and drop the trailing home operand.
            for ov_line in _rm_split_unquoted_newlines(source):
                found |= _rm_frame_overflow_targets([], raw_source=ov_line)
            if flat_token_budget > 0:
                flat_token_budget -= len(norm_tokens)
                found |= _rm_targets_in_argv(norm_tokens, strip_quotes=False, _budget=[0])
                for line in _rm_split_unquoted_newlines(source):
                    found |= _rm_targets_in_argv(
                        _split_shell_words(line), strip_quotes=True, _budget=[0]
                    )
            if {"root", "home"} <= found:
                break
            continue
        frames_left -= 1
        # Skip a descended payload frame that is a data-consumer's printed ``-c``
        # mention, not an executed command.
        if source.strip() in shell_c_data:
            continue
        # The DECODED view (payload walk's own tokens) is always classified: it
        # resolves ANSI-C / unicode escapes and env expansion, so ``rm -rf $'/'`` /
        # ``$'\u002f'`` is caught, and a ``$'"/"'`` filename's LITERAL quotes stay in
        # the token so it is NOT misread as root. A frame spanning unquoted NEWLINES
        # is classified PER LINE — the shell runs each line separately, so a later
        # line's tokens must not fuse into an earlier ``rm``'s argv.
        #
        # The program and flag WORDS are taken from this decoded view (so ``r''m`` ->
        # ``rm`` and ``-f$'r'`` -> ``-fr`` resolve), but each OPERAND keeps its
        # quote-preserving source: a decoded operand drops its quotes, and the one
        # quote-aware command-boundary split would then read a literal ``'a;b'``
        # filename's ``;`` as a boundary and drop the trailing home operand.
        source_lines = _rm_split_unquoted_newlines(source)
        decoded_targets: set[str] = set()
        if len(source_lines) == 1:
            merged = _rm_decoded_argv_preserving_operands(_split_shell_words(source), norm_tokens)
            decoded_targets |= _rm_targets_in_argv(merged, strip_quotes=False)
        else:
            for line in source_lines:
                decoded_targets |= _rm_targets_in_argv(_split_shell_words(line), strip_quotes=False)
        # A decoded ``home`` verdict can be SPURIOUS: the payload walk tilde/HOME-
        # expands a SINGLE-QUOTED ``'~'`` / ``'$HOME'`` operand (a literal cwd file
        # bash never expands) to the real home path, which the home matcher then
        # equals (``cd ~/src && rm -fr '~'`` deletes a file named ``~``; base allows
        # it). Re-classify the DECODED view of the source with its
        # single-quoted spans MASKED to spaces: if ``home`` disappears, it came only
        # from a single-quoted literal and must not count. ``root`` is unaffected (a
        # single-quoted ``'/'`` is still the literal root bash deletes), and a frame
        # with no single-quoted span skips the re-check.
        # A decoded ``home`` verdict can be SPURIOUS: the payload walk tilde/HOME-
        # expands an operand whose ``~``/``$HOME`` is QUOTED or ESCAPED and so is a
        # literal cwd file bash never expands (``'~'`` / ``"~"`` / ``\~`` / ``'$HOME'``),
        # to the real home path, which the home matcher then equals — kept alive only
        # by an unrelated live ``~`` elsewhere on the line (``cd ~/src && rm -fr '~'``
        # deletes a file named ``~``; base allows it). Re-classify the
        # DECODED view of the source with its NON-LIVE home spellings neutralised: if
        # ``home`` disappears, it came only from a literal and must not count. ``root``
        # is unaffected (a quoted ``'/'`` is still the literal root bash deletes), and a
        # live ``"$HOME"`` / bare ``~`` is preserved so a genuine wipe still denies.
        if "home" in decoded_targets:
            masked_src = _rm_mask_non_live_home(source)
            if masked_src != source:
                # Re-classify to see whether the ``home`` verdict survives once the
                # non-live home spellings are neutralised. Mask each merged-argv
                # TOKEN in place and keep the operand's own quoting, rather than
                # masking the raw TEXT: masking the text drops a double-quote
                # delimiter, which re-exposes a literal ``;``/``|`` inside a
                # double-quoted operand (``"a;b"`` -> ``a;b``) and re-triggers the
                # boundary mis-split that would wrongly drop a live home target
                # elsewhere in the span. A token masked to only blanks/empties is a
                # neutralised non-live literal and is dropped so it cannot act as a
                # spurious boundary either.
                masked_home: set[str] = set()
                for mline in _rm_split_unquoted_newlines(source):
                    merged_line = _rm_decoded_argv_preserving_operands(
                        _split_shell_words(mline),
                        next(
                            (toks for _s, toks in _shell_payload_walk(mline)),
                            _split_shell_words(mline),
                        ),
                    )
                    masked_tokens: list[str] = []
                    for tok in merged_line:
                        masked = _rm_mask_non_live_home(tok)
                        # A token masked to nothing (a single-quoted ``'~'`` literal)
                        # contributes no operand; drop it so it is not an empty token.
                        if masked.strip() == "" and tok.strip() != "":
                            continue
                        # Keep the operand's literal quoting so a neutralised double-
                        # quoted ``"a;b"`` does not re-expose its ``;`` as a boundary.
                        masked_tokens.append(tok if masked == _rm_strip_all_quotes(tok) else masked)
                    masked_home |= _rm_targets_in_argv(masked_tokens, strip_quotes=False)
                    if {"root", "home"} <= masked_home:
                        break
                if "home" not in masked_home:
                    decoded_targets.discard("home")
        found |= decoded_targets
        # A REPAIRED frame (ANSI-C-decoded copy) is classified ONLY via its decoded
        # tokens above. Its raw source went through ``_decode_shell_quoted_literals``
        # + ``shlex.quote``, so a quote-stripping raw split would peel BOTH the added
        # shell quotes and the LITERAL decode quotes (``$'"/"'`` -> ``/``), reading a
        # filename as root. Its raw ``$HOME``/``~`` is already covered by the
        # ORIGINAL-text frame.
        if repaired:
            if {"root", "home"} <= found:
                break
            continue
        # Non-repaired frame: also classify the RAW split, which keeps ``$HOME`` /
        # ``~`` unexpanded so home is classified by its written spelling. Surrounding
        # SHELL quotes are stripped only here (``"$HOME"`` -> ``$HOME``). Split on
        # unquoted newlines first so a multi-line frame is classified per line.
        for line in source_lines:
            found |= _rm_targets_in_argv(_split_shell_words(line), strip_quotes=True)
        # A command-substitution OPERAND resolves to its OUTPUT: ``rm -rf
        # "$(printf /)"`` keeps the unresolved ``$(printf /)`` and misses the root.
        # Resolve each ``$(…)`` / backtick operand to the word
        # it STATICALLY expands to (the narrow ``echo``/``printf`` resolver) and
        # re-classify. A dynamic generator resolves to a non-matching sentinel, so
        # this only ADDS coverage.
        resolved_subst = _rm_resolve_substitution_operands(_split_shell_words(source))
        if resolved_subst is not None:
            subst_targets = _rm_targets_in_argv(resolved_subst, strip_quotes=True)
            found |= subst_targets
            if "home" in subst_targets:
                subst_confirmed_home = True
        # Execution-substitution bodies: a ``$(…)`` / backtick / process-sub that
        # EXECUTES its command, so an ``rm`` inside is a real wipe even when the
        # output is consumed as data (``grep -rn "$(rm -rf /)"`` runs the wipe
        # first). The shared ``_substitution_bodies`` walk is quote-aware for its own
        # nesting but still extracts a backtick / ``$(…)`` body sitting inside a
        # SINGLE-quoted span, which bash treats as a literal (``git commit -m 'see
        # `rm -rf /`'`` runs no ``rm``). So mask the
        # single-quoted spans (via the kept ``_rm_single_quoted_positions``) to
        # spaces before the walk; the surviving ``$(…)`` / backtick bodies execute.
        # A bare ``(…)`` subshell / ``${ …;}`` funsub holding an exact-root wipe
        # stays caught by the fail-closed catalog regex UNION. Each body is
        # classified as its own argv.
        _sq = _rm_single_quoted_positions(source)
        _masked = "".join(" " if (i < len(_sq) and _sq[i]) else c for i, c in enumerate(source))
        for body in _substitution_bodies(_masked):
            body_targets = _rm_targets_in_argv(_split_shell_words(body), strip_quotes=True)
            found |= body_targets
            # A ``$(…)`` / backtick body EXECUTES, so a ``home`` wipe inside it is a
            # confirmed real wipe (``echo "$(rm -fr /home/alice)"`` runs the rm). Its
            # operand lives in the body, not as a live top-level token, so exempt it
            # from the final quoted-home suppression.
            if "home" in body_targets:
                frame_confirmed_home = True
        # A ``home`` verdict introduced by a LATER (executed, nested) frame -- a
        # ``bash -c`` / ``sh -c`` payload body or a ``$(…)`` / backtick execution
        # body -- is confirmed: the shell runs that frame, so its ``rm -fr
        # /home/alice`` is a real wipe even though the top-level text carries the
        # operand only inside quotes/a substitution. Exempt it from the final
        # suppression.
        if frame_index > 0 and "home" in found and not home_before_frame:
            frame_confirmed_home = True
        if {"root", "home"} <= found:
            break
    # An EXECUTED shell ``-c`` payload (``bash -c 'rm -fr "$HOME"'``) runs in a
    # CHILD shell that expands ``$HOME``/``~`` by SPELLING, so classify the payload
    # tokens directly — the walk's ``$HOME``->login-home expansion is ``/``-rooted
    # only where ``expanduser`` is, so a drive-path home (Windows CI) forms no home
    # verdict. ``root`` always counts; ``home`` only on a LIVE home
    # expansion in the payload's OWN quote state (``"$HOME"`` live, ``'$HOME'`` not).
    payload_live_home = False
    for payload in _rm_shell_c_executed_payloads(_split_shell_words(text_lower)):
        # Classify each command LINE of a multi-line ``-c`` payload separately, so a
        # later line's operand is not fused into an earlier line's ``rm`` argv
        # (``rm -rf ./dist\ncd ~\npwd`` must not read ``cd``'s ``~`` as a wipe --
        # Security Scope). ``_split_shell_words`` alone flattens the newline.
        p_targets: frozenset[str] = frozenset()
        for _pline in _rm_split_unquoted_newlines(payload):
            p_targets = p_targets | _rm_targets_in_argv(
                _split_shell_words(_pline), strip_quotes=True
            )
        found |= p_targets - {"home"}
        if "home" in p_targets and _rm_has_live_home_expansion(payload.lower()):
            found.add("home")
            payload_live_home = True
    # A quoted-literal ``'~'`` / ``'$HOME'`` is a cwd file the shell never expands;
    # drop a ``home`` verdict with NO live home expansion. A real
    # ``$HOME``/``~``, an EXECUTED ``-c`` payload verdict (quote state applied
    # above), or an ABSOLUTE-PATH equality (``rm -fr /home/alice`` = real home, no
    # ``$HOME``/``~`` token) all KEEP it — any would wrongly drop a common
    # irreversible-wipe spelling.
    if (
        "home" in found
        and not payload_live_home
        and not subst_confirmed_home
        and not frame_confirmed_home
        and not _rm_has_live_home_expansion(text_lower)
        and not _rm_absolute_home_operand(text_lower)
    ):
        found.discard("home")
    return frozenset(found)


def _rm_mask_non_live_home(source: str) -> str:
    """*source* with every NON-LIVE home spelling blanked to spaces, LIVE ones kept.

    Bash expands ``~`` to the home dir ONLY when it is unquoted and unescaped, and
    expands ``$HOME`` when it is unquoted or DOUBLE-quoted (never single-quoted, and
    never when the ``$`` is backslash-escaped). So a literal ``'~'`` / ``"~"`` /
    ``\\~`` and a ``'$HOME'`` or ``\\$HOME`` name a cwd file, not the home dir. This
    blanks:

    * every character inside a SINGLE-quoted span (so ``'~'`` / ``'$HOME'`` vanish);
    * a ``~`` that is DOUBLE-quoted or BACKSLASH-escaped (``"~"`` / ``\\~``);
    * a BACKSLASH-escaped ``$`` (``\\$HOME``), whose ``$`` is a literal dollar bash
      never expands -- dropping the ``$`` leaves inert ``HOME`` text so the home-ref
      matcher cannot read it as a live expansion.

    A bare ``~`` and a double-quoted ``"$HOME"`` are LEFT INTACT, so a genuine home
    wipe still classifies. The result is handed to the decoded re-classification:
    if the decoded ``home`` verdict survives this blanking it was a real live
    expansion; if it disappears it came only from a literal.
    """
    out: list[str] = []
    prev_state = 0
    dropped_delimiter = False
    in_var_name = False
    for step in _iter_shell_chars(source):
        before, prev_state = prev_state, step.state
        if step.trailing_escape or _rm_quote_delimiter(step, before):
            dropped_delimiter = True  # drop the delimiter, joining the spans
            continue
        if before == 1:
            dropped_delimiter = False
            in_var_name = False
            continue  # single-quoted content is a literal -- drop it
        if len(step.text) == 2:
            # Escaped next char: ``\~`` is a literal tilde and ``\$`` a literal
            # dollar -- bash expands neither, so drop the ``~`` / ``$`` (a dropped
            # ``$`` leaves ``HOME`` as inert text, not a ``$HOME`` ref); any other
            # escape keeps the escaped char so token boundaries are preserved.
            dropped_delimiter = False
            in_var_name = False
            out.append("" if step.char in ("~", "$") else step.char)
            continue
        if step.char == "~" and before == 2:
            dropped_delimiter = False
            in_var_name = False
            continue  # double-quoted tilde is literal, not expanded -- drop it
        # A removed quote still ENDS a bare ``$name`` reference: ``"$HO"ME`` reads
        # variable ``HO`` and appends literal ``ME`` to its VALUE, not ``$HOME``.
        # When a dropped delimiter ends a live ``$name`` run with a word char after
        # it, insert a non-word sentinel so the name does not absorb the text past
        # the quote (GPT 6.1). Scoped to a ``$name`` run so a split word still fuses.
        if dropped_delimiter and in_var_name and (step.char.isalnum() or step.char == "_"):
            out.append("\x00")
        dropped_delimiter = False
        if step.char == "$":
            in_var_name = True
        elif in_var_name and not (step.char.isalnum() or step.char == "_"):
            in_var_name = False
        out.append(step.char)  # bare ~, "$HOME" (its $ kept), and ordinary text
    return "".join(out)


def _rm_single_quoted_positions(source: str) -> "list[bool]":
    """One forward pass marking each index as inside a SINGLE-quoted span.

    A single quote in bash suppresses every expansion, so a ``${`` / ``$(`` /
    backtick inside one is literal text, not a construct. A single quote INSIDE a
    double-quoted span is itself a literal apostrophe and opens no span, so BOTH
    contexts are tracked: a ``'`` toggles single-quote state only when NOT inside
    double quotes, and a ``"`` toggles double-quote state only when NOT inside
    single quotes; a backslash outside single quotes escapes the next character.
    Replaces the per-match ``_index_in_single_quote`` rescan that scanned from 0
    on every regex match — O(N**2) on the synchronous gate.
    ``mask[i]`` is True when index *i* is inside a single-quoted span.
    """
    return [state == 1 for state in _rm_quote_states(source)]


def _rm_quote_states(source: str) -> "list[int]":
    """Per-index quote state (0 unquoted, 1 single, 2 double) read from the shared
    ``_iter_shell_chars`` machine -- the state AFTER the step covering that index,
    so an opening quote is marked quoted and a closing one unquoted. Both indices
    of a backslash escape pair carry the pair's state."""
    states = [0] * len(source)
    for step in _iter_shell_chars(source):
        for k in range(step.offset, step.offset + len(step.text)):
            states[k] = step.state
    return states


def _rm_quote_delimiter(step: "_ShellChar", prev_state: int) -> bool:
    """True if *step* is a quote DELIMITER (it opened or closed a span) rather than
    a literal quote character or any other character."""
    return len(step.text) == 1 and step.char in "'\"" and step.state != prev_state


def _rm_quoted_positions(source: str) -> "list[bool]":
    """One forward pass marking each index as inside a SINGLE- OR DOUBLE-quoted
    span. Unlike :func:`_rm_single_quoted_positions` (which marks only single
    quotes, because single quotes suppress expansion), this marks either, so a
    heredoc ``<<`` operator that sits inside ``echo "<<EOF"`` is seen as literal
    text and opens no heredoc. ``mask[i]`` is True when *i*
    is quoted."""
    return [state != 0 for state in _rm_quote_states(source)]


def _rm_cmdsub_open_before(line: str, pos: int) -> bool:
    """True if index *pos* in *line* sits inside an OPEN ``$(…)`` or backtick
    command substitution — quote-aware, so a literal ``(`` in a quoted word
    (``--title 'fix(security)'``) does not count toward the balance.

    A ``$(`` opens a substitution even inside DOUBLE quotes (``--body "$(cat
    …"``); a bare ``(`` outside quotes opens a subshell; a backtick toggles one.
    A ``(`` / ``)`` inside single quotes, inside double quotes without a leading
    ``$``, or backslash-escaped is literal. Used by the heredoc gate so a ``<<``
    inside ``"$(cat <<EOF …)"`` is seen as a REAL operator while a ``<<`` in a
    plain double-quoted ``echo "<<EOF"`` stays literal.
    """
    return _rm_cmdsub_open_states(line)[min(pos, len(line))]


def _rm_cmdsub_open_states(line: str) -> "list[bool]":
    """``states[i]`` (for ``0 <= i <= len(line)``) is True iff the text BEFORE index
    *i* leaves an ``$(…)`` / backtick / subshell open. Quote state comes from the
    shared ``_iter_shell_chars`` machine; only the paren/backtick depth is counted
    here, on characters that are neither escaped nor single-quoted (and, for a bare
    ``(``, not double-quoted -- ``$(`` opens even inside double quotes)."""
    n = len(line)
    states = [False] * (n + 1)
    depth = 0
    in_backtick = False
    prev_state = 0
    for step in _iter_shell_chars(line):
        open_now = depth > 0 or in_backtick
        for k in range(step.offset, min(step.offset + len(step.text), n)):
            states[k] = open_now
        before, prev_state = prev_state, step.state
        if len(step.text) != 1 or step.trailing_escape or step.char in "'\"":
            continue  # an escape pair, or a quote (delimiter or literal)
        if before == 1:
            continue  # single-quoted: literal
        ch = step.char
        if ch == "`":
            in_backtick = not in_backtick
        elif ch == "(" and step.offset > 0 and line[step.offset - 1] == "$":
            depth += 1
        elif ch == "(" and before == 0 and not in_backtick:
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
    states[n] = depth > 0 or in_backtick
    return states


def _rm_cmdsub_open_mask(line: str) -> "list[bool]":
    """``mask[i]`` is True iff index *i* in *line* sits inside an OPEN ``$(…)`` /
    backtick / subshell command substitution -- the per-index form of
    :func:`_rm_cmdsub_open_before`, computed in ONE forward pass, so the heredoc
    gate reads it per ``<<`` opener in O(1) rather than rescanning."""
    return _rm_cmdsub_open_states(line)[: len(line)]


def _rm_substitution_depth(source: str) -> "list[int]":
    """Per-index nesting depth of command-substitution / subshell bodies.

    ``depth[i]`` is how many ``$(…)`` / ``${…}`` / backtick / bare ``(…)``
    subshell bodies index *i* sits inside. A ``HOME=`` assignment with depth > 0
    runs in a subshell and does NOT persist to the parent shell, so it cannot
    protect a parent ``rm`` from a home wipe. An opener or closer
    that is quoted or escaped is literal; quote state comes from the shared
    ``_iter_shell_chars`` machine. Backticks toggle a span rather than nest, which
    is sufficient here (a nested backtick must be escaped in bash anyway).
    """
    n = len(source)
    depth = [0] * n
    cur = 0
    in_backtick = False
    prev_state = 0
    for step in _iter_shell_chars(source):
        before, prev_state = prev_state, step.state
        i = step.offset
        ch = step.char
        literal = len(step.text) != 1 or step.trailing_escape or ch in "'\"" or before != 0
        if not literal and ch == "`":
            if in_backtick:
                depth[i] = cur
                cur -= 1
                in_backtick = False
            else:
                cur += 1
                in_backtick = True
                depth[i] = cur
            continue
        if not literal and not in_backtick:
            if ch == "(" or (ch == "{" and i > 0 and source[i - 1] == "$"):
                cur += 1
                depth[i] = cur
                continue
            if ch in ")}" and cur > 0:
                depth[i] = cur
                cur -= 1
                continue
        for k in range(i, min(i + len(step.text), n)):
            depth[k] = cur
    return depth


#: A whole-token command substitution: ``$(…)`` or a backtick pair spanning the
#: entire operand word (after surrounding shell quotes are peeled). The output
#: of such a word becomes the operand ``rm`` receives.
_RM_WHOLE_SUBSTITUTION_RE = re.compile(r"\A\$\((?P<body>.*)\)\Z|\A`(?P<btck>.*)`\Z", re.DOTALL)


def _body_has_downstream_transform(body: str) -> bool:
    """True when a command-substitution *body* carries an UNQUOTED, non-nested
    shell control operator (``|``, ``&``, ``;``, ``>``, ``<`` or a newline).

    Any of these means the first producer's output can be transformed by a
    downstream stage (``echo ~ | sed …``), appended to by a second command
    (``echo ~ ; echo /tmp``) or redirected, so the first literal
    ``_static_substitution_output`` returns is NOT the body's complete output.
    The scan is quote-aware and skips NESTED ``$(…)`` / backtick substitutions, so
    an operator belonging to an inner command does not count. A body with none of
    these is a single plain producer whose first literal is its whole output."""
    i = 0
    n = len(body)
    in_single = in_double = False
    depth = 0
    while i < n:
        ch = body[i]
        if ch == "\\" and not in_single and i + 1 < n:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if in_single or in_double:
            i += 1
            continue
        if ch == "$" and i + 1 < n and body[i + 1] == "(":
            depth += 1
            i += 2
            continue
        if ch == "`":
            # A backtick toggles into/out of a nested substitution; treat its body
            # as nested so an operator inside it does not count.
            depth = depth + 1 if depth <= 0 else depth - 1
            i += 1
            continue
        if ch == ")" and depth > 0:
            depth -= 1
            i += 1
            continue
        if depth == 0 and ch in "|&;<>\n":
            return True
        i += 1
    return False


def _producer_has_multiple_output_operands(words: "list[str]") -> bool:
    """True when an ``echo`` / ``printf`` body has MORE THAN ONE output operand.

    The shared resolver returns only the FIRST non-flag literal, but a producer
    with several operands emits them joined -- ``echo / tmp`` -> ``/ tmp``,
    ``printf %s/cache "$HOME"`` -> ``$HOME/cache`` -- so the first literal is not
    the complete output and reading it as a bare root/home target is wrong. Count
    the operands the resolver treats as output: for ``echo`` every non-flag word;
    for ``printf`` the format word plus its arguments (a lone format like
    ``printf /`` is one). A body whose program is neither is not resolved here
    anyway, so it returns False and the single-operand path is unchanged."""
    if not words:
        return False
    program = _program_basename(words[0])
    if program not in ("echo", "printf"):
        return False
    operands = [w for w in words[1:] if not _shell_normalizer._normalize_operand(w).startswith("-")]
    return len(operands) > 1


def _rm_resolve_substitution_operands(tokens: "list[str]") -> "list[str] | None":
    """``tokens`` with each command-substitution OPERAND replaced by its STATIC
    output, or ``None`` when none resolves (caller skips a redundant re-classify).

    ``rm -rf --no-preserve-root "$(printf /)"`` reaches ``rm`` with operand ``/``,
    but the raw split keeps the unresolved ``$(printf /)`` so the matchers never see
    ``/``. Resolve a whole-token ``$(…)`` / backtick operand to its static
    expansion via the sibling argv floor's ``echo``/``printf`` resolver (literal
    first operand only). A dynamic generator resolves to the ``"\\x00"`` sentinel no
    matcher accepts, so this only ADDS coverage. Flags/non-subs left untouched.
    """
    from .argv_floor import _static_substitution_output

    def _resolve_body(body: str) -> str:
        """``_static_substitution_output`` for *body*, except a ``printf`` carrying
        a ``%`` FORMAT directive returns the unresolvable sentinel. The shared
        resolver skips a ``%`` operand and returns the NEXT literal, so
        ``printf '%s/tmp/build-cache' /`` resolves to ``/`` (root) when the real
        output is ``//tmp/build-cache`` (a descendant) -- an allowed cleanup read as
        a root wipe (GPT 6.1 over-refusal). A format string's output is not the
        first-literal approximation, so leave the operand unresolved rather than
        substitute the wrong word.

        A PIPED or COMPOUND body is likewise left unresolved: the shared resolver
        reads only the FIRST producer's first literal and ignores anything after a
        ``|`` / ``&&`` / ``||`` / ``;`` / ``>`` or a second command, so
        ``$(echo ~ | sed 's|$|/.cache|')`` resolved to ``~`` when the real output
        is the ``~/.cache`` descendant -- an allowed cache cleanup read as a home
        wipe (GPT 6.1 over-refusal). The first literal is not the body's complete
        output once a downstream stage can transform it, so return the unresolvable
        sentinel; the raw ``$(…)`` token is then left in place and no matcher
        refuses it. A plain single-command producer whose whole output IS root/home
        (``$(echo /)``, ``$(printf ~)``) has no transforming stage and still
        resolves, so a real wipe still denies."""
        words = body.split()
        if (
            words
            and _program_basename(words[0]) == "printf"
            and any("%" in _shell_normalizer._normalize_operand(w) for w in words[1:])
        ):
            return "\x00"
        if _body_has_downstream_transform(body):
            return "\x00"
        if _producer_has_multiple_output_operands(words):
            # A producer with more than one output operand (``echo / tmp``,
            # ``printf %s/cache "$HOME"``) emits them joined -- ``/ tmp``,
            # ``$HOME/cache`` -- but the shared resolver returns only the FIRST
            # literal (``/``), reading a space-joined or concatenated output as a
            # bare root/home wipe (GPT 6.1 over-refusal). The first operand is not
            # the complete output, so leave the body unresolved; a single-operand
            # producer whose whole output IS root/home still resolves and denies.
            return "\x00"
        return _static_substitution_output(body)

    def _emit(body: str, output: str) -> str:
        """The resolved *output* as an operand -- neutralized to a literal filename
        when the body's operand was QUOTED (its ``~`` / glob, and for a single-quote
        its ``$HOME`` too, are not expanded), left as-is otherwise so ``$(echo ~)``
        still reads as a home wipe."""
        kind = _rm_substitution_output_quote_kind(body)
        if kind is not None:
            return _rm_neutralize_substitution_output(output, kind)
        return output

    resolved: list[str] = []
    changed = False
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        peeled = _strip_outer_quotes(tok)
        m = _RM_WHOLE_SUBSTITUTION_RE.match(peeled)
        if m is not None:
            body = m.group("body")
            if body is None:
                body = m.group("btck") or ""
            output = _resolve_body(body)
            if output and output != "\x00":
                resolved.append(_emit(body, output))
                changed = True
                i += 1
                continue
        # An UNQUOTED ``$(…)`` whose body has internal spaces splits across
        # several shlex tokens (``$(echo`` … ``/)``); reassemble from the ``$(``
        # opener to the token closing the balanced ``)`` and resolve the joined
        # body, so ``rm -rf $(echo /)`` resolves its operand too.
        if peeled.startswith("$(") and ")" not in peeled[2:]:
            depth = peeled.count("(") - peeled.count(")")
            j = i + 1
            while j < n and depth > 0:
                depth += tokens[j].count("(") - tokens[j].count(")")
                j += 1
            if depth == 0 and j <= n:
                joined = " ".join(tokens[i:j])
                inner = _strip_outer_quotes(joined)
                mm = _RM_WHOLE_SUBSTITUTION_RE.match(inner)
                if mm is not None:
                    body = mm.group("body") or ""
                    output = _resolve_body(body)
                    if output and output != "\x00":
                        resolved.append(_emit(body, output))
                        changed = True
                        i = j
                        continue
            # The opener did not balance. Advance PAST the scanned span (appending
            # those tokens verbatim) not by one — a chain of unclosed ``$(`` (``"$( "
            # * 1000``) would otherwise re-scan to the end per opener, O(openers²) and
            # minutes on the gate. Scanned tokens keep their raw spelling.
            resolved.extend(tokens[i:j])
            i = j
            continue
        resolved.append(tok)
        i += 1
    return resolved if changed else None


#: Multi-call binaries that DISPATCH to the applet named by their first non-flag
#: argument: ``busybox rm -rf /`` runs the ``rm`` applet (``toybox`` too). Here
#: ``rm`` is the dispatcher's first ARGUMENT, not the program word and not behind
#: an exec wrapper, so the plain scan + wrapper set both miss it. Matched
#: positionally — ``busybox echo rm -rf /`` runs ``echo``, not ``rm``.
_RM_APPLET_DISPATCHERS: frozenset[str] = frozenset({"busybox", "toybox"})

#: Filesystem-MOVER programs: every non-flag argument is a PATH, never a program
#: to run. A ``rm`` among a mover's operands (``env rm -fr rm rm …``, ``cp rm rm
#: dst``) is a file named ``rm``, not a command, so it is skipped before the
#: per-``rm`` span cap and suffix scan. A data-PRINTER
#: (``echo``/``printf``) is already covered by ``_data_consumer_exempt``.
_RM_MOVER_PROGRAMS: frozenset[str] = frozenset(
    {"rm", "cp", "mv", "ln", "mkdir", "rmdir", "touch", "chmod", "chown"}
)

#: One-word exec wrappers whose FIRST argument is the command they run, so a later
#: ``rm`` in the span is an operand of THAT command (``_argv_programs`` attributes
#: it to the wrapper; resolving one layer recovers the real mover).
#: ``sudo``/``ssh``/``docker``/``setsid`` are absent — their ``rm`` executes.
_RM_SPAN_EXEC_WRAPPERS: frozenset[str] = frozenset({"env", "nice", "stdbuf", "time"})

#: Exec wrappers that take ONE mandatory leading NON-OPTION operand before the
#: command: ``timeout DURATION COMMAND``. ``timeout``'s duration is a bare word, so
#: resolving past it recovers the real command and ``timeout 5 printf '%s\n' rm -fr
#: /`` is read as a ``printf`` that only PRINTS the ``rm`` words (GPT 6.1
#: over-refusal). ``timeout`` is deliberately NOT in ``_RM_SPAN_EXEC_WRAPPERS``
#: alone, because its leading duration would be mistaken for the command.
_RM_DURATION_WRAPPERS: frozenset[str] = frozenset({"timeout"})

#: Dispatch wrappers that run their first non-option argument as a NEW program
#: (``sudo``/``setsid``/``nohup``/``stdbuf``). Unlike ``_RM_SPAN_EXEC_WRAPPERS``
#: these are NOT in the mover-resolution set -- a ``sudo rm -fr /`` really runs
#: ``rm`` and must deny -- but when the dispatched program is a DATA CONSUMER
#: (``sudo printf '%s\n' rm -fr /`` only prints) a later ``rm`` is a mention, not a
#: wipe (GPT 6.1 over-refusal). Resolved for the data-consumer-under-wrapper skip
#: ONLY; ``sudo``/``setsid`` stay executable everywhere else.
_RM_DISPATCH_WRAPPERS: frozenset[str] = frozenset(
    {"sudo", "setsid", "nohup", "stdbuf", "nice", "env", "time", "ionice", "doas"}
)


def _rm_dispatched_program(programs: "list[str]", tokens: "list[str]", index: int) -> str:
    """The program a dispatch wrapper (``sudo``/``setsid``/…) actually runs, so the
    data-consumer check can see a ``sudo printf …`` as a printer rather than as the
    wrapper. Resolves ONE wrapper layer: find the span start, skip the wrapper's own
    options / assignments (and ``sudo``'s ``-u user`` style value options), and
    return the basename of the first remaining word. Returns the base program when
    the span is not a dispatch wrapper or the token IS that first word (so a
    dispatched ``rm`` still classifies).
    """
    base = programs[index] if 0 <= index < len(programs) else ""
    if base not in _RM_DISPATCH_WRAPPERS:
        return base
    start = index
    while start > 0 and programs[start - 1] == base:
        start -= 1
    arg = start + 1
    while arg < len(tokens):
        tok = tokens[arg]
        if tok == "--":
            arg += 1
            break
        if _shell_normalizer.ENV_ASSIGNMENT_RE.match(tok):
            arg += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            arg += 1
            # Value-taking options whose value is a SEPARATE word.
            if (
                (base == "sudo" and tok in ("-u", "-g", "-U", "-C", "-h", "-p", "-r", "-t"))
                or (base == "nice" and tok in ("-n",))
                or (base == "ionice" and tok in ("-c", "-n", "-p"))
            ):
                arg += 1
            continue
        break
    if arg >= len(tokens) or arg == index:
        return base
    return _rm_program_basename(tokens[arg])


#: ``find`` predicates that INTRODUCE an executed command: the word AFTER one of
#: these (up to the ``;`` / ``+`` terminator) is a real program ``find`` runs. Every
#: OTHER ``find`` operand -- a start path, a ``-name`` / ``-path`` / ``-regex``
#: pattern value -- is DATA, so an ``rm`` sitting in one is a mention, not a wipe
#: (GPT 6.1 over-refusal: ``find / -name rm -o -name -fr -o -path /`` is a read-only
#: search). ``-exec`` / ``-execdir`` run their command; the ``-ok`` / ``-okdir``
#: forms prompt first but still run it, so they introduce a command too.
_RM_FIND_EXEC_PREDICATES: frozenset[str] = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _rm_find_command_word_mask(tokens: "list[str]", programs: "list[str]") -> "list[bool]":
    """For each token, True iff it is a word ``find`` actually EXECUTES.

    Within a command whose program is ``find``, only the first word after an
    ``-exec`` / ``-execdir`` / ``-ok`` / ``-okdir`` predicate -- and the words of
    that nested command up to its ``;`` / ``+`` terminator -- are run; every other
    ``find`` operand (start paths, ``-name`` / ``-path`` pattern VALUES) is data.
    A token outside any ``find`` command is left True so the ordinary classifier
    decides it. ``find`` reached through a wrapper keeps its own program name here,
    so the mask applies wherever ``_rm_argv_programs`` attributes a token to
    ``find``.
    """
    mask = [True] * len(tokens)
    i = 0
    n = len(tokens)
    while i < n:
        if (i < len(programs) and programs[i] == "find") and _program_basename(tokens[i]) != "find":
            # tokens[i] is a find OPERAND (programs[i]=="find" and it is not the
            # ``find`` program word itself). Default to data; flip only the words of
            # an ``-exec``-family nested command.
            tok = tokens[i]
            if tok in _RM_FIND_EXEC_PREDICATES:
                # The nested command runs from the next token up to the ``;`` / ``+``
                # terminator (a lone ``;`` / ``\;`` / ``+`` token).
                mask[i] = False  # the predicate word itself is not a command
                j = i + 1
                while j < n and programs[j] == "find":
                    jt = tokens[j]
                    if jt in (";", "\\;", "+"):
                        mask[j] = False
                        j += 1
                        break
                    mask[j] = True  # a word of the executed nested command
                    j += 1
                i = j
                continue
            mask[i] = False  # an ordinary find operand: data
        i += 1
    return mask


#: ``git`` subcommands whose arguments the floor must NOT read as a raw
#: recursive-force ``rm`` of root or home. ``grep`` / ``log`` / ``shortlog`` only
#: READ their patterns; ``rm`` is git's own index removal, which operates on repo
#: pathspecs and cannot wipe the filesystem root or the home directory the way a
#: bare ``rm -rf /`` does, so ``git rm -rf --cached ~`` is not that threat and base
#: allowed it (Opus 5.5 over-refusal). Deliberately narrow: an EXECUTING subcommand
#: (``bisect run``, ``submodule foreach``, ``filter-branch``, ``rebase --exec``) is
#: absent, so its operands keep their ordinary classification.
_RM_GIT_SEARCH_SUBCOMMANDS = frozenset({"grep", "log", "shortlog", "rm"})


def _rm_git_search_arg_mask(tokens: "list[str]", programs: "list[str]") -> "list[bool]":
    """For each token, True unless it is an ARGUMENT of a read-only ``git`` search
    subcommand (``git grep`` / ``git log`` / ``git shortlog``), which only reads
    its patterns and paths and never executes them.

    Within a command whose program is ``git`` whose first non-option word is a
    read-only search subcommand, every later operand is marked data (False) so an
    ``rm`` sitting in a ``-e`` pattern list (``git grep -e rm -e -fr ~``) is a
    mention, not an executed ``rm`` (GPT 6.1 over-refusal). An EXECUTING ``git``
    subcommand (``git bisect``, ``git submodule``, anything not in the read-only
    set) leaves its operands True, so a real ``git … rm -rf /`` still classifies.
    A ``git`` reached through a wrapper keeps its own program name here, so the
    mask applies wherever ``_rm_argv_programs`` attributes a token to ``git``.
    """
    mask = [True] * len(tokens)
    n = len(tokens)
    i = 0
    while i < n:
        if (i < len(programs) and programs[i] == "git") and _program_basename(tokens[i]) == "git":
            # tokens[i] is the ``git`` program word. Find the first non-option word
            # of this command: the subcommand. ``git`` global options precede it.
            # The floor reads LOWERCASED text, so the cwd flag ``-C`` and the config
            # flag ``-c`` are indistinguishable here; ``-c alias.x='!sh -c …' x`` can
            # run a command, so either spelling is treated as EXECUTING-capable and
            # leaves the whole command classified (mask untouched) -- fail closed.
            j = i + 1
            saw_dash_c = False
            subcommand = ""
            while j < n and programs[j] == "git":
                jt = tokens[j]
                base = _program_basename(jt)
                if base == "git":
                    break  # a new git command started (should not happen mid-span)
                if jt in ("-c", "--config-env"):
                    saw_dash_c = True
                    j += 2  # option + its value word
                    continue
                if jt.startswith("-"):
                    j += 1  # a flag with no separate value word
                    continue
                subcommand = base
                break
            # Advance past the whole git command regardless, so the outer loop does
            # not re-scan these tokens as a fresh git command.
            end = j
            while end < n and programs[end] == "git":
                end += 1
            if subcommand in _RM_GIT_SEARCH_SUBCOMMANDS and not saw_dash_c and j < n:
                # Mark every ARGUMENT after the subcommand word as data; keep the
                # ``git`` program word True.
                start_mask = j + 1
                if _program_basename(tokens[j]) == "rm":
                    # ``git rm`` is git's removal. Neutralize it only when it is
                    # INDEX-ONLY (``--cached``) or a dry run (``--dry-run`` / ``-n``):
                    # that cannot touch the working tree, so ``git rm -rf --cached
                    # ~`` is not a home wipe (Opus 5.5 over-refusal). A plain ``git
                    # rm -rf ~`` CAN delete tracked working-tree files, so it is left
                    # classified -- fail closed. When neutralized, mask the ``rm``
                    # subcommand word too so the floor does not read it as a raw rm.
                    git_rm_flags = set(tokens[j + 1 : end])
                    if git_rm_flags & {"--cached", "--dry-run", "-n"}:
                        start_mask = j
                    else:
                        start_mask = end  # leave a working-tree git rm classified
                for k in range(start_mask, end):
                    mask[k] = False
            i = end
            continue
        i += 1
    return mask


def _rm_effective_span_program(programs: "list[str]", tokens: "list[str]", index: int) -> str:
    """Program of the command owning ``tokens[index]``, resolving ONE leading
    one-word exec wrapper. When ``env`` leads the span (``env rm -fr rm rm``) every
    token is attributed to ``env``; the real command is its first post-option
    argument. A non-first token returns the wrapped basename (``rm``); the first
    wrapped argument (the executed ``rm``) stays the wrapper so it is classified.
    """
    base = programs[index] if 0 <= index < len(programs) else ""
    if base not in _RM_SPAN_EXEC_WRAPPERS and base not in _RM_DURATION_WRAPPERS:
        return base
    # Find this span's start, then its first post-option argument — the command.
    start = index
    while start > 0 and programs[start - 1] == base:
        start -= 1
    arg = start + 1
    # Skip the wrapper's OWN options AND assignments, not just ``VAR=val``: ``env
    # -i`` / ``env -u NAME`` / ``nice -n 5`` / ``env --`` precede the wrapped
    # command, so stopping at the first option mis-reads it as the mover and lets a
    # 2000-operand flood reach the per-operand suffix scan.
    while arg < len(tokens):
        tok = tokens[arg]
        if tok == "--":
            arg += 1  # end-of-options; next word is the command
            break
        if _shell_normalizer.ENV_ASSIGNMENT_RE.match(tok):
            arg += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            arg += 1
            # ``env -u NAME`` / ``nice -n 5`` take a separate value word (unless
            # glued, ``-uNAME``). ``timeout -s SIGNAL`` / ``-k DURATION`` likewise.
            if base in ("env", "nice") and tok in ("-u", "-n", "--unset"):
                arg += 1
            elif base == "timeout" and tok in ("-s", "-k", "--signal", "--kill-after"):
                arg += 1
            continue
        break
    # ``timeout`` takes a mandatory leading DURATION operand (a bare non-option
    # word) BEFORE the command, so the command is the token after it: ``timeout 5
    # printf`` runs ``printf``. Skip exactly one such operand for a duration wrapper.
    if base in _RM_DURATION_WRAPPERS and arg < len(tokens):
        arg += 1
    if arg >= len(tokens) or arg == index:
        # No wrapped command, or this token IS the wrapped command word: not an
        # operand — leave it to the executable path.
        return base
    return _program_basename(tokens[arg])


def _rm_deescape_unquoted_backslashes(text: str) -> str:
    """Remove backslash escapes as an UNQUOTED inner shell would, so an escaped
    program name reforms.

    A ``bash -c $"\\r\\m -rf /"`` payload reaches the inner shell as the script
    ``\\r\\m -rf /``; unquoted, bash drops each backslash before an ordinary
    character, so ``\\r\\m`` becomes the word ``rm``. The outer walk's
    ``_decode_printf_escapes`` instead maps ``\\r`` to whitespace and drops the
    ``r``, so the ``rm`` never reforms and the wipe was missed.

    Backslashes INSIDE single quotes are literal and are left untouched; a
    backslash outside single quotes removes itself and keeps the next character
    (``\\n`` -> ``n``, matching the inner shell's own unquoted lexing rather than
    the C-escape meaning — the shell does not turn an unquoted ``\\n`` into a
    newline). A trailing backslash is dropped.
    """
    out: list[str] = []
    for step in _iter_shell_chars(text):
        if step.trailing_escape:
            continue  # a trailing backslash is dropped
        # An escape pair outside single quotes yields its escaped character;
        # everything else (quotes included) is kept verbatim.
        out.append(step.char if len(step.text) == 2 else step.text)
    return "".join(out)


#: Shell programs whose ``-c`` argument is a command STRING they execute. When a
#: nested payload's escaped quoting defeats the walk's own descent, the walk can
#: still hand this frame a FLATTENED argv (``['sh', '-c', 'rm', '-rf', '/']``);
#: the tokens after ``-c`` are then the executed command, read here as their own
#: argv so the ``rm`` leads its own command instead of sitting behind ``sh``.
#: Programs whose ``-c <string>`` argument is a SHELL command string the program
#: runs via a shell. The real shells run it directly; ``flock FILE -c 'cmd'`` runs
#: ``cmd`` through ``/bin/sh -c``, so a ``flock /tmp/lock -c 'rm -fr ~'`` wipe is an
#: executed shell payload too (GPT 6.1). The other common execution wrappers
#: (``timeout``/``nice``/``nohup``/``env``/``sudo``) take a COMMAND, not a ``-c``
#: shell string, so an ``rm`` behind them is already caught by the executor-wrapper
#: denylist in ``_rm_targets_in_argv`` -- they are deliberately NOT listed here.
_RM_SHELL_C_PROGRAMS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox", "flock"}
)

#: Programs whose FIRST non-flag operand is a bare SHELL command STRING they run via
#: ``sh -c`` -- NO ``-c`` flag precedes it (``watch 'rm -rf "$HOME"'`` runs the
#: quoted command through a shell). Their operand is surfaced as an executed shell
#: payload too (GPT 6.1). Kept to a small fixed list; a wrapper that takes a COMMAND
#: (``timeout``/``nice``/``nohup``/``env``/``sudo``) is handled by the executor
#: denylist in ``_rm_targets_in_argv`` instead and is NOT listed here.
_RM_COMMAND_STRING_WRAPPERS: frozenset[str] = frozenset({"watch"})


def _rm_shell_c_data_payloads(tokens: "list[str]") -> "frozenset[str]":
    """The ``-c`` payload strings that are DATA, not executed, in *tokens*.

    ``echo bash -c 'rm -rf "/"'`` / ``cat sh -c '…'`` PRINT the shell command —
    ``bash``/``sh`` is an ARGUMENT of the data consumer (``echo``/``cat``), so the
    ``-c`` string is never run (base allowed this as a mention; the
    payload walk otherwise descends it into a frame and refuses it as a wipe).
    Returns the raw ``-c`` argument of every such mention so the caller can skip
    the descended frame it would produce. An EXECUTED shell (``bash -c '…'`` at
    program position, ``sudo bash -c '…'``) is NOT a data consumer's operand and
    is not listed, so a real wipe still denies.
    """
    programs = _rm_argv_programs(tokens)
    disqualified = _rm_command_disqualified(tokens, programs)
    data: set[str] = set()
    n = len(tokens)
    # Precompute, in ONE left-to-right pass, the next command boundary at or after
    # each position. The per-shell-token ``-c`` search then scans only within its
    # own command segment instead of rescanning every later token to the end --
    # ``echo sh sh … rm`` (thousands of ``sh`` words in ONE segment) made the old
    # inner loop O(n x n) and stalled the gate ~27s past the watchdog.
    next_boundary = [n] * (n + 1)
    nb = n
    for p in range(n - 1, -1, -1):
        if _rm_token_ends_argv(tokens[p]):
            nb = p
        next_boundary[p] = nb
    scanned_through = 0
    for i, tok in enumerate(tokens):
        if _program_basename(tok) not in _RM_SHELL_C_PROGRAMS:
            continue
        # The shell is DATA only when it is itself an argument of a data consumer
        # (``echo``/``cat`` …), not when it is the command being run.
        if not _data_consumer_exempt(i, tok, programs, tokens, command_disqualified=disqualified):
            continue
        # Scan THIS command's segment for the ``-c`` whose next token is the printed
        # payload -- but only ONCE per segment. A segment already scanned (because an
        # earlier exempt shell word in it was visited) is skipped, so a long run of
        # shell words in one segment (``echo sh sh … rm``) costs O(segment) total,
        # not O(segment) per word (the old per-word rescan stalled the
        # gate ~27s past the watchdog).
        seg_end = next_boundary[i]
        if i < scanned_through:
            continue
        scanned_through = seg_end
        for j in range(i + 1, seg_end):
            if tokens[j] == "-c" and j + 1 < n:
                data.add(_strip_outer_quotes(tokens[j + 1]))
                break
    return frozenset(data)


def _rm_shell_c_executed_payloads(tokens: "list[str]") -> "frozenset[str]":
    """The ``-c`` payload strings of EXECUTED shells in *tokens* (the inverse of
    :func:`_rm_shell_c_data_payloads`).

    ``bash -c 'rm -fr "$HOME"'`` at program position, ``sudo bash -c '…'`` — the
    shell RUNS the payload, so ``$HOME``/``~`` inside it is expanded by that child
    shell regardless of the OUTER quotes that merely delimit the payload. The home
    liveness check must read the payload's OWN quote state, not the enclosing
    command's: ``"$HOME"`` inside the payload is live
    even though the whole payload sits in the outer ``'…'``. A shell that is a data
    consumer's operand (``echo bash -c '…'``) is NOT executed and is excluded.

    Recurses through NESTED payloads to a bounded depth: ``bash -c 'bash -c "rm
    -fr ~"'`` runs the inner ``rm -fr ~`` two levels down, so the inner payload
    must be surfaced for the liveness check too. Recursion is capped
    at ``_RM_DESCENT_BUDGET`` payloads so a pathological nest cannot spin.
    """
    out: set[str] = set()
    worklist = [tokens]
    seen: set[str] = set()
    budget = _RM_DESCENT_BUDGET
    while worklist and budget > 0:
        cur = worklist.pop()
        budget -= 1
        programs = _rm_argv_programs(cur)
        disqualified = _rm_command_disqualified(cur, programs)
        n = len(cur)
        # Precompute the next ``-c`` reachable from each position without crossing
        # an argv-end, so the per-token search is O(1). A repeated non-``-c`` shell
        # operand (``'sh rm ' + 'sh ' * 6600``) otherwise rescans the same suffix
        # per token -- O(tokens²), ~19s on the synchronous gate, past the 25s
        # watchdog (same shape as
        # ``_rm_targets_in_shell_c``).
        next_c = [-1] * (n + 1)
        for p in range(n - 1, -1, -1):
            if _rm_token_ends_argv(cur[p]):
                next_c[p] = -1
            elif cur[p] == "-c":
                next_c[p] = p
            else:
                next_c[p] = next_c[p + 1]
        for i, tok in enumerate(cur):
            # A command-string wrapper (``watch 'rm -rf "$HOME"'``) runs its FIRST
            # non-flag operand through a shell -- surface that operand as a payload.
            # Only at program position (``i == 0`` or after a command boundary), so
            # a ``watch`` MENTIONED as data is not treated as executing.
            if _program_basename(tok) in _RM_COMMAND_STRING_WRAPPERS and (
                i == 0 or (i > 0 and _rm_token_ends_argv(cur[i - 1]))
            ):
                k = i + 1
                while k < n and cur[k].startswith("-"):
                    k += 1
                if k < n and not _rm_token_ends_argv(cur[k]):
                    payload = _strip_outer_quotes(cur[k])
                    out.add(payload)
                    if payload not in seen:
                        seen.add(payload)
                        worklist.append(_split_shell_words(payload))
            if _program_basename(tok) not in _RM_SHELL_C_PROGRAMS:
                continue
            # Skip the DATA case (printed mention); keep only an executed shell.
            if _data_consumer_exempt(i, tok, programs, cur, command_disqualified=disqualified):
                continue
            j = next_c[i + 1] if i + 1 <= n else -1
            if j != -1 and j + 1 < n:
                payload = _strip_outer_quotes(cur[j + 1])
                out.add(payload)
                if payload not in seen:
                    seen.add(payload)
                    worklist.append(_split_shell_words(payload))
    return frozenset(out)


def _rm_shell_c_positional_arg_mask(tokens: "list[str]", programs: "list[str]") -> "list[bool]":
    """Which tokens are POSITIONAL ARGUMENTS of a shell ``-c`` invocation -- data
    bound to ``$0``/``$1``/…, not executed commands -- in ONE left-to-right pass.

    ``sh -c '<string>' _ rm -fr /`` runs only ``<string>``; the words after it
    (``_`` as ``$0``, then ``rm``, ``-fr``, ``/`` as ``$1``..``$3``) are passed to
    that string as positional parameters and are NEVER executed by the outer shell.
    The command string itself is classified separately by
    :func:`_rm_targets_in_shell_c`, so a real ``rm`` INSIDE the payload still
    denies; this only stops the OUTER executable scan from reading a positional
    ``rm`` as a root/home wipe (GPT 6.1: ``sh -c 'printf "%s\\n" "$@"' _ rm -fr /``
    is an argument-printer that deletes nothing, yet was refused).

    A token is a positional arg when a shell program's ``-c`` flag precedes it in
    the current command (no argv boundary crossed) AND the command-string operand
    has already gone by -- so it is at or past the first positional parameter. A
    per-token LEFT-scan would be O(tokens**2): ``bash -c ':'`` followed by thousands
    of ``rm`` arguments took 27.3 s on the synchronous gate, past the 25 s watchdog
    (GPT 6.1). State carried forward -- a shell ``-c`` seen since the last command
    boundary and whether its command string has passed -- makes every verdict a
    constant-time ``mask[i]`` read. A command boundary (``;`` / ``|`` / newline)
    resets the state.
    """
    n = len(tokens)
    mask = [False] * n
    saw_c = False  # a shell ``-c`` flag seen since the last command boundary
    saw_command_string = False  # at least one operand after that ``-c``
    for i in range(n):
        tok = tokens[i]
        if _rm_token_ends_argv(tok):
            saw_c = False
            saw_command_string = False
            # A boundary token is never itself a positional arg; the next token
            # starts a fresh command. Fall through with mask[i] False.
            continue
        if saw_c:
            # Already past a shell ``-c``: this token is the command string (first
            # operand) or a positional arg after it. Mark positional args only.
            if saw_command_string:
                mask[i] = True
            else:
                saw_command_string = True
        if tok == "-c" and _program_basename(programs[i]) in _RM_SHELL_C_PROGRAMS:
            saw_c = True
            saw_command_string = False
    return mask


def _rm_targets_in_shell_c(
    tokens: "list[str]", programs: "list[str]", *, strip_quotes: bool, _budget: "list[int]"
) -> "frozenset[str]":
    """Catastrophic ``rm`` targets in a ``sh -c <cmd>`` argv flattened into a frame.

    The payload walk normally descends ``bash -c '<script>'`` into a frame of its
    own, but a two-level nest with ESCAPED inner quotes
    (``bash -c 'sh -c "rm -rf \\"/\\""'``) can defeat the inner extraction and
    leave the ``sh -c`` frame's argv flattened to ``['sh', '-c', 'rm', '-rf',
    '/']``. There ``rm`` is not at program position (``sh`` is) and ``sh`` is not
    an exec wrapper, so the plain scan misses it. When a nested-shell program is
    followed by a ``-c`` flag, the tokens after ``-c`` are the command string it
    runs, so they are classified as their own argv — the same treatment
    ``find -exec`` gets. Scoped to a frame whose PROGRAM is the shell
    (``_argv_programs``), so a ``-c`` that is data to another command is untouched.
    """
    found: set[str] = set()
    i = 0
    n = len(tokens)
    # Precompute, in ONE right-to-left pass, the index of the next ``-c`` reachable
    # from each position WITHOUT crossing an argv-end (``;`` / ``|`` / newline). A
    # command-boundary token resets the lookahead to "none". This turns the inner
    # ``-c`` search into an O(1) read, so a repeated non-``-c`` shell operand
    # (``'sh rm ' + 'sh ' * 6600``) does not rescan the same suffix per token —
    # which would be O(tokens²), ~19s on the synchronous gate and past the 25s
    # watchdog.
    next_c = [-1] * (n + 1)
    for p in range(n - 1, -1, -1):
        if _rm_token_ends_argv(tokens[p]):
            next_c[p] = -1
        elif tokens[p] == "-c":
            next_c[p] = p
        else:
            next_c[p] = next_c[p + 1]
    while i < n:
        advance = 1
        if (
            _program_basename(tokens[i]) in _RM_SHELL_C_PROGRAMS
            and _program_basename(programs[i]) in _RM_SHELL_C_PROGRAMS
        ):
            # The shell's own ``-c`` (if any) before its argv ends, read in O(1)
            # from the precomputed table; the rest of the argv is the command
            # string it executes.
            j = next_c[i + 1] if i + 1 <= n else -1
            if j != -1 and j + 1 < n:
                span = []
                k = j + 1
                while k < n and not _rm_token_ends_argv(tokens[k]):
                    span.append(tokens[k])
                    k += 1
                if span:
                    # ONLY ``span[0]`` is the command STRING; tokens AFTER it
                    # are positional args bound to ``$0``/``$1``/… (``sh -c
                    # '…rm -rf .cache' sh "$HOME"`` — ``$HOME`` is ``$1``, not an
                    # ``rm`` operand), so joining them wrongly refused a legit
                    # cache clean. ONE descent per span.
                    if _budget[0] > 0:
                        _budget[0] -= 1
                        payload = _rm_deescape_unquoted_backslashes(span[0])
                        found |= _rm_targets_in_argv(
                            _split_shell_words(payload),
                            strip_quotes=strip_quotes,
                            _budget=_budget,
                        )
                # Advance PAST the consumed ``-c`` span, not by one — a chain of
                # ``sh -c`` tokens (``"sh -c " * 1000``) would otherwise re-scan
                # the span to the end for every ``sh``, O(spans²) and minutes on
                # the synchronous gate.
                advance = max(advance, k - i)
        i += advance
    return frozenset(found)


def _rm_overflow_span_targets(tokens: "list[str]", start: int) -> "frozenset[str]":
    """A CHEAP root/home-shape test for one recursive-force ``rm`` span, used only
    past the per-span classification cap.

    The full structural classifier is capped so a pathological flood of ``rm``
    spans cannot cost O(spans x operand). Past the cap we must still not fail OPEN
    on a real wipe, yet a long bulk cleanup of DESCENDANTS (``rm -fr build0 ; … ;
    rm -fr build69``) is legit and base allowed it -- so a blanket root+home deny
    newly refused it. Instead scan THIS span's operand tokens once (raw, no brace or
    dot-segment expansion) and report only the class whose exact root/home target
    appears: a ``/``- or ``~``/``$HOME``-rooted operand that the itself-matchers
    accept. Each token is visited once across spans, so the whole walk stays linear.
    """
    out: set[str] = set()
    j = start + 1
    n = len(tokens)
    while j < n and not _rm_token_ends_argv(tokens[j]):
        tok = tokens[j]
        j += 1
        if tok.startswith("-"):
            continue
        # Neutralize a QUOTED/escaped brace/glob metachar to a sentinel so a quoted
        # literal (``'/*'`` / ``'/{,tmp}'``) is a filename, not a root glob/expansion
        # -- the same exemption the non-overflow raw pass applies (GPT 6.1
        # over-refusal: an overflow span over-refused ``rm -fr <many files> '/*'``).
        # An UNQUOTED ``/*`` keeps its ``*`` through the neutralizer and still
        # matches. The neutralized form IS the raw candidate (it only replaces quoted
        # metachars), so the raw ``tok`` / ``stripped`` candidates -- which would
        # re-activate the quoted glob -- are deliberately not scanned here.
        neutralized = _rm_neutralize_literal_metachars(tok)
        for cand in (
            neutralized,
            _rm_normalize_dot_segments(neutralized),
        ):
            if _RM_ROOT_ITSELF_RE.fullmatch(cand):
                out.add("root")
            if _RM_HOME_ITSELF_RE.fullmatch(cand):
                out.add("home")
        stripped = _rm_strip_all_quotes(tok)
        # Preserve the EXPANDED-home equality the full classifier applies: an operand
        # that is a literal absolute path equal to the real home (``rm -fr
        # /home/alice`` where ``$HOME`` is ``/home/alice``) carries no ``~``/``$HOME``
        # marker, so the itself-matchers above miss it, yet it is a real home wipe.
        # Fold separators + lowercase + home-itself tail strip, as
        # ``_rm_absolute_home_operand`` does.
        if "home" not in out:
            home_real = _rm_expanded_home_path()
            if home_real and (
                _rm_strip_home_itself_tail(_rm_fold_home_case(stripped.replace("\\", "/")))
                == home_real
                or _rm_strip_home_itself_tail(
                    _rm_fold_home_case(_rm_normalize_dot_segments(stripped).replace("\\", "/"))
                )
                == home_real
            ):
                out.add("home")
        # A BRACE operand can expand to the root/home dir ITSELF via an empty or
        # ``/``/``~`` member (``"$HOME"/{,.cache}`` -> ``$HOME`` + ``$HOME/.cache``;
        # ``/{,bin}`` -> ``/``), which the itself-matchers above miss on the
        # unexpanded token. Use the SAME bounded brace test the
        # non-overflow path uses -- a numeric/char RANGE and a non-root frame stay
        # ALLOWED, so a descendant bulk cleanup (``rm -fr build{0..69}``) is not
        # newly refused. The class comes from the residual frame's spelling.
        if ("{" in tok and "}" in tok) and _rm_brace_word_could_be_catastrophic(tok):
            frame = _rm_strip_all_quotes(_RM_BRACE_GROUP_RE.sub("", tok)).strip()
            frame_norm = _rm_normalize_dot_segments(frame)
            if (
                frame == ""
                or _RM_ROOT_ITSELF_RE.fullmatch(frame)
                or _RM_ROOT_ITSELF_RE.fullmatch(frame_norm)
            ):
                out.add("root")
            if _RM_HOME_ITSELF_RE.fullmatch(frame) or _RM_HOME_ITSELF_RE.fullmatch(frame_norm):
                out.add("home")
            # A home-rooted brace frame (``$HOME/{,.cache}``) whose empty member is
            # home itself: the frame is ``$HOME/`` -> home.
            if frame[:5].lower() in ("$home", "${hom") or frame.startswith("~"):
                out.add("home")
        if {"root", "home"} <= out:
            break
    return frozenset(out)


def _rm_frame_overflow_targets(
    tokens: "list[str]", *, raw_source: "str | None" = None
) -> "frozenset[str]":
    """A CHEAP, BOUNDED, quote-normalized exact root/home scan of ONE frame, used
    when the frame-walk's shared token budget is exhausted.

    Past the budget the frame walk must NOT silently skip a frame -- a padded line
    of many substitutions each with many operands exhausts the budget before the
    wipe's frame, and the whole-text deny-net regex does not catch a split spelling
    like ``r''m -fr ~``. This runs ONE linear
    pass per frame with NO brace/dot/candidate/recursion machinery: it splits glued
    command boundaries, finds each EXECUTED recursive-force ``rm`` span (its program
    word read on the de-quoted spelling, so ``r''m`` is recognised), and applies the
    cheap :func:`_rm_overflow_span_targets` exact-target test. O(frame tokens), so
    it cannot be exhausted -- the EXPENSIVE full classifier stays budget-gated.

    Quote boundary: the command-boundary split and the span scan run over the
    QUOTE-PRESERVING argv so an operand keeps its literal punctuation -- only the
    program and flag WORDS are de-quoted, and only to recognise them (``r''m`` ->
    ``rm``). A decoded view strips an operand's surrounding quotes, so a literal
    ``'a;b'`` filename would collapse to ``a;b`` and the glued-boundary split would
    read the ``;`` as a command boundary, dropping the trailing ``~`` and failing a
    home wipe open. When *raw_source* is given it is re-split (quotes intact) and
    used instead of the de-quoted *tokens*; the program-word match de-quotes each
    word individually, so a split spelling is still caught."""
    toks = _rm_split_glued_boundaries(
        _split_shell_words(raw_source) if raw_source is not None else tokens
    )
    programs = _rm_argv_programs(toks)
    out: set[str] = set()
    expect_program = True
    program_word_at = -1
    for i, token in enumerate(toks):
        is_program_word = expect_program
        if is_program_word:
            expect_program = False
            program_word_at = i
        if _rm_token_ends_argv(token):
            expect_program = True
        # Match ``rm`` on the DE-QUOTED spelling so ``r''m`` / ``r""m`` is caught.
        if _program_basename(_rm_strip_all_quotes(token)) != "rm":
            continue
        # Executed iff ``rm`` leads its own command, is a multi-call dispatcher's
        # applet (``busybox rm``), or its parent is not a data consumer -- mirrors
        # the full classifier's executability test, cheaply.
        executed = (
            is_program_word
            or (
                program_word_at >= 0
                and program_word_at == i - 1
                and _program_basename(programs[program_word_at]) in _RM_APPLET_DISPATCHERS
            )
            or (
                program_word_at >= 0
                and program_word_at != i
                and _program_basename(programs[program_word_at]) not in _DATA_CONSUMER_PROGRAMS
            )
        )
        if not executed:
            continue
        if not _rm_span_is_recursive_force(toks, i):
            continue
        out |= _rm_overflow_span_targets(toks, i)
        if {"root", "home"} <= out:
            break
    return frozenset(out)


def _rm_split_glued_boundaries(tokens: "list[str]") -> "list[str]":
    """Split tokens on glued, UNESCAPED, UNQUOTED command boundaries.

    ``_split_shell_words`` keeps a glued separator fused with its neighbours, so
    ``rm -fr ./build;rm -fr ~`` arrives as ``[..., './build;rm', ...]`` and the
    main loop's tail-only ``_ends_argv`` never sees the ``;`` — the second
    command hides and its wipe fails open. Rewrite each such
    token into its operand fragments plus a standalone boundary token
    (``['./build', ';', 'rm']``) so the main loop resets ``expect_program`` at the
    boundary and attributes the following command correctly.

    A boundary that is backslash-escaped (``a\\;b``) or inside shell quotes
    (``';'`` / ``";"``) is NOT a separator — a quoted ``;`` is a literal filename
    (``rm -rf ';' /`` deletes a file named ``;``, then root). Routes through the
    single :func:`_rm_unescaped_boundary` scan with ``treat_subshell_closer=False``
    so a ``)`` closing a ``$(…)`` is left to the depth-tracking classifier, while a
    redirection ``&`` (``2>&1``) is correctly not split — the same boundary rules
    the span scan uses.
    """
    out: "list[str]" = []
    for tok in tokens:
        if not tok or _rm_unescaped_boundary(tok, treat_subshell_closer=False) is None:
            out.append(tok)
            continue
        # Walk the token, carving out each unquoted, unescaped boundary as its own
        # token. Restrict to command-SEQUENCING operators (``; & | \n``), NOT a
        # subshell closer (``)``) — that is handled by the depth-tracking
        # classifier downstream.
        rest = tok
        guard = 0
        while rest:
            guard += 1
            if guard > len(tok) + 2:
                out.append(rest)
                break
            pos = _rm_unescaped_boundary(rest, treat_subshell_closer=False)
            if pos is None:
                out.append(rest)
                break
            before = rest[:pos]
            sep = rest[pos]
            if before:
                out.append(before)
            out.append(sep)
            rest = rest[pos + 1 :]
    return out


def _rm_targets_in_argv(
    tokens: "list[str]",
    *,
    strip_quotes: bool,
    _budget: "list[int] | None" = None,
) -> "frozenset[str]":
    """The catastrophic ``rm`` targets deleted within ONE frame's raw argv.

    Fires for each token whose basename is ``rm`` and that is EXECUTED — ``rm`` at
    program position, the first argument of a multi-call dispatcher (``busybox
    rm``), or ``rm`` whose parent does NOT treat its args as data. That last is a
    DENYLIST: an ``rm`` behind ANY parent executes UNLESS the parent is in
    ``_DATA_CONSUMER_PROGRAMS`` (``echo``/``cat``/``cp``/``mv``), so an unknown exec
    wrapper (``setsid``/``nohup``/…) is executable rather than slipping an
    allowlist; an ``rm`` that is a data consumer's argument (``echo rm -rf /``) is
    skipped.

    From each executed ``rm`` its OWN argv is read forward until the command ends.
    A resolved operand is denied when it IS the root (``/``, ``/*``) or home (``~``
    / ``$HOME`` / their ``/*``) dir ITSELF, in ANY flag spelling. A DESCENDANT
    (``rm -rf /etc``, ``rm -rf ~/.ssh``) is NOT denied by this structural pass;
    base's descendant coverage is reproduced by the frame-text pin (``rm -rf /.*``
    / ``rm -rf ~.*``), so only a descendant base's contiguous ``rm -rf `` text
    matched is denied while a widened spelling (``rm -fr /tmp/x``) stays allowed.

    ``strip_quotes`` peels surrounding SHELL quotes — True for the raw split
    (``"$HOME"`` -> ``$HOME``), False for the decoded view where a surrounding
    quote is a literal the decode produced (``$'"/"'`` -> ``"/"``).
    """
    if not tokens:
        return frozenset()
    # ------------------------------------------------------------------
    # F1 preprocessing: split glued command boundaries.
    # ``_split_shell_words`` can produce tokens like ``./build;rm`` where an
    # unquoted ``;`` (or ``&`` / ``|``) is fused with its neighbours.  The main
    # loop's ``_ends_argv`` only checks a token's TAIL, so the glued boundary is
    # invisible and the next command hides.  Expand every such token into
    # ``['./build', ';', 'rm']`` so the main loop sees the boundary as its own
    # token and resets ``expect_program``.
    # Re-join a ``${...}`` parameter expansion that word-splitting tore apart at
    # interior whitespace (``${HOME:?must be set}``) so the home operand is one word
    # the home matcher can read.
    tokens = _rm_rejoin_param_expansions(tokens)
    tokens = _rm_split_glued_boundaries(tokens)
    # Shared brace expander (argv_floor) rather than a private rm-local copy.
    from .argv_floor import _brace_expansions

    # One shared descent budget per top-level classification. The public entry
    # (and every non-recursive caller) passes None, so a fresh cell is created
    # here; the sub-helpers thread the SAME cell into their recursive
    # ``_rm_targets_in_argv`` calls, so nested spans draw down one common budget.
    if _budget is None:
        _budget = [_RM_DESCENT_BUDGET]
    programs = _rm_argv_programs(tokens)
    found: set[str] = set()
    found |= _rm_targets_in_shell_c(tokens, programs, strip_quotes=strip_quotes, _budget=_budget)
    # Computed once per argv (not per ``rm`` token): the pipe-into-shell / trailing
    # operator guards ``_data_consumer_exempt`` consults, whose sweep is quadratic
    # per token. ``None`` until the first ``rm`` needs it.
    disqualified: "bool | None" = None
    # Whether ANY shell-``-c`` program word appears in this argv (``sh``/``bash``/…
    # at program position). The shell-``-c`` positional-argument skip below only
    # applies when one does, so this ONE O(n) scan replaces a per-``rm`` left-scan:
    # a bare-``rm`` flood (``sudo rm -fr rm rm … rm``) has no shell ``-c``, so the
    # per-token helper -- which walks left to the command start, O(span) each and
    # O(n**2) over the flood, past the gate's 120s cap -- is never entered.
    argv_has_shell_c = any(
        tok == "-c" and _program_basename(programs[p]) in _RM_SHELL_C_PROGRAMS
        for p, tok in enumerate(tokens)
    )
    # Positional-arg membership for every token, computed in ONE pass only when a
    # shell ``-c`` is present (a bare-``rm`` flood skips it entirely). The main
    # loop then reads ``shell_c_positional_mask[i]`` in constant time instead of
    # left-scanning per ``rm`` token, which was O(tokens**2) -- 27.3 s on the gate
    # for ``bash -c ':'`` + thousands of ``rm`` args, past the watchdog (GPT 6.1).
    shell_c_positional_mask = (
        _rm_shell_c_positional_arg_mask(tokens, programs) if argv_has_shell_c else None
    )
    # Which tokens ``find`` actually EXECUTES, computed once in ONE pass only when a
    # ``find`` command is present. Outside a ``find``, every token is True so the
    # ordinary classifier decides it; inside one, only an ``-exec``-family nested
    # command's words are True, so a ``-name rm`` / ``-path /`` pattern VALUE is read
    # as data (GPT 6.1 over-refusal). A line with no ``find`` pays nothing.
    find_command_word_mask = (
        _rm_find_command_word_mask(tokens, programs)
        if any(_program_basename(p) == "find" for p in programs)
        else None
    )
    # Which tokens are search-pattern ARGUMENTS of a read-only ``git`` search
    # (``git grep …`` / ``git log --grep …``), computed once only when a ``git``
    # command is present. Those subcommands never execute an operand, so an ``rm``
    # among their patterns (``git grep -e rm -e -fr ~``) is DATA, not an executed
    # ``rm`` (GPT 6.1 over-refusal: ``git`` is not a data-consumer program, so the
    # exemption below could not see the search). The mask covers ONLY read-only
    # search subcommands, so ``git bisect run rm -rf /`` and other executing
    # subcommands keep their ``rm`` True and still classify.
    git_search_arg_mask = (
        _rm_git_search_arg_mask(tokens, programs)
        if any(_program_basename(p) == "git" for p in programs)
        else None
    )
    expect_program = True
    #: Index of the most recent command's program word, so a dispatcher's FIRST
    #: argument (its applet) can be recognised: ``busybox rm -rf /`` runs ``rm``.
    program_word_at = -1
    #: How many ``rm`` spans this argv has structurally classified. Bounds the
    #: per-``rm`` suffix re-scan to keep a ``rm``-padded argv linear;
    #: a wipe past the cap is still caught by the whole-text deny-net regex.
    rm_spans_classified = 0
    #: Per-SPAN cache of the recursive-force verdict, keyed on the span's
    #: program-word index, so a flagless ``rm`` flood costs O(1) per token.
    span_start_at = -2
    span_is_rf = False
    #: Per-SPAN cache of the overflow root/home-shape verdict, keyed on
    #: the same span program-word index. The overflow scan reads the WHOLE span's
    #: operands, so it is a property of the span, not of the operand index it is
    #: called at — computing it once per span keeps a bare-``rm`` flood past the cap
    #: (``sudo rm -fr rm rm …``) at O(1) per operand instead of rescanning the span
    #: suffix for every operand named ``rm`` (O(n²), past the gateway watchdog).
    span_overflow_targets: frozenset[str] = frozenset()
    #: Cumulative bytes of brace members materialized across every span so far. A
    #: single word AT the per-word count cap can still carry large members, and the
    #: span loop re-materializes a word's members once per span, so this is the only
    #: bound on the TOTAL expansion work. Once it crosses
    #: ``_RM_EXPANSION_BYTE_BUDGET`` no further span materializes members: each falls
    #: back to the cheap ``span_overflow_targets`` shape verdict instead.
    expansion_bytes = 0
    for i, token in enumerate(tokens):
        is_program_word = (
            expect_program and bool(token) and not _shell_normalizer.ENV_ASSIGNMENT_RE.match(token)
        )
        starts_command = is_program_word
        if is_program_word:
            expect_program = False
            program_word_at = i
        # A glued ``&`` ends the command (``echo hi& rm -rf /`` runs the ``rm``); the
        # quote/escape-aware :func:`_rm_token_ends_argv` catches that UNESCAPED ``&``.
        # A raw ``token.endswith("&")`` once rode alongside but also fired on a
        # BACKSLASH-ESCAPED ``\&`` (literal data), newly refusing a print-only
        # ``echo x\& rm -fr /``; dropped (GPT 6.1 over-refusal).
        if _rm_token_ends_argv(token):
            expect_program = True
        if _rm_program_basename(token) != "rm":
            continue
        # A ``find`` OPERAND that is not the word of an ``-exec``-family nested
        # command is a start path or a pattern VALUE -- data, never executed -- so an
        # ``rm`` sitting in one (``find / -name rm -o -name -fr -o -path /``) is a
        # mention. ``find … -exec rm -rf {} \;`` keeps the ``rm`` True in the mask and
        # still classifies here (and the nested-frame walk covers it too).
        if find_command_word_mask is not None and not find_command_word_mask[i]:
            continue
        # A ``git grep`` / ``git log --grep`` search ARGUMENT is a pattern or path
        # the subcommand only reads, never a command it runs, so an ``rm`` among
        # them (``git grep --no-index -e rm -e -fr ~``) is data (GPT 6.1
        # over-refusal). An executing ``git`` subcommand (``git bisect run rm -rf
        # /``) is left True by the mask and still classifies.
        if git_search_arg_mask is not None and not git_search_arg_mask[i]:
            continue
        # Executed iff ``rm`` leads its own command, is the FIRST argument of a
        # multi-call dispatcher (``busybox rm``), or its parent does NOT treat args
        # as data. A DENYLIST: an UNKNOWN parent defaults to EXECUTABLE; only a
        # ``_DATA_CONSUMER_PROGRAMS`` parent makes ``rm`` a mention (and ``echo rm
        # -rf / | sh`` still executes — ``_data_consumer_exempt`` refuses a pipe).
        dispatched_applet = (
            i == program_word_at + 1
            and program_word_at >= 0
            and _program_basename(tokens[program_word_at]) in _RM_APPLET_DISPATCHERS
        )
        # When the program is a multi-call dispatcher, its FIRST argument is the
        # applet that runs, so THAT — not ``busybox`` — is the effective parent of a
        # later ``rm``. ``busybox echo rm -rf /`` runs ``echo``, which prints it: a
        # mention. Resolve to the applet before the data-consumer test so the
        # dispatcher itself does not make its applet's arguments look executed.
        dispatcher_applet_is_consumer = (
            not dispatched_applet
            and program_word_at >= 0
            and program_word_at + 1 < len(tokens)
            and _program_basename(tokens[program_word_at]) in _RM_APPLET_DISPATCHERS
            and _program_basename(tokens[program_word_at + 1]) in _DATA_CONSUMER_PROGRAMS
        )
        if not (starts_command or dispatched_applet):
            if dispatcher_applet_is_consumer:
                continue
            if disqualified is None:
                disqualified = _rm_command_disqualified(tokens, programs)
            if _data_consumer_exempt(i, token, programs, tokens, command_disqualified=disqualified):
                continue
            # A ONE-WORD exec wrapper (``env`` / ``nice`` / ``setsid`` …) leaves
            # every token attributed to the WRAPPER, so ``_data_consumer_exempt``
            # (which reads ``programs[parent]``) cannot see that the real command is
            # a data consumer: ``env printf '%s\n' rm -fr /`` and ``nice echo rm -fr
            # /`` only PRINT the ``rm`` words (GPT 6.1 over-refusal). Resolve past
            # the wrapper and, when the EFFECTIVE program is a data consumer, treat
            # the ``rm`` as a mention -- retaining the pipeline/execution
            # disqualifier, so ``env printf … rm -fr / | sh`` (which runs the output)
            # still classifies.
            effective = _rm_effective_span_program(programs, tokens, i)
            if (
                effective != _program_basename(programs[i] if i < len(programs) else "")
                and effective in _DATA_CONSUMER_PROGRAMS
                and not disqualified
            ):
                continue
            # A DISPATCH wrapper (``sudo``/``setsid``/``nohup``/…) runs its first
            # argument as a new program; when that program is a data consumer
            # (``sudo printf '%s\n' rm -fr /``, ``setsid echo rm -fr /``) the later
            # ``rm`` is only PRINTED (GPT 6.1 over-refusal). Resolve the dispatched
            # program and skip -- retaining the pipeline/execution disqualifier, so
            # ``sudo printf … rm -fr / | sh`` still classifies, and keeping a real
            # dispatched ``sudo rm`` executable (its dispatched program is ``rm``,
            # not a data consumer, so this does not fire).
            dispatched = _rm_dispatched_program(programs, tokens, i)
            if (
                dispatched != _program_basename(programs[i] if i < len(programs) else "")
                and dispatched in _DATA_CONSUMER_PROGRAMS
                and not disqualified
            ):
                continue
        # A ``rm`` that is a trailing OPERAND of a filesystem-MOVER (``env rm -fr rm
        # rm`` runs ``rm -fr`` on operands ``rm rm``) is a path. A mover under an
        # exec wrapper is attributed to the wrapper, so skip the operand when it is
        # NOT the span's program word and the effective command is a mover.
        # A wrapper-reached EXECUTED ``rm`` (``ssh host rm -rf /tmp/x``) still
        # classifies.
        if not (starts_command or dispatched_applet):
            effective_program = _rm_effective_span_program(programs, tokens, i)
            if effective_program in _RM_MOVER_PROGRAMS:
                continue
            # A ``rm`` that is a POSITIONAL ARGUMENT of a shell ``-c`` invocation
            # (``sh -c '<string>' _ rm -fr /``) is DATA bound to ``$1``..``$n``, not
            # an executed command -- the outer shell runs only ``<string>``, which
            # ``_rm_targets_in_shell_c`` classifies on its own. Skip it here so an
            # argument-printing payload is not misread as a wipe. Gated on the
            # O(n) ``argv_has_shell_c`` precheck so a bare-``rm`` flood (no shell
            # ``-c``) never pays the helper's per-token left-scan.
            if shell_c_positional_mask is not None and shell_c_positional_mask[i]:
                continue
        # Bound the per-``rm`` suffix scans: a flagless ``rm`` flood
        # (``setsid true`` + 2,500 bare ``rm`` args) otherwise pays an O(span) scan
        # per token, O(n²), past the gateway watchdog. The recursive-force verdict is
        # a property of the SPAN, so compute it ONCE per span and reuse it; and a
        # NON-recursive-force span can never be a catastrophic wipe (base's ``rm -rf``
        # literal never matched it either), so skip its whole operand classification.
        if span_start_at != program_word_at:
            span_start_at = program_word_at
            span_is_rf = _rm_span_is_recursive_force(tokens, i)
            # Compute the overflow root/home-shape verdict ONCE per span, here at the
            # span's program word. It reads the whole span's operands, so
            # it does not depend on which operand ``i`` reached the overflow branch;
            # caching it stops the quadratic rescan a bare-``rm`` flood would cause.
            span_overflow_targets = _rm_overflow_span_targets(tokens, i)
        if not span_is_rf:
            continue
        if rm_spans_classified >= _RM_CLASSIFY_SPAN_CAP:
            # Past the per-span classification cap, do NOT blanket-deny: a long bulk
            # cleanup of recursive-force rm on DESCENDANTS (``rm -fr build0 ; … ;
            # rm -fr build69``) is legit and base allowed it, so failing closed to
            # root+home newly refused it. Instead run a
            # CHEAP, bounded root/home-SHAPE test on this overflow span's operands
            # (raw regex + prefix, no brace/dot machinery): fail closed only for the
            # class actually targeted. The verdict is a per-SPAN property computed
            # once at the span's program word (``span_overflow_targets``), so each
            # operand past the cap costs O(1) here, not an O(span) rescan (a
            # bare-``rm`` flood ``sudo rm -fr rm rm …`` was otherwise O(n²),
            # 38s past the gateway watchdog).
            found |= span_overflow_targets
            if {"root", "home"} <= found:
                break
            continue
        rm_spans_classified += 1
        # Cumulative brace-materialization budget: once the total expanded
        # bytes across prior spans crossed the ceiling, do NOT run the full
        # structural pass for this span (its ``for arg in expanded_args`` walk is
        # what the materialized bytes feed). Fall back to the same CHEAP per-span
        # root/home-shape verdict the span-count overflow uses — it reads the raw
        # operand tokens once with no brace materialization, so it is bounded and
        # does not newly refuse a legitimate descendant cleanup.
        if expansion_bytes > _RM_EXPANSION_BYTE_BUDGET:
            found |= span_overflow_targets
            if {"root", "home"} <= found:
                break
            continue
        # STRUCTURAL classification (below) denies the EXACT root/home target for
        # ANY ``rm`` — direct, dispatcher applet, or EXEC-WRAPPER reached. base's
        # DESCENDANT coverage (``rm -rf /etc``, ``rm -rf ~/.ssh``) is reproduced by
        # the base-contiguous-literal pin, so a descendant base matched is denied
        # while a WIDENED spelling (``rm -fr /tmp/x``) stays allowed.
        has_rec = has_force = has_npr = False
        end_of_options = False
        depth = 0
        operands: list[str] = []
        #: Parallel to ``operands``, metachar-safe for the RAW ``strip_quotes=True``
        #: classification only: each operand with its LITERAL (quoted/escaped,
        #: non-``${...}``) brace/glob metacharacters neutralized, so ``'/{,tmp}'`` /
        #: ``'/*'`` are not read as a shell-active brace / root glob (GPT 6.1
        #: over-refusal). The DECODED view keeps the operand verbatim (its quotes are
        #: already gone, and touching it regressed the nested-``$(...)`` frame), so a
        #: quoted-metachar literal inside a ``$(...)`` body is a documented residual.
        operands_metachar_safe: list[str] = []
        #: (``'~'`` / ``'$HOME'`` / ``'${HOME}'``), which bash passes verbatim as a
        #: literal cwd file — never the home dir. Collected from the RAW arg before
        #: quote-peeling so the home matcher can ignore the de-quoted spelling, and
        #: ``span_has_live_home`` keeps a co-present live ``~`` (``rm -fr '~' ~``)
        #: denying. A single-quoted ``'/'`` root is unaffected (home-only).
        home_single_quote_literals: set[str] = set()
        span_has_live_home = False
        # Base ran its whole-line literal against the QUOTE-NORMALIZED re-join, so
        # ``rm -rf "/etc"`` / ``rm "-rf" /etc`` denied on base; the raw pin misses
        # them. Reproduce STRUCTURALLY, raw view only: ``$HOME`` stays literal
        # so base's contiguity still excludes ``$HOME``-descendants.
        # ``rm {--recursive,--force,--no-preserve-root} {/,/tmp}`` reaches the floor
        # with brace GROUPS as single tokens matching no flag/operand, yet bash
        # expands each word and wipes ``/``. Expand every token's
        # statically-decidable alternation members BEFORE flag/operand parsing;
        # non-brace expands to itself. Bounded by ``_RM_BRACE_EXPANSION_CAP``.
        expanded_args: list[str] = []
        #: Count of RAW operand tokens (argv words BEFORE brace expansion). The
        #: operand cap targets a bare-``rm`` flood of many SEPARATE operands, not a
        #: single brace word that legitimately expands to many descendant members
        #: (``rm -rf {1..70}{,.log}`` is ONE raw operand -- a legit numbered cleanup
        #: the full classifier allows; capping on expanded members wrongly forced it
        #: into the fail-closed shape verdict).
        raw_operand_count = 0
        #: Set when this span's brace materialization crossed the cumulative byte
        #: budget mid-expansion: the structural pass below is then skipped
        #: and the cheap per-span shape verdict is used instead.
        brace_budget_exhausted = False
        for raw_arg in tokens[i + 1 :]:
            # An unquoted word starting with ``#`` opens a shell COMMENT — discarded
            # with the rest of the line, never an ``rm`` operand (``rm -rf dist #
            # remove /`` deletes ``dist``). A quoted/glued ``#`` is literal.
            if strip_quotes and raw_arg.startswith("#"):
                break
            raw_operand_count += 1
            # Expand this word's brace members via the shared ``_brace_expansions``.
            # It returns ``[word]`` for a non-brace / single-member word and ``None``
            # on a product past the shared cap; on overflow keep the raw word (the
            # per-span ``brace_overflow`` fail-closed below, plus the deny-net regex,
            # still catch a catastrophic member). On the RAW view a QUOTED brace
            # (``'/{,tmp}'``) is a LITERAL filename, not an expansion -- the shared
            # expander is quote-UNAWARE and would expand it to ``'/'`` (root). Expand
            # the metachar-neutralized spelling so a quoted-literal brace does not
            # expand, while an UNQUOTED ``{a,b}`` and a ``${...}`` parameter
            # expansion are unchanged. Decoded view uses ``raw_arg`` as before.
            brace_src = _rm_neutralize_literal_metachars(raw_arg) if strip_quotes else raw_arg
            members = _brace_expansions(brace_src)
            if brace_src != raw_arg and not (members and len(members) > 1):
                # The raw view neutralized a quoted-literal metachar and it did NOT
                # expand to multiple members, so the single ``members`` entry is the
                # de-escaped ``brace_src``; keep the ORIGINAL ``raw_arg`` to preserve
                # its escaping/quoting (``\$HOME`` must stay a literal, not become a
                # live ``$HOME``). When ``brace_src == raw_arg`` behaviour is exactly
                # as before.
                produced = [raw_arg]
            else:
                produced = members if members else [raw_arg]
            # Charge the materialized members against the cumulative byte budget.
            # A word at the per-word count cap can still carry 256 x
            # ~16 KB members, and this loop runs once per span, so without a byte
            # bound a 64-span chain costs ~116 CPU s past the gate watchdog. Once the
            # running total crosses the ceiling, stop expanding this span and fall
            # back to the cheap shape verdict below — never materialize the overflow.
            expansion_bytes += sum(len(w) for w in produced)
            if expansion_bytes > _RM_EXPANSION_BYTE_BUDGET:
                brace_budget_exhausted = True
                break
            expanded_args.extend(produced)
            # Stop at THIS ``rm``'s command boundary — an unescaped ``;``/``&``/
            # ``|``/newline ends the command, so a later command's operands are not
            # brace-expanded (``rm a ;`` + brace blobs is O(commands × operands).
            # A separator bare only from a peeled quote (``';'``) is a
            # literal — gate on ``not did_peel`` (``rm -rf ';' /`` deletes root).
            if strip_quotes:
                # A control character that is bare only BECAUSE a quote pair was
                # peeled was QUOTED in the source (``';'`` is a filename), so it is
                # not a boundary: ``rm -rf ';' /`` really deletes root.
                _boundary_arg = _strip_outer_quotes(raw_arg)
                _boundary_did_peel = _boundary_arg != raw_arg
                # A ``)`` that closes a command SUBSTITUTION in this word (``$(…)``
                # / backtick) is NOT an argv boundary -- the substitution is one
                # outer word whose body is a separate frame, and the ``rm`` argv
                # continues after the ``)`` (``rm -fr $(true) ~`` still reaches
                # ``~``). Treating that ``)`` as a boundary ended the span early and
                # let a trailing home/root operand slip (GPT 6.1 fail-open). A BARE
                # subshell closer (``(rm -fr /)`` -> word ``/)``, no ``$(`` opener)
                # still ends the argv, so root in a bare subshell cannot hide.
                _word_has_cmdsub = "$(" in _boundary_arg or "`" in _boundary_arg
                if (
                    not _boundary_did_peel
                    and _rm_unescaped_boundary(
                        _boundary_arg, treat_subshell_closer=not _word_has_cmdsub
                    )
                    is not None
                ):
                    break
        if brace_budget_exhausted:
            # This span's expansion crossed the cumulative byte ceiling: do not run
            # the structural pass over a partially-materialized operand list (it
            # would be both unbounded-costly on the next span and incomplete here).
            # Use the cheap per-span root/home-shape verdict, computed from the raw
            # operand tokens with no materialization.
            found |= span_overflow_targets
            if {"root", "home"} <= found:
                break
            continue
        # Cap the OPERAND COUNT the structural pass walks for one span. The
        # structural classification below builds a candidate set and runs several
        # ``any(... for op in candidates)`` passes, each O(operands), so a single
        # span with hundreds of operands (a bare-``rm`` flood ``sudo rm -fr rm rm …
        # rm`` with 800 words) is O(operands) per pass and ~22s under coverage
        # instrumentation. Past the cap use the cheap per-span
        # root/home-shape verdict (raw regex + prefix, no candidate/brace/dot
        # machinery) -- it reads the operands once and classifies the class actually
        # targeted, so a real wipe past the cap still denies and a bulk descendant
        # cleanup still allows.
        if raw_operand_count > _RM_SPAN_OPERAND_CAP:
            found |= span_overflow_targets
            if {"root", "home"} <= found:
                break
            continue
        _redirect_target_next = False
        for arg in expanded_args:
            # A word that is a REDIRECT SOURCE/TARGET (the file after ``<`` / ``>`` /
            # ``<<`` / ``>>`` / ``<<<`` / ``<>`` / a fd-prefixed ``2>``) is opened or
            # written by the shell, not an operand ``rm`` deletes, so it must not be
            # classified as a root/home target: ``rm -fr x < /`` reads from ``/``,
            # it does not delete it (Opus 5.5 over-refusal). Skip the redirect
            # operator token and the word that follows it. The operator is tested on
            # the RAW token (quotes/escapes intact): a redirection is never quoted,
            # so a QUOTED ``">"`` is a literal filename, not a redirect, and must NOT
            # consume the next operand (``rm -fr ">" "$HOME"`` deletes home -- GPT
            # 6.1 fail-open).
            if _redirect_target_next:
                _redirect_target_next = False
                continue
            if _rm_is_redirect_operator(arg):
                # ``2>&1`` fuses the target into the operator word; a bare ``<`` /
                # ``>`` takes the NEXT word as its target.
                if not _rm_redirect_has_glued_target(arg):
                    _redirect_target_next = True
                continue
            operand = _strip_outer_quotes(arg) if strip_quotes else arg
            quote_peeled = operand != arg
            # Quoting a flag does NOT stop GNU ``rm`` option parsing — bash strips
            # the quotes, so ``rm '-rf' ~`` / ``rm "-rf" ~`` / ``rm -r''f ~`` / ``rm
            # \-rf ~`` all reach ``rm`` as ``-rf``. Test the flag predicates
            # on the FULLY de-quoted spelling; the raw ``arg`` keeps its quotes and
            # would be mis-read as an operand, failing open on the wipe.
            flag_tok = _rm_strip_all_quotes(arg) if strip_quotes else arg
            # A glued operator (``/;reboot``) leaves the real operand before it;
            # classify that head and end the argv at the boundary. A separator bare
            # only from a peeled quote (``';'``) is a literal, not a boundary, so the
            # split is suppressed (e.g. ``rm -rf ';' /``), as for a backslash-escaped
            # operator. Raw view only.
            glued_boundary = False
            # A token carrying a command SUBSTITUTION (``$(…)`` / backtick) must not
            # be split at its closing ``)``: that ``)`` ends the substitution, not
            # the ``rm`` argv, so splitting there dropped a later home/root operand
            # (``rm -fr $(:) "$HOME"``), GPT 6.1 fail-open. The substitution body is
            # a separate frame; here the whole word is left intact.
            _arg_has_cmdsub = "$(" in arg or "`" in arg
            if (
                strip_quotes
                and not quote_peeled
                and not _arg_has_cmdsub
                and depth + _rm_substitution_depth_delta(arg) <= 0
            ):
                operand, glued_boundary = _rm_operand_before_boundary(operand)
            # A glued separator can split a FLAG from its terminator (``-fr;``): the
            # prefix is the flag, not an operand. Re-read the glued
            # prefix as a flag so ``rm ~ -fr; true`` is recognised recursive-force.
            glued_flag = _rm_strip_all_quotes(operand) if glued_boundary else ""
            if flag_tok == "--" and not end_of_options:
                end_of_options = True
            elif not end_of_options and flag_tok == "--no-preserve-root":
                has_npr = True
            elif not end_of_options and _rm_is_recursive_flag(flag_tok):
                has_rec = True
                if _rm_is_force_flag(flag_tok):
                    has_force = True
            elif not end_of_options and _rm_is_force_flag(flag_tok):
                has_force = True
            elif (
                not end_of_options
                and glued_flag.startswith("-")
                and (_rm_is_recursive_flag(glued_flag) or _rm_is_force_flag(glued_flag))
            ):
                # The glued prefix (``-fr`` from ``-fr;``) is a flag, not an operand.
                if _rm_is_recursive_flag(glued_flag):
                    has_rec = True
                if _rm_is_force_flag(glued_flag):
                    has_force = True
                if glued_boundary:
                    break  # the separator ends this rm's argv
            elif not end_of_options and glued_flag == "--no-preserve-root":
                has_npr = True
                if glued_boundary:
                    break
            elif operand:
                _dequoted_arg = _rm_strip_all_quotes(arg)
                if strip_quotes and (
                    _RM_HOME_ITSELF_RE.fullmatch(_dequoted_arg)
                    or _RM_HOME_ITSELF_RE.fullmatch(_rm_normalize_dot_segments(_dequoted_arg))
                ):
                    # Live only when the ``~``/``$HOME`` is unquoted/unescaped (or a
                    # double-quoted ``$HOME``); a ``'~'`` / ``"~"`` / ``\~`` / ``'$HOME'``
                    # is a literal cwd file. A dot-tail (``"~"/./``) resolves to the
                    # same home shape, so the home-ITSELF test is run on BOTH the raw
                    # de-quoted operand and its dot-normalized form -- else a
                    # quoted-literal tilde with a ``/.`` tail was never recognised as a
                    # home-shaped operand, so it was neither excluded as a literal nor
                    # counted as live, and a co-present live ``$HOME`` elsewhere on the
                    # line (``rm -fr "$HOME"/.cache "~"/./``) wrongly activated it.
                    # Mask the non-live spellings: if nothing home-shaped survives,
                    # this operand is a literal.
                    if not _RM_HOME_REF_RE.search(_rm_mask_non_live_home(arg)) and not any(
                        c == "~" for c in _rm_mask_non_live_home(arg)
                    ):
                        home_single_quote_literals.add(_dequoted_arg)
                        home_single_quote_literals.add(_rm_normalize_dot_segments(_dequoted_arg))
                    else:
                        span_has_live_home = True
                operands.append(operand)
                # Metachar-safe twin for the raw view only: a QUOTED literal
                # ``{``/``}``/``*`` (not a ``${...}`` expansion) is inert. Only
                # substitute the neutralized ``arg`` when it actually differs by a
                # neutralized metachar AND no glued boundary trimmed the operand --
                # otherwise keep ``operand`` verbatim so an escaped ``\$HOME`` and
                # every non-metachar operand classify exactly as before. The decoded
                # view keeps the operand verbatim (documented ``$(...)`` residual).
                safe_arg = _rm_neutralize_literal_metachars(arg) if strip_quotes else operand
                if (
                    strip_quotes
                    and not glued_boundary
                    and _RM_LITERAL_METACHAR_SENTINEL in safe_arg
                ):
                    operands_metachar_safe.append(safe_arg)
                else:
                    operands_metachar_safe.append(operand)
            # A shell-ELIDED empty word (``rm "" -rf /``) contributes no flag/
            # operand — bash expands ``""`` to nothing, so base never saw it and the
            # ``rm -rf /`` stayed contiguous.
            depth += _rm_substitution_depth_delta(arg)
            # The rm span started at depth 0 (relative to its own program word). A
            # token whose delta drops depth BELOW 0 is the ``)`` / closer of the
            # substitution that HOLDS this rm (``ls $(rm -rf ./build) ~``): the rm
            # argv ends there, so the OUTER command's operands (the trailing ``~``)
            # are not miscounted as rm targets (base allowed both).
            if depth < 0:
                break
            # A quoted-``;`` (``';'``) or backslash-ESCAPED operator (``a\;b``) is a
            # literal filename, not a terminator — a non-quote-aware ``_ends_argv``
            # on the raw ``arg`` would stop the argv before a later ``/`` or ``~``
            # (e.g. ``rm -rf ';' /``, ``r''m -fr 'a;b' ~``). Both the raw and the
            # decoded view route the boundary test through the SAME quote-aware
            # helper so an operand keeps its literal punctuation; the raw view adds
            # the unpeeled/unescaped guard a peeled quote would otherwise lose.
            if strip_quotes:
                raw_terminates = (
                    not quote_peeled
                    and _rm_token_ends_argv(arg)
                    and _rm_unescaped_boundary(arg) is not None
                )
            else:
                raw_terminates = _rm_token_ends_argv(arg)
            if depth <= 0 and (glued_boundary or raw_terminates):
                break
        # STRUCTURAL classification denies the EXACT root/home target ITSELF in ANY
        # flag spelling. A DESCENDANT in a WIDENED spelling (``rm -fr /tmp/x``) is
        # NOT denied here (base's whole-line literal never matched it).
        # base's OWN ``rm -rf /``/``~`` descendant coverage is the LIVE
        # whole-line deny-net regex, which already scans the raw command text.
        root_re, home_re = _RM_ROOT_ITSELF_RE, _RM_HOME_ITSELF_RE
        # Classify each operand AND its dot-normalized form (``/./``, ``/tmp/../``,
        # ``~/.`` resolve to root/home). On the RAW split also classify the fully
        # de-quoted spelling so a PARTIALLY quoted ``"$HOME"/`` keeps its anchor;
        # NOT on the decoded view, where a decode-produced
        # quote is a literal filename char (``$'"/"'`` is a file ``/``, not root).
        # Classify the metachar-SAFE operands so a RAW quoted-literal ``'/*'`` /
        # ``'/{,tmp}'`` is not read as a root glob / expansion (GPT 6.1 over-refusal);
        # on the decoded view ``operands_metachar_safe`` IS ``operands``, so the
        # nested-``$(...)`` frame classification is unchanged.
        candidates = list(operands_metachar_safe) + [
            _rm_normalize_dot_segments(op) for op in operands_metachar_safe
        ]
        if strip_quotes:
            dequoted = [_rm_strip_all_quotes(op) for op in operands_metachar_safe]
            candidates += dequoted + [_rm_normalize_dot_segments(op) for op in dequoted]
        # A brace word (``{~,/x}``, ``/{,bin}``, ``$HOME/{,.cache}``) expands to
        # several operands and the exact matchers must see each, else the root/home
        # member hides behind the un-expandable brace word. Expand every
        # candidate via the shared ``_brace_expansions`` and classify each member +
        # its dot-normalized form; a non-brace / single-member word contributes
        # nothing extra. A product past the shared cap returns ``None`` — fail CLOSED
        # (treat the span as reaching root/home) rather than drop a catastrophic
        # member past the bound.
        brace_members: list[str] = []
        brace_overflow = False
        for op in list(candidates):
            expanded = _brace_expansions(op)
            if expanded is None:
                # Overflow past the shared cap. Fail CLOSED only when a member COULD
                # be root/home — i.e. the word, with its brace groups stripped, is
                # empty or still root/home-rooted. A word with a non-root LITERAL
                # prefix (``./bench-out/run-{1..500}``, ``/tmp/kc-shard-{000..511}``,
                # ``out/{a,b}{1..99}``) can only expand to descendants, so a huge
                # relative/descendant brace is NOT a catastrophic wipe.
                if _rm_brace_word_could_be_catastrophic(op):
                    brace_overflow = True
                continue
            brace_members.extend(expanded)
        if brace_members:
            candidates += brace_members + [_rm_normalize_dot_segments(m) for m in brace_members]
        root_target = any(root_re.fullmatch(op) for op in candidates)
        home_excluded = home_single_quote_literals if not span_has_live_home else set()
        home_target = any(home_re.fullmatch(op) and op not in home_excluded for op in candidates)
        # ``x=$HOME; rm -rf "$x"`` — the decoded view substitutes ``$x`` to the
        # EXPANDED home (no ``~``/``$HOME`` marker), matching neither matcher.
        # Classify an operand that IS the expanded home as ``home``, decoded
        # view and the reconstructed-literal view alike: a literal absolute path that
        # IS the real home (``echo /home/alice | xargs rm -fr``) is the home dir
        # whichever view rebuilt it. Fold separators + lowercase + ``/``+``*``
        # tail strip (``expanduser`` native-case on Windows), else ``/*`` slip.
        if not home_target:
            home_real = _rm_expanded_home_path()
            if home_real and any(
                _rm_strip_home_itself_tail(_rm_fold_home_case(op.replace("\\", "/"))) == home_real
                or _rm_strip_home_itself_tail(
                    _rm_fold_home_case(_rm_normalize_dot_segments(op).replace("\\", "/"))
                )
                == home_real
                for op in candidates
            ):
                home_target = True

        # base's own ``rm -rf /.*`` / ``rm -rf ~.*`` descendant coverage is the LIVE
        # whole-line deny-net regex, which scans the raw command text; the structural
        # path below denies the exact root/home target ITSELF in any flag spelling,
        # and a brace-overflow fails closed. A separate ``base_rf_descendant`` pin
        # here only re-spelled the regex's descendant denial
        # (one mechanism, not two) -- the full suite stays green without it, so the
        # descendant case is left to the regex.
        if has_npr or (has_rec and has_force):
            if root_target or brace_overflow:
                found.add("root")
            if home_target or brace_overflow:
                found.add("home")
    return frozenset(found)
