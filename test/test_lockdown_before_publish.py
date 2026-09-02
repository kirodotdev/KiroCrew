"""A secret-bearing file must be locked down BEFORE it is published.

Applying the owner-only lockdown after the payload is already at its final path
leaves a window in which the file exists under whatever permissions it
inherited. On Windows that is the parent directory's DACL, and POSIX mode bits
are not enforced there at all, so ``atomic_write(mode=0o600)`` does not close
it. Issue #5307 converted the last seven such writers to
``atomic_write(..., restrict_to_owner=True)``, which locks the temp file down
before the first content byte and before the rename.

Nothing prevented a NEW writer from reintroducing the shape. Two layers here:

* ``scripts/check_lockdown_before_publish.py`` is an AST rule over
  ``src/kiro_crew``, exercised below against fixtures for every shape it must
  catch and every correct shape it must not. Validated against real history:
  run against the tree before #5329 it flags 6/6 of #5307's sites; against
  ``main`` after it, 0/6.
* behavioural probes assert the ORDER at the live writers, rather than the
  final mode -- a final-mode assertion passes just as happily when the payload
  was exposed for the whole write window, and on NTFS reports ``0o666``
  regardless of the DACL. The technique (record whether the FINAL path exists
  at the moment lockdown runs) is from PR #5314 by @leonlaiyc, whose
  production change landed via #5329.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from test_live_target import _make_valid_checkout

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = REPO_ROOT / "scripts" / "check_lockdown_before_publish.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("_lockdown_checker", CHECKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_lockdown_checker"] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _functions(source: str) -> set[str]:
    return {fn for _line, fn, _expr in checker.scan_source(source)}


# ─── shapes the rule MUST catch ─────────────────────────────────────────────


VIOLATIONS = {
    "atomic_write then restrict the final path (#5307 tier 2)": """
def write_overlay(target, spec):
    atomic_write(target, spec, mode=0o600)
    platform_compat.restrict_to_owner(target)
""",
    "temp write, publish, then chmod the published path": """
def save(path, payload):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
""",
    "open(file=...) keyword path, then chmod the published path": """
def persist(final, secret):
    with open(file=final, mode="w", encoding="utf-8") as handle:
        handle.write(secret)
    os.chmod(final, 0o600)
""",
    "Path.replace(target=...) keyword publish, then chmod the published path": """
def save(tmp, final, payload):
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target=final)
    os.chmod(final, 0o600)
""",
    "method-form final.chmod(0o600) after content": """
def persist(final, secret):
    final.write_text(secret, encoding="utf-8")
    final.chmod(0o600)
""",
    "method-form final.chmod(mode=0o600) after content": """
def persist(final, secret):
    final.write_text(secret, encoding="utf-8")
    final.chmod(mode=0o600)
""",
    "the two-argument helper spelling still records a lockdown": """
def persist(final, secret):
    final.write_text(secret, encoding="utf-8")
    platform_compat.chmod_safe(final, 0o600)
""",
    "rotate the old secret aside FIRST, then write and lock the freed path": """
def rotate(path, backup, secret):
    path.rename(backup)
    path.write_text(secret)
    os.chmod(path, 0o600)
""",
    "the same rotation in the os.replace spelling": """
def rotate(path, backup, secret):
    os.replace(path, backup)
    path.write_text(secret)
    platform_compat.restrict_to_owner(path)
""",
    "a rename inside a NESTED function does not exempt the outer lockdown": """
def outer(path, final, secret):
    def unrelated():
        path.rename(final)

    path.write_text(secret)
    os.chmod(path, 0o600)
""",
    "a conditional rotation branch does not exempt the lockdown": """
def save(path, secret, rotate):
    if rotate:
        path.rename(path.with_suffix(".bak"))
    path.write_text(secret)
    os.chmod(path, 0o600)
""",
    "shutil.move into place, then chmod the published path": """
def save(tmp, final, secret):
    tmp.write_text(secret)
    shutil.move(tmp, final)
    os.chmod(final, 0o600)
""",
    "os.link into place, then restrict the published path": """
def save(tmp, final, secret):
    tmp.write_bytes(secret)
    os.link(tmp, final)
    platform_compat.restrict_to_owner(final)
