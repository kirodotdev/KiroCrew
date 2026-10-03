"""Argv-structural floor for recursive-force ``rm`` deletion of root / home.

Split out of ``argv_floor.py`` as a cohesive sibling: this module owns the
recursive-force ``rm`` deny floor and nothing else. ``argv_floor.is_denied``'s
caller reaches it through :func:`_recursive_force_rm_targets` (and the
fail-closed fallback), which read only the ``rm`` command's OWN argv and return
the catastrophic target set ``{"root", "home"}`` a command deletes.

Enforcement lives here rather than in the regex tier: a whole-line text pattern
cannot tell an ``rm`` operand from the same text quoted in a ``git commit -m``
message or a ``grep`` pattern, so the two ``rm`` catalog patterns are stripped
from the regex tier in ``is_denied`` and this floor is their sole enforcement.
"""

from __future__ import annotations

import fnmatch as _fnmatch
import os as _os
import posixpath as _posixpath
import re

from . import shell_normalizer as _shell_normalizer
from .shell_normalizer import (
    _DATA_CONSUMER_PROGRAMS,
    _argv_programs,
    _brace_pairs,
    _data_consumer_exempt,
    _decode_shell_quoted_literals,
    _ends_argv,
    _program_basename,
    _shell_payload_walk,
    _split_brace_alternatives,
    _split_shell_words,
    _substitution_depth_delta,
)

# ── Recursive-force ``rm`` deletion floor ──
# ``rm`` recursively force-deleting the filesystem ROOT or the user's HOME is
# catastrophic; a path UNDER either (``/tmp/scratch``, ``$HOME/.cache``) is an
# ordinary cleanup and must stay allowed. The catalog literals ``rm -rf /`` /
# ``rm -rf ~`` only matched one flag spelling, and every attempt to widen them
# as a REGEX went wrong two ways at once:
#   * a left-to-right pattern cannot see flags AFTER the operand, which GNU
#     ``getopt`` accepts (``rm / -rf --no-preserve-root``); and
#   * a text pattern matches a SUBSTRING of the whole command line, so it fired
#     on ``/tmp/x`` (a descendant of ``/``) and on the words ``rm -fr /`` sitting
#     inside a ``git commit -m`` message or a ``grep`` pattern.
# The only sound closure is argv-STRUCTURAL and EXACT, like the self-protection
# and git-publish floors: tokenize, look only at the ``rm`` command's OWN argv,
# collect the flags from every position, and deny only when a resolved operand
# IS the root or the home directory itself — never a descendant, never a text
# mention. This floor is therefore the SOLE enforcement (its catalog patterns are
# stripped from the regex tier in ``is_denied``, exactly as git-publish is), so
# there is no whole-line text match left to fire on a commit message.
#
# The tokens come from ``_split_shell_words`` — the RAW, quote-resolved but
# ENV-UNEXPANDED split — for two reasons the review named: (1) a home operand
# must be classified by its written spelling (``~`` / ``$HOME`` / ``${HOME}``),
# because the expanding tokenizer turns ``$HOME`` into its absolute path which
# then reads as ROOT, inverting the home rule's opt-out (GPT + Opus finding); and
# (2) it keeps the classification on what the argv literally is.
#
# The floor fires only for a command whose PROGRAM is ``rm`` (``_argv_programs``
# tracks command boundaries), so ``confirm -rf /``, an ``rm`` mentioned as data
# (``echo rm -rf /``), and a sibling command's flags (``ls -rf; rm /tmp/x``) do
# not trigger it.


#: ``rm``'s long options, so an abbreviation can be tested for ambiguity. GNU
#: ``getopt_long`` accepts any UNAMBIGUOUS prefix of a long option, so ``rm
#: --rec …`` and ``rm --for …`` run the identical recursive/force delete while a
#: fixed ``--recursive``/``--force`` string comparison would miss them (GPT
#: security-class). A prefix is honoured only when it matches exactly ONE
#: of ``rm``'s long options — ``--r`` resolves to ``--recursive`` (nothing else
#: begins with ``r``), ``--f`` to ``--force`` — never a prefix shared by two.
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
def _rm_is_recursive_flag(tok: str) -> bool:
    if tok.startswith("--"):
        return _rm_long_option_resolves_to(tok, "--recursive")
    return bool(re.fullmatch(r"-[a-z]*r[a-z]*", tok))


#: Whether an ``rm`` argument token carries the force flag (``--force`` or an
#: unambiguous prefix of it, or a single-dash short cluster containing ``f``).
def _rm_is_force_flag(tok: str) -> bool:
    if tok.startswith("--"):
        return _rm_long_option_resolves_to(tok, "--force")
    return bool(re.fullmatch(r"-[a-z]*f[a-z]*", tok))


#: The filesystem ROOT ITSELF — ``/`` (a run of slashes) or the ``/*`` glob over
#: its children, with an optional trailing slash, and NOTHING under it. Used for
#: an ``rm`` reached through an EXEC WRAPPER (``setsid rm -rf /``, ``sudo …``):
#: base ``main`` caught a wrapper-reached descendant only incidentally, as a
#: substring of its ``rm -rf /`` literal, so denying wrapper-reached DESCENDANTS
#: newly refuses benign work the widening never intended (``docker exec kc-ci
#: rm -fr /tmp/build-cache`` — a container cache cleanup base ALLOWED, since its
#: literal is ``rm -rf`` not ``rm -fr``; Security Scope ruling). So a
#: wrapper-reached ``rm`` denies only the catastrophic root ITSELF.
_RM_ROOT_ITSELF_RE = re.compile(r"/+(?:\*/*)?")
#: The HOME dir ITSELF — ``~`` / ``$HOME`` / ``${HOME}``, bare or with a RUN of
#: trailing slashes (``~//`` / ``~///``) or the ``~/*`` glob, nothing under it.
#: A path-collapsing shell treats ``~//`` and ``~///`` as home, so any run of
#: trailing slashes is accepted; the glob ``*`` is
#: admitted only as the whole remainder after the slashes. The ``${home}`` brace
#: form also admits a ``%``/``%%`` suffix-removal whose pattern is only slashes
#: (``${HOME%/}`` / ``${HOME%%/}``): the shell strips a trailing ``/`` the value
#: does not have, so the expansion is the home dir itself (GPT security-class).
#: It further admits the ``:?`` parameter-CHECK operator (``${HOME:?}`` /
#: ``${HOME:?message}``), a documented defensive idiom that aborts when HOME is
#: unset and OTHERWISE expands to HOME's value UNCHANGED — so ``rm -rf
#: "${HOME:?}"`` wipes the home tree exactly as ``rm -rf "$HOME"`` does (GPT 5.6
#: F1, security-class, UPHOLD-FENCED). The ``:?`` word (an error message) can be
#: any text up to the closing brace. It also admits the IDENTITY substring
#: expansion ``${HOME:0}`` — an offset of zero with NO ``:length`` suffix expands
#: to HOME's whole value unchanged (``${HOME: 0}`` / ``${HOME:00}`` are the same),
#: so ``rm -rf "${HOME:0}"`` is an exact-home wipe (GPT 6.1, security-class,
#: UPHOLD-FENCED). The VALUE-CHANGING operators ``:-`` / ``:+``
#: / ``:=`` / a NON-zero or LENGTH-bearing substring (``:1`` / ``:0:3``) are
#: deliberately NOT admitted: each can expand to
#: something other than HOME (``${HOME:-/x}`` yields ``/x`` when unset, ``${HOME:+y}``
#: yields ``y`` when set, ``${HOME:0:3}`` truncates), so matching them would
#: over-refuse a value that is not the home dir. Wrapper-reached only.
_RM_HOME_ITSELF_RE = re.compile(
    r"(?:~|\$\{home(?:%%?/*|:\?[^}]*|:[ \t]*0+[ \t]*)?\}|\$home(?![a-z0-9_]))(?:/+(?:\*/*)?)?",
    re.IGNORECASE,
)
#: Escape / quote / substitution characters that can reconstruct the ``rm``
#: program name from text that does not contain the literal ``rm`` (a folded
#: ``"r\<nl>m"``, an octal ``$'r\555'``). The cheap pre-filter admits a command
#: carrying any of these so the walk gets a chance to decode it.
_RM_OBFUSCATION_MACHINERY_RE = re.compile(r"[\\$`'\"]")

#: Ceiling on how many nested-frame descents ONE top-level classification may
#: make. Each ``find -exec`` / ``sh -c`` / interpreter-code span classified as
#: its own argv recurses back into :func:`_rm_targets_in_argv`, so a crafted
#: nest (``sh -c 'sh -c 'sh -c … rm -rf /'''``) would fan out and hang the
#: SYNCHRONOUS PreToolUse gate — measured seconds on a ~120-byte command (Opus
#: security-class). The budget is a single mutable cell threaded through the
#: recursion and decremented on every descent; once it reaches zero no further
#: nested span is opened, so the total work is linear in the cap regardless of
#: nesting depth. It fails SAFE: a real ``rm`` at any reachable depth is already
#: classified by the frames the walk visits BEFORE the cap bites (a genuine
#: nested wipe denies at the shallow frame that carries it), and the raw tier
#: still sees the whole command text — so the cap drops only pathological
#: deep-nest coverage, never a shallow real target. 64 is far past any real
#: command's nesting yet bounds a hostile one to a few milliseconds.
_RM_DESCENT_BUDGET = 64

#: How many ``rm`` command spans one argv structurally classifies before it
#: stops. Each ``rm`` token that leads a command re-scans its operand suffix, so
#: an argv padded with thousands of ``rm`` words (``env rm -fr `` + ``rm `` *
#: 3000 + ``./build``) is quadratic in token count on the synchronous gate and
#: measured 44 s — past the desktop gateway's 25 s hard-exit deadline (GPT 6.1
#: F2, security-class). A real command carries a handful of ``rm`` invocations,
#: so this bound never trims a legitimate one; past it the structural scan stops,
#: and a catastrophic literal in a later span is still caught by the whole-text
#: ``rm -rf /.*`` / ``rm -rf ~.*`` deny-net regex, so the cap cannot fail open.
_RM_CLASSIFY_SPAN_CAP = 64

#: How many execution-substitution bodies (``$(…)`` / backtick / ``${ …;}`` /
#: ``(…)`` subshell) one source extracts before it stops. An unterminated opener
#: makes the matcher yield the whole remainder as a body, and each body is
#: re-tokenized, so a source padded with openers (``"echo " + "${ " * 1600``) is
#: quadratic on the synchronous gate (Opus security-class). A real command
#: carries a handful; past the cap, extraction stops — a literal ``rm -rf /`` in
#: a later body is still caught by the whole-text deny-net regex, so this cannot
#: fail open.
_RM_SUBST_BODY_CAP = 64


def _rm_operand_before_boundary(operand: str) -> "tuple[str, bool]":
    """The operand text up to its first unquoted control-operator boundary.

    Returns ``(head, ended)``: *head* is the operand with everything from the
    first ``;`` / ``&`` / ``|`` / newline onward removed, and *ended* is True
    when such a boundary was present. The tokens reaching here have already had
    their quotes resolved (raw split) or normalized away (decoded view), so a
    remaining operator character is unquoted and genuinely separates commands —
    ``rm -rf /;reboot`` tokenizes to the single operand ``/;reboot`` whose real
    target is ``/``. Splitting here classifies that
    ``/`` and stops the argv, so a command glued after the boundary is neither
    read as another rm operand nor able to hide the target before it.
    """
    match = _rm_unescaped_boundary(operand)
    if match is None:
        return operand, False
    return operand[:match], True


def _rm_unescaped_boundary(operand: str) -> "int | None":
    """Index of the first control-operator (``;`` / ``&`` / ``|`` / newline) that
    is NOT backslash-escaped, or ``None``.

    ``_split_shell_words`` preserves a source backslash, so ``rm -rf a\\;b /*``
    reaches here as the single operand ``a\\;b`` whose ``;`` is an ESCAPED literal
    filename character (bash runs ``rm`` on a file named ``a;b``), not a command
    separator — splitting there would end the argv before ``/*`` and fail open
    (Opus security-class). A backslash escapes the next character, so a boundary
    char immediately preceded by an odd run of backslashes is literal and skipped.
    """
    k = 0
    n = len(operand)
    while k < n:
        ch = operand[k]
        if ch == "\\":
            k += 2  # the backslash escapes the next char; neither is a boundary
            continue
        if ch in ";&|\n":
            return k
        k += 1
    return None


def _rm_strip_surrounding_quotes(token: str) -> str:
    """Peel balanced surrounding quote pairs from a raw operand token.

    ``_split_shell_words`` leaves a quoted operand quoted (``"$home"``), so an
    exact operand match needs the wrapper removed. Only a matching leading and
    trailing quote of the same kind is peeled, to a fixed point, so an operand
    that merely CONTAINS a quote is left alone.
    """
    return _rm_strip_surrounding_quotes_reporting(token)[0]


