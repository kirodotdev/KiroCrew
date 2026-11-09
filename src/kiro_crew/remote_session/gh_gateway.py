"""Gateway side of the gh proxy: ``gh`` in a remote workspace, run here.

The gh proxy is the git proxy's sibling. A remote workspace has no GitHub
credential, so ``gh_proxy.py`` stands in for ``gh`` there (named ``gh-proxy``
everywhere it speaks). It frames the command's arguments, plus any body file it
reads from the workspace, as one JSON request to the session's git proxy at
:data:`GH_PROXY_PATH`; :class:`~kiro_crew.remote_session.git_gateway.GitProxyChannel`
hands that request to :class:`GhProxy` here, which:

1. answers only when the gh proxy setting is on, the agent sandbox tier lends a
   gateway login (never under ``strict``) and governance allows this session to
   reach GitHub (``network.egress`` for ``github.com`` and ``api.github.com``,
   fail-closed, audited);
2. accepts only the commands and flags in :data:`COMMANDS`, each value checked
   by kind; everything else is refused, and some commands (merge, review, auth,
   repo, secret, release, workflow and ``gh api`` writes) are always refused
   with their own reason;
3. scopes every command to one repository the session's git has used through
   the git proxy (:meth:`GhProxy.note_repository`), forced with ``--repo`` (or
   rebuilt into a ``gh api`` path), and refuses any other repository, URL or
   host;
4. runs the gateway's trusted ``gh`` with the gateway owner's own login, in an
   empty directory, with prompts, pagers, browsers and editors off, and sends
   back its exit code, stdout and stderr (bounded), with the gateway's GitHub
   token redacted from both should it ever appear;
5. adds an attribution line to every pull request or comment body it writes,
   saying it was made through Kiro Crew's gh proxy from a remote session, and
   writes one SEL row per command, allowed or refused.

The token never leaves this process: ``gh`` reads its own login here, and the
answer carries only output. GitHub only (``github.com``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from kiro_crew.remote_session.git_gateway import GitProxyChannel

logger = logging.getLogger(__name__)

#: The git proxy path the workspace's gh proxy posts to. Mirrors
#: ``gh_proxy.PROXY_PATH``; ``test_remote_gh_proxy`` pins the two equal.
GH_PROXY_PATH = "/kirocrew/gh"
HOST = "github.com"
API_HOST = "api.github.com"
#: Largest request the workspace may send: argv plus one body.
MAX_REQUEST_BYTES = 1024 * 1024
#: GitHub's own limit for a pull request or comment body, in characters.
MAX_BODY_CHARS = 65536
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
RUN_TIMEOUT_SECS = 300.0
LOGIN_TTL_SECS = 300.0
#: Most repositories one session's gh proxy scope holds; each costs a governed
#: git request, and one session works in a handful.
MAX_SCOPED_REPOSITORIES = 64
#: Commands that write to GitHub: allowed only where the session's git pushed.
WRITE_COMMANDS = frozenset({("pr", "create"), ("pr", "edit"), ("pr", "comment")})
_CHUNK = 24 * 1024
#: Free text (titles, labels, queries): no NUL or other control character.
_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,1024}")
_BRANCH = re.compile(r"(?!-)[A-Za-z0-9._/+-]{1,255}")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_HANDLE = re.compile(r"(?:@me|[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9._-]{1,100})?)")
_INT = re.compile(r"[0-9]{1,12}")
_SCOPE_QUALIFIER = re.compile(r"(?i)(?:repo|org|user|owner)\s*:|is\s*:\s*(?:public|private)")
#: Search syntax that regroups the query: GitHub's operators are upper case.
_SEARCH_ESCAPE = re.compile(r"[()]|\b(?:OR|AND|NOT)\b")
_FIELDS = re.compile(r"[A-Za-z]{1,64}(?:,[A-Za-z]{1,64}){0,99}")
_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_REPO = re.compile(r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<name>[A-Za-z0-9_.-]{1,100})")
_PR_URL = re.compile(r"https://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>[0-9]{1,12})/?")
_ENUMS = {
    "state": ("open", "closed", "merged", "all"),
    "color": ("always", "never", "auto"),
    "status": (
        "queued",
        "completed",
        "in_progress",
        "requested",
        "waiting",
        "pending",
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    ),
}
#: Separator of an ``owner/name`` repository and a GitHub API path: URL syntax,
#: the same on every OS, so pathlib/os.path never split it.
_SLASH = "/"
#: Marks a body this proxy already attributed, so an edit never stacks a second line.
ATTRIBUTION_MARKER = "<!-- kiro-crew-gh-proxy -->"


class GhRefusal(Exception):
    """A command the gh proxy will not run; ``rule`` names why in the audit row."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class Spec:
    """One allowed command: its flags (long name -> value kind) and positional shape.

    ``positional`` is ``pr`` (one pull request: number, branch or this
    repository's pull URL; the workspace's branch when omitted), ``run`` (one
    numeric run id, optional), ``api`` (one repository-scoped path) or ``none``.
    A value kind of ``""`` is a boolean flag.
    """

    flags: dict[str, str]
    short: dict[str, str] = field(default_factory=dict)
    positional: str = "none"
    #: Flags whose value is the body this command writes.
    body: tuple[str, ...] = ()


#: Only ``--json``: a ``--jq`` or ``--template`` expression is a program the
#: gateway's gh would run, and gojq's ``$ENV`` reads gh's environment (its
#: token among it). The workspace filters the JSON itself.
_FORMAT = {"json": "fields"}
_FORMAT_SHORT: dict[str, str] = {}
_BODY = {"body": "body", "body-file": "body-file"}
_BODY_SHORT = {"b": "body", "F": "body-file"}

