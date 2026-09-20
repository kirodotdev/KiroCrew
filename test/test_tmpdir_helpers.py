"""The temp base fixtures use must be usable on every OS.

The Windows half is what regressed: `mkdtemp(dir="/tmp")` resolves against the
current drive and does not create its `dir`, so it raised FileNotFoundError
unless something unrelated had already made that directory. These tests pin the
branch itself, which is verifiable from any host, rather than the shard outcome.
"""

import ast
import shutil
import tempfile
from pathlib import Path

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import platform_compat


class TestShortTmpBase:
    def test_posix_keeps_the_low_entropy_tmp(self, monkeypatch):
        """`/tmp` is what satisfies the redaction and sun_path constraints."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        assert short_tmp_base() == "/tmp"

    def test_windows_defers_to_the_platform_base(self, monkeypatch):
        """`None` makes mkdtemp use gettempdir(), which exists by construction --
        unlike `<drive>\\tmp`, which mkdtemp will not create."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        assert short_tmp_base() is None

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
            probe = base / "probe.txt"
            probe.write_text("ok", encoding="utf-8")
            assert probe.read_text(encoding="utf-8") == "ok"
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestEveryShortRootedFixtureIsNamed:
    """A short-rooted dir sits in the shared `/tmp`, outside the per-test root the floor
    pins, so a hygiene probe can only tell it from a host leak by its name.

    Two names fail that. The stdlib default `/tmp/tmp<random>` is what an accidental bare
    `mkdtemp()` produces too, so a probe cannot sanction it without going blind to real
    leaks; a per-fixture name instead forces the probe to carry a list of invented
    prefixes, which is wrong the moment a fixture is added. One shared prefix is one
    anchored stem with nothing to keep in sync, and these pin every caller to it.
    """

    def test_the_prefix_is_a_distinct_anchored_stem(self):
        """Not the stdlib default, and ending in `-` so the stem cannot match a bare name."""
        assert SHORT_TMP_PREFIX not in ("", "tmp", "tmp-")
        assert SHORT_TMP_PREFIX.endswith("-")

    # --- the predicate the two ratchets below share -------------------------------------

    @staticmethod
    def _carries_shared_prefix(call: ast.Call) -> bool:
        """Whether a `mkdtemp` call's `prefix=` is built from `SHORT_TMP_PREFIX`.

        `SHORT_TMP_PREFIX + "label"` and the bare name both qualify; anything else, a
        literal of its own or no `prefix=` at all, does not.
        """
        prefix = next((kw.value for kw in call.keywords if kw.arg == "prefix"), None)
        if isinstance(prefix, ast.BinOp):
            return getattr(prefix.left, "id", None) == "SHORT_TMP_PREFIX"
        return getattr(prefix, "id", None) == "SHORT_TMP_PREFIX"

    @staticmethod
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

    # --- the predicate's own pins: a ratchet loosened here passes over a real offender ---

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
        assert self._carries_shared_prefix(call) is shared

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
        assert len(self._short_rooted_calls(ast.parse(src))) == found

    # --- the ratchets ------------------------------------------------------------------

    def test_every_short_rooted_fixture_passes_the_shared_prefix(self):
        """Ratchet. A new fixture in the shared root without the shared prefix fails here
        rather than surfacing later as an unexplained `host_write` cluster."""
        offenders = []
        for path in sorted(Path(__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for call in self._short_rooted_calls(tree):
                if not self._carries_shared_prefix(call):
                    offenders.append(f"{path.name}:{call.lineno}")
        assert (
            offenders == []
        ), "temp dir in the shared short root without SHORT_TMP_PREFIX: " + ", ".join(offenders)

    def test_the_ratchet_scans_more_than_one_file(self):
        """The healthy side. A glob that resolved to nothing, or a parse that raised into a
        swallowed except, would report zero offenders over zero files -- green, and blind."""
        scanned = [
            path
            for path in sorted(Path(__file__).parent.glob("*.py"))
            if self._short_rooted_calls(ast.parse(path.read_text(encoding="utf-8")))
        ]
        assert len(scanned) >= 8, f"only scanned {[p.name for p in scanned]}"