def _rm_strip_surrounding_quotes_reporting(token: str) -> "tuple[str, bool]":
    """``(peeled_token, did_peel)`` — like :func:`_rm_strip_surrounding_quotes`
    but reports whether ANY surrounding quote pair was removed.

    A control-operator character that only becomes bare BECAUSE a quote was
    peeled was QUOTED in the source (``';'`` is a literal filename argument, not
    a command separator), so the operand-boundary split must NOT fire on it:
    ``rm -rf ';' /`` has operands ``;`` and ``/`` and really deletes root, but
    treating the peeled ``;`` as a boundary ends the argv before ``/`` and fails
    open (Opus security-class).
    """
    previous = None
    peeled = False
    while token != previous:
        previous = token
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
            peeled = True
    return token, peeled


def _rm_strip_all_quotes(token: str) -> str:
    """Remove every unescaped shell quote character from an operand.

    A shell removes quoting during word expansion, so ``"$HOME"/`` and
    ``$HOME/`` are the SAME path, as are ``"${HOME}"/x`` and ``${HOME}/x`` and a
    split ``"$HO"ME``. ``_rm_strip_surrounding_quotes`` only peels a BALANCED
    surrounding pair, so a PARTIALLY quoted operand keeps a leading ``"`` that
    defeats the ``~`` / ``$HOME`` anchor of the home/root matchers (GPT
    security-class: ``setsid rm -fr "$HOME"/`` bypassed the enabled home rule).
    This yields the de-quoted spelling the matchers are anchored on; a backslash
    escape keeps the quote it escapes (``\\"`` is a literal quote char in the
    filename, not a quoting delimiter).
    """
    out: list[str] = []
    i = 0
    n = len(token)
    while i < n:
        ch = token[i]
        if ch == "\\" and i + 1 < n:
            out.append(token[i + 1])
            i += 2
            continue
        if ch in "\"'":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _rm_normalize_dot_segments(operand: str) -> str:
    """Collapse ``.`` / ``..`` path segments in an rm operand, LEXICALLY.

    A shell hands ``rm`` the operand verbatim and the kernel resolves the dot
    segments, so ``/./`` / ``/.`` / ``/tmp/../`` are all the filesystem ROOT and
    ``~/./`` is the home dir — yet the exact-root / exact-home matchers see a
    string that is not ``/`` and miss it (GPT security-class: ``setsid rm -fr
    /./`` bypassed the wrapped-root guard). This resolves the segments the way
    ``os.path.normpath`` does but WITHOUT touching the filesystem (no ``realpath``,
    so a symlink is never followed — the resolved sensitive-path keystone remains
    the layer that resolves), and preserves a leading ``~`` / ``$HOME`` /
    ``${HOME}`` marker plus a trailing glob ``*`` so the home and glob matchers
    still fire on the normalized form.

    Returns the operand unchanged when it carries no dot segment, so the raw
    classification (which keeps ``$HOME``/``~`` spellings) is unaffected.
    """
    if "." not in operand:
        return operand
    # Preserve a leading home marker and a trailing ``*`` glob across normpath,
    # which would otherwise mangle ``~`` or drop the glob.
    prefix = ""
    for marker in ("~", "${home}", "$home"):
        if operand[: len(marker)].lower() == marker:
            prefix = operand[: len(marker)]
            operand = operand[len(marker) :] or "/"
            break
    glob_tail = ""
    if operand.endswith("/*"):
        operand, glob_tail = operand[:-1], "*"
    if prefix:
        # A ``..`` after the home marker must collapse against HOME, not against
        # ``/``: with ``HOME=/home/alice``, ``$HOME/../alice`` IS the home dir, but
        # normalizing only the REMAINDER (``/../alice`` -> ``/alice``) collapses
        # ``..`` against root and leaves ``$home/alice``, which the exact-home
        # matcher misses (GPT security-class, exact home deletion). Resolve against
        # the REAL expanded home (the same ``os.path.expanduser("~")`` the shell
        # normalizer uses for ``$HOME``), normalize the WHOLE joined path, then map
        # the result back to the marker spelling the home matcher recognizes:
        # EXACTLY home -> home ITSELF (restore ``prefix``); still UNDER home ->
        # a home descendant (``prefix`` + the sub-path); ESCAPED above home
        # (``$HOME/../..``) -> the bare normalized absolute path (no marker), which
        # the root/descendant matchers then judge on its own. ``normpath`` is
        # lexical — no ``realpath``, so no symlink is followed. ``expanduser`` is
        # platform-native, so on Windows it returns a BACKSLASH path
        # (``C:\Users\x``); fold the separators to ``/`` first so the ``..``
        # collapse against the forward-slash operand is consistent on every
        # platform (Windows CI: ``$HOME/../<home-base>`` must still resolve to home).
        # The *operand* reaching this helper is already LOWERCASED by the shell
        # walk, but ``expanduser`` returns the home path in its native case
        # (a mixed-case ``C:/Users/<account>`` on the Windows runner), so a
        # case-sensitive compare would miss ``$HOME/../<home-base>`` on a
        # case-INSENSITIVE filesystem (Windows CI failure). Lowercase the real
        # home too so both sides are compared in the same case the walk uses;
        # ``..``-collapse is case-independent, so this cannot widen POSIX, where
        # the lowercased operand already matched a lowercase home basename.
        home_real = _posixpath.normpath(_os.path.expanduser("~").replace("\\", "/")).lower()
        joined = home_real + ("" if operand == "/" else operand)
        try:
            collapsed_full = _posixpath.normpath(joined)
        except (TypeError, ValueError):
            return prefix + operand + glob_tail
        # The home MATCHER admits a glob only after a ``/`` separator
        # (``_RM_HOME_ITSELF_RE`` is ``~(?:/+(?:\*/*)?)?``), so a bare ``~*`` /
        # ``~/foo*`` with the separator dropped would never fullmatch and the wipe
        # would fail open (Opus security-class: ``rm -fr ~/./*`` collapsed to
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
    # ``~`` is tilde-expanded by the shell ONLY as the first character of a word.
    # ``./~`` is a directory literally named ``~`` in the cwd (the shell does NOT
    # expand it), the canonical safe spelling for removing such a stray entry —
    # base's ``rm -rf ~.*`` literal never matched its leading ``./`` (Security
    # Scope regression). When no home prefix was present on the original operand
    # yet ``normpath`` collapsed a leading ``./`` to leave a bare ``~`` segment,
    # that ``~`` is a literal filename, not home: keep the collapsed form from
    # re-reading as a home target by restoring the dot anchor the shell saw.
    if not prefix and collapsed[:1] == "~":
        collapsed = "./" + collapsed
    return prefix + collapsed + glob_tail


#: The real expanded home path (``/home/<user>``), computed once. ``expanduser`` is
#: platform-native (a backslash path on Windows), so the separators are folded to
#: ``/`` and the result lowercased to match the lowercased operands the walk
#: produces — the same spelling ``_rm_normalize_dot_segments`` compares against.
_RM_EXPANDED_HOME_CACHE: "list[str] | None" = None


def _rm_expanded_home_path() -> str:
    """The lowercased, ``/``-separated real home directory, cached per process."""
    global _RM_EXPANDED_HOME_CACHE
    if _RM_EXPANDED_HOME_CACHE is None:
        try:
            resolved = _posixpath.normpath(_os.path.expanduser("~").replace("\\", "/")).lower()
        except (TypeError, ValueError):
            resolved = ""
        # A degenerate ``~`` resolving to ``/`` or ``.`` is NOT a usable home anchor
        # (it would misclassify every path), so store empty and the F3 check no-ops.
        _RM_EXPANDED_HOME_CACHE = [resolved if resolved not in ("", "/", ".") else ""]
    return _RM_EXPANDED_HOME_CACHE[0]


#: Mirror of the ``(?:/+(?:\*/*)?)?`` tail ``_RM_HOME_ITSELF_RE`` gives ``~``/
#: ``$HOME``: a run of trailing ``/`` with an OPTIONAL whole-remainder ``*`` glob
#: (and trailing slashes after it). Strips exactly that tail so an expanded-home
#: operand carrying it (``/home/<user>/``, ``/home/<user>/*``) compares equal to
#: the bare home path, while a real descendant (``/home/<user>/.cache``) keeps a
#: non-slash/non-glob segment and does NOT reduce to home.
_RM_HOME_ITSELF_TAIL_RE = re.compile(r"/+(?:\*/*)?$")


def _rm_strip_home_itself_tail(path: str) -> str:
    """``path`` with a single home-itself tail (trailing slashes / ``/*`` glob) removed."""
    return _RM_HOME_ITSELF_TAIL_RE.sub("", path, count=1)


#: Ceiling on the operands one brace word may expand to before the rest is dropped.
#: A real ``rm`` operand carries one or two small groups (``~/{,.cache}``); a word
#: built to be ``{a,b}{c,d}…`` to a dozen levels is a resource trap, so past the cap
#: the expansion simply stops contributing members — the bare word itself is still
#: classified, so a root/home member that fits under the cap is never missed.
_RM_BRACE_EXPANSION_CAP = 64


def _rm_expand_brace_members(operand: str) -> "list[str]":
    """The literal words bash's brace expansion makes of *operand*.

    ``rm -rf {~,/tmp/x}`` deletes ``~`` AND ``/tmp/x``; ``rm -rf /{,bin}`` deletes
    ``/`` AND ``/bin``; ``rm -rf $HOME/{,.cache}`` deletes ``$HOME`` AND
    ``$HOME/.cache`` — yet the exact root/home matchers see the single brace word
    ``{~,/tmp/x}`` / ``/{,bin}`` / ``$HOME/{,.cache}`` and miss the catastrophic
    member (GPT security-class: static brace expansion bypasses the floor). This
    performs the SAME alternation expansion the shell does so the member that IS
    root/home is classified on its own.

    Only alternation (``{a,b,c}``) is expanded — the one shape that can introduce a
    root/home spelling the bare word did not carry. A single-member brace
    (``{state}``, an awk ``{print $1}`` program, a ``{directory}`` format literal)
    has no top-level comma, is NOT brace-expansion to bash, and is left untouched so
    it is never read as a rooted operand. Sequences (``{1..9}``) cannot name root or
    home, so they are not expanded either. Nested groups expand with the outer, and
    the total member count is bounded by ``_RM_BRACE_EXPANSION_CAP`` so a crafted
    ``{a,b}{c,d}…`` word cannot explode the synchronous gate; past the cap the
    expansion stops (fail-safe: the bare word is still classified alongside these
    members, so a member within the cap is still caught).

    Returns ``[]`` when *operand* has no expandable brace, so the caller adds nothing
    and the existing bare-word classification is unchanged.
    """
    if "{" not in operand or "," not in operand:
        return []
    pairs = _brace_pairs(operand)
    if not pairs:
        return []

    def _expand(word: str) -> "list[str]":
        pr = _brace_pairs(word)
        if not pr:
            return [word]
        # Expand the FIRST top-level ``{…}`` with >1 alternative, then recurse on the
        # product; a single-alternative or sequence brace is left literal here and
        # re-tested only if a later group splits it.
        for open_idx in sorted(pr):
            close_idx = pr[open_idx]
            # top-level only: no enclosing brace spans this open index.
            if any(o < open_idx < pr[o] for o in pr if o != open_idx):
                continue
            body = word[open_idx + 1 : close_idx]
            parts = _split_brace_alternatives(body)
            if len(parts) <= 1:
                continue
            head, tail = word[:open_idx], word[close_idx + 1 :]
            out: list[str] = []
            for part in parts:
                for rest in _expand(head + part + tail):
                    out.append(rest)
                    if len(out) >= _RM_BRACE_EXPANSION_CAP:
                        return out
            return out
        return [word]

    members = _expand(operand)
    # Drop the identity (the bare word, when nothing split) — the caller already
    # classifies it — and cap defensively.
    return [m for m in members if m != operand][:_RM_BRACE_EXPANSION_CAP]


def _rm_walk_frames(text_lower: str, raw_text: "str | None") -> "list[tuple[str, list[str], bool]]":
    """``(source, norm_tokens, repaired)`` frames for the rm floor to classify.

    The first block is ``_shell_payload_walk(text_lower)`` — the ordinary
    lowercased walk. When *raw_text* is supplied AND carries an ANSI-C span, a
    SECOND block is the walk of that text with its ``$'…'`` spans decoded
    (case-preserved) then lowercased, so the width-sensitive ``\\U`` unicode
    escape resolves (``is_denied`` lowercases first, which would turn ``\\U`` into
    ``\\u`` and truncate the read at 4 digits).

    ``repaired`` marks the second block, and the caller uses it to classify a
    repaired frame ONLY through its decoded ``norm_tokens`` — never through a
    quote-stripping raw split. ``_decode_shell_quoted_literals`` re-quotes an
    ANSI-C value with ``shlex.quote`` (``$'\\"/\\"'`` -> ``'"/"'``), so a raw
    split of the repaired source would strip BOTH the added shell quotes and the
    LITERAL quotes the decode produced, reading the filename ``"/"`` as the root.
    The original-text walk (always included) is where a raw ``$HOME`` / ``~`` home
    operand is classified, so the repaired block loses no coverage by skipping it.
    Deduplicated: the ordinary command (no ``$'…'``, or the decode changes
    nothing) yields only the first block.
    """
    frames: "list[tuple[str, list[str], bool]]" = [
        (source, toks, False) for source, toks in _shell_payload_walk(text_lower)
    ]
    if raw_text is not None and "$'" in raw_text:
        repaired = _decode_shell_quoted_literals(raw_text).lower()
        if repaired != text_lower:
            frames.extend((source, toks, True) for source, toks in _shell_payload_walk(repaired))
    return frames


