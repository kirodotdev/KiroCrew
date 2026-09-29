"""Ratchet: the number of places that read a runtime's pid only goes down.

A `kiro-cli` runtime is one operating-system process owned by the pool that
spawned it, and a session holds a lease on it rather than its pid. Several
sessions may hold leases on one runtime, so a pid identifies a process and
answers nothing about which session is using it. "One session, one process" is
the layout a pool cap of 1 produces, not a property the code may rely on.

Every site this test counts is therefore a place where a shared process is
attributed to one of its tenants: a signal, an RSS reading, a liveness test, a
resource name or an identity lookup that is wrong for every other session on the
same process. At a pool cap of 1 each reading happens to coincide with the right
answer, which is what lets such a site look correct in review and in manual
testing.

The count is asserted EXACTLY, not as a ceiling. A ceiling accumulates slack: a
change that removes ten readers buys room for ten new ones, and the regression
this test exists to refuse then passes. So a change that removes a site records
the new number in the same commit, and the failure message says which number to
write.

Two families are counted, because a reader has two spellings. An attribute or
call is matched as text, with comments and string literals blanked first so prose
naming a reader is not a site. A reflective read names the reader in a string
literal -- ``getattr(client, "process_instance", "")`` -- and is resolved through
``ast`` instead, which sees a receiver that is itself a call
(``getattr(getattr(p, "client", None), "_pid", None)``) and a call the formatter
wrapped across lines. A text match cannot span either shape, and each shape is a
way to keep a reading while lowering the number.

Two readings are legitimate: the module that owns a runtime, and the one function
that ends it. Those mark their own lines rather than raising the total, and the
number of marked sites is pinned too -- otherwise marking an ordinary reader would
quietly buy a slot, and the exact-count message would even congratulate the author
for removing one.

A ratchet is only worth its number if it cannot pass vacuously, so the total is
not asserted alone: every needle must find a site, files known to hold sites must
still hold them, and the exemption is exercised end to end against a fixture tree
so deleting its branch fails a test. A scanner that silently stops matching
reports zero offenders, which reads exactly like success.
"""

from __future__ import annotations

import ast
import functools
import io
import re
import tokenize
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

#: One worker owns the whole-tree scan, so the cached walk is paid once per run
#: rather than once per worker the suite's loadgroup distribution picks.
pytestmark = pytest.mark.xdist_group("tree_scan_pid_reader_ratchet")

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: Each needle names one way code gets from a session to a process, spelled as an
#: attribute or a call. Fragments are concatenated so a scan of this tree cannot
#: report this file as an offender.
_NEEDLES: dict[str, str] = {
    # The session layer's pid accessor, and its own definitions down the chain.
    "get" + "_pid()": r"\bget" + r"_pid\(",
    # A provider or runtime handing out the pid it holds.
    "." + "_pid": r"\._" + r"pid\b",
    # The (runtime_pid, gateway_socket_path) pair a provider exposes about its
    # backing process.
    "runtime" + "_info()": r"\bruntime" + r"_info\(",
    # The process-identity string, which is a process fact, not a session fact.
    "process" + "_instance": r"\bprocess" + r"_instance\b",
    # Liveness asked of the process rather than of the session's lease.
    "is_process" + "_alive": r"\bis_process" + r"_alive\b",
    # The pid-keyed sidecar that maps a process back to a single session key.
    "sidecar": (
        r"\bread_session"
        + r"_pid_txt\b|\bverify_session"
        + r"_pid\b|\bsession_pid"
        + r"_mapping_path\b"
    ),
}

#: The name under which the reflective family is reported.
_REFLECTIVE = "getattr-string"

#: Builtins that take an attribute name as a string.
_REFLECTIVE_FUNCS = frozenset({"get" + "attr", "has" + "attr", "set" + "attr"})

#: Reader names worth catching in that string position. Same readers as above; the
#: sidecar functions are absent because they are module functions, not attributes.
_REFLECTIVE_NAMES = frozenset(
    {
        "_" + "pid",
        "get" + "_pid",
        "runtime" + "_info",
        "process" + "_instance",
        "is_process" + "_alive",
    }
)

#: Test support and in-tree test modules are not product surface.
_EXCLUDED_PARTS = ("kiro_crew/testing/", "container_tests/")

#: A site the pool itself owns carries this marker as a comment on one of the
#: lines the site spans, and is counted separately instead of against the total.
#: Two readings are legitimate: the pool that spawns a runtime owns its pid, and
#: the one function that ends the process needs it. Nothing else does. Neither of
#: those lives in ``src`` on this branch, so the marker appears nowhere here; the
#: change that lands the ownership module marks its own sites rather than raising
#: the total, which the exact-count rule would otherwise refuse. Spelled like the
#: tree's other in-line exemptions (``brand-ok``, ``testpaths-ok``).
_OWNER_MARKER = "pid-owner" + "-ok"

