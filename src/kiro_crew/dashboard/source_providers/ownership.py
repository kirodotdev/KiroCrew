"""Which pull requests and issues a chat OWNS, for its sidebar card chips.

A URL that a chat's messages only mention (a status note, pasted context, a
compaction summary) is not the chat's. These are the signals that are:

* **created** -- a successful tool row that ran ``gh pr create`` or
  ``gh issue create``; the URL is the line gh printed in ``meta.output``.
* **acted on** -- a successful tool row that ran a ``gh pr`` write verb, or a
  writing ``gh api .../pulls/<N>/...``. Read and write are told apart by the batch
  guard's own classifier (``agent_sdk.gh_call_writes``), so the card and the
  guard agree on what writes.
* **reported** -- a ``work_report`` that landed on the work ledger: the item's
  ``pr`` and any pull-request URL among its ``artifacts``.

The gh proxy a remote session runs prints gh's own stdout and exit code, so its
rows have the same shape as a local ``gh`` and need no branch of their own.

Pure functions over rows and ledger records. The slot-side merge (one chat's
own links, its own report, and its workers' links) lives in
``state_parts/source_links.py``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import shlex
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import datetime
from itertools import islice
from typing import Any, NamedTuple

from kiro_crew.dashboard.source_providers.contract import SourceRef

logger = logging.getLogger(__name__)


#: ``gh pr``/``gh issue`` flags that take a value, so a numeric value is never
#: read as the target (``--milestone 3`` is not pull request 3).
_VALUE_FLAGS = frozenset(
    "-R --repo -b --body -F --body-file -t --title -B --base -H --head -l --label "
    "--add-label --remove-label -a --assignee --add-assignee --remove-assignee "
    "-r --reviewer --add-reviewer --remove-reviewer -m --milestone -p --project "
    "--add-project --remove-project --subject --match-head-commit -A --author-email "
    "-T --template -q --jq --json -c --comment --reason -L --limit -s --state".split()
)
#: Letters that take a value for most verbs but are on/off switches for these.
_VERB_SWITCHES = {
    "review": frozenset("-a --approve -c --comment -r --request-changes".split()),
    "merge": frozenset(
        "-m --merge -r --rebase -s --squash -d --delete-branch --auto --admin "
        "--disable-auto".split()
    ),
}
#: Command words that run the next word as the command.
_WRAPPERS = frozenset({"env", "command", "exec", "time", "nohup", "timeout", "nice", "stdbuf"})
_ASCII_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_ASCII_DIGITS = frozenset("0123456789")
_ASCII_ALNUM = _ASCII_LETTERS | _ASCII_DIGITS
#: Folds ASCII capitals, and the four non-ASCII letters ``re.IGNORECASE`` equates
#: with ASCII ones (``ſ`` ``İ`` ``ı`` ``K``), one character for one, so an index into
#: the folded text is one into the text.
_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ\u017f\u0130\u0131\u212a", "abcdefghijklmnopqrstuvwxyzsiik"
)
#: The first character of an owner in gh's ``pull request owner/repo#N``, matched
#: case-insensitively: ASCII letters and digits, and those four letters.
_SAID_OWNER_START = _ASCII_ALNUM | frozenset("\u017f\u0130\u0131\u212a")
#: Characters a path segment of a ``repos/<owner>/<repo>`` endpoint never holds.
_API_SEGMENT_STOP = frozenset("/?#{}")


def _word_char(ch: str) -> bool:
    """A word character as Python's ``\\w`` reads one: alphanumeric or ``_``."""
    return ch.isalnum() or ch == "_"


def _ends_word(text: str, at: int) -> bool:
    """Whether a word that stops just before *at* ends there (``\\b`` after it)."""
    return at >= len(text) or not _word_char(text[at])


def _without_final_newline(text: str) -> str:
    """*text* less one trailing newline, as ``$`` lets a match end before it."""
    return text[:-1] if text.endswith("\n") else text


def _is_assignment(word: str) -> bool:
    """``NAME=value``: a shell variable assignment."""
    name, eq, _value = word.partition("=")
    return (
        bool(eq)
        and name[:1] in _ASCII_LETTERS | {"_"}
        and all(ch in _ASCII_ALNUM or ch == "_" for ch in name)
    )


def _is_wrapper_arg(word: str) -> bool:
    """An option (``-n``) or a duration (``60``, ``1.5m``) a wrapper like ``timeout`` takes."""
    body = _without_final_newline(word)
    if body.startswith("-"):
        return "\n" not in body
    if body[-1:] in ("s", "m", "h", "d"):
        body = body[:-1]
    whole, dot, frac = body.partition(".")
    return whole.isdecimal() and (not dot or frac.isdecimal())


def _exit_code(output: str) -> str | None:
    """The ``N`` of the shell's closing ``Exit Code: N`` trailer, if the output ends in one."""
    _head, sep, tail = output.rstrip().rpartition("Exit Code: ")
    if not sep:
        return None
    digits = tail[1:] if tail.startswith("-") else tail
    return tail if digits.isdecimal() else None


def _ascii_number(text: str, at: int, stops: str) -> tuple[str, int] | None:
    """1-12 ASCII digits at *at*, then the end, a newline at the end, or one of *stops*."""
    end = at
    while end < len(text) and text[end] in _ASCII_DIGITS:
        end += 1
    rest = text[end:]
    if not 1 <= end - at <= 12 or not (rest in ("", "\n") or rest[0] in stops):
        return None
    return text[at:end], end


def _owner_repo(owner: str, repo: str) -> bool:
    """An ``owner`` and ``repo`` as gh spells them: word characters, ``.`` and ``-``."""
    return (
        owner[:1] in _ASCII_ALNUM
        and repo != ""
        and all(_word_char(ch) or ch in ".-" for ch in owner + repo)
    )


def _owner_repo_number(value: str) -> tuple[str, str, str] | None:
    """``owner/repo#123`` written in a report's artifacts, as its three parts."""
    head, sep, number = _without_final_newline(value).rpartition("#")
    owner, slash, repo = head.partition("/")
    if (
        not sep
        or not slash
        or "/" in repo
        or "#" in head
        or not 1 <= len(number) <= 12
        or not all(ch in _ASCII_DIGITS for ch in number)
        or not _owner_repo(owner, repo)
    ):
        return None
    return owner, repo, number


