"""No GNU-only shell form may reach a macOS host without a probe in front of it.

macOS ships the BSD userland, so a handful of GNU spellings fail there while they
pass on every Linux runner: ``date -d``, ``grep -P``, ``sed -i`` with no suffix
attached, ``stat -c``, a bare ``sha256sum -c`` reading stdin, ``find -printf`` and
``base64 -w``. Two kinds of file put such a form on a macOS host:

* a tracked shell script outside ``.github/``. The installers, the dev scripts and
  ``packaging/`` run on a Mac for real.
* a test that runs bash. Workflow scripts may use GNU forms, since Actions runs
  them on Linux, but a test that extracts a ``run:`` block (or a
  ``.github/scripts/*.sh``, or a one-liner of its own) and executes it does so on
  the macOS Platform Tests too. Each nightly that went red this way was fixed the
  same way: the test probes the host and puts a shim or a skip in front of the
  GNU form (``_has_gnu_date``, ``_pcre_grep_path``, ``_gnu_sed_path``,
  ``_host_has_gnu_sha256sum``, a ``sed -i.bak`` rewrite).

The sweep reads text only, so it runs on every platform. For a test it counts the
workflow steps the file names as a string literal, the ``.github/scripts`` files it
names, and its own non-comment text, and asks for the rule's probe or shim token
in the same file. A reviewed exception goes in ``EXEMPT`` with its reason; an entry
that matches nothing fails, so the table cannot outlive its cause.

Left out on purpose, because the BSD tools on the macOS runners accept them:
``readlink -f`` (macOS 12.3+), ``xargs -r``, ``sha256sum FILE`` and
``sha256sum -c -``, and ``timeout``, which the scripts already guard with
``command -v``. ``mktemp -d`` ignoring ``TMPDIR`` is a BSD behaviour, not a flag,
so a text rule cannot see it; the macOS lane's path list covers the installer
scripts instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import source_corpus
import yaml

ROOT = source_corpus.repo_root()
SELF = Path(__file__).resolve()

# One worker builds ``all_hits``; spread over two, each walks the whole tree.
pytestmark = pytest.mark.xdist_group(name="tree_scan_gnu_only_shell_usage")

#: The GNU-only forms, matched against one shell line.
RULES: dict[str, re.Pattern[str]] = {
    "date-d": re.compile(r"\bdate(?:\s+-u)?\s+(?:-d|--date)\b"),
    "grep-P": re.compile(r"\bgrep\s+(?:-[A-Za-z]*P[A-Za-z]*\b|--perl-regexp)"),
    # `sed -i.bak` (suffix attached) runs on both seds; `sed -i ''` is the BSD form.
    "sed-i": re.compile(r"\bsed\s+-i(?:\s+(?!\s|['\"]{2})|$)"),
    "stat-c": re.compile(r"\bstat\s+(?:-c|--format)\b"),
    # BSD sha256sum checks a named file (`-c -` works); a bare `-c` on stdin fails.
    "sha256sum-c": re.compile(r"\bsha256sum\s+(?:-c|--check)(?=\s*(?:$|[;&|)'\"]))"),
    "find-printf": re.compile(r"\bfind\b[^\n|;]*\s-printf\b"),
    "base64-w": re.compile(r"\bbase64\s+-w(?![A-Za-z])"),
}

#: The same forms written as an argv list, the way a test calls subprocess.
ARGV: dict[str, re.Pattern[str]] = {
    "date-d": re.compile(r"""["']date["']\s*,\s*(?:["']-u["']\s*,\s*)?["'](?:-d|--date)["']"""),
    "grep-P": re.compile(r"""["']grep["']\s*,\s*["']-[A-Za-z]*P"""),
    "sed-i": re.compile(r"""["']sed["']\s*,\s*["']-i["']"""),
    "stat-c": re.compile(r"""["']stat["']\s*,\s*["'](?:-c|--format)["']"""),
    "sha256sum-c": re.compile(r"""["']sha256sum["']\s*,\s*["'](?:-c|--check)["']"""),
}