#: The pid-reader count this branch measures. The assertion is EQUALITY, not an
#: upper bound: an upper bound accumulates slack, so a change that removes ten
#: readers silently buys room for ten new ones. A change that removes a site
#: lowers this number in the same commit. A change that wants a new reader takes
#: the lease handle instead, or marks the site as the pool's own.
_BASELINE_SITES = 148

#: Owner-marked sites in ``src``. Pinned for the same reason the total is: marking
#: an ordinary reader would otherwise move a site out of the total for free.
_BASELINE_OWNER_MARKED = 2

#: Files that hold sites at the baseline and are not yet migrated. Each must still
#: be found, so a scanner that matches nothing cannot pass as a clean tree. Drop a
#: name here in the same change that removes its last site.
_KNOWN_SITE_FILES = (
    "acp/client.py",
    "acp/runtime.py",
    "acp/session_provider.py",
    "providers/base.py",
    "session.py",
    "session_pid_sig.py",
    # Reached ONLY by the reflective family, so one of these failing is the signal
    # that the string-literal form stopped being counted.
    "process_identity.py",
    "workflows/agent_pool.py",
)


class Scan(NamedTuple):
    """One reading of one tree."""

    per_needle: Counter[str]
    per_file: Counter[str]
    witnesses: tuple[tuple[str, tuple[str, ...]], ...]
    owner_marked: int


def _blanked(source: str) -> str:
    """``source`` with every comment and string literal blanked in place.

    Spaces replace the token's bytes and newlines are kept, so offsets and line
    numbers still line up with the file on disk and a needle cannot match across a
    blanked region. Only the text family reads this; the reflective family needs
    the string literals and goes through ``ast``, which ignores prose by
    construction.
    """
    lines = source.splitlines(keepends=True)
    offsets, running = [0], 0
    for line in lines:
        running += len(line)
        offsets.append(running)
    buffer = list(source)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        # An unparseable module is scanned raw rather than skipped: a file the
        # tokenizer refuses is the one place a site could hide for free.
        return source
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        start = offsets[start_row - 1] + start_col
        end = min(offsets[end_row - 1] + end_col, len(buffer))
        for index in range(start, end):
            if buffer[index] != "\n":
                buffer[index] = " "
    return "".join(buffer)


def _reflective_spans(source: str) -> list[tuple[int, int]]:
    """(first line, last line) of every reflective reader call in ``source``.

    A call qualifies when its function is one of ``_REFLECTIVE_FUNCS`` and its
    second positional argument is a string constant naming a reader. Resolving
    this through the syntax tree rather than the text is what makes a nested
    receiver and a formatter-wrapped call visible, and it also means a docstring
    quoting the pattern is not a site.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id not in _REFLECTIVE_FUNCS:
            continue
        if len(node.args) < 2:
            continue
        name = node.args[1]
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            continue
        if name.value not in _REFLECTIVE_NAMES:
            continue
        spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _in_scope(path: Path) -> bool:
    text = path.as_posix()
    if any(part in text for part in _EXCLUDED_PARTS):
        return False
    return not path.name.startswith("test_")


def _scan_tree(root: Path) -> Scan:
    """Read ``root`` once. Uncached, so a test may point it at a fixture tree."""
    per_needle: Counter[str] = Counter()
    per_file: Counter[str] = Counter()
    witnesses: dict[str, list[str]] = {name: [] for name in (*_NEEDLES, _REFLECTIVE)}
    owner_marked = 0
    for path in sorted(root.rglob("*.py")):
        if not _in_scope(path):
            continue
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.splitlines()
        code_lines = _blanked(source).splitlines()

        def record(name: str, count: int, exempt: bool) -> None:
            nonlocal owner_marked
            if exempt:
                owner_marked += count
                return
            per_needle[name] += count
            per_file[relative] += count
            if relative not in witnesses[name]:
                witnesses[name].append(relative)

        for raw, code in zip(raw_lines, code_lines):
            marked = _OWNER_MARKER in raw
            for name, pattern in _NEEDLES.items():
                found = len(re.findall(pattern, code))
                if found:
                    record(name, found, marked)
        for first, last in _reflective_spans(source):
            span = raw_lines[first - 1 : last]
            record(_REFLECTIVE, 1, any(_OWNER_MARKER in line for line in span))
    frozen = tuple((name, tuple(files)) for name, files in witnesses.items())
    return Scan(per_needle, per_file, frozen, owner_marked)


@functools.lru_cache(maxsize=1)
def _scan() -> Scan:
    """The product tree's reading. Cached: every assertion reads the same one."""
    return _scan_tree(_SRC)