COMMANDS: dict[tuple[str, str], Spec] = {
    ("pr", "create"): Spec(
        {
            "title": "text",
            **_BODY,
            "base": "branch",
            "head": "head",
            "draft": "",
            "label": "text",
            "reviewer": "handle",
            "assignee": "handle",
            "milestone": "text",
            "no-maintainer-edit": "",
        },
        {
            "t": "title",
            **_BODY_SHORT,
            "B": "base",
            "H": "head",
            "d": "draft",
            "l": "label",
            "r": "reviewer",
            "a": "assignee",
            "m": "milestone",
        },
        body=("body", "body-file"),
    ),
    ("pr", "view"): Spec({**_FORMAT, "comments": ""}, {**_FORMAT_SHORT, "c": "comments"}, "pr"),
    ("pr", "checks"): Spec({**_FORMAT, "required": ""}, dict(_FORMAT_SHORT), "pr"),
    ("pr", "diff"): Spec({"name-only": "", "patch": "", "color": "color"}, {}, "pr"),
    ("pr", "list"): Spec(
        {
            **_FORMAT,
            "state": "state",
            "limit": "int",
            "label": "text",
            "author": "handle",
            "assignee": "handle",
            "base": "branch",
            "head": "branch",
            "search": "search",
            "draft": "",
        },
        {
            **_FORMAT_SHORT,
            "s": "state",
            "L": "limit",
            "l": "label",
            "A": "author",
            "a": "assignee",
            "B": "base",
            "H": "head",
            "S": "search",
            "d": "draft",
        },
    ),
    ("pr", "edit"): Spec(
        {
            "title": "text",
            **_BODY,
            "base": "branch",
            "milestone": "text",
            "add-label": "text",
            "remove-label": "text",
            "add-reviewer": "handle",
            "remove-reviewer": "handle",
            "add-assignee": "handle",
            "remove-assignee": "handle",
        },
        {"t": "title", **_BODY_SHORT, "B": "base", "m": "milestone"},
        "pr",
        body=("body", "body-file"),
    ),
    ("pr", "comment"): Spec(dict(_BODY), dict(_BODY_SHORT), "pr", body=("body", "body-file")),
    ("run", "view"): Spec(
        {
            **_FORMAT,
            "log": "",
            "log-failed": "",
            "job": "int",
            "attempt": "int",
            "verbose": "",
            "exit-status": "",
        },
        {**_FORMAT_SHORT, "j": "job", "a": "attempt", "v": "verbose"},
        "run",
    ),
    ("run", "list"): Spec(
        {
            **_FORMAT,
            "branch": "branch",
            "limit": "int",
            "workflow": "text",
            "status": "status",
            "commit": "sha",
            "event": "text",
            "user": "handle",
            "all": "",
        },
        {
            **_FORMAT_SHORT,
            "b": "branch",
            "L": "limit",
            "w": "workflow",
            "s": "status",
            "c": "commit",
            "e": "event",
            "u": "user",
            "a": "all",
        },
    ),
    ("api", ""): Spec(
        {"method": "method", "paginate": "", "slurp": "", "include": "", "silent": ""},
        {"X": "method", "i": "include"},
        "api",
    ),
}

#: Commands refused with their own reason, whatever their flags.
ALWAYS_REFUSED: dict[tuple[str, str], str] = {
    ("pr", "merge"): "merging is the person's call, never a remote session's",
    ("pr", "review"): "reviews and approvals are the person's call, never a remote session's",
    ("pr", "close"): "closing a pull request is the person's call",
    ("pr", "reopen"): "reopening a pull request is the person's call",
    ("pr", "ready"): "marking a pull request ready is the person's call",
    ("pr", "lock"): "locking a conversation is the person's call",
    (
        "pr",
        "checkout",
    ): "it would change a checkout on the gateway machine; use git in the workspace",
    ("pr", "update-branch"): "it pushes to the pull request's branch; push with git instead",
    ("auth", ""): "the gateway's GitHub login never leaves the gateway machine",
    ("config", ""): "gh settings on the gateway machine are not changed from a remote session",
    ("repo", ""): "gh repo commands are not lent to remote sessions",
    ("secret", ""): "secrets are the person's call",
    ("variable", ""): "Actions variables are the person's call",
    ("ssh-key", ""): "SSH keys are the person's call",
    ("gpg-key", ""): "GPG keys are the person's call",
    ("release", ""): "releases are the person's call",
    ("workflow", ""): "starting or changing workflows is the person's call",
    ("extension", ""): "gh extensions would run code on the gateway machine",
    ("alias", ""): "gh aliases on the gateway machine are not changed from a remote session",
    ("codespace", ""): "codespaces are not lent to remote sessions",
    ("gist", ""): "gists are not lent to remote sessions",
    ("ruleset", ""): "rulesets are the person's call",
    ("cache", ""): "Actions caches are the person's call",
}
#: Flags refused with their own reason on every command.
REFUSED_FLAGS: dict[str, str] = {
    "web": "it would open a browser on the gateway machine",
    "editor": "it would open an editor on the gateway machine",
    "watch": "it waits without a bound; run it again instead",
    "fill": "it reads commits from a checkout on the gateway machine; pass --title and --body",
    "fill-first": "it reads commits from a checkout on the gateway machine; pass --title and --body",
    "fill-verbose": "it reads commits from a checkout on the gateway machine; pass --title and --body",
    "jq": "the expression would run on the gateway machine; pipe --json to jq or python here",
    "template": "the template would run on the gateway machine; format --json output here",
    "recover": "it reads a file on the gateway machine",
    "attach": "it reads a file on the gateway machine",
    "hostname": "the gh proxy is for github.com only",
    "repo": "",  # handled before parsing
    "auto": "auto-merge is the person's call",
    "admin": "it bypasses branch protection",
    "delete-last": "deleting comments is the person's call",
}


