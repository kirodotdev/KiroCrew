"""``kirocrew gh-comment`` -- write a GitHub issue comment that cannot start the Kiro Agent app.

An agent that comments on a GitHub issue runs this in its shell in place of
``gh issue comment`` or a raw ``gh api`` call on ``.../comments``. Credential and
exfiltration-URL redaction run first, then
:func:`kiro_crew.github_comment_safety.assert_safe`: a body whose raw markdown
holds ``/kiro`` in any case is REFUSED with exit 3 and nothing is sent, and the
refusal lists each place with the replacement to write there. The command rewrites
no word of the body -- a safe body is sent byte-for-byte as the redaction passes
left it, with the agent's own ``gh`` as JSON on stdin, never on argv.

It deliberately runs the ``gh`` on the caller's ``PATH`` with the caller's
environment: it is a safer spelling of a call the agent could already make in the
same shell, so it adds no reach the agent did not have, and it uses no credential
of the gateway's.

The body comes from ``--body-file``, which is read through
:func:`kiro_crew.hooks.safe_read_file`, so a sensitive path (a credential store, an
SSH key) is refused before anything is read and before any ``gh`` call. A file is
the only source: a body is markdown an agent composes over several lines, and
writing it to a file keeps it off argv and out of the shell's quoting.

Exit codes: ``0`` sent; ``1`` the ``gh`` call failed, or the body is empty,
unreadable or at a refused path; ``2`` usage; ``3`` refused -- the body would start
the app and nothing was sent.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from kiro_crew import cli_help, github_comment_safety
from kiro_crew.github_runner import GITHUB_OWNER_SEGMENT_RE, GITHUB_REPO_SEGMENT_RE
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 3

#: Bound on one ``gh api`` call. A comment write is a single small request.
GH_TIMEOUT_SEC = 60.0

_EPILOG = """\
examples:
  kirocrew gh-comment post --repo OWNER/REPO --number 42 --body-file body.md
  kirocrew gh-comment edit --repo OWNER/REPO --comment-id 123456 --body-file body.md

post and edit print {"id", "url"} of the comment. Exit codes: 0 sent; 1 the gh
call failed, or the body is unreadable; 2 usage; 3 refused, nothing sent."""


def _repo_arg(value: str) -> str:
    owner, sep, name = value.partition("/")
    if (
        not sep
        or owner in (".", "..")
        or name in (".", "..")
        or not GITHUB_OWNER_SEGMENT_RE.match(owner)
        or not GITHUB_REPO_SEGMENT_RE.match(name)
    ):
        raise argparse.ArgumentTypeError(f"expected OWNER/REPO, got {value!r}")
    return value


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the ``post`` / ``edit`` verbs to *parser*."""
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = _EPILOG
    verbs = parser.add_subparsers(dest="gh_comment_action", metavar="<verb>")
    verbs.required = True

    def body_arg(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--body-file",
            required=True,
            metavar="FILE",
            help="Read the body from FILE (UTF-8)",
        )

    post = verbs.add_parser("post", help="Post a new comment on an issue or pull request")
    post.add_argument("--repo", required=True, type=_repo_arg, metavar="OWNER/REPO")
    post.add_argument("--number", required=True, type=_positive_int, metavar="N")
    body_arg(post)

    edit = verbs.add_parser("edit", help="Replace the body of an existing comment")
    edit.add_argument("--repo", required=True, type=_repo_arg, metavar="OWNER/REPO")
    edit.add_argument("--comment-id", required=True, type=_positive_int, metavar="ID")
    body_arg(edit)


def register_gh_comment_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Wire ``kirocrew gh-comment`` into the top-level parser."""
    configure_parser(cli_help.add_command(sub, "gh-comment"))


def _read_body(args: argparse.Namespace) -> str:
    """The body, read from ``--body-file`` through the sensitive-path guard.

    ``safe_read_file`` canonicalizes the path, refuses a sensitive target through a
    symlink, and raises :class:`PermissionError` when it does -- which reaches the
    caller as an ``OSError`` and so as the same read failure a missing file gives,
    before any ``gh`` call.

    ``hooks`` is imported here rather than at module scope: ``cli.py`` loads this
    module on every ``kirocrew`` invocation and every MCP stdio server, and only a
    call that reads a body needs the guard (``test/test_cli_lazy_imports.py`` holds
    that line).
    """
    from kiro_crew import hooks

    return hooks.safe_read_file(args.body_file)


def _send(path: str, method: str, body: str) -> dict:
    """Send *body* with ``gh api``, as JSON on stdin, and return ``{id, url}``."""
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh is not on PATH")
    proc = subprocess.run(
        [gh, "api", path, "--method", method, "--input", "-"],
        input=json.dumps({"body": body}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GH_TIMEOUT_SEC,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"gh api {method} {path} failed (exit {proc.returncode}): {tail}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"id": data.get("id"), "url": data.get("html_url")}


def gh_comment_cmd(args: argparse.Namespace) -> int:
    """Run one ``kirocrew gh-comment`` verb and return its exit code."""
    action = args.gh_comment_action
    try:
        body = _read_body(args)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: cannot read the body: {exc}", file=sys.stderr)
        return EXIT_FAILED

    repo = args.repo
    body, _ = redact_exfiltration_urls(body)
    body, _ = redact_credentials(body)
    try:
        github_comment_safety.assert_safe(body, repo)
    except github_comment_safety.UnsafeCommentError as exc:
        print(f"REFUSED, nothing sent: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if not body.strip():
        print("ERROR: a comment needs a body", file=sys.stderr)
        return EXIT_FAILED
    if action == "post":
        path, method = f"repos/{repo}/issues/{args.number}/comments", "POST"
    else:
        path, method = f"repos/{repo}/issues/comments/{args.comment_id}", "PATCH"
    try:
        result = _send(path, method, body)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(result))
    return EXIT_OK
