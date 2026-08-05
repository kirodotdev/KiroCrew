#!/usr/bin/env python3
"""Source adapter — normalize a GitHub PR link into a single ``ReviewTarget``.

The brain only ever sees a ``ReviewTarget``. Adding a new platform later is a new
adapter, not a brain change. This build ships the **GitHub PR** adapter only.

The network fetch itself is performed by the pipeline via the ``gh`` CLI; this
module is the deterministic, token-free part: parsing the fetched payload into a
``ReviewTarget``.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

GITHUB_PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", re.I)
# A change is a "fix" if its title/description signals a bug/revert/incident.
FIX_RE = re.compile(r"\b(fix(es|ed)?|revert(s|ed)?|bug|hotfix|regression|incident|patch)\b", re.I)
# GitHub-style issue reference (e.g. "#204") linked from the PR body.
GH_ISSUE_RE = re.compile(r"#(\d+)")


class AdapterError(ValueError):
    """Base class for adapter failures (fail-fast)."""


class UnsupportedPlatform(AdapterError):
    """The link's platform is not supported in this build."""


class AdapterParseError(AdapterError):
    """The fetched payload could not be normalized into a ReviewTarget."""


@dataclass
class ReviewTarget:
    """The single normalized shape the review brain consumes."""

    platform: str
    repo_identity: str          # host/org/repo — the learning key
    change_id: str
    url: str
    title: str = ""
    description: str = ""
    linked_issue: str = ""
    author: str = ""
    target_branch: str = ""
    revision: str = ""
    files: list[dict] = field(default_factory=list)        # [{path, diff}]
    existing_comments: list[dict] = field(default_factory=list)
    design_discussion: list[dict] = field(default_factory=list)
    is_fix: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform(link: str) -> str:
    """Return ``github`` for a GitHub PR link, else raise UnsupportedPlatform.

    The host is validated against an allowlist of the PARSED URL hostname (not a
    substring of the raw link), so a URL where ``github.com`` merely appears in
    the path/query/userinfo (e.g. ``https://evil.example/github.com/x/pull/1``)
    is rejected. Aligns with SSRF/allowlist guidance (parse to components,
    default-deny)."""
    if not link or not isinstance(link, str):
        raise UnsupportedPlatform("empty or non-string link")
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    if host in {"github.com", "www.github.com"} and "/pull/" in (parsed.path or ""):
        return "github"
    raise UnsupportedPlatform(f"unsupported link/platform: {link!r} (expected a GitHub PR URL)")


def _sanitize_seg(s: str) -> str:
    """Make an owner/repo segment safe for use in a change-id (which names a
    result record on disk). Non ``[A-Za-z0-9.]`` chars — including ``-`` — become
    ``_``. Excluding ``-`` is deliberate: ``-`` is the segment delimiter in
    ``github_change_id`` (``GH-<owner>-<repo>-<n>``), so keeping it inside a
    segment would make different owner/repo pairs collide (e.g. ``a-b``/``c`` vs
    ``a``/``b-c``). Stripping it to ``_`` keeps ``-`` unambiguous as the delimiter."""
    return re.sub(r"[^A-Za-z0-9.]", "_", str(s or "")).strip("_") or "unknown"


def github_pr_parts(link: str) -> tuple[str, str, str]:
    """Parse ``(owner, repo, number)`` from a GitHub PR URL. Fails fast."""
    m = GITHUB_PR_RE.search(link or "")
    if not m:
        raise AdapterParseError(f"not a GitHub PR link: {link!r}")
    owner, repo, number = m.group(1), m.group(2), m.group(3)
    repo = re.sub(r"\.git$", "", repo)  # tolerate a trailing .git
    return owner, repo, number


