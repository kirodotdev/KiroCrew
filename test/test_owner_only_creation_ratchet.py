"""Ratchet: creation calls that leave a data-home file or directory to the umask.

``kiro_crew.owner_only_files`` states the policy: every file Kiro Crew creates in
the data home is ``0600`` and every directory ``0700``, set at creation. Four
call shapes break it without looking wrong:

* ``touch-no-mode`` -- ``Path.touch()`` / ``touch(exist_ok=...)`` with no
  ``mode=``: the file is created ``0666 & ~umask``, ``0644`` under ``022``;
* ``lock-open-no-opener`` -- a lock file opened for append or write
  (``open(lock, "a+")``, ``lock.open("a+b")``) with no ``opener=``: same mode;
* ``lock-os-open-0o644`` -- ``os.open`` / ``open_create_or_existing`` of a lock
  file asking for ``0o644`` outright;
* ``mkdir-leaf-mode`` -- ``mkdir(parents=True, mode=...)``: the mode reaches the
  last component only, and every parent the call creates is ``0755``.

The owner-only spellings are ``touch(mode=OWNER_ONLY_FILE_MODE, ...)``,
``open(..., opener=owner_only_opener)`` (or ``owner_only_opener_for``), an explicit
``0o600``, and ``mkdirs_owner_only`` / ``ensure_directory``.

Some sites are deliberately left: a target outside the data home (the kiro-cli
settings lock, a source-tree lock, a tempfile scratch dir), or a directory whose
pre-existing mode must not change. :data:`BASELINE_FILE` holds, per file under
``src/kiro_crew``, the count it had when this gate landed. A count may go down
and never up; a file not listed starts at zero. A new site that is genuinely
outside the data home says so on its line with ``# owner-only-ok: <reason>``.

Lowering the baseline is optional (a lower real count always passes), so a
change that removes a site elsewhere never has to touch this file.
``python test/test_owner_only_creation_ratchet.py --write-baseline`` lowers every
line to its real count and drops files that are gone; it never raises or adds.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "kiro_crew"
BASELINE_FILE = Path(__file__).resolve().parent / "owner_only_creation_baseline.txt"

_HEADER = """\
# Umask-mode creation sites per file under src/kiro_crew when the owner-only
# creation ratchet landed. Read by test/test_owner_only_creation_ratchet.py;
# one "<count> <path under src/kiro_crew/>" per line.
# RATCHET: a count may only go down (delete the line at zero); never raise or add one.
# `python test/test_owner_only_creation_ratchet.py --write-baseline` lowers every
# line to its real count and drops files that are gone; it never raises or adds.
"""

_OK = re.compile(r"#\s*owner-only-ok:\s*(?P<reason>\S.*?)\s*$")
MIN_REASON_CHARS = 10
_APPEND_OR_WRITE = re.compile(r"^[aw]\+?b?$|^[aw]b\+$")
_LOCKISH = re.compile(r"lock", re.IGNORECASE)
_TRIGGERS = (".touch(", "open(", "mkdir(", "open_create_or_existing(")


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _kinds(call: ast.Call, source: str) -> list[str]:
    func = call.func
    found: list[str] = []
    if isinstance(func, ast.Attribute) and func.attr == "touch":
        receiver_is_self = isinstance(func.value, ast.Name) and func.value.id == "self"
        keywords = {kw.arg for kw in call.keywords}
        if not receiver_is_self and not call.args and keywords <= {"exist_ok"}:
            found.append("touch-no-mode")
    if isinstance(func, ast.Attribute) and func.attr == "mkdir":
        if _is_true(_kw(call, "parents")) and _kw(call, "mode") is not None:
            found.append("mkdir-leaf-mode")
    path_node: ast.expr | None = None
    mode_node: ast.expr | None = None
    if isinstance(func, ast.Name) and func.id == "open" and call.args:
        path_node = call.args[0]
        mode_node = call.args[1] if len(call.args) > 1 else _kw(call, "mode")
    elif (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and not (isinstance(func.value, ast.Name) and func.value.id in {"os", "io", "self"})
    ):
        path_node = func.value
        mode_node = call.args[0] if call.args else _kw(call, "mode")
    if path_node is not None and _kw(call, "opener") is None:
        mode = _str(mode_node)
        segment = ast.get_source_segment(source, path_node) or ""
        if mode and _APPEND_OR_WRITE.match(mode) and _LOCKISH.search(segment):
            found.append("lock-open-no-opener")
    is_os_open = (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
        and func.attr == "open"
    )
    is_create = isinstance(func, ast.Attribute) and func.attr == "open_create_or_existing"
    if (is_os_open or is_create) and call.args:
        segment = ast.get_source_segment(source, call.args[0]) or ""
        if _LOCKISH.search(segment) and any(
            isinstance(a, ast.Constant) and a.value == 0o644 for a in call.args[1:]
        ):
            found.append("lock-os-open-0o644")
    return found


def _excused(lines: list[str], call: ast.Call) -> bool:
    end = call.end_lineno or call.lineno
    for number in range(call.lineno, min(end, len(lines)) + 1):
        match = _OK.search(lines[number - 1])
        if match and len(match.group("reason")) >= MIN_REASON_CHARS:
            return True
    return False


def sites_in(source: str) -> list[tuple[int, str]]:
    """``(line, kind)`` for every counted site in one file's source."""
    if not any(t in source for t in _TRIGGERS):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kinds = _kinds(node, source)
            if kinds and not _excused(lines, node):
                out.extend((node.lineno, kind) for kind in kinds)
    return sorted(out)


