#!/usr/bin/env python3
"""Is this repository, path or technique inside the rules of engagement?

The conductor decides scope with this script, never by its own judgment about
what seems reasonable: a tone instruction degrades silently across a long
session, and a scope verdict is testable. **Exit codes are the interface** --
stdout carries the reasons, the exit code carries the answer:

===  ==================  ================================================
  0  ``IN_SCOPE``        every asked-about item is covered by an active rule
 10  ``OUT_OF_SCOPE``    a ``forbidden`` rule matched, or an item is covered
                         by no ``scope`` / ``allowed_techniques`` rule
 20  ``UNKNOWN``         no rules, an unreadable database, a rule this script
                         cannot interpret, or nothing asked
 30  ``NEEDS_APPROVAL``  a technique the ``human_approval`` gate names
===  ==================  ================================================

``UNKNOWN`` IS NEVER PERMISSION. It is a distinct nonzero code rather than a
flavour of zero precisely so a caller that only checks ``rc == 0`` cannot read
"I could not tell" as "go ahead". ``NEEDS_APPROVAL`` is nonzero for the same
reason: the gate is held by not proceeding, and only a human lifts it.

Usage::

    python3 scope_check.py [--db PATH] [--roe-json PATH] \\
        [--repo NAME] [--path P ...] [--technique T ...]

Where the rules come from. The ACTIVE ``roe_rules`` rows are the source of
truth, read straight out of the ledger (``ledger.py``, ``--db`` overriding
``ledger.default_db_path()``). ``rules-of-engagement.json`` beside the skill is
an EXPORT, so it is consulted ONLY when the database holds zero rule ROWS, and
that substitution is announced on stderr -- a scope answer that came from a file
somebody could edit without leaving a row behind should say so out loud.

Two things are NOT fallback triggers, because neither says the ledger is empty:

- **An unreadable database.** It told us nothing, so the answer is ``UNKNOWN``.
- **Rows that exist with none active.** That is the RFC's revert path -- a bad
  rule is undone by flipping ``active``, not by deleting it -- so reading it as
  "no rules" and consulting the export restored the very scope the revert
  removed. Every rule revoked means there is no scope to check against:
  ``UNKNOWN``.

The value grammar this script can interpret, and nothing else:

``repo:<name>``
    Matches ``--repo`` exactly, case-insensitively.
``path:<prefix>``
    Matches a ``--path`` that IS the prefix or sits under it, compared by whole
    path segment so ``path:src`` never covers ``srcfoo``. The query is
    normalised first, so ``src/../../etc/passwd`` cannot borrow ``src/``'s
    coverage.
``technique:<slug>`` or a bare slug
    A technique. In ``allowed_techniques`` and ``human_approval`` -- the two
    fields that GRANT -- those two forms are the ONLY interpretable ones: a
    ``repo:`` or ``path:`` value there is malformed, and since the matcher
    compares the value with its prefix stripped, reading one anyway let
    ``path:network-egress`` authorise the technique ``network-egress``.

**A rule that PERMITS is matched exactly; only a rule that REFUSES is matched by
containment.** ``allowed_techniques`` and ``human_approval`` need whole-value
equality, because containment there let a fragment stand in for the whole:
``--technique unit`` matched the allowed ``local-unit-poc`` and came back
``IN_SCOPE`` for a technique no rule authorises. ``forbidden`` keeps containment,
since its values are negative phrases -- ``no-network-egress-from-a-poc`` -- and
matching ``--technique network-egress`` against it is the point. The asymmetry is
the invariant: widening only ever widens what is refused.

``severity_scale`` and ``report_schema`` rows are not scope inputs; they are
skipped, and their presence never makes a verdict ``UNKNOWN``. Any OTHER field
name, and any value whose form is not above, is a rule this script cannot
interpret -- recorded and reported, never guessed at.

Precedence, in evaluation order:

1. a ``forbidden`` match, which no other rule trades against;
2. no interpretable rules at all -- there is nothing to check against;
3. an item no rule covers;
4. a ``human_approval`` technique -- the gate holds even when an
   ``allowed_techniques`` row also names it, because a gate any allow row can
   neutralise is not a gate;
5. a rule that could not be interpreted;
6. otherwise in scope.

Reads one SQLite file (SELECT only) or one JSON file. No network, no subprocess,
and no write anywhere -- including no creation of an absent database, so asking
a scope question never leaves a ledger behind.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import posixpath
import re
import sys
from collections.abc import Buffer
from pathlib import Path
from typing import Any, Iterable, Sequence

IN_SCOPE = "IN_SCOPE"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
UNKNOWN = "UNKNOWN"
NEEDS_APPROVAL = "NEEDS_APPROVAL"

EXIT_CODES = {
    IN_SCOPE: 0,
    OUT_OF_SCOPE: 10,
    UNKNOWN: 20,
    NEEDS_APPROVAL: 30,
}

SCOPE_FIELD = "scope"
ALLOWED_FIELD = "allowed_techniques"
FORBIDDEN_FIELD = "forbidden"
APPROVAL_FIELD = "human_approval"
SCOPE_FIELDS = (SCOPE_FIELD, ALLOWED_FIELD, FORBIDDEN_FIELD, APPROVAL_FIELD)
# The two fields that GRANT latitude. `forbidden` is excluded on purpose: a
# `path:` or `repo:` value there is meaningful and is matched as one, because
# widening what is REFUSED is safe in a way that widening what is permitted is
# not.
PERMITTING_FIELDS = (ALLOWED_FIELD, APPROVAL_FIELD)
# Present in the rules of engagement, but not scope inputs: they describe how a
# finding is graded and shaped, not what an auditor may touch. Named so their
# presence is a deliberate skip rather than an uninterpretable field.
NON_SCOPE_FIELDS = ("severity_scale", "report_schema")

ROE_EXPORT_FILENAME = "rules-of-engagement.json"

# ANY drive-prefixed Windows path, separator or not. `C:/x` and `C:\x` are
# drive-ABSOLUTE; `C:x` is drive-RELATIVE -- relative to that drive's own current
# directory, which is still not this repository. Requiring the separator let the
# drive-relative form read as an ordinary relative path, so a bare-dot scope
# prefix covered `C:Windows\System32`. Neither form is inside the repo, so the
# separator is not part of what makes it out of scope.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path: str, data: Buffer, *, _mode: int = 0o666) -> None:
        return None


def load_ledger() -> Any:
    """Load the sibling ledger without cwd, sys.path, or bytecode side effects.

    Mirrors ``prepare-pr/scripts/pr_status.py``: a skill's scripts are synced out
    of the package tree and run as bare files, so ``ledger`` is a file beside
    this one rather than an importable module, and importing it the ordinary way
    would drop a ``__pycache__`` entry into the checked-out tree.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.py")
    name = "_security_conductor_ledger"
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import security conductor ledger: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_LEDGER: Any = None