def parse_repo_url(link: str) -> tuple[str, str]:
    """Parse ``(owner, repo)`` from a GitHub REPO URL (no ``/pull/``).

    Mirrors ``detect_platform``'s PARSED-hostname allowlist (default-deny,
    SSRF/allowlist guidance) but accepts a bare repo URL like
    ``https://github.com/<owner>/<repo>`` so a batch of that repo's open PRs can
    be enumerated. Raises ``UnsupportedPlatform`` for a non-GitHub host and
    ``AdapterParseError`` when the owner/repo path segments are missing."""
    if not link or not isinstance(link, str):
        raise UnsupportedPlatform("empty or non-string repo link")
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        raise UnsupportedPlatform(
            f"unsupported repo host: {link!r} (expected a github.com repo URL)")
    path = parsed.path or ""
    if "/pull/" in path:
        # A PR URL, not a repo URL — route the user to the paste flow so we don't
        # silently review the PR's whole repo.
        raise AdapterParseError(
            f"that's a PR URL, not a repo URL: {link!r} (paste it in the PR box)")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise AdapterParseError(f"not a GitHub repo link: {link!r}")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    _seg = re.compile(r"^[A-Za-z0-9._-]+$")
    if owner in (".", "..") or repo in (".", "..") or not (_seg.match(owner) and _seg.match(repo)):
        raise AdapterParseError(f"invalid owner/repo in {link!r}")
    return owner, repo


def github_change_id(owner: str, repo: str, number: str | int) -> str:
    """Filesystem-safe, platform-namespaced change id: ``GH-<owner>-<repo>-<n>``.
    Unlike a raw URL, this is a valid filename."""
    return f"GH-{_sanitize_seg(owner)}-{_sanitize_seg(repo)}-{number}"


def github_review_key(owner: str, repo: str, number: str | int) -> str:
    """Collision-free canonical identity for the durable reviewed-index key.

    Distinct from ``github_change_id``: that value ALSO names an on-disk result
    file, so it runs owner/repo through ``_sanitize_seg`` — which collapses ``-``
    to ``_`` to keep ``-`` unambiguous as its segment delimiter. That sanitization
    is lossy: ``acme/service-api`` and ``acme/service_api`` both become
    ``GH-acme-service_api-<n>``, so two DIFFERENT repos with the same PR number
    shared one ``reviewed.json`` key and clobbered each other's dedup record —
    silently skipping a requested review when their PR heads happened to share a
    commit SHA (as mirrored repos can).

    This key never names a file, so it keeps owner/repo verbatim and joins with
    ``/`` (a character GitHub owner/repo can never contain), giving a lossless,
    unambiguous identity. Owner/repo are lower-cased because GitHub treats them
    case-insensitively for identity."""
    return f"github.com/{str(owner).lower()}/{str(repo).lower()}#{number}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _author_alias(raw: dict) -> str:
    a = _first(raw, "author", "authorAlias", "owner", default="")
    if isinstance(a, dict):
        return _first(a, "alias", "login", "name", default="")
    return str(a) if a else ""


def detect_is_fix(title: str, description: str) -> bool:
    return bool(FIX_RE.search(f"{title}\n{description}"))


def extract_linked_issue(text: str) -> str:
    """Extract a linked GitHub issue reference (``#123``) from the PR body."""
    m = GH_ISSUE_RE.search(text or "")
    return f"#{m.group(1)}" if m else ""


# ---------------------------------------------------------------------------
# GitHub adapter
# ---------------------------------------------------------------------------