""",
    "fchmod_safe on the descriptor AFTER content": """
def save(final, secret):
    fd = os.open(final, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secret)
    platform_compat.fchmod_safe(fd, 0o600)
""",
    "a named module-level owner-only mode is still a lockdown": """
_SECRET_MODE = 0o600


def save(final, secret):
    final.write_text(secret)
    platform_compat.chmod_safe(final, _SECRET_MODE)
""",
    "a named mode built from stat symbols is still a lockdown": """
_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR


def save(final, secret):
    final.write_text(secret)
    os.chmod(final, _OWNER_ONLY)
""",
    "the private _atomic_json_write publishes, then chmod the published path": """
def _read_modify_write(path, denied):
    _atomic_json_write(path, denied)
    chmod_safe(path, 0o600)
""",
    "a single-assignment local alias is the same path": """
def save(secret_path, secret):
    target = secret_path
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(secret)
    os.chmod(secret_path, 0o600)
""",
    "an alias chain of two hops is still the same path": """
def save(secret_path, secret):
    mid = secret_path
    target = mid
    target.write_text(secret)
    os.chmod(secret_path, 0o600)
""",
    "write_bytes then restrict": """
def persist(path, blob):
    path.write_bytes(blob)
    platform_compat.restrict_to_owner(path)
""",
    "open for write then restrict": """
def persist(path, secret):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(secret)
    platform_compat.restrict_to_owner(path)
""",
    "copy2 into place then restrict (#5346 snapshot shape)": """
def restore(src, dst):
    shutil.copy2(str(src), str(dst))
    platform_compat.restrict_to_owner(str(dst))
""",
    "content through the fd, then chmod (pre-#5329 spool shape)": """
def write_spool(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)
""",
    "restrict_to_owner=False must not exempt the write": """
def persist(secret_path, token):
    atomic_write(secret_path, token, restrict_to_owner=False)
    platform_compat.restrict_to_owner(secret_path)
""",
    "the path= keyword form is still a write": """
def persist(secret_path, token):
    atomic_write(path=secret_path, content=token, mode=0o600)
    platform_compat.restrict_to_owner(secret_path)
""",
    "the pathlib open method form is a write": """
def persist(secret_path, secret):
    with secret_path.open("w", encoding="utf-8") as handle:
        handle.write(secret)
    platform_compat.restrict_to_owner(secret_path)
""",
    "os.write through a descriptor on the final path is a write": """
def persist(secret_path, secret):
    fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, secret)
    os.close(fd)
    platform_compat.restrict_to_owner(secret_path)
""",
    "the method-form publish records a write to its destination": """
def save(path, payload):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.rename(path)
    os.chmod(path, 0o600)
""",
    "a symbolic owner-only mode is still a lockdown": """
def write_pod_config(dst_cfg, cfg_data):
    dst_cfg.write_text(json.dumps(cfg_data))
    os.chmod(dst_cfg, stat.S_IRUSR | stat.S_IWUSR)
""",
    "a symbolic 0o700 is still a lockdown": """
def persist(path, blob):
    path.write_bytes(blob)
    os.chmod(path, stat.S_IRWXU)
""",
    "a lockdown addressed by keyword is still a lockdown": """
def persist(secret_path, token):
    secret_path.write_bytes(token)
    platform_compat.restrict_to_owner(path=secret_path)
""",
    "str() wrapper must not hide the match": """
def persist(outfile, blob):
    outfile.write_bytes(blob)
    platform_compat.restrict_to_owner(str(outfile))
""",
}


# ─── shapes the rule MUST NOT catch ─────────────────────────────────────────


CORRECT = {
    "the fix: atomic_write locks the temp before content": """
def write_overlay(target, spec):
    atomic_write(target, spec, restrict_to_owner=True)
""",
    "restrict the temp, then os.replace it into place": """
def save(path, payload):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    platform_compat.restrict_to_owner(tmp)
    os.replace(tmp, path)
""",
    "restrict the temp, then Path.rename it into place (#5317 shape)": """
