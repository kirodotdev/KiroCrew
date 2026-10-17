"""The temp base fixtures use must be usable on every OS.

The Windows half is what regressed: `mkdtemp(dir="/tmp")` resolves against the
current drive and does not create its `dir`, so it raised FileNotFoundError
unless something unrelated had already made that directory. These tests pin the
branch itself, which is verifiable from any host, rather than the shard outcome.
"""

import ast
import functools
import os
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pytest
from tmpdir_helpers import _SHORT_TMP_ROOT_ENV as SHORT_TMP_ROOT_ENV
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import platform_compat


class TestShortTmpBase:
    def test_the_runs_own_short_root_wins_when_one_exists(self, monkeypatch, tmp_path):
        """The run-owned root is what makes a stray directory attributable to a RUN.

        ``SHORT_TMP_PREFIX`` already names a short-rooted directory as the suite's. What it
        cannot say is which run made it or who removes it: the residue guard and the hygiene
        probe sanction the ``kc-pytest-<user>-<pid>-`` stem and nothing else, so a correctly
        prefixed directory dropped straight into the shared ``/tmp`` is still owned by no
        session. Returning the run's own root is what closes that half.
        """
        root = tmp_path / "kc-pytest-u-1-short-abcd"
        root.mkdir()
        monkeypatch.setenv(SHORT_TMP_ROOT_ENV, str(root))
        assert short_tmp_base() == str(root)

    def test_a_root_that_vanished_falls_back_instead_of_breaking_the_fixture(
        self, monkeypatch, tmp_path
    ):
        """A stale variable must not hand ``mkdtemp`` a path that does not exist.

        The value is inherited by every xdist worker and by spawned children, so it can
        outlive the directory (a reaped ``/tmp``, a killed controller). Falling back keeps
        the socket fixtures working; trusting the variable would turn that into a
        ``FileNotFoundError`` in unrelated tests.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setenv(SHORT_TMP_ROOT_ENV, str(tmp_path / "gone"))
        assert short_tmp_base() == "/tmp"

    def test_posix_keeps_the_low_entropy_tmp_without_a_run_root(self, monkeypatch):
        """`/tmp` is what satisfies the redaction and sun_path constraints."""
        monkeypatch.delenv(SHORT_TMP_ROOT_ENV, raising=False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        assert short_tmp_base() == "/tmp"

    def test_windows_defers_to_the_platform_base(self, monkeypatch):
        """`None` makes mkdtemp use gettempdir(), which exists by construction --
        unlike `<drive>\\tmp`, which mkdtemp will not create."""
        monkeypatch.delenv(SHORT_TMP_ROOT_ENV, raising=False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        assert short_tmp_base() is None

    def test_this_run_published_a_root_that_carries_the_run_stem(self):
        """The floor's end of the contract, observed live rather than reasoned about.

        A root without the run stem is invisible to the residue guard, which is the
        failure this half exists to remove -- so assert the shape, not just that some
        directory was published.
        """
        root = os.environ.get(SHORT_TMP_ROOT_ENV)
        if root is None:  # a checkout whose rootdir conftest predates the root
            return
        assert Path(root).is_dir(), root
        assert Path(root).name.startswith("kc-pytest-"), root
        assert "-short-" in Path(root).name, root

    def test_the_resolved_base_is_usable_on_this_host(self):
        """The regression itself: whatever branch THIS platform takes must yield a
        directory `mkdtemp` can actually create.

        Deliberately not parametrized over both branches. Forcing the POSIX branch
        while running on Windows asks for `/tmp` -- precisely the path that does
        not exist there -- so a both-branches loop would reproduce the very
        runner-state dependency the rest of this change removes.
        """
        base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "helpers-", dir=short_tmp_base()))
        try:
            assert base.is_dir()
            assert os.access(base, os.W_OK | os.X_OK), f"{base} is not writable"
            probe = base / "probe.txt"
            probe.write_text("ok", encoding="utf-8")
            assert probe.read_text(encoding="utf-8") == "ok"
        finally:
            shutil.rmtree(base, ignore_errors=True)


# --- the predicates the ratchets share -------------------------------------------------


def _carries_shared_prefix(call: ast.Call) -> bool:
    """Whether a `mkdtemp` call's `prefix=` is built from `SHORT_TMP_PREFIX`.

    `SHORT_TMP_PREFIX + "label"` and the bare name both qualify; anything else, a
    literal of its own or no `prefix=` at all, does not.
    """
    prefix = next((kw.value for kw in call.keywords if kw.arg == "prefix"), None)
    if isinstance(prefix, ast.BinOp):
        return getattr(prefix.left, "id", None) == "SHORT_TMP_PREFIX"
    return getattr(prefix, "id", None) == "SHORT_TMP_PREFIX"


def _short_rooted_calls(tree: ast.AST) -> list[ast.Call]:
    """Calls that place a temp dir in the shared short root, either way of asking:
    `dir=short_tmp_base()` or a hardcoded `dir="/tmp"`."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "dir":
                continue
            via_helper = (
                isinstance(kw.value, ast.Call)
                and getattr(kw.value.func, "id", None) == "short_tmp_base"
            )
            hardcoded = isinstance(kw.value, ast.Constant) and kw.value.value == "/tmp"
            if via_helper or hardcoded:
                out.append(node)
                break
    return out


