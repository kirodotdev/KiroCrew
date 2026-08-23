#!/usr/bin/env python3
"""check_subprocess_encoding.py -- no text-mode subprocess without a pinned encoding.

## The failure class

``subprocess.run(cmd, text=True)`` (or ``universal_newlines=True``) with no
``encoding=`` decodes the child's output with ``locale.getpreferredencoding``:
UTF-8 on POSIX, the legacy ANSI code page on Windows. Any non-ASCII byte the
child prints -- a path, a commit message, a translated error -- comes back as
mojibake, or raises ``UnicodeDecodeError`` under strict decoding. Issue #3219
was this class reaching users through the dashboard's file diffs; #3669 fixed
the confirmed sites. This gate (#5249) keeps the class from growing back.

## What counts as a violation

A call to a subprocess-spawning function (``run``, ``Popen``, ``check_output``,
``check_call``, ``call`` -- as attribute or bare name -- plus this repository's
kwargs-forwarding wrappers ``run_limited`` and ``popen_limited``) that requests
text mode without pinning the decode. Text mode is requested by a ``text=`` or
``universal_newlines=`` keyword that is not a literal falsy, or by a ``**{...}``
dict-literal splat carrying such a key. The decode is pinned by an ``encoding=``
keyword whose value is not the literal ``None`` (``encoding=None`` is byte-for-
byte the locale fallback this gate exists to stop), or by a ``**UTF8_TEXT``
splat (the shared mapping from ``kiro_crew.subprocess_utf8``, which carries the
encoding).

Matching is BY NAME, deliberately: resolving imports would miss the wrappers
and monkeypatched aliases this repository actually uses, and an aliased
``from subprocess import run as spawn`` is an evasion this gate does not try to
outrun -- the marker below is the sanctioned escape. The cost is that an
unrelated ``client.run(x, text=True)`` is flagged; the fix there is one marker
or a real ``encoding=``, and no such call exists in the tree today.

The check is AST-based so a call formatted across multiple lines is judged as
one call, which a regex cannot do reliably. A file that does not parse is a
hard ERROR, never "clean": under a shrink-only ratchet a parse failure that
reads as zero violations would invite a prune that deletes the file's real
entry (the same fail-loud rule the black gate pins for "black exited 1 with no
findings").

## The opt-out marker

Some children genuinely write in the console/locale encoding (``systeminfo``,
user shells); pinning UTF-8 there would trade one mojibake for another. Such a
site opts out with an inline COMMENT on any line of the call:

    subprocess.run(cmd, text=True)  # subprocess-encoding: locale

Only a real comment token counts -- the phrase inside a string literal on the
call's lines does not exempt it. The marker is an audit trail, not an escape
hatch: it asserts the author chose locale decoding on purpose.

## The ratchet

The repository predates this gate, so existing violations are recorded in
``.github/subprocess-encoding-baseline.txt`` as ``<count> <path>`` lines. The
rules mirror ``check_black_formatting.py`` (same problem: a large pre-existing
violation set that must only shrink):

* a file NOT in the baseline must be clean;
* a baselined file may not grow its count;
* in a file this change touches, a violation sitting on an ADDED line is a new
  offender even when the count is level -- otherwise fixing one old call while
  adding one new one would slip through the count unchanged;
* a baselined file whose count has shrunk (or that is clean or gone) must be
  pruned so the list only shrinks -- run ``--update-baseline``, which only ever
  lowers counts and deletes lines, never adds or raises one.

Like the black gate, the "new offender" verdict covers only the files this
change touches (CI evaluates a merge ref, so an unscoped gate would redden a PR
for files the base branch merged after the baseline was recorded). The
count-shrink prune demand is likewise scoped to this change's files: a stale
count caused by someone else's merged cleanup is their prune to record, and
reddening an unrelated PR for it would make this gate's colour depend on other
people's hygiene.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import re
import subprocess
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / ".github" / "subprocess-encoding-baseline.txt"
# docs/ (prose snippets, not shipped code) and semgrep-tests/ (deliberately
# defective probe sources) are excluded on purpose.
DEFAULT_TARGETS = (
    "src",
    "scripts",
    "test",
    "tests",
    "packages",
    "docker",
    "packaging",
    "conftest.py",
    "xdist_budget.py",
    "setup.py",
)

SPAWN_FUNCS = frozenset({"run", "Popen", "check_output", "check_call", "call"})
WRAPPER_FUNCS = frozenset({"run_limited", "popen_limited"})
SPLAT_NAME = "UTF8_TEXT"
MARKER = "subprocess-encoding: locale"
TEXT_KEYS = ("text", "universal_newlines")

HEADER = """\
# Text-mode subprocess calls without an explicit encoding, as `<count> <path>`.
# Each one decodes with the locale's code page -- mojibake on Windows (#5249).
# The gate requires every OTHER file to be clean and none of these counts to
# grow, so this list can only shrink.
#
# Do NOT add or raise a line to make a red gate green: a new offender needs
# `encoding="utf-8"` / `**UTF8_TEXT` (see src/kiro_crew/subprocess_utf8.py), or
# the `# subprocess-encoding: locale` marker if locale decoding is deliberate.
# The refresh command below only lowers counts and deletes lines.
#
# Refresh (after fixing something listed here):
#   python3 scripts/check_subprocess_encoding.py --update-baseline
"""

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _load_changed_paths():
    """Reuse the black gate's change-scope resolver instead of duplicating it.

    Both gates need the identical answer to "which files does THIS change
    touch", including the merge-ref subtleties documented there.
    """
    script = ROOT / "scripts" / "check_black_formatting.py"
    spec = importlib.util.spec_from_file_location("check_black_formatting", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._changed_paths


def _added_lines(scope_label: str) -> dict[str, set[int]] | None:
    """Repo-relative path -> line numbers this change ADDED, or None.

    Uses the diff endpoints named by the scope resolver's label, so the added
    set and the changed-file set always describe the same diff. An unknown
    label (or a failing git) degrades to None -- the added-line rule is then
    skipped rather than guessed, and the count rules still apply.
    """
    if scope_label == "merge HEAD^1..HEAD":
        args = ["diff", "--unified=0", "HEAD^1", "HEAD"]
    elif scope_label == "merge parents":
        args = ["diff", "--unified=0", "HEAD^1", "HEAD^2"]
    elif scope_label.endswith("...HEAD"):
        args = ["diff", "--unified=0", scope_label]
    else:
        return None
    proc = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return None
    added: dict[str, set[int]] = {}
    current: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+++ "):
            current = None  # /dev/null or unusual prefix
        elif current is not None:
            match = _HUNK_RE.match(line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                added.setdefault(current, set()).update(range(start, start + count))
    return added


def _callee_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _splat_kind(node: ast.Call) -> str:
    """'utf8' for **UTF8_TEXT; 'text' for a dict-literal splat that requests
    text mode without a real encoding pin (missing or literal-None); '' else."""
    for kw in node.keywords:
        if kw.arg is not None:
            continue
        value = kw.value
        if isinstance(value, ast.Name) and value.id == SPLAT_NAME:
            return "utf8"
        if isinstance(value, ast.Attribute) and value.attr == SPLAT_NAME:
            return "utf8"
        if isinstance(value, ast.Dict):
            pairs = {
                k.value: v
                for k, v in zip(value.keys, value.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if not (set(pairs) & set(TEXT_KEYS)):
                continue
            enc = pairs.get("encoding")
            # A missing encoding key and a literal-None one are both the
            # locale fallback; only a real value pins the decode.
            if enc is None or (isinstance(enc, ast.Constant) and enc.value is None):
                return "text"
    return ""


def _marker_lines(source: str) -> set[int]:
    """Line numbers whose COMMENT token carries the opt-out marker.

    tokenize (not a substring scan) so the marker phrase inside a string
    literal cannot exempt a call.
    """
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and MARKER in tok.string:
                lines.add(tok.start[0])
    except tokenize.TokenizeError:
        pass  # the AST parse of the same source decides parseability
    return lines


def _violations_in_source(source: str) -> list[int]:
    """Line numbers of unpinned text-mode subprocess calls in one file.

    Raises SyntaxError for an unparseable file: the caller turns that into a
    hard error because "could not parse" must never read as "clean".
    """
    tree = ast.parse(source)
    markers = _marker_lines(source)
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name not in SPAWN_FUNCS and name not in WRAPPER_FUNCS:
            continue
        splat = _splat_kind(node)
        if splat == "utf8":
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        text_kw = keywords.get("text", keywords.get("universal_newlines"))
        text_mode = splat == "text" or (
            text_kw is not None and not (isinstance(text_kw, ast.Constant) and not text_kw.value)
        )
        if not text_mode:
            continue
        encoding = keywords.get("encoding")
        if encoding is not None and not (
            isinstance(encoding, ast.Constant) and encoding.value is None
        ):
            continue  # a real pin; encoding=None is the locale fallback
        end = node.end_lineno or node.lineno
        if markers & set(range(node.lineno, end + 1)):
            continue
        found.append(node.lineno)
    return found


def _scan(targets: tuple[str, ...]) -> dict[str, list[int]]:
    """Map of repo-relative path -> violation line numbers, files with any."""
    results: dict[str, list[int]] = {}
    for name in targets:
        target = ROOT / name
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = sorted(target.rglob("*.py"))
        else:
            continue
        for path in files:
            if path.suffix != ".py":
                continue
            rel = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                lines = _violations_in_source(source)
            except SyntaxError as exc:
                raise SystemExit(
                    f"{rel} does not parse ({exc.msg}, line {exc.lineno}); refusing "
                    "to read a parse failure as zero violations -- under a "
                    "shrink-only ratchet that would invite a prune that deletes "
                    "the file's real baseline entry"
                )
            if lines:
                results[rel] = lines
    return results


def _read_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise SystemExit(
            f"baseline {path} is missing; restore it from git rather than "
            "regenerating it, since a regenerated baseline would silently absorb "
            "every offender added since it was recorded"
        )
    entries: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        count_str, _, rel = line.partition(" ")
        if not count_str.isdigit() or not rel:
            raise SystemExit(f"malformed baseline line: {line!r}")
        if rel in entries:
            raise SystemExit(
                f"duplicate baseline entry for {rel}; a later duplicate would "
                "silently override the recorded ceiling"
            )
        entries[rel] = int(count_str)
    return entries


def _write_baseline(path: Path, entries: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{count} {rel}\n" for rel, count in sorted(entries.items()))
    path.write_text(HEADER + body, encoding="utf-8")


def _shrunken_baseline(baseline: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    """The refresh result: counts only ever lowered, clean/gone entries dropped."""
    survivors: dict[str, int] = {}
    for rel, recorded in baseline.items():
        now = current.get(rel, 0)
        if now > 0:
            survivors[rel] = min(recorded, now)
    return survivors


def _verdicts(
    violations: dict[str, list[int]],
    baseline: dict[str, int],
    changed: set[str] | None,
    added: dict[str, set[int]] | None,
) -> tuple[list[str], list[str], dict[str, list[int]], list[str]]:
    """(new_offenders, grown, added_line_offenders, shrunk) under the ratchet.

    ``changed`` None means scope was undeterminable: judge the whole tree.
    ``added`` None means added-line info was unavailable: skip only that rule.
    """
    current = {rel: len(lines) for rel, lines in violations.items()}

    def in_scope(rel: str) -> bool:
        return changed is None or rel in changed

    new_offenders: list[str] = []
    grown: list[str] = []
    added_line_offenders: dict[str, list[int]] = {}
    shrunk: list[str] = []
    for rel, count in sorted(current.items()):
        recorded = baseline.get(rel)
        if recorded is None:
            if in_scope(rel):
                new_offenders.append(rel)
        elif in_scope(rel):
            if count > recorded:
                grown.append(rel)
            elif added is not None:
                on_added = sorted(set(violations[rel]) & added.get(rel, set()))
                if on_added:
                    added_line_offenders[rel] = on_added
    for rel, recorded in sorted(baseline.items()):
        if current.get(rel, 0) < recorded and in_scope(rel):
            shrunk.append(rel)
    return new_offenders, grown, added_line_offenders, shrunk


def _fix_hint(path: str) -> str:
    return (
        f"::error file={path}::text-mode subprocess call without encoding= "
        "(decodes with the Windows ANSI code page). Pin it with "
        'encoding="utf-8" / **UTF8_TEXT (src/kiro_crew/subprocess_utf8.py), '
        f"or mark deliberate locale decoding with `# {MARKER}`."
    )


def run_gate(baseline_path: Path, update: bool) -> int:
    violations = _scan(DEFAULT_TARGETS)
    current = {rel: len(lines) for rel, lines in violations.items()}
    baseline = _read_baseline(baseline_path)

    if update:
        survivors = _shrunken_baseline(baseline, current)
        pruned = len(baseline) - len(survivors)
        lowered = sum(1 for rel in survivors if survivors[rel] < baseline[rel])
        _write_baseline(baseline_path, survivors)
        print(f"pruned {pruned} entr(y/ies), lowered {lowered}; {len(survivors)} remain")
        return 0

    changed, scope_label = _load_changed_paths()()
    print(f"subprocess-encoding gate scope: {scope_label}", end="")
    print("" if changed is None else f" ({len(changed)} changed file(s))")
    added = _added_lines(scope_label) if changed is not None else None

    new_offenders, grown, added_line_offenders, shrunk = _verdicts(
        violations, baseline, changed, added
    )

    for rel in new_offenders:
        print(_fix_hint(rel))
        for line in violations[rel]:
            print(f"  {rel}:{line}")
    for rel in grown:
        print(
            f"::error file={rel}::unpinned text-mode subprocess calls grew from "
            f"{baseline[rel]} to {current[rel]}. New calls must pin "
            f'encoding="utf-8" / **UTF8_TEXT or carry `# {MARKER}`.'
        )
        for line in violations[rel]:
            print(f"  {rel}:{line}")
    for rel, lines in added_line_offenders.items():
        print(
            f"::error file={rel}::this change ADDS unpinned text-mode subprocess "
            "call(s) (the baseline grandfathers only pre-existing lines). Pin "
            f'them with encoding="utf-8" / **UTF8_TEXT or `# {MARKER}`.'
        )
        for line in lines:
            print(f"  {rel}:{line}")
    if shrunk:
        print(
            f"::error::{len(shrunk)} baselined file(s) now have fewer unpinned "
            "calls. Record the progress so the baseline keeps shrinking: "
            "python3 scripts/check_subprocess_encoding.py --update-baseline"
        )
        for rel in shrunk:
            print(f"  {rel}: {baseline[rel]} -> {current.get(rel, 0)}")

    if new_offenders or grown or added_line_offenders or shrunk:
        print(
            f"\nsubprocess-encoding gate FAILED: {len(new_offenders)} new "
            f"offender(s), {len(grown)} grown count(s), "
            f"{len(added_line_offenders)} file(s) with new calls on added lines, "
            f"{len(shrunk)} entr(y/ies) to prune."
        )
        return 1

    total = sum(baseline.values())
    print(
        "subprocess-encoding gate passed: nothing in scope decodes with the "
        f"locale code page outside the baseline ({total} known call(s) in "
        f"{len(baseline)} file(s) still listed)."
    )
    return 0


def _self_test() -> int:
    """Plant one probe per rule family; a broken rule fails here, not in prod."""
    flagged_probes = {
        "plain text=True": "import subprocess\nsubprocess.run(['git', 'st'], text=True)\n",
        "universal_newlines": (
            "import subprocess\n"
            "subprocess.check_output(['git', 'st'], universal_newlines=True)\n"
        ),
        "multi-line call": (
            "import subprocess\n"
            "subprocess.Popen(\n"
            "    ['git', 'st'],\n"
            "    stdout=subprocess.PIPE,\n"
            "    text=True,\n"
            ")\n"
        ),
        "wrapper run_limited": (
            "from kiro_crew.sandbox import run_limited\n" "run_limited(['git', 'st'], text=True)\n"
        ),
        "non-literal text value": (
            "import subprocess\n" "def f(mode):\n" "    subprocess.run(['git', 'st'], text=mode)\n"
        ),
        "encoding=None is the locale fallback": (
            "import subprocess\n" "subprocess.run(['git', 'st'], text=True, encoding=None)\n"
        ),
        "dict-literal splat carrying text": (
            "import subprocess\n" "subprocess.run(['git', 'st'], **{'text': True})\n"
        ),
        "dict-literal splat with encoding None": (
            "import subprocess\n"
            "subprocess.run(['git', 'st'], **{'text': True, 'encoding': None})\n"
        ),
        "marker inside a string is not a comment": (
            "import subprocess\n" f"subprocess.run(['echo', '{MARKER}'], text=True)\n"
        ),
    }
    clean_probes = {
        "explicit encoding": (
            "import subprocess\n"
            "subprocess.run(['git', 'st'], text=True, encoding='utf-8', errors='replace')\n"
        ),
        "UTF8_TEXT splat": (
            "import subprocess\n"
            "from kiro_crew.subprocess_utf8 import UTF8_TEXT\n"
            "subprocess.run(['git', 'st'], **UTF8_TEXT)\n"
        ),
        "attribute UTF8_TEXT splat": (
            "import subprocess\n"
            "from kiro_crew import subprocess_utf8\n"
            "subprocess.run(['git', 'st'], **subprocess_utf8.UTF8_TEXT)\n"
        ),
        "opt-out marker": (
            "import subprocess\n" f"subprocess.run(['ps'], text=True)  # {MARKER}\n"
        ),
        "marker on multi-line kwarg": (
            "import subprocess\n"
            "subprocess.run(\n"
            "    ['ps'],\n"
            f"    text=True,  # {MARKER}\n"
            ")\n"
        ),
        "dict-literal splat with encoding": (
            "import subprocess\n" "subprocess.run(['git'], **{'text': True, 'encoding': 'utf-8'})\n"
        ),
        "text=False": "import subprocess\nsubprocess.run(['git', 'st'], text=False)\n",
        "binary mode": "import subprocess\nsubprocess.run(['git', 'st'], capture_output=True)\n",
        "unrelated callee": "def note(**kw): ...\nnote(text=True)\n",
    }
    failures: list[str] = []
    for label, source in flagged_probes.items():
        if not _violations_in_source(source):
            failures.append(f"NOT flagged but should be: {label}")
    for label, source in clean_probes.items():
        if _violations_in_source(source):
            failures.append(f"flagged but should be clean: {label}")
    for failure in failures:
        print(f"::error::self-test: {failure}")
    if failures:
        return 1
    print(
        f"self-test passed: {len(flagged_probes)} flagged probes, "
        f"{len(clean_probes)} clean probes."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="lower counts / prune entries that improved; never adds or raises",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="run the rule-family self-test instead of the gate",
    )
    args = parser.parse_args(argv)
    if args.test:
        return _self_test()
    return run_gate(args.baseline, args.update_baseline)


if __name__ == "__main__":
    raise SystemExit(main())
