"""gh's body-from-file flags, read on shell words (the ``data-exfil-gh-file-body`` row).

The exfil gate (:func:`kiro_crew.security.exfil_bash.audit_bash_exfiltration`) and the
``is_denied`` floor both call :func:`_gh_file_body_flag`.
"""

from __future__ import annotations

import bisect
import heapq
import re

from .shell_commands import ShellWord as _ShellWord
from .shell_commands import _heredoc_end
from .shell_commands import read_commands as _read_commands
from .shell_normalizer import _matching_close_paren
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
#: Bash reserved words that may stand before a command's program word
#: (`{ gh …; }`, `then gh …`, `do gh …`, `! gh …`, `time gh …`).
_LEADING_WORDS = frozenset({"{", "!", "if", "then", "else", "elif", "while", "until", "do", "time"})


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
            chars.append(step.text if ansi >= 0 else step.char)
        dollar = step.active and step.text == "$"
    return "".join(chars)


def _gh_argvs(command: str) -> "list[list[str]]":
    """Each of *command*'s commands as the words its program receives.

    Read by :func:`shell_commands.read_commands` with ``heredoc_bodies``: a
    redirection (a leading `2>/dev/null` included) is never a word, a here-document
    body is skipped and an unquoted one adds only its substitutions' commands, and
    every `$(…)` or backtick body is read once.
    """
    argvs = [[_word_text(w) for w in c.argv] for c in _read_commands(command, heredoc_bodies=True)]
    if _heredoc_unclear(command):
        argvs.extend(_flat_argvs(command))
    return argvs


_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([^\s;&|()<>'\"]*)\2")


def _heredoc_unclear(command: str) -> bool:
    """A `<<` the reader may misread, so the matcher fails closed: one after an unclosed
    `((`/`$((`, an unquoted-tag here-document inside a `$(…)`/backtick span, or a span
    whose computed close falls inside a here-document body (a lone `)` in the data).
    A quoted-tag body inside a span the reader closes after it is data."""
    closes: "list[int]" = []  # min-heap: the computed close of each span still open
    budget = 4 * len(command) + 64  # characters the close scans may read in total
    joins = [m.start() for m in re.finditer(r"\\\n", command)]  # each read once per command
    depth = at = 0
    for doc in _HEREDOC_RE.finditer(command):
        seen, at = at, doc.start()  # read the text before this `<<` once, not per `<<`
        depth += command.count("((", seen, at) - command.count("))", seen, at)
        for opener in re.compile(r"\$\(|`").finditer(command, seen, at):  # each span once
            start = opener.end()
            if opener.group() == "`":
                close = command.find("`", start)
            else:
                close = start + _matching_close_paren(command[start:], 0)[0] - 1
            budget -= (close if close >= 0 else len(command)) - start + 1
            if budget < 0:
                return True  # nested or unclosed spans: fail closed rather than rescan
            heapq.heappush(closes, close)
        head = command[at + 2 : at + 258].replace("\\\n", "")  # live line joins
        word = re.match(r"-?[ \t]*([^\s;&|()<>]*)", head).group(1)  # type: ignore[union-attr]
        if depth > 0 or not re.fullmatch(r"[\w.-]+|'[\w.-]*'|\"[\w.-]*\"", word):
            return True  # after an unclosed `((`; `$'…'`, `$"…"`, backslash or mixed quoting
        line = command.find("\n", doc.end()) + 1
        strip = bool(doc.group(1))  # only `<<-` strips leading tabs
        term = _heredoc_end(command, line, word.strip("'\""), strip) if line else -1
        j = bisect.bisect_left(joins, line)  # the first line join in or after the body
        if word[0] not in "'\"" and line and j < len(joins) and (term < 0 or joins[j] < term):
            return True  # bash joins lines in an unquoted body
        while closes and 0 <= closes[0] < at:
            heapq.heappop(closes)  # spans that closed before this here-document
        if closes:  # the earliest close of a span still open (-1: an unclosed backtick)
            end = _heredoc_end(command, line, doc.group(3), strip) if line else -1
            if not doc.group(2) or end < 0 or closes[0] < end:
                return True
    return False


def _flat_argvs(command: str) -> "list[list[str]]":
    """The whole command as commands: split at every `;&|()`, backtick and newline, quotes
    and backslashes dropped, so no span or body hides a program word."""
    text = re.sub(r"[\\'\"]", "", command)  # then drop redirections and their targets
    text = re.sub(r"\d*(?:&>>?|[<>]+&?)[ \t]*[^\s;&|()`<>]*", " ", text)
    return [piece.split() for piece in re.split(r"[;&|()`\n]", text)]


def _is_assignment(word: str) -> bool:
    name, eq, _value = word.partition("=")
    return bool(eq) and name.isidentifier()


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
        i = 0
        while i < len(words) and (_is_assignment(words[i]) or words[i] in _LEADING_WORDS):
            i += 1
        if i >= len(words) or words[i].rsplit("/", 1)[-1] != "gh":
            continue
        args = words[i + 1 :]
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