def _recursive_force_rm_targets(
    text_lower: str, *, raw_text: "str | None" = None
) -> "frozenset[str]":
    """Which catastrophic target(s) a top-level ``rm`` recursively force-deletes.

    Returns a subset of ``{"root", "home"}`` — ``root`` when a resolved operand
    IS the filesystem root, ``home`` when one IS the home directory (by ``~`` or
    the ``$HOME`` variable). Empty when the command is not a recursive-force
    ``rm`` against such an EXACT target; a descendant (``/tmp/x``,
    ``$HOME/.cache``) and a mere text mention both return empty.

    ``--no-preserve-root`` is a trigger on its own (meaningless without ``-rf``,
    and its whole purpose is to defeat the ``/`` guard); otherwise BOTH a
    recursive and a force flag must be present, in any position. A ``--``
    end-of-options marker stops flag parsing, so a token after it is an operand
    even if it is dash-shaped — matching GNU ``rm``.

    Every command FRAME is inspected — the top-level argv and the argv of every
    nested shell payload (``bash -c '…'``, ``sh -c``, ``$(…)``, a here-string, a
    chained segment). Each frame is re-split from its RAW source with
    ``_split_shell_words`` (quote-resolved but ENV-UNEXPANDED), so ``$HOME`` is
    classified by its written form rather than the home path a shlex expansion
    would produce (which would read as root). This is the same payload descent
    the self-protection floor uses, so a wrapper (``sudo rm -rf /``), a nested
    script (``bash -c 'rm -rf /'``) and a chain (``… && rm -rf /``) are all
    reached, while the frame's own ``_argv_programs`` scoping keeps a string that
    is merely an argument to another program (a ``git commit -m`` message, a
    ``grep`` pattern) from ever being read as an ``rm`` command.

    *raw_text* is the ORIGINAL-case command, when the caller has it. Bash's
    ANSI-C unicode escapes are CASE-SENSITIVE in width (``\\u`` is 4 hex digits,
    ``\\U`` is 8), so a ``$'\\U0000002d…'`` spelling decodes correctly only from
    case-preserved text -- the lowercased ``\\u`` truncates at 4 digits and reads
    the wrong character. When *raw_text* is supplied its ANSI-C spans are decoded
    (case-preserved) then lowercased and walked as an ADDITIONAL frame source, so
    the ``\\U`` spelling is caught the same as its ``\\u`` twin.
    """
    # Cheap necessary condition. A plain ``rm`` invocation contains the literal
    # ``rm``; an OBFUSCATED one (``"r\<nl>m"``, ``$'r\555'``) does not — its ``rm``
    # is built by escape/quote/substitution machinery whose decoded output can be
    # any character, so the only sound cheap gate is "contains ``rm`` OR contains
    # such machinery". When neither is present the walk cannot yield an ``rm``.
    if "rm" not in text_lower and not _RM_OBFUSCATION_MACHINERY_RE.search(text_lower):
        return frozenset()
    found: set[str] = set()
    for source, norm_tokens, repaired in _rm_walk_frames(text_lower, raw_text):
        # The DECODED view (payload walk's own tokens) is always classified: it
        # resolves ANSI-C / unicode escapes and env expansion, so ``rm -rf $'/'``
        # / ``$'\u002f'`` is caught as the exact root, and a ``$'"/"'`` filename's
        # LITERAL quotes stay in the token so it is NOT misread as root.
        found |= _rm_targets_in_argv(norm_tokens, strip_quotes=False)
        # Base-literal pin: base denied the contiguous unquoted text ``rm -rf /``
        # / ``rm -rf ~`` wherever it appeared, including behind a data consumer
        # (``echo rm -rf /``). The structural exemption must not walk that back, so
        # the literal is denied here regardless of the exemption — matched on the
        # frame SOURCE text, outside quotes, so a quoted search-verb pattern or a
        # ``git commit -m`` message stays exonerated.
        found |= _rm_base_literal_bare_tokens(source)
        # A REPAIRED frame (from the ANSI-C-decoded copy) is classified ONLY via
        # its decoded tokens above. Its raw source has been through
        # ``_decode_shell_quoted_literals`` + ``shlex.quote``, so a quote-stripping
        # raw split would peel the shell quotes shlex added AND the LITERAL quotes
        # the decode produced (``$'"/"'`` -> ``'"/"'`` -> ``/``), reading a
        # filename as the root. The raw-spelling ``$HOME`` / ``~`` classification
        # it would otherwise add is already covered by the ORIGINAL-text frame.
        if repaired:
            if {"root", "home"} <= found:
                break
            continue
        # Non-repaired frame: also classify the RAW split, which keeps ``$HOME`` /
        # ``~`` unexpanded so home is classified by its written spelling. Surrounding
        # SHELL quotes are stripped only here (``"$HOME"`` -> ``$HOME``).
        found |= _rm_targets_in_argv(_split_shell_words(source), strip_quotes=True)
        # A command-substitution OPERAND resolves to its OUTPUT, which the raw
        # split classifies verbatim (the unresolved ``$(printf /)`` spelling), so
        # ``rm -rf --no-preserve-root "$(printf /)"`` keeps ``$(printf /)`` as the
        # operand and the root wipe is missed (GPT security-class). Resolve each
        # ``$(…)`` / backtick operand to the word it STATICALLY expands to (the
        # same narrow ``echo``/``printf`` resolver the sibling argv floor uses) and
        # re-classify, so the resolved ``/`` reaches the operand matchers. A
        # dynamic generator resolves to a non-matching sentinel, so this can only
        # ADD coverage, never conjure a target.
        resolved_subst = _rm_resolve_substitution_operands(_split_shell_words(source))
        if resolved_subst is not None:
            found |= _rm_targets_in_argv(resolved_subst, strip_quotes=True)
        # Execution-substitution bodies the shared walk does not surface as their
        # own frames: a ``$(…)`` / backtick command substitution nested INSIDE a
        # double-quoted argument (the enclosing quote makes the closing ``)``
        # quote-inactive, so ``_substitution_bodies`` over-reads it), a bash 5.3
        # ``${ …;}`` funsub, and a bare ``(…)`` subshell. Each EXECUTES the command
        # it carries, so an ``rm`` inside one is a real wipe even when the
        # substitution's OUTPUT is then consumed as data (``grep -rn "$(rm -rf /)"
        # test/`` runs the wipe before grep starts). Each extracted body is
        # classified as its own argv — flag order, the exact-operand test and the
        # glob shape all apply inside it.
        for body in _rm_exec_substitution_bodies(source):
            found |= _rm_targets_in_argv(_split_shell_words(body), strip_quotes=True)
        # ``<producer> | xargs [xargs-opts] rm [flags]`` APPENDS the producer's
        # stdin words to ``rm``'s own argv, so the destructive operand lives in NO
        # token of the ``rm`` command and the floor sees ``rm -rf`` with no target
        # (GPT 5.6 security-class, UPHOLD-FENCED: ``echo "$HOME" | xargs rm -rf``
        # wipes home). Rebuild ``rm <flags> <statically-recoverable stdin
        # operands>`` and classify it ITSELF-only (``base_descendant_pin=False``):
        # the reconstruction is SYNTHETIC — base never saw this as contiguous
        # ``rm -rf /`` text — so, exactly like the find ``{}``-resolved span and the
        # command-string wrappers, it denies the catastrophic root/home dir itself
        # while a descendant (``echo /tmp/x | xargs rm -rf``) stays allowed as base
        # allowed it. The stdin operands are recovered only from a literal
        # ``echo``/``printf`` producer; a dynamic producer yields nothing.
        xargs_rm = _rm_xargs_reconstructed_argv(_split_shell_words(source))
        if xargs_rm is not None:
            found |= _rm_targets_in_argv(xargs_rm, strip_quotes=True, base_descendant_pin=False)
        if {"root", "home"} <= found:
            break
    return frozenset(found)


#: The BASE-LITERAL spellings of the two ``rm`` deny rules, as whole-line
#: patterns: base ``main``'s regex was ``rm -rf /.*`` / ``rm -rf ~.*`` — the
#: contiguous text ``rm -rf `` immediately followed by ``/`` or ``~`` and THEN
#: ANY tail (the root/home ITSELF or any DESCENDANT: ``rm -rf /etc``, ``rm -rf
#: /tmp/foo``, ``rm -rf ~/.ssh`` were all denied). They serve two roles:
#:
#: * the FAIL-CLOSED fallback when the structural tokenizer RAISES — the floor
#:   must still deny the one spelling base denied with NO tokenizer (First
#:   Principles items 5+6); and
#: * the base-contiguous pin (:func:`_rm_base_literal_bare_tokens`) that restores
#:   base's DESCENDANT coverage the structural ITSELF matchers deliberately
#:   leave to this text pin — exactly reproducing base's substring match without
#:   the token-adjacency guesswork a structural flag check would need.
#:
#: They recover ONLY base's exact ``rm -rf `` spelling, not the widened flag
#: coverage (``-fr``, split ``-r -f``, long options) — those are the spellings
#: base's literal never contained, so a descendant in a widened spelling (``rm
#: -fr /tmp/x``) is NOT matched here and stays allowed (Security Scope ruling),
#: while the catastrophic root/home ITSELF in a widened spelling is caught by the
#: structural ITSELF matchers regardless. The target is ``/`` / ``~`` immediately
#: after ``rm -rf ``, so ``rm -rf -- /tmp/x`` (``--`` between) and ``rm /tmp/x
#: -rf`` (flags after path) and ``rm -rf $HOME/x`` (``$HOME`` ≠ ``/`` / ``~``) do
#: NOT match — none contained base's contiguous ``rm -rf /`` / ``rm -rf ~`` text.
_RM_ROOT_LITERAL_RE = re.compile(r"rm -rf /\S*")
_RM_HOME_LITERAL_RE = re.compile(r"rm -rf ~\S*")


def _recursive_force_rm_targets_fail_closed(text_lower: str) -> "frozenset[str]":
    """Base-literal ``rm`` targets, for when the structural tokenizer RAISED.

    Applies the pre-widening bare-literal check (``rm -rf /`` / ``rm -rf ~`` as a
    substring of the lowercased command) with NO tokenization, so the floor
    denies the catastrophic literal even when :func:`_recursive_force_rm_targets`
    could not run. This is deliberately the SAME shape ``main``'s deny rule had
    before this change, so the fail path is no weaker than base was.
    """
    found: set[str] = set()
    if _RM_ROOT_LITERAL_RE.search(text_lower):
        found.add("root")
    if _RM_HOME_LITERAL_RE.search(text_lower):
        found.add("home")
    return frozenset(found)


def _rm_quoted_positions(source: str) -> "list[bool]":
    """One forward pass marking each index as inside a single/double-quoted span.

    One forward pass is the whole scan: a span-state lookup for every regex match
    reuses this single mask instead of rescanning from index 0 per match, so a
    command of ``"'rm -rf /' " * N`` stays linear rather than O(N**2) on the
    SYNCHRONOUS gate. The escape-skip (``\\'`` / ``\\"`` outside single quotes is a
    literal character, not a span toggle) applies to BOTH quote contexts, so an
    odd number of escaped double quotes does not leave double-quote state wrong
    and drop base's ``rm -rf /etc`` coverage after it. ``mask[i]`` is True when
    index *i* is inside either quote span.
    """
    mask = [False] * len(source)
    in_single = False
    in_double = False
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "\\" and not in_single:
            # bash: a backslash outside single quotes escapes the next character,
            # so neither it nor the escaped char toggles a span.
            mask[i] = in_single or in_double
            if i + 1 < n:
                mask[i + 1] = in_single or in_double
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        mask[i] = in_single or in_double
        i += 1
    return mask


