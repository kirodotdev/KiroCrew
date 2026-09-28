#!/usr/bin/env python3
"""Pipeline spec check — the startup predicate for the spec's closed-value fields.

One invocation answers "is this spec runnable?" for every field whose value set
is CLOSED, before the run arms anything.

WHY ONLY THE CLOSED FIELDS. A closed field is the one shape where a typo is
SILENT. A misspelled repo, branch pattern or threshold fails at first use, loudly
and immediately. A misspelled enum matches no branch: the mode the operator asked
for never engages, the run proceeds under whatever the surrounding procedure does
by default, and the report reads as a normal run. ``verifier.repro_gate`` is the
field where that bites hardest — ``pod_required`` is a hard admission gate whose
whole purpose is to make unit-only evidence inadmissible, and
``"pod-required"`` (hyphen) is neither value, so the gate is off while the spec
says it is on and the campaign's own metric still counts the run as pod-verified.
That is precisely the failure the gate exists to prevent, one typo lower.

So the check FAILS CLOSED: any value outside the declared set refuses the run
rather than picking a default. Choosing a default here would be the same silent
degradation with an extra step.

An ABSENT field is not an error — the spec documents a default for every field,
and omission is how a pipeline asks for it. An explicit ``null`` is not omission
and is refused: it is a value, and it is not one of the declared ones.

Usage (the startup procedure supplies ``<skill-dir>``):

POSIX::

    "$KIROCREW_RUNTIME_PYTHON" -I -B "<skill-dir>/scripts/spec_check.py" --spec <pipeline-spec.json>

PowerShell::

    & $env:KIROCREW_RUNTIME_PYTHON -I -B "<skill-dir>/scripts/spec_check.py" --spec <pipeline-spec.json>

``-I`` removes the current directory, script directory, user site, and inherited
Python environment from startup before the sensitive-path gate imports. ``-B``
keeps the packaged desktop from writing bytecode even though isolated mode
ignores its ``PYTHONDONTWRITEBYTECODE`` environment setting.

``KIROCREW_RUNTIME_PYTHON`` is overwritten by the Kiro Crew ACP parent with the
absolute interpreter already running Kiro Crew. That path works in venv, POSIX
desktop (``bin/python3.12``), and Windows desktop (bundle-root ``python.exe``)
layouts without requiring a system ``python`` name. A manual invocation under a
foreign interpreter can still relocate through :func:`bundled_python` below.

Exit codes:

    0   the spec's closed-value fields are usable
    2   malformed spec — stderr names the field, the offending value, and the
        accepted set, or, where the document is unusable before any field is
        reachable, what makes it unusable. Do not start the run. Also returned
        when the spec path is REFUSED by the sensitive-path read gate, or when
        that gate cannot be reached from any interpreter: an unenforceable
        precondition is a refusal here, never a plain read.

Reads one file; writes nothing. Spawns at most one subprocess, and only ever
itself, under the interpreter that carries the read gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

try:
    # The spec path comes from the operator's seed message, so it is
    # caller-influenced: a symlink could point it at a credential store the
    # sandbox leaves readable. ``safe_read_file`` canonicalizes the path,
    # re-checks the RESOLVED target against ``is_sensitive_path``, and opens it
    # ``O_NOFOLLOW``, so the gate holds through a link and through a TOCTOU swap.
    from kiro_crew.hooks import safe_read_file
except Exception:  # pragma: no cover - exercised when the package is not importable
    # The normal startup procedure uses Kiro Crew's injected runtime, but the
    # copied skill script can still be run manually under a foreign interpreter,
    # where ``kiro_crew`` is not guaranteed to be importable. There is no safe
    # degradation for a READ GATE: falling back to ``read_text`` would reopen
    # exactly the bypass this import exists to close, and it would do so silently,
    # in the case that is hardest to notice. So this is not the fallback —
    # ``main`` re-execs under an interpreter that HAS the gate, and refuses only
    # when there is none.
    safe_read_file = None  # type: ignore[assignment]

#: Set on the child when this script re-execs itself, so an interpreter that
#: also lacks the gate refuses instead of spawning a third.
_REEXEC_ENV = "KIROCREW_SPEC_CHECK_REEXEC"

#: Spec field (dotted path) -> the values it accepts.
#:
#: This table is the seam: a future closed field is registered here and inherits
#: the fail-closed behavior and the error shape, rather than growing a second
#: validator somewhere else. Fields with open value sets belong nowhere near it —
#: listing one would turn a legitimate value into a refused startup.
_ENUMS: dict[str, tuple[str, ...]] = {
    "verifier.repro_gate": ("best_effort", "pod_required"),
}


def bundled_python() -> str | None:
    """Path to an interpreter that can import the read gate, or ``None``.

    The ACP parent supplies the authoritative answer in
    ``KIROCREW_RUNTIME_PYTHON``. It is the exact ``sys.executable`` already
    running Kiro Crew, injected after the child-env scrub and after agent
    overrides, so a desktop install never depends on a Python name on ``PATH``.

    The launcher-derived candidates keep manual and older invocations usable:
    a venv keeps Python beside ``kirocrew``; the POSIX desktop bundle keeps
    ``python3.12`` beside it; and the Windows desktop shim lives in ``bin`` with
    ``python.exe`` one directory above. ``realpath`` matters for the common
    user-bin symlink into any of those layouts.
    """
    injected = os.environ.get("KIROCREW_RUNTIME_PYTHON", "").strip()
    if injected and os.path.isfile(injected):
        return injected

    launcher = shutil.which("kirocrew")
    if not launcher:
        return None
    script_dir = os.path.dirname(os.path.realpath(launcher))
    if os.name == "nt":
        candidates = (
            os.path.join(script_dir, "python.exe"),
            os.path.join(os.path.dirname(script_dir), "python.exe"),
        )
    else:
        candidates = (
            os.path.join(script_dir, "python"),
            os.path.join(script_dir, "python3.12"),
        )
    return next((candidate for candidate in candidates if os.path.isfile(candidate)), None)


def _refuse_unenforceable(reason: str) -> int:
    """Refuse a spec whose read gate cannot be reached from any interpreter."""
    print(
        "malformed spec: cannot enforce the sensitive-path read gate "
        f"(kiro_crew.hooks is not importable and {reason}); refusing to read the spec",
        file=sys.stderr,
    )
    return 2


def _reexec_with_the_gate(spec: str) -> int:
    """Re-run this check under the interpreter that has the read gate.

    A relocation, not a second opinion: the child answers the same question with
    the gate in force, so its exit code and its message are this invocation's.
    """
    if os.environ.get(_REEXEC_ENV):
        return _refuse_unenforceable(f"the re-exec under {sys.executable} already happened")
    interpreter = bundled_python()
    if interpreter is None:
        return _refuse_unenforceable(
            "KIROCREW_RUNTIME_PYTHON names no usable file and no supported "
            "interpreter was found from the 'kirocrew' launcher"
        )
    env = {**os.environ, _REEXEC_ENV: "1"}
    return subprocess.call(
        [interpreter, "-I", "-B", os.path.abspath(__file__), "--spec", spec], env=env
    )


def _expected(values: tuple[str, ...]) -> str:
    """Render an accepted set the way the error message needs it."""
    quoted = [repr(value) for value in values]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} or {quoted[1]}"
    return f"{', '.join(quoted[:-1])}, or {quoted[-1]}"


def spec_error(spec: dict[str, Any]) -> str | None:
    """Return the first problem in ``spec``, or ``None`` when it is runnable.

    One message, not a list: a spec with two bad enums is refused on the first,
    and the operator re-runs the check after fixing it. A partial spec is never
    "runnable except for" — there is no degraded mode to report.
    """
    for path, allowed in _ENUMS.items():
        parent: Any = spec
        keys = path.split(".")
        for key in keys[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                parent = None
                break
            parent = parent[key]
            if not isinstance(parent, dict):
                # A block spelled as a scalar or a list hides every field under
                # it, so the enum below would read as absent and default.
                return f"{key}: expected a JSON object"
        leaf = keys[-1]
        if not isinstance(parent, dict) or leaf not in parent:
            # Absent: the spec's documented default applies.
            continue
        value = parent[leaf]
        if not isinstance(value, str) or value not in allowed:
            return f"{path} {value!r}: expected {_expected(allowed)}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a pipeline spec before a run.")
    parser.add_argument("--spec", required=True, help="path to the pipeline spec JSON")
    args = parser.parse_args(argv)

    if safe_read_file is None:
        return _reexec_with_the_gate(args.spec)
    try:
        spec = json.loads(safe_read_file(args.spec))
        if not isinstance(spec, dict):
            raise ValueError("spec must be a JSON object")
    except PermissionError as exc:
        # The gate's own refusal, kept distinct from a malformed file: the spec
        # may be perfectly well-formed and still not be ours to read.
        print(f"refused spec: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RecursionError) as exc:
        # ``RecursionError`` is listed because a deeply nested document exhausts
        # the scanner's stack instead of failing to parse, and it subclasses
        # ``RuntimeError`` rather than ``ValueError`` — so without it the one
        # malformed shape that is not a parse error would leave through the
        # traceback as exit 1. Every unusable spec exits 2, which is the code
        # documented above and the only one a caller reads as "malformed".
        print(f"malformed spec: {exc}", file=sys.stderr)
        return 2
    problem = spec_error(spec)
    if problem is not None:
        print(f"malformed spec: {problem}", file=sys.stderr)
        return 2
    print(f"OK {args.spec}: {len(_ENUMS)} closed-value field(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
