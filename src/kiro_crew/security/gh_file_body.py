"""gh's body-from-file flags, read on shell words (the ``data-exfil-gh-file-body`` row).

The exfil gate (:func:`kiro_crew.security.exfil_bash.audit_bash_exfiltration`) and the
``is_denied`` floor both call :func:`_gh_file_body_flag`.
"""

from __future__ import annotations

import re

from .argv_rules import _program_args
from .shell_commands import ShellWord as _ShellWord
from .shell_commands import read_commands as _read_commands
from .shell_normalizer_escapes import _decode_ansi_c_body

# gh reads a request body from a LOCAL FILE through flags the exfil gate's substring
# table does not name: `gh api --input <file>`, `--field k=@<file>`, and
# `gh release create|edit --notes-file <file>` (`-F <file>`). They are matched on
# shell words, and only where `gh` is a command's first word, so text that only
# mentions a flag (`echo …`, `grep …`, a later command's own `--input`) is left
# alone. Stdin (`-`, `k=@-`) names no file and stays allowed. `-f`/`--raw-field`
# never reads a file.
_GH_FILE_BODY_RULE = "data-exfil-gh-file-body"
_GH_FILE_BODY_SUBCOMMANDS = frozenset({"api", "repo", "release"})
#: gh global flags that take the next word as their value.
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
#: Per subcommand, the gh options whose next word is a plain value (gh's own
#: flag tables; a short letter means different things per subcommand: `-d` is
#: `repo --description` but `release --draft`).
_GH_VALUE_OPTIONS: "dict[str, frozenset[str]]" = {
    "api": _GH_VALUE_FLAGS
    | frozenset(
        {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-q", "--jq"}
        | {"-t", "--template", "-p", "--preview", "--cache"}
    ),
    "repo": _GH_VALUE_FLAGS
    | frozenset(
        {"-d", "--description", "-h", "--homepage", "--add-topic", "--remove-topic"}
        | {"--default-branch", "--visibility", "-g", "--gitignore", "-l", "--license"}
        | {"-t", "--team", "-p", "--template", "-s", "--source", "-r", "--remote"}
        | {"-u", "--upstream-remote-name", "--fork-name", "-b", "--branch"}
    ),
    "release": _GH_VALUE_FLAGS
    | frozenset(
        {"-t", "--title", "-n", "--notes", "--target", "--tag", "--notes-start-tag"}
        | {"--discussion-category", "-L", "--limit", "-O", "--output", "--pattern"}
        | {"-D", "--dir", "-A", "--archive", "-q", "--jq", "-T", "--template"}
    ),
}


def _word_text(word: "_ShellWord") -> str:
    """*word* as its program receives it: quotes, escapes and `\\<newline>` removed,
    the `$` of `$'…'` / `$"…"` dropped and a `$'…'` body's escapes decoded as bash
    does (`$'--\\x69nput'` is `--input`). Nothing is expanded."""
    chars: "list[str]" = []
    state, dollar, ansi = 0, False, -1  # ansi: where the open `$'…'` body starts
    for step in word.steps:
        if step.state != state:  # an opening or closing quote
            if dollar and state == 0:
                chars.pop()
                ansi = len(chars) if step.state == 1 else -1
            elif ansi >= 0:  # the `$'…'` body closes: its raw text, decoded once
                chars[ansi:], ansi = [_decode_ansi_c_body("".join(chars[ansi:]))], -1
            state = step.state
        elif step.text != "\\\n":
            # In double quotes a backslash escapes only `$`, `` ` ``, `"` and `\`;
            # before anything else bash keeps it (`"\-"` is the two characters `\-`).
            kept = step.state == 2 and step.text[:1] == "\\" and step.char not in '$`"\\'
            chars.append(step.text if ansi >= 0 or kept else step.char)
        dollar = step.active and step.text == "$"
    return "".join(chars)


def _gh_argvs(command: str) -> "list[list[str]]":
    """Each of *command*'s commands as the words its program receives.

    Read by the shared :func:`shell_commands.read_commands` with ``heredocs``: a
    redirection (a leading `2>/dev/null` included) is never a word, a here-document
    body is data and an unquoted one adds only its substitutions' commands, and
    every `$(…)` or backtick body is read once. With ``whole_words`` an unquoted
    substitution stays one word of its command, so the flags after it are still gh's.
    """
    commands = _read_commands(command, heredocs=True, whole_words=True)
    return [[_word_text(w) for w in c.argv] for c in commands]


def _names_stdin(value: str) -> bool:
    """*value* reads no file on disk: empty (a process substitution `<( … )` is a
    redirection, so it leaves no word), `-`, `/dev/stdin` or `/dev/fd/N` (digits only:
    `/dev/fd/../../etc/passwd` is a file path)."""
    return value in ("", "-", "/dev/stdin") or re.fullmatch(r"/dev/fd/\d+", value) is not None


def _gh_field_reads_file(spec: str) -> bool:
    """A gh field spec `k=@<file>`; `k=@-` and the other stdin names are not files."""
    _key, sep, value = spec.partition("=")
    return bool(sep) and value.startswith("@") and not _names_stdin(value[1:])


def _gh_short_cluster(word: str, value_options: "frozenset[str]") -> "tuple[str, bool]":
    """Read a short-flag cluster the way gh's flag parser does: booleans, then at
    most one flag that takes the rest of the word or, when none is left, the next
    word (`-dt TITLE`, `-tTITLE`). Returns the `-F…` part still to check (or
    ``""``) and whether the next word is a value. Any letter that is not a known
    value option counts as a boolean, so `-dF notes.md` still reaches `-F`."""
    for ci in range(1, len(word)):
        flag, rest = "-" + word[ci], word[ci + 1 :]
        if flag == "-F":
            return "-F" + rest, False
        if flag in value_options:
            return "", not rest
    return "", False


def _gh_release_action(args: "list[str]", value_options: "frozenset[str]") -> str:
    """The word after `release` that names its action (`create`, `download`, …)."""
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in value_options
        else:
            return word
    return ""


def _may_run_gh(command: str) -> bool:
    """A cheap test before :func:`_gh_file_body_flag`: `gh` appears once quotes,
    backslashes and newlines are removed, so `g\\h` and `g''h` (both run `gh`) pass; any
    `$'…'` passes too, since its escapes can spell `gh` (`$'g\\x68'`)."""
    return "gh" in re.sub(r"[\\'\"\n]", "", command.lower()) or "$'" in command


def _gh_file_body_flag(command: str) -> str:
    """The gh flag in *command* that sends a local file, or ``""``."""
    for words in _gh_argvs(command):
        # The argv floor's own program reader: `VAR=x`, reserved words and wrappers
        # (`sudo`, `env`, `nohup`, …) are stepped over the same way.
        parsed = _program_args(words)
        if parsed is None or parsed[0] != "gh":
            continue
        args = parsed[1]
        # Global flags may sit before the subcommand.
        j = 0
        while j < len(args):
            w = args[j]
            if w.startswith("-"):
                j += 2 if w in _GH_VALUE_FLAGS else 1  # a value flag (`-R o/r`) takes the next word
            elif j and args[j - 1].startswith("-") and "=" not in args[j - 1]:
                if w in _GH_FILE_BODY_SUBCOMMANDS:
                    break
                j += 1  # the value of a flag this walk does not know
            else:
                break
        if j >= len(args) or args[j] not in _GH_FILE_BODY_SUBCOMMANDS:
            continue
        release = args[j] == "release"
        value_options = _GH_VALUE_OPTIONS[args[j]]
        if release and _gh_release_action(args[j + 1 :], value_options) == "download":
            value_options = value_options | {"-p"}  # `--pattern` here, `--prerelease` on create
        # Global flags count (`gh --input f api …` gives api the flag); `-F` on
        # release means `--notes-file` only after the subcommand.
        # A known option's value (`--title '--input=x'`) is data, not a flag.
        skip = False
        for k, word in enumerate(args):
            if skip:
                skip = False
                continue
            skip = word in value_options
            if len(word) > 2 and word[0] == "-" and word[1] not in "-F":
                word, skip = _gh_short_cluster(word, value_options)
                if not word:
                    continue
            if release and k <= j and word.startswith("-F"):
                continue
            nxt = args[k + 1] if k + 1 < len(args) else ""
            name, eq, value = word.partition("=")
            if word.startswith("-F") and not word.startswith("--"):
                name, value = "-F", word[2:].removeprefix("=") or nxt
                skip = word == "-F"
            elif not eq:
                value = nxt
            if name in ("--input", "--notes-file", "--field"):
                skip = not eq
            if name == "--input" and not _names_stdin(value):
                return "--input"
            if release and name in ("--notes-file", "-F") and not _names_stdin(value):
                return "--notes-file"
            if not release and name in ("--field", "-F") and _gh_field_reads_file(value):
                return "--field"
    return ""