@dataclass(frozen=True)
class GhCommand:
    """A checked command: the argv after ``gh`` and the bytes for its stdin."""

    argv: list[str]
    stdin: bytes
    repo: str
    summary: str


def _repo_name(value: str) -> Optional[str]:
    """``owner/name`` from ``owner/name``, ``github.com/owner/name`` or its https URL."""
    text = value.strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.removesuffix("/").removesuffix(".git")
    match = _REPO.fullmatch(text)
    if match is None or match["name"] in (".", ".."):
        return None
    return _SLASH.join((match["owner"], match["name"]))


def _check_value(kind: str, flag: str, value: str, repo: str) -> str:
    """The value as gh gets it, or a refusal naming the flag."""

    def bad() -> GhRefusal:
        return GhRefusal("bad-value", f"the value of --{flag} is not one the gh proxy passes on")

    if "\x00" in value:
        raise bad()
    if kind == "text":
        if not _TEXT.fullmatch(value):
            raise bad()
    elif kind == "search":
        # gh sends ``repo:<scope> (<keywords>)``: another qualifier, a paren,
        # a boolean operator or an unclosed quote would leave that group and
        # search every repository the owner's login can read.
        if (
            not _TEXT.fullmatch(value)
            or _SCOPE_QUALIFIER.search(value)
            or _SEARCH_ESCAPE.search(value)
            or value.count('"') % 2
        ):
            raise GhRefusal(
                "other-repository",
                f"--search stays inside {repo}; drop repo:, org: and user: qualifiers",
            )
    elif kind == "fields":
        if not _FIELDS.fullmatch(value):
            raise bad()
    elif kind == "branch":
        if not _BRANCH.fullmatch(value) or ".." in value:
            raise bad()
    elif kind == "head":
        owner, colon, branch = value.rpartition(":")
        if colon and owner.lower() != repo.split(_SLASH)[0].lower():
            raise GhRefusal(
                "other-repository",
                f"--head {value} names another owner's branch; the gh proxy is scoped to {repo}",
            )
        if not _BRANCH.fullmatch(branch) or ".." in branch:
            raise bad()
    elif kind == "handle":
        if not all(_HANDLE.fullmatch(part) for part in value.split(",")):
            raise bad()
    elif kind == "int":
        if not _INT.fullmatch(value):
            raise bad()
    elif kind == "sha":
        if not _SHA.fullmatch(value):
            raise bad()
    elif kind == "method":
        if value.upper() != "GET":
            raise GhRefusal(
                "api-write", "gh api runs read-only GET requests only through the gh proxy"
            )
        value = "GET"
    elif kind in _ENUMS:
        if value not in _ENUMS[kind]:
            raise bad()
    elif kind not in ("body", "body-file"):
        raise bad()
    return value


def _takes_value(spec: Spec, token: str) -> bool:
    """Whether ``token`` is one of ``spec``'s flags written apart from its value."""
    if token.startswith("--") and "=" not in token:
        name = token[2:]
    elif len(token) == 2 and token.startswith("-"):
        name = spec.short.get(token[1], "")
    else:
        return False
    return bool(spec.flags.get(name))


def pr_value_flags() -> frozenset[str]:
    """Every ``gh pr`` flag that takes the next argument as its value.

    Mirrors ``gh_proxy.PR_VALUE_FLAGS``; ``test_remote_gh_proxy`` pins the two equal.
    """
    names: set[str] = set()
    for (group, _sub), spec in COMMANDS.items():
        if group != "pr":
            continue
        names.update(f"--{n}" for n, kind in spec.flags.items() if kind)
        names.update(f"-{s}" for s, n in spec.short.items() if spec.flags.get(n))
    return frozenset(names | {"--repo", "-R"})


def _take_repo_flag(argv: list[Any]) -> tuple[list[str], str, dict[int, int]]:
    """``argv`` without its ``--repo``/``-R``, the repository it named ("" when none),
    and each kept argument's old index -> its new one (body files are keyed by index).

    The gateway reads the repository from the command itself, never from a
    separate claim, and passes its own ``--repo`` once the scope check passes.
    """
    if not all(isinstance(a, str) for a in argv):
        raise GhRefusal("bad-request", "the gh proxy request could not be read")
    key = ("api", "") if argv[:1] == ["api"] else (argv[0], argv[1]) if len(argv) > 1 else None
    spec = COMMANDS.get(key) if key is not None else None
    kept: list[str] = []
    named: list[str] = []
    moved: dict[int, int] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1
        if spec is not None and _takes_value(spec, token) and index < len(argv):
            # The next argument is this flag's value, whatever it looks like.
            moved[index - 1] = len(kept)
            kept.append(token)
            moved[index] = len(kept)
            kept.append(argv[index])
            index += 1
            continue
        if token in ("--repo", "-R"):
            if index >= len(argv):
                raise GhRefusal("bad-flag", "--repo needs a value")
            named.append(argv[index])
            index += 1
        elif token.startswith(("--repo=", "-R=")):
            named.append(token.partition("=")[2])
        else:
            moved[index - 1] = len(kept)
            kept.append(token)
    if len(named) > 1:
        raise GhRefusal("bad-flag", "give --repo once")
    return kept, (named[0] if named else ""), moved