def ledger() -> Any:
    """The ledger module, loaded once on first use.

    LAZY, and public, so ``finding_entry.py`` can share this one instance rather
    than executing ``ledger.py`` a second time in the same invocation.
    """
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = load_ledger()
    return _LEDGER


def normalize_technique(value: str) -> tuple[str, ...]:
    """A technique as comparable whole words: lowercase, punctuation as breaks.

    Returned as a tuple of words rather than a string so containment can be
    checked word-wise. ``no-network-egress``, ``no_network_egress`` and
    ``No Network Egress`` are one technique written three ways, and a rule
    author should not have to guess which spelling the script compares.
    """
    return tuple(part for part in re.split(r"[^0-9a-z]+", value.strip().lower()) if part)


def technique_equals(rule_value: str, asked: str) -> bool:
    """Whole-value equality, for a rule that PERMITS.

    Used by ``allowed_techniques`` and ``human_approval``. Anything looser lets
    a fragment authorise a technique nobody approved.
    """
    rule_words = normalize_technique(rule_value)
    asked_words = normalize_technique(asked)
    return bool(rule_words) and rule_words == asked_words


def forbidden_technique_matches(rule_value: str, asked: str) -> bool:
    """Containment, for a rule that REFUSES.

    A ``forbidden`` value is a negative phrase, so the asked-about technique is
    looked for as a run of consecutive whole words inside it once a leading
    ``no`` is dropped. Word runs, not substrings: ``dos`` must not match
    ``no-windows-paths``, and ``poc`` must not match ``no-pocket``.
    """
    rule_words = normalize_technique(rule_value)
    if rule_words and rule_words[0] == "no":
        rule_words = rule_words[1:]
    asked_words = normalize_technique(asked)
    if not asked_words or not rule_words:
        return False
    if rule_words == asked_words:
        return True
    span = len(asked_words)
    return any(
        rule_words[index : index + span] == asked_words
        for index in range(len(rule_words) - span + 1)
    )