def _said_pulls(text: str) -> Iterator[tuple[str, str, str]]:
    """Each ``pull request owner/repo#123`` as gh prints it after close, merge, ready and
    review. Anchored on gh's own words so a ``#N`` in the PR's title cannot win."""
    folded = text.translate(_ASCII_LOWER)
    pos = 0
    while (start := folded.find("pull request ", pos)) >= 0:
        at = start + len("pull request ")
        end = at
        while end < len(text) and (_word_char(text[end]) or text[end] in ".-"):
            end += 1
        owner = text[at:end]
        if end < len(text) and text[end] == "/" and owner[:1] in _SAID_OWNER_START:
            repo_end = end + 1
            while repo_end < len(text) and (_word_char(text[repo_end]) or text[repo_end] in ".-"):
                repo_end += 1
            repo = text[end + 1 : repo_end]
            if repo and text[repo_end : repo_end + 1] == "#":
                number_end = repo_end + 1
                while number_end < len(text) and _word_char(text[number_end]):
                    number_end += 1
                number = text[repo_end + 1 : number_end]
                if 1 <= len(number) <= 12 and all(ch in _ASCII_DIGITS for ch in number):
                    yield owner, repo, number
                    pos = number_end
                    continue
        pos = start + 1


def _api_pull(endpoint: str) -> tuple[str, str, str] | None:
    """``repos/<owner>/<repo>/pulls/<N>`` (then ``/``, ``?``, ``#`` or the end) as parts."""
    if not endpoint.startswith("repos/"):
        return None
    owner, _slash, rest = endpoint[len("repos/") :].partition("/")
    repo, slash, rest = rest.partition("/")
    if (
        not owner
        or not repo
        or not slash
        or any(ch in _API_SEGMENT_STOP for ch in owner + repo)
        or not rest.startswith("pulls/")
    ):
        return None
    found = _ascii_number(rest, len("pulls/"), "/?#")
    return (owner, repo, found[0]) if found else None


def _api_number(endpoint: str) -> int | None:
    """The first ``/pulls/<N>`` or ``/issues/<N>`` number anywhere in *endpoint*."""
    pos = 0
    while True:
        hits = [
            (at, marker)
            for marker in ("/pulls/", "/issues/")
            if (at := endpoint.find(marker, pos)) >= 0
        ]
        if not hits:
            return None
        at, marker = min(hits)
        found = _ascii_number(endpoint, at + len(marker), "/?#")
        if found:
            return int(found[0])
        pos = at + 1


def _api_host(endpoint: str) -> tuple[str, str] | None:
    """``https://<host>/`` (and an Enterprise ``api/v3/``) split off an absolute endpoint."""
    if not endpoint.startswith("https://"):
        return None
    host, slash, path = endpoint[len("https://") :].partition("/")
    if not host or not slash:
        return None
    return host, path.removeprefix("api/v3/")


def _gh_already_exists(output: str) -> bool:
    """gh's own refusal to create a second pull request for one branch."""
    for line in output.translate(_ASCII_LOWER).split("\n"):
        pos = 0
        while (start := line.find("a pull request for branch", pos)) >= 0:
            end = start + len("a pull request for branch")
            pos = start + 1
            if not _ends_word(line, end):
                continue
            at = end
            while (at := line.find("already exists", at)) >= 0:
                if at == 0 or not _word_char(line[at - 1]):
                    return True
                at += 1
    return False


def _gh_write_hint(text: str) -> bool:
    """Whether *text* holds a gh call that can own a link (``gh pr edit``, ``gh api``).

    No leading word boundary: in the JSON-encoded input a call on a new line reads
    ``\\ngh``, and the ``n`` would hide it.
    """

    def spaces(at: int) -> int:
        end = at
        while end < len(text) and text[end].isspace():
            end += 1
        return end

    def word(at: int, words: tuple[str, ...]) -> bool:
        return any(text.startswith(w, at) and _ends_word(text, at + len(w)) for w in words)

    from kiro_crew import agent_sdk

    pr_verbs = tuple(agent_sdk.gh_write_verbs("pr"))
    pos = 0
    while (start := text.find("gh", pos)) >= 0:
        pos = start + 1
        at = spaces(start + 2)
        if at == start + 2:
            continue
        if word(at, ("api",)):
            return True
        for program, verbs in (("pr", pr_verbs), ("issue", ("create",))):
            if text.startswith(program, at):
                after = spaces(at + len(program))
                if after > at + len(program) and word(after, verbs):
                    return True
    return False


#: Output URL candidates read per tool row, so one huge output costs a bounded parse.
MAX_OUTPUT_CANDIDATES = 16
#: Command text read per row. The shell reader costs about 2 us a character
#: (heredoc bodies far less), so this bounds one read at about 70 ms.
#: shortcut: a gh call past it (deep in a long script) is missed; raise it if one is.
MAX_COMMAND_CHARS = 32768
#: The end of a tool row's output that is read; gh prints what counts last.
OUTPUT_SCAN_CHARS = 16384

Parse = Callable[[str], SourceRef]


class OwnedRef(NamedTuple):
    """One link a row proves the chat owns, and how."""

    ref: SourceRef
    relation: str


#: Private keys a derived link carries until the slot-side merge strips them: the
#: owning event's epoch stamp, ``created`` / ``acted`` / ``reported``, and whether
#: any event on the link was its creation (a worker's created links roll up even
#: after a later edit).
OWNED_AT_KEY = "_owned_at"
RELATION_KEY = "_relation"
CREATED_KEY = "_created"
PRIVATE_KEYS = (OWNED_AT_KEY, RELATION_KEY, CREATED_KEY)


def source_link_dict(
    ref: SourceRef,
    label: Callable[[SourceRef], str],
    identity_key: Callable[[tuple], str],
) -> dict[str, Any]:
    """The wire shape of one source-link chip."""
    return {
        "provider": ref.provider,
        "number": ref.number,
        "url": ref.url,
        "kind": ref.kind,
        "label": label(ref),
        # The serialized identity travels to the client so an unlink affordance
        # can name this exact object back to the DELETE endpoint without
        # re-parsing the URL. It is the SAME key the dismissed-set filter tests,
        # so a round-trip through the wire cannot drift from what is suppressed.
        "identity": identity_key(ref.identity),
    }


