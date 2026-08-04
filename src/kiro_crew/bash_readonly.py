"""Read-only bash command classification.

Extracted from ``dashboard/state.py`` so leaf modules can reuse it without
importing the dashboard package. ``hooks.py`` (the tool-approval pre-rung that
every surface consults) needs this predicate for plan mode, and importing
``kiro_crew.dashboard.state`` from there would pull aiohttp and the whole
dashboard graph into a security leaf.

``dashboard/state.py`` re-exports the three public names, so existing callers
and tests that import them from there keep working.

Deny-by-default: anything not positively recognized as read-only is unsafe.

Scope, stated plainly: the command allowlist is exhaustive (an unlisted command
is denied), but the per-command write-flag table below is ENUMERATIVE — a novel
write-causing flag on an allowlisted command is a gap until it is added. This is
a best-effort look-before-you-leap boundary, not a sandbox; process isolation and
the sensitive-path floor remain the hard controls.
"""

from __future__ import annotations

import re
import shlex

_READ_ONLY_BASH_PREFIXES: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "wc",
    "which",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "diff",
    "pwd",
    "echo",
    "date",
    "whoami",
    "hostname",
    "uname",
    "readlink",
    "realpath",
    "basename",
    "dirname",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git rev-parse",
    "git describe",
    "git ls-files",
    "git ls-tree",
    "git cat-file",
    "git blame",
    "brazil ws show",
    "brazil ws list",
    "brazil workspace show",
    "brazil workspace list",
    "brazil versionset print",
    "brazil versionset show",
    "brazil-path",
    "python --version",
    "python3 --version",
    "node --version",
    "java -version",
    "javac -version",
)

#: Commands whose BARE form only reads but whose flags mutate, so they cannot be
#: prefix-matched: ``git branch -D`` destroys a branch, ``git tag -d`` deletes a
#: tag, and ``git remote set-url`` repoints the push target. These are matched on
#: the WHOLE command instead, so no flag can ride along.
_READ_ONLY_EXACT: frozenset[str] = frozenset(
    {
        "git branch",
        "git branch -a",
        "git branch -r",
        "git branch -v",
        "git branch -vv",
        "git branch -l",
        "git branch --list",
        "git branch --all",
        "git branch --show-current",
        "git tag",
        "git tag -l",
        "git tag --list",
        "git tag -n",
        "git remote",
        "git remote -v",
        "git remote --verbose",
        "git remote show",
        "git stash list",
    }
)

#: Flags that turn an otherwise read-only command into a file write. ``sort -o``
#: and ``tree -o`` write to an arbitrary path with no shell redirect, so
#: ``_UNSAFE_SHELL_RE`` never sees them. Keyed per command because the same
#: letter is harmless elsewhere: ``grep -o`` prints only the match, and
#: ``git ls-files -o`` lists untracked files — which is why git bans only the
#: long ``--output`` form.
_WRITE_FLAGS: dict[str, frozenset[str]] = {
    "sort": frozenset({"-o", "--output"}),
    "tree": frozenset({"-o", "--output"}),
    "date": frozenset({"-s", "--set"}),
    "du": frozenset({"-o", "--output-file"}),
    "git": frozenset({"--output"}),
    # `hostname -F FILE` / `--file` reads the new name FROM a file and sets it,
    # and `-b` / `--boot` sets one too. The operand cap above already catches the
    # detached spellings (their value counts as an operand), but the ATTACHED
    # short form `-F/tmp/name` looks like a flag, so it needs an entry here --
    # the same attached-argument case `-oFILE` is handled for.
    "hostname": frozenset({"-F", "--file", "-b", "--boot"}),
    # `file -C` COMPILES the magic file, writing `<magicfile>.mgc` to disk (to the
    # cwd when no `-m` is given). Every other `file` flag only reports.
    "file": frozenset({"-C", "--compile"}),
}

#: ``<one bare command> --help|--version`` and nothing else. Two escapes had to
#: be closed here, and the second is why the head is checked separately below.
#: The original form accepted ANY command ENDING in the flag, which made
#: ``bash -c '<arbitrary>' --help`` read-only: the shell runs the payload and
#: ``--help`` merely lands in ``$0``. Anchoring both ends closed that.
#: Anchoring alone is still not enough, because ``foo --help`` EXECUTES ``foo``:
#: ``./destructive-script --help`` matched, and a script is free to ignore the
#: flag and do whatever it likes. So the shape match is necessary but not
#: sufficient — see :func:`_is_help_probe`.
_HELP_ONLY_RE = re.compile(r"^[\w./+-]+\s+--(?:help|version)$")

#: Command words the help/version form may be used with: the head of every
#: allowlisted whole command and prefix. Derived from the allowlists rather than
#: written out again, so a command added there cannot be silently omitted here
#: (or, worse, a word allowed here that the classifier does not otherwise trust).
_HELP_ALLOWED_HEADS: frozenset[str] = frozenset()  # populated below


