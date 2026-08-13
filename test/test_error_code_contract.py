"""Contract gate: a non-2xx JSON body carries a machine-readable ``code``.

The dashboard renders server prose verbatim into a localized UI --
``setError(res.error)`` (``JobForm.tsx``), ``{operation.error || operation.message}``
(``KiroPrerequisiteGate.tsx``). So an English sentence produced in Python lands
untranslated inside a Chinese page, and no amount of frontend i18n can reach it:
the string never passes through a catalog. The backend is emitting *presentation*
where it should emit *data*.

Four independent authorities require the same fix -- an identifier the client
switches on, with the prose demoted to advisory:

  * **RFC 9457** 3.1.1: consumers "MUST use the ``type`` URI ... as the problem
    type's primary identifier"; 3.1.3 ``title`` is "advisory" and "SHOULD NOT
    change ... except for localization"; 3.1.4 "Consumers SHOULD NOT parse the
    ``detail`` member."
  * **Google AIP-193**: ``ErrorInfo.reason`` is the identity, and request-specific
    values must live in ``metadata`` -- "critical so that machine actors do not
    need to parse error messages". With the decisive warning: ship prose without
    an id and ``Status.message`` becomes "implicitly part of the API contract, so
    must not be updated". The English freezes forever.
  * **Azure**: ``x-ms-error-code`` values "are part of your API contract ... and
    cannot change"; every other field is "*not* considered part of your service's
    API contract."
  * **Stripe**: display permission is *scoped* -- "For card errors, these messages
    can be shown to your users" -- and ``param`` is a machine pointer precisely so
    the client can supply its own text.

## What this gate does, and what it deliberately does not

It is a **ratchet, not a sweep**. There are 1484 error responses with a literal
``status >= 400`` and exactly one carries a ``code`` today. Converting them is
Track B work that has to move **with its consumers** -- a ``code`` field alone
changes nothing while the frontend still renders ``res.error`` verbatim -- so this
gate freezes the debt at its current count and fails only when a *new*
non-compliant handler lands. Existing sites are inventoried in
``error-code-baseline.json``, which doubles as the Track B worklist: drive files
to zero, one PR per file or per directory.

The counts are per file rather than one global number on purpose. A single total
lets a new bad handler hide behind an unrelated deletion, and a per-file map is
the only form that tells the converter where to start.

## Three buckets, because two of them are not statically decidable

``missing_code``   literal ``status >= 400``, dict-literal body, no ``code`` key.
                   This is the debt, and the only bucket a static check can call
                   with certainty.
``opaque_body``    literal ``status >= 400``, body is a variable, a call, or a
                   dict containing a ``**spread``. The ``code`` may well be in
                   there; this scan cannot see it, so it is counted separately and
                   never reported as a violation.
``dynamic_status`` ``status=`` is an expression (``status=code``,
                   ``status=500 if ... else 400``). Whether the response is even
                   an error depends on runtime state, so no static rule applies.

Ratcheting all three matters: without a cap on ``opaque_body`` and
``dynamic_status`` the gate is trivially defeated by hoisting the body into a
local or computing the status, and the escape would look like ordinary
refactoring in review.

## Known false negatives

Named honestly, because every filter above buys a blind spot:

  * **Indirect sinks.** ``_err("...", 400)`` and ``_err500(exc)`` wrappers
    (``handlers/artifacts.py``, ``handlers/agents.py``) are ordinary calls -- the
    ``json_response`` inside the helper is a single site, so N call sites count as
    one. Converting a helper fixes all of its callers at once, which is why this
    is an acceptable blind spot rather than a hole to plug.
  * **Non-``json_response`` sinks.** ``web.Response(text=json.dumps(...))``,
    ``HTTPBadRequest(reason=...)``, raised aiohttp exceptions, and every
    catalog-less target (Slack, Discord, CLI, cron) are out of scope here.
  * **Callee matching is by name.** Any method named ``json_response`` matches,
    aliased or not. That direction is safe -- it over-collects, never under --
    but it means the count is not a count of aiohttp calls specifically.
  * **``code`` presence, not correctness.** A ``"code"`` key whose value is a
    formatted sentence satisfies the ratchet. The value-shape assertion below
    catches the literal cases; an f-string value is invisible to it.

Ground truth for backend coverage is a server-side pseudolocale, exactly as
``en-XA`` is for the frontend -- not this scan. See
``knowledge/i18n-architecture-audit-and-plan.md`` A.3.

## Regenerating

    python test/test_error_code_contract.py --update

Only ever *after* the count legitimately drops. Raising a baseline number to make
CI pass is the one move this file exists to prevent.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import pathlib
import re
import sys
from collections.abc import Sequence

# Value shape for the identifier. lower_snake rather than AIP-193's UPPER_SNAKE:
# what the authorities above make normative is that the field be a stable
# machine-readable identifier the client switches on, not its casing, and the one
# code shipped today is `"code": "exists"` (handlers/discover.py). Matching the
# shipped value keeps a single convention for the ~1484 conversions ahead; the
# alternative was churning a live API string for cosmetics.
_CODE_VALUE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src" / "kiro_crew"
_BASELINE_PATH = pathlib.Path(
    os.environ.get("KIROCREW_ERROR_CODE_BASELINE", _REPO_ROOT / "error-code-baseline.json")
)

_BUCKETS = ("missing_code", "opaque_body", "dynamic_status")


class _Finding:
    __slots__ = ("path", "lineno", "bucket", "code_value")

    def __init__(self, path: str, lineno: int, bucket: str, code_value: str | None = None) -> None:
        self.path = path
        self.lineno = lineno
        self.bucket = bucket
        self.code_value = code_value

    def __str__(self) -> str:  # pragma: no cover - diagnostic only
        return f"src/kiro_crew/{self.path}:{self.lineno}"


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _status(call: ast.Call) -> tuple[str, int | None]:
    """``('literal', n) | ('dynamic', None) | ('absent', None)``.

    aiohttp's ``json_response`` takes ``status`` keyword-only, so there is no
    positional form to handle.
    """
    for kw in call.keywords:
        if kw.arg != "status":
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
            return "literal", kw.value.value
        return "dynamic", None
    return "absent", None


def _body(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "data":
            return kw.value
    return None


def _dict_code(node: ast.Dict) -> tuple[bool, bool, str | None]:
    """``(is_transparent, has_code, literal_code_value)``.

    A ``**spread`` makes the dict opaque: ``code`` may arrive from the spread and
    this scan cannot follow it.
    """
    has_code = False
    value: str | None = None
    for key, val in zip(node.keys, node.values):
        if key is None:
            return False, False, None
        if isinstance(key, ast.Constant) and key.value == "code":
            has_code = True
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                value = val.value
    return True, has_code, value


def scan(src: pathlib.Path = _SRC) -> list[_Finding]:
    """Every error-response site in ``src``, bucketed. Deterministic order.

    Memoized: six assertions below share one AST pass over ~600 files.
    """
    return list(_scan_uncached(src))


@functools.lru_cache(maxsize=4)
def _scan_uncached(src: pathlib.Path) -> tuple[_Finding, ...]:
    findings: list[_Finding] = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        # Cheap pre-filter before the parse. `ast` can only produce a
        # `json_response` attribute if the identifier appears literally in the
        # source, so a file without the substring cannot contribute a finding.
        # This skips the parse for the large majority of ~600 files, which keeps
        # the suite from monopolising a CI worker -- on Windows the sibling
        # `test_platform_compat` asserts against a PowerShell/WMI call with a 10s
        # timeout whose expiry is swallowed into `""`, so a CPU-hungry test
        # elsewhere in the shard turns that into a failure.
        if "json_response" not in text:
            continue
        rel = path.relative_to(src).as_posix()
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee_name(node) != "json_response":
                continue
            kind, status = _status(node)
            if kind == "absent":
                continue  # defaults to 200
            if kind == "dynamic":
                findings.append(_Finding(rel, node.lineno, "dynamic_status"))
                continue
            if status is None or status < 400:
                continue
            body = _body(node)
            if not isinstance(body, ast.Dict):
                findings.append(_Finding(rel, node.lineno, "opaque_body"))
                continue
            transparent, has_code, value = _dict_code(body)
            if not transparent:
                findings.append(_Finding(rel, node.lineno, "opaque_body"))
            elif has_code:
                findings.append(_Finding(rel, node.lineno, "compliant", value))
            else:
                findings.append(_Finding(rel, node.lineno, "missing_code"))
    return tuple(findings)


def tally(findings: Sequence[_Finding]) -> dict[str, dict[str, int]]:
    per_file: dict[str, dict[str, int]] = {}
    for f in findings:
        if f.bucket not in _BUCKETS:
            continue
        per_file.setdefault(f.path, {})
        per_file[f.path][f.bucket] = per_file[f.path].get(f.bucket, 0) + 1
    return {path: dict(sorted(counts.items())) for path, counts in sorted(per_file.items())}


def build_baseline() -> dict:
    findings = scan()
    files = tally(findings)
    totals = {b: sum(c.get(b, 0) for c in files.values()) for b in _BUCKETS}
    return {
        "_comment": (
            "Error responses without a machine-readable `code`, per file. Generated - "
            "regenerate with `python test/test_error_code_contract.py --update`. This is both "
            "the CI ratchet and the Track B worklist: drive `missing_code` to zero, one PR per "
            "file or per directory, moving each frontend consumer in the same PR. Never raise a "
            "number to make CI pass. See test/test_error_code_contract.py for what each bucket "
            "means and which false negatives it accepts."
        ),
        "_totals": totals,
        "_compliant": sum(1 for f in findings if f.bucket == "compliant"),
        "files": files,
    }


def _load_baseline() -> dict:
    with open(_BASELINE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_baseline_file_is_parseable_and_shaped() -> None:
    baseline = _load_baseline()
    assert set(baseline["_totals"]) == set(_BUCKETS)
    assert isinstance(baseline["files"], dict)
    for path, counts in baseline["files"].items():
        assert not path.startswith("/"), f"baseline paths are relative to src/kiro_crew: {path}"
        assert set(counts) <= set(_BUCKETS), f"unknown bucket in {path}: {sorted(counts)}"
        assert all(isinstance(n, int) and n > 0 for n in counts.values()), path


def test_no_new_error_response_without_a_code() -> None:
    """The ratchet. A new non-compliant site in any file fails the build."""
    baseline = _load_baseline()["files"]
    findings = scan()
    actual = tally(findings)

    regressions: list[str] = []
    for path, counts in actual.items():
        allowed = baseline.get(path, {})
        for bucket, count in counts.items():
            cap = allowed.get(bucket, 0)
            if count <= cap:
                continue
            lines = sorted(f.lineno for f in findings if f.path == path and f.bucket == bucket)
            regressions.append(f"  src/kiro_crew/{path}: {bucket} {cap} -> {count} (lines {lines})")

    assert not regressions, (
        "New error response(s) without a machine-readable `code`.\n"
        + "\n".join(sorted(regressions))
        + '\n\nReturn `{"error": "<English prose>", "code": "<lower_snake_id>"}` instead of '
        "prose alone: the dashboard renders `res.error` verbatim into a localized UI, so an "
        "un-coded sentence is untranslatable by construction. `code` is the contract; `error` is "
        "advisory (RFC 9457 3.1.3). Do NOT regenerate the baseline to silence this."
    )


def test_baseline_is_not_stale() -> None:
    """A count that dropped must be regenerated, or the ratchet loosens silently."""
    baseline = _load_baseline()["files"]
    actual = tally(scan())

    stale: list[str] = []
    for path, counts in baseline.items():
        live = actual.get(path, {})
        for bucket, cap in counts.items():
            count = live.get(bucket, 0)
            if count < cap:
                stale.append(f"  src/kiro_crew/{path}: {bucket} {cap} -> {count}")

    assert not stale, (
        "The baseline is stale - these counts improved but were never re-snapshotted, so the "
        "ratchet is now looser than the code:\n"
        + "\n".join(sorted(stale))
        + "\n\nRun: python test/test_error_code_contract.py --update"
    )


def test_baseline_totals_match_the_per_file_map() -> None:
    baseline = _load_baseline()
    recomputed = {b: sum(c.get(b, 0) for c in baseline["files"].values()) for b in _BUCKETS}
    assert baseline["_totals"] == recomputed, (
        "`_totals` disagrees with `files` - the baseline was hand-edited. "
        "Run: python test/test_error_code_contract.py --update"
    )


def test_every_error_code_is_a_machine_readable_identifier() -> None:
    """``code`` is switched on by clients, so it may not be prose.

    Applies to literal values only; an f-string value is invisible here (see the
    false-negative list in the module docstring).
    """
    bad = [
        f"  {f} -> {f.code_value!r}"
        for f in scan()
        if f.bucket == "compliant"
        and f.code_value is not None
        and not _CODE_VALUE_RE.match(f.code_value)
    ]
    assert not bad, (
        "`code` must be a stable lower_snake identifier, not prose or a localized string "
        "(RFC 9457 3.1.1, AIP-193):\n" + "\n".join(bad)
    )


def test_the_scan_actually_reaches_the_backend() -> None:
    """Guard against the gate silently passing because it scanned nothing."""
    findings = scan()
    assert len(findings) > 1000, f"expected the backend error surface, got {len(findings)} sites"
    assert any(f.bucket == "compliant" for f in findings), "no compliant site found - scan broken?"


if __name__ == "__main__":  # pragma: no cover - developer entry point
    if "--update" in sys.argv:
        payload = build_baseline()
        with open(_BASELINE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {_BASELINE_PATH}")
        print(f"  totals:    {payload['_totals']}")
        print(f"  compliant: {payload['_compliant']}")
        print(f"  files:     {len(payload['files'])}")
    else:
        print(json.dumps(build_baseline()["_totals"], indent=2))