def _request_shape(request: Any) -> tuple[dict[str, str], str]:
    """The files and branch of a well-formed ``run`` request, or a refusal."""
    refusal = GhRefusal("bad-request", "the gh proxy request could not be read")
    if not isinstance(request, dict) or request.get("op") != "run":
        raise refusal
    argv = request.get("argv")
    files = request.get("files") or {}
    branch = request.get("branch") or ""
    if (
        not isinstance(argv, list)
        or not 0 < len(argv) <= 256
        or not isinstance(files, dict)
        or not all(
            isinstance(k, str)
            and k.isascii()
            and k.isdigit()
            and len(k) <= 3
            and isinstance(v, str)
            for k, v in files.items()
        )
        or not isinstance(branch, str)
    ):
        raise refusal
    return files, branch


def _api_path(value: str, repo: str) -> str:
    """A ``gh api`` path inside ``repos/<repo>``; ``{owner}``/``{repo}`` filled here."""
    owner, name = repo.split(_SLASH)
    path, _, query = value.lstrip("/").partition("?")
    path = path.replace("{owner}", owner).replace("{repo}", name)
    refusal = GhRefusal(
        "other-repository",
        f"gh api reaches only paths under repos/{repo}/ through the gh proxy",
    )
    if "{" in path or "}" in path or "\\" in path or "%" in path or "//" in path:
        raise refusal
    segments = path.split(_SLASH)
    if len(segments) < 3 or segments[0] != "repos":
        raise refusal
    if _SLASH.join(segments[1:3]).lower() != repo.lower():
        raise refusal
    for segment in segments[3:]:
        if segment in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_.@,:+=~-]{1,255}", segment):
            raise refusal
    if query and not re.fullmatch(r"[A-Za-z0-9_.,:+=&%~-]{1,2048}", query):
        raise refusal
    return path + (f"?{query}" if query else "")


def _scrubbed(text: str) -> str:
    """Agent-written text, as posted to GitHub: credentials and exfiltration URLs redacted.

    One composed pass (both redactors find their spans on the original text),
    through the platform context so a loaded companion's patterns apply: run
    one after the other, the URL pass can cut a credential below the matcher.
    """
    from kiro_crew.platform.context import redact_via_context

    return redact_via_context(text)


def _audit_safe(text: str, withheld: str) -> str:
    """``text`` for an audit row, or ``withheld`` when it carries a secret in any encoding."""
    return withheld if _carries_secret(text, api_path=False) else text


def _attributed(body: str, login: str) -> str:
    """``body`` led by exactly one gh proxy attribution line.

    First, so nothing in the body (an unclosed ``<!--`` or ``<details>``) can
    fold it out of sight. Any line already carrying the marker is dropped, so
    an edit never stacks a second line and a planted marker cannot stand in
    for the real one.
    """
    kept = [line for line in body.splitlines() if ATTRIBUTION_MARKER not in line]
    body = "\n".join(kept).strip()
    line = (
        f"_Made by an agent in a Kiro Crew remote session, through Kiro Crew's gh proxy "
        f"as @{login}._ {ATTRIBUTION_MARKER}"
    )
    return f"{line}\n\n{body}" if body else line


def parse_command(
    argv: list[str],
    *,
    repo: str,
    branch: str,
    files: dict[str, str],
    login: str,
) -> GhCommand:
    """Check one workspace command and build what the gateway's gh runs.

    The one outbound chokepoint: free text is already redacted, so any argument
    the redactors would still change (an API query, a branch, head, base or
    handle carrying a credential or an exfiltration URL) is refused rather than
    sent to GitHub as the gateway owner. Bodies go on stdin, redacted.
    """
    command = _build_command(argv, repo=repo, branch=branch, files=files, login=login)
    for at, arg in enumerate(command.argv):
        if _carries_secret(arg, api_path=command.argv[0] == "api" and at == 1):
            raise GhRefusal(
                "credential-in-argument",
                "an argument looks like a credential or an exfiltration URL; "
                "the gh proxy does not send it to GitHub",
            )
    return command


def _carries_secret(arg: str, *, api_path: bool) -> bool:
    """Whether a redactor would change ``arg`` as sent, decoded, or as GitHub sees it.

    Each percent-decoding layer is checked (gh and GitHub decode it), and an API
    path is also checked as the full ``https://api.github.com/`` URL, the form
    the exfiltration-URL redactor recognises.
    """
    from urllib.parse import unquote_plus

    seen = arg
    for _ in range(4):
        forms = [seen, f"https://{API_HOST}/{seen.lstrip('/')}"] if api_path else [seen]
        if any(_scrubbed(form) != form for form in forms):
            return True
        decoded = unquote_plus(seen)
        if decoded == seen:
            return False
        seen = decoded
    return True  # still encoded after four layers: nothing ordinary needs that


