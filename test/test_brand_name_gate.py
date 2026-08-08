"""Unit tests for scripts/check_brand_name.py.

The gate blocks merges, so every exemption gets a test: an exemption that
silently stops matching turns the gate into a rubber stamp, and an exemption that
silently widens turns it into a false-positive machine. The script's own
``--test`` mode covers the same rule families; these tests add the git-diff
scoping, the file-level scan, and the exit-code contract that ``--test`` cannot
reach without a repository.
"""

from __future__ import annotations

import importlib.util
import math
import os
import subprocess
import sys
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "check_brand_name.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_brand_name", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_brand_name"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _hits(line: str, path: str = "probe.py", in_code: bool = False) -> list[str]:
    return [v.token for v in gate.scan_line(path, 1, line, in_code=in_code)]


# ---------------------------------------------------------------------------
# The misspellings the gate exists to catch
# ---------------------------------------------------------------------------


class TestCaught:
    @pytest.mark.parametrize(
        "line",
        [
            "KiroCrew keeps working while you sleep.",
            "Run KiroCrew on your own hardware.",
            "This is KiroCrew's own sandbox.",
            "the process that launched KiroCrew.",
            "A desktop companion for KiroCrew that paces your day",
            "every KiroCrew-owned file",
            '"about_blurb": "A companion built into KiroCrew."',
            "# KiroCrew needs Python >= 3.10 at runtime",
        ],
    )
    def test_concatenated_prose_is_flagged(self, line: str) -> None:
        assert _hits(line), f"missed: {line}"

    @pytest.mark.parametrize("token", ["KiroCrew", "Kirocrew", "kiroCrew", "KiroCREW"])
    def test_every_mixed_case_join_is_flagged(self, token: str) -> None:
        assert _hits(f"powered by {token} today") == [token]

    def test_uncapitalised_spacing_is_flagged(self) -> None:
        # "at least the first letter capitalised" — a spaced but lowercase brand
        # is still wrong.
        assert _hits("install kiro crew first") == ["kiro crew"]

    def test_capitalised_spacing_is_accepted(self) -> None:
        assert _hits("Kiro Crew keeps working.") == []
        assert _hits("Kiro crew keeps working.") == []

    def test_multiple_hits_on_one_line_all_reported(self) -> None:
        assert _hits("KiroCrew talks to KiroCrew over SSH") == ["KiroCrew", "KiroCrew"]


# ---------------------------------------------------------------------------
# Identifiers keep the spelling their own system gave them
# ---------------------------------------------------------------------------


class TestExempt:
    @pytest.mark.parametrize(
        "line",
        [
            # The systems that own the joined spelling.
            "run `kirocrew serve` to start the gateway",
            'os.environ["KIROCREW_HOME"]',
            "from kiro_crew.config import loader",
            "state lives under ~/.kiro/crew/workspace",
            "mailto:kiro-crew-security-support@example.com",
            # Repo slug and URLs.
            "https://github.com/kirodotdev/KiroCrew/issues",
            "git clone https://github.com/kirodotdev/KiroCrew.git",
            "pushed to `kirodotdev/KiroCrew` itself",
            # Path segments — each separator side alone, since a brand with a
            # separator on both sides is covered by either check.
            "built from ~/src/KiroCrew last night",
            "KiroCrew/website holds the frontend",
            r"installed to C:\Program Files\KiroCrew",
            r"launches KiroCrew\resources\app.asar",
            "artifacts/KiroCrew-notarized-stable/KiroCrew.dmg",
            # One identifier, not two words.
            "resolve KiroCrewApps from the registry",
            "the KiroCrewPublishCDK distribution stack",
            "SHIM = MyKiroCrewShim()",
            # Hyphenated identifier interior — an HTTP header, not prose.
            'assert "X-KiroCrew-Proxy" in headers',
            # Release artifacts.
            "electron-builder signs KiroCrew.exe and Update.exe",
            "publishes KiroCrew-x86_64.AppImage beside the deb",
            "opens /Applications/KiroCrew.app",
            "the KiroCrew-Nightly channel",
        ],
    )
    def test_identifier_forms_are_not_flagged(self, line: str) -> None:
        assert _hits(line) == [], f"false positive: {line}"

    def test_sentence_end_is_not_read_as_a_file_extension(self) -> None:
        # `KiroCrew.exe` is exempt; `KiroCrew.` at the end of a sentence is not.
        assert _hits("shipped with KiroCrew.") == ["KiroCrew"]
        assert _hits("shipped with KiroCrew.exe") == []

    def test_hyphen_exemption_does_not_swallow_prose(self) -> None:
        # Only an *interior* hyphen segment is an identifier.
        assert _hits("a KiroCrew-specific workaround") == ["KiroCrew"]

    def test_line_suppression(self) -> None:
        assert _hits("correct = 'KiroCrew'  # brand-ok: transcript fixture") == []

    def test_channel_qualified_identifier(self) -> None:
        # PRODUCT_NAMES in cli_desktop.py spells the macOS log dir this way, so the
        # space form is an OS identifier even though it reads like prose.
        assert _hits('home / "Library" / "Logs" / "KiroCrew Nightly"') == []
        assert _hits("the KiroCrew Insider channel") == []
        # Any other following word is prose again.
        assert _hits("the KiroCrew dashboard") == ["KiroCrew"]


