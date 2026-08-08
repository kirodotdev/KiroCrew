"""Tests for GET /api/skills/-/budget (skill context budget endpoint)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from kiro_crew.skill_usage import SkillUsageLedger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(tmp_path: Path, key: str, body: str = "# Skill\nHello") -> Path:
    """Create a SKILL.md under tmp_path/<key>/SKILL.md and return the file.

    Writes UTF-8 explicitly: bare ``write_text`` uses the platform's locale
    encoding, which is cp1252 on the Windows runners, so a non-ASCII body was
    written as cp1252 and then failed to decode as UTF-8 the way production reads
    it. The fixture must encode the way the code under test decodes.
    """
    d = tmp_path / key
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(body, encoding="utf-8")
    return f


def _make_ledger(tmp_path: Path, keys: dict[str, tuple[int, float]]) -> SkillUsageLedger:
    """Build a ledger pre-populated with hits/last_seen per key."""
    path = tmp_path / "skill-usage.json"
    payload = {
        "version": 1,
        "keys": {k: {"hits": h, "last_seen": ls} for k, (h, ls) in keys.items()},
    }
    path.write_text(json.dumps(payload))
    return SkillUsageLedger(path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAliasFold:
    """Two ledger keys resolving to one file are folded."""

    def test_fold_sums_deliveries_and_reports_alias(self, tmp_path):
        """Symlinked alias key's hits are added to the canonical key."""
        from kiro_crew.dashboard.handlers.skill_budget import (
            _compute_budget,
        )

        # Create the canonical skill file.
        canonical_file = _make_skill(tmp_path, "ns/pod-e2e", "# Pod\nbody here!!")

        # Create a symlink at the root level (the alias path).
        alias_dir = tmp_path / "pod-e2e"
        alias_dir.mkdir()
        alias_skill = alias_dir / "SKILL.md"
        os.symlink(str(canonical_file), str(alias_skill))

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "ns/pod-e2e": (262, now - 3600),
            "pod-e2e": (52, now - 7200),
        })

        # Build a minimal SkillsLoader mock.
        skill_pairs = [("ns/pod-e2e", canonical_file)]

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return skill_pairs

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Pod E2e"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row["key"] == "ns/pod-e2e"
        assert row["deliveries"] == 314  # 262 + 52
        assert row["folded_from"] == ["pod-e2e"]
        # Cost is characters, not bytes. Asserting against size_bytes passed only
        # where the two happen to coincide -- and never on Windows, where git
        # checks out CRLF so st_size exceeds the decoded character count.
        char_len = len(canonical_file.read_text(encoding="utf-8"))
        assert row["chars"] == char_len * 314

    def test_unresolvable_ledger_key_is_dropped(self, tmp_path):
        """A ledger key whose SKILL.md doesn't exist is not folded."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        canonical_file = _make_skill(tmp_path, "real-skill", "# Real\ncontent")

        now = time.time()
        # "ghost-skill" has no SKILL.md on disk at all.
        ledger = _make_ledger(tmp_path, {
            "real-skill": (10, now - 100),
            "ghost-skill": (999, now - 50),
        })

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("real-skill", canonical_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Real Skill"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        # The ghost key must NOT appear in deliveries or folded_from.
        assert row["deliveries"] == 10
        assert "folded_from" not in row

    def test_untracked_skill_gives_null_deliveries(self, tmp_path):
        """A skill with no ledger entry yields deliveries=None, not 0."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        skill_file = _make_skill(tmp_path, "brand-new", "# New\nstuff")

        # Empty ledger — no keys at all.
        ledger = _make_ledger(tmp_path, {})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("brand-new", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Brand New"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        assert row["deliveries"] is None
        assert row["idle_days"] is None
        assert row["chars"] == 0

    def test_chars_arithmetic(self, tmp_path):
        """chars = character length * deliveries (NOT byte size)."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        body = "x" * 500
        skill_file = _make_skill(tmp_path, "measured", body)

        now = time.time()
        ledger = _make_ledger(tmp_path, {"measured": (7, now - 60)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("measured", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Measured"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        assert row["chars"] == len(skill_file.read_text(encoding="utf-8")) * 7
        assert row["deliveries"] == 7

    def test_total_chars_equals_row_sum(self, tmp_path):
        """total_chars is the sum of all row chars."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        f1 = _make_skill(tmp_path, "alpha", "aaa")
        f2 = _make_skill(tmp_path, "beta", "bbbbb")

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "alpha": (3, now),
            "beta": (2, now),
        })

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("alpha", f1), ("beta", f2)]

            def _cached_frontmatter(self, path, mtime=None):
                return {}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        row_sum = sum(r["chars"] for r in result["rows"])
        assert result["total_chars"] == row_sum

    def test_unreadable_ledger_degrades_gracefully(self, tmp_path):
        """When ledger is None, all deliveries are None and no crash."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        skill_file = _make_skill(tmp_path, "orphan", "# Hi")

        class FakeLoader:
            _usage = None  # Ledger unavailable
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("orphan", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Orphan"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        assert result["window_days"] == 30
        row = result["rows"][0]
        assert row["deliveries"] is None
        assert row["idle_days"] is None
        assert row["chars"] == 0
        assert result["total_chars"] == 0


class TestNestedFirstSkillBaseDir:
    """Regression: nested first skill must NOT break alias resolution (item 1)."""

    def test_nested_first_skill_still_resolves_aliases(self, tmp_path):
        """When the alphabetically-FIRST skill key contains '/' (nested),
        the alias fold still resolves correctly because the loader's _dir
        is used, not inferred from the first skill's path.
        """
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        # "apps/deploy" is alphabetically FIRST and is NESTED.
        nested_file = _make_skill(tmp_path, "apps/deploy", "# Deploy\napp deploy")
        # "zebra" is a non-nested skill.
        zebra_file = _make_skill(tmp_path, "zebra", "# Zebra\nzebra content")

        # Create a symlink alias: "old-deploy" -> same file as "apps/deploy"
        alias_dir = tmp_path / "old-deploy"
        alias_dir.mkdir()
        alias_skill = alias_dir / "SKILL.md"
        os.symlink(str(nested_file), str(alias_skill))

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "apps/deploy": (100, now - 100),
            "old-deploy": (50, now - 200),
            "zebra": (10, now - 50),
        })

        # Pairs sorted alphabetically — "apps/deploy" comes FIRST.
        skill_pairs = [("apps/deploy", nested_file), ("zebra", zebra_file)]

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return skill_pairs

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Deploy" if "deploy" in str(path) else "Zebra"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        # Find the apps/deploy row — the alias must be folded in.
        deploy_row = next(r for r in result["rows"] if r["key"] == "apps/deploy")
        assert deploy_row["deliveries"] == 150  # 100 + 50
        assert deploy_row["folded_from"] == ["old-deploy"]


class TestAliasUnderExtraPath:
    """An app's skills dir is an extra path, not a subdir of the main skills dir.

    `_iter()` serves the main dir AND every extra path, naming each skill
    relative to its OWN root, so an app skill's alias key only resolves under
    that app's root. Resolving candidates against `_dir` alone drops every
    app-skill alias -- the real-world case being a renamed built-in app skill
    whose old ledger key still holds hundreds of deliveries.
    """

    def test_alias_resolving_only_under_an_extra_path_still_folds(self, tmp_path):
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        main_dir = tmp_path / "skills"
        main_dir.mkdir()
        app_dir = tmp_path / "app-skills"
        app_dir.mkdir()

        # The served skill lives under the APP root, not the main skills dir.
        served_file = _make_skill(app_dir, "pod-e2e", "# Pod E2E\nbody")
        # Its old ledger key resolves to the same file, also under the app root.
        alias_dir = app_dir / "old-pod-e2e"
        alias_dir.mkdir()
        os.symlink(str(served_file), str(alias_dir / "SKILL.md"))

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "pod-e2e": (291, now - 100),
            "old-pod-e2e": (51, now - 500),
        })

        class FakeLoader:
            _usage = ledger
            _dir = main_dir
            _extra_paths: list = [app_dir]
            _alias_cache = None

            def _iter(self):
                return [("pod-e2e", served_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Pod E2E"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())
        row = next(r for r in result["rows"] if r["key"] == "pod-e2e")
        assert row["deliveries"] == 342, "the app-skill alias's hits were dropped"
        assert row["folded_from"] == ["old-pod-e2e"]


class TestServedAliasIsFolded:
    """A file-level symlink leaves BOTH directories real, so `_iter()` serves both
    keys for one file. Excluding every served key from folding splits that file's
    cost across two rows and lists it twice -- the exact confusion the fold
    exists to remove.
    """

    def test_two_served_keys_for_one_file_yield_one_folded_row(self, tmp_path):
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        real_file = _make_skill(tmp_path, "new-name", "# Skill\nbody")
        alias_dir = tmp_path / "old-name"
        alias_dir.mkdir()
        alias_file = alias_dir / "SKILL.md"
        os.symlink(str(real_file), str(alias_file))

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "new-name": (100, now - 100),
            "old-name": (40, now - 900),
        })
        # Both are served -- this is what _iter_skill_files really returns here.
        skill_pairs = [("new-name", real_file), ("old-name", alias_file)]

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return skill_pairs

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Skill"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        keys = [r["key"] for r in result["rows"]]
        assert keys == ["new-name"], f"one file must yield one row, got {keys}"
        row = result["rows"][0]
        assert row["deliveries"] == 140, "the served alias's hits were not folded"
        assert row["folded_from"] == ["old-name"]

    def test_the_real_file_wins_over_the_symlink_regardless_of_order(self, tmp_path):
        """Canonical choice must not depend on directory iteration order."""
        from kiro_crew.skills import SkillsLoader

        real_file = _make_skill(tmp_path, "zzz-real", "# Skill\nbody")
        alias_dir = tmp_path / "aaa-alias"
        alias_dir.mkdir()
        alias_file = alias_dir / "SKILL.md"
        os.symlink(str(real_file), str(alias_file))

        now = time.time()
        ledger = _make_ledger(tmp_path, {"zzz-real": (10, now), "aaa-alias": (5, now)})

        # Alphabetically the symlink sorts FIRST; the real file must still win.
        for pairs in (
            [("aaa-alias", alias_file), ("zzz-real", real_file)],
            [("zzz-real", real_file), ("aaa-alias", alias_file)],
        ):
            class FakeLoader:
                _usage = ledger
                _dir = tmp_path
                _extra_paths: list = []
                _alias_cache = None

                def _iter(self, _p=pairs):
                    return _p

            got = SkillsLoader.resolve_ledger_aliases(FakeLoader())
            assert got == {"zzz-real": ["aaa-alias"]}, f"order changed the winner: {got}"


class TestFrontmatterFailurePolicy:
    """An unreadable SKILL.md must degrade for READERS and raise for WRITERS.

    `update_auto_skill` reads frontmatter to carry `created_at`, `version`,
    `pinned` and `inject_on_trigger` across a rewrite. Degrading to "no metadata"
    inside the shared loader helper would make that path silently drop them and
    clobber a version snapshot -- turning a loud failure into data loss. So the
    loader propagates, and the read-only budget endpoint catches at its own call
    site.
    """

    def test_the_loader_propagates_so_writers_abort(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        bad = bad_dir / "SKILL.md"
        bad.write_bytes(b"---\nname: B\xff\xfead\n---\nbody")  # invalid UTF-8

        class FakeLoader:
            _fm_cache: dict = {}
            _parse_frontmatter = staticmethod(SkillsLoader._parse_frontmatter)

        with pytest.raises(UnicodeDecodeError):
            SkillsLoader._cached_frontmatter(FakeLoader(), bad)

    def test_nothing_is_cached_for_a_failed_read(self, tmp_path):
        """A propagated failure must not leave a poisoned mtime-keyed entry."""
        from kiro_crew.skills import SkillsLoader

        bad_dir = tmp_path / "bad2"
        bad_dir.mkdir()
        bad = bad_dir / "SKILL.md"
        bad.write_bytes(b"---\nname: B\xff\xfead\n---\nbody")

        class FakeLoader:
            _fm_cache: dict = {}
            _parse_frontmatter = staticmethod(SkillsLoader._parse_frontmatter)

        loader = FakeLoader()
        with pytest.raises(UnicodeDecodeError):
            SkillsLoader._cached_frontmatter(loader, bad)
        assert loader._fm_cache == {}, "a failed read must not be cached"

    def test_a_successful_parse_is_still_cached(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        skill = _make_skill(tmp_path, "stable", "---\nname: Stable\n---\nbody")
        calls: list[int] = []

        class FakeLoader:
            _fm_cache: dict = {}

            @staticmethod
            def _parse_frontmatter(path):
                calls.append(1)
                return SkillsLoader._parse_frontmatter(path)

        loader = FakeLoader()
        SkillsLoader._cached_frontmatter(loader, skill)
        SkillsLoader._cached_frontmatter(loader, skill)
        assert len(calls) == 1, "a successful parse should be cached, not re-read"


class TestUndecodableSkillDoesNotCrash:
    """A SKILL.md that is not valid UTF-8 must not take down the whole endpoint.

    Frontmatter is best-effort metadata for every caller, so one undecodable file
    degrades to "no metadata" rather than raising UnicodeDecodeError out of
    read_text and turning the request into a 500.
    """

    def test_non_utf8_skill_still_returns_rows(self, tmp_path):
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget
        from kiro_crew.skills import SkillsLoader

        good = _make_skill(tmp_path, "good", "---\nname: Good\n---\nbody")
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        bad = bad_dir / "SKILL.md"
        bad.write_bytes(b"---\nname: B\xff\xfead\n---\nbody")  # invalid UTF-8

        now = time.time()
        ledger = _make_ledger(tmp_path, {"good": (5, now), "bad": (3, now)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []
            _alias_cache = None
            _fm_cache: dict = {}

            def _iter(self):
                return [("bad", bad), ("good", good)]

            def _cached_frontmatter(self, path, mtime=None):
                return SkillsLoader._cached_frontmatter(self, path, mtime)

            _parse_frontmatter = staticmethod(SkillsLoader._parse_frontmatter)

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                return {}

        result = _compute_budget(FakeLoader())
        keys = sorted(r["key"] for r in result["rows"])
        assert keys == ["bad", "good"], "the undecodable skill broke the listing"


class TestSymlinkLoopDoesNotCrash:
    """`Path.resolve()` raises RuntimeError -- NOT OSError -- on a cyclic symlink
    (verified on CPython 3.12: RuntimeError("Symlink loop from ...")). Catching
    only OSError lets one bad link turn every budget request into a 500.
    """

    def test_cyclic_symlink_is_skipped_not_raised(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        good = _make_skill(tmp_path, "good", "# Good\nbody")

        # loop/ -> other/ -> loop/, so loop/SKILL.md cannot be resolved.
        loop = tmp_path / "loop"
        other = tmp_path / "other"
        os.symlink(str(other), str(loop))
        os.symlink(str(loop), str(other))

        now = time.time()
        ledger = _make_ledger(tmp_path, {"good": (7, now), "loop": (3, now)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("good", good), ("loop", loop / "SKILL.md")]

        # Must return, not raise.
        got = SkillsLoader.resolve_ledger_aliases(FakeLoader())
        assert got == {}, f"a symlink loop should fold nothing, got {got}"


class TestCacheCoversTheFilesystem:
    """The alias map depends on the ledger AND on which files are served, so a
    cache keyed on the ledger alone goes stale in a reachable way: deleting an
    alias leaves the ledger untouched, and the stale map keeps folding hits from
    a file that no longer exists.
    """

    def test_removing_a_served_alias_invalidates_the_cache(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        real_file = _make_skill(tmp_path, "canonical", "# Skill\nbody")
        alias_dir = tmp_path / "alias"
        alias_dir.mkdir()
        alias_file = alias_dir / "SKILL.md"
        os.symlink(str(real_file), str(alias_file))

        now = time.time()
        # The ledger keeps BOTH keys throughout -- only the filesystem changes.
        ledger = _make_ledger(tmp_path, {"canonical": (50, now), "alias": (20, now)})

        pairs = [("canonical", real_file), ("alias", alias_file)]

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return list(pairs)

        loader = FakeLoader()
        first = SkillsLoader.resolve_ledger_aliases(loader)
        assert first == {"canonical": ["alias"]}

        # The alias is gone from disk and from the served list; the LEDGER is
        # unchanged, which is exactly what the old cache key could not see.
        alias_file.unlink()
        pairs.pop()

        second = SkillsLoader.resolve_ledger_aliases(loader)
        assert second == {}, f"stale fold survived the alias's removal: {second}"


class TestNameIsRedacted:
    """An auto-skill's frontmatter is LLM-authored, so its `name` is untrusted
    text on its way to dashboard JSON. Credential-shaped content must not survive.
    """

    def test_credential_shaped_name_does_not_reach_the_response(self, tmp_path):
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget
        from kiro_crew.security import redact_credentials

        # Only meaningful if the redactor actually matches this shape -- assert
        # that first, so the test cannot pass by redacting nothing.
        planted = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        cleaned, warnings = redact_credentials(planted)
        assert cleaned != planted and warnings, (
            "fixture is not credential-shaped for this redactor; pick another"
        )

        skill = _make_skill(tmp_path, "leaky", "# Leaky\nbody")
        now = time.time()
        ledger = _make_ledger(tmp_path, {"leaky": (3, now)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []

            def _iter(self):
                return [("leaky", skill)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": planted}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                return {}

        result = _compute_budget(FakeLoader())
        row = result["rows"][0]
        assert row["name"] != planted, "the raw credential reached the response"
        assert "wJalrXUtnFEMI" not in row["name"], f"secret survived: {row['name']!r}"

    def test_an_ordinary_name_is_left_alone(self, tmp_path):
        """Redaction must not mangle legitimate skill names."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        skill = _make_skill(tmp_path, "normal", "# Normal\nbody")
        now = time.time()
        ledger = _make_ledger(tmp_path, {"normal": (3, now)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []

            def _iter(self):
                return [("normal", skill)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "kirocrew-worktree-dev"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                return {}

        row = _compute_budget(FakeLoader())["rows"][0]
        assert row["name"] == "kirocrew-worktree-dev"


class TestCostIsCharactersNotBytes:
    """The screen is denominated in characters; `st_size` is bytes.

    A skill with non-ASCII prose encodes to more UTF-8 bytes than it has
    characters, so costing it by file size inflates that skill's spend and
    misranks the table against ASCII-only skills.
    """

    def test_non_ascii_skill_is_costed_by_characters(self, tmp_path):
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        # Every em-dash is 3 UTF-8 bytes, so bytes >> chars here. (Deliberately
        # not CJK: a pre-commit hook forbids Chinese characters in tests.)
        body = "---\nname: Dashes\n---\n" + ("\u2014" * 100)
        skill = _make_skill(tmp_path, "dashes", body)
        byte_size = skill.stat().st_size
        char_size = len(skill.read_text(encoding="utf-8"))
        assert byte_size > char_size, "fixture must actually have multibyte content"

        now = time.time()
        ledger = _make_ledger(tmp_path, {"dashes": (10, now)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []

            def _iter(self):
                return [("dashes", skill)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Dashes"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                return {}

        result = _compute_budget(FakeLoader())
        row = next(r for r in result["rows"] if r["key"] == "dashes")

        assert row["chars"] == char_size * 10, (
            f"cost used bytes, not characters: {row['chars']} "
            f"(bytes would give {byte_size * 10})"
        )
        # size_bytes remains the FILE SIZE -- that field is about bytes.
        assert row["size_bytes"] == byte_size
        assert result["total_chars"] == char_size * 10


class TestAliasCacheInvalidation:
    """The map is recomputed per call, because caching it soundly is impossible.

    Its value depends on what each served path RESOLVES to, and no cheap key
    captures that: deleting an alias or retargeting a served symlink changes no
    key name, so a name-keyed cache kept crediting deliveries to the wrong skill.
    """

    def test_a_retargeted_symlink_is_reflected_immediately(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        first = _make_skill(tmp_path, "first", "# First")
        second = _make_skill(tmp_path, "second", "# Second")

        alias_dir = tmp_path / "alias"
        alias_dir.mkdir()
        alias_link = alias_dir / "SKILL.md"
        os.symlink(str(first), str(alias_link))

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "first": (10, now), "second": (20, now), "alias": (7, now),
        })

        pairs = [("first", first), ("second", second)]

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path
            _extra_paths: list = []

            def _iter(self):
                return list(pairs)

        loader = FakeLoader()
        assert SkillsLoader.resolve_ledger_aliases(loader) == {"first": ["alias"]}

        # Retarget the symlink. No key name changes anywhere -- which is exactly
        # what a name-keyed cache could not see.
        alias_link.unlink()
        os.symlink(str(second), str(alias_link))

        got = SkillsLoader.resolve_ledger_aliases(loader)
        assert got == {"second": ["alias"]}, (
            f"a retargeted alias was still credited to the old skill: {got}"
        )

    def test_cache_invalidates_on_key_set_change(self, tmp_path):
        """Adding a ledger key invalidates the cache."""
        from kiro_crew.skills import SkillsLoader

        skill_file = _make_skill(tmp_path, "alpha", "# A")
        alias_dir = tmp_path / "old-alpha"
        alias_dir.mkdir()
        os.symlink(str(skill_file), str(alias_dir / "SKILL.md"))

        now = time.time()
        ledger_path = tmp_path / "skill-usage.json"

        # Start with one key.
        payload1 = {"version": 1, "keys": {"alpha": {"hits": 5, "last_seen": now}}}
        ledger_path.write_text(json.dumps(payload1))
        ledger = SkillUsageLedger(ledger_path)

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("alpha", skill_file)]

        loader = FakeLoader()
        r1 = SkillsLoader.resolve_ledger_aliases(loader)
        # Expect no aliases (only canonical key in ledger).
        assert r1 == {}

        # Now add "old-alpha" to the ledger (simulating a new record).
        ledger.record("old-alpha")
        ledger.flush()

        # The key set changed, so cache must be invalidated.
        r2 = SkillsLoader.resolve_ledger_aliases(loader)
        # Now "old-alpha" should fold into "alpha".
        assert "alpha" in r2
        assert "old-alpha" in r2["alpha"]
        # Must NOT be the same object (cache was invalidated).
        assert r1 is not r2


class TestAlwaysTrueSkillCost:
    """Regression: always:true skills must NOT report 0 cost (item 4)."""

    def test_always_true_reports_chars_none(self, tmp_path):
        """A skill with always:true has chars=None (unmeasurable), not 0."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        skill_file = _make_skill(tmp_path, "core-skill", "# Core\nbody content here")

        # Even with zero ledger entries, an always:true skill should not say 0.
        ledger = _make_ledger(tmp_path, {})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("core-skill", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Core Skill", "always": "true"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        assert row["always"] is True
        # chars must be None (unmeasurable), NOT 0.
        assert row["chars"] is None
        # It must not contribute to total_chars.
        assert result["total_chars"] == 0

    def test_always_true_with_deliveries_still_none_chars(self, tmp_path):
        """Even with recorded deliveries, always:true chars stays None."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        skill_file = _make_skill(tmp_path, "pinned", "# Pinned\nlots of content")

        now = time.time()
        # Someone manually recorded hits (edge case, shouldn't happen normally).
        ledger = _make_ledger(tmp_path, {"pinned": (42, now - 60)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("pinned", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Pinned", "always": "true"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        assert row["always"] is True
        assert row["deliveries"] == 42  # still reported honestly
        assert row["chars"] is None  # but cost is unmeasurable
        assert result["total_chars"] == 0

    def test_mixed_always_and_regular_total_chars(self, tmp_path):
        """total_chars only includes regular skills, not always:true."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        always_file = _make_skill(tmp_path, "always-skill", "x" * 100)
        regular_file = _make_skill(tmp_path, "regular", "y" * 200)

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "always-skill": (10, now),
            "regular": (5, now),
        })

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            _extra_paths: list = []
            _alias_cache = None

            def _iter(self):
                return [("always-skill", always_file), ("regular", regular_file)]

            def _cached_frontmatter(self, path, mtime=None):
                if "always-skill" in str(path):
                    return {"name": "Always", "always": "true"}
                return {"name": "Regular"}

            def _owned_hint(self, path):
                return True

            def resolve_ledger_aliases(self):
                from kiro_crew.skills import SkillsLoader
                return SkillsLoader.resolve_ledger_aliases(self)

        result = _compute_budget(FakeLoader())

        always_row = next(r for r in result["rows"] if r["key"] == "always-skill")
        regular_row = next(r for r in result["rows"] if r["key"] == "regular")

        assert always_row["chars"] is None
        assert regular_row["chars"] == regular_file.stat().st_size * 5
        # total_chars only counts the regular skill.
        assert result["total_chars"] == regular_row["chars"]


class TestSnapshotMethod:
    """SkillUsageLedger.snapshot() returns a consistent copy."""

    def test_snapshot_returns_all_keys(self, tmp_path):
        ledger = _make_ledger(tmp_path, {
            "a": (5, time.time()),
            "b": (3, time.time() - 100),
        })
        snap = ledger.snapshot()
        assert set(snap.keys()) == {"a", "b"}
        assert snap["a"][0] == 5
        assert snap["b"][0] == 3

    def test_snapshot_empty_ledger(self, tmp_path):
        ledger = _make_ledger(tmp_path, {})
        assert ledger.snapshot() == {}
