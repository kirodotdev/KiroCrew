"""The lexical scanners the cron shell gate runs on a command line or a script body.

The protected-path and secret-environment matchers, the shell quote states, local
assignments, ``$`` expansions, extglob and brace expansion, and whether a glob can
reach a credential directory. Each answers one question about the text; the verdicts
built from them live in :mod:`kiro_crew.mcp_cron_vet`.

Served through :mod:`kiro_crew.mcp_cron`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator

from kiro_crew.sandbox import _AGENT_DENIED_ENV_KEYS
from kiro_crew.security import (
    _SENSITIVE_HOME_DIRS,
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
    _iter_shell_chars,
)
from kiro_crew.security.readonly_bash import _EXTGLOB_RE

# Credential dirs/files a cron shell command must never reference directly. The
# sandbox (cron_script.run_command_sandboxed, mode="cc") is the only
# sanctioned access path. We reuse security._SENSITIVE_HOME_DIRS (the canonical
# list, kept DRY so it can't drift) and match the token ANYWHERE in the command
# -- the shared is_sensitive_bash_command matches no paths at all (the OS sandbox
# is its path control) -- because tools such as ``curl -d @~/.aws/credentials`` or
# ``wget --post-file=$HOME/.ssh/id_rsa`` read files via flags with no recognizable
# read-command prefix.
# A reference to a protected path: a path/quote/flag lead-in, the name itself in
# a capturing group, then a separator/quote/end. The one search both detects the
# match and reports WHICH name it hit (``match.group(1)``), so no second per-name
# pass is needed to name the path.
_CRON_CRED_PATH_RE = re.compile(
    r"(?:^|[\s'\"=@/~`]|\$\{?HOME\}?)"
    r"(" + "|".join(re.escape(d) for d in _SENSITIVE_HOME_DIRS) + r")"
    r"(?:/|\s|['\"]|$)",
    re.IGNORECASE,
)
# The COMMAND form of the same matcher. A shell command line reaches the
# directory through a redirection, a list separator or a backslash too:
# `cat <.ssh/id_rsa`, `cd .ssh;`, `~/.ssh\\/id_rsa` (verified in real bash).
# Script bodies keep `_CRON_CRED_PATH_RE`: Python source gets no shell grammar.
_CRON_CMD_CRED_PATH_RE = re.compile(
    r"(?:^|[\s'\"=@/~`<>|&;]|\$\{?HOME\}?)"
    r"(" + "|".join(re.escape(d) for d in _SENSITIVE_HOME_DIRS) + r")"
    r"(?:[/\\\s'\";&|<>]|$)",
    re.IGNORECASE,
)
# The matcher is case-insensitive, so map a hit back to the canonical entry
# spelling for the refusal message.
_SENSITIVE_HOME_DIRS_BY_LOWER = {d.lower(): d for d in _SENSITIVE_HOME_DIRS}


def _matched_sensitive_name(text: str, pattern: re.Pattern[str] = _CRON_CRED_PATH_RE) -> str | None:
    """Return the ``_SENSITIVE_HOME_DIRS`` entry a literal reference in *text*
    hits (canonical spelling), or ``None``. One ``_CRON_CRED_PATH_RE`` search
    both detects the match and names it via its capturing group."""
    m = pattern.search(text)
    if m is None:
        return None
    return _SENSITIVE_HOME_DIRS_BY_LOWER.get(m.group(1).lower(), m.group(1))


def _protected_path_refusal(surface: str, matched: str | None) -> str:
    """Build the refusal for a cron ``surface`` ("command" or "script") that
    referenced a protected path. Names the matched entry when known. The list it
    classifies mixes credential stores with paths fenced for other reasons (a
    kubeconfig, an SSO cookie dir, an agent's own data home, the governance
    trust-root), and the entry strings are not a reliable credential signal, so
    every match is described with one neutral "protected path" wording rather
    than guessing whether the entry is a credential file."""
    if matched is None:
        # Detected via glob expansion, whose matched name is not carried back.
        return (
            f"Error: cron {surface} blocked: a glob could expand onto a protected "
            f"path. Cron {surface}s may not reach fenced paths (credential stores "
            f"and other protected directories) directly."
        )
    return (
        f"Error: cron {surface} blocked: references the protected path {matched!r}. "
        f"Cron {surface}s may not read fenced paths directly."
    )


# Protected secret env vars a cron command must not read by name. Union of the
# sandbox-scrubbed agent keys (Slack tokens, owner id) and well-known cloud /
# source-control credential env vars. The sandbox strips _AGENT_DENIED_ENV_KEYS
# from
# the cron subprocess env, but AWS_*/token vars may still be present (or arrive
# via a future regression), so denying a by-name reference at storage time is a
# cheap, precise backstop that mirrors the existing execute_bash deniedCommands
# AWS patterns in config/defaults.json.
_CRON_SECRET_ENV_NAMES = list(_AGENT_DENIED_ENV_KEYS) + [
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITLAB_TOKEN",
]
_CRON_SECRET_ENV_RE = re.compile(
    r"\$\{?(?:" + "|".join(re.escape(k) for k in _CRON_SECRET_ENV_NAMES) + r")\}?",
    re.IGNORECASE,
)
# Bare-name form for scanning SCRIPT bodies: a Python/Ruby cron script reads
# secrets by env-var NAME (e.g. os.environ["AWS_SECRET_ACCESS_KEY"]), not via
# the shell ``$NAME`` syntax, so we also match the names on word boundaries.
_CRON_SECRET_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _CRON_SECRET_ENV_NAMES) + r")\b",
)
# Command substitution / shell arithmetic in a cron `command`: hostile because it
# assembles sensitive paths at runtime that no static string check can see. We
# accept ONLY unassembled shell one-liners on this surface; a job that needs
# composition belongs in a `script`, where the body is scanned in full. We match
# every construct that yields runtime string composition: `$(...)`, `$((...))`,
# `$'...'` ANSI-C quoting (which decodes `\xNN`/`\NNN`/`\t` escapes, so
# `$'\x2e\x73\x73\x68'` becomes `.ssh` — invisible to a literal scan), and legacy
# backtick pairs. We deliberately reject a lone backtick too — a stray one means
# unmatched-quoting confusion, not a benign literal.
_CRON_CMD_SUBST_RE = re.compile(
    r"\$\(|"  # $( ... ) and $(( ... )) (`\(` covers both since $((… starts with $()
    r"\$'|"  # $'...' ANSI-C quoting: `$'\x2e\x73\x73\x68'` decodes to ".ssh"
    r"`",  # backtick — matches EITHER end of a pair, and unmatched too
)
# A ``${...}`` that is NOT a plain ``${NAME}`` reference. Every other brace form
# COMPOSES a string at expansion time, which is the same hazard as command
# substitution and equally invisible to a static path scan:
#   ${X:-.s}${Y:-sh}  default values, assembling ".ssh" from two literals that
#                     appear nowhere as a credential path
#   ${X#pre} ${X%suf} ${X/a/b}  prefix/suffix/replace transforms
#   ${#X}                       length
# A bare ``${HOME}`` / ``${MYVAR}`` stays allowed — it is an ordinary reference
# and, being a single name, cannot smuggle a fragment the assignment resolver
# does not already follow. Matching on "brace content is not just an identifier"
# rather than enumerating the operators means a form nobody listed is refused by
# default instead of admitted.
_CRON_BRACE_EXPANSION_RE = re.compile(r"\$\{(?![A-Za-z_][A-Za-z0-9_]*\})")
# BASH BRACE EXPANSION — the one composition form on this surface that carried no
# storage-time refusal, and so was left entirely to a runtime shell probe
# (``cron_script._shell_is_posix_strict``). It composes exactly like the forms
# above and is equally invisible to a static path scan:
#   cat ~/.a{w,w}s/creds   expands to a path whose literal text never contains it
#   {1..9}                 sequence form, same hazard with no comma
# Refusing it here makes the guarantee independent of WHICH shell runs the
# command, which is what lets the resolver accept a trusted shell that would
# otherwise expand. Enumerating a re-enable denylist instead (`set -B`,
# `shopt -s braceexpand`) leaks — `eval "set -B"` reaches the same state — so the
# refusal targets the braces, not the switch.
# A NESTED comma form is a real expansion too — `.a{w,{w}}s` -> `.aws .a{w}s`, so
# the first expanded word IS the credential directory — and it becomes reachable
# precisely under this change, since the `+B` probe this ships alongside is what
# admits a brace-expanding bash as the cron executor.
# Whitespace inside the braces is what separates an expansion from an ordinary
# braced argument such as `awk '{print x, y}'`, but only BARE whitespace is: bash
# leaves `{a b,c}` literal and expands every QUOTED or ESCAPED spelling of that
# same space. Which characters those are depends on the quote state each one sits
# in, and quote state is not a regular property of the surrounding text, so this
# refusal is a left-to-right scan rather than a pattern —
# see ``_has_bash_brace_expansion`` for the measured table it implements.
# Any `$NAME` / `${NAME}` variable reference. sh expands an unset variable to the empty
# string, so `cat ~/.ss${UNSET}h/id_rsa` reads `.ssh` (verified) while the literal text
# keeps the name split. Any reference composes a string the static scan cannot follow,
# whether or not the command assigns it, so it is refused. `$HOME` (and `${HOME}`)
# is the one exception: it is the documented way a cron names the home dir, its
# value is a fixed prefix that cannot smuggle a fragment the credential-path
# scan does not already anchor on, and refusing it would break ordinary crons.
_CRON_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_CRON_VAR_REF_ALLOWED = frozenset({"HOME"})
# Positional and special parameters: `$1`..`$9`, `$@`, `$*`, `$#`. These compose
# from values `set --` supplies, which the assignment resolver does not track:
#   set -- .s sh; cp ~/$1$2/id_rsa   reads ~/.ssh/id_rsa (verified)
# Unlike a named variable these are refused OUTRIGHT rather than resolved,
# because a cron `command` is run as ``sh -c <command>`` with no arguments — so
# every positional parameter is empty unless the command itself set them, which
# only a composition payload does. There is no legitimate use to preserve.
_CRON_POSITIONAL_PARAM_RE = re.compile(r"\$[0-9@*#]|\$\{[0-9@*#]")
# Shell loop / compound-command keywords in COMMAND-WORD position. A `for NAME
# in .s sh; do ... $NAME ...` binds the loop variable to values the NAME=VALUE
# resolver does not track, so `~/$A$B` reads `.ssh` unseen (verified). A cron
# `command` is a single unassembled one-liner by contract; a job that needs a
# loop belongs in a `script`, whose body is scanned in full. Anchored to
# start-of-command / after a separator / after `do`/`then` and word-bounded, so
# `git log --format=for` (keyword as an argument) and a quoted `'while ...'` are
# NOT matched. `case` is included because its patterns compose the same way.
_CRON_SHELL_KEYWORD_RE = re.compile(r"(?:^|[;&|]|\bdo\b|\bthen\b)\s*\b(?:for|while|until|case)\b")
# Pathname expansion (globbing) is a FOURTH way to compose a sensitive path that
# never appears literally: ``cat ~/.s?h/id_rsa`` reads ``~/.ssh/id_rsa`` (verified
# against real sh, all three metacharacters). Blanket-refusing ``*``/``?``/``[``
# would break ordinary crons (``rm /tmp/*.log``, ``tar czf - logs/*.txt``), so a
# glob-bearing word is instead MATCHED against the sensitive names as a glob —
# see _glob_could_reach_credentials for why matching beats substitution.
# Ceiling on the glob-bearing word length handed to fnmatch. The longest
# sensitive name is well under 60 characters, so this is far above anything that
# can legitimately match one; it bounds fnmatch's superlinear pattern compile
# on a hostile `cat ????...`.
_CRON_MAX_GLOB_WORD = 256

# BASH EXTGLOB GROUPS `@(…)` `?(…)` `+(…)` `*(…)` `!(…)` are a FIFTH way to
# compose a path: `bash -O extglob -c 'cat ~/.ss@(h)/id_rsa'` reads
# `~/.ssh/id_rsa`. Whether extglob is ON cannot be read off the text (a command
# can switch it on with text it builds at run time), so the vet never asks: every
# group in the WHOLE command is rewritten into plain-glob text and each rewrite
# is judged by the literal and plain-glob checks that already exist. Rewriting
# the whole command, not one word, keeps a group whose alternatives hold spaces
# (`@(h| x)`) intact, and lets the plain-glob path do its own word split.
_EXTGLOB_OP_RE = _EXTGLOB_RE
# Leaf budget for one rewrite. `@(a|b)` / `?(a|b)` groups multiply alternatives;
# past the budget the command is refused (adversarial input only — an ordinary
# `grep -E '(a|b)?(c|d)?(e|f)?(g|h)?'` is 81 leaves).
_EXTGLOB_MAX_EXPANSIONS = 512


# Local variable assignments can smuggle path fragments past the vet:
# `A=.s; B=sh; cp ~/$A$B/id_rsa ...` -- the vetter sees `~/` and `/id_rsa` as separate
# tokens and misses the assembled `~/.ssh/id_rsa`. The vet does not substitute a value:
# any `$NAME` other than `$HOME` is refused as a variable reference, and this scan only
# bounds how many assignments one command may carry. It covers both an assignment per
# command (`A=.s; B=sh`) and an assignment LIST in one (`A=.s B=sh`).
def _iter_local_assignments(text: str) -> Iterator[tuple[str, str]]:
    """Yield the conservative assignment scan's name/value pairs in source order."""
    for word in re.split(r"[;&|\s]+", text):
        name, separator, value = word.partition("=")
        # ASCII identifiers are exactly the shell NAME grammar. Partition at
        # the first '=' once; failed names cannot restart a pattern search.
        # ``cp a=b`` still conservatively counts as an assignment for scanning.
        if separator and name.isascii() and name.isidentifier():
            yield name, value


