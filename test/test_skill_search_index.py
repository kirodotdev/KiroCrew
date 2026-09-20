"""Skill discovery at scale — the term index and the family line.

Two properties are pinned here, because both are invisible at a few dozen skills
and decisive at a thousand:

* ``search_skills``'s body fallback answers from the persisted term index, so a
  repeated query reads no ``SKILL.md`` at all, and an unusable index still returns
  the same skills through the original reader.
* The default discovery entry names the FAMILIES the eight listed skills leave
  out, so the tail is reachable by a query the model can form instead of guess.

Everything stays under ``tmp_path``: the index file is resolved from the skills
root's parent, which the fixtures own.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from kiro_crew import skill_search_index as skill_search_index_module
from kiro_crew.skill_search_index import (
    SKILL_SEARCH_INDEX_FILENAME,
    SkillSearchIndex,
    body_fingerprint,
    prefix_upper_bound,
)
from kiro_crew.skills import SkillsLoader, _namespace_groups

pytestmark = pytest.mark.xdist_group("skill_search_index")


def _skill(root: Path, key: str, *, description: str = "nothing relevant here", body: str) -> Path:
    path = root / key / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {key.split('/')[-1]}\ndescription: {description}\n---\n# H\n{body}\n",
        encoding="utf-8",
    )
    return path


def _loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


@pytest.fixture
def small_ceiling(monkeypatch):
    """Reach the index's size ceiling without writing a megabyte per test.

    The branch under test is "this body is too large to index, read it directly",
    which a 2 KB ceiling reaches exactly as the shipped one does. Five megabyte
    fixtures in one file is real cost in a shared xdist worker, paid on every run
    for no extra coverage. The shipped value is documented in
    `docs/system-specs/modules/memory-skills-hooks.md`.
    """
    monkeypatch.setattr(skill_search_index_module, "_MAX_INDEXED_BODY_BYTES", 2_000)
    return 2_000


class TestBodyIndex:
    def test_invalid_persisted_metadata_is_reparsed(self, tmp_path):
        import sqlite3

        _skill(tmp_path / "skills", "repair", description="zirconium", body="procedure")
        loader = _loader(tmp_path)
        loader.list_skills()
        with sqlite3.connect(tmp_path / SKILL_SEARCH_INDEX_FILENAME) as db:
            db.execute("UPDATE skill_metadata SET metadata = '[]'")
        assert _loader(tmp_path).search_skills("zirconium")[0]["key"] == "repair"

    def test_metadata_write_failure_retains_search_recall(self, tmp_path, monkeypatch):
        _skill(tmp_path / "skills", "repair", description="zirconium", body="procedure")
        loader = _loader(tmp_path)
        monkeypatch.setattr(loader._search_index, "store_metadata", lambda rows: False)
        monkeypatch.setattr(loader, "_body_matches", lambda *args: {})
        assert loader.search_skills("zirconium")[0]["key"] == "repair"

    def test_external_mapping_body_fallback_without_index(self, tmp_path):
        path = _skill(tmp_path / "external", "repair", body="zirconium procedure")
        loader = _loader(tmp_path)
        loader._search_index = None
        rows = loader.search_skills("zirconium", only=[str(path)])
        assert len(rows) == 1 and rows[0]["key"].startswith("mapped/")

    def test_repeated_body_search_reads_no_files(self, tmp_path, monkeypatch):
        """The second identical query must not touch a single body file.

        This is the whole point of the index: without it the metadata-miss path
        reads every skill on the machine, on every call, so its cost grows with
        the corpus instead of with the query.
        """
        skills_dir = tmp_path / "skills"
        for n in range(5):
            _skill(skills_dir, f"s{n}", body=f"This mentions kubernetes and topic{n} internally.")
        loader = _loader(tmp_path)

        reads: list[str] = []
        original = SkillsLoader.load_skill

        def counting(self, key, project_dir=None, **kwargs):
            reads.append(str(key))
            return original(self, key, project_dir, **kwargs)

        monkeypatch.setattr(SkillsLoader, "load_skill", counting)

        first = [h["key"] for h in loader.search_skills("kubernetes")]
        assert sorted(first) == ["s0", "s1", "s2", "s3", "s4"]
        reads.clear()
        second = [h["key"] for h in loader.search_skills("kubernetes")]
        assert second == first
        assert reads == []

    def test_index_file_lands_beside_the_skills_root(self, tmp_path):
        _skill(tmp_path / "skills", "s1", body="This mentions kubernetes internally.")
        loader = _loader(tmp_path)
        assert [h["key"] for h in loader.search_skills("kubernetes")] == ["s1"]
        assert (tmp_path / SKILL_SEARCH_INDEX_FILENAME).exists()

    def test_edited_body_is_reindexed(self, tmp_path):
        """A changed fingerprint must re-read, or a skill keeps matching its old text."""
        skills_dir = tmp_path / "skills"
        path = _skill(skills_dir, "s1", body="This mentions kubernetes internally.")
        loader = _loader(tmp_path)
        assert [h["key"] for h in loader.search_skills("kubernetes")] == ["s1"]

        path.write_text(
            "---\nname: s1\ndescription: nothing relevant here\n---\n# H\nNow about terraform.\n",
            encoding="utf-8",
        )
        loader._invalidate_iter_cache()
        assert [h["key"] for h in loader.search_skills("terraform")] == ["s1"]
        assert loader.search_skills("kubernetes") == []

    def test_terms_after_index_ceiling_use_full_body_fallback(self, tmp_path, small_ceiling):
        """A global skill remains searchable beyond the index's memory ceiling."""
        body = "x" * (small_ceiling + 1)
        _skill(tmp_path / "skills", "s1", body=f"{body} tailneedle")
        loader = _loader(tmp_path)

        assert [h["key"] for h in loader.search_skills("tailneedle")] == ["s1"]

    def test_prefix_match_still_finds_a_longer_body_word(self, tmp_path):
        """``deploy`` must still reach a body that only says ``deployment``."""
        _skill(tmp_path / "skills", "s1", body="Covers the deployment pipeline.")
        loader = _loader(tmp_path)
        assert [h["key"] for h in loader.search_skills("deploy")] == ["s1"]

    def test_unusable_index_falls_back_to_reading_bodies(self, tmp_path, monkeypatch):
        """Same results with the database refusing to open — the negative control.

        A read-only home or a held lock must cost the search nothing but speed.
        """
        _skill(tmp_path / "skills", "s1", body="This mentions kubernetes internally.")
        loader = _loader(tmp_path)
        monkeypatch.setattr(SkillSearchIndex, "_db", lambda self: None)
        assert [h["key"] for h in loader.search_skills("kubernetes")] == ["s1"]

    def test_missing_index_object_falls_back(self, tmp_path):
        _skill(tmp_path / "skills", "s1", body="This mentions kubernetes internally.")
        loader = _loader(tmp_path)
        loader._search_index = None
        assert [h["key"] for h in loader.search_skills("kubernetes")] == ["s1"]

    def test_metadata_hit_outranks_a_body_hit(self, tmp_path):
        """Ranking is unchanged: metadata is worth ten body hits, as before."""
        skills_dir = tmp_path / "skills"
        _skill(skills_dir, "meta", description="kubernetes rollout", body="unrelated prose")
        _skill(skills_dir, "body", body="kubernetes " * 20)
        loader = _loader(tmp_path)
        assert [h["key"] for h in loader.search_skills("kubernetes")][0] == "meta"