def parse_github_payload(raw: dict | str, *, link: str | None = None) -> ReviewTarget:
    """Normalize a GitHub PR payload into a ReviewTarget. The worker assembles
    this payload from ``gh api``: the ``pulls/{n}`` object merged with a ``files``
    array (each carrying its per-file ``patch``) and optional ``comments``. Tolerant
    of field-name variants (``filename``/``path``, ``patch``/``diff``); fails fast
    when there is no usable content. ``owner``/``repo``/``number`` are taken from
    the payload (``base.repo.full_name`` + ``number``) and fall back to the link/
    ``html_url`` so the adapter works whether or not the caller echoes the URL."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterParseError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdapterParseError("payload must be a JSON object")

    _base = raw.get("base")
    base: dict = _base if isinstance(_base, dict) else {}
    _head = raw.get("head")
    head: dict = _head if isinstance(_head, dict) else {}
    _base_repo = base.get("repo")
    base_repo: dict = _base_repo if isinstance(_base_repo, dict) else {}

    # NOTE: do NOT fall back to raw["id"] — on GitHub that is the internal
    # database id (e.g. 1847293847), NOT the PR number in the URL. Using it would
    # produce a change_id that mismatches _cid()'s URL-derived id (write/read would
    # hit different result files). The link/html_url fallback below supplies the
    # number when the payload omits it.
    number = _first(raw, "number", default="")
    owner = repo = ""
    full = _first(base_repo, "full_name", default="")
    if full and "/" in full:
        owner, repo = full.split("/", 1)

    # Fill any missing part from the link, then from html_url.
    html_url = _first(raw, "html_url", "url", default="")
    for candidate in (link, html_url):
        if owner and repo and number:
            break
        if not candidate:
            continue
        try:
            lo, lr, ln = github_pr_parts(candidate)
        except AdapterParseError:
            continue
        owner = owner or lo
        repo = repo or lr
        number = number or ln

    if not (owner and repo and number):
        raise AdapterParseError(
            "could not determine GitHub owner/repo/number from payload or link")

    description = _first(raw, "body", "description", default="")
    title = _first(raw, "title", default="") or (description.splitlines()[0] if description else "")

    raw_files = raw.get("files") or raw.get("diffs") or []
    files: list[dict] = []
    for d in raw_files:
        if not isinstance(d, dict):
            continue
        path = _first(d, "filename", "path", "name", default="")
        diff = _first(d, "patch", "diff", "unifiedDiff", default="")
        if path:
            files.append({"path": path, "diff": diff})

    # Fail fast: a PR with neither files nor a description is unusable.
    if not files and not description:
        raise AdapterParseError("payload has no files and no description")

    # GitHub author lives under user.login (fall back to the generic extractor).
    author = ""
    user = raw.get("user")
    if isinstance(user, dict):
        author = _first(user, "login", "name", default="")
    if not author:
        author = _author_alias(raw)

    revision = (_first(head, "sha", default="")
                or _first(raw, "head_sha", "sha", "revision", default=""))
    target_branch = (_first(base, "ref", default="")
                     or _first(raw, "base_ref", "targetBranch", default=""))

    comments = raw.get("comments") or raw.get("review_comments") or raw.get("allComments") or []
    if not isinstance(comments, list):
        comments = []

    return ReviewTarget(
        platform="github",
        repo_identity=f"github.com/{owner}/{repo}",
        change_id=github_change_id(owner, repo, number),
        url=html_url or f"https://github.com/{owner}/{repo}/pull/{number}",
        title=title,
        description=description,
        linked_issue=extract_linked_issue(description),
        author=str(author) if author else "",
        target_branch=target_branch,
        revision=str(revision),
        files=files,
        existing_comments=comments,
        design_discussion=[],
        is_fix=detect_is_fix(title, description),
    )


def normalize(link: str, raw_payload: dict | str) -> ReviewTarget:
    """Top-level entry: detect platform, then parse. Fails fast on unsupported."""
    platform = detect_platform(link)
    if platform == "github":
        return parse_github_payload(raw_payload, link=link)
    raise UnsupportedPlatform(f"unsupported platform: {platform!r}")


def validate_review_target(target: ReviewTarget) -> list[str]:
    """Non-fatal warnings about a normalized target — surfaces likely GitHub
    payload-mapping gaps before review. Empty == looks complete."""
    warns: list[str] = []
    if not target.files:
        warns.append("no files/diffs parsed — check the payload's `files` mapping")
    else:
        if not any(f.get("diff") for f in target.files):
            warns.append("files present but all diffs are empty — check the `patch` field name")
    if not target.title and not target.description:
        warns.append("no title or description — check the payload field names")
    if not target.target_branch:
        warns.append("no target branch parsed (branch-gate checks will be skipped)")
    if target.repo_identity.endswith("/unknown"):
        warns.append("repo could not be determined — learning key will be 'unknown'")
    return warns