# A backslash escaping any character. sh drops the backslash and keeps the
# character during word expansion, so the scan must do the same to see the string
# the shell will actually use.
_BACKSLASH_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)
#: Cap on the number of variable assignments in one cron `command`. No real one-liner
#: sets this many, and a command that does is composing strings a static check cannot
#: follow, so it is refused rather than scanned.
_CRON_MAX_ASSIGNMENTS = 64
# Cap how much of a cron script we read for the security review (256 KiB is far
# larger than any legitimate cron script; bounds memory on a hostile huge file).
# ALIASED to the gate's own source-body ceiling rather than restated, so the reader and
# the gate cannot disagree about which one is the operative limit.
_MAX_SCRIPT_SCAN_BYTES = MAX_SCANNABLE_SOURCE_BODY_CHARS
#: What :func:`_vet_script_file` actually reads: ONE character past the cap, so an
#: oversized script is DETECTED rather than silently truncated. Reading exactly the cap
#: is a fence bypass, not a bound: the vetter then sees a body at the limit, scans it
#: clean, and the sandbox executes the whole file -- so a benign 256 KiB prefix followed
#: by a credential read in its tail was allowed. The extra character makes the body
#: exceed ``MAX_SCANNABLE_SOURCE_BODY_CHARS``, which refuses it, and refusing an
#: unscannable script is the same direction every other budget in the gate takes. A file
#: of exactly the cap still reads short of this and is scanned in full, so no legitimate
#: script is refused for being at the boundary.
_SCRIPT_READ_PROBE_BYTES = _MAX_SCRIPT_SCAN_BYTES + 1