class TestIndexUnit:
    def test_fingerprint_changes_with_content(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("one", encoding="utf-8")
        first = body_fingerprint(path)
        path.write_text("one and more", encoding="utf-8")
        assert first is not None
        assert body_fingerprint(path) != first

    def test_same_size_same_mtime_replacement_is_reindexed(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("alpha term", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        first = body_fingerprint(path)
        assert first is not None
        assert index.sync([("k", str(path), first)], live_keys=["k"]) == frozenset()
        assert index.body_hits(["k"], ["alpha"]) == {"k": 1}

        original = path.stat()
        replacement = tmp_path / "replacement.md"
        replacement.write_text("bravo term", encoding="utf-8")
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, path)
        replaced = path.stat()
        assert replaced.st_size == original.st_size
        assert replaced.st_mtime_ns == original.st_mtime_ns

        second = body_fingerprint(path)
        assert second is not None
        assert second != first
        assert index.sync([("k", str(path), second)], live_keys=["k"]) == frozenset()
        assert index.body_hits(["k"], ["alpha"]) == {}
        assert index.body_hits(["k"], ["bravo"]) == {"k": 1}

    def test_fingerprint_of_missing_path_is_none(self, tmp_path):
        assert body_fingerprint(tmp_path / "absent" / "SKILL.md") is None

    def test_sync_prunes_keys_that_are_gone(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("terraform plan", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        fingerprint = body_fingerprint(path)
        assert fingerprint is not None
        assert index.sync([("gone", str(path), fingerprint)], live_keys=["gone"]) == frozenset()
        assert index.body_hits(["gone"], ["terraform"]) == {"gone": 1}
        assert index.sync([], live_keys=["other"]) == frozenset()
        assert index.body_hits(["gone"], ["terraform"]) == {}

    def test_refused_hardened_read_clears_stale_terms(self, tmp_path, monkeypatch):
        path = tmp_path / "SKILL.md"
        path.write_text("oldterm", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        first = body_fingerprint(path)
        assert first is not None
        assert index.sync([("k", str(path), first)], live_keys=["k"]) == frozenset()
        assert index.body_hits(["k"], ["oldterm"]) == {"k": 1}

        path.write_text("secretterm replacement", encoding="utf-8")
        second = body_fingerprint(path)
        assert second is not None
        reads: list[str] = []

        def refuse(raw, *args, **kwargs):
            reads.append(str(raw))
            return None

        monkeypatch.setattr(skill_search_index_module, "safe_read_file_bytes_nolink", refuse)
        # Handed back for a direct read, NOT silently answered as a body with no
        # terms: the caller's own reader decides whether those bytes are served.
        assert index.sync([("k", str(path), second)], live_keys=["k"]) == frozenset({"k"})
        assert reads == [str(path)]
        assert index.body_hits(["k"], ["oldterm", "secretterm"]) == {}
        # No fingerprint was stored, so the next search retries instead of
        # treating the refusal as a cached answer.
        assert index.sync([("k", str(path), second)], live_keys=["k"]) == frozenset({"k"})

    def test_body_hits_counts_each_term_once(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("terraform terraform plan plan plan", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        fingerprint = body_fingerprint(path)
        assert fingerprint is not None
        index.sync([("k", str(path), fingerprint)], live_keys=["k"])
        assert index.body_hits(["k"], ["terraform", "plan"]) == {"k": 2}

    def test_unreadable_database_reports_none(self, tmp_path):
        """A directory where the file belongs makes every method report None."""
        (tmp_path / "index.sqlite3").mkdir()
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        assert index.sync([]) is None
        assert index.body_hits(["k"], ["terraform"]) is None

    def test_astral_term_is_found_by_its_prefix(self, tmp_path):
        """A body word continuing with an astral character must still match.

        ``\\uffff`` encodes as ``EF BF BF`` and an astral character starts at
        ``F0``, so a high-sentinel upper bound sorted BELOW the very terms it was
        meant to include and the prefix search returned nothing.
        """
        path = tmp_path / "SKILL.md"
        path.write_text("\U00020000\U00020001 计划", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        fingerprint = body_fingerprint(path)
        assert fingerprint is not None
        index.sync([("k", str(path), fingerprint)], live_keys=["k"])
        assert index.body_hits(["k"], ["\U00020000"]) == {"k": 1}

    def test_prefix_upper_bound_edges(self):
        assert prefix_upper_bound("deploy") == "deploz"
        assert prefix_upper_bound("\U00020000") == "\U00020001"
        # An increment landing in the surrogate block skips past it, because a
        # lone surrogate cannot be encoded for the query parameter.
        assert prefix_upper_bound("\ud7ff") == "\ue000"
        # No representable bound above the maximum code point; the caller then
        # scans from the lower bound alone.
        assert prefix_upper_bound("\U0010ffff") is None
        assert prefix_upper_bound("") is None

    def test_max_code_point_term_still_matches(self, tmp_path):
        """The unbounded branch must still find the term it scans for."""
        path = tmp_path / "SKILL.md"
        path.write_text("terraform", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        fingerprint = body_fingerprint(path)
        assert fingerprint is not None
        index.sync([("k", str(path), fingerprint)], live_keys=["k"])
        assert index.body_hits(["k"], ["\U0010ffff"]) == {}

    def test_index_survives_use_from_another_thread(self, tmp_path):
        """A second thread must not latch the index unusable.

        Searches legitimately arrive on different threads — the dashboard route
        hands the call to a thread pool — and a thread-bound connection raises
        there. Because a raise latches this index off, one such call would drop
        every later search back to reading files for the life of the process.
        """
        path = tmp_path / "SKILL.md"
        path.write_text("terraform plan", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        fingerprint = body_fingerprint(path)
        assert fingerprint is not None
        assert index.sync([("k", str(path), fingerprint)], live_keys=["k"]) == frozenset()

        results: list[dict[str, int] | None] = []

        def worker() -> None:
            results.append(index.body_hits(["k"], ["terraform"]))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        assert results == [{"k": 1}]
        # Still usable on the original thread afterwards.
        assert index.body_hits(["k"], ["plan"]) == {"k": 1}


class TestFamilyLine:
    def test_groups_label_namespace_and_hyphen_prefix(self):
        groups = _namespace_groups(
            [
                {"key": "kirocrew-dev/babysit"},
                {"key": "kirocrew-dev/prepare-pr"},
                {"key": "web-verify"},
                {"key": "web-browse"},
                {"key": "web-preview"},
                {"key": "artifacts"},
                {"key": "lonely-one"},
            ]
        )
        assert groups == [("web-*", 3), ("kirocrew-dev/", 2)]

    @staticmethod
    def _parse(text: str) -> tuple[set[str], list[tuple[str, int]]]:
        """The keys the entry named, and the (label, count) pairs on its line."""
        named = {
            line[2:].split(":", 1)[0]
            for line in text.splitlines()
            if line.startswith("- ") and ":" in line
        }
        families: list[tuple[str, int]] = []
        for line in text.splitlines():
            if not line.startswith("More families: "):
                continue
            for item in line[len("More families: ") :].split(", "):
                if item.startswith("+"):
                    continue
                label, _, count = item.rpartition(" ")
                families.append((label, int(count.strip("()"))))
        return named, families

    def test_family_counts_are_the_members_the_entry_hides(self, tmp_path):
        """Each count must equal that family's UNNAMED members, not its size.

        The line exists to cover what the entry hides. Counting a member the
        reader can already see spends the budget saying nothing, and on a tight
        budget it crowds out a family that is genuinely unreachable.

        Asserted as an exact mapping rather than as a sum, because WHICH eight of
        twelve equal-usage skills get named follows directory enumeration order.
        A family with a single hidden member is deliberately absent: its siblings
        are named, so the reader already has that word.
        """
        skills_dir = tmp_path / "skills"
        keys = [f"alpha-{n}" for n in range(6)] + [f"beta-{n}" for n in range(6)]
        for key in keys:
            _skill(skills_dir, key, body="body")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000, discovery_only=True)

        named, families = self._parse(text)
        assert len(named) == 8
        hidden_per_family = {
            label: len([k for k in keys if k.startswith(label) and k not in named])
            for label in ("alpha-", "beta-")
        }
        expected = {f"{label}*": count for label, count in hidden_per_family.items() if count > 1}
        assert expected, "an entry hiding four skills must name at least one family"
        assert dict(families) == expected

    def test_named_skills_win_a_tight_budget(self, tmp_path):
        """Under pressure the descriptions survive and the family line is dropped.

        A name carries the only text that says what a skill DOES, so it outranks
        the coverage hint rather than competing with it.
        """
        skills_dir = tmp_path / "skills"
        for n in range(12):
            _skill(skills_dir, f"web-{n}", body="body")
        loader = _loader(tmp_path)
        full = loader.get_context(budget=4000, discovery_only=True)
        assert "More families" in full
        tight = loader.get_context(budget=len(full) - 20, discovery_only=True)
        assert "More families" not in tight
        assert tight.count("\n- ") == 8

    def test_no_family_line_when_nothing_is_hidden(self, tmp_path):
        """Eight or fewer skills are all named, so the line would repeat them."""
        skills_dir = tmp_path / "skills"
        for key in ("web-verify", "web-browse"):
            _skill(skills_dir, key, body="body")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000, discovery_only=True)
        assert "web-verify" in text
        assert "More families" not in text


class TestDecliningOneBodyDoesNotCostTheCatalog:
    """A body this index will not store must not disable it for every other skill.

    Both reasons it declines -- a body past the size ceiling, and one the hardened
    reader refuses -- resolve the same way: that ONE key is handed back for a direct
    read. Answering for the whole call instead would send an entire catalog to
    reading files on every search, which is the cost this index exists to remove.
    """

    def test_an_oversized_body_defers_only_itself(self, tmp_path, monkeypatch, small_ceiling):
        skills = tmp_path / "skills"
        _skill(skills, "small", body="smallneedle")
        big = "x" * (small_ceiling + 1)
        _skill(skills, "huge", body=f"{big} tailneedle")
        loader = _loader(tmp_path)

        # Both stay findable: the small one from the index, the huge one by a read.
        assert [h["key"] for h in loader.search_skills("smallneedle")] == ["small"]
        assert [h["key"] for h in loader.search_skills("tailneedle")] == ["huge"]

        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        rows = []
        for key in ("small", "huge"):
            body = skills / key / "SKILL.md"
            fingerprint = body_fingerprint(body)
            assert fingerprint is not None
            rows.append((key, str(body), fingerprint))
        assert index.sync(rows, live_keys=["small", "huge"]) == frozenset({"huge"})
        # The small body was still indexed by that very same call.
        assert index.body_hits(["small"], ["smallneedle"]) == {"small": 1}

    def test_only_the_deferred_body_is_read_from_disk(self, tmp_path, monkeypatch, small_ceiling):
        """The caller must read the ONE key the index declines, not the catalog.

        This is the whole point of a per-key answer: declining for the whole call
        would send every body back through ``load_skill`` on every search, which is
        the cost this index exists to remove.
        """
        skills = tmp_path / "skills"
        for n in range(5):
            _skill(skills, f"plain-{n}", body=f"plainneedle{n}")
        big = "x" * (small_ceiling + 1)
        _skill(skills, "huge", body=f"{big} tailneedle")
        loader = _loader(tmp_path)
        loader.search_skills("warmup")  # build the index once

        read: list[str] = []
        original = loader.load_skill

        def counting(key, *args, **kwargs):
            read.append(str(key))
            return original(key, *args, **kwargs)

        monkeypatch.setattr(loader, "load_skill", counting)
        assert [h["key"] for h in loader.search_skills("tailneedle")] == ["huge"]
        assert read == ["huge"]

    def test_a_hardlinked_body_is_not_served(self, tmp_path):
        """A declined index read must not fall back to an unsafe direct read."""
        skills = tmp_path / "skills"
        _skill(skills, "ordinary", body="ordinaryneedle")
        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: linked\ndescription: A skill.\n---\nlinkedneedle\n", encoding="utf-8"
        )
        linked = skills / "linked"
        linked.mkdir(parents=True)
        os.link(outside, linked / "SKILL.md")
        assert (linked / "SKILL.md").stat().st_nlink > 1
        loader = _loader(tmp_path)

        assert loader.search_skills("linkedneedle") == []
        assert [h["key"] for h in loader.search_skills("ordinaryneedle")] == ["ordinary"]


class TestTransientLockDoesNotLatch:
    """A busy neighbour must not downgrade the whole process to reading files.

    The module promises the caller reads bodies "this once" when a lock outlives the
    timeout. Latching there would make one contended moment permanent.
    """

    def test_a_busy_database_is_retried(self, tmp_path, monkeypatch):
        """Driven by a REAL contended lock, so the message this matches on is real."""
        db_path = tmp_path / "index.sqlite3"
        path = tmp_path / "SKILL.md"
        path.write_text("alpha term", encoding="utf-8")
        # Patch before construction: the connection takes its timeout at connect.
        monkeypatch.setattr(skill_search_index_module, "_BUSY_TIMEOUT_SECS", 0.05)
        index = SkillSearchIndex(db_path)
        first = body_fingerprint(path)
        assert first is not None
        assert index.sync([("k", str(path), first)], live_keys=["k"]) == frozenset()

        path.write_text("bravo term", encoding="utf-8")
        second = body_fingerprint(path)
        assert second is not None
        assert second != first

        blocker = skill_search_index_module.sqlite3.connect(str(db_path), timeout=0.05)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            # The write cannot get the lock within the timeout.
            assert index.sync([("k", str(path), second)], live_keys=["k"]) is None
        finally:
            blocker.rollback()
            blocker.close()

        # Not latched: with the neighbour gone the very next call indexes again.
        assert index.sync([("k", str(path), second)], live_keys=["k"]) == frozenset()
        assert index.body_hits(["k"], ["bravo"]) == {"k": 1}

    def test_a_broken_database_still_latches(self, tmp_path, monkeypatch):
        path = tmp_path / "SKILL.md"
        path.write_text("alpha term", encoding="utf-8")
        index = SkillSearchIndex(tmp_path / "index.sqlite3")
        fingerprint = body_fingerprint(path)
        assert fingerprint is not None
        assert index.sync([("k", str(path), fingerprint)], live_keys=["k"]) == frozenset()

        class _Broken:
            def execute(self, *args, **kwargs):
                raise skill_search_index_module.sqlite3.DatabaseError("file is not a database")

            def rollback(self):
                pass

        monkeypatch.setattr(index, "_db", lambda: _Broken())
        assert index.body_hits(["k"], ["alpha"]) is None
        monkeypatch.undo()
        # Latched: a corrupt file would raise and log once per search otherwise.
        assert index.body_hits(["k"], ["alpha"]) is None
        assert index.sync([("k", str(path), fingerprint)], live_keys=["k"]) is None


class TestTokenizerChangeInvalidatesTheIndex:
    """Stored terms are the tokenizer's output, so its change must drop them.

    A tokenizer edit that resplits or renormalizes leaves rows a new query can no
    longer match. That is a MISS, not an error: nothing raises, nothing logs, and
    the search quietly answers with fewer skills. Neither the body fingerprint nor
    a hand-maintained schema number can see it.
    """

    def test_signature_follows_the_tokenizer_answer(self, monkeypatch):
        before = skill_search_index_module.tokenizer_signature()
        assert before == skill_search_index_module.tokenizer_signature()

        monkeypatch.setattr(
            skill_search_index_module,
            "recall_terms",
            lambda text: frozenset({"deliberately", "different"}),
        )
        assert skill_search_index_module.tokenizer_signature() != before

    def test_stored_terms_are_dropped_when_the_tokenizer_changes(self, tmp_path, monkeypatch):
        db_path = tmp_path / "index.sqlite3"
        path = tmp_path / "SKILL.md"
        path.write_text("alpha term", encoding="utf-8")
        fingerprint = body_fingerprint(path)
        assert fingerprint is not None

        index = SkillSearchIndex(db_path)
        assert index.sync([("k", str(path), fingerprint)], live_keys=["k"]) == frozenset()
        assert index.body_hits(["k"], ["alpha"]) == {"k": 1}

        # A different tokenizer, same file, same fingerprint: a fresh index over
        # the same database must rebuild rather than trust the stored terms.
        real = skill_search_index_module.recall_terms
        monkeypatch.setattr(
            skill_search_index_module,
            "recall_terms",
            lambda text: frozenset({f"z{t}" for t in real(text)}),
        )
        rebuilt = SkillSearchIndex(db_path)
        assert rebuilt.body_hits(["k"], ["alpha"]) == {}
        assert rebuilt.sync([("k", str(path), fingerprint)], live_keys=["k"]) == frozenset()
        assert rebuilt.body_hits(["k"], ["zalpha"]) == {"k": 1}


class TestBothPathsScoreTheSameWay:
    """The index and the direct read must agree on what counts as a body hit.

    They can answer within ONE search -- the index hands individual keys back for a
    direct read -- so a substring scan on one side and token-prefix matching on the
    other would make a skill's rank depend on which side answered for it.
    """

    def test_a_term_inside_a_longer_word_matches_neither_path(self, tmp_path, small_ceiling):
        skills = tmp_path / "skills"
        _skill(skills, "indexed", body="scrollback history")
        big = "x" * (small_ceiling + 1)
        # Deferred to a direct read, and carrying the same near-miss word.
        _skill(skills, "deferred", body=f"{big} scrollback history")
        loader = _loader(tmp_path)

        # `rollback` is strictly inside `scrollback`: a near miss on both paths.
        assert loader.search_skills("rollback") == []
        # The whole token still matches on both.
        assert sorted(h["key"] for h in loader.search_skills("scrollback")) == [
            "deferred",
            "indexed",
        ]

    def test_a_prefix_matches_on_both_paths(self, tmp_path, small_ceiling):
        skills = tmp_path / "skills"
        _skill(skills, "indexed", body="deployment runbook")
        big = "x" * (small_ceiling + 1)
        _skill(skills, "deferred", body=f"{big} deployment runbook")
        loader = _loader(tmp_path)

        assert sorted(h["key"] for h in loader.search_skills("deploy")) == [
            "deferred",
            "indexed",
        ]


class TestCoverageAndPersistedMetadata:
    def test_new_loader_dedupes_from_persisted_digest_without_content_reads(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "skills"
        first_path = _skill(root, "one/payment", body="payment procedure")
        second_path = _skill(root, "two/payment", body="payment procedure")
        second_path.write_bytes(first_path.read_bytes())
        assert len(_loader(tmp_path).search_skills("payment")) == 1

        warm = _loader(tmp_path)

        def unexpected(*args, **kwargs):
            pytest.fail("persisted metadata and body index should avoid content reads")

        monkeypatch.setattr(warm, "_cached_frontmatter", unexpected)
        monkeypatch.setattr(warm._search_index, "_read_terms", unexpected)
        assert len(warm.search_skills("payment")) == 1

    def test_multiword_body_coverage_beats_single_metadata_word(self, tmp_path):
        root = tmp_path / "skills"
        for n in range(60):
            _skill(root, f"distractor-{n}", description="deploy", body="unrelated")
        _skill(root, "rare-repair", body="zirconium deployment")
        loader = _loader(tmp_path)
        # Complete the bounded incremental refresh before testing the ranking.
        for _ in range(80):
            result = loader.search_skills("deploy zirconium")
            if not loader.search_incomplete:
                break
        assert not loader.search_incomplete
        assert result[0]["key"] == "rare-repair"

    def test_new_loader_reuses_metadata_and_body_then_refreshes_a_change(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "skills"
        path = _skill(root, "team/repair", description="deployment", body="zirconium")
        first = _loader(tmp_path)
        assert first.search_skills("zirconium")[0]["key"] == "team/repair"
        second = _loader(tmp_path)

        def unexpected(*args, **kwargs):
            pytest.fail("unchanged persistent index should not read skill content")

        with monkeypatch.context() as m:
            m.setattr(second, "_cached_frontmatter", unexpected)
            m.setattr(second._search_index, "_read_terms", unexpected)
            assert second.search_skills("deployment")[0]["key"] == "team/repair"
            assert second.search_skills("zirconium")[0]["key"] == "team/repair"
        path.write_text("---\nname: repair\ndescription: updated\n---\nnewword", encoding="utf-8")
        third = _loader(tmp_path)
        assert third.search_skills("zirconium") == []
        assert third.search_skills("newword")[0]["key"] == "team/repair"

    def test_bounded_refresh_reports_pending_keys_and_eventually_reaches_tail(self, tmp_path):
        root = tmp_path / "skills"
        paths = [_skill(root, f"s{n}", body="zirconium") for n in range(4)]
        index = SkillSearchIndex(tmp_path / "bounded.sqlite3")
        rows = [(p.parent.name, str(p), body_fingerprint(p)) for p in paths]
        assert index.sync(rows, budget_seconds=0) == frozenset(p.parent.name for p in paths)
        assert index.pending_keys
        assert index.sync(rows) == frozenset()
        assert not index.pending_keys
        assert len(index.body_hits([r[0] for r in rows], ["zirconium"])) == 4
