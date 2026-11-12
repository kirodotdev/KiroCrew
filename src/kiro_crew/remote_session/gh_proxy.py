"""Workspace-side gh proxy: ``gh`` in a remote workspace, run on the gateway.

The git proxy's sibling. ``workspace_tools.run_command`` puts a ``gh`` shim
that runs this file at the END of each command's ``PATH``, so a workspace that
has its own ``gh`` keeps it, and one without gets this stand-in. It never runs
GitHub's ``gh`` here and holds no credential: it sends the command's
arguments, the workspace's ``origin`` repository and branch, and any body file
it reads from the workspace, to the session's git proxy on guest loopback
(:data:`PROXY_PATH`), which carries them over the authenticated link to the
gateway. The gateway checks and runs the command with its own ``gh`` and
login, and answers with the exit code and output, which this prints.

It says what it is: ``gh --version``, ``gh help``, ``gh auth status`` and every
refusal name it ``gh-proxy (Kiro Crew)`` and say that commands run on the
gateway machine as the gateway owner's GitHub login, scoped to this session's
repository; its own messages on stderr start with ``gh-proxy:``. Each command
the gateway ran is noted in the file ``KIROCREW_GH_PROXY_NOTE`` names, so the
tool result says so even when the command hides its stderr.

Stdlib only and Python 3.9 compatible.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

NAME = "gh-proxy (Kiro Crew)"
#: Mirrors ``gh_gateway.GH_PROXY_PATH``; ``test_remote_gh_proxy`` pins the two equal.
PROXY_PATH = "/kirocrew/gh"
ENV_PORT_FILE = "KIROCREW_GH_PROXY_PORT_FILE"
ENV_SECRET = "KIROCREW_GH_PROXY_AUTH"
ENV_ROOT = "KIROCREW_GH_PROXY_ROOT"
ENV_NOTE = "KIROCREW_GH_PROXY_NOTE"
#: GitHub's own body limit is 65,536 characters; a file far past it is refused here.
MAX_BODY_FILE_BYTES = 512 * 1024
#: Past the gateway's own bounds for one call: its login lookup and the command,
#: 300 s each, so the gateway answers first and a slow call is never retried twice.
ANSWER_TIMEOUT_SECS = 630.0
_GITHUB_ORIGIN = re.compile(
    r"(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repo>[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?"
)
#: Every ``gh pr`` flag that takes the next argument as its value, so a value
#: such as ``--body -F`` is never read as a flag. Mirrors
#: ``gh_gateway.pr_value_flags()``; ``test_remote_gh_proxy`` pins the two equal.
PR_VALUE_FLAGS = frozenset(
    {
        "--add-assignee",
        "--add-label",
        "--add-reviewer",
        "--assignee",
        "--author",
        "--base",
        "--body",
        "--body-file",
        "--color",
        "--head",
        "--json",
        "--label",
        "--limit",
        "--milestone",
        "--remove-assignee",
        "--remove-label",
        "--remove-reviewer",
        "--repo",
        "--reviewer",
        "--search",
        "--state",
        "--title",
        "-A",
        "-B",
        "-F",
        "-H",
        "-L",
        "-R",
        "-S",
        "-a",
        "-b",
        "-l",
        "-m",
        "-r",
        "-s",
        "-t",
    }
)
ALLOWED = (
    "pr create, pr view, pr edit, pr comment, pr checks, pr diff, pr list, "
    "run view, run list, and read-only gh api GET requests under repos/OWNER/NAME/"
)
REFUSED = (
    "pr merge, pr review, pr close, auto-merge, auth, config, repo, secret, variable, "
    "ssh-key, release, workflow, extension, gh api writes, and --jq or --template "
    "(use --json and filter the output here)"
)


def say(text: str) -> None:
    for line in text.splitlines() or [""]:
        sys.stderr.write(f"gh-proxy: {line}\n")
    sys.stderr.flush()


def _git(args: List[str]) -> str:
    try:
        done = subprocess.run(
            ["git", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""


def workspace_repo() -> str:
    """``owner/name`` of ``origin`` when it is on github.com, as configured.

    The configured URL, not the rewritten one: the git proxy's ``insteadOf``
    rewrite applies to fetches, and ``git config`` returns the URL as written.
    """
    match = _GITHUB_ORIGIN.fullmatch(_git(["config", "--get", "remote.origin.url"]))
    return match["repo"] if match else ""


def current_branch() -> str:
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"])
    return branch if branch and branch != "HEAD" else ""


def _within(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def body_files(argv: List[str]) -> Dict[str, str]:
    """The body files a ``gh pr`` command names, read here, keyed by argv index.

    ``-`` reads this command's stdin. A path must resolve inside the workspace
    root: the gh proxy posts what it reads to GitHub, so a file elsewhere on
    this machine (a key, a credential) is never read for it.
    """
    if not argv or argv[0] != "pr":
        return {}
    files: Dict[str, str] = {}
    root = os.environ.get(ENV_ROOT, "")
    index = 1
    while index < len(argv):
        token = argv[index]
        at = index + 1
        value: Optional[str] = None
        if token in ("-F", "--body-file"):
            value = argv[at] if at < len(argv) else None
            index += 2
        elif token.startswith(("--body-file=", "-F=")):
            value, at = token.partition("=")[2], index
            index += 1
        else:
            # Another flag's value is that flag's, never a body file.
            index += 2 if token in PR_VALUE_FLAGS else 1
            continue
        if value is None:
            continue
        if value == "-":
            data = sys.stdin.buffer.read(MAX_BODY_FILE_BYTES + 1)
        else:
            if not root or not _within(value, root):
                raise SystemExit(
                    _refusal(f"--body-file {value} is outside the workspace ({root or 'unknown'})")
                )
            try:
                with open(value, "rb") as handle:
                    data = handle.read(MAX_BODY_FILE_BYTES + 1)
            except OSError as exc:
                raise SystemExit(_refusal(f"cannot read --body-file {value}: {exc.strerror}"))
        if len(data) > MAX_BODY_FILE_BYTES:
            raise SystemExit(_refusal(f"--body-file {value} is over {MAX_BODY_FILE_BYTES} bytes"))
        files[str(at)] = data.decode("utf-8", "replace")
    return files


def _identity(login: str, repositories: List[str]) -> str:
    who = f"@{login}" if login else "the gateway owner's GitHub login"
    scope = ", ".join(repositories) if repositories else "this session's repository"
    return f"runs on the gateway machine as {who}, scoped to {scope}"


def _refusal(reason: str, login: str = "", repositories: Optional[List[str]] = None) -> int:
    say(f"refused: {reason}")
    say(f"{NAME} {_identity(login, repositories or [])}; run gh help for what it allows")
    return 1


def _endpoint() -> Tuple[int, str]:
    secret = os.environ.get(ENV_SECRET, "")
    try:
        with open(os.environ.get(ENV_PORT_FILE, ""), encoding="ascii") as handle:
            port = int(handle.read().strip())
    except (OSError, ValueError):
        port = 0
    return port, secret


def ask(request: dict) -> dict:
    """POST one request to the git proxy and return the gateway's JSON answer."""
    port, secret = _endpoint()
    if not port or not secret:
        raise ConnectionError("the session's git proxy is not running")
    body = json.dumps(request).encode("utf-8")
    # http.client, not urllib: no HTTP proxy the workspace sets may see the capability.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=ANSWER_TIMEOUT_SECS)
    try:
        conn.request(
            "POST",
            PROXY_PATH,
            body=body,
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        data = response.read()
    except OSError as exc:
        raise ConnectionError(str(exc)) from None
    finally:
        conn.close()
    if response.status != 200:
        raise ConnectionError(data.decode("utf-8", "replace").strip()[:300] or str(response.status))
    try:
        answer = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ConnectionError("the gateway's answer could not be read") from None
    if not isinstance(answer, dict):
        raise ConnectionError("the gateway's answer could not be read")
    return answer


def _about() -> dict:
    try:
        return ask({"op": "about"})
    except ConnectionError as exc:
        return {"login": "", "repositories": [], "refused": f"not connected to the gateway: {exc}"}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: object) -> List[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def show_about(*, help_text: bool) -> int:
    about = _about()
    login = _text(about.get("login"))
    repositories = _strings(about.get("repositories"))
    print(f"{NAME}: Kiro Crew's gh proxy, the git proxy's sibling. This is not GitHub's gh.")
    print(f"It {_identity(login, repositories)}.")
    if isinstance(about.get("refused"), str):
        print(f"Right now it refuses every command: {about['refused']}.")
    if help_text:
        print(f"Allowed: {ALLOWED}.")
        print(f"Refused: {REFUSED}.")
        print("Pull requests and comments it writes say they came from a remote session")
        print("through Kiro Crew's gh proxy. The gateway's token never reaches this machine.")
    return 0


def note(login: str, repo: str, argv: List[str]) -> None:
    """Record, for the tool result, that the gateway ran this command."""
    path = os.environ.get(ENV_NOTE, "")
    if not path:
        return
    command = " ".join(argv[:2]) if argv and argv[0] != "api" else "api"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{login}\t{repo}\tgh {command}\n")
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("help", "--help", "-h") or "--help" in args:
        return show_about(help_text=True)
    if args[0] in ("--version", "version"):
        return show_about(help_text=False)
    if args[0] == "auth":
        shows_token = any(a in ("-t", "--show-token") for a in args)
        if args[1:2] == ["status"] and not shows_token:
            about = _about()
            show_about(help_text=False)
            return 0 if about.get("login") else 1
        return _refusal("the gateway's GitHub login never leaves the gateway machine")
    try:
        files = body_files(args)
    except SystemExit as stop:
        return int(stop.code or 1)
    request = {
        "op": "run",
        "argv": args,
        "workspace_repo": workspace_repo(),
        "branch": current_branch(),
        "files": files,
    }
    try:
        answer = ask(request)
    except ConnectionError as exc:
        return _refusal(f"not connected to the gateway: {exc}")
    login = _text(answer.get("login"))
    if isinstance(answer.get("refused"), str):
        return _refusal(answer["refused"], login, _strings(answer.get("repositories")))
    out = base64.b64decode(str(answer.get("stdout") or ""))
    err = base64.b64decode(str(answer.get("stderr") or ""))
    repo = _text(answer.get("repo"))
    if answer.get("ran") is True:
        note(login, repo, args)
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    sys.stderr.buffer.write(err)
    say(f"ran on the gateway machine as @{login} for {repo}")
    code = answer.get("code")
    return code if isinstance(code, int) and 0 <= code < 256 else 1


if __name__ == "__main__":
    sys.exit(main())