class TestUrlBoundary:
    """A URL exempts what is *inside* it, and nothing after it."""

    def test_wrapped_link_still_exempts_the_slug(self) -> None:
        assert _hits("see [docs](https://github.com/kirodotdev/KiroCrew/issues)") == []
        assert _hits("<https://github.com/kirodotdev/KiroCrew>") == []

    def test_markup_closing_a_url_does_not_exempt_the_text_after_it(self) -> None:
        assert _hits('<a href="https://example.com/">KiroCrew</a>') == ["KiroCrew"]
        assert _hits("[KiroCrew](https://example.com/)") == ["KiroCrew"]

    def test_a_url_earlier_on_the_line_does_not_exempt_later_prose(self) -> None:
        assert _hits("https://example.com/x is where KiroCrew lives") == ["KiroCrew"]

    @pytest.mark.parametrize(
        "filler,glue",
        [(".", " "), ("a-", " "), ("x.com", " "), ("`", " "), ("a.", "")],
    )
    def test_a_very_long_line_stays_linear(self, filler: str, glue: str) -> None:
        # A generated file can carry one enormous line. Quadratic backtracking here
        # would blow the CI job's timeout on input nobody can see is pathological.
        line = filler * (200_000 // len(filler)) + glue + "KiroCrew is here"
        started = time.monotonic()
        found = _hits(line, path="big.md")
        assert time.monotonic() - started < 2.0
        assert found == ["KiroCrew"]

    def test_many_brand_names_on_one_line_stay_linear(self) -> None:
        # The other axis: not one long line, but MANY matches on it. Any per-match
        # step that slices the line or rescans its prefix is quadratic here, and a
        # ratio assertion catches that where a wall-clock budget loose enough for a
        # loaded runner would not.
        #
        # We only need to separate two regimes: a linear scan doubles the input for
        # ~2x the time, while a quadratic one (slice/rescan the whole line per
        # match) costs ~4x AND with a far larger constant that blows well past 4x
        # at these sizes. The bound sits between them with headroom for a noisy
        # runner, so real regressions are still caught.
        #
        # This mirrors the script's own ``--test`` growth check (and reuses its
        # budgets, so tuning one tunes both) because it failed the same way and for
        # the same reason: dividing two measured durations puts the whole burden on
        # the timer, and the wall-clock form this test used to carry was the single
        # largest source of Backend Tests (Windows) flakes.
        #
        # * WRONG CLOCK. ``time.monotonic()`` is ``GetTickCount64()`` on Windows, a
        #   ~15.625ms tick. A ~50ms scan is only ~3 ticks wide, so quantisation
        #   ALONE moved the ratio ~25%.
        # * WALL CLOCK AT ALL. xdist workers oversubscribe the runner's cores, so
        #   the timed region gets descheduled -- and the LONGER scan absorbs more
        #   preemption than the shorter one, which inflates the ratio
        #   systematically rather than symmetrically. Taking min() of several
        #   wall-clock samples does NOT fix that: it is exactly what this test did,
        #   and it still reported 3.7x against a 3.5x bound on a linear scan.
        #   Measuring the two sizes in separate loops made it worse again, because
        #   the two halves of the ratio then came from different conditions.
        #
        # So measure CPU time, which does not advance while the thread is off-CPU;
        # pair each baseline with its own doubled sample; keep the best RATIO
        # rather than the best time; and refuse to judge a baseline too small to
        # divide. That is what lets the bound come back DOWN to the script's 3.0x
        # from the 3.5x the noise had forced: measured under 2x CPU
        # oversubscription, a linear scan stays at most 2.02x while a deliberately
        # quadratic one never drops below 3.88x.
        def ratio_of(base: int) -> tuple[float, float, int, int]:
            """Best (least noisy) doubled/base CPU-time ratio over several attempts."""
            best = math.inf
            best_base = 0.0
            found = (0, 0)

            def once(count: int) -> tuple[float, int]:
                line = "!KiroCrew" * count
                began = time.process_time()
                hits = len(_hits(line, path="big.md"))
                return time.process_time() - began, hits

            for _ in range(gate._PERF_ATTEMPTS):
                base_time, base_hits = once(base)
                doubled_time, doubled_hits = once(base * 2)
                found = (base_hits, doubled_hits)
                if base_time <= 0.0:
                    continue
                candidate = doubled_time / base_time
                if candidate < best:
                    best, best_base = candidate, base_time
            return (0.0 if best is math.inf else best), best_base, found[0], found[1]

        # Grow the workload until the baseline is big enough to divide. A regressed
        # scan clears the floor at the first size, so only the fast case ever pays
        # for a larger one.
        base_count = 0
        ratio = base_time = 0.0
        base_found = doubled_found = 0
        for base_count in gate._PERF_BASE_SIZES:
            ratio, base_time, base_found, doubled_found = ratio_of(base_count)
            if base_time >= gate._PERF_MIN_BASE_SECS:
                break

        # Match counts are a correctness assertion, not a timing one -- they hold
        # whatever the clock did, so they are checked before the floor bails out.
        assert (base_found, doubled_found) == (base_count, base_count * 2)
        if base_time < gate._PERF_MIN_BASE_SECS:
            pytest.skip(
                f"baseline {base_time * 1000:.1f}ms at {base_count} brands is still below "
                f"the {gate._PERF_MIN_BASE_SECS * 1000:.0f}ms measurement floor; a ratio "
                "here would be noise divided by noise. Quadratic growth at this size "
                "costs orders of magnitude more than the floor, so this cannot be hiding "
                "a regression."
            )
        assert ratio < 3.0, (
            f"doubling the input cost {ratio:.1f}x CPU time (best of {gate._PERF_ATTEMPTS}, "
            f"baseline {base_time:.3f}s at {base_count} brands); linear is ~2x, so a "
            "per-match scan of the line has come back"
        )

    def test_many_backticks_stay_linear(self) -> None:
        line = "`x`" * 30_000 + " KiroCrew"
        started = time.monotonic()
        assert _hits(line, path="big.md") == ["KiroCrew"]
        assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------
# Markdown code context
# ---------------------------------------------------------------------------


class TestMarkdownCode:
    def test_fenced_block_is_exempt_and_surrounding_prose_is_not(self) -> None:
        doc = [
            "Install KiroCrew:",
            "```bash",
            "git clone https://github.com/kirodotdev/KiroCrew.git",
            "cd KiroCrew",
            "```",
            "Then start KiroCrew.",
        ]
        fenced = gate.fenced_lines(doc)
        assert fenced == {2, 3, 4, 5}
        flagged = [
            i
            for i, line in enumerate(doc, start=1)
            for _ in gate.scan_line("doc.md", i, line, in_code=i in fenced)
        ]
        assert flagged == [1, 6]

    def test_tilde_fences_and_reopened_blocks(self) -> None:
        doc = ["a", "~~~", "cd KiroCrew", "~~~", "b", "```", "cd KiroCrew", "```"]
        assert gate.fenced_lines(doc) == {2, 3, 4, 6, 7, 8}

    def test_a_wider_fence_is_not_closed_by_a_narrower_run(self) -> None:
        # `builtin_skills/artifacts/SKILL.md` quotes fenced examples inside
        # four-backtick blocks. Closing the outer fence on the inner ``` would
        # expose the example's contents as prose and flag its identifiers.
        doc = ["````markdown", "```bash", "cd KiroCrew", "```", "````", "Then run KiroCrew."]
        assert gate.fenced_lines(doc) == {1, 2, 3, 4, 5}
        flagged = [
            i
            for i, line in enumerate(doc, start=1)
            for _ in gate.scan_line("doc.md", i, line, in_code=i in gate.fenced_lines(doc))
        ]
        assert flagged == [6]

    def test_a_closing_fence_may_not_carry_an_info_string(self) -> None:
        # Only a bare run closes; ```` ```python ```` inside a block is an opener
        # for a nested example, not the outer block's terminator.
        doc = ["````", "```python", "x = 1", "```", "````", "KiroCrew here"]
        assert gate.fenced_lines(doc) == {1, 2, 3, 4, 5}

    def test_inline_code_span_is_exempt_but_the_rest_of_the_line_is_not(self) -> None:
        assert _hits("clone `KiroCrew` then start KiroCrew.", path="doc.md") == ["KiroCrew"]

    def test_inline_code_exemption_is_markdown_only(self) -> None:
        # A backtick in Python is not a code span; a TS template literal string
        # is still shipped text.
        assert _hits("s = 'see `KiroCrew` docs'", path="a.py") == ["KiroCrew"]

    def test_the_uncapitalised_rule_has_its_own_inline_code_exemption(self) -> None:
        # A separate branch from the concatenated rule's, so it needs its own case.
        assert _hits("run `kiro crew` from a shell", path="doc.md") == []
        assert _hits("run kiro crew from a shell", path="doc.md") == ["kiro crew"]


# ---------------------------------------------------------------------------
# Scope: files and diffs
# ---------------------------------------------------------------------------


class TestScope:
    def test_out_of_scope_paths(self) -> None:
        assert not gate.in_scope("website/package-lock.json")
        assert not gate.in_scope("node_modules/foo/README.md")
        assert not gate.in_scope("assets/banner.png")
        assert not gate.in_scope("scripts/check_brand_name.py")
        assert not gate.in_scope("src/kiro_crew/_vendor/libggml-base.so.0")
        assert not gate.in_scope("website/electron/package.json")
        assert gate.in_scope("README.md")

    def test_generated_artifacts_are_reported_but_never_enforced(self) -> None:
        # A regeneration or re-indent moves lines the author did not write, and JSON
        # has no comment syntax to opt out of.
        for path in (
            "website/src/i18n/locales/de.json",
            "website/src/i18n/locales/zh-CN.json",
            "website/src/i18n/locales/en-XA.json",
            "src/kiro_crew/data/tips_catalog.json",
        ):
            assert gate.in_scope(path), path
            assert not gate.enforced(path), path
        # The sources they are generated FROM stay enforced — that is where a human
        # writes the string, and fixing it there is what reaches the artifact.
        for path in (
            "website/src/i18n/locales/en.json",
            "website/src/i18n/locales/en.manual.json",
            "website/src/i18n/glossary.json",
            "src/kiro_crew/docs/skills.md",
        ):
            assert gate.enforced(path), path

    def test_enforced_is_never_wider_than_in_scope(self) -> None:
        for path in (
            "website/package-lock.json",
            "src/kiro_crew/_vendor/x.so.0",
            "website/electron/package.json",
            "website/src/i18n/locales/de.json",
            "src/kiro_crew/data/tips_catalog.json",
        ):
            assert not (gate.enforced(path) and not gate.in_scope(path)), path

    def test_scan_file_restricted_to_given_lines(self, tmp_path, monkeypatch) -> None:
        # Point the scanner's root at tmp_path rather than deriving a relative path
        # from it: on Windows the temp dir and the repo sit on different drives, and
        # os.path.relpath raises across mounts.
        monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
        (tmp_path / "note.md").write_text(
            "KiroCrew one\nKiroCrew two\nKiroCrew three\n", encoding="utf-8"
        )
        assert len(gate.scan_file("note.md")) == 3
        assert [v.line_no for v in gate.scan_file("note.md", {2})] == [2]

    def test_unreadable_file_is_skipped_not_fatal(self) -> None:
        assert gate.scan_file("does/not/exist.md") == []
        assert gate.read_lines("does/not/exist.md") is None

    def test_lines_are_split_the_way_git_counts_them(self, tmp_path, monkeypatch) -> None:
        # git splits on \n only. A lone \r must not start a new line here, or every
        # line number after it points at the wrong text.
        monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
        (tmp_path / "crlf.md").write_bytes(b"one\r\ntwo \rstill two\r\nKiroCrew\r\n")
        lines = gate.read_lines("crlf.md")
        assert lines is not None and len(lines) == 4  # 3 real lines + trailing ""
        assert [v.line_no for v in gate.scan_file("crlf.md")] == [3]


@pytest.mark.xdist_group(name="subprocess_spawn")
class TestDiffScopedRun:
    """End-to-end through a throwaway repo: only ADDED lines may fail the gate."""

    @staticmethod
    def _repo(tmp_path) -> str:
        root = str(tmp_path / "repo")
        os.makedirs(root)
        run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "t")
        os.makedirs(os.path.join(root, "scripts"))
        with open(_SCRIPT_PATH, encoding="utf-8") as src:
            body = src.read()
        dest = os.path.join(root, "scripts", "check_brand_name.py")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(body)
        return root

    @staticmethod
    def _head(root: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

    @staticmethod
    def _run(root: str, base: str | None, **env_extra: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        if base:
            env["BRAND_BASE_REF"] = base
        else:
            env.pop("BRAND_BASE_REF", None)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "check_brand_name.py")],
            cwd=root,
            env=env,
            capture_output=True,
            # The gate writes UTF-8 deliberately. `text=True` would decode with the
            # locale's preferred encoding, which is cp1252 on Windows, and a
            # non-ASCII path in a finding would come back as mojibake.
            encoding="utf-8",
            errors="replace",
        )

    def _commit(self, root: str, name: str, body: str, msg: str) -> None:
        target = os.path.join(root, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(body)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", msg], cwd=root, check=True, capture_output=True)

    def test_preexisting_violation_does_not_fail_the_diff_gate(self, tmp_path) -> None:
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "Old line about KiroCrew.\n", "base")
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        self._commit(root, "doc.md", "Old line about KiroCrew.\nA clean new line.\n", "head")

        result = self._run(root, base)
        assert result.returncode == 0, result.stdout
        assert "no misspellings" in result.stdout

    def test_added_violation_fails_with_the_correct_spelling_in_the_message(
        self, tmp_path
    ) -> None:
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "Nothing to see.\n", "base")
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        self._commit(root, "doc.md", "Nothing to see.\nNow with KiroCrew in it.\n", "head")

        result = self._run(root, base)
        assert result.returncode == 1, result.stdout
        assert "::error::" in result.stdout
        assert "'Kiro Crew'" in result.stdout
        assert "doc.md:2" in result.stdout

    def test_no_base_ref_reports_the_whole_tree_without_failing(self, tmp_path) -> None:
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "Prose about KiroCrew everywhere.\n", "base")

        result = self._run(root, None)
        assert result.returncode == 0, result.stdout
        assert "::notice::" in result.stdout
        assert "doc.md:1" in result.stdout

    # --- the three ways a diff-scoped gate can skip itself green -------------

    def test_a_no_diff_gitattribute_cannot_hide_a_file(self, tmp_path) -> None:
        # `-diff` makes git report only "Binary files differ" with no @@ hunks.
        # Without --text there is nothing to scan and the file passes unread.
        root = self._repo(tmp_path)
        self._commit(root, ".gitattributes", "*.md -diff\n", "base")
        base = self._head(root)
        self._commit(root, "doc.md", "Run KiroCrew today.\n", "head")

        result = self._run(root, base)
        assert result.returncode == 1, result.stdout
        assert "doc.md:1" in result.stdout

    def test_a_non_ascii_path_cannot_hide_a_file(self, tmp_path) -> None:
        # git quotes such a path as `+++ "b/docs/\346..."`, which a `+++ b/` parser
        # misses — and then misattributes the hunks to whichever file came before.
        root = self._repo(tmp_path)
        self._commit(root, "clean.md", "nothing here\n", "base")
        base = self._head(root)
        self._commit(root, "日本語.md", "Run KiroCrew today.\n", "head")

        result = self._run(root, base)
        assert result.returncode == 1, result.stdout
        assert "日本語.md:1" in result.stdout
        assert "clean.md" not in result.stdout

    def test_an_undecodable_changed_file_fails_closed(self, tmp_path) -> None:
        root = self._repo(tmp_path)
        self._commit(root, "clean.md", "nothing here\n", "base")
        base = self._head(root)
        with open(os.path.join(root, "notes.txt"), "wb") as fh:
            fh.write(b"text\n\xff\xfe not utf-8\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "head"], cwd=root, check=True, capture_output=True)

        result = self._run(root, base)
        assert result.returncode == 1, result.stdout
        assert "cannot read" in result.stdout
        assert "notes.txt" in result.stdout

    def test_a_pure_deletion_hunk_contributes_nothing(self, tmp_path) -> None:
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "keep\nKiroCrew line\nkeep\n", "base")
        base = self._head(root)
        self._commit(root, "doc.md", "keep\nkeep\n", "head")

        result = self._run(root, base)
        assert result.returncode == 0, result.stdout

    def test_a_brand_new_file_is_scanned(self, tmp_path) -> None:
        # `@@ -0,0 +1,N @@` — the shape every added file produces.
        root = self._repo(tmp_path)
        self._commit(root, "existing.md", "nothing\n", "base")
        base = self._head(root)
        self._commit(root, "fresh.md", "line one\nRun KiroCrew here.\n", "head")

        result = self._run(root, base)
        assert result.returncode == 1, result.stdout
        assert "fresh.md:2" in result.stdout

    def test_a_base_with_no_common_ancestor_still_enforces(self, tmp_path) -> None:
        # What a shallow CI clone looks like: the base is fetched as its own tip, so
        # `merge-base` finds nothing and the base tip has to serve as the range end.
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "clean\n", "first")
        subprocess.run(
            ["git", "checkout", "-q", "--orphan", "detached"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        self._commit(root, "doc.md", "Now with KiroCrew in it.\n", "orphan")
        assert (
            subprocess.run(
                ["git", "merge-base", "main", "HEAD"], cwd=root, capture_output=True
            ).returncode
            != 0
        ), "precondition: the two histories must share no ancestor"

        result = self._run(root, "main")
        assert result.returncode == 1, result.stdout
        assert "doc.md:1" in result.stdout

    def test_a_translated_catalog_cannot_fail_the_gate(self, tmp_path) -> None:
        root = self._repo(tmp_path)
        os.makedirs(os.path.join(root, "website/src/i18n/locales"))
        self._commit(root, "website/src/i18n/locales/de.json", "{}\n", "base")
        base = self._head(root)
        self._commit(
            root,
            "website/src/i18n/locales/de.json",
            '{\n  "update": "KiroCrew wird aktualisiert"\n}\n',
            "head",
        )

        result = self._run(root, base)
        assert result.returncode == 0, result.stdout

    def test_a_clean_verdict_survives_a_non_utf8_console(self, tmp_path) -> None:
        # The clean verdict ends in a check mark and the error line carries an em
        # dash. A cp1252 console (the Windows default) cannot encode the check mark,
        # which turned a PASS into a traceback and failed the build on a good tree.
        # PYTHONIOENCODING reproduces that on any platform.
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "nothing here\n", "base")
        base = self._head(root)
        self._commit(root, "doc.md", "nothing here\nstill clean\n", "head")

        result = self._run(root, base, PYTHONIOENCODING="ascii")
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "UnicodeEncodeError" not in result.stderr
        assert "no misspellings" in result.stdout

    def test_a_non_ascii_path_is_printable_on_a_non_utf8_console(self, tmp_path) -> None:
        root = self._repo(tmp_path)
        self._commit(root, "clean.md", "nothing here\n", "base")
        base = self._head(root)
        self._commit(root, "日本語.md", "Run KiroCrew today.\n", "head")

        result = self._run(root, base, PYTHONIOENCODING="ascii")
        assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "UnicodeEncodeError" not in result.stderr
        # The path itself may be replacement-charactered, but the finding must print.
        assert ".md:1" in result.stdout

    def test_self_test_mode_passes(self, tmp_path) -> None:
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "clean\n", "base")
        result = subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "check_brand_name.py"), "--test"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stdout
        assert "self-test passed" in result.stdout

    def test_self_test_actually_judges_the_growth_ratio(self, tmp_path) -> None:
        """The repeated-brands check must reach a verdict, not skip itself.

        Its baseline used to be a fixed 20k brands, which costs fast hardware
        ~19-21ms against a 20ms measurement floor. Landing under the floor made
        the check print `ok ... ratio not judged` and test nothing, so on a fast
        machine the quadratic guard was a coin-flip no-op. The workload now grows
        until the baseline is measurable, so a real ratio is always reported.
        """
        root = self._repo(tmp_path)
        self._commit(root, "doc.md", "clean\n", "base")
        result = subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "check_brand_name.py"), "--test"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stdout
        verdict = [ln for ln in result.stdout.splitlines() if "repeated-brands" in ln]
        assert len(verdict) == 1, f"expected one repeated-brands line, got {verdict!r}"
        assert "ratio not judged" not in verdict[0], (
            f"the quadratic guard skipped its own measurement: {verdict[0]!r}"
        )
        assert "doubling cost" in verdict[0], verdict[0]