def _scanned(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "tests" not in p.relative_to(root).parts and p.is_file()
    )


def scan(root: Path = SRC_ROOT) -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for path in _scanned(root):
        try:
            found = sites_in(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
        if found:
            out[path.relative_to(root).as_posix()] = found
    return out


@functools.lru_cache(maxsize=1)
def _tree_scan() -> dict[str, list[tuple[int, str]]]:
    return scan()


def read_baseline(path: Path = BASELINE_FILE) -> dict[str, int]:
    out: dict[str, int] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count, _, rel = line.partition(" ")
        if not count.isdigit() or not rel or rel in out:
            raise ValueError(f"{path.name}:{number}: expected '<count> <path>', got {raw!r}")
        out[rel] = int(count)
    return out


def format_baseline(counts: dict[str, int]) -> str:
    width = max((len(str(n)) for n in counts.values()), default=1)
    body = "".join(f"{n:>{width}} {rel}\n" for rel, n in sorted(counts.items()) if n > 0)
    return _HEADER + body


# ── the gate ─────────────────────────────────────────────────────────────


def test_no_file_gains_a_umask_mode_creation_site() -> None:
    baseline = read_baseline()
    risen = {
        rel: (baseline.get(rel, 0), sites)
        for rel, sites in _tree_scan().items()
        if len(sites) > baseline.get(rel, 0)
    }
    if risen:
        detail = "\n".join(
            f"  src/kiro_crew/{rel}: {len(sites)} > baseline {was}: "
            + ", ".join(f"{line}:{kind}" for line, kind in sites)
            for rel, (was, sites) in sorted(risen.items())
        )
        raise AssertionError(
            "A file gained a creation call that leaves its mode to the umask:\n"
            f"{detail}\n"
            "Use touch(mode=OWNER_ONLY_FILE_MODE, ...), open(..., opener=owner_only_opener"
            "[_for]), 0o600, or mkdirs_owner_only / ensure_directory from "
            "kiro_crew.owner_only_files. A target outside the data home says so with "
            "`# owner-only-ok: <reason>` on its line."
        )


def test_the_baseline_names_only_real_files() -> None:
    missing = [rel for rel in read_baseline() if not (SRC_ROOT / rel).is_file()]
    assert missing == [], f"baseline lines for files that are gone: {missing}"


# ── the scanner itself ───────────────────────────────────────────────────


def _kinds_of(snippet: str) -> list[str]:
    return [kind for _, kind in sites_in(snippet)]


def test_each_umask_mode_shape_is_counted() -> None:
    assert _kinds_of("lock_path.touch(exist_ok=True)\n") == ["touch-no-mode"]
    assert _kinds_of("marker.touch()\n") == ["touch-no-mode"]
    assert _kinds_of('open(lock_path, "a+", encoding="utf-8")\n') == ["lock-open-no-opener"]
    assert _kinds_of('(out / ".restore.lock").open("a+b")\n') == ["lock-open-no-opener"]
    assert _kinds_of('os.open(str(d / "x.lock"), os.O_RDWR, 0o644)\n') == ["lock-os-open-0o644"]
    assert _kinds_of(
        'platform_compat.open_create_or_existing(str(d / "x.lock"), os.O_RDWR, 0o644)\n'
    ) == ["lock-os-open-0o644"]
    assert _kinds_of("root.mkdir(mode=0o700, parents=True, exist_ok=True)\n") == ["mkdir-leaf-mode"]


def test_the_owner_only_spellings_are_not_counted() -> None:
    clean = "\n".join(
        [
            "lock_path.touch(mode=OWNER_ONLY_FILE_MODE, exist_ok=True)",
            'open(lock_path, "a+", opener=owner_only_opener)',
            'open(lock_path, "a+b", opener=owner_only_opener_for(lock_path))',
            'os.open(str(d / "x.lock"), os.O_RDWR, 0o600)',
            "mkdirs_owner_only(root)",
            "root.mkdir(mode=0o700)",
            "root.mkdir(parents=True, exist_ok=True)",
            'open(report_path, "a")',
            'open(lock_path, "r+")',
            "self.touch()",
            "slots.touch(session_key)",
            "marker.touch()  # owner-only-ok: tempfile scratch, outside the data home",
        ]
    )
    assert sites_in(clean + "\n") == []


def test_an_excuse_needs_a_reason() -> None:
    assert _kinds_of("marker.touch()  # owner-only-ok: x\n") == ["touch-no-mode"]


def _write_baseline() -> int:
    counts = Counter({rel: len(sites) for rel, sites in scan().items()})
    if BASELINE_FILE.exists():
        old = read_baseline()
        lowered = {rel: min(n, counts.get(rel, 0)) for rel, n in old.items()}
    else:
        lowered = dict(counts)
    BASELINE_FILE.write_text(format_baseline(lowered), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-baseline"]:
        sys.exit(_write_baseline())
    for rel, sites in sorted(scan().items()):
        print(f"{len(sites):>3} src/kiro_crew/{rel}")