def _build_command(
    argv: list[str],
    *,
    repo: str,
    branch: str,
    files: dict[str, str],
    login: str,
) -> GhCommand:
    """Check one workspace command and build what the gateway's gh runs.

    ``argv`` follows ``gh``; ``repo`` is the scoped repository (already
    resolved and allowed by the caller); ``branch`` is the workspace's current
    branch, used for a pull request command that names none; ``files`` holds
    the body file contents the workspace read, keyed by the argv index of the
    file argument. Raises :class:`GhRefusal`.
    """
    if not argv or not all(isinstance(a, str) for a in argv):
        raise GhRefusal("not-allowed", "name a gh command; run gh help to see what is allowed")
    group = argv[0]
    has_sub = group != "api" and len(argv) > 1 and not argv[1].startswith("-")
    sub = argv[1] if has_sub else ""
    for key in ((group, sub), (group, "")):
        if key in ALWAYS_REFUSED:
            raise GhRefusal(
                "always-refused",
                f"gh {' '.join(k for k in key if k)} is refused: {ALWAYS_REFUSED[key]}",
            )
    spec = COMMANDS.get((group, "" if group == "api" else sub))
    if spec is None:
        named = " ".join(argv[:2]) if has_sub else group
        raise GhRefusal("not-allowed", f"gh {named} is not one the gh proxy runs")
    rest = argv[1:] if group == "api" else argv[2:]
    offset = len(argv) - len(rest)
    out: list[str] = []
    positionals: list[str] = []
    stdin = b""
    body_set = False
    index = 0
    while index < len(rest):
        token = rest[index]
        where = offset + index
        index += 1
        if token == "--" or not token.startswith("-") or token == "-":
            positionals.append(token)
            continue
        if token.startswith("--"):
            name, eq, inline = token[2:].partition("=")
        else:
            if len(token) != 2 and token[2] != "=":
                raise GhRefusal(
                    "bad-flag", f"write {token[:2]} and its value as separate arguments"
                )
            letter, eq, inline = token[1], token[2:3], token[3:]
            name = spec.short.get(letter, "")
            if not name:
                reason = {"w": "web", "e": "editor", "q": "jq", "t": "template"}.get(letter, "")
                if reason and reason in REFUSED_FLAGS and reason not in spec.flags:
                    raise GhRefusal("refused-flag", f"{token} is refused: {REFUSED_FLAGS[reason]}")
                raise GhRefusal("bad-flag", f"{token} is not a flag the gh proxy passes on here")
        if name in REFUSED_FLAGS and name not in spec.flags:
            reason = REFUSED_FLAGS[name]
            if name == "template" and (group, sub) == ("pr", "create"):
                reason = "it reads a file on the gateway machine"
            if name == "repo":
                reason = f"the gh proxy is scoped to {repo}"
            raise GhRefusal("refused-flag", f"--{name} is refused: {reason}")
        if name not in spec.flags:
            raise GhRefusal("bad-flag", f"--{name} is not a flag the gh proxy passes on here")
        kind = spec.flags[name]
        if kind == "":
            if eq:
                raise GhRefusal("bad-flag", f"--{name} takes no value")
            out.append(f"--{name}")
            continue
        if eq:
            value = inline
            value_at = where
        else:
            if index >= len(rest):
                raise GhRefusal("bad-flag", f"--{name} needs a value")
            value = rest[index]
            value_at = offset + index
            index += 1
        if name in spec.body:
            if body_set:
                raise GhRefusal("bad-flag", "give the body once, with --body or --body-file")
            body_set = True
            if kind == "body-file":
                text = files.get(str(value_at))
                if text is None:
                    raise GhRefusal(
                        "body-file-unread",
                        f"the workspace did not send the contents of --body-file {value}",
                    )
            else:
                text = value
            text = _attributed(_scrubbed(text), login)
            if len(text) > MAX_BODY_CHARS:
                raise GhRefusal(
                    "body-too-long",
                    f"the body is {len(text)} characters with the gh proxy's attribution "
                    f"line; GitHub takes at most {MAX_BODY_CHARS}",
                )
            # Every body goes through stdin: no length limit on an argument,
            # and no file is written on the gateway.
            stdin = text.encode("utf-8")
            out += ["--body-file", "-"]
            continue
        checked = _check_value(kind, name, value, repo)
        # Free text goes to GitHub as the gateway owner: redacted like a body.
        out += [f"--{name}", _scrubbed(checked) if kind in ("text", "search") else checked]
    if group == "pr" and sub == "comment" and not body_set:
        raise GhRefusal(
            "bad-flag", "gh pr comment needs --body or --body-file through the gh proxy"
        )
    if group == "pr" and sub == "create" and not body_set:
        # gh asks for a body without one; the attribution line is the body.
        out += ["--body-file", "-"]
        stdin = _attributed("", login).encode("utf-8")
    if group == "pr" and sub == "create":
        if "--title" not in out:
            raise GhRefusal("bad-flag", "gh pr create needs --title through the gh proxy")
        if "--head" not in out:
            if not branch:
                raise GhRefusal(
                    "no-branch",
                    "pass --head: the workspace is not on a branch the gh proxy can name",
                )
            out += ["--head", _check_value("branch", "head", branch, repo)]
    summary = f"{group} {sub}".strip()
    if spec.positional == "none":
        if positionals:
            raise GhRefusal("bad-argument", f"gh {summary} takes no argument through the gh proxy")
        return GhCommand([group, sub, *out, "--repo", repo], stdin, repo, summary)
    if len(positionals) > 1:
        raise GhRefusal("bad-argument", f"gh {summary} takes one argument through the gh proxy")
    arg = positionals[0] if positionals else ""
    if spec.positional == "api":
        if not arg:
            raise GhRefusal("bad-argument", "gh api needs a path under repos/OWNER/NAME/")
        path = _api_path(arg, repo)
        return GhCommand(["api", path, *out], stdin, repo, f"api {path.split('?')[0]}")
    if spec.positional == "run":
        if arg and not _INT.fullmatch(arg):
            raise GhRefusal("bad-argument", "name the run by its numeric id")
        return GhCommand(
            [group, sub, *([arg] if arg else []), *out, "--repo", repo], stdin, repo, summary
        )
    # A pull request: number, branch or this repository's pull URL.
    if not arg:
        if not branch:
            raise GhRefusal("no-branch", "name the pull request: the workspace is not on a branch")
        arg = branch
    url = _PR_URL.fullmatch(arg)
    if url is not None:
        if url["repo"].lower() != repo.lower():
            raise GhRefusal(
                "other-repository",
                f"{arg} is in another repository; the gh proxy is scoped to {repo}",
            )
        arg = url["number"]
    elif "://" in arg or ("/" in arg and arg.split("/", 1)[0].lower() == "github.com"):
        raise GhRefusal(
            "other-repository", f"the gh proxy is scoped to {repo}; name the pull request by number"
        )
    elif not (_INT.fullmatch(arg) or (_BRANCH.fullmatch(arg) and ".." not in arg)):
        raise GhRefusal("bad-argument", "name the pull request by number or branch")
    return GhCommand([group, sub, arg, *out, "--repo", repo], stdin, repo, f"{summary} {arg}")