def _is_help_probe(normalized: str, *, strict: bool) -> bool:
    """True when *normalized* is a help/version probe we are willing to run.

    A path-bearing executable is NEVER a probe, in either mode:
    ``./destructive-script --help`` runs a script that is free to ignore the flag
    entirely, and the path makes it attacker-placeable rather than PATH-resolved.

    ``strict`` additionally requires the head to be a command the classifier
    already trusts. Plan mode asks for that, because its promise is that nothing
    changes; trust-reads does not, because auto-approving ``python --version`` on
    an arbitrary PATH tool is its pre-existing (and narrower-consequence)
    bargain, and tightening it belongs in its own change.
    """
    if not _HELP_ONLY_RE.match(normalized):
        return False
    head = normalized.split()[0]
    if "/" in head:
        return False
    if strict:
        return head in _HELP_ALLOWED_HEADS
    return True


# Derived once, after both allowlists exist: the first word of every allowlisted
# whole command and prefix.
_HELP_ALLOWED_HEADS = frozenset(
    {c.split()[0] for c in _READ_ONLY_EXACT if c.split()}
    | {p.split()[0] for p in _READ_ONLY_BASH_PREFIXES if p.split()}
)


_READ_ONLY_PIPE_RE = re.compile(
    r"^\s*(grep|egrep|fgrep|head|tail|wc|sort|uniq|cut|less|more|cat)\b"
)

# Reject redirections, command/process substitution, backgrounding, and BRACE
# EXPANSION — conservative, and the brace case is load-bearing rather than
# stylistic: the shell expands `-{u,o/tmp/file}` into `-u -o/tmp/file` before
# execution, so every per-token check below (write-flag table, operand cap,
# allowlist head) would be inspecting a token that never runs. One brace pair is
# enough to smuggle any flag or path past all of them.
_UNSAFE_SHELL_RE = re.compile(r">|`|\$\(|<\(|(?<!&)&(?!&)|[{}]")

# Discard-only redirect idioms that are read-only despite containing '>'/'&':
# `2>/dev/null`, `>/dev/null`, `&>/dev/null`, `2>>/dev/null`, and `2>&1`.
# These sink or merge output, never writing a real file, so they must be
# stripped before _UNSAFE_SHELL_RE — otherwise every `find … 2>/dev/null`
# falls through to an interactive prompt. A redirect to any real path
# (e.g. `cmd > out.txt`) still trips _UNSAFE_SHELL_RE and stays unsafe.
# The `(?![\w./-])` guard pins the match to the literal device `/dev/null`:
# without it, `>/dev/nullx` or `>/dev/null/../etc/passwd` would be scrubbed as
# a sink, smuggling a real-file write past the unsafe-shell check.
_DEVNULL_REDIR_RE = re.compile(r"(?:\d*>>?|&>)\s*/dev/null(?![\w./-])|\d*>&\d+")


def _classify_bash(cmd: str, *, strict_help: bool = False) -> str:
    """Single source of truth for read-only bash classification.

    Returns "" when the command is read-only, otherwise a human-readable
    reason it was rejected. :func:`is_read_only_bash` and
    :func:`unsafe_bash_reason` both delegate here so the two can never
    diverge — the invariant "reason is non-empty iff not read-only" holds
    by construction rather than by parallel maintenance. Deny-by-default.
    """
    if not cmd.strip():
        return "empty command"
    # Strip discard-only redirects (output sinks / stderr-merge) before the
    # unsafe-shell check; they are read-only but contain '>' / '&'.
    scrubbed = _DEVNULL_REDIR_RE.sub(" ", cmd)
    if _UNSAFE_SHELL_RE.search(scrubbed):
        return (
            "unsafe shell pattern (redirect, command/process substitution, "
            "backgrounding, or brace expansion)"
        )
    # Reject quoting the shell itself could not resolve. A fragment that fails
    # to parse later (a '|' inside quotes) falls back to a whitespace split;
    # this check means such a fallback can never be the only thing standing
    # between an unbalanced command and the write-flag table.
    try:
        shlex.split(cmd, posix=True)
    except ValueError:
        return "unbalanced quoting"
    parts = re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd.strip())
    for part in parts:
        if not part.strip():
            continue
        pipe_parts = [p.strip() for p in part.split("|") if p.strip()]
        if not pipe_parts:
            return "unsafe shell pattern"
        first = pipe_parts[0].strip().lower()
        normalized = " ".join(first.split())
        cased = " ".join(pipe_parts[0].strip().split())
        if not (
            _is_help_probe(normalized, strict=strict_help)
            or normalized in _READ_ONLY_EXACT
            or any(first == p or first.startswith(p + " ") for p in _READ_ONLY_BASH_PREFIXES)
        ):
            base = first.split()[0] if first.split() else first
            return f"command '{base}' is not on the read-only allowlist"
        # The write checks get the CASE-PRESERVING form. `normalized` is
        # lowercased for allowlist matching, but short flags are case-sensitive
        # and two of them differ from a read flag by case ALONE: `hostname -F`
        # (set the name from a file) vs `-f` (print the FQDN), and `file -C`
        # (compile the magic file to disk) vs `-c`. Matching the flag table
        # against lowercased text silently misses every uppercase write flag.
        write_flag = _writing_flag(cased)
        if write_flag:
            return f"'{write_flag}' writes a file"
        operand = _writing_operand(cased)
        if operand:
            return operand
        argv_reason = _unverifiable_argv(cased)
        if argv_reason:
            return argv_reason
        for target in pipe_parts[1:]:
            if not _READ_ONLY_PIPE_RE.match(target):
                tgt = target.split()[0] if target.split() else target
                return f"pipe target '{tgt}' is not a read-only filter"
            target_cased = " ".join(target.split())
            target_flag = _writing_flag(target_cased)
            if target_flag:
                return f"pipe target flag '{target_flag}' writes a file"
            target_operand = _writing_operand(target_cased)
            if target_operand:
                return f"pipe target {target_operand}"
            target_argv = _unverifiable_argv(target_cased)
            if target_argv:
                return f"pipe target {target_argv}"
    return ""