def is_absolute_path(value: str) -> bool:
    """Outside relative repository scope on ANY host.

    A leading separator, or ANY drive prefix. Not ``os.path.isabs``, which answers
    for the host running this script -- a Windows path checked on Linux would read
    as relative, and the rules of engagement are the same document on both. Nor is
    it only the drive-ABSOLUTE form: `C:x` is relative to that drive's current
    directory, which is not this repository either.
    """
    text = value.strip().replace("\\", "/")
    return text.startswith("/") or bool(_DRIVE_RE.match(value.strip()))


def normalize_path(value: str) -> str:
    """A path as compared: posix separators, normalised, no trailing slash.

    ``normpath`` runs BEFORE any prefix comparison so a traversal cannot borrow
    an in-scope prefix's coverage: ``src/../../etc/passwd`` normalises to
    ``../etc/passwd``, which no ``path:src/`` rule covers.
    """
    text = value.strip().replace("\\", "/")
    if not text:
        return ""
    normalized = posixpath.normpath(text)
    if normalized in (".", "/"):
        return normalized
    return normalized.rstrip("/")


def path_covered_by(prefix: str, asked: str, *, fold_case: bool = False) -> bool:
    """Is ``asked`` the path ``prefix`` or something under it, segment-wise.

    ``fold_case`` is for rules that REFUSE. On a case-insensitive filesystem
    ``SRC/private/key.py`` and ``src/private/key.py`` are one file, so a
    forbidden prefix compared byte-for-byte missed the file it names. Granting
    rules keep the exact comparison: folding case there would widen what is
    permitted, and the permit/refuse asymmetry says only refusal may widen.
    """
    base = normalize_path(prefix)
    target = normalize_path(asked)
    if fold_case:
        base = base.lower()
        target = target.lower()
    if not base or not target:
        return False
    if target == ".." or target.startswith("../"):
        # Escapes whatever it was measured against, so no rule can cover it.
        return False
    if base == ".":
        # "Everything under the repository root" is the only sensible reading of
        # a bare-dot prefix, and only for a RELATIVE query. An absolute path is
        # somewhere else on the host entirely -- including a drive-qualified
        # Windows one, which has no leading slash and so read as relative.
        return not is_absolute_path(asked)
    return target == base or target.startswith(base + "/")


class Rule:
    """One active ``roe_rules`` row, or one entry of the JSON export."""

    def __init__(self, *, rule_id: int | None, field: str, value: str) -> None:
        self.id = rule_id
        self.field = field
        self.value = value

    def as_match(self) -> dict[str, Any]:
        return {"id": self.id, "field": self.field, "value": self.value}


KNOWN_KINDS = ("repo", "path", "technique")
# A value whose text before the first colon LOOKS like a kind prefix: a run of
# word characters with no whitespace, hyphen or path separator. `pth:src/private`
# and the one-character `p:src/private` are both kind-shaped typos. The ONE thing
# that is not a kind is a Windows drive: a single letter followed by `:` and then
# a separator (`C:/x`, `C:\\x`) or nothing at all (`C:`), which `is_absolute_path`
# already refuses as out of repo. `no-dos:ever` and `a b:c` are bare values.
_KIND_PREFIX_RE = re.compile(r"^([A-Za-z_]\w*):")
_DRIVE_ONLY_RE = re.compile(r"^[A-Za-z]:(?:[\\/]|$)")


def split_value(value: str) -> tuple[str | None, str]:
    """``("repo", "x")`` for ``repo:x``; ``(None, value)`` for a bare value.

    An UNKNOWN kind prefix is returned as itself -- ``("pth", "src/private")`` --
    so ``uninterpretable`` can refuse it. Stripping it to a bare value read a
    typo'd ``forbidden=pth:src/private`` as a technique phrase, and the path
    ceiling it was meant to be silently vanished.
    """
    match = _KIND_PREFIX_RE.match(value)
    if match is None or _DRIVE_ONLY_RE.match(value):
        return None, value.strip()
    kind = match.group(1)
    return kind, value[match.end() :].strip()


