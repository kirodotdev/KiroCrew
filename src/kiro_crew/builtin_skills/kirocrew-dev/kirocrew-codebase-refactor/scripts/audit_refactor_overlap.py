#!/usr/bin/env python3
"""Audit local Git drift and path overlap for a refactor scope without writes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from refactor_limits import (
    MAX_RETAINED_ITEMS,
    MAX_RETAINED_STRING_CHARS,
    BoundedAppendAction,
    bounded_cli_text,
    bounded_text,
    filesystem_decode,
    markdown_code,
)

from kiro_crew.git_divergence import divergence_count_args, parse_divergence_counts


def git(root: Path, *args: str, check: bool = True) -> str:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def git_path(root: Path, *args: str) -> str:
    """Run Git for one pathname result without losing undecodable bytes."""

    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({result.returncode}): {detail}")
    return filesystem_decode(result.stdout.rstrip(b"\r\n"))


def git_lines(root: Path, *args: str, check: bool = True) -> tuple[list[str], int]:
    """Stream bounded Git output lines instead of retaining an unbounded blob."""

    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    process = subprocess.Popen(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    lines: list[str] = []
    overflow = 0
    assert process.stdout is not None
    for raw_line in process.stdout:
        line, truncated = bounded_text(raw_line.rstrip("\r\n"))
        if truncated or len(lines) >= MAX_RETAINED_ITEMS:
            overflow += 1
            continue
        lines.append(line)
    returncode = process.wait()
    if check and returncode:
        raise RuntimeError(f"git {' '.join(args)} failed ({returncode})")
    return lines, overflow


def git_nul_fields(root: Path, *args: str, check: bool = True) -> tuple[list[str], int]:
    """Stream bounded NUL-delimited Git pathnames without quote decoding."""

    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    process = subprocess.Popen(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    fields: list[str] = []
    overflow = 0
    current = bytearray()
    discarding = False
    max_field_bytes = MAX_RETAINED_STRING_CHARS * 4
    assert process.stdout is not None
    while chunk := process.stdout.read(64 * 1024):
        for value in chunk:
            if value == 0:
                if discarding or len(fields) >= MAX_RETAINED_ITEMS:
                    overflow += 1
                elif current:
                    fields.append(filesystem_decode(bytes(current)))
                current.clear()
                discarding = False
            elif not discarding:
                if len(current) >= max_field_bytes:
                    current.clear()
                    discarding = True
                else:
                    current.append(value)
    if discarding:
        overflow += 1
    elif current:
        if len(fields) >= MAX_RETAINED_ITEMS:
            overflow += 1
        else:
            fields.append(filesystem_decode(bytes(current)))
    returncode = process.wait()
    if check and returncode:
        raise RuntimeError(f"git {' '.join(args)} failed ({returncode})")
    return fields, overflow


def root_and_ref(start: Path, ref: str) -> tuple[Path, str]:
    root = Path(git_path(start, "rev-parse", "--show-toplevel")).resolve()
    sha = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    return root, sha


def changed_files(root: Path, left: str, right: str) -> tuple[set[str], int]:
    output, overflow = git_nul_fields(
        root, "diff", "--no-renames", "--name-only", "-z", f"{left}..{right}", "--"
    )
    paths: set[str] = set()
    for pathname in output:
        normalized, truncated = bounded_text(pathname)
        if not normalized:
            continue
        if truncated or (normalized not in paths and len(paths) >= MAX_RETAINED_ITEMS):
            overflow += 1
            continue
        paths.add(normalized)
    return paths, overflow


def in_claim(path: str, claims: list[str]) -> bool:
    normalized = path.strip("/")
    return any(
        claim == "." or normalized == claim or normalized.startswith(claim.rstrip("/") + "/")
        for claim in claims
    )


def path_history(root: Path, base: str, upstream: str, claim: str) -> tuple[dict[str, Any], int]:
    count_text = git(root, "rev-list", "--count", f"{base}..{upstream}", "--", claim).strip()
    latest_lines, retention_overflow = git_lines(
        root,
        "log",
        "-1",
        "--format=%H%x09%aI%x09%s",
        f"{base}..{upstream}",
        "--",
        claim,
    )
    latest = latest_lines[0] if latest_lines else ""
    latest_item = None
    if latest:
        parts = latest.split("\t", 2)
        subject, bounded_truncated = bounded_text(parts[2] if len(parts) > 2 else "")
        latest_item = {
            "sha": parts[0],
            "date": parts[1] if len(parts) > 1 else None,
            "subject": subject or None,
            "subject_truncated": bool(retention_overflow or bounded_truncated),
        }
    return (
        {"path": claim, "upstream_commits": int(count_text or 0), "latest": latest_item},
        retention_overflow,
    )


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Refactor Overlap Audit",
        "",
        f"- Collected: {markdown_code(str(report['collected_at']))}",
        f"- Root: {markdown_code(str(report['root']))}",
        f"- Base: {markdown_code(str(report['base']['ref']))} → {markdown_code(str(report['base']['sha']))}",
        f"- Upstream: {markdown_code(str(report['upstream']['ref']))} → {markdown_code(str(report['upstream']['sha']))}",
        f"- Head: {markdown_code(str(report['head']['ref']))} → {markdown_code(str(report['head']['sha']))}",
        f"- Upstream-only / head-only commits: **{report['relationship']['upstream_only']:,} / {report['relationship']['head_only']:,}**",
        f"- Upstream commits since base: **{report['relationship']['upstream_since_base']:,}**",
        f"- Dirty worktree entries: **{report['dirty_entries']:,}**",
        f"- Retention overflow: **{report['retention_overflow']:,}** item(s)",
        "",
        "## Claim history",
        "",
        "| Claimed path | Upstream commits since base | Latest upstream touch |",
        "|---|---:|---|",
    ]
    for item in report["claim_history"]:
        latest = item["latest"]
        detail = (
            "—"
            if latest is None
            else markdown_code(f"{latest['sha'][:10]} {latest['date']} {latest['subject']}")
        )
        lines.append(
            f"| {markdown_code(str(item['path']))} | {item['upstream_commits']:,} | {detail} |"
        )

    lines.extend(
        [
            "",
            "## Changed paths",
            "",
            f"- Head changed in claim: **{len(report['head_changed_in_claim']):,}**",
            f"- Upstream changed in claim: **{len(report['upstream_changed_in_claim']):,}**",
            f"- Exact-path overlap: **{len(report['exact_overlap']):,}**",
        ]
    )
    if report["exact_overlap"]:
        lines.extend(["", "### Exact overlap", ""])
        lines.extend(f"- {markdown_code(str(path))}" for path in report["exact_overlap"])
    lines.extend(
        [
            "",
            "> Exact path overlap is a screening signal, not a semantic mergeability verdict. Inspect intent and contracts before replaying changes.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=bounded_cli_text, default=".", help="Repository/worktree path"
    )
    parser.add_argument(
        "--base", type=bounded_cli_text, required=True, help="Original baseline ref or SHA"
    )
    parser.add_argument(
        "--upstream", type=bounded_cli_text, required=True, help="Current upstream ref"
    )
    parser.add_argument(
        "--head",
        type=bounded_cli_text,
        default="HEAD",
        help="Scope head ref (default: HEAD)",
    )
    parser.add_argument(
        "--path",
        action=BoundedAppendAction,
        type=bounded_cli_text,
        default=[],
        help="Claimed repository-relative file or directory; repeatable",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        root, base_sha = root_and_ref(Path(args.root), args.base)
        upstream_sha = git(root, "rev-parse", "--verify", f"{args.upstream}^{{commit}}").strip()
        head_sha = git(root, "rev-parse", "--verify", f"{args.head}^{{commit}}").strip()
        counts = parse_divergence_counts(
            git(root, *divergence_count_args(upstream_sha, head=head_sha))
        )
        if counts is None:
            raise ValueError("git returned an unreadable upstream/head divergence count")
        upstream_files, upstream_overflow = changed_files(root, base_sha, upstream_sha)
        head_files, head_overflow = changed_files(root, base_sha, head_sha)
        claims: list[str] = []
        claim_overflow = 0
        for raw_claim in args.path:
            normalized = raw_claim.replace("\\", "/").strip("/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            normalized, truncated = bounded_text(normalized)
            if not normalized:
                continue
            if truncated or len(claims) >= MAX_RETAINED_ITEMS:
                claim_overflow += 1
                continue
            claims.append(normalized)
        if not claims:
            claims = sorted(head_files)
        upstream_in_claim = sorted(path for path in upstream_files if in_claim(path, claims))
        head_in_claim = sorted(path for path in head_files if in_claim(path, claims))
        exact_overlap = sorted(set(upstream_in_claim) & set(head_in_claim))
        dirty_lines, dirty_overflow = git_lines(root, "status", "--porcelain")
        dirty_entries = sum(bool(line) for line in dirty_lines) + dirty_overflow
        upstream_since_base = int(
            git(root, "rev-list", "--count", f"{base_sha}..{upstream_sha}").strip() or 0
        )
        claim_history: list[dict[str, Any]] = []
        history_overflow = 0
        for claim in claims:
            history, overflow = path_history(root, base_sha, upstream_sha, claim)
            claim_history.append(history)
            history_overflow += overflow
        report = {
            "schema_version": 1,
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "root": str(root),
            "base": {"ref": args.base, "sha": base_sha},
            "upstream": {"ref": args.upstream, "sha": upstream_sha},
            "head": {"ref": args.head, "sha": head_sha},
            "relationship": {
                "upstream_only": counts.behind,
                "head_only": counts.ahead,
                "upstream_since_base": upstream_since_base,
            },
            "dirty_entries": dirty_entries,
            "retention_overflow": (
                upstream_overflow
                + head_overflow
                + claim_overflow
                + dirty_overflow
                + history_overflow
            ),
            "claims": claims,
            "claim_history": claim_history,
            "head_changed_in_claim": head_in_claim,
            "upstream_changed_in_claim": upstream_in_claim,
            "exact_overlap": exact_overlap,
            "note": "Path overlap is not semantic mergeability; inspect intent, contracts, and active forge changes.",
        }
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2, ensure_ascii=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