#: Commands that write their SECOND positional operand instead of stdout, so no
#: flag appears anywhere on the line. ``uniq [INPUT [OUTPUT]]`` is the one on the
#: read-only allowlists: ``uniq - /tmp/x`` and ``uniq in out`` both create a
#: file. The sibling filters are safe here because their extra operands are all
#: additional INPUTS (``sort a b`` merges, ``cat a b`` concatenates), which is
#: why this is a per-command rule and not a blanket cap on operand count.
#:
#: ``date`` and ``hostname`` are capped at ZERO for a different reason: they are
#: allowlisted by PREFIX, and both mutate HOST state through a positional operand
#: rather than a flag. ``hostname new-name`` sets the hostname, and BSD/macOS
#: ``date MMDDhhmm`` sets the system clock -- neither is visible to the write-flag
#: table, which only knows ``date -s`` / ``--set``. Both need privilege to bite,
#: which a service-installed gateway has. Every display form is a flag or a
#: ``+FORMAT`` (see ``_READ_ONLY_OPERAND_PREFIXES``), so a zero cap costs nothing
#: except a flag's separate value argument (e.g. ``date -r 1234567890``), which
#: is miscounted as an operand and therefore denied -- failing closed.
_WRITE_OPERAND_MAX: dict[str, int] = {"uniq": 1, "date": 0, "hostname": 0}

#: Operand prefixes that are read-only for a specific command, excluded from the
#: cap above. ``date +%Y-%m-%d`` is a display format, not a value being set --
#: without this the cap would take the most common read form with it.
_READ_ONLY_OPERAND_PREFIXES: dict[str, tuple[str, ...]] = {"date": ("+",)}


def _argv(command: str) -> list[str]:
    """Split *command* into the argv the shell will actually execute.

    ``str.split`` is not enough. The shell removes quotes and backslash escapes
    BEFORE the command runs, so ``sort "-o/tmp/x"`` executes ``sort -o/tmp/x``
    while a literal-token check sees ``"-o/tmp/x"`` — a string starting with a
    quote, not a dash, so it matches no entry in the write-flag table and is not
    even counted as a flag by the operand cap. Same defeat as brace expansion:
    the executed argv differs from the written tokens.

    Unlike braces, quoting cannot simply be rejected — ``grep "a b"`` and
    ``git log --format="%h"`` are ordinary read-only commands — so the argv is
    parsed instead of banned.

    Falls back to a whitespace split when the fragment cannot be parsed, which
    happens when :func:`_classify_bash` splits a command at a ``|`` inside
    quotes (``grep "a|b" f``). That fragment is not a runnable command, and the
    whole-command balance check in :func:`_classify_bash` rejects genuinely
    unbalanced input before it gets here.
    """
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _writing_operand(command: str) -> str:
    """Return why *command*'s positional operands write a file, or "".

    Counts only non-flag tokens. A flag's separate argument would be
    miscounted as an operand, but every flag in the allowed filters' write-free
    set is either boolean or attached, and an unknown flag taking a value can
    only push the count UP — that fails closed, which is the safe direction.
    """
    tokens = _argv(command)
    if not tokens:
        return ""
    # The executable is matched case-INSENSITIVELY: the allowlist lowercases it,
    # so `GIT diff` reaches this code and must still find the `git` rules. The
    # OPTIONS keep their case, because `-F` / `-C` are write flags whose lowercase
    # spellings (`-f`, `-c`) are ordinary reads.
    head = tokens[0].lower()
    limit = _WRITE_OPERAND_MAX.get(head)
    if limit is None:
        return ""
    read_only_prefixes = _READ_ONLY_OPERAND_PREFIXES.get(head, ())
    operands = [
        t
        for t in tokens[1:]
        if (not t.startswith("-") or t == "-")
        and not (read_only_prefixes and t.startswith(read_only_prefixes))
    ]
    if len(operands) > limit:
        return f"'{head}' writes its operand '{operands[limit]}'"
    return ""