# ``HOME`` named as a bare word: not directly after ``$`` or ``${`` and not inside a longer
# identifier. ``$HOME`` and ``${HOME}`` are the only spellings the vet lets through.
_CRON_BARE_HOME_RE = re.compile(r"(?<![$A-Za-z0-9_{])HOME(?![A-Za-z0-9_])")


_CRON_QUOTE_OR_BACKSLASH_RE = re.compile(r"[\\'\"]")


def _rebinds_home(text: str) -> bool:
    """True when *text* names ``HOME`` anywhere other than as ``$HOME`` or ``${HOME}``.

    ``$HOME`` is the one variable the vet lets a command read, and ``~`` expands from
    it too, so anything that changes it composes a path no static scan sees:
    ``HOME=/x/.ssh; cat ~/id_rsa``, ``export HOME=...``, and ``read HOME`` /
    ``printf -v HOME`` / ``getopts o HOME`` / ``unset HOME``, which rebind it with no ``=``
    in the text (each verified under real sh to read ``~/.ssh/id_rsa``). Judged on the raw
    text like the other rules; a ``HOME`` in a comment or a quoted word is refused too.

    The name is also looked for with EVERY quote and backslash deleted: a builtin receives
    its arguments after quote removal, and each nested shell removes another level, so
    ``read HO''ME``, ``printf -v HO\\ME`` and ``sh -c "export HO\\\\ME=..."`` rebind it
    while the raw text never holds the four letters together. Deleting all of them,
    rather than undoing one level, is what makes the answer the same at any depth. The
    reverse split (``$HO''ME``) cannot hide a read, because the unresolved-reference rule
    refuses it.
    """
    stripped = _CRON_QUOTE_OR_BACKSLASH_RE.sub("", text)
    return any(_CRON_BARE_HOME_RE.search(form) is not None for form in (text, stripped))


# A character after ``$`` that begins an expansion some other rule owns: ``_`` (a name, the
# unresolved-reference rule), ``{`` (the brace rule), ``(`` (command substitution) and
# ``@ * #`` (the positional rule; a digit or letter is handled the same way). ``! ? $ -``
# are NOT listed: no rule owns them, and ``$!`` expands to nothing before a background job,
# so ``cat ~/.ss$!h/id_rsa`` reads ``.ssh``. They fall to the refusal below.
_CRON_DOLLAR_EXPANSION_START = frozenset("_{(@*#")
# A character after ``$`` that leaves it plainly literal: an anchor or a price.
_CRON_DOLLAR_PLAIN_NEXT = frozenset(" \t\n/|.,:)")