# The two spellings `_short_rooted_calls` matches on, as they appear in source text. A
# file whose text has neither cannot hold a short-rooted call, so it is read but never
# parsed. The identifier is matched on the NFKC-normalised text because CPython folds
# identifiers at parse time: a compatibility homoglyph of `short_tmp_base` IS
# `short_tmp_base` in the tree but not in the bytes (the same gate
# `test/source_corpus.py` applies to the package-tree ratchets). The literal is matched
# as written; an escaped spelling of the string (`"\x2ftmp"`) is not an accident a
# fixture author makes, and accidents are what the ratchet is for.
_SHORT_ROOT_SPELLINGS = ("short_tmp_base", "/tmp")


def _can_hold_short_rooted_call(source: str) -> bool:
    text = source if source.isascii() else unicodedata.normalize("NFKC", source)
    return any(spelling in text for spelling in _SHORT_ROOT_SPELLINGS)


@dataclass(frozen=True)
class _Finding:
    file: str
    lineno: int
    shared_prefix: bool


@dataclass(frozen=True)
class _Scan:
    files: int  # every `*.py` the glob resolved to
    parsed: int  # the subset whose text could hold a short-rooted call
    findings: tuple[_Finding, ...]  # every short-rooted call, prefixed or not


def _scan_tree(root: Path) -> _Scan:
    """Read every `*.py` directly under `root` once; parse only the candidates.

    Uncached so a test can point it at a fixture tree. Nothing but the findings is
    retained: trees are dropped as soon as they are walked, so the scan leaves no source
    text or AST behind on the worker.
    """
    files = parsed = 0
    findings: list[_Finding] = []
    for path in sorted(root.glob("*.py")):
        files += 1
        source = path.read_text(encoding="utf-8")
        if not _can_hold_short_rooted_call(source):
            continue
        parsed += 1
        tree = ast.parse(source, filename=str(path))
        for call in _short_rooted_calls(tree):
            findings.append(_Finding(path.name, call.lineno, _carries_shared_prefix(call)))
    return _Scan(files, parsed, tuple(findings))


@functools.lru_cache(maxsize=1)
def _scan() -> _Scan:
    """The test tree's reading. Cached: both ratchets below read the same one.

    The tree is ~3700 files / ~120 MB of Python. Parsing all of it is ~18 s on an idle
    core; two tests each doing so independently, on a loaded shard that is also running
    the other tree-scan ratchets, is what overran the 120 s per-test timeout. One read,
    and a parse of only the ~500 files whose text can hold a site, is ~6 s shared.
    """
    return _scan_tree(Path(__file__).parent)