def snapshot(outfile, stage):
    tmp_tar = outfile.with_suffix(".tar.gz.tmp")
    with tarfile.open(str(tmp_tar), "w:gz") as tar:
        tar.add(str(stage))
    platform_compat.restrict_to_owner(str(tmp_tar))
    tmp_tar.rename(outfile)
""",
    "os.open an EMPTY file, restrict it, then write through the fd": """
def write_secret_file(secret_path, secret):
    fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    platform_compat.restrict_to_owner(secret_path)
    with os.fdopen(fd, "w") as handle:
        handle.write(secret)
""",
    "the fix written in keyword form": """
def persist(secret_path, token):
    atomic_write(path=secret_path, content=token, restrict_to_owner=True)
""",
    "restrict the temp, then os.replace it by keyword": """
def save(path, payload):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    platform_compat.restrict_to_owner(tmp)
    os.replace(src=tmp, dst=path)
""",
    "mkstemp then os.write then lock then link -- the published path is never bare": """
def install_id(path, fresh):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
    os.write(tmp_fd, fresh.encode("utf-8"))
    os.close(tmp_fd)
    platform_compat.restrict_to_owner(tmp_path)
    os.link(tmp_path, str(path))
""",
    "the pathlib open method form in READ mode is not a write": """
def load(path):
    with path.open("r", encoding="utf-8") as handle:
        data = handle.read()
    platform_compat.restrict_to_owner(path)
    return data
""",
    "a module-form open still reads its path from arg 0, restricted then renamed": """
def snapshot(outfile, stage):
    tmp_tar = outfile.with_suffix(".tar.gz.tmp")
    with tarfile.open(str(tmp_tar), "w:gz") as tar:
        tar.add(str(stage))
    platform_compat.restrict_to_owner(str(tmp_tar))
    tmp_tar.rename(outfile)
""",
    "read-side re-assert on a path this function never wrote": """
def load(path):
    data = path.read_bytes()
    platform_compat.restrict_to_owner(path)
    return data
""",
    "a symbolic mode that grants group/other is not a lockdown": """