def _writing_flag(command: str) -> str:
    """Return the write-causing flag present in *command*, or "".

    Checked per command name: the same option letter is harmless elsewhere, so a
    blanket ban on ``-o`` would reject ``grep -o``. Matches three spellings —
    separate (``-o FILE``), attached-with-equals (``--output=FILE``), and
    attached short (``-oFILE``) — because a short option's argument may be
    glued on, which an exact-match check would miss entirely.
    """
    tokens = _argv(command)
    if not tokens:
        return ""
    # Executable case-insensitive, options case-SENSITIVE -- see _writing_operand.
    flags = _WRITE_FLAGS.get(tokens[0].lower())
    if not flags:
        return ""
    for token in tokens[1:]:
        for flag in flags:
            if token == flag or token.startswith(flag + "="):
                return flag
            # Attached short-option argument: -oFILE. Long flags are excluded
            # because "--outputfoo" is a different (unknown) option, not
            # --output with a glued value.
            if len(flag) == 2 and not flag.startswith("--") and token.startswith(flag):
                return flag
    return ""


#: Characters through which the shell rewrites a token into something else before
#: the command runs: glob metacharacters and parameter expansion. Deliberately NOT
#: in ``_UNSAFE_SHELL_RE`` -- globbing and `$VAR` are ordinary in reads (``ls *.py``,
#: ``cat $HOME/.bashrc``); see :func:`_unverifiable_argv` for the scoping.
_ARGV_REWRITE_RE = re.compile(r"[*?\[$]")


def _unverifiable_argv(command: str) -> str:
    """Return why *command*'s argv cannot be checked for a write, or "".

    Same family as brace expansion and quote removal: the shell rewrites the argv
    AFTER every per-token check, so the string inspected here is not the one that
    executes. Three forms reach it, and each defeats a DIFFERENT check:

    * ``sort *`` in a directory holding a file named ``-o`` runs
      ``sort -o victim`` -- defeats the write-flag table.
    * ``uniq [ab]`` with files ``a`` and ``b`` runs ``uniq a b``, and ``uniq``
      writes its SECOND operand -- defeats the operand cap, which counted one
      operand where two will exist.
    * ``sort $IFS-o$IFS /tmp/victim`` expands to ``sort -o /tmp/victim`` --
      defeats both, since the write flag is not a token here at all.

    Rejecting these characters outright is not an option: ``ls *.py``,
    ``cat *.txt`` and ``cat $HOME/.bashrc`` are ordinary read-only commands. So
    the rule is scoped to the commands that can actually write -- those with a
    write FLAG (``_WRITE_FLAGS``) or a writing OPERAND (``_WRITE_OPERAND_MAX``).
    For those, an argv that cannot be enumerated is the difference between a read
    and a truncate; everywhere else the expansion is harmless and stays allowed.

    Planting an ``-o`` filename needs a write, which plan mode denies -- but
    trust-reads permits the write and then auto-approves this "read" with no
    permission prompt, so the vector is reachable there.
    """
    tokens = _argv(command)
    if not tokens:
        return ""
    head = tokens[0].lower()
    if head not in _WRITE_FLAGS and head not in _WRITE_OPERAND_MAX:
        return ""
    for token in tokens[1:]:
        hit = _ARGV_REWRITE_RE.search(token)
        if hit:
            return (
                f"'{head}' argument '{token}' contains shell expansion "
                f"'{hit.group()}', so its real argv cannot be checked for a write"
            )
    return ""


def is_read_only_bash(cmd: str, *, strict_help: bool = False) -> bool:
    """Check if a bash command is read-only. Deny-by-default."""
    return _classify_bash(cmd, strict_help=strict_help) == ""


def unsafe_bash_reason(cmd: str, *, strict_help: bool = False) -> str:
    """Human-readable reason a bash command failed read-only classification.

    Used to make rejection messages specific ("unsafe shell pattern …")
    instead of the generic adapter default ("User refused permission to run
    tool"). Returns "" when the command IS read-only (no reason to reject on
    safety grounds).
    """
    return _classify_bash(cmd, strict_help=strict_help)