def unknown_kind(kind: str | None) -> bool:
    return kind is not None and kind not in KNOWN_KINDS


def rules_from_db(db_path: Path, module: Any) -> tuple[list[Rule], int]:
    """``(active rules, total rule rows)``. Raises on an unreadable database.

    The TOTAL count is returned because the two zero cases are opposite
    instructions. Zero rows means nobody has written the rules of engagement yet.
    Zero ACTIVE rows out of many means somebody deliberately switched them off --
    which is the RFC's revert path (``UPDATE roe_rules SET active = 0``) -- so
    reading that as "no rules" and consulting the export restored exactly the
    scope the revert removed.
    """
    conn = module.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, field, value FROM roe_rules WHERE active = 1 ORDER BY field ASC, id ASC"
        ).fetchall()
        total = int(conn.execute("SELECT COUNT(*) FROM roe_rules").fetchone()[0])
    finally:
        conn.close()
    return [
        Rule(rule_id=int(row["id"]), field=str(row["field"]), value=str(row["value"]))
        for row in rows
    ], total


def rules_from_json(path: Path) -> list[Rule]:
    """Rules from the export file. Raises on unreadable or malformed JSON.

    Export entries carry no row id, so ``id`` is ``null`` in the verdict -- the
    visible difference between an answer backed by an attributable row and one
    backed by a file.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    grouped = payload.get("rules", payload) if isinstance(payload, dict) else payload
    if not isinstance(grouped, dict):
        raise ValueError(f"{path}: expected an object of rule fields")
    rules: list[Rule] = []
    for field, entries in grouped.items():
        if not isinstance(entries, list):
            raise ValueError(f"{path}: field {field!r} is not a list")
        for entry in entries:
            value = entry.get("value") if isinstance(entry, dict) else entry
            if not isinstance(value, str):
                raise ValueError(f"{path}: field {field!r} has a non-string value")
            rules.append(Rule(rule_id=None, field=str(field), value=value))
    return rules


def default_roe_json() -> Path:
    """The export beside the skill, one directory up from ``scripts/``."""
    return Path(os.path.dirname(os.path.abspath(__file__))).parent / ROE_EXPORT_FILENAME


class Question:
    """What the caller asked about."""

    def __init__(
        self, *, repo: str | None, paths: Sequence[str], techniques: Sequence[str]
    ) -> None:
        self.repo = (repo or "").strip() or None
        self.paths = [item for item in (p.strip() for p in paths) if item]
        self.techniques = [item for item in (t.strip() for t in techniques) if item]

    def is_empty(self) -> bool:
        return self.repo is None and not self.paths and not self.techniques


class Verdict:
    def __init__(self, verdict: str, matched: list[dict[str, Any]], reasons: list[str]) -> None:
        self.verdict = verdict
        self.matched = matched
        self.reasons = reasons

    def payload(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "matched_rules": self.matched, "reasons": self.reasons}

    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]


def forbidden_hits(rules: Iterable[Rule], question: Question) -> list[tuple[Rule, str]]:
    """Every ``forbidden`` rule the question runs into, with its reason."""
    hits: list[tuple[Rule, str]] = []
    for rule in rules:
        if rule.field != FORBIDDEN_FIELD:
            continue
        kind, body = split_value(rule.value)
        if unknown_kind(kind):
            # Not a technique phrase; `uninterpretable` reports it.
            continue
        if kind == "repo":
            if question.repo and body.lower() == question.repo.lower():
                hits.append((rule, f"repository {question.repo!r} is forbidden by {rule.value!r}"))
            continue
        if kind == "path":
            for asked in question.paths:
                if path_covered_by(body, asked, fold_case=True):
                    hits.append((rule, f"path {asked!r} is forbidden by {rule.value!r}"))
            continue
        # A bare or `technique:`-prefixed forbidden value is a technique phrase.
        for asked in question.techniques:
            if forbidden_technique_matches(body, asked):
                hits.append((rule, f"technique {asked!r} is forbidden by {rule.value!r}"))
    return hits


def uninterpretable(rules: Iterable[Rule]) -> list[tuple[Rule, str]]:
    """Rules whose field or value form this script cannot read."""
    problems: list[tuple[Rule, str]] = []
    for rule in rules:
        if rule.field in NON_SCOPE_FIELDS:
            continue
        if rule.field not in SCOPE_FIELDS:
            problems.append((rule, f"rule field {rule.field!r} is not one this script interprets"))
            continue
        kind, body = split_value(rule.value)
        if not body:
            problems.append((rule, f"rule {rule.field}={rule.value!r} has an empty value"))
            continue
        if unknown_kind(kind):
            # Applies to `forbidden` too, the one field the permit/refuse
            # asymmetry otherwise leaves alone: a refusing rule nobody can read
            # is a ceiling that is not being enforced, and that fails OPEN.
            problems.append(
                (
                    rule,
                    f"rule {rule.field}={rule.value!r} names {kind!r}, not a kind this script reads",
                )
            )
            continue
        if rule.field == SCOPE_FIELD and kind not in ("repo", "path"):
            # A bare `scope` value cannot be read: a repository and a path are
            # matched by different rules, and guessing which was meant is
            # exactly the judgment this script exists to remove.
            problems.append((rule, f"scope rule {rule.value!r} names neither repo: nor path:"))
            continue
        if rule.field in PERMITTING_FIELDS and kind in ("repo", "path"):
            # A PERMITTING field grants a technique, so a `repo:` or `path:`
            # value in one is malformed, and reading it fails OPEN: the matcher
            # strips the prefix before comparing, so `path:network-egress`
            # authorises the technique `network-egress`. A malformed rule in the
            # field that GRANTS is the one place a wrong reading hands out
            # latitude, so it withholds permission instead.
            problems.append(
                (
                    rule,
                    f"{rule.field} rule {rule.value!r} names a"
                    f" {kind}, but that field grants a technique",
                )
            )
    return problems


def _first_permitting(rules: Sequence[Rule], asked: str) -> Rule | None:
    return next(
        (rule for rule in rules if technique_equals(split_value(rule.value)[1], asked)), None
    )


def evaluate(rules: Sequence[Rule], question: Question) -> Verdict:
    """Apply the precedence order in the module docstring."""
    if question.is_empty():
        return Verdict(UNKNOWN, [], ["nothing was asked about; UNKNOWN is never permission"])

    hits = forbidden_hits(rules, question)
    if hits:
        return Verdict(
            OUT_OF_SCOPE,
            [rule.as_match() for rule, _ in hits],
            [reason for _, reason in hits],
        )

    problems = uninterpretable(rules)
    broken = {id(rule) for rule, _ in problems}
    interpretable = [
        rule for rule in rules if rule.field not in NON_SCOPE_FIELDS and id(rule) not in broken
    ]
    if not interpretable:
        return Verdict(
            UNKNOWN,
            [rule.as_match() for rule, _ in problems],
            ["no interpretable rule of engagement to check against"]
            + [reason for _, reason in problems],
        )

    allowed = [rule for rule in interpretable if rule.field == ALLOWED_FIELD]
    gates = [rule for rule in interpretable if rule.field == APPROVAL_FIELD]
    scope_rules = [rule for rule in interpretable if rule.field == SCOPE_FIELD]
    repo_rules = [rule for rule in scope_rules if split_value(rule.value)[0] == "repo"]
    path_rules = [rule for rule in scope_rules if split_value(rule.value)[0] == "path"]

    matched: list[dict[str, Any]] = []
    reasons: list[str] = []
    approvals: list[tuple[Rule, str]] = []
    uncovered: list[str] = []

    for asked in question.techniques:
        # The gate is checked FIRST. A technique in both `human_approval` and
        # `allowed_techniques` is the ordinary "in scope, but a human says go"
        # configuration, and letting the allow row win made the approval
        # ceiling vanish whenever someone also listed the technique as allowed.
        gate = _first_permitting(gates, asked)
        if gate is not None:
            approvals.append((gate, f"technique {asked!r} needs a human yes per {gate.value!r}"))
            continue
        hit = _first_permitting(allowed, asked)
        if hit is not None:
            matched.append(hit.as_match())
            reasons.append(f"technique {asked!r} is allowed by {hit.value!r}")
            continue
        uncovered.append(f"technique {asked!r} is in no allowed_techniques rule")

    if question.repo is not None:
        hit = next(
            (
                rule
                for rule in repo_rules
                if split_value(rule.value)[1].lower() == question.repo.lower()
            ),
            None,
        )
        if hit is None:
            uncovered.append(f"repository {question.repo!r} is in no scope rule")
        else:
            matched.append(hit.as_match())
            reasons.append(f"repository {question.repo!r} is in scope by {hit.value!r}")

    for asked in question.paths:
        hit = next(
            (rule for rule in path_rules if path_covered_by(split_value(rule.value)[1], asked)),
            None,
        )
        if hit is None:
            uncovered.append(f"path {asked!r} is in no scope rule")
        else:
            matched.append(hit.as_match())
            reasons.append(f"path {asked!r} is in scope by {hit.value!r}")

    if uncovered:
        return Verdict(OUT_OF_SCOPE, matched, uncovered)
    if approvals:
        return Verdict(
            NEEDS_APPROVAL,
            matched + [rule.as_match() for rule, _ in approvals],
            reasons + [reason for _, reason in approvals],
        )
    if problems:
        # Everything asked about is covered, but a rule in the set could not be
        # read -- so "in scope" would be a claim about rules nobody parsed.
        return Verdict(
            UNKNOWN,
            matched + [rule.as_match() for rule, _ in problems],
            reasons + [reason for _, reason in problems],
        )
    return Verdict(IN_SCOPE, matched, reasons)


def load_rules(db_path: Path, roe_json: Path, module: Any) -> tuple[list[Rule], Verdict | None]:
    """Rules to evaluate, or the ``UNKNOWN`` verdict that replaces them."""
    if db_path.exists():
        try:
            rules, total = rules_from_db(db_path, module)
        except Exception as exc:  # sqlite3.Error, OSError, a missing table
            return [], Verdict(UNKNOWN, [], [f"cannot read rules from {db_path}: {exc}"])
        if rules:
            return rules, None
        if total:
            # Rules exist and every one is switched off. That is a REVOCATION,
            # not an empty ledger, and the export still carries what was revoked.
            return [], Verdict(
                UNKNOWN,
                [],
                [
                    f"{db_path} holds {total} rule(s) and none is active;"
                    " every rule of engagement has been revoked, so there is no scope to"
                    " check against and the export is not a substitute for it"
                ],
            )
        print(
            f"{db_path} holds no rules of engagement; falling back to the export at {roe_json}",
            file=sys.stderr,
        )
    else:
        print(
            f"{db_path} does not exist; falling back to the export at {roe_json}",
            file=sys.stderr,
        )
    if not roe_json.exists():
        return [], Verdict(UNKNOWN, [], [f"no rules in {db_path} and no export at {roe_json}"])
    try:
        return rules_from_json(roe_json), None
    except Exception as exc:
        return [], Verdict(UNKNOWN, [], [f"cannot read rules from {roe_json}: {exc}"])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Is this repository, path or technique inside the rules of engagement?"
    )
    parser.add_argument("--db", default=None, help="ledger path (default: data home)")
    parser.add_argument(
        "--roe-json",
        default=None,
        help="rules-of-engagement export to fall back to (default: beside the skill)",
    )
    parser.add_argument("--repo", default=None, help="repository to ask about")
    parser.add_argument("--path", action="append", default=[], dest="paths")
    parser.add_argument("--technique", action="append", default=[], dest="techniques")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    module = ledger()
    db_path = Path(args.db) if args.db else module.default_db_path()
    roe_json = Path(args.roe_json) if args.roe_json else default_roe_json()

    rules, failure = load_rules(db_path, roe_json, module)
    if failure is not None:
        verdict = failure
    else:
        question = Question(repo=args.repo, paths=args.paths, techniques=args.techniques)
        verdict = evaluate(rules, question)
    print(json.dumps(verdict.payload(), sort_keys=True))
    return verdict.exit_code()


if __name__ == "__main__":
    sys.exit(main())
