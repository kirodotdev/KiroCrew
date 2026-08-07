#!/usr/bin/env python3
"""Review pipeline — deterministic orchestration helpers (design §4).

The token-free backbone the orchestrating session calls between LLM judgment
steps: parse GitHub PR links, resolve a per-repo rule pack, prepare a
ReviewTarget (+ blast radius) for the gate, and build draft-only comment
payloads. The per-change Phase 1/2 judgment runs in a spawned clean session
(one per change) using the code-review-sage ruleset.

CLI subcommands let the agent invoke each step:
    python3 sage_lib/pipeline.py batch "<pasted PR links>"
    python3 sage_lib/pipeline.py rule-pack <repo_identity>
    python3 sage_lib/pipeline.py prepare --link <link> --payload-file <json>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Optional KiroCrew redaction — used to scrub LLM-generated text before it is
# posted to an external surface. Imported at module top (absent only when run
# fully standalone outside the KiroCrew runtime).
try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls
except ImportError:  # pragma: no cover - standalone fallback
    redact_credentials = redact_exfiltration_urls = None  # type: ignore

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/pipeline.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import adapters, blast_radius, discovery, results, store  # noqa: E402

# Identifies a pending review as OURS. The poster matches on it to delete only its
# own stale draft (never a human's in-progress one), and the driver matches on it to
# confirm a delivery it is about to record. One definition so those two readers and
# the writers below cannot drift apart.
DRAFT_MARKER = "[code-review-sage]"


def _redact(text: str) -> str:
    """Scrub credentials + exfiltration URLs before text leaves for an external
    surface. Delegates to `store`, which owns the redactor so readers outside the
    posting path can apply the same scrub; kept under this name because tests and
    other modules patch `pipeline._redact` to observe egress."""
    return store.redact_text(text)


# ---------------------------------------------------------------------------
# Entry point (a): single link  — just normalize(); handled by the adapter.
# Entry point (b): batch paste
# ---------------------------------------------------------------------------

def parse_batch(text: str) -> list[str]:
    """Split a pasted blob (newline/comma separated) into a de-duplicated,
    order-preserving list of GitHub PR URLs. Non-PR tokens are dropped."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tok in re.split(r"[\n,]+", text):
        tok = tok.strip()
        if not tok:
            continue
        try:
            owner, repo, number = adapters.github_pr_parts(tok)
        except adapters.AdapterParseError:
            continue
        link = f"https://github.com/{owner}/{repo}/pull/{number}"
        key = link.lower()
        if key not in seen:
            seen.add(key)
            out.append(link)
    return out


def list_open_prs(owner: str, repo: str, *, timeout: float = 60.0) -> list[dict]:
    """Enumerate a repo's OPEN pull requests via the authenticated ``gh`` CLI.

    Deterministic backbone (no LLM): runs ``gh api`` with a LIST argv (never
    ``shell=True``). ``owner``/``repo`` are constrained to ``[^/]+`` by
    ``adapters.parse_repo_url`` before this is called and are interpolated only
    into the ``gh api`` PATH argument (which `gh` treats as an API path, not a
    shell command), so there is no shell-injection surface. Returns
    ``[{url, number, head_sha, title, author, updated_at, draft}]`` in GitHub's
    order. Raises ``RuntimeError`` (with the stderr tail) if `gh` is missing,
    unauthenticated, times out, or the repo can't be read.

    The ``gh`` binary is resolved through ``discovery.gh_bin()`` — the same
    validated resolution the dashboard's PR panel uses — rather than trusting a
    bare ``gh`` off ``PATH``."""
    path = f"repos/{owner}/{repo}/pulls?state=open&per_page=100"
    try:
        gh = discovery.gh_bin()
    except discovery.GhError as e:
        raise RuntimeError(str(e)) from e
    argv = [
        gh, "api", path, "--paginate",
        "--jq", ".[] | {url: .html_url, number: .number, "
                "head_sha: .head.sha, title: .title, author: .user.login, "
                "updated_at: .updated_at, draft: .draft}",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise RuntimeError("the `gh` CLI is not installed on this host") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"`gh` timed out listing open PRs for {owner}/{repo}") from e
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(
            f"gh api failed for {owner}/{repo} (exit {proc.returncode}): {tail}")
    prs: list[dict] = []
    for line in (proc.stdout or "").splitlines():   # --jq emits JSONL
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        prs.append({
            "url": obj.get("url") or "",
            "number": obj.get("number"),
            "head_sha": obj.get("head_sha") or "",
            "title": obj.get("title") or "",
            "author": obj.get("author") or "",
            "updated_at": obj.get("updated_at") or "",
            "draft": bool(obj.get("draft")),
        })
    # Non-silent: gh returned 0 but produced non-empty, unparseable output (e.g. a
    # gh build that pretty-prints jq). Don't masquerade that as "no open PRs".
    if not prs and (proc.stdout or "").strip():
        raise RuntimeError(
            f"could not parse `gh` output for {owner}/{repo} "
            "(expected one JSON object per line)")
    return prs