class TestEveryShortRootedFixtureIsNamed:
    """A short-rooted dir sits outside the per-test root the floor pins, so a hygiene probe
    can only tell it from a host leak by its name.

    Two names fail that. The stdlib default `/tmp/tmp<random>` is what an accidental bare
    `mkdtemp()` produces too, so a probe cannot sanction it without going blind to real
    leaks; a per-fixture name instead forces the probe to carry a list of invented
    prefixes, which is wrong the moment a fixture is added. One shared prefix is one
    anchored stem with nothing to keep in sync, and these pin every caller to it.

    The prefix is still the answer now that `short_tmp_base()` returns a RUN-OWNED root: the
    root says which run, the prefix says which fixture, and a caller that skips the prefix is
    unattributable the moment the root is absent -- outside a pytest run, or in a checkout
    whose conftest predates it, both of which fall back to the shared `/tmp`.
    """

    def test_the_prefix_is_a_distinct_anchored_stem(self):
        """Not the stdlib default, and ending in `-` so the stem cannot match a bare name."""
        assert SHORT_TMP_PREFIX not in ("", "tmp", "tmp-")
        assert SHORT_TMP_PREFIX.endswith("-")

    # --- the predicates' own pins: a ratchet loosened here passes over a real offender ---

    @pytest.mark.parametrize(
        "src, shared",
        [
            ('mkdtemp(prefix=SHORT_TMP_PREFIX + "pod-", dir=short_tmp_base())', True),
            ('mkdtemp(prefix=SHORT_TMP_PREFIX, dir="/tmp")', True),
            # The two shapes the sweep found, both of which a weaker predicate accepts:
            ("mkdtemp(dir=short_tmp_base())", False),  # stdlib default name
            ('mkdtemp(prefix="pw-", dir=short_tmp_base())', False),  # invented name
            ('mkdtemp(prefix="kcs-", dir="/tmp")', False),
        ],
    )
    def test_the_predicate_accepts_only_the_shared_prefix(self, src, shared):
        """Relaxing `_carries_shared_prefix` to `prefix is not None`, or to any string
        literal, turns both ratchets below green while the offending fixture ships."""
        call = ast.parse(src).body[0].value
        assert _carries_shared_prefix(call) is shared

    @pytest.mark.parametrize(
        "src, found",
        [
            ("mkdtemp(dir=short_tmp_base())", 1),
            ('mkdtemp(dir="/tmp")', 1),
            ("mkdtemp()", 0),
            ("mkdtemp(dir=tmp_path)", 0),  # the pinned per-test root, not the shared one
            ('mkdtemp(dir="/tmpfs")', 0),  # substring of "/tmp", a different directory
        ],
    )
    def test_the_scanner_finds_exactly_the_short_rooted_calls(self, src, found):
        """Widening `_short_rooted_calls` to every `mkdtemp` would demand a prefix from
        fixtures that correctly use the pinned root; narrowing it to one spelling lets the
        other spelling through unchecked."""
        assert len(_short_rooted_calls(ast.parse(src))) == found
        # The pre-filter must keep every file the scanner can find a call in: a spelling
        # the scanner matches but the filter drops would be skipped unparsed, green and blind.
        if found:
            assert _can_hold_short_rooted_call(src)

    def test_the_prefilter_skips_only_files_that_cannot_hold_a_call(self, tmp_path):
        """The scan reads every file but parses only the candidates, so the filter is the
        one place a site could hide for free. Exercised end to end against a fixture tree:
        both spellings are found, a homoglyph identifier is found (CPython folds it to the
        ASCII name at parse time, and the filter normalises the same way), and a file with
        neither spelling is read, counted, and never parsed -- which is why its deliberate
        syntax error does not raise."""
        (tmp_path / "offender.py").write_text(
            'x = mkdtemp(prefix="pw-", dir=short_tmp_base())\n', encoding="utf-8"
        )
        (tmp_path / "clean.py").write_text(
            "\n".join(
                [
                    "import os",
                    'a = mkdtemp(prefix=SHORT_TMP_PREFIX, dir="/tmp")',
                    'b = mkdtemp(prefix=SHORT_TMP_PREFIX + "x-", dir=\uff53hort_tmp_base())',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (tmp_path / "pinned.py").write_text("y = mkdtemp(dir=tmp_path)\n", encoding="utf-8")
        (tmp_path / "prose.py").write_text("this is not python (\n", encoding="utf-8")
        (tmp_path / "ignored.txt").write_text('mkdtemp(dir="/tmp")\n', encoding="utf-8")

        scan = _scan_tree(tmp_path)

        assert scan.files == 4
        assert scan.parsed == 2
        assert scan.findings == (
            _Finding("clean.py", 2, True),
            _Finding("clean.py", 3, True),
            _Finding("offender.py", 1, False),
        )

    # --- the ratchets ------------------------------------------------------------------

    def test_every_short_rooted_fixture_passes_the_shared_prefix(self):
        """Ratchet. A new fixture in the shared root without the shared prefix fails here
        rather than surfacing later as an unexplained `host_write` cluster."""
        offenders = [f"{f.file}:{f.lineno}" for f in _scan().findings if not f.shared_prefix]
        assert (
            offenders == []
        ), "temp dir in the shared short root without SHORT_TMP_PREFIX: " + ", ".join(offenders)

    def test_the_ratchet_scans_more_than_one_file(self):
        """The healthy side. A glob that resolved to nothing, a parse that raised into a
        swallowed except, or a pre-filter that dropped the whole tree would report zero
        offenders over zero files -- green, and blind."""
        scan = _scan()
        scanned = sorted({f.file for f in scan.findings})
        assert len(scanned) >= 8, f"only scanned {scanned}"
        assert scan.files >= scan.parsed >= len(scanned)
        assert scan.parsed < scan.files, "the pre-filter kept every file; it filters nothing"