def row_timestamp(value: Any) -> float:
    """An ISO stamp as epoch seconds, ``0.0`` when absent or unreadable."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _command_text(meta: dict) -> str | None:
    """The shell command a tool row ran, or ``None`` when it ran no gh write.

    A cheap search of the raw input runs first: most tool rows never run gh,
    and many that do only read (``gh pr view``), so only a row that could own a
    link pays for the JSON decode and the shell lexer.
    """
    raw = meta.get("input")
    if not isinstance(raw, str):
        return None
    # A direct argv (``["gh", "pr", "create"]`` or ``["/usr/bin/gh", ...]``) has no
    # ``gh pr `` in its JSON. The raw text is JSON, where a tab is ``\t``, so this
    # only looks for the words; the decoded command is checked properly below.
    argv_form = '"gh"' in raw or '/gh"' in raw
    if not argv_form and (
        "gh" not in raw or ("pr" not in raw and "issue" not in raw and "api" not in raw)
    ):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    command = parsed.get("command", parsed.get("cmd"))
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        # ``["bash", "-lc", "<script>"]`` runs the script; any other argv is the command.
        if len(command) >= 3 and command[1] in ("-c", "-lc"):
            command = command[2]
        else:
            command = shlex.join(command)
    if not isinstance(command, str) or not _gh_write_hint(command):
        return None
    return command


def gh_write_command(meta: dict) -> str | None:
    """The shell command a tool row ran when it could own a link, else ``None``."""
    return _command_text(meta)


def row_gate(meta: dict) -> tuple:
    """What :func:`tool_row_succeeded` reads from a row's meta besides its output,
    so a memo of a row's refs is dropped when its terminal status lands."""
    return (meta.get("tool_status"), bool(meta.get("failed")), bool(meta.get("error")))


def tool_row_succeeded(meta: dict, output: str) -> bool:
    """Whether a tool row's command positively completed with exit code 0.

    Invariant: a row counts only on positive proof that its call ran and
    succeeded -- a shell trailer ``Exit Code: 0``, or, with no trailer, a stored
    ACP status of ``completed``. A refused, denied, failed, cancelled or
    still-running row never counts, whatever its argv names.

    shortcut: a row from before the status was stored, from a backend that
    prints no trailer, makes no chip; nothing can tell it ran.
    """
    if meta.get("failed") or meta.get("error"):
        return False
    status = meta.get("tool_status")
    if status is not None and status != "completed":
        return False
    code = _exit_code(output)
    if code is not None:
        return code == "0"
    return status == "completed"


def _word_text(word: Any) -> str:
    """*word* as the shell passes it on: its live quotes removed, escapes resolved."""
    return "".join(
        step.char
        for step in word.steps
        if not (step.text in ("'", '"') and (step.active or step.state == 0))
    )


class _GhCall(NamedTuple):
    """One gh invocation in a shell command."""

    #: A ``GH_HOST=`` set on the call itself.
    host: str | None
    #: The argv after ``gh``.
    argv: list[str]
    #: Whether the row's exit code 0 proves this call ran and succeeded: every
    #: operator after it is ``&&`` and it was not reached through ``||``.
    decisive: bool
    #: Whether another program in the command can print to the same output
    #: (``cat body.md && gh pr edit``), so a URL in it may not be gh's.
    shared_output: bool = False
    #: Inside a substitution: its stdout is captured, never the row's.
    nested: bool = False
    #: Its stdout goes to a file (``>/dev/null``), so nothing it prints reaches the row.
    quiet: bool = False


#: Commands that print nothing a PR URL could come from.
_SILENT = frozenset(
    {
        "cd",
        "pushd",
        "popd",
        "export",
        "unset",
        "set",
        "true",
        ":",
        "mkdir",
        "touch",
        "rm",
        "cp",
        "mv",
        "sleep",
        "chmod",
        "ln",
        "[",
        "[[",
        "test",
    }
)