# ---------------------------------------------------------------------------
# Per-repo rule pack resolution (design §4.3 — read-only reuse, the ONLY merge)
# ---------------------------------------------------------------------------

def resolve_rule_pack(pack_name: str) -> str | None:
    """Find a rule-pack SKILL.md by skill name under the KiroCrew skills dir
    (``<config_dir>/skills/`` — ``~/.kiro/crew/skills/`` by default, honoring
    ``KIROCREW_HOME`` — this is where the app framework symlinks app + user
    skills). Prefers the most recently modified copy. Returns an absolute path
    or None."""
    if not pack_name or "/" in pack_name or ".." in pack_name:
        return None
    skills = store.crew_home() / "skills"
    if not skills.exists():
        return None
    candidates = list(skills.glob(f"{pack_name}/SKILL.md"))          # flat link
    candidates += list(skills.glob(f"*/{pack_name}/SKILL.md"))       # namespaced link
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0])


def rule_pack_for_repo(repo_identity: str, config: dict | None = None) -> str | None:
    """Map a repo to its pack via config, then resolve to a file. None if no pack."""
    cfg = config or store.load_config()
    pack_name = (cfg.get("rule_packs") or {}).get(repo_identity)
    if not pack_name:
        return None
    return resolve_rule_pack(pack_name)


# ---------------------------------------------------------------------------
# Prepare a target for the Phase 1 gate (normalize + blast radius)
# ---------------------------------------------------------------------------

def prepare_target(link: str, raw_payload: dict | str, config: dict | None = None) -> dict:
    """Normalize a link+payload into a ReviewTarget and attach blast-radius
    signals + the resolved rule-pack path. This is the gate's input bundle."""
    cfg = config or store.load_config()
    target = adapters.normalize(link, raw_payload)
    radius = blast_radius.analyze(target.files, cfg.get("sensitive_globs", []))
    return {
        "target": target.to_dict(),
        "blast_radius": radius,
        "rule_pack": rule_pack_for_repo(target.repo_identity, cfg),
        "warnings": adapters.validate_review_target(target),
    }


# ---------------------------------------------------------------------------
# Draft-only comment posting — platform-keyed (hard rule: publish is ALWAYS False)
# ---------------------------------------------------------------------------

# Posting tool + anchoring hint, surfaced into the deep-review prompt. GitHub is
# the only platform: findings post to a PENDING (draft) review via the `gh` CLI.
POSTING_SPECS = {
    "github": {
        "tool": "one `gh api --method POST repos/<owner>/<repo>/pulls/<n>/reviews` "
                "call with NO `event` key (creates a PENDING, unsubmitted review)",
        "anchor": "a comments[] entry {path, line, side:'RIGHT'} against commit_id=<head SHA>",
        "top_anchor": "the review `body` field (a general summary on the pending review)",
    },
}


def posting_spec(platform: str) -> dict:
    """Posting tool + anchoring hint for a platform (GitHub is the only platform)."""
    return POSTING_SPECS.get(platform, POSTING_SPECS["github"])


# FETCH instruction surfaced into the gate/deep prompts so the worker retrieves
# the change BEFORE normalizing. GitHub uses the ``gh`` CLI (the repo may be
# private, so ``gh`` must be authenticated on the host). Symmetric with
# ``posting_spec`` — a new platform adds an entry, not a prompt rewrite.
FETCH_SPECS = {
    "github": (
        "use the `gh` CLI to fetch the PR (the repo may be PRIVATE, so `gh` must "
        "be authenticated on this host). Parse <owner>/<repo>/<number> from the "
        "URL, then run `gh api repos/<owner>/<repo>/pulls/<number>` (PR metadata "
        "including head.sha) and `gh api repos/<owner>/<repo>/pulls/<number>/files "
        "--paginate` (per-file `patch` diffs). Merge them into ONE JSON object of "
        'the form {...pull, "files":[{filename, patch}], "comments":[...]} and pass '
        "THAT object as the payload"
    ),
}


def fetch_spec(platform: str) -> str:
    """FETCH instruction for a platform (GitHub is the only platform)."""
    return FETCH_SPECS.get(platform, FETCH_SPECS["github"])


def _comment_body(finding: dict) -> str:
    """Build the platform-neutral comment body (redacted). Shared across platforms."""
    sev = "🔴" if finding.get("severity") == "red" else "🟡"
    lang = finding.get("lang", "")
    snippet = finding.get("snippet", "")
    body = (
        f"{sev} {finding.get('observation', '').strip()}\n\n"
        f"```{lang}\n{snippet}\n```\n\n"
        f"**Why it matters:** {finding.get('consequence', '').strip()}\n\n"
        f"**Suggestion:** {finding.get('suggestion', '').strip()}\n\n"
        f"_{DRAFT_MARKER}_"
    )
    # Redact LLM-generated content before it leaves for an external surface.
    return _redact(body)