def _report(scan: Scan) -> str:
    total = sum(scan.per_needle.values())
    by_needle = "\n".join(f"  {c:4d}  {n}" for n, c in sorted(scan.per_needle.items()))
    by_file = "\n".join(
        f"  {c:4d}  {n}" for n, c in sorted(scan.per_file.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return (
        f"pid-reader sites: {total} in {len(scan.per_file)} files "
        f"(recorded {_BASELINE_SITES}, owner-marked and counted apart: {scan.owner_marked})\n"
        f"by needle:\n{by_needle}\n"
        f"by file:\n{by_file}"
    )


def test_every_needle_finds_a_site() -> None:
    """A needle that matches nothing makes the ratchet pass for the wrong reason."""
    dead = sorted(name for name, files in _scan().witnesses if not files)
    assert not dead, (
        "these pid-reader needles match nothing in src/kiro_crew: "
        + ", ".join(dead)
        + ". Either the surface is genuinely gone -- then remove the needle and "
        "lower the baseline in the same change -- or the spelling drifted and the "
        "ratchet is measuring an empty set."
    )


def test_known_reader_files_are_still_counted() -> None:
    """The scanner reaches the modules the migration is about."""
    per_file = _scan().per_file
    missing = [name for name in _KNOWN_SITE_FILES if per_file.get(name, 0) == 0]
    assert not missing, (
        "no pid-reader site found in: "
        + ", ".join(missing)
        + ". If a module is genuinely migrated, drop it from _KNOWN_SITE_FILES in "
        "the same change; otherwise the scan is not reaching the tree."
    )


def test_reflective_reads_survive_nesting_and_wrapping(tmp_path: Path) -> None:
    """The shapes a text match cannot see are counted.

    Each of these keeps the reading while moving the number: a receiver that is
    itself a call, and a call the formatter splits across lines.
    """
    name = "_" + "pid"
    module = tmp_path / "shapes.py"
    module.write_text(
        "def plain(p):\n"
        f'    return getattr(p, "{name}", None)\n'
        "\n\n"
        "def nested(p):\n"
        f'    return getattr(getattr(p, "client", None), "{name}", None)\n'
        "\n\n"
        "def wrapped(p):\n"
        "    return getattr(\n"
        "        p,\n"
        f'        "{name}",\n'
        "        None,\n"
        "    )\n"
        "\n\n"
        "def prose(p):\n"
        f'    """Not a site: getattr(p, "{name}", None) inside a docstring."""\n'
        "    return None\n",
        encoding="utf-8",
    )
    scan = _scan_tree(tmp_path)
    assert scan.per_needle[_REFLECTIVE] == 3, (
        "expected the plain, nested and wrapped calls and NOT the docstring: "
        f"{dict(scan.per_needle)}"
    )
    assert scan.owner_marked == 0


def test_owner_marker_exempts_a_site_and_is_counted_apart(tmp_path: Path) -> None:
    """The exemption is exercised through the real scan, not a synthetic match.

    Deleting the exemption branch has to fail a test, and a marked site has to
    stay visible as marked -- otherwise marking an ordinary reader would move it
    out of the total for free.
    """
    name = "_" + "pid"
    module = tmp_path / "owned.py"
    module.write_text(
        "def ordinary(p):\n"
        f"    return p.{name}\n"
        "\n\n"
        "def owned(p):\n"
        f"    return p.{name}  # {_OWNER_MARKER}: the pool owns this runtime\n"
        "\n\n"
        "def owned_reflective(p):\n"
        f'    return getattr(p, "{name}", None)  # {_OWNER_MARKER}: same\n',
        encoding="utf-8",
    )
    scan = _scan_tree(tmp_path)
    assert sum(scan.per_needle.values()) == 1, (
        "only the unmarked site counts toward the total: " f"{dict(scan.per_needle)}"
    )
    assert scan.owner_marked == 2, (
        "both marked sites must be reported as marked rather than dropped: " f"{scan.owner_marked}"
    )


def test_owner_marked_sites_match_the_recorded_count() -> None:
    """Marking a reader is a declaration, so the number of marks is pinned too."""
    scan = _scan()
    assert scan.owner_marked == _BASELINE_OWNER_MARKED, (
        f"{scan.owner_marked} owner-marked pid reader(s) in src, recorded "
        f"{_BASELINE_OWNER_MARKED}. The marker says a reading belongs to the module "
        "that owns a runtime or the one function that ends it. Adding one is a "
        "declaration: set _BASELINE_OWNER_MARKED in the same commit, so marking an "
        "ordinary reader cannot quietly move it out of the total.\n" + _report(scan)
    )


def test_pid_reader_sites_match_the_recorded_count_exactly() -> None:
    scan = _scan()
    total = sum(scan.per_needle.values())
    if total > _BASELINE_SITES:
        raise AssertionError(
            "this change ADDS a place that reads a runtime's pid. A runtime is shared, so a "
            "per-session pid reading attributes one process to one of its tenants. Take the "
            "lease handle instead. If the reading really is the pool's own -- the module that "
            f"owns the runtime, or the one function that ends it -- put `# {_OWNER_MARKER}: "
            "<why>` on the line and record the new mark count.\n" + _report(scan)
        )
    if total < _BASELINE_SITES:
        raise AssertionError(
            f"this change removes {_BASELINE_SITES - total} pid reader(s), which is the point -- "
            f"now record it: set _BASELINE_SITES to {total} in this same commit. The number is an "
            "exact count rather than a ceiling, because a ceiling would leave "
            f"{_BASELINE_SITES - total} slot(s) of room for a future reader to fill silently.\n"
            + _report(scan)
        )