#: Shell reserved words that open or close a block; the command after them runs.
_RESERVED = frozenset(
    {"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "!", "{", "}"}
)


def _prints(cmd: Any, words: list[str]) -> bool:
    """Whether simple command *cmd* (its argv *words*) can write to the row's output."""
    if not words or all(_is_assignment(word) or word in _RESERVED for word in words):
        return False
    if words[0] in _SILENT or words[0] in ("for", "select", "case", "esac"):
        return False
    # Its stdout feeds the next command (``cat body | gh pr comment -F -``) or goes
    # to a file (``cat > body.md <<'EOF'``, ``echo x >> log``).
    if cmd.separator == "|":
        return False
    return not any(
        redirect.raw.startswith((">", "1>", "&>")) for redirect, _target in cmd.redirects
    )


@functools.lru_cache(maxsize=512)
def _gh_calls(command: str) -> tuple[_GhCall, ...]:
    """Every simple command in *command* that runs gh.

    Read with the shared shell reader (``security.shell_commands.read_commands``),
    so ``cd x && gh pr edit`` finds the gh call, a heredoc body is never a command,
    and a redirection (``> out``, ``2>&1``) is stepped over. Cached by text: a row's
    command stays the same while its output streams in. Callers must not change
    the argv lists.
    """
    from kiro_crew.security import shell_commands, shell_normalizer

    text = command[:MAX_COMMAND_CHARS]
    if "\\\n" in text:
        text = shell_normalizer._fold_line_continuations(text)
    # Invariant: the operator chain and the other-printer check read the
    # TOP-LEVEL commands only. A substitution (``$(...)``, ``"$(...)"``, a
    # backtick pair) joins no ``&&``/``||`` chain and its stdout is captured, so
    # it gives up nothing but its gh calls, and those are never decisive. Heredoc
    # bodies are blanked first: their prose (``It's ready at $(git ...)``) is
    # data, and its quotes would hide a substitution from the stripping.
    full = shell_commands.read_commands(text, heredocs=True)
    # Every blanking keeps the text's length, so a command's offsets are the same
    # in both reads and a gh call is matched across them by where it starts.
    top_text = _blank_backticks(_blank_substitutions(_without_heredoc_bodies(text, full)))
    commands = shell_commands.read_commands(top_text, heredocs=True, nested=False)
    # A line break right after ``&&``/``||``/``|`` continues the same list, but
    # the reader cuts an empty command there; drop it so the real operator joins.
    commands = [
        cmd
        for at, cmd in enumerate(commands)
        if cmd.steps or not at or commands[at - 1].separator not in ("&&", "||", "|")
    ]
    segments = [[_word_text(word) for word in cmd.argv] for cmd in commands]
    #: The operator that ends each segment ("" for the last).
    operators = [cmd.separator for cmd in commands]
    # What is left in parentheses is a subshell, whose exit the row's does not prove.
    grouped = any(operator in ("(", ")") for operator in operators)
    # ``only_and_after[i]``: every operator that joins segment i to a later
    # non-empty segment is ``&&`` (a trailing newline or ``;`` runs nothing).
    # One backward pass, so a long command costs linear time.
    only_and_after = [True] * len(segments)
    later_command = False
    for position in range(len(segments) - 1, -1, -1):
        if position + 1 < len(segments):
            only_and_after[position] = only_and_after[position + 1] and (
                not later_command or operators[position] == "&&"
            )
        later_command = later_command or bool(segments[position])
    found: list[_GhCall] = []
    top_gh_at: set[int] = set()
    other_prints = False
    # In a pipeline gh starts (``gh pr merge 42 2>&1 | tail -5``), every later
    # command only reshapes gh's own output.
    piping_gh = False
    for position, words in enumerate(segments):
        index = _command_start(words)
        is_gh = index < len(words) and words[index].rsplit("/", 1)[-1] == "gh"
        if is_gh:
            top_gh_at.add(commands[position].argv[index].start)
            before = operators[position - 1] if position else ""
            # ``! gh ...`` inverts gh's exit, and ``gh ... &`` runs in the background,
            # so the row's exit says nothing about either.
            negated = "!" in words[:index]
            decisive = (
                not grouped
                and not negated
                and operators[position] != "&"
                and only_and_after[position]
                and "||" not in before
            )
            quiet = any(
                redirect.raw.startswith((">", "1>", "&>"))
                for redirect, _target in commands[position].redirects
            )
            found.append(
                _GhCall(_gh_host(words[:index]), words[index + 1 :], decisive, quiet=quiet)
            )
        elif not piping_gh and _prints(commands[position], words[index:]):
            other_prints = True
        piping_gh = operators[position] == "|" and (is_gh or piping_gh)
    # gh calls inside a substitution (``url="$(gh pr create --fill)"``): every gh
    # call of the full read that does not start where a top-level one does.
    if "$(" in text or "`" in text:
        for cmd in full:
            words = [_word_text(word) for word in cmd.argv]
            index = _command_start(words)
            if index >= len(words) or words[index].rsplit("/", 1)[-1] != "gh":
                continue
            if cmd.argv[index].start in top_gh_at:
                continue
            found.append(_GhCall(_gh_host(words[:index]), words[index + 1 :], False, nested=True))
    if other_prints:
        return tuple(call._replace(shared_output=True) for call in found)
    return tuple(found)


def _without_heredoc_bodies(text: str, commands: list[Any]) -> str:
    """*text* with every heredoc body *commands* read blanked, newlines kept."""
    spans = [(doc.start, doc.end) for cmd in commands for doc in cmd.heredocs]
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for at in range(max(start, 0), min(end, len(chars))):
            if chars[at] != "\n":
                chars[at] = " "
    return "".join(chars)


def _blank(text: str, start: int, end: int) -> str:
    """*text* with ``[start, end)`` as the word ``_`` padded to its length, newlines kept."""
    body = "".join("\n" if ch == "\n" else " " for ch in text[start + 1 : end])
    return text[:start] + "_" + body + text[end:]


def _blank_substitutions(text: str) -> str:
    """*text* with each unquoted ``$(…)`` / ``<(…)`` / ``>(…)`` blanked, length kept.

    The same spans ``security.shell_commands.without_substitutions`` replaces.
    """
    if "(" not in text:
        return text
    from kiro_crew.security import shell_normalizer

    pos = 0
    while True:
        steps = shell_normalizer._iter_shell_chars(text[pos:])
        opens = (pos + s.offset for s in steps if s.active and s.char in "$<>")
        hit = next((at for at in opens if text.startswith("(", at + 1)), None)
        if hit is None:
            return text
        end = shell_normalizer._matching_close_paren(text, hit + 2)[0]
        text = _blank(text, hit, end)
        pos = end


def _blank_backticks(text: str) -> str:
    """*text* with each live backtick substitution blanked, length kept."""
    if "`" not in text:
        return text
    from kiro_crew.security import shell_normalizer

    ticks = [
        step.offset
        for step in shell_normalizer._iter_shell_chars(text)
        if step.active and step.char == "`"
    ]
    for opening, closing in zip(ticks[::2], ticks[1::2]):
        text = _blank(text, opening, closing + 1)
    return text


def _command_start(words: list[str]) -> int:
    """The index of the program in *words*, past assignments, wrappers and ``if``/``do``."""
    index = 0
    while index < len(words) and (
        _is_assignment(words[index])
        or words[index] in _WRAPPERS
        or words[index] in _RESERVED
        or (index and words[index - 1] != "gh" and _is_wrapper_arg(words[index]))
    ):
        index += 1
    return index


def _gh_host(prefix: list[str]) -> str | None:
    """A ``GH_HOST=`` set on the call itself, in the words before the program."""
    host: str | None = None
    for word in prefix:
        if word.startswith("GH_HOST="):
            host = word[len("GH_HOST=") :] or None
    return host


def _flag_value(args: list[str], names: frozenset[str]) -> str | None:
    """The value of the first of *names* in *args* (``-R x``, ``--repo=x``, ``-Rx``)."""
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            if name.startswith("--") and arg.startswith(name + "="):
                return arg[len(name) + 1 :]
            if not name.startswith("--") and arg.startswith(name) and len(arg) > len(name):
                return arg[len(name) :]
    return None


def _positionals(args: list[str], value_flags: frozenset[str]) -> list[str]:
    """The batch guard's positional reader, over arguments with no program word."""
    from kiro_crew import agent_sdk

    return agent_sdk.command_positionals(["", *args], value_flags)


def _api_positionals(args: list[str]) -> list[str]:
    """The positional words of a ``gh api`` call's arguments, skipping the values of
    the batch guard's own valued-option table, so both find the same endpoint."""
    from kiro_crew import agent_sdk

    return _positionals(args, agent_sdk.gh_valued_options())


def _gh_writes(call: list[str]) -> bool:
    """Whether the gh call with argv *call* (after ``gh``) writes, as the batch guard
    judges it: one table of write verbs and one ``gh api`` method rule for both."""
    from kiro_crew import agent_sdk

    return agent_sdk.gh_call_writes(["gh", *call])


def _default_host(hint: str | None = None) -> str | None:
    """The host an ``owner/repo`` with no host names: the call's own ``GH_HOST``,
    else github.com, unless the operator configured GitHub Enterprise hosts, when
    gh's default host is whichever ``GH_HOST`` the shell had and the row does not
    say which."""
    from kiro_crew.dashboard.source_providers import hosts

    if hint:
        return hint
    # Fail closed while the host settings have not loaded: an empty Enterprise
    # list then means "not read yet", not "none configured".
    if not hosts._gitlab_hosts_loaded_at:
        return None
    return None if hosts._allowed_github_hosts() else "github.com"


def _repo_url(
    repo: str | None, number: int, segment: str = "pull", *, host_hint: str | None = None
) -> str | None:
    """``https://<host>/<owner>/<repo>/<segment>/<n>`` from a ``-R`` value."""
    if not repo:
        return None
    bare = (
        repo.removeprefix("https://")
        if repo.startswith("https://")
        else repo.removeprefix("http://")
    )
    # A URL path, not a filesystem one: its separator is always ``/``.
    parts: list[str] = []
    rest = bare
    while rest:
        part, _sep, rest = rest.partition("/")
        if part:
            parts.append(part)
    if len(parts) == 2:
        host = _default_host(host_hint)
        if host is None:
            return None
        parts.insert(0, host)
    # ``{owner}/{repo}`` is gh's placeholder for the working directory's repository,
    # which the row does not name.
    if len(parts) != 3 or any("{" in part or "}" in part for part in parts):
        return None
    return f"https://{parts[0]}/{parts[1]}/{parts[2]}/{segment}/{number}"


def _target_number(token: str) -> int | None:
    """A ``123`` / ``#123`` target as a number; ASCII digits only, at most 12 of them."""
    digits = token[1:] if token.startswith("#") else token
    if digits.isascii() and digits.isdigit() and len(digits) <= 12:
        return int(digits)
    return None


def _output_change_refs(
    output: str,
    parse: Parse,
    candidates: Callable[[str], Iterator[str]],
    host_hint: str | None = None,
) -> list[SourceRef]:
    """Pull-request refs gh printed: its ``pull request owner/repo#N`` first, then URLs.

    The ``owner/repo#N`` form names no host, so it counts only where the host is
    certain (:func:`_default_host`).
    """
    refs: list[SourceRef] = []
    host = _default_host(host_hint)
    texts = (
        [
            f"https://{host}/{owner}/{repo}/pull/{number}"
            for owner, repo, number in _said_pulls(output[-4096:])
        ][:MAX_OUTPUT_CANDIDATES]
        if host is not None
        else []
    )
    texts += [*islice(candidates(output), MAX_OUTPUT_CANDIDATES)]
    for text in texts:
        try:
            ref = parse(text)
        except ValueError:
            continue
        if ref.kind == "change":
            refs.append(ref)
    return refs


def _acted_ref(
    args: list[str],
    printed: Callable[[str | None], list[SourceRef]],
    *,
    alone: bool,
    parse: Parse,
    host_hint: str | None = None,
) -> SourceRef | None:
    """The pull request one ``gh pr <write verb>`` call targets, or ``None``.

    *printed* answers the pull requests the row's output names, read once per
    row and host (:func:`_output_change_refs`).

    shortcut: ``gh pr merge 5`` / ``gh pr review 5`` with no ``-R`` and no URL print
    nothing off a terminal, so the repository is unknown and no chip is made; read
    the row's working directory's remote if agents come to rely on that form.
    """
    verb, args = (args[0], args[1:]) if args else ("", args)
    positional = _positionals(args, _VALUE_FLAGS - _VERB_SWITCHES.get(verb, frozenset()))
    target = positional[0] if positional else ""
    repo = _flag_value(args, frozenset({"-R", "--repo"}))
    if target.startswith("https://"):
        try:
            ref = parse(target)
        except ValueError:
            return None
        return ref if ref.kind == "change" else None
    number = _target_number(target) if target else None
    if number is not None:
        url = _repo_url(repo, number, host_hint=host_hint)
        if url is not None:
            try:
                return parse(url)
            except ValueError:
                return None
        # No -R: the repository is the working directory's, which only gh's
        # own output names. The ref must carry the targeted number.
        for ref in printed(host_hint):
            if ref.number == number:
                return ref
        return None
    if not alone:
        # One of several gh calls whose outputs interleave.
        return None
    found = printed(host_hint)
    if target and repo:
        # A branch target: gh's output names the PR, kept only in the named repository.
        def named(ref: SourceRef) -> bool:
            url = _repo_url(repo, ref.number, host_hint=host_hint)
            try:
                return url is not None and parse(url).identity == ref.identity
            except ValueError:
                return False

        found = [ref for ref in found if named(ref)]
    return found[0] if found else None


def _api_ref(args: list[str], parse: Parse, host_hint: str | None = None) -> SourceRef | None:
    """The pull request a ``gh api repos/<o>/<r>/pulls/<N>/...`` call names.

    Whether the call writes is the caller's question (:func:`_gh_writes`).
    """
    positional = _api_positionals(args)
    if not positional:
        return None
    endpoint = positional[0]
    # An absolute endpoint names its own host (``https://ghe.example.com/api/v3/...``);
    # a relative one is on ``--hostname``, else on gh's default host, which is
    # certain only with no Enterprise host configured (:func:`_default_host`).
    absolute = _api_host(endpoint)
    if absolute is None:
        named_host = _flag_value(args, frozenset({"--hostname"})) or _default_host(host_hint)
        if named_host is None:
            return None
        host = named_host
    else:
        host = "github.com" if absolute[0] == "api.github.com" else absolute[0]
        endpoint = absolute[1]
    match = _api_pull(endpoint.lstrip("/"))
    if match is None:
        return None
    try:
        ref = parse(f"https://{host}/{match[0]}/{match[1]}/pull/{match[2]}")
    except ValueError:
        return None
    return ref if ref.kind == "change" else None


def _named_target(call: list[str], host_hint: str | None, parse: Parse) -> SourceRef | None:
    """The pull request or issue a non-create gh call names in its own arguments."""
    if len(call) >= 3 and call[0] in ("pr", "issue"):
        verb, args = call[1], call[2:]
        positional = _positionals(args, _VALUE_FLAGS - _VERB_SWITCHES.get(verb, frozenset()))
        target = positional[0] if positional else ""
        if target.startswith("https://"):
            text: str | None = target
        else:
            number = _target_number(target)
            repo = _flag_value(args, frozenset({"-R", "--repo"}))
            segment = "pull" if call[0] == "pr" else "issues"
            text = (
                _repo_url(repo, number, segment, host_hint=host_hint)
                if number is not None
                else None
            )
    elif len(call) >= 2 and call[0] == "api":
        endpoint = _api_positionals(call[1:])
        absolute = _api_host(endpoint[0]) if endpoint else None
        rest = absolute[1] if absolute else (endpoint[0] if endpoint else "")
        match = _api_pull(rest.lstrip("/"))
        host = (
            ("github.com" if absolute[0] == "api.github.com" else absolute[0])
            if absolute
            else _default_host(host_hint)
        )
        text = f"https://{host}/{match[0]}/{match[1]}/pull/{match[2]}" if match and host else None
    else:
        return None
    if text is None:
        return None
    try:
        return parse(text)
    except ValueError:
        return None


def _bare_number(call: list[str], host_hint: str | None, parse: Parse) -> int | None:
    """The number a non-create gh call names with no repository to resolve it in.

    ``gh pr view 2`` or ``gh api repos/{owner}/{repo}/pulls/2`` acts on the working
    directory's repository, which the row does not name, so only the number is known.
    """
    if _named_target(call, host_hint, parse) is not None:
        return None
    if len(call) >= 3 and call[0] in ("pr", "issue") and call[1] != "create":
        args = call[2:]
        positional = _positionals(args, _VALUE_FLAGS - _VERB_SWITCHES.get(call[1], frozenset()))
        return _target_number(positional[0]) if positional else None
    if len(call) >= 2 and call[0] == "api":
        endpoint = _api_positionals(call[1:])
        return _api_number(endpoint[0]) if endpoint else None
    return None


def owned_refs_in_tool_row(
    msg: dict, parse: Parse, candidates: Callable[[str], Iterator[str]]
) -> list[OwnedRef]:
    """Every link a tool row proves the chat created or acted on, in output order.

    ``candidates`` yields the raw URL tokens of a text (the shared transcript
    grammar). Nothing counts unless the command exited 0. A row this cannot read
    yields nothing rather than raising: one odd row must never fail the slots
    payload every card is served from.
    """
    try:
        return _owned_refs(msg, parse, candidates)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.debug("could not read a tool row's gh call for chips", exc_info=True)
        return []


def memoized_owned_refs(
    slot: Any, msg: dict, parse: Parse, candidates: Callable[[str], Iterator[str]]
) -> list[OwnedRef]:
    """*msg*'s owned refs, from the slot's row memo (``slot._owned_row_memo``, which
    the chip derivation in ``slot_projection.py`` rebuilds each walk) when it holds
    this row unchanged, else read fresh.

    The memo counts only under the host allowlists it was built with, since a
    row's refs depend on them. Read defensively for a bare slot.
    """
    from kiro_crew.dashboard.source_providers.hosts import gitlab_hosts_generation

    memo = getattr(slot, "_owned_row_memo", None)
    meta = msg.get("meta")
    held = memo[1].get(id(msg)) if memo and memo[0] == gitlab_hosts_generation() else None
    if (
        held is not None
        and isinstance(meta, dict)
        and held[0] is msg
        and held[1] is meta.get("input")
        and held[2] is meta.get("output")
        and held[4] == row_gate(meta)
    ):
        return list(held[3])
    return owned_refs_in_tool_row(msg, parse, candidates)


def _owned_refs(
    msg: dict, parse: Parse, candidates: Callable[[str], Iterator[str]]
) -> list[OwnedRef]:
    meta = msg.get("meta")
    if not isinstance(meta, dict):
        return []
    command = _command_text(meta)
    if command is None:
        return []
    output = meta.get("output")
    if not isinstance(output, str) or not tool_row_succeeded(meta, output):
        return []
    # gh prints what this needs at the end (the new URL, ``pull request o/r#N``),
    # and the output can be up to a megabyte, so only its tail is read.
    output = output[-OUTPUT_SCAN_CHARS:]
    # Sliced before the cached call, so each kept cache key is at most this long.
    hosted = _gh_calls(command[:MAX_COMMAND_CHARS])
    calls = [call.argv for call in hosted]
    owned: list[OwnedRef] = []
    printed_by_host: dict[str | None, list[SourceRef]] = {}

    def printed(host_hint: str | None) -> list[SourceRef]:
        # Read once per row and host, however many gh calls ask.
        if host_hint not in printed_by_host:
            printed_by_host[host_hint] = _output_change_refs(output, parse, candidates, host_hint)
        return printed_by_host[host_hint]

    creates = {
        "change" if call[0] == "pr" else "issue"
        for call in calls
        if len(call) >= 2
        and call[0] in ("pr", "issue")
        and call[1] == "create"
        and "--help" not in call
        and "-h" not in call
    }
    # What each call names: a full identity, or only a number when it names no
    # repository. Another call's printed URL is never this call's evidence.
    named = [_named_target(call.argv, call.host, parse) for call in hosted]
    bare = [_bare_number(call.argv, call.host, parse) for call in hosted]

    def named_by_others(index: int) -> set[tuple]:
        return {ref.identity for at, ref in enumerate(named) if at != index and ref is not None}

    # A create the row's exit does not prove ran (``gh pr view ... || gh pr create``)
    # owns a printed URL only when no other gh call could have printed it: one at
    # the top level whose stdout reaches the row. A call inside a substitution or
    # with its stdout sent to a file prints nothing there.
    create_calls = [
        call
        for call in hosted
        if len(call.argv) >= 2 and call.argv[0] in ("pr", "issue") and call.argv[1] == "create"
    ]
    other_gh_prints = any(
        not call.nested and not call.quiet and call not in create_calls for call in hosted
    )
    create_proven = all(call.decisive for call in create_calls) or not other_gh_prints

    if creates and create_proven and not _gh_already_exists(output):
        # gh prints the new URL on a line of its own. "a pull request for branch
        # ... already exists" is gh's refusal, which prints the EXISTING PR's
        # URL; a ``|| true`` or a pipe can still leave the trailer at 0. A URL
        # another gh call in the same command names is that call's, not the
        # create's: by identity (``gh pr view 2 -R o/r``), or by number when the
        # call names no repository (``gh pr view 2``, ``gh api repos/{owner}/
        # {repo}/pulls/2``).
        #
        # shortcut: in a compound command, a bare PR URL line another program
        # prints (``cat links.txt``), or another gh call that names a branch,
        # reads as created too; split output per call if that bites.
        elsewhere = named_by_others(-1)
        elsewhere_numbers = {number for number in bare if number is not None}
        for line in output.splitlines()[-64:]:
            line = line.strip()
            if not line.startswith("https://"):
                continue
            try:
                created = parse(line)
            except ValueError:
                continue
            if (
                created.kind in creates
                and created.identity not in elsewhere
                and created.number not in elsewhere_numbers
            ):
                owned.append(OwnedRef(created, "created"))
    for index, (host_hint, call, decisive, shared_output, _nested, _quiet) in enumerate(hosted):
        ref: SourceRef | None = None
        if "--help" in call or "-h" in call:
            # Help prints usage and exits 0 without touching anything.
            continue
        taken = named_by_others(index)

        def own_printed(
            hint: str | None, taken: set[tuple] = taken, shared: bool = shared_output
        ) -> list[SourceRef]:
            # gh's output, less the PRs another call in the command names. With
            # another program printing to the same output, a URL is gh's only
            # when it is the one PR the output names (``git push && gh pr comment
            # 42`` prints only gh's); two or more, and none is known to be gh's.
            found = [r for r in printed(hint) if r.identity not in taken]
            if shared and len({r.identity for r in found}) > 1:
                return []
            return found

        if len(call) >= 2 and call[0] == "pr" and call[1] != "create" and _gh_writes(call):
            ref = _acted_ref(
                call[1:],
                own_printed,
                alone=sum(not other.nested for other in hosted) == 1 and not hosted[index].nested,
                parse=parse,
                host_hint=host_hint,
            )
        elif len(call) >= 2 and call[0] == "api" and _gh_writes(call):
            ref = _api_ref(call[1:], parse, host_hint)
        if ref is not None and not decisive:
            # ``x || gh ...`` or ``gh ...; true``: the row's exit code says
            # nothing about this call, so only gh's own output can, and a URL
            # another call or another program could have printed proves nothing.
            if (
                shared_output
                or ref.identity in taken
                or ref.identity not in {r.identity for r in own_printed(host_hint)}
            ):
                ref = None
        if ref is not None:
            owned.append(OwnedRef(ref, "acted"))
    return owned


# Ledger: a chat's own report, and the reports and workers of the items it conducts.


class LedgerLinks(NamedTuple):
    """What the work ledger says about one chat's links.

    ``links`` are ``(url, reported_at)`` pairs: the chat's own item's report, then
    every item on the board the chat conducts. ``numbers`` are reported PR numbers
    no repository could be found for (``(number, reported_at)``); the slot-side
    merge resolves each against a link a tool row or worker gave the same number.
    ``workers`` are the local worker sessions bound to that board's items, whose
    own created links roll up.
    """

    links: tuple[tuple[str, float], ...]
    numbers: tuple[tuple[int, float], ...]
    workers: tuple[str, ...]


EMPTY_LEDGER_LINKS = LedgerLinks((), (), ())

#: How long a ledger answer is trusted while no write in this process moved the
#: change mark of a store it was read from. shortcut: a write from another process
#: (a CLI sweep) shows after this long; hook the store's file watcher if it matters.
LEDGER_TRUST_SECS = 60.0
#: The most links one chat's card derives, and so the most reported links,
#: unresolved numbers and workers one ledger answer keeps
#: (``state_parts.source_links._MAX_SOURCE_LINKS_PER_SLOT`` is this value).
MAX_LEDGER_LINKS = 64
#: Chats whose answer is kept, used last first out. Well above the open slots a
#: gateway serves, so a slots pass does not evict what the next pass reads.
_LEDGER_CACHE_MAX = 8192


class _Kept(NamedTuple):
    """One chat's ledger answer and what it was read under."""

    #: The change marks of the chat's own board and of the board its binding
    #: names, and the host allowlists' generation.
    marks: tuple[int, int, int]
    conductor: str
    born: float
    value: LedgerLinks


_ledger_cache: "OrderedDict[str, _Kept]" = OrderedDict()
#: The answer last published per chat, so an off-loop read pushes the slots only
#: when what the card shows changes.
_ledger_published: "OrderedDict[str, LedgerLinks]" = OrderedDict()
#: Cache keys whose off-loop read is running, so one is never queued twice.
_ledger_inflight: set[str] = set()
#: Called on the loop when an off-loop read changed a chat's answer; the gateway
#: installs its slots push here (:func:`set_ledger_refresh_hook`).
_ledger_refresh_hook: list[Callable[[], None]] = []
#: When this process first saw each (home, item, link) reported. An item keeps only
#: its NEWEST report's stamp, so a later progress report would otherwise move an
#: old PR ahead of newer ones. shortcut: after a gateway restart the first stamp
#: seen is the item's latest report; record the stamp on the item if that matters.
_first_reported: "OrderedDict[tuple[str, str, str], float]" = OrderedDict()
_ledger_lock = threading.Lock()


def _item_links(
    item: Any, parse: Parse, candidates: Callable[[str], Iterator[str]]
) -> tuple[list[str], int | None]:
    """The PR URLs one work item's latest report names, and its ``pr`` if no repo is known."""
    urls: list[str] = []
    numbers: set[int] = set()
    artifacts = getattr(item, "artifacts", None)
    values = list(artifacts.values())[:16] if isinstance(artifacts, dict) else []
    for value in values:
        if not isinstance(value, str):
            continue
        texts = [*islice(candidates(value), 4)] if "https://" in value else []
        named = _owner_repo_number(value.strip())
        if named is not None:
            url = _repo_url(f"{named[0]}/{named[1]}", int(named[2]))
            if url is not None:
                texts.append(url)
        for text in texts:
            try:
                ref = parse(text)
            except ValueError:
                continue
            if ref.kind == "change":
                urls.append(ref.url)
                numbers.add(ref.number)
    pr = getattr(item, "pr", None)
    if not isinstance(pr, int) or pr <= 0:
        return urls, None
    acceptance = getattr(item, "acceptance", None)
    repo = acceptance.get("repo") if isinstance(acceptance, dict) else None
    url = _repo_url(repo if isinstance(repo, str) else None, pr)
    if url is not None:
        try:
            reported = parse(url).url
        except ValueError:
            pass
        else:
            # Compared by URL: an artifact in another repository can share the number.
            if reported not in urls:
                urls.append(reported)
            return urls, None
    # No repository: a number an artifact URL already names is that URL.
    return urls, None if pr in numbers else pr


def _reported_at(home: str, item: Any, link: str) -> float:
    key = (home, str(getattr(item, "item_id", "") or ""), link)
    with _ledger_lock:
        held = _first_reported.get(key)
        if held is None:
            held = _first_reported[key] = row_timestamp(item.last_report_at)
            while len(_first_reported) > _LEDGER_CACHE_MAX * 4:
                _first_reported.popitem(last=False)
        return held


def _read_ledger_links(
    key: str, home: str, parse: Parse, candidates: Callable[[str], Iterator[str]]
) -> _Kept:
    """Read *key*'s ledger links. BLOCKING: a binding stat, one item read, and a
    listing of the board the chat conducts (absent for most chats)."""
    from kiro_crew import work_ledger
    from kiro_crew.session_ledger_layout import ledger_key

    links: list[tuple[str, float]] = []
    numbers: list[tuple[int, float]] = []

    def take(item: Any) -> None:
        urls, number = _item_links(item, parse, candidates)
        links.extend((url, _reported_at(home, item, url)) for url in urls)
        if number is not None:
            numbers.append((number, _reported_at(home, item, f"#{number}")))

    # Each mark is read BEFORE its store: a write landing during the read moves
    # the mark past the one kept, so the next check reads again.
    own_mark = work_ledger.change_mark(key)
    # Reported links resolve hosts against the allowlists, so a change to them
    # makes this answer stale too.
    from kiro_crew.dashboard.source_providers import hosts

    generation = hosts._gitlab_hosts_generation
    conductor, conductor_mark = "", 0
    workers: list[str] = []
    try:
        binding = work_ledger.read_binding(key)
        if binding is not None:
            conductor = binding[0]
            conductor_mark = work_ledger.change_mark(conductor)
            item = work_ledger.read_work_item(*binding)
            if item is not None and ledger_key(item.worker_session_key or "") == key:
                take(item)
    except Exception:  # noqa: BLE001 - an unreadable binding reads as no report
        logger.debug("could not read the work item %s reports to", key, exc_info=True)
    try:
        for item in work_ledger.list_work_items(key):
            take(item)
            worker = ledger_key(item.worker_session_key or "")
            # An item on a peer crew names that crew's session key, never a local one.
            if worker and worker != key and not getattr(item, "worker_instance_id", None):
                # Only while the worker is still bound to THIS item: one rebound to
                # another board creates for that board, not this one.
                bound = work_ledger.read_binding(worker)
                if bound is not None and (ledger_key(bound[0]), bound[1]) == (
                    key,
                    getattr(item, "item_id", None),
                ):
                    workers.append(worker)
    except Exception:  # noqa: BLE001 - an unreadable board reads as no items
        logger.debug("could not list the work items %s conducts", key, exc_info=True)
    # A board keeps its closed items, so a long-lived conductor's grows without a
    # bound; the answer keeps the newest links and workers, as many as a card holds.
    newest: dict[str, float] = {}
    for url, at in links:
        newest[url] = max(at, newest.get(url, at))
    kept_links = sorted(newest.items(), key=lambda pair: -pair[1])[:MAX_LEDGER_LINKS]
    kept_numbers = sorted(dict(numbers).items(), key=lambda pair: -pair[1])[:MAX_LEDGER_LINKS]
    kept_workers = list(dict.fromkeys(reversed(workers)))[:MAX_LEDGER_LINKS]
    value = LedgerLinks(tuple(kept_links), tuple(kept_numbers), tuple(kept_workers))
    return _Kept((own_mark, conductor_mark, generation), conductor, time.monotonic(), value)


def _is_fresh(kept: _Kept, key: str) -> bool:
    from kiro_crew import work_ledger
    from kiro_crew.dashboard.source_providers import hosts

    if time.monotonic() - kept.born >= LEDGER_TRUST_SECS:
        return False
    marks = (
        work_ledger.change_mark(key),
        work_ledger.change_mark(kept.conductor) if kept.conductor else 0,
        hosts._gitlab_hosts_generation,
    )
    return marks == kept.marks


def _keep(cache_key: str, kept: _Kept, held: _Kept | None) -> _Kept:
    if held is not None and held.value == kept.value:
        # The same answer keeps its identity, so the merged chip cache stays warm.
        kept = kept._replace(value=held.value)
    with _ledger_lock:
        _ledger_cache[cache_key] = kept
        _ledger_cache.move_to_end(cache_key)
        while len(_ledger_cache) > _LEDGER_CACHE_MAX:
            _ledger_cache.popitem(last=False)
    return kept


def ledger_links(
    slot_key: str, parse: Parse, candidates: Callable[[str], Iterator[str]]
) -> LedgerLinks:
    """*slot_key*'s ledger links, re-read only after a write to a store they came
    from, or after :data:`LEDGER_TRUST_SECS`.

    On the event loop the read never runs inline: a stale or missing answer
    queues one read on the default executor and serves what it has (nothing, for
    a chat never read), and the gateway's slots push runs when the new answer
    differs. Off the loop (a worker thread, a test) the read runs inline.
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_ledger_layout import ledger_key

    key = ledger_key(slot_key)
    if not key:
        return EMPTY_LEDGER_LINKS
    # The data home is part of the key: it is overridable, and two homes can
    # hold a ledger under the same slot key.
    home = str(data_home())
    cache_key = f"{home}\0{key}"
    with _ledger_lock:
        held = _ledger_cache.get(cache_key)
        if held is not None:
            _ledger_cache.move_to_end(cache_key)
    if held is not None and _is_fresh(held, key):
        return held.value
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _keep(cache_key, _read_ledger_links(key, home, parse, candidates), held).value
    _queue(loop, cache_key, key, home, parse, candidates, held)
    return held.value if held is not None else EMPTY_LEDGER_LINKS


def _queue(
    loop: asyncio.AbstractEventLoop,
    cache_key: str,
    key: str,
    home: str,
    parse: Parse,
    candidates: Callable[[str], Iterator[str]],
    held: _Kept | None,
) -> None:
    """Start one off-loop read of *key*'s ledger links unless one is running."""
    with _ledger_lock:
        if cache_key in _ledger_inflight:
            return
        _ledger_inflight.add(cache_key)

    def landed(done: "asyncio.Future[_Kept]") -> None:
        # On the loop. Keep the answer; push the slots when it changed; read
        # again at once when a write landed while this read ran.
        with _ledger_lock:
            _ledger_inflight.discard(cache_key)
        if done.cancelled() or done.exception() is not None:
            return
        kept = _keep(cache_key, done.result(), held)
        # Compared with what was last PUBLISHED for this key, not with the cached
        # entry: an entry the cache evicted is not a change.
        with _ledger_lock:
            previous = _ledger_published.get(cache_key, EMPTY_LEDGER_LINKS)
            _ledger_published[cache_key] = kept.value
            _ledger_published.move_to_end(cache_key)
            while len(_ledger_published) > _LEDGER_CACHE_MAX:
                _ledger_published.popitem(last=False)
        if kept.value != previous:
            for hook in list(_ledger_refresh_hook):
                try:
                    hook()
                except Exception:  # noqa: BLE001 - a push failure must not lose the answer
                    logger.debug("slots push after a ledger read failed", exc_info=True)
        if not _is_fresh(kept, key) and not loop.is_closed():
            _queue(loop, cache_key, key, home, parse, candidates, kept)

    try:
        future = loop.run_in_executor(None, _read_ledger_links, key, home, parse, candidates)
    except RuntimeError:
        # The executor is shut down (the gateway is stopping): serve what is kept.
        with _ledger_lock:
            _ledger_inflight.discard(cache_key)
        return
    future.add_done_callback(landed)


def set_ledger_refresh_hook(hook: Callable[[], None]) -> None:
    """Install the callback that re-publishes the slots when a ledger answer changes."""
    _ledger_refresh_hook[:] = [hook]


def reset_ledger_cache() -> None:
    """Forget every kept ledger answer (tests)."""
    with _ledger_lock:
        _ledger_cache.clear()
        _ledger_inflight.clear()
        _ledger_published.clear()
        _first_reported.clear()