def _rm_base_literal_bare_tokens(source: str) -> "frozenset[str]":
    """Targets of the EXACT base literal ``rm -rf /`` / ``rm -rf ~`` appearing as
    CONTIGUOUS, UNQUOTED text — the one spelling base ``main``'s whole-line regex
    denied that the structural data-consumer exemption must not walk back.

    base denied ANY command whose text contained the contiguous substring
    ``rm -rf /`` / ``rm -rf ~``, including ``echo rm -rf /`` (a mention behind a
    data consumer). The PR narrows that to stop refusing the literal QUOTED inside
    a search-verb pattern or a ``git commit -m`` message — so the pin matches the
    literal only OUTSIDE single/double quotes (First Principles: the exemption
    must not walk back the base pin, but the quoted-mention FP fix stands). It is
    text-contiguous exactly as base's regex was, so a separator between the words
    (``echo rm; -rf /``, ``echo rm`` newline ``-rf /``) is not base's literal and
    is not matched; a glob tail (``rm -rf /*``) is a descendant base matched too.
    """
    found: set[str] = set()
    quoted = _rm_quoted_positions(source)
    for pattern, target in ((_RM_ROOT_LITERAL_RE, "root"), (_RM_HOME_LITERAL_RE, "home")):
        for m in pattern.finditer(source):
            if not quoted[m.start()]:
                found.add(target)
                break
    return frozenset(found)


#: A bash 5.3 command funsub: ``${ COMMANDS; }`` / ``${|COMMANDS; }`` — runs the
#: commands in the current shell (unlike ``$(…)``, no subshell). The body runs to
#: the matching ``}``; the leading ``|`` (value-returning form) and a trailing
#: ``;`` are stripped when the body is classified.
_FUNSUB_OPEN_RE = re.compile(r"\$\{[ \t\n|]")


def _rm_single_quoted_positions(source: str) -> "list[bool]":
    """One forward pass marking each index as inside a SINGLE-quoted span.

    A single quote in bash suppresses every expansion, so a ``${`` / ``$(`` /
    backtick inside one is literal text, not a construct. A single quote INSIDE a
    double-quoted span is itself a literal apostrophe and opens no span, so BOTH
    contexts are tracked: a ``'`` toggles single-quote state only when NOT inside
    double quotes, and a ``"`` toggles double-quote state only when NOT inside
    single quotes; a backslash outside single quotes escapes the next character.
    Replaces the per-match ``_index_in_single_quote`` rescan that scanned from 0
    on every regex match — O(N**2) on the synchronous gate (Opus security-class,
    ReDoS). ``mask[i]`` is True when index *i* is inside a single-quoted span.
    """
    mask = [False] * len(source)
    in_single = False
    in_double = False
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "\\" and not in_single:
            mask[i] = in_single
            if i + 1 < n:
                mask[i + 1] = in_single
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        mask[i] = in_single
        i += 1
    return mask


def _rm_matching_close(source: str, start: int, opener: str, closer: str) -> int:
    """Index of the *closer* that balances the *opener* already consumed, QUOTE-AWARE.

     Scans from *start* tracking nesting of ``opener``/``closer`` and both quote
     contexts, so an ``opener``/``closer`` INSIDE a single- or double-quoted span
     (or backslash-escaped) does not change the depth — ``$(echo "a)b"; rm -rf /)``
     keeps its real close, where a quote-blind paren count would stop at the ``)``
     inside ``"a)b"`` and truncate the body before the wipe (GPT security-class,
    ). Returns the index of the balancing ``closer``, or ``len(source)``
     when the construct is unterminated (the caller then takes the remainder,
     which only ever feeds the classifier MORE text — the fail-closed direction).
    """
    depth = 1
    j = start
    n = len(source)
    in_single = in_double = False
    while j < n:
        ch = source[j]
        if ch == "\\" and not in_single:
            j += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    return n


def _rm_matching_backtick(source: str, start: int) -> int:
    """Index of the backtick closing the one already consumed, QUOTE-AWARE.

    A backtick inside a SINGLE-quoted span is literal and does not close the
    substitution; inside double quotes a backtick DOES still delimit a command
    substitution, so only single-quote state suppresses it. A backslash escapes
    the next character outside single quotes. Returns the closing backtick's
    index, or ``len(source)`` when unterminated (caller takes the remainder).
    """
    j = start
    n = len(source)
    in_single = in_double = False
    while j < n:
        ch = source[j]
        if ch == "\\" and not in_single:
            j += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "`" and not in_single:
            return j
        j += 1
    return n


def _rm_exec_substitution_bodies(source: str) -> "list[str]":
    """Inner command lines of the execution-substitutions the shared walk misses.

    Three shapes, all of which RUN the command they carry (so an ``rm`` inside is
    executed, not data), and none of which the payload walk surfaces as a frame
    of its own:

    * a ``$(…)`` command substitution or a backtick one nested INSIDE a
      double-quoted word — ``grep -rn "$(rm -rf /)" test/``. The enclosing
      double quote makes the closing ``)`` quote-INACTIVE to the quote-aware
      body scan, so ``_substitution_bodies`` reads past it; but ``$(…)`` executes
      inside double quotes and unquoted, so the body is extracted here.
    * a bash 5.3 ``${ …;}`` funsub — ``grep x ${ rm -rf /;}`` — which the walk
      does not recognise as a substitution at all.
    * a bare ``(…)`` SUBSHELL — ``(rm -rf /)`` — which runs its body in a child
      shell. When it is glued (``(rm``) the tokenizer keeps the ``(`` on the
      program word, so ``rm`` never reaches program position; extracting the
      parenthesised body and classifying it as its own argv recovers it. (The
      spaced form ``( rm -rf / )`` already tokenizes cleanly, so this only ADDS
      the glued spelling.)

    SINGLE-QUOTE AWARE, and that is load-bearing: inside single quotes ``$(``,
    a backtick, ``(`` and ``${`` are all LITERAL — bash executes none of them —
    so ``git commit -m 'see `rm -rf /` warning'`` and ``grep '`rm -rf /`' src/``
    run no ``rm`` and must NOT be extracted (that is exactly the text false
    positive the Security Scope lane rejects). ``$(…)`` and backticks execute
    inside DOUBLE quotes, so they ARE extracted there; a bare ``(…)`` subshell,
    however, is a LITERAL inside double quotes (bash runs no subshell), so a
    parenthesised mention in a double-quoted commit message is NOT extracted
    (Security Scope ruling). Double-quote state is therefore tracked,
    for the bare-``(`` distinction.

    Returned bodies are command lines; the caller classifies each as its own
    argv. Over-extraction (a body that is not really an ``rm``) yields nothing,
    and an unbalanced/unterminated construct yields the remainder, which only ever
    feeds the classifier MORE text — the fail-closed direction.
    """
    bodies: list[str] = []
    n = len(source)
    # ``$(…)`` command substitutions, bare ``(…)`` subshells, and backtick
    # substitutions — skipped when inside a SINGLE-quoted span, where they are
    # literal. A ``(`` preceded by ``$`` is the command-sub opener; any other
    # ``(`` opens a subshell (both matched by the same paren walk).
    #
    # Both quote contexts are tracked: a ``'`` toggles single-quote state only
    # when NOT inside double quotes (an apostrophe in ``"it's $(rm -rf /)"`` is a
    # literal, and must not suppress the executing ``$(…)`` that follows — GPT
    # security-class), and a ``"`` toggles double-quote state only when
    # NOT inside single quotes. A backslash outside single quotes escapes the
    # next character.
    i = 0
    in_single = False
    in_double = False
    while i < n:
        if len(bodies) >= _RM_SUBST_BODY_CAP:
            break
        ch = source[i]
        if ch == "\\" and not in_single:
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
        if in_single:
            i += 1
            continue
        if ch == "(":
            # A bare ``(`` SUBSHELL inside DOUBLE quotes is a LITERAL character —
            # bash runs no subshell there (unlike ``$(…)`` and backticks, which
            # DO execute in double quotes) — so a parenthesised mention in a
            # double-quoted commit message or log string (``git commit -m "remove
            # the (rm -rf /) step"``) must NOT be extracted (Security Scope ruling
            # the ruling). A ``$(`` command substitution is still extracted in
            # double quotes: it is matched by this same paren walk, and the ``$``
            # just before marks it, so only a bare ``(`` (no leading ``$``) inside
            # double quotes is skipped.
            dollar_sub = i > 0 and source[i - 1] == "$"
            if in_double and not dollar_sub:
                i += 1
                continue
            open_at = i + 1  # body starts just past the '('
            j = _rm_matching_close(source, open_at, "(", ")")
            bodies.append(source[open_at:j] if j < n else source[open_at:])
            i = j + 1
            continue
        if ch == "`":
            j = _rm_matching_backtick(source, i + 1)
            bodies.append(source[i + 1 : j] if j < n else source[i + 1 :])
            i = j + 1 if j < n else n
            continue
        i += 1
    # ``${ …;}`` / ``${|…;}`` funsubs, matched to their closing brace — also only
    # OUTSIDE a single-quoted span (a ``${`` in single quotes is literal). One
    # quote-state pass for all matches, not a rescan from 0 per match (Opus
    # security-class, ReDoS).
    single_quoted = _rm_single_quoted_positions(source)
    for match in _FUNSUB_OPEN_RE.finditer(source):
        # Cap the number of funsub bodies extracted. An unclosed ``${ `` makes the
        # brace walk run to the end of the source and yield the whole remainder as
        # a body, which is then re-tokenized — so ``"echo " + "${ " * 1600`` is
        # quadratic on the synchronous gate (Opus security-class). A real command
        # carries a handful of funsubs; past the cap, stop extracting — a literal
        # ``rm -rf /`` in a later body is still caught by the whole-text deny-net
        # regex, so the cap cannot fail open.
        if len(bodies) >= _RM_SUBST_BODY_CAP:
            break
        if single_quoted[match.start()]:
            continue
        depth = 1
        j = match.end()
        while j < n and depth:
            if source[j] == "{":
                depth += 1
            elif source[j] == "}":
                depth -= 1
            j += 1
        body = source[match.end() : j - 1] if depth == 0 else source[match.end() :]
        # Strip the value-returning ``|`` lead and a trailing statement ``;``.
        bodies.append(body.lstrip("|").rstrip().rstrip(";"))
    return bodies


#: A whole-token command substitution: ``$(…)`` or a backtick pair spanning the
#: entire operand word (after surrounding shell quotes are peeled). The output
#: of such a word becomes the operand ``rm`` receives.
_RM_WHOLE_SUBSTITUTION_RE = re.compile(r"\A\$\((?P<body>.*)\)\Z|\A`(?P<btck>.*)`\Z", re.DOTALL)


def _rm_resolve_substitution_operands(tokens: "list[str]") -> "list[str] | None":
    """``tokens`` with each command-substitution OPERAND replaced by its STATIC
    output, or ``None`` when none resolves (so the caller skips a redundant
    re-classification).

    ``rm -rf --no-preserve-root "$(printf /)"`` reaches ``rm`` with the operand
    ``/`` — the substitution's output — but the raw split keeps the unresolved
    ``$(printf /)`` spelling, so the operand matchers never see ``/`` (GPT
    security-class). Resolve a whole-token ``$(…)`` / backtick operand to the word
    it statically expands to, reusing the sibling argv floor's narrow
    ``echo``/``printf`` resolver (literal first operand only). A dynamic generator
    resolves to the ``"\\x00"`` sentinel, which no operand matcher accepts, so
    this can only ADD coverage, never conjure a target. Flag tokens and
    non-substitution operands are left untouched.
    """
    from .argv_floor import _static_substitution_output

    resolved: list[str] = []
    changed = False
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        peeled = _rm_strip_surrounding_quotes(tok)
        m = _RM_WHOLE_SUBSTITUTION_RE.match(peeled)
        if m is not None:
            body = m.group("body")
            if body is None:
                body = m.group("btck") or ""
            output = _static_substitution_output(body)
            if output and output != "\x00":
                resolved.append(output)
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
                inner = _rm_strip_surrounding_quotes(joined)
                mm = _RM_WHOLE_SUBSTITUTION_RE.match(inner)
                if mm is not None:
                    body = mm.group("body") or ""
                    output = _static_substitution_output(body)
                    if output and output != "\x00":
                        resolved.append(output)
                        changed = True
                        i = j
                        continue
        resolved.append(tok)
        i += 1
    return resolved if changed else None


#: ``xargs`` options that take a SEPARATE operand token, so the command-name scan
#: skips BOTH the flag and its value (``xargs -n 1 rm -rf`` -> the command is
#: ``rm -rf``). Short forms (``-I`` replace-str as a SEPARATE token ``-I R``,
#: ``-n`` max-args, ``-P`` max-procs, ``-d`` delimiter, ``-s`` max-chars,
#: ``-E``/``-e`` eof-str, ``-L``/``-l`` max-lines, ``-a`` arg-file) and their GNU
#: long spellings. A value GLUED to a short option (``-n1``, and the ATTACHED
#: replace-str forms ``-I{}``/``-i{}`` where ``{}`` is the replace-str, not a
#: separate operand) is one ``-``-token already skipped by the leading-option
#: loop, so only the separate-token forms need listing -- an attached ``-i{}``
#: must NOT be here or the scan would wrongly skip the following command word.
_RM_XARGS_OPTIONS_WITH_OPERAND: frozenset[str] = frozenset(
    {
        "-i",
        "-n",
        "--max-args",
        "-p",
        "--max-procs",
        "-d",
        "--delimiter",
        "-s",
        "--max-chars",
        "-e",
        "--eof",
        "-l",
        "--max-lines",
        "-a",
        "--arg-file",
        "--replace",
    }
)
#: Literal-emitting producers whose words are the stdin ``xargs`` reads. Only
#: these are resolved — a dynamic producer's output is not statically known, so
#: it contributes no operand (the reconstruction can only ADD coverage).
_RM_XARGS_LITERAL_PRODUCERS: frozenset[str] = frozenset({"echo", "printf"})