def _has_unplain_dollar(text: str) -> bool:
    """True when *text* has a ``$`` that is neither an expansion nor plainly literal.

    A ``$`` before a quote, a backslash or any other character is not an expansion
    for this shell. Quote removal then joins it to what follows, so a nested shell,
    ``python3 -c`` or ``os.system`` reads ``\\$"1``, ``"$"{X}`` or ``$\\1`` as ``$1``
    or ``${X}``. Each of those spellings was found one review at a time, so the rule
    does not follow quoting at all: it reads the characters on either side of every
    ``$``. Allowed after ``$``: the end of the text, whitespace, ``/ | . , : )``, a
    letter, a digit or ``_ { ( @ * #`` (the last group is another rule's).
    """
    n = len(text)
    for i, ch in enumerate(text):
        if ch != "$":
            continue
        if i + 1 >= n:
            continue
        following = text[i + 1]
        if following in _CRON_DOLLAR_PLAIN_NEXT or following in _CRON_DOLLAR_EXPANSION_START:
            continue
        if following.isascii() and following.isalnum():
            continue
        return True
    return False


def _contains_glob_meta(value: str) -> bool:
    """Recognize wildcard markers without rescanning unmatched bracket suffixes."""
    if "*" in value or "?" in value:
        return True
    opening = value.find("[")
    return opening >= 0 and value.find("]", opening + 1) >= 0


def _extglob_group_end(text: str, start: int) -> int | None:
    """Index just past the ``)`` closing the group whose body starts at *start*.

    A backslash escapes the next character and a quoted span is literal, so
    ``@(")"|ssh)`` has two alternatives (``")"`` and ``ssh``), not one. ``None``
    when the group never closes: bash treats a lone ``(`` as a syntax error, so
    it is not a group.
    """
    depth = 1
    pos = start
    while pos < len(text) and depth > 0:
        ch = text[pos]
        if ch == "\\":
            pos += 2
            continue
        if ch in "'\"":
            close = text.find(ch, pos + 1)
            pos = len(text) if close < 0 else close + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        pos += 1
    return pos if depth == 0 else None


def _extglob_expansions(text: str, _budget: list[int] | None = None) -> list[str | None]:
    """Rewrite every bash extglob group in *text* into plain-glob text.

    Each group becomes a set of plain strings whose union matches at least what
    the group matches, so the caller judges a SUPERSET:

    - ``@(a|b)`` -> ``a``, ``b``; ``?(a|b)`` -> ``""``, ``a``, ``b``.
    - ``+(a|b)`` -> ``a*``, ``b*``; ``*(a|b)`` -> ``""``, ``a*``, ``b*``. One
      occurrence is spelled out, the rest is ``*``. Spelling the first one keeps
      a leading dot explicit, so ``+(.ss)h`` still reaches ``.ssh`` while
      ``grep -E '.+(INFO|WARN)'`` rewrites to ``.INFO*`` and reaches nothing.
    - ``!(…)`` -> ``*``: a negation cannot be enumerated.

    A backslash escapes the next character; an unmatched ``(`` is not a group, so
    only that paren is dropped. A ``None`` entry means the leaf budget ran out.
    """
    if _budget is None:
        _budget = [_EXTGLOB_MAX_EXPANSIONS]
    # Every step spends budget, so a command stacked with thousands of groups
    # or lone `(` ends as a refusal instead of a RecursionError.
    _budget[0] -= 1
    if _budget[0] < 0:
        return [None]
    m = _EXTGLOB_OP_RE.search(text)
    if m is None:
        return [text]
    op_char = text[m.start()]
    pos = _extglob_group_end(text, m.end())
    before = text[: m.start()]
    if pos is None:
        return _extglob_expansions(before + op_char + text[m.end() :], _budget)
    inner = text[m.end() : pos - 1]
    after = text[pos:]
    alts: list[str] = []
    cur: list[str] = []
    depth2 = 0
    idx = 0
    while idx < len(inner):
        ch = inner[idx]
        if ch == "\\":
            cur.append(inner[idx : idx + 2])
            idx += 2
            continue
        if ch in "'\"":
            close = inner.find(ch, idx + 1)
            end = len(inner) if close < 0 else close + 1
            cur.append(inner[idx:end])
            idx = end
            continue
        if ch == "(":
            depth2 += 1
        elif ch == ")":
            depth2 -= 1
        elif ch == "|" and depth2 == 0:
            alts.append("".join(cur))
            cur = []
            idx += 1
            continue
        cur.append(ch)
        idx += 1
    alts.append("".join(cur))
    if op_char == "!":
        # `*` is the superset of what a negation matches; the inner alternatives
        # ride along so a group nested in them is still rewritten.
        alts = ["*", *alts]
    elif op_char in "+*":
        # One occurrence spelled plainly. When the enclosing word is a path
        # (`~` or `/`), one followed by `*` as well, for the repetitions:
        # `~/.+(s)h` -> `~/.s*h` reaches `.ssh`. A bare regex string is not a
        # path, so `grep -E '.*(k|m|g)'` -> `.k` and never `.k*`, which the
        # plain-glob matcher would read as the dotfile `.kube`.
        word = before.rsplit(None, 1)[-1] if before and not before[-1].isspace() else ""
        word += after.split(None, 1)[0] if after and not after[0].isspace() else ""
        if "/" in word or "~" in word:
            alts = alts + [a + "*" for a in alts]
        if op_char == "*":
            alts.insert(0, "")
    elif op_char == "?":
        alts.insert(0, "")
    results: list[str | None] = []
    for alt in alts:
        for item in _extglob_expansions(before + alt + after, _budget):
            results.append(item)
            if item is None:
                return results
    return results


def _extglob_superset(text: str) -> str:
    """Widen every extglob group in *text* to a plain wildcard.

    The result matches at least everything any alternative of any group can
    match, so judging it is sound when the leaf budget of
    ``_extglob_expansions`` ran out. A group that can start with a dot
    (any ``.`` inside it, ``@(a|.ssh)`` or ``@(z|?(x).ssh)``) widens to ``.*``: the plain-glob
    matcher never lets a bare ``*`` reach a dotfile, and the dot must survive.
    Nested groups collapse with their parent.
    """
    out = text
    while (m := _EXTGLOB_OP_RE.search(out)) is not None:
        end = _extglob_group_end(out, m.end())
        if end is None:
            out = out[: m.start()] + out[m.start()] + out[m.end() :]
            continue
        inner = out[m.end() : end - 1]
        # Any dot inside the group may start a segment once nested groups and
        # alternatives resolve (`?(x).ssh`), so the superset keeps it.
        star = ".*" if "." in inner else "*"
        out = out[: m.start()] + star + out[end:]
    return out


