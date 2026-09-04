#!/usr/bin/env python3
"""File one finding against the ledger: validate its shape, then dedupe it.

One record per real defect. A surface re-audited in a later round must not
re-file what is already recorded, so this script never inserts blindly: it hands
the candidate to ``ledger.add_finding``, whose identity is
``(surface, title, sorted paths)`` and which returns the EXISTING id on a hit.

Usage::

    python3 finding_entry.py [--db PATH] [--roe-json PATH] --json-file FINDING.json

One input shape. The RFC has the auditor "emit one structured finding file per
candidate", and that file is what this reads. An inline-flag spelling shipped
beside it for a while with no consumer -- SKILL.md never invoked it -- and the
two modes' mutual exclusion produced its own defect (a `--round-id` that was
neither honoured nor refused), so the second spelling is gone.


Exit codes::

    0  filed -- a new finding, and ``created`` is true
    3  duplicate -- nothing written, and the EXISTING id is still printed
    2  invalid input -- nothing written

stdout is one JSON object, ``{"finding_id": N, "created": true|false}``, on the
success and the duplicate path alike. A duplicate prints the id because the
caller's next step (dispatch a verifier, cite it in a report) needs the handle,
and making it parse stderr for it would be the same information behind a worse
contract.

**Why a malformed finding fails HERE and not later.** ``report_schema`` in the
rules of engagement names the fields a finding must carry, and the verifier pass
is the whole product -- so every requirement below exists because the record is
useless to a verifier without it:

- ``severity`` must be a level ``severity_scale`` names. A grade invented
  outside the scale cannot be adjudicated, and severity is what drives the fixer
  gate. When no ``severity_scale`` rule can be read, that is a REFUSAL (exit 2),
  not a pass: an unvalidatable grade is exactly what the scale exists to stop.
- ``poc`` must be present and must be a shape ``verify_finding.py`` can run.
  The check is that script's OWN parser, loaded as a sibling rather than a second
  list of prefixes here -- write-time validation and the verifier then cannot
  drift apart, so adding a third proof shape cannot leave this script rejecting
  what the verifier runs.
- At least one entry in ``paths``, because the paths are part of the dedupe identity and
  of after-the-fact scope checking.

Reads and writes one SQLite file through ``ledger.py``. No network, no
subprocess.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
from collections.abc import Buffer
from pathlib import Path
from typing import Any, Sequence

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_DUPLICATE = 3

JSON_FIELDS = ("surface", "severity", "title", "paths", "poc", "round_id")
JSON_REQUIRED = ("surface", "severity", "title", "paths", "poc")

SEVERITY_FIELD = "severity_scale"


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path: str, data: Buffer, *, _mode: int = 0o666) -> None:
        return None


def load_sibling(filename: str, module_name: str) -> Any:
    """Load a script beside this one without cwd, sys.path or bytecode effects.

    Mirrors ``prepare-pr/scripts/pr_status.py``: a skill's scripts are synced out
    of the package tree and run as bare files, so a sibling is a file next to
    this one rather than an importable module, and importing it the ordinary way
    would drop a ``__pycache__`` entry into the checked-out tree.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    loader = _NoBytecodeSourceLoader(module_name, path)
    spec = importlib.util.spec_from_loader(module_name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot import {filename}: {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# scope_check is the reader of record for the rules of engagement -- rows first,
# export only when there are no rows. Reusing it means the severity scale is
# resolved by exactly the same precedence as a scope verdict, instead of by a
# second implementation that could disagree with it.
_scope_check = load_sibling("scope_check.py", "_security_conductor_scope_check")
# verify_finding owns the proof shapes, because it is what runs them.
_verify_finding = load_sibling("verify_finding.py", "_security_conductor_verify_finding")
# Both siblings load the ledger lazily, so this shares ONE instance with them
# rather than executing ledger.py again in the same invocation.
_ledger = _scope_check.ledger()


def severity_label(rule_value: str) -> str:
    """The level named by a ``severity_scale`` value.

    A scale row reads ``"High: privilege escalation, ..."`` -- a level and its
    definition in one string, because the definition is what an adjudicator
    grades against. The comparable part is the label before the first colon.
    """
    head = rule_value.split(":", 1)[0]
    return head.strip().lower()


def known_severities(rules: Sequence[Any]) -> list[str]:
    """Every level the scale names, in the order the rules gave them."""
    labels: list[str] = []
    for rule in rules:
        if rule.field != SEVERITY_FIELD:
            continue
        label = severity_label(rule.value)
        if label and label not in labels:
            labels.append(label)
    return labels


def has_auditor_verdict(conn: Any, finding_id: int) -> bool:
    """Whether this finding already carries the auditor's own claim.

    Asked of the ``verdicts`` rows rather than of ``findings.auditor_verdict``,
    because that column is a fold of these rows and this is the thing being
    folded -- reading the summary to decide whether to write the detail would
    invert the direction the ledger maintains it in.
    """
    row = conn.execute(
        "SELECT 1 FROM verdicts WHERE finding_id = ? AND role = 'auditor' LIMIT 1",
        (finding_id,),
    ).fetchone()
    return row is not None


def validate_poc(poc: str) -> str | None:
    """``None`` when the verifier can run this PoC, else why it cannot."""
    if not poc.strip():
        return "poc must not be blank; a finding with no proof cannot be verified"
    if _verify_finding.poc_argv(poc) is None:
        shapes = ", ".join(_verify_finding.POC_PREFIXES)
        return f"poc must be a shape verify_finding.py can run ({shapes}<...>); got {poc!r}"
    return None


class Candidate:
    """A finding as asked for, before the ledger has seen it."""

    def __init__(
        self,
        *,
        surface: str,
        severity: str,
        title: str,
        paths: Sequence[str],
        poc: str,
        round_id: str | None,
    ) -> None:
        self.surface = surface.strip()
        self.severity = severity.strip()
        self.title = title.strip()
        self.paths = [item for item in (p.strip() for p in paths) if item]
        self.poc = poc.strip()
        self.round_id = (round_id or "").strip() or None

    def problems(self, severities: Sequence[str]) -> list[str]:
        """Every reason this candidate is not a filable finding."""
        found: list[str] = []
        for name, value in (
            ("surface", self.surface),
            ("title", self.title),
            ("severity", self.severity),
        ):
            if not value:
                found.append(f"{name} must not be blank")
        if not self.paths:
            found.append("at least one path is required; paths are part of a finding's identity")
        poc_problem = validate_poc(self.poc)
        if poc_problem is not None:
            found.append(poc_problem)
        if self.severity:
            if not severities:
                found.append(
                    "the rules of engagement name no severity_scale, so a severity cannot be"
                    " validated; add the scale as a rule before filing findings"
                )
            elif self.severity.lower() not in severities:
                found.append(
                    f"severity {self.severity!r} is not in the severity_scale"
                    f" ({', '.join(severities)})"
                )
        return found


def candidate_from_json(path: Path) -> Candidate:
    """Read a candidate from a JSON file. Raises ``ValueError`` when malformed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    unknown = sorted(set(payload) - set(JSON_FIELDS))
    if unknown:
        # Refused rather than ignored: a typo'd key is silently dropped data,
        # and the field it meant to set is one report_schema asked for.
        raise ValueError(f"{path}: unknown field(s) {', '.join(unknown)}")
    missing = [name for name in JSON_REQUIRED if payload.get(name) in (None, "", [])]
    if missing:
        raise ValueError(f"{path}: missing required field(s) {', '.join(missing)}")
    paths = payload["paths"]
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise ValueError(f"{path}: 'paths' must be a string or a list of strings")
    for name in ("surface", "severity", "title", "poc"):
        if not isinstance(payload[name], str):
            raise ValueError(f"{path}: {name!r} must be a string")
    round_id = payload.get("round_id")
    if round_id is not None and not isinstance(round_id, str):
        raise ValueError(f"{path}: 'round_id' must be a string")
    return Candidate(
        surface=payload["surface"],
        severity=payload["severity"],
        title=payload["title"],
        paths=paths,
        poc=payload["poc"],
        round_id=round_id,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="File one finding against the ledger (deduped)")
    parser.add_argument("--db", default=None, help="ledger path (default: data home)")
    parser.add_argument(
        "--roe-json",
        default=None,
        help="rules-of-engagement export to fall back to (default: beside the skill)",
    )
    parser.add_argument("--json-file", required=True, help="the finding to file, as a JSON file")
    return parser


def _refuse(reasons: Sequence[str]) -> int:
    for reason in reasons:
        print(reason, file=sys.stderr)
    return EXIT_INVALID


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        candidate = candidate_from_json(Path(args.json_file))
    except ValueError as exc:
        return _refuse([str(exc)])

    db_path = Path(args.db) if args.db else _ledger.default_db_path()
    roe_json = Path(args.roe_json) if args.roe_json else _scope_check.default_roe_json()
    rules, failure = _scope_check.load_rules(db_path, roe_json, _ledger)
    if failure is not None:
        # No readable rules means no readable scale, and an unvalidatable
        # severity is the one thing the scale exists to prevent.
        return _refuse(failure.reasons)

    problems = candidate.problems(known_severities(rules))
    if problems:
        return _refuse(problems)

    conn = _ledger.connect(db_path)
    try:
        _ledger.init_schema(conn)
        finding_id, created = _ledger.add_finding(
            conn,
            surface=candidate.surface,
            title=candidate.title,
            severity=candidate.severity,
            paths=candidate.paths,
            poc=candidate.poc,
            round_id=candidate.round_id,
        )
        # Filing IS the auditor's claim that the defect is real, and
        # ``findings.auditor_verdict`` is a materialised view of the verdict rows,
        # so a finding with no row leaves that column NULL and makes an auditor's
        # claim indistinguishable from an absent one. The retrospective reads the
        # disagreement between this row and the verifier's as the round's
        # false-positive signal, which the procedure obliges the conductor to
        # report.
        #
        # Written on the ABSENCE of the row rather than on `created`, which makes
        # filing idempotent and self-repairing. `add_finding` commits on its own,
        # so a crash between the insert and this write is possible and would
        # otherwise be permanent: the retry returns the existing id as a duplicate
        # and, keyed on `created`, would skip the repair forever. Keyed on the
        # row, the retry completes the record. A finding that already carries an
        # auditor verdict is left exactly as it is, so a re-report never stacks a
        # second claim or overwrites a human's.
        # The absence check and the insert run under ONE write lock. Two
        # parallel filings of the same defect both saw "no auditor row" and both
        # appended one; BEGIN IMMEDIATE makes the second wait for the first's
        # commit and then observe the row. record_verdict's own `with conn:`
        # commits this transaction.
        conn.execute("BEGIN IMMEDIATE")
        if has_auditor_verdict(conn, finding_id):
            conn.execute("COMMIT")
        else:
            _ledger.record_verdict(
                conn,
                finding_id=finding_id,
                role="auditor",
                verdict="confirmed",
                reason="filed by the auditor as a real defect",
            )
    finally:
        conn.close()

    print(json.dumps({"finding_id": finding_id, "created": created}, sort_keys=True))
    if not created:
        print(
            f"finding {finding_id} already records this defect"
            f" ({candidate.surface} / {candidate.title}); nothing was written",
            file=sys.stderr,
        )
        return EXIT_DUPLICATE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