#: What a test carries when it has put a probe or shim in front of a rule's form.
SHIM: dict[str, re.Pattern[str]] = {
    "date-d": re.compile(r"gnu_date|date_stub", re.I),
    "grep-P": re.compile(r"pcre", re.I),
    "sed-i": re.compile(r"gnu_sed|-i\.bak", re.I),
    "stat-c": re.compile(r"bsd_stat|gnu_stat", re.I),
    "sha256sum-c": re.compile(r"gnu_sha256sum|sha256sum_check", re.I),
    "find-printf": re.compile(r"gnu_find", re.I),
    "base64-w": re.compile(r"gnu_base64", re.I),
}

#: (repo-relative path, rule) -> why the match does not reach a macOS host as GNU.
EXEMPT: dict[tuple[str, str], str] = {
    ("test/test_ai_review_base_read_fallback.py", "sed-i"): (
        "each Site's `cut` ends the step body before its `sed -i` line"
    ),
    ("test/test_ai_review_workflows_gpt.py", "sed-i"): (
        "runs only the discovery pass's failure-classification slice, after the `sed -i`"
    ),
    ("test/test_ci_nonroot_boundary.py", "stat-c"): (
        "the `stat -c` line is expected workflow text; the test never executes it"
    ),
    ("test/test_cloud_ec2.py", "sha256sum-c"): (
        "asserts the generated install text contains `sha256sum -c`; never executes it"
    ),
    ("test/test_macos_platform_tests_gate.py", "stat-c"): (
        "the `stat -c` line is expected workflow text; the test never executes it"
    ),
}


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def rules_in(text: str) -> set[str]:
    """The rules any non-comment line of *text* matches."""
    lines = _code_lines(text)
    return {rule for rule, rx in RULES.items() if any(rx.search(line) for line in lines)}


def _rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def _workflow_steps() -> dict[str, list[tuple[str, set[str]]]]:
    """workflow file name -> [(step name, rules its `run:` matches)], GNU steps only."""
    out: dict[str, list[tuple[str, set[str]]]] = {}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                name, run = step.get("name"), step.get("run")
                if isinstance(name, str) and isinstance(run, str):
                    rules = rules_in(run)
                    if rules:
                        out.setdefault(path.name, []).append((name, rules))
    return out


def _runner_scripts() -> dict[str, set[str]]:
    scripts = sorted((ROOT / ".github" / "scripts").glob("*.sh"))
    found = {p.name: rules_in(p.read_text(encoding="utf-8")) for p in scripts}
    return {name: rules for name, rules in found.items() if rules}


# shortcut: a test that reaches bash only through a helper (`_bash()`) is not seen;
# counting those flags many tests that run a slice of a step before its GNU line.
# Upgrade to line-level extraction if a nightly breaks through that gap.
_RUNS_BASH = re.compile(r"""["']bash["']|which\(\s*["']bash["']""")


def unprobed_in_test(
    text: str,
    steps: dict[str, list[tuple[str, set[str]]]],
    scripts: dict[str, set[str]],
) -> dict[str, set[str]]:
    """rule -> where the test reaches it, for each rule the test has no shim for."""
    needed: dict[str, set[str]] = {}
    if "subprocess" in text and _RUNS_BASH.search(text):
        for workflow, named in steps.items():
            if workflow not in text:
                continue
            for step, rules in named:
                if f'"{step}"' in text or f"'{step}'" in text:
                    for rule in rules:
                        needed.setdefault(rule, set()).add(f"{workflow}: {step}")
        for script, rules in scripts.items():
            if script in text:
                for rule in rules:
                    needed.setdefault(rule, set()).add(script)
        for rule in rules_in(text):
            needed.setdefault(rule, set()).add("its own text")
    for rule, rx in ARGV.items():
        if rx.search(text):
            needed.setdefault(rule, set()).add("an argv list")
    return {rule: where for rule, where in needed.items() if not SHIM[rule].search(text)}