#: ``_iter_shell_chars`` reports quote state as an int; the scans below read the
#: quote CHARACTER, which is what makes ``states[i] in (None, ch)`` express "this
#: quote is the delimiter that opens or closes here".
_SHELL_STATE_QUOTE: dict[int, str | None] = {0: None, 1: "'", 2: '"'}


def _quote_states(command: str) -> tuple[list[str | None], list[bool]]:
    """Return per-character ``(quote_state, is_escaped)`` for *command*.

    A THIN ADAPTER over ``security.shell_normalizer._iter_shell_chars``, which is
    THE shell quote/escape machine for this repo. Keeping a second copy here is the
    defect that module was cured of twice, and its docstring records the exact
    escape this file would otherwise get wrong: inside ``$'...'`` a backslash
    escapes, so ``$'a\\'b'`` does not close at the escaped quote. Measured against a
    hand-rolled copy on ``x $'a\\'b' {p,q} y`` — the copy closed early, reopened on
    the next quote, and then disagreed for the whole remainder of the string,
    labelling an UNQUOTED ``{p,q}`` as single-quoted. Ten of seventeen positions
    differed. That mislabelling did not open a hole in the brace rule, because the
    group is mislabelled uniformly and the rule compares a group against its own
    opening state -- but the continuation folding decides whether a
    continuation is literal from these same states, and it runs BEFORE the ``$'``
    refusal, so the desync was reachable.

    ``quote_state[i]`` is the quote the character at ``i`` sits INSIDE (``None``,
    ``"'"`` or ``'"'``). A quote character carries the state it is changing FROM, so
    an opening quote reads as outside and a closing quote as inside — the generator
    reports state AFTER each step, so the previous step's state is what this
    convention needs. ``is_escaped[i]`` marks a character consumed by a preceding
    backslash; the generator hands back an escape as ONE step whose ``text`` is both
    characters, which is what makes the pair identifiable here.
    """
    n = len(command)
    states: list[str | None] = [None] * n
    escaped: list[bool] = [False] * n
    prev = 0
    for step in _iter_shell_chars(command):
        during = _SHELL_STATE_QUOTE[prev]
        if len(step.text) == 2 and step.text[0] == "\\":
            states[step.offset] = during
            if step.offset + 1 < n:
                states[step.offset + 1] = during
                escaped[step.offset + 1] = True
        else:
            states[step.offset] = during
        prev = step.state
    return states, escaped


# The ONLY characters whose bare presence stops bash brace-expanding a group.
# Measured, one word at a time, by echoing it under `bash -c` and `bash +B -c` and
# comparing: space, tab and newline leave the group literal, while form feed
# (\x0c), vertical tab (\x0b), carriage return (\r) and NBSP (\xa0) all still
# expand. ``str.isspace()`` is true for all seven, so using it here fails OPEN —
# the disqualifier is what makes the scan ALLOW, so a too-generous notion of
# whitespace admits a payload rather than over-refusing one.
_BASH_WORD_BREAKING_WHITESPACE = " \t\n"

# A hard ceiling on the WORK the brace scan may do, because the scan is
# quadratic in the worst case and the import path hands it an UNCAPPED string.
# The shape: a long run of `{` with no closing brace at the same state makes the
# inner walk run to end-of-string for every one of them. Measured -- doubling the
# input multiplies the time by ~4 (145 ms at 1k, 572 ms at 2k, 2.3 s at 4k, 9.2 s
# at 8k), so a few hundred KB hangs the process.
#
# A length cap is not enough on its own. ``cron_add`` is capped at 5000 by
# ``validation.FieldSpec("command", max_len=5000)``, but ``portability.py``
# re-vets an imported job with the raw dict value and that cap does not apply
# there -- and 5000 still costs seconds, once per imported job. Bounding the
# STEPS bounds the cost for every shape rather than for one of them, which is the
# same reason ``_CRON_MAX_GLOB_WORD`` bounds the word handed to fnmatch instead of
# bounding the command.
#
# Exhaustion REFUSES (see ``_ScanTooComplex``). Short-circuiting to "clean" would
# convert a denial of service into a bypass, which is the worse of the two.
_BRACE_SCAN_STEP_BUDGET = 1_000_000

# The step budget above bounds the WALK. It does not bound the per-character state
# ``_quote_states`` allocates BEFORE the walk starts, and on the uncapped import path
# named above that allocation is itself the attack: two lists of one entry per
# character cost a measured 16.0 bytes/char (flat at n = 1e4, 1e6, 1e7), so a command
# at the 2 GiB ``_MAX_IMPORT_UNCOMPRESSED`` ceiling asks for 32 GiB and the gateway is
# OOM-killed. A kill is not catchable -- ``_sanitize_imported_crons`` wraps this call
# in ``except Exception``, but the kernel sends SIGKILL rather than raising
# ``MemoryError``, so the drop-the-job path never runs.
#
# So the two bounds are complementary rather than alternatives: length bounds the
# allocation, steps bound the walk, and neither substitutes for the other.
#
# The VALUE is set by a second measurement, and it is why this is not simply "far
# above anything legitimate" in the style of ``_CRON_MAX_GLOB_WORD``. The step budget
# bounds the brace walk and nothing else: a single long WORD with no whitespace is
# quadratic through the rules below, independently of that walk, measured at 133 ms
# for 2k, 781 ms for 8k, 11.4 s for 32k and 178 s for 128k, while the brace shape the
# budget does cover stays flat (156 ms -> 269 ms across the same range). That path is
# PRE-EXISTING -- the base commit measures 129 ms / 757 ms / 11.3 s for the same
# inputs, within noise -- so it is not this change's to fix, but it is this ceiling's
# to survive.
#
# Hence a ceiling just above the storable maximum rather than a generous one. A
# ``command`` is capped at 5000 where it is stored, so 8192 leaves ~1.6x headroom for
# a caller with a different cap while holding the worst case under a second (~780 ms
# measured at 8k) and the allocation near 131 KB. At 64 KiB the same quadratic path
# would cost roughly 45 s per call, which is a bound in name only.
_CRON_MAX_COMMAND_SCAN = 8192