def install_launcher(launcher, body):
    launcher.write_text(body, encoding="utf-8")
    os.chmod(launcher, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
""",
    "chmod with an executable mode is not a lockdown": """
def install_launcher(launcher, body):
    launcher.write_text(body, encoding="utf-8")
    platform_compat.chmod_safe(launcher, 0o755)
""",
    "a directory mode set before anything is written into it": """
def ensure_dir(directory):
    directory.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(directory, 0o700)
""",
    "restrict the temp, then Path.replace(target=...) it into place": """
def save(tmp, final, payload):
    tmp.write_text(payload, encoding="utf-8")
    platform_compat.restrict_to_owner(tmp)
    tmp.replace(target=final)
""",
    "a method-form chmod that grants group/other is not a lockdown": """
def install(launcher, body):
    launcher.write_text(body, encoding="utf-8")
    launcher.chmod(0o755)
""",
    "open(file=...) in READ mode is not a content write": """
def load(final):
    with open(file=final, mode="r", encoding="utf-8") as handle:
        blob = handle.read()
    os.chmod(final, 0o600)
    return blob
""",
    "chmod_safe the temp after content, then publish it": """
def save(path, payload):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    platform_compat.chmod_safe(tmp, 0o600)
    tmp.replace(path)
""",
    "lock then publish inside a loop, once per item": """
def save_all(files, payload):
    for f in files:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(payload)
        platform_compat.restrict_to_owner(tmp)
        tmp.replace(f)
""",
    "lock the temp, then shutil.move it into place": """
def save(tmp, final, secret):
    tmp.write_text(secret)
    platform_compat.restrict_to_owner(tmp)
    shutil.move(tmp, final)
""",
    "fchmod_safe on the descriptor BEFORE content": """
def save(final, secret):
    fd = os.open(final, os.O_WRONLY | os.O_CREAT, 0o600)
    platform_compat.fchmod_safe(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secret)
""",
    "a named module-level EXECUTABLE mode is not a lockdown": """
_LAUNCHER_MODE = 0o755


def install(launcher, body):
    launcher.write_text(body)
    platform_compat.chmod_safe(launcher, _LAUNCHER_MODE)
""",
    "an unresolvable imported mode leaves the mode unknown": """
def save(final, secret):
    final.write_text(secret)
    os.chmod(final, some_module.WHATEVER)
""",
    "a mode named inside the function body is not resolved": """
def save(final, secret, hardened):
    mode = 0o600 if hardened else 0o644
    final.write_text(secret)
    os.chmod(final, mode)
""",
    "an aliased temp still matches the publish that exempts it": """
def save(path, payload):
    tmp = path.with_suffix(".tmp")
    p = tmp
    p.write_text(payload)
    platform_compat.restrict_to_owner(p)
    os.replace(tmp, path)
""",
    "a REBOUND name is too ambiguous to alias": """
def save(a, b, secret):
    target = a
    target = b
    target.write_text(secret)
    os.chmod(a, 0o600)
""",
    "two unrelated locals are not conflated": """
def save(a, b, secret):
    target = a
    target.write_text(secret)
    os.chmod(b, 0o600)
""",
    "an explicit reasoned suppression": """
def append(path, lines):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(lines)
    os.chmod(path, 0o600)  # lockdown-ok: re-assert on an existing audit log
""",
}


class TestTheRuleCatchesTheDefect:
    @pytest.mark.parametrize("label", sorted(VIOLATIONS))
    def test_a_write_then_restrict_shape_is_reported(self, label: str) -> None:
        found = checker.scan_source(VIOLATIONS[label])
        assert found, f"the rule missed a real violation: {label}"


class TestTheRuleLeavesCorrectCodeAlone:
    @pytest.mark.parametrize("label", sorted(CORRECT))
    def test_a_correct_shape_is_not_reported(self, label: str) -> None:
        found = checker.scan_source(CORRECT[label])
        assert not found, f"false positive on a correct shape: {label} -> {found}"

    def test_the_publish_must_follow_the_lockdown_to_exempt_it(self) -> None:
        """The temp exemption is about ORDER, not about the path appearing somewhere.

        Same two statements, swapped: locking a path the function LATER renames
        away is the fix; locking a path it renamed away EARLIER means the payload
        was written to the freed final path and locked afterwards. Keyed on the
        path alone these were indistinguishable, and the second one passed.
        """
        exempt = """
def save(tmp, final, payload):
    tmp.write_text(payload)
    platform_compat.restrict_to_owner(tmp)
    tmp.rename(final)
"""
        violation = """
def save(path, backup, payload):
    path.rename(backup)
    path.write_text(payload)
    platform_compat.restrict_to_owner(path)
"""
        assert not checker.scan_source(exempt), "the correct idiom stopped being exempt"
        assert checker.scan_source(violation), "an earlier publish still exempts a lockdown"

    def test_a_nested_function_does_not_pool_into_its_parent(self) -> None:
        """Scope, not just order: ``ast.walk`` descends into nested ``def``s.

        A rename in an unrelated nested helper entered the outer function's temp
        set and exempted a real violation there. The nested function is still
        scanned in its own right, so this narrows nothing.
        """
        source = """
def outer(path, final, secret):
    def unrelated():
        path.rename(final)

    path.write_text(secret)
    os.chmod(path, 0o600)
"""
        assert _functions(source) == {"outer"}

    def test_resolving_named_modes_does_not_invent_lockdowns(self) -> None:
        """The constant map must not turn every named mode into a lockdown.

        Resolving ``_SECRET_MODE = 0o600`` is only safe if ``_LAUNCHER_MODE = 0o755``
        still reads as "not owner-only". Same shape, same spelling, opposite
        verdict -- decided by the mode's value, which is the property the rule
        has always used.
        """
        template = """
_MODE = %s


def install(path, body):
    path.write_text(body)
    platform_compat.chmod_safe(path, _MODE)
"""
        assert checker.scan_source(template % "0o600"), "a named owner-only mode was missed"
        assert not checker.scan_source(
            template % "0o755"
        ), "a named group/other-readable mode was treated as a lockdown"

    def test_only_a_bare_local_name_is_aliased(self) -> None:
        """Attributes and subscripts are left alone on purpose.

        ``self._path`` or ``cfg["p"]`` can change between the write and the
        lockdown, so resolving them would be a guess. Pinned because the natural
        way to write the alias map -- textual substitution on any assignment --
        would happily rewrite both.
        """
        source = """
def save(cfg, secret):
    cfg.target = cfg.secret_path
    cfg.target.write_text(secret)
    os.chmod(cfg.secret_path, 0o600)
"""
        assert not checker.scan_source(source), "an attribute alias was resolved"

    def test_a_suppression_without_a_reason_does_not_suppress(self) -> None:
        """`# lockdown-ok:` with nothing after the colon must not silence it."""
        source = """
def persist(path, blob):
    path.write_bytes(blob)
    platform_compat.restrict_to_owner(path)  # lockdown-ok:
"""
        assert checker.scan_source(source), "a reasonless marker suppressed the finding"


class TestTheRuleAttributesToTheRightFunction:
    def test_the_reported_function_is_the_enclosing_one(self) -> None:
        source = """
def innocent(path):
    return path.read_text(encoding="utf-8")


def guilty(path, blob):
    path.write_bytes(blob)
    platform_compat.restrict_to_owner(path)
"""
        assert _functions(source) == {"guilty"}

    def test_a_write_in_a_sibling_function_does_not_implicate_a_reassert(self) -> None:
        source = """
def writer(path, blob):
    atomic_write(path, blob, restrict_to_owner=True)


def reasserter(path):
    platform_compat.restrict_to_owner(path)
"""
        assert not checker.scan_source(source)


class TestTheRealTree:
    """The gate the CI job runs."""

    def test_src_has_no_unclassified_violation(self) -> None:
        exit_code = checker.main(["check", str(REPO_ROOT / "src" / "kiro_crew")])
        assert exit_code == 0, (
            "a lockdown-before-publish violation is unclassified. Convert it to "
            "atomic_write(..., restrict_to_owner=True), or annotate a genuine "
            "re-assert with `# lockdown-ok: <reason>`."
        )

    def test_every_known_unconverted_entry_still_violates(self) -> None:
        """KNOWN_UNCONVERTED is shrink-only: a paid-off entry must be deleted.

        Without this the list would quietly outlive the debt it tracks, and a
        future regression at one of those very sites would land unnoticed
        because its entry was already there.
        """
        src = REPO_ROOT / "src" / "kiro_crew"
        live: set[str] = set()
        for py in sorted(src.rglob("*.py")):
            for rel, _line, fn, _expr in checker.scan_path(py, REPO_ROOT):
                live.add(f"{rel}::{fn}")

        stale = sorted(set(checker.KNOWN_UNCONVERTED) - live)
        assert not stale, (
            "these KNOWN_UNCONVERTED entries no longer violate -- delete them "
            f"from scripts/{CHECKER_PATH.name}: {stale}"
        )

    # Source text deliberately built to break a NAIVE segment reader. Every line
    # that matters puts a node AFTER non-ASCII text, so a reader slicing
    # CHARACTERS where CPython reports BYTES lands in the wrong place -- and one
    # emoji is 4 UTF-8 bytes, so the error is not a fixed offset either.
    _UNICODE_FIXTURE = (
        "def load(base):\n"
        '    label = "\u914d\u7f6e\u6587\u4ef6"\n'
        '    status = {"\u540d\u79f0": label, "\u72b6\u6001 \u2705": base / "x.json"}\n'
        "    payload = helper(\n"
        '        "\u53c2\u6570\u4e00",\n'
        '        "\u53c2\u6570\u4e8c",\n'
        "    )\n"
        '    os.chmod(status["\u72b6\u6001 \u2705"], 0o600)  # \u5c3e\u90e8\u6ce8\u91ca\n'
        "    return payload\n"
    )

    def test_the_cached_segment_reader_agrees_with_the_stdlib(self) -> None:
        """`_seg` replaces `ast.get_source_segment` for speed, not for behaviour.

        The stdlib helper re-splits the whole file on every call, which turned the
        alias map's per-assignment lookups into a ~117s whole-tree scan. The
        replacement caches the split and slices BYTES, because CPython reports
        `col_offset` as a UTF-8 byte index.

        Asserted against the stdlib on a fixture built for the failure mode
        rather than on a large sample of the tree. An earlier version of this
        test walked ~10,000 nodes across four real files including the 142KB
        `sel.py`, and reintroduced on the REFERENCE side exactly the quadratic
        cost `_seg` exists to remove: it passed locally and timed out at 120s in
        CI. Node count was never the property -- byte offsets and multi-line
        spans are, and those are exhausted here in a few dozen nodes.
        """
        import ast

        source = self._UNICODE_FIXTURE
        tree = ast.parse(source)
        checked = 0
        multiline = 0
        for node in ast.walk(tree):
            if not hasattr(node, "lineno") or not hasattr(node, "end_col_offset"):
                continue
            assert checker._seg(source, node) == (
                ast.get_source_segment(source, node) or ""
            ), f"line {node.lineno} disagrees with ast.get_source_segment"
            checked += 1
            if node.end_lineno != node.lineno:
                multiline += 1

        # Floors, so a future edit to the fixture cannot quietly make this vacuous
        # or drop the branch that joins several lines.
        assert checked > 25, f"the fixture stopped exercising the reader: {checked}"
        assert multiline >= 2, f"the multi-line branch went uncovered: {multiline}"

    def test_the_segment_reader_agrees_on_a_real_file(self) -> None:
        """The fixture is synthetic; this keeps one real file in the comparison.

        Bounded on both axes -- the checker's own source (~37KB) rather than the
        tree's large modules, and a node cap -- because the stdlib reference is
        O(file size) per call and this test must not become the slow thing again.
        """
        import ast

        source = CHECKER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        nodes = [
            node
            for node in ast.walk(tree)
            if hasattr(node, "lineno") and hasattr(node, "end_col_offset")
        ]
        assert len(nodes) > 500, f"the checker source stopped being a real sample: {len(nodes)}"
        for node in nodes[:1200]:
            assert checker._seg(source, node) == (
                ast.get_source_segment(source, node) or ""
            ), f"{CHECKER_PATH.name}:{node.lineno} disagrees with ast.get_source_segment"

    def test_every_known_unconverted_key_is_a_posix_path(self) -> None:
        """A backslash key matches nothing, so the entry would read as stale.

        `scan_path` normalises with `.as_posix()` for the same reason. On Linux
        `str()` and `as_posix()` agree, so no assertion here can distinguish
        them -- the Windows shard is the real verification, and it caught this
        exact bug: every allowlist entry reported "no longer violates" while
        every real site reported as new.
        """
        bad = [key for key in checker.KNOWN_UNCONVERTED if "\\" in key]
        assert not bad, f"KNOWN_UNCONVERTED keys must use forward slashes: {bad}"

    def test_every_known_unconverted_entry_names_an_issue(self) -> None:
        bad = {
            key: entry
            for key, entry in checker.KNOWN_UNCONVERTED.items()
            if not entry[0].startswith("#") or not entry[0][1:].isdigit()
        }
        assert not bad, f"KNOWN_UNCONVERTED entries must cite an issue: {bad}"

    def test_every_known_unconverted_entry_names_its_path(self) -> None:
        """Each entry waives ONE path, not the whole function.

        Keyed by function alone, a second unrelated violating writer added to an
        allowlisted function would be suppressed with the tracked one.
        """
        bad = {
            key: entry
            for key, entry in checker.KNOWN_UNCONVERTED.items()
            if not isinstance(entry, tuple) or len(entry) != 2 or not entry[1].strip()
        }
        assert not bad, f"KNOWN_UNCONVERTED entries need (issue, path): {bad}"

    def test_a_second_writer_in_an_allowlisted_function_is_not_waived(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        """The waiver covers the tracked path only -- driven through ``main()``.

        The enforcement lives in ``main()`` (``entry[1] == expr``), so this runs a
        real file through it: one function, the tracked violation plus a second
        unrelated one. An earlier version of this test only compared two dict
        entries and never invoked the scanner at all, so it asserted nothing
        about the property it claimed to pin (First Principles review, #5348).
        """
        fixture = tmp_path / "writer.py"
        fixture.write_text(
            "def save(tracked, other, payload):\n"
            "    tracked.write_text(payload)\n"
            "    tracked.chmod(0o600)\n"
            "    other.write_text(payload)\n"
            "    other.chmod(0o600)\n",
            encoding="utf-8",
        )
        # `scan_path` keys a file outside the repo by its absolute posix path.
        monkeypatch.setattr(
            checker,
            "KNOWN_UNCONVERTED",
            {f"{fixture.as_posix()}::save": ("#9999", "tracked")},
        )

        exit_code = checker.main(["check", str(fixture)])

        out = capsys.readouterr().out
        assert exit_code == 1, f"the untracked second writer was waived too:\n{out}"
        assert "`other`" in out, f"the new violation was not reported:\n{out}"
        assert (
            "no longer violate" not in out
        ), f"the tracked entry was reported stale, so the waiver never matched:\n{out}"

    def test_the_tracked_path_alone_is_waived(self, tmp_path: Path, monkeypatch) -> None:
        """Mirror of the above: with only the tracked writer present, main() passes.

        Without this, the assertion above would also hold if the waiver were
        broken outright and every entry reported as a new violation.
        """
        fixture = tmp_path / "writer.py"
        fixture.write_text(
            "def save(tracked, payload):\n"
            "    tracked.write_text(payload)\n"
            "    tracked.chmod(0o600)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            checker,
            "KNOWN_UNCONVERTED",
            {f"{fixture.as_posix()}::save": ("#9999", "tracked")},
        )

        assert checker.main(["check", str(fixture)]) == 0


# ─── behavioural probes: ORDER at the live writers ──────────────────────────
#
# Technique from PR #5314 (@leonlaiyc): patch the lockdown helper and record
# whether the FINAL path already exists when it runs. That is a property both
# platforms must satisfy, unlike a final-mode assertion.


@pytest.fixture()
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _record_final_path_presence(final_path_of):
    """Patch restrict_to_owner, returning the list it records into."""
    from kiro_crew import platform_compat

    seen: list[bool] = []
    real = platform_compat.restrict_to_owner

    def _recording(path, *args, **kwargs):
        seen.append(Path(final_path_of()).exists())
        return real(path, *args, **kwargs)

    return seen, _recording


class TestTheLiveTargetPointerIsLockedDownBeforePublication:
    """The PUBLISHED-path invariant at the live writers: the final path must not
    exist yet when the lockdown runs.

    Not a duplicate of ``test_live_target.py``'s ``test_lockdown_precedes_content``
    / ``test_restore_reapplies_the_owner_only_dacl``, which measure SIZE == 0 at
    lockdown time -- that is the TEMP-path invariant, and neither implies the
    other. ``os.open(final)`` then lock then write satisfies size == 0 while the
    final path already exists; writing a temp, locking it, then renaming
    satisfies final-absent with a non-zero size. This gate enforces the
    published-path invariant, so these probes pin the property it enforces.
    """

    def test_write_target_never_publishes_an_unprotected_pointer(self, _home) -> None:
        from kiro_crew.service import live_target

        seen, recording = _record_final_path_presence(live_target.pointer_path)
        with patch("kiro_crew.platform_compat.restrict_to_owner", side_effect=recording):
            live_target.write_target(_make_valid_checkout(_home))

        assert seen, "the pointer was written with no owner-only lockdown at all"
        assert not any(seen), (
            "the lockdown ran while the pointer already existed at its final "
            "path -- the payload was published before it was protected"
        )

    def test_restore_never_publishes_an_unprotected_pointer(self, _home) -> None:
        from kiro_crew.service import live_target

        live_target.write_target(_make_valid_checkout(_home))
        prior = live_target.pointer_path().read_text(encoding="utf-8")
        live_target.pointer_path().unlink()

        seen, recording = _record_final_path_presence(live_target.pointer_path)
        with patch("kiro_crew.platform_compat.restrict_to_owner", side_effect=recording):
            live_target.restore(prior)

        assert seen, "restore rewrote the pointer with no lockdown at all"
        assert not any(seen), "restore locked the pointer down only after republishing it"