def _shell_script_hits() -> dict[tuple[str, str], str]:
    hits: dict[tuple[str, str], str] = {}
    for path in source_corpus.repo_files_named(".sh"):
        rel = _rel(path)
        if rel.startswith(".github/"):
            continue
        for rule in rules_in(path.read_text(encoding="utf-8", errors="replace")):
            hits[(rel, rule)] = "a shell script that runs on macOS"
    return hits


def _test_hits() -> dict[tuple[str, str], str]:
    steps, scripts = _workflow_steps(), _runner_scripts()
    hits: dict[tuple[str, str], str] = {}
    for path in source_corpus.repo_files_named(".py"):
        rel = _rel(path)
        if not rel.startswith("test/") or path.resolve() == SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for rule, where in unprobed_in_test(text, steps, scripts).items():
            hits[(rel, rule)] = ", ".join(sorted(where))
    return hits


@pytest.fixture(scope="module")
def all_hits() -> dict[tuple[str, str], str]:
    with source_corpus.untraced():
        return {**_shell_script_hits(), **_test_hits()}


def test_no_gnu_only_form_reaches_a_macos_host_unprobed(all_hits) -> None:
    offenders = [
        f"{path} [{rule}] via {where}"
        for (path, rule), where in sorted(all_hits.items())
        if (path, rule) not in EXEMPT
    ]
    assert not offenders, (
        "GNU-only shell form on a path that runs on macOS. Use the portable form, or "
        "probe the host and shim/skip as the existing tests do (see this module's "
        "docstring), or add a reviewed EXEMPT entry with its reason:\n" + "\n".join(offenders)
    )


def test_every_exemption_still_matches(all_hits) -> None:
    stale = sorted(f"{path} [{rule}]" for path, rule in EXEMPT if (path, rule) not in all_hits)
    assert not stale, "EXEMPT entries that match nothing; remove them:\n" + "\n".join(stale)


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ('END="$(date -d "$X" +%s)"', "date-d"),
        ("date -u --date=@0", "date-d"),
        ("grep -oP '(?<=a)b' f", "grep-P"),
        ('sed -i "s/a/b/" f', "sed-i"),
        ("stat -c %a f", "stat-c"),
        ('curl -o p u && echo "$h  p" | sha256sum -c && install p', "sha256sum-c"),
        ("find . -type f -printf '%p\\n'", "find-printf"),
        ("base64 -w0 f", "base64-w"),
    ],
    ids=[
        "date-d",
        "date-long",
        "grep-P",
        "sed-i",
        "stat-c",
        "sha256sum-c",
        "find-printf",
        "base64-w",
    ],
)
def test_each_rule_matches_its_gnu_form(line: str, rule: str) -> None:
    assert rules_in(line) == {rule}


@pytest.mark.parametrize(
    "line",
    [
        "date -u +%Y-%m-%d",
        "grep -E 'a|b' f",
        'sed -i.bak "s/a/b/" f',
        "sed -i '' 's/a/b/' f",
        "sed -i  '' 's/a/b/' f",
        "stat -f %Lp f",
        'echo "$h  $f" | sha256sum -c -',
        'got="$(sha256sum "$f" | cut -d" " -f1)"',
        "find . -type f -print",
        "base64 < f",
        "# date -d is GNU only",
    ],
    ids=lambda line: re.sub(r"\W+", "-", line).strip("-"),
)
def test_portable_forms_are_not_flagged(line: str) -> None:
    assert rules_in(line) == set()


def test_a_test_that_runs_a_named_gnu_step_needs_its_probe() -> None:
    steps = {"w.yml": [("Deliver report", {"date-d"})]}
    runs = 'import subprocess\nWF = "w.yml"\nstep = steps["Deliver report"]\nsubprocess.run(["bash", "-c", step])\n'
    assert unprobed_in_test(runs, steps, {}) == {"date-d": {"w.yml: Deliver report"}}
    probed = runs + "if not _has_gnu_date(): pass\n"
    assert unprobed_in_test(probed, steps, {}) == {}
    reads_only = 'WF = "w.yml"\nstep = steps["Deliver report"]\n'
    assert unprobed_in_test(reads_only, steps, {}) == {}