# A word that re-parses a shell STRING: a shell, ``eval``, ``busybox``, or a remote
# or user switch that hands its argument to one (``ssh``, ``su``). An argv wrapper
# (``env``, ``timeout``, ``nice``, ``nohup``, ``flock``, ``xargs``, ``find``) execs its
# argument as a word list and never parses it, so it is not counted: counting it let
# two wrappers open a level no shell ever reaches and refuse an escaped BRE interval.
# A wrapper that does reach a shell names that shell (``xargs sh -c``, ``env bash -c``,
# ``find -exec sh -c``), and that word is what counts. The list gates only the brace
# scan's THIRD and deeper levels, each needing one more match, so a shell spelled
# outside it is a miss only for a group hidden behind two or more nested re-parses.
_CRON_NESTED_REPARSER_RE = re.compile(
    r"(?:^|[\s;&|()`/])(?:sh|bash|dash|zsh|ksh|ash|busybox|eval|ssh|su)(?=$|[\s;&|()`])"
)


class _ScanTooComplex(Exception):
    """The brace scan hit its step budget, so no verdict was reached."""


def _strip_shell_quotes(command: str) -> str:
    """Return *command* as a NESTED shell receives it, after quote removal.

    ``bash -c "cat ~/.ss{h","h}/x"`` concatenates two double-quoted runs, so the
    comma sits outside both while the braces sit inside — verified, the inner shell
    receives ``cat ~/.ss{h,h}/x`` and prints the expansion. Scanning this projection
    is what catches the spelling where quoting splits one group across several quote
    states.

    Backslash removal is QUOTE-STATE DEPENDENT, because the shell's own quote
    removal is, and getting this wrong in either direction is a hole:

    - **Unquoted** backslash — the shell's escape character. Quote removal deletes
      it, so the inner shell sees the escaped character BARE. Measured:
      ``/bin/sh +B -c '/bin/sh -c "echo "p{x\\,x}q'`` prints ``pxq pxq``, the same
      as the unescaped control, and ``"echo "p\\{x,x}q`` prints it too. Keeping the
      backslash here reads both as separator-free.
    - **Inside double or single quotes** — a backslash before ``{`` or ``,`` is not
      special, so it survives into the inner shell and the word stays literal.
      Measured: ``bash -c 'bash -c "echo \\{a,b\\}"'`` prints ``{a,b}``. Removing it
      here would refuse a group no shell in the chain ever expands.

    Brace expansion runs BEFORE quote removal, which is why an unquoted escape is
    only a separator to a shell that parses the word a SECOND time. Level 1 scans
    the command as written and leaves those literal; this projection is what models
    the second parse, so it is deliberately stricter — see the over-refusal rows in
    ``_BRACE_SHAPES_MEASURED_AGAINST_BASH``.
    """
    states, escaped = _quote_states(command)
    out = []
    for i, ch in enumerate(command):
        if not escaped[i] and ch in ("'", '"') and states[i] in (None, ch):
            continue  # an opening (state None) or closing (state == ch) delimiter
        if not escaped[i] and ch == "\\" and states[i] is None:
            continue  # unquoted escape: quote removal deletes it before the inner parse
        out.append(ch)
    return "".join(out)


def _scan_one_level(command: str, budget: list[int] | None = None) -> bool:
    """True when *command* holds a brace expansion at its own parse level.

    Every character's meaning depends on the quote state it sits in, which is not
    a regular property of the surrounding text — hence a scan rather than a
    pattern. The state at the OPENING BRACE is the reference: the closing brace,
    the separator (``,`` or ``..``) and any disqualifying whitespace each count
    only when unescaped and in that same state. Whitespace in a DIFFERENT state is
    nested quoting inside the group, which is precisely the case bash expands.

    Two separate reasons the first ``}`` is not the end of the group, and each is
    load-bearing rather than tidiness — without either, a group reads as
    separator-free and the command is stored:

    - **Nesting is counted.** ``{{x}h,h}`` expands (verified,
      ``echo p{{x}s,s}q`` -> ``p{x}sq psq``), so an inner ``}`` closes the inner
      group, not the outer one. That is what the depth counter is for.
    - **A separator-free ``}`` at depth 0 does not close either.** bash keeps
      hunting for a later ``}`` that does have a depth-0 separator before it and
      treats this one as ordinary text: verified, ``echo p{x},x}q`` ->
      ``px}q pxq``, so the group bash uses is ``{x},x}``. This needs no quoting,
      no escape and no nesting, so it is the cheapest spelling of the three.

    Two edges of the over-approximation, both measured, both refusing rather than
    allowing, and neither modelled here:

    - **Quote state is the quote CHARACTER, not the identity of one quoted run.**
      A group opened inside ``'…'`` skips every byte in a different state — the
      spaces between arguments, and ``|``, ``;``, ``&&`` — and a LATER
      single-quoted region reads as the same state, so it can supply the
      separator and the closing brace. That refuses
      ``awk '{print}' f | grep ',}'`` and ``jq -c '{a:.b}' f | sed 's/,/}/'``,
      which bash leaves literal at both levels. It needs one more ``}`` than
      ``{`` and a later ``,``, which is what keeps the class narrow; a realistic
      corpus including ``docker ps --format '{{.ID}},{{.Names}}'`` is unaffected,
      because a trailing ``}}`` decrements depth and never reaches a depth-0 close.
    - **bash declines the retry when the word STARTS with ``{}``**: ``{},}`` is
      literal while ``a{},}b`` expands. This scan refuses both.
    """
    states, escaped = _quote_states(command)
    n = len(command)
    # Bounded because the walk below is quadratic on a hostile shape and one entry
    # point receives an uncapped string. See ``_BRACE_SCAN_STEP_BUDGET``. A caller
    # scanning several projections passes ONE budget so the bound covers them all.
    if budget is None:
        budget = [_BRACE_SCAN_STEP_BUDGET]
    for start, ch in enumerate(command):
        if ch != "{" or escaped[start]:
            continue
        state = states[start]
        sep = False
        bare_ws = False
        closed = False
        depth = 0
        j = start + 1
        while j < n:
            budget[0] -= 1
            if budget[0] <= 0:
                raise _ScanTooComplex(f"brace scan exceeded {_BRACE_SCAN_STEP_BUDGET} steps")
            if escaped[j] or states[j] != state:
                # Escaped, or nested inside a quote the brace itself is not in:
                # bash expands these, so they neither close the group nor
                # disqualify it.
                j += 1
                continue
            cur = command[j]
            if cur == "{":
                depth += 1
            elif cur == "}":
                if depth == 0:
                    if sep:
                        closed = True
                        break
                    # bash does NOT close a separator-free group here: it keeps
                    # hunting for a later `}` that does have a depth-0 separator
                    # before it, and treats this one as ordinary text. Verified,
                    # `echo p{x},x}q` -> `px}q pxq`, so the group bash uses is
                    # `{x},x}`. Closing at the first `}` regardless of `sep` read
                    # that as separator-free and stored it.
                    j += 1
                    continue
                depth -= 1
            elif depth == 0 and cur == ",":
                sep = True
            elif (
                depth == 0
                and cur == "."
                and j + 1 < n
                and command[j + 1] == "."
                and not escaped[j + 1]
                and states[j + 1] == state
            ):
                sep = True
            elif cur in _BASH_WORD_BREAKING_WHITESPACE:
                bare_ws = True
            j += 1
        # An unterminated group expands to nothing; there is nothing to refuse.
        if closed and sep and not bare_ws:
            return True
    return False


