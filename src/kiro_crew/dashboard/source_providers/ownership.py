"""Which pull requests and issues a chat OWNS, for its sidebar card chips.

A URL that a chat's messages only mention (a status note, pasted context, a
compaction summary) is not the chat's. These are the signals that are:

* **created** -- a successful tool row that ran ``gh pr create`` or
  ``gh issue create``; the URL is the line gh printed in ``meta.output``.
* **acted on** -- a successful tool row that ran a ``gh pr`` write verb
  (:data:`PR_WRITE_VERBS`) or a non-GET ``gh api .../pulls/<N>/...``.
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
import json
import logging
import re
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

#: ``gh pr`` verbs that change a pull request. ``view``, ``list``, ``checks``,
#: ``diff``, ``status`` and ``checkout`` only read it, so they never count.
PR_WRITE_VERBS = frozenset({"edit", "comment", "close", "merge", "ready", "review"})

#: ``gh pr``/``gh issue`` flags that take a value, so a numeric value is never
#: read as the target (``--milestone 3`` is not pull request 3).
_VALUE_FLAGS = frozenset(
    "-R --repo -b --body -F --body-file -t --title -B --base -H --head -l --label "
    "--add-label --remove-label -a --assignee --add-assignee --remove-assignee "
    "-r --reviewer --add-reviewer --remove-reviewer -m --milestone -p --project "
    "--add-project --remove-project --subject --match-head-commit -A --author-email "
    "-T --template -q --jq --json -c --comment --reason".split()
)
#: Letters that take a value for most verbs but are on/off switches for these.
_VERB_SWITCHES = {
    "review": frozenset("-a --approve -c --comment -r --request-changes".split()),
    "merge": frozenset(
        "-m --merge -r --rebase -s --squash -d --delete-branch --auto --admin "
        "--disable-auto".split()
    ),
}
#: ``gh api`` flags that take a value.
_API_VALUE_FLAGS = frozenset(
    "-X --method -f --raw-field -F --field -H --header --hostname --input "
    "-q --jq -t --template --cache -p --preview".split()
)
#: ``gh api`` flags whose presence makes the default method POST.
_API_BODY_FLAGS = frozenset({"-f", "--raw-field", "-F", "--field", "--input"})
#: Command words that run the next word as the command.
_WRAPPERS = frozenset({"env", "command", "exec", "time", "nohup", "timeout", "nice", "stdbuf"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPER_ARG = re.compile(r"^(-.*|\d+(\.\d+)?[smhd]?)$")
_EXIT_TRAILER = re.compile(r"Exit Code: (-?\d+)\s*\Z")
_PUNCTUATION = "();<>|&\n"
#: One shell word (unquoted runs and quoted strings glued together), a run of
#: operators, or a stray character such as an unclosed quote.
_SHELL_TOKEN = re.compile(
    r"""(?:[^ \t\r\n'"();<>|&\\]+|\\.|'[^']*'|"(?:\\.|[^"\\])*")+|[();<>|&\n]+|\S""",
    re.DOTALL,
)
_QUOTED_PART = re.compile(r"""'([^']*)'|"((?:\\.|[^"\\])*)"|\\(.)""", re.DOTALL)
#: ``pull request owner/repo#123`` as gh prints it after close, merge, ready and
#: review. Anchored on gh's own words so a ``#N`` in the PR's title cannot win.
_GH_SAID_PULL = re.compile(
    r"pull request ([A-Za-z0-9][\w.-]*)/([\w.-]+)#([0-9]{1,12})\b", re.IGNORECASE
)
#: ``owner/repo#123`` written in a report's artifacts.
_OWNER_REPO_NUMBER = re.compile(r"^([A-Za-z0-9][\w.-]*)/([\w.-]+)#([0-9]{1,12})$")
_API_PULL = re.compile(r"^repos/([^/?#{}]+)/([^/?#{}]+)/pulls/([0-9]{1,12})(?:[/?#]|$)")
#: A heredoc start: ``<<EOF``, ``<<-'EOF'``, ``<< "EOF"``.
_HEREDOC = re.compile(r"""(?<!<)<<(?!<)-?[ \t]*(['"]?)([A-Za-z_][\w.-]*)\1""")
#: gh's own refusal to create a second pull request for one branch.
_GH_ALREADY_EXISTS = re.compile(r"a pull request for branch\b.*\balready exists", re.IGNORECASE)
#: Every gh call that can own a link, as it appears in a row's raw input. The
#: lexer's own rules decide; this only skips the rows that cannot match them. No
#: leading word boundary: in the JSON-encoded input a call on a new line reads
#: ``\ngh``, and the ``n`` would hide it.
_GH_WRITE_HINT = re.compile(
    r"gh\s+(?:pr\s+(?:create|edit|comment|close|merge|ready|review)|issue\s+create|api)\b"
)
#: Output URL candidates read per tool row, so one huge output costs a bounded parse.
MAX_OUTPUT_CANDIDATES = 16
#: Shell words lexed per command; a gh call past them (deep in a long script) is missed.
MAX_COMMAND_TOKENS = 2048
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
    # ``gh pr `` in its JSON.
    argv_form = '"gh"' in raw or '/gh"' in raw
    if not argv_form and (
        ("gh pr " not in raw and "gh issue " not in raw and "gh api " not in raw)
        or _GH_WRITE_HINT.search(raw) is None
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
    if not isinstance(command, str) or _GH_WRITE_HINT.search(command) is None:
        return None
    return command


def gh_write_command(meta: dict) -> str | None:
    """The shell command a tool row ran when it could own a link, else ``None``."""
    return _command_text(meta)


def tool_row_succeeded(meta: dict, output: str) -> bool:
    """Whether a tool row's command finished with exit code 0.

    The shell trailer (``Exit Code: N``) decides when it is there. A row whose
    tool is not a shell (``meta.kind`` other than ``execute``) counts only with a
    trailer, so another tool that merely takes a ``command`` argument cannot.

    shortcut: a shell backend whose output carries no trailer is read as a success
    unless the row is marked failed; store the ACP tool status on the row if a
    failure that prints a URL turns up there.
    """
    if meta.get("failed") or meta.get("error"):
        return False
    trailer = _EXIT_TRAILER.search(output)
    if trailer is not None:
        return trailer.group(1) == "0"
    return meta.get("kind") in (None, "", "execute")


def _unquote(word: str) -> str:
    """*word* with its shell quoting removed, the way the shell would pass it on."""
    if "'" not in word and '"' not in word and "\\" not in word:
        return word

    def piece(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        if match.group(2) is not None:
            return re.sub(r"\\(.)", r"\1", match.group(2), flags=re.DOTALL)
        return match.group(3)

    return _QUOTED_PART.sub(piece, word)


def _without_heredoc_bodies(command: str) -> str:
    """*command* with every heredoc's body lines removed, so they never read as commands.

    The line that opens a heredoc stays, and so does every line after its closing
    delimiter: ``cat > body.md <<'EOF' ... EOF`` followed by ``gh pr create`` keeps
    the gh call.
    """
    if "<<" not in command:
        return command
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            delimiter, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line).rstrip() == delimiter:
                pending.pop(0)
            continue
        kept.append(line)
        for match in _HEREDOC.finditer(line):
            pending.append((match.group(2), match.group(0).startswith("<<-")))
    return "\n".join(kept)


class _GhCall(NamedTuple):
    """One gh invocation in a shell command."""

    #: A ``GH_HOST=`` set on the call itself.
    host: str | None
    #: The argv after ``gh``.
    argv: list[str]
    #: Whether the row's exit code 0 proves this call ran and succeeded: every
    #: operator after it is ``&&`` and it was not reached through ``||``.
    decisive: bool


def _gh_calls(command: str) -> list[_GhCall]:
    """Every simple command in *command* that runs gh.

    A command is split at shell operators and newlines, so ``cd x && gh pr edit``
    finds the gh call; heredoc bodies are removed first, and a redirection
    (``> out``, ``2>&1``) stays part of its command. The split is a regular
    expression rather than ``shlex``: this runs on every gh row of a transcript on
    the event loop, and ``shlex`` costs about ten times as much.
    """
    text = _without_heredoc_bodies(command)
    tokens = islice(_SHELL_TOKEN.finditer(text), MAX_COMMAND_TOKENS)
    segments: list[list[str]] = [[]]
    #: The operator that ends each segment ("" for the last).
    operators: list[str] = []
    previous_end, previous_was_word, redirect_target = -1, False, False
    grouped = False
    for match in tokens:
        token = match.group(0)
        if token == "\\\n":
            # A line continuation joins two lines of one command.
            continue
        if all(ch in _PUNCTUATION for ch in token):
            if (token[0] in "<>" or token.startswith("&>")) and "\n" not in token:
                # A redirection belongs to its command, and so does its target.
                # ``2>&1``: the descriptor number glued to it is not an argument.
                if (
                    previous_was_word
                    and previous_end == match.start()
                    and segments[-1]
                    and segments[-1][-1].isdigit()
                ):
                    segments[-1].pop()
                redirect_target = True
            else:
                grouped = grouped or "(" in token or ")" in token
                operators.append(token.strip(" ") or "\n")
                segments.append([])
                redirect_target = False
            previous_was_word = False
        else:
            if redirect_target:
                redirect_target = False
            else:
                segments[-1].append(_unquote(token))
            previous_was_word = True
        previous_end = match.end()
    operators.append("")
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
    for position, words in enumerate(segments):
        index = 0
        host: str | None = None
        while index < len(words) and (
            _ASSIGNMENT.match(words[index])
            or words[index] in _WRAPPERS
            or (index and words[index - 1] != "gh" and _WRAPPER_ARG.match(words[index]))
        ):
            if words[index].startswith("GH_HOST="):
                host = words[index][len("GH_HOST=") :] or None
            index += 1
        if index < len(words) and words[index].rsplit("/", 1)[-1] == "gh":
            before = operators[position - 1] if position else ""
            decisive = not grouped and only_and_after[position] and "||" not in before
            found.append(_GhCall(host, words[index + 1 :], decisive))
    return found


def gh_invocations(command: str) -> list[list[str]]:
    """The argv after ``gh`` of every simple command in *command* that runs gh."""
    return [call.argv for call in _gh_calls(command)]


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
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--":
            continue
        if arg.startswith("-"):
            skip = arg in value_flags
            continue
        out.append(arg)
    return out


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
    parts = [part for part in re.sub(r"^https?://", "", repo).strip("/").split("/") if part]
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
            f"https://{host}/{m.group(1)}/{m.group(2)}/pull/{m.group(3)}"
            for m in _GH_SAID_PULL.finditer(output[-4096:])
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
    if target or not alone:
        # A branch name, or one of several gh calls whose outputs interleave.
        return None
    found = printed(host_hint)
    return found[0] if found else None


def _api_ref(args: list[str], parse: Parse, host_hint: str | None = None) -> SourceRef | None:
    """The pull request a non-GET ``gh api repos/<o>/<r>/pulls/<N>/...`` writes to."""
    method = (_flag_value(args, frozenset({"-X", "--method"})) or "").upper()
    if not method:
        has_body = any(
            arg in _API_BODY_FLAGS or arg.split("=", 1)[0] in _API_BODY_FLAGS for arg in args
        )
        method = "POST" if has_body else "GET"
    if method == "GET":
        return None
    positional = _positionals(args, _API_VALUE_FLAGS)
    if not positional:
        return None
    endpoint = positional[0]
    # An absolute endpoint names its own host (``https://ghe.example.com/api/v3/...``);
    # a relative one is on ``--hostname``, else on gh's default host, which is
    # certain only with no Enterprise host configured (:func:`_default_host`).
    absolute = re.match(r"^https://([^/]+)/(?:api/v3/)?", endpoint)
    if absolute is None:
        named_host = _flag_value(args, frozenset({"--hostname"})) or _default_host(host_hint)
        if named_host is None:
            return None
        host = named_host
    else:
        host = "github.com" if absolute.group(1) == "api.github.com" else absolute.group(1)
        endpoint = endpoint[absolute.end() :]
    match = _API_PULL.match(endpoint.lstrip("/"))
    if match is None:
        return None
    try:
        ref = parse(f"https://{host}/{match.group(1)}/{match.group(2)}/pull/{match.group(3)}")
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
        endpoint = _positionals(call[1:], _API_VALUE_FLAGS)
        absolute = re.match(r"^https://([^/]+)/(?:api/v3/)?", endpoint[0]) if endpoint else None
        rest = endpoint[0][absolute.end() :] if absolute else (endpoint[0] if endpoint else "")
        match = _API_PULL.match(rest.lstrip("/"))
        host = (
            ("github.com" if absolute.group(1) == "api.github.com" else absolute.group(1))
            if absolute
            else _default_host(host_hint)
        )
        text = (
            f"https://{host}/{match.group(1)}/{match.group(2)}/pull/{match.group(3)}"
            if match and host
            else None
        )
    else:
        return None
    if text is None:
        return None
    try:
        return parse(text)
    except ValueError:
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
    hosted = _gh_calls(command)
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
    if creates and _GH_ALREADY_EXISTS.search(output) is None:
        # gh prints the new URL on a line of its own. "a pull request for branch
        # ... already exists" is gh's refusal, which prints the EXISTING PR's
        # URL; a ``|| true`` or a pipe can still leave the trailer at 0. A URL
        # another gh call in the same command names (``&& gh pr view 2 --jq
        # .url``) is that call's, not the create's.
        #
        # shortcut: in a compound command, a bare PR URL line another program
        # prints (``cat links.txt``) reads as created too; split output per call
        # if that bites.
        elsewhere = {
            named.identity
            for host_hint, call, _decisive in hosted
            if not (len(call) >= 2 and call[1] == "create")
            and (named := _named_target(call, host_hint, parse)) is not None
        }
        for line in output.splitlines()[-64:]:
            line = line.strip()
            if not line.startswith("https://"):
                continue
            try:
                created = parse(line)
            except ValueError:
                continue
            if created.kind in creates and created.identity not in elsewhere:
                owned.append(OwnedRef(created, "created"))
    for host_hint, call, decisive in hosted:
        ref: SourceRef | None = None
        if "--help" in call or "-h" in call:
            # Help prints usage and exits 0 without touching anything.
            continue
        if len(call) >= 2 and call[0] == "pr" and call[1] in PR_WRITE_VERBS:
            ref = _acted_ref(
                call[1:],
                printed,
                alone=len(calls) == 1,
                parse=parse,
                host_hint=host_hint,
            )
        elif len(call) >= 2 and call[0] == "api":
            ref = _api_ref(call[1:], parse, host_hint)
        if ref is not None and not decisive:
            # ``x || gh ...`` or ``gh ...; true``: the row's exit code says
            # nothing about this call, so only gh's own output can.
            if ref.identity not in {r.identity for r in printed(host_hint)}:
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
        named = _OWNER_REPO_NUMBER.match(value.strip())
        if named is not None:
            url = _repo_url(f"{named.group(1)}/{named.group(2)}", int(named.group(3)))
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
    if not isinstance(pr, int) or pr <= 0 or pr in numbers:
        return urls, None
    acceptance = getattr(item, "acceptance", None)
    repo = acceptance.get("repo") if isinstance(acceptance, dict) else None
    url = _repo_url(repo if isinstance(repo, str) else None, pr)
    if url is not None:
        try:
            urls.append(parse(url).url)
            return urls, None
        except ValueError:
            pass
    return urls, pr


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
    from kiro_crew.session_ledger import ledger_key

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
    from kiro_crew.session_ledger import ledger_key

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
    """Forget every kept ledger answer (tests, and a data-home switch)."""
    with _ledger_lock:
        _ledger_cache.clear()
        _ledger_inflight.clear()
        _ledger_published.clear()
        _first_reported.clear()