def build_comment_payload(finding: dict, change_id: str, revision: str,
                          platform: str = "github") -> dict:
    """Build a DRAFT-only comment payload from a finding. ``publish`` is ALWAYS
    False (draft-only safety). For GitHub this is the single-finding anchor shape
    ({path, line, side} against the head commit SHA); the full pending review is
    assembled by ``build_github_review_payload``."""
    body = _comment_body(finding)
    if platform == "github":
        line = int(finding.get("line", 0) or 0)
        return {
            "path": finding.get("file", ""),
            "line": line,
            "side": "RIGHT",
            "commit_id": revision,
            "content": body,
            "publish": False,  # NON-NEGOTIABLE — a human submits the pending review.
        }
    raise ValueError(f"unsupported posting platform: {platform!r}")


def build_ship_comment(record: dict) -> str:
    """Build the (redacted) top-level SHIP-READINESS comment body — posted on EVERY
    review. The ship decision keys on 🔴 must-fix ONLY: the change is good to ship
    iff there are zero 🔴 findings AND the design verdict is not BLOCK. 🟡 should-fix
    findings are surfaced (inline + counted) but never gate the ship call. The
    verdict header + counts are deterministic; the one-line reason comes from the
    reviewer-recorded ``ship_summary`` (falling back to the design headline). Python-
    authored + ``_redact``-scrubbed so the body is clean before it reaches the CR."""
    p1 = record.get("phase1", {}) or {}
    verdict = str(p1.get("gate_verdict", "")).upper()
    counts = record.get("counts", {}) or {}
    red = int(counts.get("red", 0) or 0)
    yellow = int(counts.get("yellow", 0) or 0)
    design_block = verdict == "BLOCK"
    ready = red == 0 and not design_block

    reason = str(record.get("ship_summary") or "").strip()
    if not reason:
        # Records without a ship_summary: fall back to the design headline, else a
        # deterministic phrase, so the comment always states a reason.
        headline = str(p1.get("design_headline") or "").strip()
        if headline:
            reason = headline
        elif ready:
            reason = "No blocking must-fix issues found."
        elif design_block:
            reason = "Design needs rework before shipping."
        else:
            reason = f"{red} blocking must-fix issue(s) to resolve first."

    header = "✅ **Good to ship**" if ready else "🔴 **Not ready to ship**"
    tally: list[str] = []
    if red:
        tally.append(f"{red} must-fix 🔴")
    if yellow:
        tally.append(f"{yellow} should-fix 🟡 (non-blocking)")
    if design_block:
        tally.append("design flagged")
    tally_line = ("\n" + " · ".join(tally)) if tally else ""
    body = f"{header}: {reason}{tally_line}\n\n_{DRAFT_MARKER}_"
    return _redact(body)


def build_pending_comments(record: dict) -> list[dict]:
    """Build the full set of DRAFT comments for a reviewed change, with every body
    Python-redacted HERE (the deterministic chokepoint). The poster worker posts
    each ``body`` VERBATIM — it only resolves the (non-sensitive) anchor. Entries:
      - finding: ``{"kind": "finding", "file", "line", "body"}``  (line-anchored,
        one per surviving 🔴/🟡 finding; nice-to-haves are already dropped upstream)
      - design:  ``{"kind": "design", "body"}``                   (top-level)
    The top-level entry is the SHIP-READINESS comment and is ALWAYS emitted (on a
    clean PASS it states "good to ship"), so the author always gets a straight
    ship / no-ship call with the reason. It keeps kind ``design`` for the top-level
    anchor + posting accounting."""
    out: list[dict] = []
    for i, f in enumerate(record.get("findings", []) or []):
        out.append({
            "kind": "finding",
            # Stable identity for selective posting: the record is frozen once the
            # review has run, and the report rows are generated from the same list
            # in the same order, so the index is a durable handle the UI can name
            # one comment by. Callers filter on this; nothing else keys off it.
            "key": f"finding:{i}",
            "file": str(f.get("file", "")),
            "line": int(f.get("line", 0) or 0),
            "body": _comment_body(f),   # _comment_body already applies _redact
        })
    out.append({"kind": "design", "key": "design",
                "body": build_ship_comment(record)})
    return out