def _has_bash_brace_expansion(command: str) -> bool:
    """True when *command* contains a bash brace expansion (``{a,b}``/``{1..9}``).

    The rule is measured against real bash, not read off the grammar. Every row
    below was run; the middle column is what bash actually did::

        {x,"x x"}    EXPANDS   a quoted space does not break it
        {x,'x x'}    EXPANDS   same, single quotes
        {x,$'x x'}   EXPANDS   same, ANSI-C quoting
        {x,x\\ x}     EXPANDS   same, backslash-escaped space
        {"x","x x"}  EXPANDS   comma between two quoted words
        {{x}h,h}     EXPANDS   nested group, separator only at depth 0
        {x,x\\x0cy}   EXPANDS   form feed, vertical tab, CR and NBSP all do
        {x,x x}      literal   a BARE space breaks it
        {x,x<TAB>x}  literal   same for tab and newline, and ONLY those
        {x","x}      literal   a quoted comma is not a separator HERE...
        {x\\,x}       literal   same, escaped
        \\{x,x\\}      literal   escaped braces
        '{'x,x'}'    literal   quoted braces

    ...but the last three rows are about THIS parse level only, and the string
    reaches more than one parser. These levels are scanned, and any one refuses:

    1. the command as written, for the shell that runs the cron;
    2. the command with quote delimiters removed, which is what a nested shell
       receives — verified, ``bash -c "cat ~/.ss{h","h}/x"`` hands the inner shell
       ``cat ~/.ss{h,h}/x``, which expands;
    3. each further quote-removal projection, until removal changes nothing, but
       level N only when its own text names at least N-1 shell re-parsers
       (``_CRON_NESTED_REPARSER_RE``).

    Levels 1 and 2 are unconditional. The separator there sits OUTSIDE the
       quotes while the braces sit inside, so no single-level rule can see it.

    The DOUBLE-quoted spellings in the table are refused for cause, not merely out
    of caution: verified, ``p{x","x}s`` and ``p"{"x,x"}"s`` both reach an inner
    shell as ``p{x,x}s`` and expand there. The genuine over-refusal is the SINGLE-
    quoted group, because single quotes survive one level of double-quoted nesting —
    verified, the inner shell receives them intact and leaves the group literal.

    THE COST, STATED PLAINLY, because it is larger than "a contrived shape". Nothing
    on the base branch refuses a bash brace expansion at all, so every refusal here
    is new, and the class users will actually hit is not ``awk '{a,b}'`` but the
    quoted regex INTERVAL: ``grep -E '[0-9]{1,3}'``, ``sed -E 's/x{2,4}//'``. Those
    are refused, and ``portability.py``'s import path re-vets with this same
    function and DROPS a job it refuses, so an exported job carrying one is not
    importable. The drop is audited rather than silent, which is the only thing that
    makes it acceptable.

    An accurate remedy exists for BRE tools and is named in the error text:
    ``grep "[0-9]\\{1,3\\}"`` is accepted — a backslash inside DOUBLE quotes escapes
    the brace for this scan and is passed through to the tool, where BRE reads
    ``\\{m,n\\}`` as the interval (verified both halves). It is not a remedy for ERE,
    where ``\\{`` means a literal brace, so an ERE interval belongs in a ``script``
    job. Levels 1 and 2 refuse rather than enumerate which commands re-parse their
    arguments: an enumeration of shell-invoking spellings (``sh -c``, ``xargs``,
    ``find -exec``, ``env``, ``timeout``, ``busybox``…) fails OPEN on the one nobody
    listed, and this scan is the only rule covering that class. Levels 3 and deeper
    consult such a list (``_CRON_NESTED_REPARSER_RE``), one named shell per extra
    level, because an ungated third level strips the BRE backslashes above. The residual is a group hidden behind two or more nested
    re-parses at least one of which is unlisted.

    The common shapes are unaffected: ``awk '{print x, y}'`` and
    ``jq '{a: .x, b: .y}'`` survive quote removal with their bare spaces intact.
    """
    budget = [_BRACE_SCAN_STEP_BUDGET]
    level = command
    depth = 1
    # Each nested shell strips one more layer, so a group can stay hidden for
    # any fixed number of levels: ``bash -c "bash -c cat\ p{x\,x}q"`` is clean at
    # levels 1 and 2 and expands at the third parse. Scan every projection until
    # quote removal stops changing the text. Each unchanged-or-shorter step
    # removes at least one character, so this ends; the shared budget bounds it.
    #
    # Levels 1 and 2 are always scanned. Level N past that models N-1 nested
    # re-parses, so it is taken only when that level's own text names at least N-1
    # re-parsers: depth is gated by evidence of that many parses. Without the gate the walk
    # strips the backslashes of ``grep "[0-9]\{1,3\}"`` -- the documented BRE
    # spelling, which reaches grep as ``[0-9]\{1,3\}`` and is never parsed again --
    # and refuses it; with a presence test instead of a count, one launcher such as
    # ``timeout 60 grep ...`` would refuse it the same way.
    while True:
        if _scan_one_level(level, budget):
            return True
        stripped = _strip_shell_quotes(level)
        if stripped == level:
            return False
        # Count on the projection about to be entered: a re-parser spelled so the
        # current level hides it (``\bash``, ``b""ash``) is a plain word there.
        if depth >= 2 and len(_CRON_NESTED_REPARSER_RE.findall(stripped)) < depth:
            return False
        depth += 1
        budget[0] -= len(level)
        if budget[0] <= 0:
            raise _ScanTooComplex(f"brace scan exceeded {_BRACE_SCAN_STEP_BUDGET} steps")
        level = stripped


