#!/usr/bin/env python3
"""Learning system V2 — file-centric with namespace support.

Learning is **detection-gap (miss) analysis**: issues that shipped, traced back
to the introducing change, turned into a forward-looking pattern. The *judgment*
(why was it missed? which dimension was blind? how to merge it cleanly into the
ruleset?) is the LLM's job in the `learn-from-sage` skill. This module is the
deterministic backbone for the file-centric flow:

- pattern <-> markdown (the on-disk pattern format),
- ``stage_learning`` — cheap append of a new learning to the **candidate** file
  (``learned-patterns.candidate.md``); no model call, admissible-sources only,
- ``consolidate_apply`` — atomically replace ``learned-patterns.md`` with the
  AI-merged result and clear the candidate (the AI does the one-shot merge),
- ``learned-patterns.md`` is the ONLY file reviews load as heuristics; the
  candidate is pure staging until a human triggers consolidation.

Namespaces: learnings are grouped by namespace. The "default" namespace maps to
``data/learnings/common/`` (backward compatible). User-created namespaces live
under ``data/learnings/namespaces/<name>/``. Reviews load patterns from the
configured active namespace(s).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/learning.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import store  # noqa: E402

# Learning is mined ONLY from human-validated, ground-truth signals.
ADMISSIBLE_SOURCES = {"fix_introduce", "human_comment", "design_outcome", "import"}

DEFAULT_NAMESPACE = "default"

# A valid user namespace token: lowercase alphanumeric start/end, with hyphens,
# dots and underscores in between, 2-64 chars. Deliberately excludes path
# separators and ".." so a namespace name can NEVER escape the namespaces/ dir.
_NS_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")


def _is_valid_ns_name(name: str) -> bool:
    """True if ``name`` is a safe user-namespace token (no path traversal)."""
    return bool(name) and ".." not in name and "/" not in name and bool(_NS_NAME_RE.match(name))


# ---------------------------------------------------------------------------
# Paths — namespace-aware
# ---------------------------------------------------------------------------

def _namespace_dir(namespace: str | None = None, root: Path | None = None) -> Path:
    """Resolve the directory for a namespace. 'default' (or None) -> common/.

    Any non-default name MUST be a valid namespace token; this is the single
    chokepoint that prevents a crafted name (e.g. '../common', absolute paths)
    from escaping the namespaces/ directory in any code path that targets a
    namespace (stage, consolidate, list, delete)."""
    ns = namespace or DEFAULT_NAMESPACE
    base = store.data_dir(root) / "learnings"
    if ns == DEFAULT_NAMESPACE:
        return base / "common"
    if not _is_valid_ns_name(ns):
        raise ValueError(f"invalid namespace name: {ns!r}")
    return base / "namespaces" / ns


def common_file(root: Path | None = None, namespace: str | None = None) -> Path:
    """The canonical, consolidated learnings file — the ONLY file reviews read."""
    return _namespace_dir(namespace, root) / "learned-patterns.md"


def candidate_file(root: Path | None = None, namespace: str | None = None) -> Path:
    """Append-only staging for new learnings, awaiting AI consolidation."""
    return _namespace_dir(namespace, root) / "learned-patterns.candidate.md"


def _consolidations_log(root: Path | None = None) -> Path:
    return store.data_dir(root) / "learnings" / "consolidations.jsonl"


# ---------------------------------------------------------------------------
# Namespace management
# ---------------------------------------------------------------------------

def list_namespaces(root: Path | None = None) -> list[str]:
    """Return all available namespace names. 'default' is always present."""
    namespaces = [DEFAULT_NAMESPACE]
    ns_dir = store.data_dir(root) / "learnings" / "namespaces"
    if ns_dir.is_dir():
        for d in sorted(ns_dir.iterdir()):
            if d.is_dir() and (d / "learned-patterns.md").exists():
                namespaces.append(d.name)
    return namespaces


def create_namespace(name: str, root: Path | None = None) -> dict:
    """Create a new namespace with an empty learnings file."""
    name = name.strip().lower().replace(" ", "-")
    if not _is_valid_ns_name(name):
        return {"ok": False, "error": f"invalid namespace name: {name!r} (use lowercase alphanumeric, hyphens, dots, 2-64 chars)"}
    if name == DEFAULT_NAMESPACE:
        return {"ok": False, "error": "'default' namespace already exists (it maps to common/)"}
    ns_path = _namespace_dir(name, root)
    if (ns_path / "learned-patterns.md").exists():
        return {"ok": False, "error": f"namespace {name!r} already exists"}
    ns_path.mkdir(parents=True, exist_ok=True)
    header = f"# Learned patterns — namespace: {name}\n\n"
    (ns_path / "learned-patterns.md").write_text(header, encoding="utf-8")
    return {"ok": True, "namespace": name, "path": str(ns_path)}


def delete_namespace(name: str, root: Path | None = None) -> dict:
    """Delete a user-created namespace. Cannot delete 'default'. Validates the
    name and confirms the resolved path is contained under namespaces/ before
    removing anything (defense-in-depth against path traversal)."""
    if name == DEFAULT_NAMESPACE:
        return {"ok": False, "error": "cannot delete the default namespace"}
    if not _is_valid_ns_name(name):
        return {"ok": False, "error": f"invalid namespace name: {name!r}"}
    ns_root = (store.data_dir(root) / "learnings" / "namespaces").resolve()
    ns_path = _namespace_dir(name, root).resolve()
    # Containment guard: the resolved target MUST live directly under namespaces/.
    if ns_path.parent != ns_root:
        return {"ok": False, "error": f"refusing to delete out-of-tree path for {name!r}"}
    if not ns_path.is_dir():
        return {"ok": False, "error": f"namespace {name!r} does not exist"}
    shutil.rmtree(ns_path)
    return {"ok": True, "deleted": name}


def get_active_namespaces(root: Path | None = None) -> list[str]:
    """Read the active namespaces from config. Defaults to ['default']."""
    cfg = store.load_config(root)
    return cfg.get("review", {}).get("active_namespaces", [DEFAULT_NAMESPACE])


# ---------------------------------------------------------------------------
# Pattern <-> markdown
# ---------------------------------------------------------------------------

def pattern_id(title: str, scope: str) -> str:
    """A stable, content-derived handle for a learned pattern.

    SHA-256 rather than SHA-1: the digest is only an identifier for deduplicating
    patterns by content, never a security control, but SHA-1 makes that
    indistinguishable to a scanner from a real cryptographic use (CodeQL's
    `py/weak-sensitive-data-hashing` flags it as high severity on every PR that
    touches this file). Using a modern digest states the intent in the code
    instead of in a suppression comment.

    Truncated to the same 16 hex characters as before, so the collision profile
    is unchanged. Safe to change without a migration: ids are never persisted --
    ``render_pattern`` writes only title/scope/impact/added_at/guidance, and every
    id is recomputed from ``title|scope`` when patterns are parsed, so nothing on
    disk refers to an id produced by the old algorithm.
    """
    payload = f"{title.strip().lower()}|{scope}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def render_pattern(p: dict) -> str:
    """Render a pattern as guidance-only markdown. A learned pattern is a single
    high-level, code-agnostic review heuristic: the title + guidance are the
    whole rule. If a rule needs a symptom anecdote or a concrete example to be
    understood, the guidance is underspecified — sharpen it instead."""
    return (
        f"### {p['title']} <!-- scope:{p.get('scope', 'common')} -->"
        f" <!-- impact:{p.get('impact', 'medium')} -->"
        f" <!-- added:{p.get('added_at', '')} -->\n"
        f"{' '.join(p.get('guidance', '').split())}\n"
    )


_HDR = re.compile(
    r"###\s+(?P<title>.*?)\s*(?:<!--\s*scope:(?P<scope>\w+)\s*-->)?"
    r"\s*(?:<!--\s*impact:(?P<impact>\w+)\s*-->)?"
    r"\s*(?:<!--\s*added:(?P<added>[^>]*?)\s*-->)?\s*$"
)


def parse_patterns(md: str) -> list[dict]:
    """Parse a learned-patterns(.candidate).md file into pattern dicts (tolerant).

    Patterns are guidance-only. Any legacy ``**Symptom ...:**`` / ``**Example:**``
    lines from the old format are ignored (they start with ``**``), so existing
    files keep parsing cleanly until the next consolidation rewrites them lean."""
    out: list[dict] = []
    for block in re.split(r"^(?=### )", md or "", flags=re.M):
        if not block.startswith("### "):
            continue
        lines = block.splitlines()
        m = _HDR.match(lines[0])
        if not m:
            continue
        title = m.group("title").strip()
        scope = m.group("scope") or "common"
        guidance_lines: list[str] = []
        for ln in lines[1:]:
            s = ln.strip()
            # Accumulate every non-empty, non-metadata line as guidance (matches
            # the JS parser); legacy Symptom/Example lines (prefixed with **) are
            # skipped, so the round-trip stays lossless for multi-line guidance.
            if s and not s.startswith("**"):
                guidance_lines.append(s)
        out.append({
            "id": pattern_id(title, scope), "title": title, "scope": scope,
            "impact": m.group("impact") or "medium", "added_at": (m.group("added") or "").strip(),
            "guidance": " ".join(guidance_lines),
        })
    return out


def list_patterns(scope: str = "common", repo_identity: str | None = None,
                  root: Path | None = None, namespace: str | None = None) -> list[dict]:
    """Parsed patterns from the consolidated file for a namespace."""
    path = common_file(root, namespace)
    if not path.exists():
        return []
    return parse_patterns(path.read_text(encoding="utf-8"))


def list_patterns_for_review(root: Path | None = None) -> list[dict]:
    """Load patterns from ALL active namespaces (union). Used by review workers."""
    active = get_active_namespaces(root)
    all_patterns: list[dict] = []
    seen_ids: set[str] = set()
    for ns in active:
        for p in list_patterns(root=root, namespace=ns):
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_patterns.append(p)
    return all_patterns


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)  # always close the fd, even if os.write raised
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _normalize_pattern(pattern: dict) -> dict:
    p = dict(pattern)
    p.setdefault("scope", "common")
    p.setdefault("impact", "medium")
    p.setdefault("added_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    p.setdefault("seed", False)
    p["id"] = pattern_id(p.get("title", ""), p["scope"])
    return p


_CANDIDATE_HEADER = (
    "# Pending learnings (candidate) — awaiting consolidation\n\n"
    "<!-- New learnings are appended here during reviews. Trigger \"Consolidate\" "
    "to AI-merge them into learned-patterns.md, after which this file is cleared. "
    "You may edit/curate this file directly before consolidating. -->\n\n"
)


# ---------------------------------------------------------------------------
# Stage (append a new learning to the candidate) — cheap, no model call
# ---------------------------------------------------------------------------

def stage_learning(pattern: dict, source: str, root: Path | None = None,
                   namespace: str | None = None) -> dict:
    """Append one new learning to the candidate file. Admissible-sources only
    (no self-poisoning). Deterministic; the AI merge happens later in
    ``consolidate_apply``."""
    if source not in ADMISSIBLE_SOURCES:
        raise ValueError(
            f"inadmissible learning source {source!r}; allowed: {sorted(ADMISSIBLE_SOURCES)} "
            "(the reviewer never learns from its own unpublished findings)")
    store.ensure_layout(root)
    ns_dir = _namespace_dir(namespace, root)
    ns_dir.mkdir(parents=True, exist_ok=True)
    p = _normalize_pattern(pattern)
    cf = candidate_file(root, namespace)
    if cf.exists() and cf.read_text(encoding="utf-8").strip():
        body = cf.read_text(encoding="utf-8").rstrip() + "\n\n" + render_pattern(p) + "\n"
    else:
        body = _CANDIDATE_HEADER + render_pattern(p) + "\n"
    _atomic_write(cf, body)
    return {"ok": True, "path": str(cf), "source": source,
            "namespace": namespace or DEFAULT_NAMESPACE,
            "staged": len(parse_patterns(body))}


def list_candidate(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    cf = candidate_file(root, namespace)
    if not cf.exists():
        return []
    return parse_patterns(cf.read_text(encoding="utf-8"))


def candidate_count(root: Path | None = None, namespace: str | None = None) -> int:
    return len(list_candidate(root, namespace))


def clear_candidate(root: Path | None = None, namespace: str | None = None) -> bool:
    cf = candidate_file(root, namespace)
    if cf.exists():
        cf.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Consolidate (apply the AI-merged result) — replaces learned-patterns.md
# ---------------------------------------------------------------------------

def _record_consolidation(consolidated: int, namespace: str | None = None,
                          root: Path | None = None) -> None:
    ns = namespace or DEFAULT_NAMESPACE
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "consolidated": consolidated, "namespace": ns}
    path = _consolidations_log(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def consolidate_apply(merged_md: str, root: Path | None = None,
                      namespace: str | None = None) -> dict:
    """Atomically replace learned-patterns.md with the AI-merged content, then
    clear the candidate. Refuses to write empty content (never wipes the ruleset
    on a bad merge)."""
    if not merged_md or not merged_md.strip():
        return {"ok": False, "error": "merged content is empty; refusing to overwrite learned-patterns.md"}
    store.ensure_layout(root)
    staged = candidate_count(root, namespace)
    body = merged_md if merged_md.endswith("\n") else merged_md + "\n"
    _atomic_write(common_file(root, namespace), body)
    cleared = clear_candidate(root, namespace)
    _record_consolidation(staged, namespace, root)
    return {"ok": True, "path": str(common_file(root, namespace)),
            "namespace": namespace or DEFAULT_NAMESPACE,
            "consolidated_from_candidate": staged, "candidate_cleared": cleared,
            "patterns_now": len(list_patterns(root=root, namespace=namespace))}


# ---------------------------------------------------------------------------
# Seed set — deliberately MINIMAL (bootstraps the common warm-start layer)
# ---------------------------------------------------------------------------

DEFAULT_SEED_PATTERNS: list[dict] = [
    {"title": "Reset guard flags on every exit path", "scope": "common", "impact": "high",
     "dimension": "correctness", "seed": True,
     "guidance": "When a boolean guard gates a loop or state machine, ensure it is reset on ALL exit paths, including early returns and exceptions, so the next cycle never reads a stale invariant."},
    {"title": "Authorize by confirming the owner, not by rejecting known-bad", "scope": "common", "impact": "high",
     "dimension": "security", "seed": True,
     "guidance": "Authorization must positively confirm the authenticated principal. Negative-only checks (reject known-bad, reject if disabled) are fail-open — any unanticipated caller passes."},
]


def seed_common(root: Path | None = None, force: bool = False) -> int:
    """Populate the common layer with the seed patterns if it has none yet."""
    store.ensure_layout(root)
    if not force and list_patterns("common", root=root):
        return 0
    header = "# Common learned patterns (cross-repo, warm start)\n\n"
    body = header + "\n".join(render_pattern(p) for p in DEFAULT_SEED_PATTERNS)
    _atomic_write(common_file(root), body)
    return len(DEFAULT_SEED_PATTERNS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage learning store (V2, file-centric + namespaces)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed").add_argument("--force", action="store_true")
    lp = sub.add_parser("list-patterns")
    lp.add_argument("--namespace", default=None, help="namespace to list (default: 'default')")
    lc = sub.add_parser("list-candidate")
    lc.add_argument("--namespace", default=None)
    sp = sub.add_parser("stage", help="append a new learning to the candidate file")
    sp.add_argument("--file", required=True, help="JSON pattern file")
    sp.add_argument("--source", required=True, choices=sorted(ADMISSIBLE_SOURCES))
    sp.add_argument("--namespace", default=None)
    cp = sub.add_parser("consolidate", help="apply the AI-merged learned-patterns.md and clear the candidate")
    cp.add_argument("--merged-file", required=True, help="file holding the AI-merged learned-patterns.md")
    cp.add_argument("--namespace", default=None)
    cc = sub.add_parser("clear-candidate")
    cc.add_argument("--namespace", default=None)
    sub.add_parser("list-namespaces")
    cn = sub.add_parser("create-namespace")
    cn.add_argument("name")
    dn = sub.add_parser("delete-namespace")
    dn.add_argument("name")
    sub.add_parser("list-for-review", help="patterns from all active namespaces (union)")

    args = ap.parse_args(argv)
    if args.cmd == "seed":
        print(json.dumps({"seeded": seed_common(force=args.force)}))
    elif args.cmd == "list-patterns":
        print(json.dumps(list_patterns(namespace=args.namespace), indent=2))
    elif args.cmd == "list-candidate":
        print(json.dumps(list_candidate(namespace=args.namespace), indent=2))
    elif args.cmd == "stage":
        pat = json.loads(Path(args.file).read_text(encoding="utf-8"))
        print(json.dumps(stage_learning(pat, args.source, namespace=args.namespace), indent=2))
    elif args.cmd == "consolidate":
        merged = Path(args.merged_file).read_text(encoding="utf-8")
        print(json.dumps(consolidate_apply(merged, namespace=args.namespace), indent=2))
    elif args.cmd == "clear-candidate":
        print(json.dumps({"cleared": clear_candidate(namespace=args.namespace)}))
    elif args.cmd == "list-namespaces":
        print(json.dumps(list_namespaces(), indent=2))
    elif args.cmd == "create-namespace":
        print(json.dumps(create_namespace(args.name), indent=2))
    elif args.cmd == "delete-namespace":
        print(json.dumps(delete_namespace(args.name), indent=2))
    elif args.cmd == "list-for-review":
        print(json.dumps(list_patterns_for_review(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