def review_payload_units(payload: dict) -> int:
    """How many deliverable units a GitHub review payload actually contains.

    The poster is instructed to write ``posted_comments = len(comments) + 1 when
    body is non-empty``, so delivery evidence is counted in PAYLOAD UNITS. Callers
    used to compare that against the number of FINDINGS instead, which is a
    different quantity: a finding with no usable ``{path, line}`` anchor is folded
    into the review body rather than becoming its own inline comment (see
    ``build_github_review_payload``). One unanchored finding therefore made a
    complete delivery look short, and the caller then re-posted comments already on
    the pull request.

    This is the single place that number is derived, so the comparison in
    ``post_recorded`` and the durable ``posting_expected`` cannot drift apart.
    """
    return len(payload.get("comments") or []) + (1 if payload.get("body") else 0)


def build_github_review_payload(record: dict) -> dict:
    """Assemble the payload for ONE ``gh api POST .../pulls/<n>/reviews`` call from
    a record's ``pending_comments``. The result deliberately has **no** ``event``
    key, so GitHub creates the review as PENDING (unsubmitted) — the GitHub
    equivalent of a draft (``publish=False``) invariant; a human submits it.

    Bodies are taken VERBATIM from ``pending_comments`` (already Python-redacted by
    ``build_pending_comments`` — the deterministic chokepoint); this only builds the
    envelope + anchors, so no LLM free-text is composed here. The ``design`` entry
    becomes the review ``body``; each ``finding`` becomes an inline ``comments[]``
    entry anchored on ``{path, line, side:'RIGHT'}`` against the head commit SHA
    (``record['revision']``). GitHub rejects inline comments outside the diff, so a
    finding lacking a usable ``{path, line}`` anchor is folded into the review body
    rather than dropped (design note §4 known constraint)."""
    pending = record.get("pending_comments") or []
    # commit_id (revision) is written by the LLM worker, so redact it too before it
    # reaches the GitHub API — same egress treatment as body/path (idempotent; a
    # real SHA never matches credential/URL patterns).
    commit_id = _redact(str(record.get("revision", "") or "")).strip()
    body_parts: list[str] = []
    comments: list[dict] = []
    for e in pending:
        kind = e.get("kind")
        # Defense-in-depth: bodies are already redacted at the deterministic
        # chokepoint (build_pending_comments -> _comment_body / build_ship_comment,
        # each of which runs _redact = redact_exfiltration_urls + redact_credentials).
        # Re-run _redact here — it is idempotent — so this external-egress point is
        # self-evidently safe even if a caller passes unredacted pending_comments.
        text = _redact(e.get("body", "") or "")
        if kind == "design":
            if text:
                body_parts.insert(0, text)   # the ship-readiness summary leads the body
            continue
        # `file` is LLM-derived and goes to an external surface, so redact it too
        # (idempotent; real file paths never match credential/URL patterns, and a
        # prompt-injected path is neutralized — a mangled path just fails GitHub's
        # in-diff anchor check, which is fail-safe).
        path = _redact(str(e.get("file", "") or ""))
        line = int(e.get("line", 0) or 0)
        if path and line > 0:
            comments.append({"path": path, "line": line, "side": "RIGHT", "body": text})
        elif text:
            body_parts.append(text)          # unanchored finding -> folded into the body
    payload: dict = {"body": "\n\n".join(p for p in body_parts if p), "comments": comments}
    if not commit_id:
        # Refuse rather than post unanchored. GitHub defaults a review with no
        # `commit_id` to the pull request's CURRENT head, which silently breaks the
        # invariant the whole submit path rests on: that a draft is bound to the head
        # it was written against. The submit guard's stale-head check then compares
        # the draft's head to the live head and passes trivially — they match because
        # GitHub stamped it at post time, not because anything reviewed that code.
        # `APPROVE` would authorize a head no review ever looked at.
        #
        # `revision` is not in the result contract's required keys, so a
        # contract-valid record can reach here without one; that is exactly the
        # case this refuses. Raising here rather than downstream keeps it a property
        # of the payload: no caller can construct an unanchored review.
        raise ValueError(
            "refusing to build a review payload with no commit_id: the record has no "
            "`revision`, and GitHub would anchor the draft to the current head "
            "instead of the reviewed one"
        )
    payload["commit_id"] = commit_id         # anchors comments to the reviewed head
    # NOTE: intentionally NO "event" key -> the review stays PENDING (unsubmitted).
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage pipeline helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("batch").add_argument("text")

    sub.add_parser("rule-pack").add_argument("repo_identity")

    pp = sub.add_parser("prepare")
    pp.add_argument("--link", required=True)
    pp.add_argument("--payload-file", required=True)

    sub.add_parser("list-results")

    args = ap.parse_args(argv)

    if args.cmd == "batch":
        print(json.dumps(parse_batch(args.text), indent=2))
    elif args.cmd == "rule-pack":
        print(json.dumps({"rule_pack": rule_pack_for_repo(args.repo_identity)}))
    elif args.cmd == "prepare":
        payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
        print(json.dumps(prepare_target(args.link, payload), indent=2))
    elif args.cmd == "list-results":
        print(json.dumps(results.list_results(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