def _rm_split_glued_pipes(tokens: "list[str]") -> "list[str]":
    """Split tokens on an EMBEDDED single ``|`` so a glued pipeline tokenizes like
    a spaced one.

    The raw shlex split keeps ``$HOME|xargs`` or ``echo/|xargs`` as one token
    because ``|`` is not whitespace, but a shell ends the producer at the ``|``
    regardless of spacing. Each lone ``|`` inside a token is promoted to its own
    token (``['$HOME', '|', 'xargs']``) so the pipeline walk finds ``xargs`` as a
    program word. ``||`` (logical OR) and ``|&`` (pipe-both) are NOT split — only a
    lone ``|`` is a simple-pipe boundary; a token carrying one of those is left
    whole. A token with no ``|`` is passed through unchanged.
    """
    out: list[str] = []
    for tok in tokens:
        if "|" not in tok or "||" in tok or "|&" in tok:
            out.append(tok)
            continue
        # Split on each lone ``|``, keeping the pipe as its own token and dropping
        # the empty pieces a leading/trailing/adjacent pipe produces.
        parts = tok.split("|")
        for pi, part in enumerate(parts):
            if part:
                out.append(part)
            if pi < len(parts) - 1:
                out.append("|")
    return out


def _rm_xargs_reconstructed_argv(tokens: "list[str]") -> "list[str] | None":
    """Rebuild ``rm <flags> <stdin operands>`` for ``<producer> | xargs … rm …``.

    ``xargs`` APPENDS the words it reads on stdin to the command given as its own
    arguments, so ``echo "$HOME" | xargs rm -rf`` executes ``rm -rf "$HOME"`` —
    the destructive operand is in NO token of the ``rm`` command and the floor
    sees ``rm -rf`` with no target (GPT 5.6 security-class, UPHOLD-FENCED). This
    rebuilds the effective ``rm`` argv: the ``rm`` command and ITS flags (xargs'
    own options are skipped) followed by the literal words a leading
    ``echo``/``printf`` producer emits on stdin. Returns ``None`` when the shape
    is not a ``producer | xargs … rm`` pipeline or the producer is dynamic (its
    stdin words are not statically known), so the caller skips a dead
    re-classification. The CALLER classifies the result ITSELF-only, so a
    descendant operand (``echo /tmp/x | xargs rm -rf``) stays allowed.
    """
    # Split any token with an EMBEDDED pipe (``$HOME|xargs``, ``echo/|xargs``)
    # into its parts around a lone ``|``, so a glued pipeline is walked the same
    # as a spaced one. A shell ends the producer at the ``|`` regardless of
    # surrounding whitespace, but the raw split keeps ``$HOME|xargs`` as one token
    # — the pipe is then found by ``"|" in tk`` yet ``xargs`` is not its own token,
    # so the reconstruction never fires and the home wipe passes (GPT 5.6
    # security-class, UPHOLD-FENCED). ``||`` / ``|&`` are left intact (not a simple
    # producer pipe); only a lone ``|`` is a split point.
    tokens = _rm_split_glued_pipes(tokens)
    pipe = next((idx for idx, tk in enumerate(tokens) if tk == "|"), -1)
    if pipe <= 0:
        return None
    n = len(tokens)
    xargs_at = next(
        (idx for idx in range(pipe + 1, n) if _program_basename(tokens[idx]) == "xargs"),
        -1,
    )
    if xargs_at == -1:
        return None
    # Skip xargs' OWN leading options (and the operand a value-taking one eats).
    k = xargs_at + 1
    while k < n and tokens[k].startswith("-"):
        opt = tokens[k].lower()
        k += 1
        if opt in _RM_XARGS_OPTIONS_WITH_OPERAND and k < n:
            k += 1
    # The command xargs runs must be ``rm`` (possibly a dispatcher/wrapper is out
    # of scope here — xargs' direct command). Keep ``rm`` and the rest of its argv
    # (its flags), stopping at a shell operator that ends the command.
    if k >= n or _program_basename(tokens[k]) != "rm":
        return None
    command: list[str] = []
    while k < n and not _ends_argv(tokens[k]):
        command.append(tokens[k])
        k += 1
    # The producer's literal stdin words: only a leading echo/printf, non-option
    # operands before the pipe. A dynamic producer contributes nothing.
    producer = tokens[:pipe]
    if not producer or _program_basename(producer[0]) not in _RM_XARGS_LITERAL_PRODUCERS:
        return None
    piped = [tk for tk in producer[1:] if not tk.startswith("-")]
    if not piped:
        return None
    return command + piped


#: Multi-call binaries that DISPATCH to the applet named by their first
#: (non-flag) argument: ``busybox rm -rf /`` runs the ``rm`` applet, and
#: ``toybox``/``busybox.exe`` do the same. Here ``rm`` is the dispatcher's first
#: ARGUMENT, not the line's program word and not behind an exec wrapper, so the
#: plain program-position scan and the wrapper set both miss it (GPT
#: security-class). Unlike an exec wrapper, ONLY the first argument is
#: the applet — ``busybox echo rm -rf /`` runs ``echo``, not ``rm`` — so the
#: dispatch is matched positionally, not by wrapper membership.
_RM_APPLET_DISPATCHERS: frozenset[str] = frozenset({"busybox", "toybox"})