def _glob_could_reach_credentials(command: str) -> bool:
    """True when a glob in *command* could expand onto a credential path.

    Pathname expansion composes a path the literal text never contains:
    ``cat ~/.s?h/id_rsa`` reads ``~/.ssh/id_rsa``. Rather than refuse every
    ``*``/``?``/``[`` — which would break ordinary crons like ``rm /tmp/*.log``
    — each glob-bearing word is tested AS A GLOB against the sensitive names.

    Matching rather than substituting is what makes this exact and independent of
    how many metacharacters the word carries. Substituting one metacharacter at a
    time missed ``~/.??h/id_rsa`` (two wildcards, neither alone landing on a
    literal ``.ssh``), and substituting all of them combinatorially is
    exponential on hostile input. ``fnmatch`` decides the whole word in one pass.
    """
    if not _contains_glob_meta(command):
        return False
    for word in command.split():
        if not _contains_glob_meta(word):
            continue
        # fnmatch compiles the pattern to a regex, which is superlinear on a
        # pathological one (`cat ????...` x 20k measured 8.2s), and this runs
        # inline in the cron_add call, so an unbounded pattern is a denial of the
        # tool. Past the bound, FAIL CLOSED: refuse rather than skip. Skipping was
        # fail-OPEN and defeated the whole check — a long prefix of junk followed
        # by `/../../.s?h/id_rsa` pushed the word over the bound and sailed
        # through. No legitimate cron one-liner has a single word this long (no
        # sensitive name is within an order of magnitude of it), so refusing is
        # the safe side of a bound that exists only to cap pathological input.
        if len(word) > _CRON_MAX_GLOB_WORD:
            return True
        # Strip shell decoration that is not part of the path: quotes (all of
        # them: sh removes every quote in the word), a
        # leading redirection/flag, and a trailing separator or list punctuation.
        candidate = word.replace('"', "").replace("'", "").lstrip("<>|&").rstrip(";&|")
        candidate = candidate.replace("\\", "/")
        # Collapse `.` and `..` segments BEFORE matching. A traversal reaches the
        # same file by a longer route — `~/junk/../../.s?h/id_rsa` is `~/.s?h/...`
        # once resolved — so a scan that compares only the leading segments would
        # measure `junk` and miss it entirely. Resolved lexically (no filesystem
        # access): the target need not exist for the glob to be judged, and
        # touching the disk from a vetting gate would be its own hazard.
        segments: list[str] = []
        for part in candidate.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if segments:
                    segments.pop()
                continue
            segments.append(part)
        candidate = "/".join(segments)
        for prefix in ("~/", "$HOME/", "${HOME}/"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :]
                break
        # A leading `~` or `$HOME` survives as its own segment once the path is
        # split, so drop it there too.
        if segments and segments[0] in ("~", "$HOME", "${HOME}"):
            candidate = "/".join(segments[1:])
        if not candidate:
            continue
        cand_segments = candidate.lower().split("/")
        for sensitive in _SENSITIVE_HOME_DIRS:
            probe = sensitive.lower()
            depth = probe.count("/") + 1
            # Slide a `depth`-wide, segment-aligned window across the WHOLE
            # candidate, not just its leading segments. A sensitive name is a
            # home-relative fragment (`.ssh`), so an ABSOLUTE path reaches it at
            # a deeper offset — `/home/alice/.s?h/id_rsa` has `.s?h` at index 2,
            # not 0 — and matching only the leading run missed every absolute
            # form. `~/.s?h/id_rsa` still matches at offset 0 (home already
            # stripped above). Both fnmatch directions so a glob in EITHER the
            # command or the sensitive name is caught.
            for start in range(len(cand_segments) - depth + 1):
                win_segs = cand_segments[start : start + depth]
                # sh does NOT let a leading `*`/`?`/`[` match a leading dot — a
                # hidden file is excluded from globbing unless the pattern spells
                # the dot literally. Every sensitive name here is a dotfile
                # (`.ssh`, `.kube`, ...), so a window segment that begins with a
                # wildcard cannot reach the corresponding probe segment. Skipping
                # these windows is what keeps `~/*/config` (cannot reach
                # `.kube/config`) benign while `~/.s?h/...` (dot spelled out)
                # still matches. Verified against real sh.
                probe_segs = probe.split("/")
                if any(
                    ws[:1] in ("*", "?", "[") and ps[:1] == "."
                    for ws, ps in zip(win_segs, probe_segs)
                ):
                    continue
                window = "/".join(win_segs)
                if not (fnmatch.fnmatch(window, probe) or fnmatch.fnmatch(probe, window)):
                    continue
                # A window made only of wildcards/separators matches EVERY
                # sensitive name, so a benign `~/projects/*/dist` would "match"
                # `.aws`. That is not targeting — a glob is evidence only when it
                # shares a literal, non-wildcard character with the sensitive
                # name it lines up against. Requiring one overlapping literal
                # keeps `.s?h`/`.ss*` (which carry `.`, `s`, `h`) while dropping a
                # standalone `*`.
                literals = set(window) - set("*?[]/")
                if literals & set(probe):
                    return True
    return False