class TestExplicitPathFailsClosed:
    """A path handed to the gate directly must be read, or the run must fail.

    The diff-scoped path already refuses to pass on an unreadable file. The
    explicit-path branch did not, so a typo'd or moved argument printed the
    success line and exited 0 — a false green that looks exactly like a real one.
    """

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop("BRAND_BASE_REF", None)
        return subprocess.run(
            [sys.executable, _SCRIPT_PATH, *args],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_a_nonexistent_path_fails_instead_of_reporting_clean(self) -> None:
        result = self._run("definitely/not/a/file.md")
        assert result.returncode == 1, result.stdout
        assert "never checked" in result.stdout
        assert "definitely/not/a/file.md" in result.stdout
        assert "no misspellings" not in result.stdout

    def test_an_undecodable_path_fails_closed(self, tmp_path) -> None:
        blob = tmp_path / "notes.md"
        blob.write_bytes(b"\xff\xfe\x00KiroCrew")
        result = self._run(str(blob))
        assert result.returncode == 1, result.stdout
        assert "never checked" in result.stdout

    def test_a_readable_clean_path_still_passes(self, tmp_path) -> None:
        doc = tmp_path / "clean.md"
        doc.write_text("Prose about Kiro Crew.\n", encoding="utf-8")
        result = self._run(str(doc))
        assert result.returncode == 0, result.stdout
        assert "no misspellings" in result.stdout

    def test_a_readable_dirty_path_still_fails_on_the_finding(self, tmp_path) -> None:
        doc = tmp_path / "dirty.md"
        doc.write_text("Prose about KiroCrew.\n", encoding="utf-8")
        result = self._run(str(doc))
        assert result.returncode == 1, result.stdout
        assert "dirty.md:1" in result.stdout
        # Failing on the finding, not on readability.
        assert "never checked" not in result.stdout


class TestReportDisclosesTruncation:
    """The whole-tree report must not hide the backlog it claims to report.

    The listing is path-sorted, so a silent cut showed only the alphabetically
    first paths. That is how UI-visible strings under src/ and website/ stayed
    invisible while the report cheerfully printed a four-digit total.
    """

    def test_a_truncated_report_says_so_and_tallies_by_path(self, capsys) -> None:
        violations = [
            gate.Violation(f"{top}/f{i}.md", 1, "KiroCrew", "KiroCrew")
            # 'zzz' sorts last, so a silent head-slice would drop it entirely.
            for top, count in (("aaa", 40), ("zzz", 5))
            for i in range(count)
        ]
        assert gate.report(violations, enforcing=False, base=None) == 0
        out = capsys.readouterr().out

        assert "... and 5 more" in out
        # The tally must carry the paths the listing could not reach.
        assert "zzz/" in out
        assert "   40  aaa/" in out
        assert "    5  zzz/" in out
        assert "all 45 lines" in out

    def test_an_untruncated_report_adds_no_tally_or_notice(self, capsys) -> None:
        violations = [gate.Violation("a.md", 1, "KiroCrew", "KiroCrew")]
        assert gate.report(violations, enforcing=False, base=None) == 0
        out = capsys.readouterr().out
        assert "more" not in out
        assert "by top-level path" not in out
        assert "a.md:1" in out

    def test_the_enforcing_path_keeps_its_own_cap_and_notice(self, capsys) -> None:
        violations = [
            gate.Violation(f"f{i}.md", 1, "KiroCrew", "KiroCrew") for i in range(205)
        ]
        assert gate.report(violations, enforcing=True, base="HEAD") == 1
        out = capsys.readouterr().out
        assert "... and 5 more" in out
        # The per-path tally is a report-path affordance; enforcement stays terse.
        assert "by top-level path" not in out

    def test_a_root_level_path_is_tallied_without_a_trailing_slash(self, capsys) -> None:
        violations = [
            gate.Violation("install.sh" if i % 2 else "docs/a.md", 1, "KiroCrew", "KiroCrew")
            for i in range(50)
        ]
        assert gate.report(violations, enforcing=False, base=None) == 0
        out = capsys.readouterr().out
        assert "install.sh" in out
        assert "install.sh/" not in out
        assert "docs/" in out
