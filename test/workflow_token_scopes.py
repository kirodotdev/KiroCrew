"""Derive the token scopes a workflow's own calls need, and read what it grants.

A workflow that writes an explicit ``permissions:`` block gets ONLY those scopes:
every unlisted scope becomes ``none``. A call made with the workflow token that
needs a scope the block does not list is therefore a 403 at run time, and a
handler that retries or fails closed on a read turns that into a quiet outage.
This module reads the calls a step makes (``gh api`` paths and methods, ``gh``
subcommands, ``github.rest.*`` in ``actions/github-script``) and returns the
scopes they need, so a test can compare them with the grant instead of a
hand-kept list that drifts.

Heuristic by design: it recognises the call shapes this repository's workflows
use. A call shape it does not recognise is reported as ``unknown``, never
silently passed, so a new shape fails the sweep until it is taught here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

READ, WRITE = "read", "write"
_RANK = {"none": 0, READ: 1, WRITE: 2}

# Scopes whose grant is implied for every token and cannot be listed.
_IMPLICIT = {"metadata"}

_HTTP_WRITE = {"POST", "PUT", "PATCH", "DELETE"}

# repos/<repo>/<first segment>[/<id>/<second segment>] -> candidate scopes. Each
# entry is a tuple of alternatives: holding ANY ONE of them at the needed level is
# enough (GitHub lets a pull-request comment be written with either issues or
# pull-requests write).
_ISSUE_OR_PR = ("issues", "pull-requests")


def _api_scopes(path: str, method: str) -> list[tuple[str, ...]] | None:
    """Alternatives (each a tuple of scopes, any one sufficient) for one REST call."""
    segs = [s for s in path.split("?")[0].strip("/").split("/") if s]
    if segs and segs[0] in {
        "rate_limit",
        "user",
        "users",
        "orgs",
        "meta",
        "rate-limit",
    }:
        return [()]
    # repos/<owner>/<repo>/... : the owner/repo are usually one `$REPO` word.
    if segs and segs[0] == "repos":
        segs = segs[1:]
        if segs and "$" in segs[0] and "/" not in segs[0]:
            segs = segs[1:]
        elif len(segs) >= 2:
            segs = segs[2:]
    if not segs:
        return [()]  # the repository object itself: metadata
    head = segs[0]
    tail = segs[1:]
    if head == "issues":
        # issues/<n> is an issue read; a comment or label call on it may be a
        # pull request's, which either issues or pull-requests covers.
        if tail and (tail[0] == "comments" or "comments" in tail[1:] or "labels" in tail[1:]):
            return [_ISSUE_OR_PR]
        return [("issues",)]
    if head == "pulls":
        return [("pull-requests",)]
    if head in {"rules", "rulesets"}:
        return [()]
    if head == "collaborators":
        return [()]  # permission lookups ride on metadata
    if head == "actions":
        return [("actions",)]
    if head == "contents":
        return [("contents",)]
    if head == "git":
        return [("contents",)]
    if head == "commits":
        if "check-runs" in tail or "check-suites" in tail:
            return [("checks",)]
        if "status" in tail or "statuses" in tail:
            return [("statuses",)]
        return [("contents",)]
    if head == "check-runs" or head == "check-suites":
        return [("checks",)]
    if head == "statuses":
        return [("statuses",)]
    if head == "labels":
        return [_ISSUE_OR_PR]
    if head in {"branches", "compare", "releases", "tags", "tarball", "zipball"}:
        return [("contents",)]
    if head == "deployments":
        return [("deployments",)]
    if head == "dispatches":
        return [("contents",)]
    if head == "dependency-graph":
        # compare and sbom: GitHub documents both under the contents scope.
        return [("contents",)]
    return None


def _level(method: str) -> str:
    return WRITE if method.upper() in _HTTP_WRITE else READ


# gh subcommand -> (scope alternatives, level)
_GH_SUB = {
    ("pr", "view"): (("pull-requests",), READ),
    ("pr", "list"): (("pull-requests",), READ),
    ("pr", "diff"): (("pull-requests",), READ),
    ("pr", "checks"): (("pull-requests",), READ),
    ("pr", "status"): (("pull-requests",), READ),
    ("pr", "comment"): (_ISSUE_OR_PR, WRITE),
    ("pr", "edit"): (("pull-requests",), WRITE),
    ("pr", "review"): (("pull-requests",), WRITE),
    ("pr", "merge"): (("pull-requests", "contents"), WRITE),
    ("pr", "close"): (("pull-requests",), WRITE),
    ("pr", "ready"): (("pull-requests",), WRITE),
    ("pr", "create"): (("pull-requests",), WRITE),
    ("issue", "view"): (("issues",), READ),
    ("issue", "list"): (("issues",), READ),
    ("issue", "comment"): (_ISSUE_OR_PR, WRITE),
    ("issue", "edit"): (("issues",), WRITE),
    ("issue", "close"): (("issues",), WRITE),
    ("issue", "create"): (("issues",), WRITE),
    ("run", "view"): (("actions",), READ),
    ("run", "list"): (("actions",), READ),
    ("run", "download"): (("actions",), READ),
    ("run", "watch"): (("actions",), READ),
    ("run", "rerun"): (("actions",), WRITE),
    ("run", "cancel"): (("actions",), WRITE),
    ("workflow", "run"): (("actions",), WRITE),
    ("workflow", "list"): (("actions",), READ),
    ("workflow", "view"): (("actions",), READ),
    ("release", "view"): (("contents",), READ),
    ("release", "list"): (("contents",), READ),
    ("release", "download"): (("contents",), READ),
    ("release", "create"): (("contents",), WRITE),
    ("release", "upload"): (("contents",), WRITE),
    ("label", "create"): (_ISSUE_OR_PR, WRITE),
    ("label", "list"): (_ISSUE_OR_PR, READ),
    ("repo", "view"): ((), READ),
    ("repo", "clone"): (("contents",), READ),
    ("auth", "status"): ((), READ),
    ("auth", "setup-git"): ((), READ),
    ("auth", "login"): ((), READ),
    ("auth", "token"): ((), READ),
    ("cache", "delete"): (("actions",), WRITE),
    ("cache", "list"): (("actions",), READ),
    ("search", "issues"): ((), READ),
    ("search", "prs"): ((), READ),
    ("api", "graphql"): None,  # handled separately
}

# github.rest.<ns>.<method> -> scope by namespace; level by method prefix.
_REST_NS = {
    "issues": _ISSUE_OR_PR,
    "pulls": ("pull-requests",),
    "repos": ("contents",),
    "actions": ("actions",),
    "checks": ("checks",),
    "git": ("contents",),
    "reactions": _ISSUE_OR_PR,
    "search": (),
    "users": (),
    "orgs": (),
    "teams": (),
}
_REST_READ = ("get", "list", "check", "compare", "download", "search")


@dataclass(frozen=True)
class Need:
    """One call: any ONE of ``scopes`` held at ``level`` satisfies it."""

    scopes: tuple[str, ...]
    level: str
    call: str
    where: str  # "<workflow>:<job>:<step>"


@dataclass
class StepScan:
    needs: list[Need] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def effective_permissions(workflow: dict, job: dict) -> dict[str, str] | None:
    """The scope map a job's token holds, or None when no block is written.

    A job-level ``permissions:`` replaces the workflow-level one entirely. With no
    block at either level the token takes the repository default, which this
    module cannot know, so None means "not governed by a written grant".
    """
    block = job.get("permissions", workflow.get("permissions"))
    if block is None:
        return None
    if isinstance(block, str):
        level = {"read-all": READ, "write-all": WRITE}.get(block)
        if level is None:
            return {}
        return {
            scope: level
            for scope in (
                "actions",
                "checks",
                "contents",
                "deployments",
                "issues",
                "packages",
                "pull-requests",
                "statuses",
                "security-events",
                "id-token",
                "discussions",
            )
        }
    return {str(k): str(v) for k, v in (block or {}).items()}


def grants(permissions: dict[str, str], need: Need) -> bool:
    """Whether the written scope map satisfies one call."""
    if not need.scopes:
        return True
    want = _RANK[need.level]
    return any(_RANK.get(permissions.get(scope, "none"), 0) >= want for scope in need.scopes)


def _uses_workflow_token(*envs: dict | None) -> bool:
    """Whether the step's GH_TOKEN/GITHUB_TOKEN is the workflow's own token.

    The innermost env that sets the variable wins. A token minted elsewhere (an
    app token, a PAT secret) is not governed by ``permissions:``.
    """
    value = None
    for env in envs:  # outermost -> innermost
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            if env and name in env:
                value = str(env[name])
    if value is None:
        return True  # `gh` with no token configured has none; github-script defaults to it
    return bool(re.search(r"secrets\.GITHUB_TOKEN|github\.token", value))


def _logical_lines(script: str) -> list[str]:
    """Join backslash continuations and drop whole-line comments."""
    joined = re.sub(r"\\\n\s*", " ", script)
    out = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


_GH_CALL = re.compile(r"(?P<lead>^|[\s;&|(`]|\$\()gh\s+(?P<rest>[^\n]*)")
_FUNC_DEF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{")
_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_CUT = re.compile(r"\s(?:\|\||\||&&|;)(?:\s|$)")
_FLAG_WITH_VALUE = {
    "--method",
    "-X",
    "-f",
    "-F",
    "--field",
    "--raw-field",
    "--input",
    "-H",
    "--header",
    "--jq",
    "-q",
    "--template",
    "-t",
    "--cache",
    "--hostname",
}


def _scan_gh_line(line: str, where: str, scan: StepScan) -> None:
    for m in _GH_CALL.finditer(line):
        # A `gh` inside a quoted message ("... gh api ...") is prose, not a call.
        prefix = line[: m.end("lead")]
        before = prefix.rstrip()
        opens_command = not before or before.endswith(
            ("$(", "&&", "||", "|", ";", "then", "do", "else", "{", "!")
        )
        if not opens_command:
            # Mid-line `gh` is prose when it sits in an open quote or an echo.
            if prefix.count('"') % 2 == 1 or prefix.count("'") % 2 == 1:
                continue
            if re.match(r"(?:echo|printf)\b", line):
                continue
        rest = _CUT.split(m.group("rest"), maxsplit=1)[0].strip()
        words = [w.strip("\"'") for w in _TOKEN.findall(rest)]
        if not words:
            continue
        sub = (words[0], words[1] if len(words) > 1 else "")
        if words[0] == "api":
            _scan_gh_api(_TOKEN.findall(rest)[1:], where, scan, rest)
            continue
        if words[0] == "attestation":
            continue  # verifies a signature against the public transparency log
        entry = _GH_SUB.get(sub)
        if sub in _GH_SUB and entry is not None:
            scopes, level = entry
            scan.needs.append(Need(scopes, level, f"gh {sub[0]} {sub[1]}", where))
        elif sub == ("release", "edit"):
            scan.needs.append(Need(("contents",), WRITE, "gh release edit", where))
        elif words[0] in {"--version", "version", "help", "extension", "config"}:
            continue
        else:
            scan.unknown.append(f"gh {' '.join(words[:3])}")


# A step-local shell function that wraps `gh api "$1"` is a GET on its first argument.
_GET_WRAPPERS = ("api_read",)


def _scan_wrapper_call(line: str, where: str, scan: StepScan) -> None:
    for name in _GET_WRAPPERS:
        m = re.search(rf'(?<![\w-]){name}\s+("[^"]*"|\'[^\']*\'|\S+)', line)
        if m and not line.lstrip().startswith(f"{name}()") and "local path" not in line:
            _scan_gh_api(["--method", "GET", m.group(1)], where, scan, line)


def _scan_gh_api(args: list[str], where: str, scan: StepScan, raw: str) -> None:
    method = None
    path = None
    has_body = False
    skip = False
    for i, word in enumerate(args):
        if skip:
            skip = False
            continue
        bare = word.strip("\"'")
        if bare in {"--method", "-X"}:
            if i + 1 < len(args):
                method = args[i + 1].strip("\"'").upper()
            skip = True
        elif bare.startswith("--method="):
            method = bare.split("=", 1)[1].upper()
        elif bare in {"-f", "-F", "--field", "--raw-field", "--input"}:
            has_body = True
            skip = True
        elif bare in _FLAG_WITH_VALUE:
            skip = True
        elif bare.startswith("-"):
            continue
        elif path is None:
            path = bare
    if path is None:
        scan.unknown.append(f"gh api {raw[:60]}")
        return
    if path in {"$path", "$1"} and not has_body:
        return  # the inside of a wrapper; its call sites are scanned instead
    if method is None:
        method = "POST" if has_body else "GET"
    if path == "graphql":
        scan.unknown.append("gh api graphql")
        return
    alternatives = _api_scopes(path, method)
    if alternatives is None:
        scan.unknown.append(f"gh api {method} {path}")
        return
    for scopes in alternatives:
        scan.needs.append(Need(scopes, _level(method), f"gh api {method} {path}", where))


def _scan_github_script(script: str, where: str, scan: StepScan) -> None:
    for m in re.finditer(r"github\.rest\.(\w+)\.(\w+)", script):
        ns, name = m.group(1), m.group(2)
        scopes = _REST_NS.get(ns)
        if scopes is None:
            scan.unknown.append(f"github.rest.{ns}.{name}")
            continue
        level = READ if name.startswith(_REST_READ) else WRITE
        scan.needs.append(Need(scopes, level, f"github.rest.{ns}.{name}", where))
    for m in re.finditer(r"github\.(?:request|graphql)\(", script):
        scan.unknown.append(m.group(0))


def scan_step(wf_name: str, job_name: str, step: dict, workflow: dict, job: dict) -> StepScan:
    """The scopes one step's calls need, when it calls with the workflow token."""
    scan = StepScan()
    where = f"{wf_name}:{job_name}:{step.get('name') or step.get('uses') or '?'}"
    if not _uses_workflow_token(workflow.get("env"), job.get("env"), step.get("env")):
        return scan
    uses = str(step.get("uses") or "")
    if uses.startswith("actions/github-script"):
        token = (step.get("with") or {}).get("github-token")
        if token is None or re.search(r"secrets\.GITHUB_TOKEN|github\.token", str(token)):
            _scan_github_script(str((step.get("with") or {}).get("script", "")), where, scan)
        return scan
    script = step.get("run")
    if not isinstance(script, str):
        return scan
    if re.search(r"github\.rest|octokit", script):
        _scan_github_script(script, where, scan)
    for line in _logical_lines(script):
        if _FUNC_DEF.match(line) and "gh " not in line:
            continue
        _scan_gh_line(line, where, scan)
        _scan_wrapper_call(line, where, scan)
    return scan


def load_workflow(path: Path) -> dict:
    # PyYAML reads the bare key `on` as the boolean True; the scan never needs it.
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def job_needs(wf_name: str, workflow: dict, job_name: str) -> StepScan:
    """Every call every step of one job makes with the workflow token."""
    job = workflow["jobs"][job_name]
    total = StepScan()
    for step in job.get("steps") or []:
        one = scan_step(wf_name, job_name, step, workflow, job)
        total.needs.extend(one.needs)
        total.unknown.extend(one.unknown)
    return total


def missing_scopes(wf_name: str, workflow: dict) -> list[Need]:
    """Calls whose scope the written ``permissions:`` grant does not hold."""
    missing: list[Need] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        granted = effective_permissions(workflow, job)
        if granted is None:
            continue
        for need in job_needs(wf_name, workflow, job_name).needs:
            if not grants(granted, need):
                missing.append(need)
    return missing