def _rm_deescape_unquoted_backslashes(text: str) -> str:
    """Remove backslash escapes as an UNQUOTED inner shell would, so an escaped
    program name reforms.

    A ``bash -c $"\\r\\m -rf /"`` payload reaches the inner shell as the script
    ``\\r\\m -rf /``; unquoted, bash drops each backslash before an ordinary
    character, so ``\\r\\m`` becomes the word ``rm``. The outer walk's
    ``_decode_printf_escapes`` instead maps ``\\r`` to whitespace and drops the
    ``r``, so the ``rm`` never reforms and the wipe was missed (Item 4).

    Backslashes INSIDE single quotes are literal and are left untouched; a
    backslash outside single quotes removes itself and keeps the next character
    (``\\n`` -> ``n``, matching the inner shell's own unquoted lexing rather than
    the C-escape meaning — the shell does not turn an unquoted ``\\n`` into a
    newline). A trailing backslash is dropped.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_single = False
    while i < n:
        ch = text[i]
        if ch == "'":
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and not in_single and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


#: ``find``'s two flags that RUN their trailing command span as a real argv
#: (``-execdir`` differs from ``-exec`` only in the working directory), so an
#: ``rm`` inside that span is executed, not data. ``-ok``/``-okdir`` prompt first
#: but still execute, so they are included — the prompt is not a control an agent
#: session can rely on.
_FIND_EXEC_FLAGS: frozenset[str] = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: The two tokens that TERMINATE a ``find -exec`` command span: ``;`` (run once
#: per match) and ``+`` (batch).
_FIND_EXEC_TERMINATORS: frozenset[str] = frozenset({";", "+"})
#: ``find`` operators that START the expression proper, ending the path-root
#: list — a grouping/negation token or the ``,`` separator. A leading OPTION
#: (``-L``/``-maxdepth``/``-type``…) does NOT end it: GNU find is lenient about
#: options appearing before the paths, so those are consumed first (see
#: ``_find_search_roots``) rather than hiding the real search root behind them.
_FIND_EXPRESSION_START = frozenset({"(", ")", "!", ","})
#: ``find`` expression predicates that prove the START ROOT is excluded from the
#: ``{}`` match set: a NAME / PATH pattern test. ``find $HOME -exec rm -rf {}``
#: with no such predicate expands ``{}`` to the whole tree (``$HOME`` itself), so
#: the home target stands; a specific NAME / PATH test (``-name __pycache__``)
#: proves ``$HOME`` is not in the set (the home/root dir is not named
#: ``__pycache__`` and its path does not match a specific ``-path`` glob), so that
#: case is a predicate-scoped purge of ordinary developer work and ``{}`` is
#: exempt. A ``-type`` / ``-prune`` / ``-perm`` / ownership / time predicate is
#: NOT here: those can match the start root directory itself, so they do NOT
#: exempt ``{}`` (GPT security-class: ``find "$HOME" -type d`` still wipes home),
#: and a catch-all pattern (``-name '*'``) matches the root too.
_FIND_NAME_PATTERN_PREDICATES: frozenset[str] = frozenset(
    {
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-wholename",
        "-iwholename",
        "-regex",
        "-iregex",
        "-lname",
        "-ilname",
    }
)
#: A ``-name`` / ``-path`` pattern argument that matches EVERYTHING — a bare glob
#: or regex catch-all — does NOT exclude the start root, so it must not exempt
#: ``{}``. Only a SPECIFIC pattern proves the root is out of the match set.
_FIND_CATCH_ALL_PATTERNS: frozenset[str] = frozenset({"*", "**", ".*", "*/*", "/*"})
#: find operators that make a root-excluding ``-name``/``-path`` predicate NOT
#: gate the ``-exec`` route, so their presence anywhere in the expression voids
#: the ``{}`` exemption (fail closed). Beyond the OR operators: NEGATION
#: (``!`` / ``-not``) INVERTS a predicate, so ``find "$HOME" ! -name __never__
#: -exec rm -rf {}`` runs ``-exec`` on everything that is NOT ``__never__`` —
#: ``$HOME`` itself (GPT security-class, irreversible home wipe) — the opposite
#: of what the positional pattern read suggests; and the COMMA operator (``,``)
#: sequences independent expressions whose later ``-exec`` is not gated by an
#: earlier branch's predicate at all.
#:
#: These three ALWAYS void: a negation or a comma cannot be reasoned about as a
#: root-excluding gate without a full expression parser, so fail closed.
_FIND_HARD_VOIDING_OPERATORS: frozenset[str] = frozenset({"!", "-not", ","})
#: The DISJUNCTION operators. A disjunction does NOT void by its mere presence:
#: ``find ~ \( -name '*.pyc' -o -name '__pycache__' \) -exec rm -rf {} +`` — the
#: stock bytecode-cache purge — is a disjunction of TWO root-excluding name
#: patterns, both gating ``-exec``, so ``~`` itself is never a match and the
#: exemption must hold (Security Scope confirmed regression). A disjunction voids
#: ONLY when a branch reaching ``-exec`` is NOT a root-excluding name/path
#: predicate — e.g. ``find "$HOME" -name __never__ -o -exec rm -rf {}``, where the
#: ``-o`` connects a predicate directly to ``-exec`` so the ``-exec`` route runs
#: when the name is FALSE (``$HOME`` included). That case is caught by the
#: all-predicates-root-excluding requirement below, since a bare ``-exec`` disjunct
#: contributes no root-excluding pattern.
_FIND_DISJUNCTION_OPERATORS: frozenset[str] = frozenset({"-o", "-or"})
#: Backwards-compatible union, retained for any caller testing membership of the
#: whole voiding class.
_FIND_EXEMPTION_VOIDING_OPERATORS: frozenset[str] = (
    _FIND_HARD_VOIDING_OPERATORS | _FIND_DISJUNCTION_OPERATORS
)
#: find options that CONSUME the following token as their value, so the operand
#: walk must skip BOTH — otherwise ``find -maxdepth 3 "$HOME" -exec …`` would
#: read ``3`` as a search root and miss ``$HOME``. Covers the common
#: value-taking positional-option predicates (``-maxdepth``/``-mindepth``/
#: ``-type``/``-name``/…). An option NOT listed here is assumed flag-only.
#: ``-d``/``-o`` are DELIBERATELY omitted: BSD/macOS ``find -d`` (depth-first)
#: and the ``-o`` OR operator take NO value, so listing them made ``find -d /
#: -exec rm -rf {}`` swallow the root ``/`` as ``-d``'s operand and fail OPEN
#: (Opus security-class); the lowercased global options ``-D``/``-O`` they were
#: meant to cover then miss, which only over-reads a search root — the
#: fail-CLOSED side.
_FIND_OPTIONS_WITH_OPERAND = frozenset(
    {
        "-maxdepth",
        "-mindepth",
        "-type",
        "-xtype",
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-regex",
        "-iregex",
        "-perm",
        "-user",
        "-group",
        "-uid",
        "-gid",
        "-size",
        "-newer",
        "-mtime",
        "-atime",
        "-ctime",
        "-mmin",
        "-amin",
        "-cmin",
    }
)


def _find_search_roots(tokens: "list[str]", find_at: int) -> "list[str]":
    """The leading path ROOTS of the ``find`` command whose program word is at
    *find_at* — the operands ``{}`` expands to.

    ``find [option …] [root …] [expression]``: GNU find accepts options before
    the paths (``find -maxdepth 3 $HOME …``), so leading OPTIONS are consumed
    first — each ``-flag`` and, when it takes a value (``_FIND_OPTIONS_WITH_OPERAND``),
    its operand — before the path roots are collected. Without that, a leading
    ``-maxdepth 3`` hid ``$HOME`` behind it and ``{}`` resolved to nothing (GPT
    security-class). Collection then runs from the first non-option token
    to the next option / grouping / ``,`` separator. ``find $HOME -exec …`` has
    root ``$HOME``; ``find -maxdepth 3 / -exec …`` has root ``/``; ``find . -exec
    …`` has root ``.`` (relative, not catastrophic).
    """
    n = len(tokens)
    j = find_at + 1
    # Consume leading options (and operand-taking option values).
    while j < n:
        tok = tokens[j]
        if tok.startswith("-") and tok not in _FIND_EXEC_FLAGS:
            if tok in _FIND_OPTIONS_WITH_OPERAND and j + 1 < n:
                j += 2  # skip the option AND its operand
            else:
                j += 1  # flag-only option
            continue
        break
    # Collect the path roots up to the first expression / option token.
    roots: list[str] = []
    while j < n:
        tok = tokens[j]
        if not tok or tok.startswith("-") or tok in _FIND_EXPRESSION_START:
            break
        roots.append(tok)
        j += 1
    return roots


#: ``find`` predicates that test a candidate's WHOLE PATH (``-path``/``-regex``
#: and their variants) versus those that test only its BASENAME (``-name``). A
#: predicate excludes the start root only when its pattern cannot match that
#: root; a whole-path test is checked against the root path, a basename test
#: against the root's last path segment.
_FIND_PATH_PATTERN_PREDICATES: frozenset[str] = frozenset(
    {"-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname"}
)
_FIND_REGEX_PATTERN_PREDICATES: frozenset[str] = frozenset({"-regex", "-iregex"})
#: A shell substitution that is UNRESOLVED at scan time: a command substitution
#: (``$(...)`` or a backtick run) or a variable expansion (``$VAR`` / ``${...}``).
#: A ``find`` predicate pattern carrying one cannot be proven to exclude the
#: search root — it may expand to the root's own basename — so such a pattern
#: fails closed (GPT/Opus security-class: ``-name "$(basename "$HOME")"``).
_RM_SHELL_SUBSTITUTION_RE = re.compile(r"\$\(|`|\$\{?[A-Za-z_]")


def _find_predicate_matches_root(predicate: str, pattern: str, root: str) -> bool:
    """True when a ``find`` NAME / PATH *predicate*'s *pattern* could match the
    search *root* itself.

    A predicate excludes the start root only when it provably CANNOT match it
    (GPT security-class: ``find / -path / -exec rm -rf {}`` — ``-path /`` is a
    specific pattern yet matches the start root ``/`` exactly, so the ``{}`` span
    that expands to ``/`` must still be classified). A whole-path predicate
    (``-path``/``-regex``/…) is matched against the root path; a basename
    predicate (``-name``/``-iname``) against the root's last segment. On any
    doubt — an unparseable regex, an unknown predicate — return True (fail
    closed: do NOT treat the predicate as excluding the root).
    """
    root = _rm_strip_all_quotes(root).rstrip("/") or "/"
    # A home-marked root (``~`` / ``$HOME`` / ``${HOME}``) is collected UNEXPANDED
    # (the rest of the file deliberately keeps the ``$home`` spelling), so a
    # literal basename compare would test the predicate against the string
    # ``$home`` — which no runtime-correct ``-name`` pattern equals, wrongly
    # granting the root-exclusion exemption and letting ``find "$HOME" -name
    # "<home-base>" -exec rm -rf {} +`` wipe home (GPT/Opus security-class,
    # UNBOUNDED). Expand a home marker to the REAL home path so the basename test
    # uses the true home basename the agent's pattern would actually match.
    _rl = root.lower()
    for _hm in ("~", "${home}", "$home"):
        if _rl == _hm or _rl.startswith(_hm + "/"):
            # ``expanduser`` returns the home path in its NATIVE case
            # (``C:/Users/<Account>`` on a Windows runner), but the
            # command walk already lowercased the operand/pattern frame reaching
            # this function, so a case-SENSITIVE ``-name`` compare below would miss
            # a mixed-case home basename and wrongly grant the exemption. Lower the
            # expanded home to the frame's case (same cross-platform fold as the
            # ``$HOME/..`` traversal collapse); the home tail of ``root`` is
            # already lowercase, so the joined path stays internally consistent.
            root = (
                _os.path.expanduser("~").replace("\\", "/").rstrip("/").lower() + root[len(_hm) :]
            )
            root = root.rstrip("/") or "/"
            break
    # A pattern carrying an UNRESOLVED shell substitution (``$(...)`` / backtick /
    # ``$VAR`` / ``${...}``) is unknown at scan time — it can expand to anything,
    # INCLUDING the root's own basename — so it can never be PROVEN to exclude the
    # root. Fail closed: treat it as possibly matching (``{}`` stays classified).
    # This closes ``find "$HOME" -name "$(basename "$HOME")" -exec rm -rf {} +``,
    # whose substitution pattern a literal ``fnmatch`` would (wrongly) find
    # unequal to the home basename (GPT/Opus security-class, UNBOUNDED).
    if _RM_SHELL_SUBSTITUTION_RE.search(pattern):
        return True
    # Expand a home-marker in the PATTERN symmetrically with the root, so a
    # whole-path ``-path ~`` still matches an expanded home root (``find ~ -path ~``
    # must deny) while ``-path ~/.cache/*`` still cannot match the bare home root
    # (stays allowed) — a one-sided root expansion would silently exempt the
    # former by making the two sides disagree. Only a whole-path predicate takes a
    # PATH pattern; a basename predicate (``-name``) tests the last segment, where
    # a full home path would never belong, so leave its pattern alone.
    if predicate in _FIND_PATH_PATTERN_PREDICATES:
        _pl = pattern.lower()
        for _hm in ("~", "${home}", "$home"):
            if _pl == _hm or _pl.startswith(_hm + "/"):
                pattern = (
                    _os.path.expanduser("~").replace("\\", "/").rstrip("/").lower()
                    + pattern[len(_hm) :]
                )
                break
    if predicate in _FIND_REGEX_PATTERN_PREDICATES:
        # ``-regex`` / ``-iregex`` carry an AGENT-AUTHORED pattern. Running
        # ``re.fullmatch`` on it here would evaluate attacker-controlled regex on
        # the SYNCHRONOUS PreToolUse gate, and catastrophic backtracking
        # (``find /aaaa… -regex '/(a+)+b' -exec rm -rf {} \;``) is NOT an
        # ``re.error`` — it just WEDGES the gate instead of returning a verdict
        # (Opus security-class). So never run the pattern: a regex predicate can
        # neither be proven to exclude the root nor cheaply evaluated, so fail
        # closed — treat it as possibly matching the root (``{}`` stays classified).
        return True
    if predicate in _FIND_PATH_PATTERN_PREDICATES:
        candidate = root
    else:  # -name / -iname: basename test
        candidate = _posixpath.basename(root) or root
    icase = predicate.startswith("-i")
    pat = pattern.lower() if icase else pattern
    cand = candidate.lower() if icase else candidate
    return _fnmatch.fnmatchcase(cand, pat)


#: Shell programs whose ``-c`` argument is a command STRING they execute. When a
#: nested payload's escaped quoting defeats the walk's own descent, the walk can
#: still hand this frame a FLATTENED argv (``['sh', '-c', 'rm', '-rf', '/']``);
#: the tokens after ``-c`` are then the executed command, read here as their own
#: argv so the ``rm`` leads its own command instead of sitting behind ``sh``.
_RM_SHELL_C_PROGRAMS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"}
)

#: NON-shell programs whose argument is a command STRING run by a shell.
#: ``watch 'rm -rf /'`` / ``flock /tmp/l -c 'rm -rf /'`` / ``ssh host 'rm -rf
#: /*'`` keep that whole command in ONE quoted token whose argv program is the
#: wrapper, so the per-``rm`` loop never fires (``_program_basename('rm -rf /')``
#: is ``'rm -rf '``, not ``rm``) and the single-quoted literal is never re-split
#: — the command wiped root unseen (Opus / GPT security-class). The value is how
#: the wrapper takes the command: ``"-c"`` means it follows a ``-c`` flag
#: (``flock FILE -c CMD``); ``"positional"`` means it is the first non-option
#: argument (``watch CMD``); ``"remote"`` means it follows a DESTINATION operand
#: that itself follows the options (``ssh [opts] HOST CMD``), and the command is
#: the remaining argv the remote shell runs. The re-split command string is
#: classified as its own argv, the same treatment ``sh -c`` and ``find -exec``
#: get. ``ssh`` denies the remote root/home wipe exactly as base's whole-line
#: literal did (it matched ``rm -rf /`` inside the quoted operand).
_RM_COMMAND_STRING_WRAPPERS: "dict[str, str]" = {
    "flock": "-c",
    "watch": "positional",
    "ssh": "remote",
}
#: Option letters that ``ssh`` takes a SEPARATE operand for, so the destination
#: scan skips both the flag and its value (``ssh -p 22 host CMD``, ``ssh -i key
#: host CMD``). Any other ``-x`` is treated as a LONE switch — and that is the
#: SAFE default: mis-reading a flag-only option as operand-taking makes the dest
#: scan swallow the host (then the quoted command becomes the destination and the
#: ``rm`` command token is lost), so an executed root wipe passes unseen (GPT 5.6
#: F1, security-class, UPHOLD-FENCED). The walk LOWERCASES the command before this
#: scan, which collapses ssh's case-paired options — ``-f`` (background, flag-only)
#: vs ``-F`` (config file, operand); ``-q`` (quiet, flag-only) vs ``-Q`` (query,
#: operand); ``-s`` (subsystem, no separate operand) vs ``-S`` (ctl path, operand);
#: ``-d`` (not an ssh option) vs ``-D`` (dynamic forward, operand) — so each pair
#: folds to one lowercase letter. Where a pair has BOTH a flag-only and an
#: operand-taking member, we MUST default it to flag-only, because eating the
#: command is the catastrophic direction. The colliding pairs defaulted to
#: flag-only are ``-f``/``-F``, ``-q``/``-Q``, ``-s``/``-S``, ``-d``/``-D``,
#: ``-c``/``-C`` (``-C`` = compression, flag-only) and ``-m``/``-M`` (``-M`` =
#: connection-sharing mode, flag-only). The only cost is that a rare
#: operand-taking spelling (``ssh -F cfg host 'rm -rf /'``) has its operand
#: treated as the destination and the real command re-split one token late —
#: which still DENIES (the command
#: is classified), just not via the destination slot. So this set lists only the
#: lowercase letters whose operand-taking reading has NO flag-only case-twin.
_RM_SSH_OPERAND_OPTIONS: frozenset[str] = frozenset(
    {"-p", "-i", "-l", "-o", "-b", "-e", "-w", "-j"}
)


def _rm_targets_in_command_wrapper(
    tokens: "list[str]", programs: "list[str]", *, strip_quotes: bool, _budget: "list[int]"
) -> "frozenset[str]":
    """Catastrophic ``rm`` targets inside a non-shell command-string wrapper.

    ``watch 'rm -rf /'`` / ``flock /tmp/l -c 'rm -rf /'`` / ``ssh host 'rm -rf
    /*'`` hand the whole command to a shell as ONE quoted token; its argv program
    is the wrapper, so the per-``rm`` loop and ``_rm_targets_in_shell_c`` (which
    requires a SHELL program word) both miss it, and the single token is never
    re-split (Opus / GPT security-class). For a frame whose program is a known
    command-string wrapper (:data:`_RM_COMMAND_STRING_WRAPPERS`), locate the
    command — after the wrapper's own ``-c`` (``-c`` form), the first non-option
    argument (``positional`` form), or the argv after the destination operand
    (``remote``/ssh form) — re-split it, and classify it as its own argv (one
    descent per command, drawing the shared budget).
    """
    found: set[str] = set()
    i = 0
    n = len(tokens)
    while i < n:
        wrapper = programs[i]
        mode = _RM_COMMAND_STRING_WRAPPERS.get(_program_basename(tokens[i]))
        if mode is not None and _program_basename(wrapper) == _program_basename(tokens[i]):
            cmd_token: "str | None" = None
            j = i + 1
            if mode == "-c":
                while j < n and not _ends_argv(tokens[j]):
                    if tokens[j] == "-c" and j + 1 < n:
                        cmd_token = tokens[j + 1]
                        break
                    j += 1
            elif mode == "remote":
                # ``ssh [opts] DEST CMD...``: skip options (and the operand an
                # option like ``-p``/``-i``/``-o`` takes), skip the DESTINATION
                # (first non-option), then the REST of the argv is the command the
                # remote shell runs — join it so a one-token quoted ``'rm -rf /*'``
                # and a multi-token unquoted ``rm -rf /`` are both re-split.
                seen_dest = False
                rest: list[str] = []
                while j < n and not _ends_argv(tokens[j]):
                    tok = tokens[j]
                    if not seen_dest:
                        if tok.startswith("-"):
                            if tok in _RM_SSH_OPERAND_OPTIONS and j + 1 < n:
                                j += 2
                                continue
                            j += 1
                            continue
                        seen_dest = True  # the destination operand
                        j += 1
                        continue
                    rest.append(tok)
                    j += 1
                if rest:
                    cmd_token = " ".join(rest)
            else:  # positional: first non-option argument
                while j < n and not _ends_argv(tokens[j]):
                    tok = tokens[j]
                    if tok.startswith("-"):
                        # ``watch -n2 CMD`` / ``watch -n 2 CMD``: only ``-n`` /
                        # ``--interval`` takes a SEPARATE operand (the interval).
                        # ``-d`` / ``--differences`` is a FLAG — it takes no value,
                        # so skipping an operand after it would swallow the command
                        # token and let ``watch -d 'rm -rf /'`` pass (GPT 5.6 F1,
                        # security-class). Treat only the interval option as
                        # operand-taking; every other ``-x`` is a lone switch.
                        if tok in ("-n", "--interval") and j + 1 < n:
                            j += 2
                            continue
                        j += 1
                        continue
                    cmd_token = tok
                    break
            if cmd_token is not None and _budget[0] > 0:
                _budget[0] -= 1
                found |= _rm_targets_in_argv(
                    _split_shell_words(cmd_token),
                    strip_quotes=strip_quotes,
                    _budget=_budget,
                )
        i += 1
    return frozenset(found)


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
    while i < n:
        if (
            _program_basename(tokens[i]) in _RM_SHELL_C_PROGRAMS
            and _program_basename(programs[i]) in _RM_SHELL_C_PROGRAMS
        ):
            # Find this shell command's own ``-c`` (before its argv ends), then
            # read the rest of the argv as the command string it executes.
            j = i + 1
            while j < n and not _ends_argv(tokens[j]):
                if tokens[j] == "-c" and j + 1 < n:
                    span = []
                    k = j + 1
                    while k < n and not _ends_argv(tokens[k]):
                        span.append(tokens[k])
                        k += 1
                    if span:
                        # The ``-c`` argument is a command STRING the inner shell
                        # re-parses, so re-split it — its OWN backslash de-escaping
                        # runs there. ``bash -c $"\r\m -rf /"`` reaches the inner
                        # shell as the script ``\r\m -rf /``, whose ``\r\m`` the
                        # inner bash de-escapes to ``rm``; the outer walk's
                        # printf-escape pass had mangled ``\r`` to whitespace and
                        # dropped the ``r``. De-escaping the joined payload the way
                        # the unquoted inner shell does, then splitting, recovers
                        # the ``rm`` program word (Item 4); classify that ONE view.
                        #
                        # Only ONE descent per ``-c`` span: de-escaping a payload
                        # with no backslash escapes is the identity, so the
                        # de-escaped split already covers the plain span — a second
                        # ``_rm_targets_in_argv(span)`` classified the same tokens
                        # again and let one nesting level fan out TWICE, which is
                        # what made a chain of ``sh -c`` spans exponential and hung
                        # the synchronous gate (Opus security-class).
                        if _budget[0] > 0:
                            _budget[0] -= 1
                            payload = _rm_deescape_unquoted_backslashes(" ".join(span))
                            found |= _rm_targets_in_argv(
                                _split_shell_words(payload),
                                strip_quotes=strip_quotes,
                                _budget=_budget,
                            )
                    break
                j += 1
        i += 1
    return frozenset(found)


def _rm_targets_in_argv(
    tokens: "list[str]",
    *,
    strip_quotes: bool,
    base_descendant_pin: bool = True,
    _budget: "list[int] | None" = None,
) -> "frozenset[str]":
    """The catastrophic ``rm`` targets deleted within ONE frame's raw argv.

    Fires for each token whose basename is ``rm`` and that is EXECUTED — ``rm``
    at program position (its command's leading word), the first argument of a
    multi-call dispatcher (``busybox rm``), or ``rm`` whose parent command does
    NOT treat its arguments as data. That last test is a DENYLIST: an ``rm``
    behind ANY parent is executed UNLESS the parent is in
    ``_DATA_CONSUMER_PROGRAMS`` (``echo`` prints, ``cat`` reads, ``cp``/``mv``
    move paths), so an unknown exec wrapper — ``setsid``/``nohup``/``chrt``/… —
    is treated as executable rather than slipping a fixed allowlist. ``rm`` is
    itself a data-consumer program, but that never mis-fires here because a
    program-position ``rm`` is caught by the leading-word test first and never
    reaches the parent check. An ``rm`` that is an argument of a data consumer
    (``echo rm -rf /`` prints, it does not run ``rm``) is skipped.

    From each executed ``rm`` its OWN argv is read forward until the command
    ends, so a sibling command's flags never leak in. A resolved operand is
    denied here when it is the root (``/``, a run of slashes, the ``/*`` glob) or
    home (``~`` / ``$HOME`` / ``${HOME}`` / their ``/*`` glob) dir ITSELF, in ANY
    flag spelling — the catastrophic root/home wipe. A DESCENDANT (``rm
    -rf /etc``, ``rm -rf ~/.ssh``) is NOT denied by this structural pass; base
    ``main``'s own descendant coverage is reproduced by the frame-text pin
    (``rm -rf /.*`` / ``rm -rf ~.*``, :data:`_RM_ROOT_LITERAL_RE`), so only a
    descendant base's contiguous ``rm -rf `` text matched is denied and a widened
    spelling (``rm -fr /tmp/x``) that base never contained stays allowed
    (Security Scope ruling).

    ``strip_quotes`` peels surrounding SHELL quotes from each operand — True for
    the raw split (``"$HOME"`` -> ``$HOME``), False for the decoded view where a
    surrounding quote is a literal character the decode produced (``$'"/"'`` ->
    ``"/"``, a filename, not the root).
    """
    if not tokens:
        return frozenset()
    # One shared descent budget per top-level classification. The public entry
    # (and every non-recursive caller) passes None, so a fresh cell is created
    # here; the sub-helpers thread the SAME cell into their recursive
    # ``_rm_targets_in_argv`` calls, so nested spans draw down one common budget.
    if _budget is None:
        _budget = [_RM_DESCENT_BUDGET]
    programs = _argv_programs(tokens)
    found: set[str] = set()
    found |= _rm_targets_in_shell_c(tokens, programs, strip_quotes=strip_quotes, _budget=_budget)
    found |= _rm_targets_in_command_wrapper(
        tokens, programs, strip_quotes=strip_quotes, _budget=_budget
    )
    # Computed once per argv (not per ``rm`` token): the pipe-into-shell / trailing
    # operator guards ``_data_consumer_exempt`` consults, whose sweep is quadratic
    # per token. ``None`` until the first ``rm`` needs it.
    disqualified: "bool | None" = None
    expect_program = True
    #: Index of the most recent command's program word, so a dispatcher's FIRST
    #: argument (its applet) can be recognised: ``busybox rm -rf /`` runs ``rm``.
    program_word_at = -1
    #: How many ``rm`` spans this argv has structurally classified. Bounds the
    #: per-``rm`` suffix re-scan to keep a ``rm``-padded argv linear (GPT 6.1 F2);
    #: a wipe past the cap is still caught by the whole-text deny-net regex.
    rm_spans_classified = 0
    for i, token in enumerate(tokens):
        is_program_word = (
            expect_program and bool(token) and not _shell_normalizer.ENV_ASSIGNMENT_RE.match(token)
        )
        starts_command = is_program_word
        if is_program_word:
            expect_program = False
            program_word_at = i
        # A glued ``&`` / ``&&`` ENDS the command (backgrounds or chains it), so
        # the next token starts a NEW command that really runs — ``echo hi& rm
        # -rf /`` is two commands, and the ``rm`` is executed, not echo's data.
        # ``_ends_argv`` catches ``|``/``;`` glued to a token but not ``&``, and a
        # standalone ``&`` token is already covered; this adds the glued-tail case
        # . A ``2>&1`` redirection ends in ``1``, not
        # ``&``, so it is not mistaken for a boundary.
        if _ends_argv(token) or token.endswith("&"):
            expect_program = True
        if _program_basename(token) != "rm":
            continue
        # Bound the per-``rm`` suffix re-scan (GPT 6.1 F2): a ``rm``-padded argv is
        # quadratic without this. Past the cap, stop classifying ``rm`` spans — a
        # catastrophic literal in a later span is still denied by the whole-text
        # deny-net regex, so this cannot fail open.
        if rm_spans_classified >= _RM_CLASSIFY_SPAN_CAP:
            break
        rm_spans_classified += 1
        # Executed iff ``rm`` leads its own command, or ``rm`` is the FIRST
        # argument of a multi-call dispatcher (``busybox rm`` runs the rm applet),
        # or its parent command does NOT treat its arguments as data. The last
        # test is a DENYLIST, not an allowlist: an exec wrapper set could only
        # ever name the wrappers someone thought of, and ``setsid``/``nohup``/
        # ``chrt``/``ionice``/… or any future one
        # would slip through. So the default for an UNKNOWN parent is EXECUTABLE,
        # and only a parent in ``_DATA_CONSUMER_PROGRAMS`` (``echo`` prints, ``cat``
        # reads, ``cp``/``mv`` move paths) makes the ``rm`` a data mention.
        # ``_data_consumer_exempt`` also refuses the exemption when the argument
        # pipes into a shell or carries a glued new-program operator, so
        # ``echo rm -rf / | sh`` is still executed.
        dispatched_applet = (
            i == program_word_at + 1
            and program_word_at >= 0
            and _program_basename(tokens[program_word_at]) in _RM_APPLET_DISPATCHERS
        )
        # When the command's program is a multi-call dispatcher, its FIRST
        # argument is the applet that actually runs, so THAT — not ``busybox`` —
        # is the effective parent of a later ``rm``. ``busybox echo rm -rf /``
        # runs ``echo``, which prints ``rm -rf /``: a mention, not a wipe. Resolve
        # the effective parent to the applet before the data-consumer test so the
        # dispatcher itself (never a data consumer) does not make its applet's
        # arguments look executed.
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
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(i, token, programs, tokens, command_disqualified=disqualified):
                continue
        # STRUCTURAL classification (below) denies the EXACT root/home target
        # ITSELF for ANY ``rm`` — direct, dispatcher applet, or EXEC-WRAPPER
        # reached (``sudo``/``setsid``/``docker exec`` …) — because the
        # catastrophic root/home wipe is denied in every flag spelling. base
        # ``main``'s own DESCENDANT coverage (``rm -rf /etc``, ``sudo
        # rm -rf /etc``, ``rm -rf ~/.ssh``) is reproduced at the FRAME-TEXT level
        # by the base-contiguous-literal pin (``rm -rf /.*`` / ``rm -rf ~.*``, see
        # :data:`_RM_ROOT_LITERAL_RE`), so a descendant base's contiguous ``rm
        # -rf `` text matched is still denied without the token-adjacency guesswork
        # a structural flag check needs, and a WIDENED spelling base never
        # contained (``rm -fr /tmp/x``) stays allowed (Security Scope ruling).
        has_rec = has_force = has_npr = False
        end_of_options = False
        depth = 0
        operands: list[str] = []
        # Base ``main`` ran its whole-line literal ``rm -rf /.*`` / ``rm -rf ~.*``
        # against the QUOTE-NORMALIZED token re-join too (``__init__`` Pass 2),
        # so ``rm -rf "/etc"`` / ``rm  -rf /etc`` / ``rm "-rf" /etc`` were denied
        # on base. The raw-source frame-text pin only reproduces the UNQUOTED view
        # and misses them (Opus security-class). Reproduce the normalized match
        # STRUCTURALLY, but ONLY on the raw (``strip_quotes``) view: there a real
        # ``rm`` command's ``$HOME`` operand stays the literal ``$home`` spelling
        # (NOT the expanded ``/home/<user>`` the decoded frame carries), so base's
        # ``/``/``~`` contiguity still excludes ``$HOME``-descendants, and a quoted
        # ``rm`` MENTION inside a data word (``echo 'rm -rf /'``) is a single
        # ``echo`` operand — never reaching this per-``rm`` loop — so it stays
        # exonerated. base's literal was a SINGLE contiguous ``-rf`` token
        # IMMEDIATELY followed by a literal ``/``/``~`` operand; a widened spelling
        # (``-fr``, split ``-r -f``, long options), a ``--`` between, or a
        # ``$HOME``-prefixed operand all break that and stay allowed.
        base_rf_token = False
        base_rf_descendant = False
        # base's literal was ``rm`` IMMEDIATELY followed by ``-rf`` (``rm -rf /``):
        # a flag or operand BEFORE the ``-rf`` (``rm -v -rf /tmp/x``, ``rm /tmp/x
        # -rf``) broke that contiguity and base NEVER matched it, so the ``-rf``
        # descendant pin may only arm when ``-rf`` is ``rm``'s first argument. This
        # stays True until the first argument is classified below, then latches off
        # (Opus security-class false-positive: ``rm -v -rf /tmp/x`` must ALLOW).
        at_first_arg = True
        # ``rm {--recursive,--force,--no-preserve-root} {/,/tmp}`` reaches the floor
        # with the brace GROUPS as single tokens that match no flag predicate and no
        # root operand, yet bash expands each word in place to
        # ``rm --recursive --force --no-preserve-root / --recursive --force
        # --no-preserve-root /tmp`` and wipes ``/`` (GPT security-class, F1). Expand
        # every token's statically-decidable alternation members in place BEFORE flag
        # and operand parsing, so a brace-grouped flag is seen as its flags and a
        # brace-grouped operand as its operands. A non-brace / single-member token
        # (``{state}``, an awk program) expands to itself and the stream is unchanged;
        # the expansion is bounded by ``_RM_BRACE_EXPANSION_CAP`` per token.
        expanded_args: list[str] = []
        for raw_arg in tokens[i + 1 :]:
            members = _rm_expand_brace_members(raw_arg)
            expanded_args.extend(members if members else [raw_arg])
        for arg in expanded_args:
            operand, quote_peeled = (
                _rm_strip_surrounding_quotes_reporting(arg) if strip_quotes else (arg, False)
            )
            # Quoting a flag does NOT stop GNU ``rm`` option parsing — bash strips
            # the quotes during word expansion, so ``rm '-rf' ~`` / ``rm "-rf" ~``
            # / ``rm -r''f ~`` / ``rm \-rf ~`` all reach ``rm`` as ``-rf`` (Opus
            # security-class). Test the flag predicates against the FULLY de-quoted
            # spelling bash acts on; the raw ``arg`` keeps its quotes and would be
            # mis-read as an operand, failing open on the wipe.
            flag_tok = _rm_strip_all_quotes(arg) if strip_quotes else arg
            # A glued control operator (``/;reboot``, ``/&&id``) leaves the real
            # operand before it; classify only that head and, outside a
            # substitution, end this rm's argv at the boundary so a command glued
            # after it is not read as another operand. A separator that only
            # became bare because a surrounding quote was peeled (``';'``) was
            # QUOTED in the source — a literal filename, not a boundary — so the
            # split is suppressed for it (Opus security-class: ``rm -rf ';' /``);
            # ``_rm_operand_before_boundary`` likewise skips a backslash-ESCAPED
            # operator (``a\;b`` is one filename). The split runs ONLY on the raw
            # (``strip_quotes``) view: the decoded view's quotes/escapes are already
            # resolved by the walk, which tokenized on the REAL separators, so a
            # bare ``;`` there is a genuine terminator the walk already placed as
            # its own token — re-splitting an operand on it would truncate a
            # legitimate filename (Opus security-class).
            glued_boundary = False
            if strip_quotes and not quote_peeled and depth + _substitution_depth_delta(arg) <= 0:
                operand, glued_boundary = _rm_operand_before_boundary(operand)
            if flag_tok == "--" and not end_of_options:
                end_of_options = True
                base_rf_token = False  # ``--`` breaks base's contiguous ``rm -rf /``
            elif not end_of_options and flag_tok == "--no-preserve-root":
                has_npr = True
                base_rf_token = False
            elif not end_of_options and _rm_is_recursive_flag(flag_tok):
                has_rec = True
                if _rm_is_force_flag(flag_tok):
                    has_force = True
                    # base's single ``-rf`` token, and ONLY when it is ``rm``'s
                    # first argument (base's contiguous ``rm -rf``).
                    base_rf_token = at_first_arg and flag_tok == "-rf"
            elif not end_of_options and _rm_is_force_flag(flag_tok):
                has_force = True
                base_rf_token = False  # ``-r -f`` split is a WIDENED spelling
            elif operand:
                # The operand IMMEDIATELY after base's ``-rf`` token: base's
                # contiguous ``rm -rf /`` / ``rm -rf ~`` matched it iff it is a
                # LITERAL ``/``- or ``~``-prefixed word (NOT ``$HOME``). Only on
                # the raw view, where ``$HOME`` is unexpanded. Test the FULLY
                # de-quoted spelling too: ``rm -rf "/"etc`` keeps a leading ``"``
                # on the raw operand, yet bash runs ``rm -rf /etc`` and base's
                # re-joined ``_shell_tokens`` view (``rm -rf /etc``) DENIED it, so
                # the stripped prefix must drive the base pin (Opus security-class).
                if strip_quotes and base_rf_token:
                    stripped_op = _rm_strip_all_quotes(operand)
                    if (
                        operand.startswith("/")
                        or operand.startswith("~")
                        or stripped_op.startswith("/")
                        or stripped_op.startswith("~")
                    ):
                        base_rf_descendant = True
                base_rf_token = False
                operands.append(operand)
            # A shell-ELIDED empty word (``rm "" -rf /``) contributes neither a
            # flag nor an operand — bash expands ``""`` to nothing, so base's
            # quote-normalized view (which ``base_rf_descendant`` reproduces) never
            # saw it and the ``rm -rf /`` that follows stayed contiguous. Only
            # advance past ``rm``'s first-argument position when THIS word actually
            # contributed a flag or an operand, so an elided word does not spuriously
            # clear ``at_first_arg`` and disarm base's ``rm -rf <root>`` pin (Opus
            # security-class: ``rm "" -rf /etc``).
            if flag_tok or operand:
                at_first_arg = False
            depth += _substitution_depth_delta(arg)
            # A quoted-``;``/``|`` token (``';'``) or a backslash-ESCAPED operator
            # (``a\;b``) is a literal filename argument, not a command terminator —
            # ``_ends_argv`` on the RAW ``arg`` would see the bare operator and stop
            # the argv before a later ``/`` operand (Opus security-class: ``rm -rf
            # ';' /``, ``rm -rf a\;b /*``). On the raw view the terminator fires
            # only on an UNESCAPED, unpeeled operator; the decoded view (already
            # tokenized on real separators by the walk) still ends on its own bare
            # terminator token.
            if strip_quotes:
                raw_terminates = (
                    not quote_peeled and _ends_argv(arg) and _rm_unescaped_boundary(arg) is not None
                )
            else:
                raw_terminates = _ends_argv(arg)
            if depth <= 0 and (glued_boundary or raw_terminates):
                break
            depth = max(depth, 0)
        # STRUCTURAL classification denies the EXACT root/home target ITSELF — the
        # catastrophic root/home wipe in ANY flag spelling (``rm -fr /``,
        # ``rm -r -f ~``, ``rm --recursive --force $HOME``, quoted-flag ``rm '-rf'
        # /`` — all root/home ITSELF). A DESCENDANT in a WIDENED spelling (``rm -fr
        # /tmp/x``, ``rm -rf $HOME/.cache``) is NOT denied here: base ``main``'s
        # whole-line literal was the contiguous ``rm -rf /`` / ``rm -rf ~`` text,
        # which never matched those spellings, so denying their descendants newly
        # refuses legitimate scratch/cache cleanup (Security Scope ruling). base's
        # OWN ``-rf`` descendant coverage (``rm -rf /etc``, ``rm -rf "/etc"``,
        # ``rm -rf ~/.ssh``) is reproduced by ``base_rf_descendant`` above (base's
        # contiguous ``-rf`` token + a literal ``/``/``~`` operand, raw view only)
        # for a quoted/space-padded ``rm`` command, and by the frame-text pin
        # (:func:`_rm_base_literal_bare_tokens`) for a wrapper-reached unquoted
        # ``sudo rm -rf /etc``.
        root_re, home_re = _RM_ROOT_ITSELF_RE, _RM_HOME_ITSELF_RE
        # Classify each operand AND its dot-segment-normalized form, so ``/./`` /
        # ``/tmp/../`` / ``~/.`` (which the kernel resolves to root/home) are caught
        # by the exact matchers. On the RAW split (``strip_quotes``), also classify
        # the fully de-quoted spelling, so a PARTIALLY quoted ``"$HOME"/`` /
        # ``"${HOME}"/x`` keeps its ``$HOME`` anchor the matchers need (GPT
        # security-class). The de-quote is NOT applied to the decoded view, where a
        # surrounding quote the decode produced is a literal filename character
        # (``$'"/"'`` is a file named ``/``, not the root).
        candidates = list(operands) + [_rm_normalize_dot_segments(op) for op in operands]
        if strip_quotes:
            dequoted = [_rm_strip_all_quotes(op) for op in operands]
            candidates += dequoted + [_rm_normalize_dot_segments(op) for op in dequoted]
        # A brace word (``{~,/x}``, ``/{,bin}``, ``$HOME/{,.cache}``) expands to
        # several operands and the exact matchers must see each member, else the
        # root/home member hides behind the un-expandable brace word (GPT
        # security-class). Expand every candidate's alternation members and classify
        # them (and their dot-normalized forms) too; a non-brace / single-member word
        # contributes nothing, so this only widens the brace case.
        brace_members = [m for op in list(candidates) for m in _rm_expand_brace_members(op)]
        if brace_members:
            candidates += brace_members + [_rm_normalize_dot_segments(m) for m in brace_members]
        root_target = any(root_re.fullmatch(op) for op in candidates)
        home_target = any(home_re.fullmatch(op) for op in candidates)
        # ``x=$HOME; rm -rf "$x"`` — on the DECODED view the walk substitutes ``$x``
        # to the EXPANDED home path (``/home/<user>``), which carries no ``~``/
        # ``$HOME`` marker and so matches neither exact matcher, allowing the home
        # wipe (GPT 5.6 F3, security-class). Classify an operand that IS the expanded
        # home path as ``home`` too. Only on the decoded view (``not strip_quotes``):
        # the raw view deliberately keeps ``$HOME`` unexpanded so base's ``/``/``~``
        # contiguity still excludes ``$HOME``-descendants, and base's own ``rm -rf
        # /home/<user>`` coverage (an absolute literal spelling) is the ``/``-rooted
        # base-literal pin, not this. Matched on EQUALITY (the home dir ITSELF), not a
        # prefix, so a home DESCENDANT (``/home/<user>/.cache``) stays allowed exactly
        # as base allowed its ``$HOME/.cache`` spelling. The walk substitutes
        # ``$HOME`` with the platform-native ``expanduser("~")`` value AFTER the
        # command was lowercased, so on Windows the operand is a native-CASE
        # BACKSLASH path (``C:\Users\<user>``) while ``_rm_expanded_home_path`` is
        # ``/``-folded and lowercased; fold each candidate's separators and case the
        # same way before comparing, or the match fires on POSIX only (Windows CI).
        # ``_RM_HOME_ITSELF_RE`` gives ``~``/``$HOME`` a trailing tail
        # ``(?:/+(?:\*/*)?)?`` so ``~/`` and ``~/*`` (a run of slashes, or the
        # glob over children) classify as the home dir itself. The expanded-home
        # comparison must apply the SAME tail, or ``rm -fr /home/<user>/`` and
        # ``/home/<user>/*`` — which delete the same tree — slip through while
        # ``/home/<user>`` is denied (Opus 5.5 FINDING). Strip a trailing run of
        # ``/`` and an optional whole-remainder ``*`` glob before comparing.
        if not strip_quotes and not home_target:
            home_real = _rm_expanded_home_path()
            if home_real and any(
                _rm_strip_home_itself_tail(op.replace("\\", "/").lower()) == home_real
                or _rm_strip_home_itself_tail(
                    _rm_normalize_dot_segments(op).replace("\\", "/").lower()
                )
                == home_real
                for op in candidates
            ):
                home_target = True

        # ``base_rf_descendant`` reproduces base's quote-normalized ``rm -rf /.*``
        # / ``rm -rf ~.*`` substring (``rm -rf "/etc"``, ``rm  -rf /etc``,
        # ``rm -rf "/"etc``): base's single ``-rf`` token immediately followed by a
        # LITERAL ``/``/``~`` operand, a DESCENDANT included. The prefix is read on
        # the raw AND the fully de-quoted operand, so a partially-quoted ``"/"etc``
        # (bash: ``/etc``) classifies as root (Opus security-class). Classify the
        # matching class.
        def _rf_prefixed(ch: str) -> bool:
            return any(
                op.startswith(ch) or _rm_strip_all_quotes(op).startswith(ch) for op in operands
            )

        base_rf_root = base_descendant_pin and base_rf_descendant and _rf_prefixed("/")
        base_rf_home = base_descendant_pin and base_rf_descendant and _rf_prefixed("~")
        if has_npr or (has_rec and has_force):
            if root_target or base_rf_root:
                found.add("root")
            if home_target or base_rf_home:
                found.add("home")
    return frozenset(found)