# ── Running gh on the gateway ────────────────────────────────────────────


def _gh_env() -> dict[str, str]:
    """gh's environment: its own login, nothing interactive, github.com only."""
    from kiro_crew.remote_session.git_gateway import _credential_env

    env = _credential_env("gh")
    for key in tuple(env):
        if key.upper() in (
            "GH_REPO",
            "GH_DEBUG",
            "DEBUG",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
        ):
            del env[key]
    env.update(
        GH_HOST=HOST,
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
        GH_NO_EXTENSION_UPDATE_NOTIFIER="1",
        GH_SPINNER_DISABLED="1",
        GH_PAGER="cat",
        PAGER="cat",
        NO_COLOR="1",
        GH_BROWSER="false",
        BROWSER="false",
        GH_EDITOR="false",
        EDITOR="false",
        VISUAL="false",
    )
    return env


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    """Up to ``cap`` bytes of ``stream``, reading the rest to its end unkept."""
    kept = bytearray()
    cut = False
    while True:
        piece = await stream.read(_CHUNK)
        if not piece:
            return bytes(kept), cut
        room = cap - len(kept)
        if room > 0:
            kept += piece[:room]
        if len(piece) > room:
            cut = True


async def run_gh(argv: list[str], stdin: bytes) -> tuple[int, bytes, bytes]:
    """Run the gateway's trusted gh with ``argv``; (exit code, stdout, stderr)."""
    from kiro_crew.remote_session.git_gateway import _binary, _kill_child

    gh = await asyncio.to_thread(_binary, "gh")
    if gh is None:
        raise GhRefusal("no-gh", "the gateway machine has no trusted gh installed")
    env = await asyncio.to_thread(_gh_env)
    # An empty directory: gh finds no checkout of the gateway's to read.
    workdir = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-gh-proxy-")
    try:
        process = await asyncio.create_subprocess_exec(
            gh,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir,
            start_new_session=True,
        )

        async def feed() -> None:
            assert process.stdin is not None
            with contextlib.suppress(ConnectionError):
                process.stdin.write(stdin)
                await process.stdin.drain()
            process.stdin.close()

        assert process.stdout is not None and process.stderr is not None
        try:
            _, (out, out_cut), (err, err_cut), code = await asyncio.wait_for(
                asyncio.gather(
                    feed(),
                    _read_capped(process.stdout, MAX_STDOUT_BYTES),
                    _read_capped(process.stderr, MAX_STDERR_BYTES),
                    process.wait(),
                ),
                RUN_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            await _kill_child(process)
            raise GhRefusal(
                "timed-out", f"gh did not finish within {int(RUN_TIMEOUT_SECS)}s"
            ) from None
        except asyncio.CancelledError:
            await _kill_child(process)
            raise
        if out_cut:
            err += f"\ngh-proxy: output cut at {MAX_STDOUT_BYTES} bytes\n".encode()
        if err_cut:
            err += b"\ngh-proxy: error output cut\n"
        return code, out, err
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def gateway_login(
    runner: Callable[[list[str], bytes], Awaitable[tuple[int, bytes, bytes]]] = run_gh,
) -> str | None:
    """The GitHub login the gh proxy would act as, for Settings to name; never raises.

    The same trusted gh and environment every proxied command runs with, so the
    name shown before the switch is turned on is the one the commands will carry.
    """
    try:
        code, out, _ = await runner(["api", "user", "--jq", ".login"], b"")
    except (GhRefusal, OSError):
        return None
    login = out.decode("utf-8", "replace").strip()
    return login if code == 0 and _LOGIN.fullmatch(login) else None


def _secret_of(credential: str) -> str:
    """The token inside the git proxy's ``Basic`` value for GitHub, or ""."""
    if not credential.startswith("Basic "):
        return ""
    try:
        decoded = base64.b64decode(credential[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    return decoded.partition(":")[2]


def _redact_all(data: bytes, secrets: list[str]) -> bytes:
    for secret in secrets:
        data = _redact(data, secret)
    return data


def _redact(data: bytes, secret: str) -> bytes:
    return (
        data.replace(secret.encode(), b"[gh-proxy: token redacted]") if len(secret) >= 8 else data
    )


def gh_proxy_enabled() -> bool:
    """``session_environments.gh_proxy``; an unreadable setting lends nothing."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        section = KiroCrewConfig.load().session_environments
        return getattr(section, "gh_proxy", False) is True
    except Exception:  # noqa: BLE001 -- fail closed: lending needs a known setting
        logger.warning("gh proxy: session environment settings unreadable; refusing")
        return False


class GhProxy:
    """One session's gh proxy, owned by its :class:`GitProxyChannel`."""

    def __init__(
        self,
        channel: "GitProxyChannel",
        *,
        runner: Callable[[list[str], bytes], Awaitable[tuple[int, bytes, bytes]]] = run_gh,
        enabled: Callable[[], bool] = gh_proxy_enabled,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channel = channel
        self._runner = runner
        self._enabled = enabled
        self._clock = clock
        #: github.com repositories (``owner/name``, as git named them) this
        #: session's git reached through the git proxy: the gh proxy's scope.
        self.repositories: dict[str, str] = {}
        #: Repositories past :data:`MAX_SCOPED_REPOSITORIES`, left out of the
        #: scope; named so a refusal says why. Bounded by the same cap.
        self.dropped: set[str] = set()
        #: Keys of :attr:`repositories` the session's git pushed to.
        self.pushed: set[str] = set()
        self._login: Optional[tuple[str, float]] = None

    def note_repository(self, host: str, repo_path: str, *, pushed: bool = False) -> None:
        """Record a repository the git proxy carried a request for.

        Any request makes it readable through the gh proxy; only a push the
        host accepted with the gateway's login (``pushed``) makes it writable (``pr
        create``, ``edit``, ``comment``): reading a repository is not leave to
        comment on it as the gateway owner. Bounded by
        :data:`MAX_SCOPED_REPOSITORIES`; past it, new repositories are not added.
        """
        if host != HOST:
            return
        name = _repo_name(repo_path)
        if name is None:
            return
        key = name.lower()
        if key not in self.repositories:
            if len(self.repositories) >= MAX_SCOPED_REPOSITORIES:
                if key not in self.dropped and len(self.dropped) < MAX_SCOPED_REPOSITORIES:
                    self.dropped.add(key)
                    logger.info("gh proxy %s: scope is full", self.channel.session_key)
                return
            self.repositories[key] = name
        if pushed:
            self.pushed.add(key)

    def scope_text(self) -> str:
        names = sorted(self.repositories.values())
        return (
            ", ".join(names)
            if names
            else "no repository yet (git has not used one through the git proxy)"
        )

    async def login(self, *, fresh: bool = False) -> str:
        """The gateway owner's GitHub login, read with ``gh api user``.

        ``fresh`` reads it again whatever is cached: a command is attributed to
        the account gh holds when it runs, so switching gh's active account
        between two calls never leaves a stale name on the next one. ``about``
        answers (``gh --version``, ``gh help``) may use the cache.
        """
        cached = self._login
        if not fresh and cached is not None and self._clock() - cached[1] < LOGIN_TTL_SECS:
            return cached[0]
        code, out, _ = await self._runner(["api", "user", "--jq", ".login"], b"")
        login = out.decode("utf-8", "replace").strip()
        if code != 0 or not _LOGIN.fullmatch(login):
            raise GhRefusal(
                "no-login", "the gateway machine has no GitHub login; run gh auth login there"
            )
        self._login = (login, self._clock())
        return login

    async def _admit(self) -> None:
        """The checks every gh proxy call passes before any gh runs, in order."""
        from kiro_crew.remote_session.git_gateway import lending_unverifiable_off_loop

        channel = self.channel
        if not await asyncio.to_thread(self._enabled):
            raise GhRefusal(
                "turned-off",
                "the gh proxy is off; the person can turn it on in Settings > Session Environments",
            )
        if not await asyncio.to_thread(channel._logins_lent):
            if await lending_unverifiable_off_loop():
                raise GhRefusal(
                    "windows-unverified",
                    "this Windows gateway cannot verify its own account, so it lends no GitHub login",
                )
            raise GhRefusal(
                "strict-sandbox",
                "the strict agent sandbox lends no gateway login, so gh is refused",
            )
        agent = channel.agent
        if not isinstance(agent, str) or not agent.strip():
            raise GhRefusal("no-agent", "governance does not allow GitHub here")
        for host in (HOST, API_HOST):
            if not await asyncio.to_thread(
                channel._egress_permitted, channel.session_key, host, agent
            ):
                raise GhRefusal("governance", f"governance does not allow {host} here")

    def _scoped_repo(self, requested: Any, workspace_repo: Any) -> str:
        """The one repository this call may reach, or a refusal."""
        named = requested if isinstance(requested, str) and requested else workspace_repo
        if not isinstance(named, str) or not named:
            raise GhRefusal(
                "no-repository",
                "run gh inside a checkout whose origin is on github.com, or pass --repo OWNER/NAME",
            )
        name = _repo_name(named)
        if name is None:
            # Scrubbed whole, then clipped: a cut credential escapes the matcher.
            shown = _audit_safe(_scrubbed(named), "[withheld]")[:200]
            raise GhRefusal("other-repository", f"{shown} is not a github.com repository")
        lent = self.repositories.get(name.lower())
        if lent is None and name.lower() in self.dropped:
            raise GhRefusal(
                "scope-full",
                f"{name} is not in scope: this session's gh proxy holds at most "
                f"{MAX_SCOPED_REPOSITORIES} repositories, and that many were in use first",
            )
        if lent is None:
            raise GhRefusal(
                "other-repository",
                f"{name} is not a repository this session's git uses through the git proxy",
            )
        return lent

    def _check_write(self, argv: list[str], repo: str) -> None:
        if tuple(argv[:2]) in WRITE_COMMANDS and repo.lower() not in self.pushed:
            raise GhRefusal(
                "not-pushed",
                f"gh {argv[0]} {argv[1]} writes to {repo}, which this session has not pushed "
                "to through the git proxy; push a branch there first",
            )

    async def handle(self, request: Any) -> dict[str, Any]:
        """Answer one decoded request: ``about`` or ``run``."""
        if isinstance(request, dict) and request.get("op") == "about":
            return await self._about()
        argv = request.get("argv") if isinstance(request, dict) else None
        # Withheld whole, never clipped first: a refused argument may be the very
        # credential refused, in any percent-encoding.
        summary = (
            _audit_safe(" ".join(str(a) for a in argv[:2]), "[withheld]")[:120]
            if isinstance(argv, list)
            else "?"
        )
        repo = ""
        try:
            files, branch = _request_shape(request)
            argv = list(request["argv"])
            await self._admit()
            argv, requested, moved = _take_repo_flag(argv)
            files = {str(moved[int(k)]): v for k, v in files.items() if int(k) in moved}
            repo = self._scoped_repo(requested, request.get("workspace_repo"))
            self._check_write(argv, repo)
            login = await self.login(fresh=True)
            command = parse_command(argv, repo=repo, branch=branch, files=files, login=login)
        except GhRefusal as refusal:
            # The message can echo the refused value; the audit row keeps it redacted.
            await self._audit(
                f"{repo or '-'} gh {summary}",
                "denied",
                refusal.rule,
                _audit_safe(refusal.message, f"{refusal.rule} (the refused value is withheld)"),
            )
            raise
        # The git proxy reads the same login, and the answer must never carry it.
        try:
            secret = _secret_of(await self.channel._credential_now(HOST))
        except Exception:  # noqa: BLE001 -- gh still has its own login; nothing to redact
            secret = ""
        # gh may also authenticate from these, which can differ from the git login.
        secrets = [secret, *(os.environ.get(k, "") for k in ("GH_TOKEN", "GITHUB_TOKEN"))]
        await self._audit(f"{repo} gh {command.summary}", "allowed", "allowlisted", "")
        logger.info("gh proxy %s: gh %s on %s", self.channel.session_key, command.summary, repo)
        code, out, err = await self._runner(command.argv, command.stdin)
        return {
            "code": code,
            "stdout": base64.b64encode(_redact_all(out, secrets)).decode("ascii"),
            "stderr": base64.b64encode(_redact_all(err, secrets)).decode("ascii"),
            "login": login,
            "repo": repo,
            "ran": True,
        }

    async def _about(self) -> dict[str, Any]:
        """Who the gh proxy runs as and what it is scoped to; runs no command but ``gh api user``."""
        try:
            await self._admit()
            login = await self.login()
        except GhRefusal as refusal:
            await self._audit("- gh about", "denied", refusal.rule, refusal.message)
            return {
                "login": "",
                "repositories": sorted(self.repositories.values()),
                "refused": refusal.message,
            }
        return {"login": login, "repositories": sorted(self.repositories.values())}

    async def _audit(self, item: str, outcome: str, rule: str, reason: str) -> None:
        """One SEL governance-decision row per gh proxy call; never changes the answer."""
        channel = self.channel
        agent = (
            channel.agent
            if isinstance(channel.agent, str) and channel.agent.strip()
            else "kirocrew"
        )

        def write() -> None:
            from kiro_crew.sel import sel

            sel().log_governance_decision(
                session_key=channel.session_key,
                agent=agent,
                tool_name="gh_proxy",
                scope="gh.command",
                # The one audit chokepoint: whole values are checked before
                # clipping, so no cut can leave a credential fragment behind.
                item=_audit_safe(item, "[withheld]")[:512],
                outcome=outcome,
                rule=rule,
                layer="gh_proxy",
                reason=_audit_safe(reason, f"{rule} (the refused value is withheld)"),
            )

        try:
            await asyncio.to_thread(write)
        except Exception:  # noqa: BLE001 -- the audit row must never change the answer
            logger.warning("gh proxy %s: audit write failed", channel.session_key)

    async def answer(self, rid: int, frame: dict, body: "asyncio.Queue[Optional[bytes]]") -> None:
        """Read one request's body off the link and send back its JSON answer.

        A refusal is an answer, not an HTTP error: the workspace prints its
        message and exits 1, as ``gh`` does for a command it cannot run.
        """
        from kiro_crew.remote_session.git_gateway import GitProxyRefusal

        channel = self.channel
        if str(frame.get("method") or "") != "POST":
            message = "gh-proxy: requests are POST"
            await self._audit("- gh ?", "denied", "bad-request", message)
            raise GitProxyRefusal(405, message)
        raw = bytearray()
        async for piece in channel._drain(rid, body):
            if len(raw) + len(piece) > MAX_REQUEST_BYTES:
                message = "gh-proxy: the command and its body are too large"
                await self._audit("- gh ?", "denied", "too-large", message)
                raise GitProxyRefusal(413, message)
            raw += piece
        try:
            request = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            request = None
        try:
            answer = await self.handle(request)
        except GhRefusal as refusal:
            answer = {
                "code": 1,
                "refused": refusal.message,
                "repositories": sorted(self.repositories.values()),
                "login": self._login[0] if self._login else "",
                "ran": False,
            }
        data = json.dumps(answer, separators=(",", ":")).encode("ascii")
        await channel._send(
            {"t": "resp", "id": rid, "status": 200, "headers": {"Content-Type": "application/json"}}
        )
        for start in range(0, len(data), _CHUNK):
            chunk = data[start : start + _CHUNK]
            await channel._send(
                {"t": "body", "id": rid, "b": base64.b64encode(chunk).decode("ascii")}
            )
        await channel._send({"t": "end", "id": rid})
