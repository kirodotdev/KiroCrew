"""Phase-1 tests: pending-approval queue for auto-skill candidates."""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


def _prov(days_ago: float = 0) -> AutoSkillProvenance:
    return AutoSkillProvenance(
        session_key="s",
        created_at=(datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat(
            timespec="seconds"
        ),
    )


def _stage(loader, slug, *, scripts=None, days_ago=0):
    name = loader.stage_skill_candidate(
        slug,
        description=f"desc {slug}",
        triggers=slug,
        procedure_md="## Steps\n\nrun it",
        provenance=_prov(days_ago),
        scripts=scripts,
    )
    # prune_pending ages candidates by filesystem mtime (not the LLM-supplied
    # created_at), so backdate the dir's mtime to simulate an old candidate.
    if days_ago:
        pdir = loader._pending_root() / slug
        old = datetime.now(tz=timezone.utc).timestamp() - days_ago * 86400
        os.utime(pdir, (old, old))
    return name


def test_staged_candidate_is_not_live_or_triggerable(loader):
    assert _stage(loader, "cand-one") == "auto/cand-one"
    # Not in the live set (pending dir is dot-pruned from discovery).
    assert loader.list_auto_skills() == []
    assert loader.get_triggered_skills("cand-one please") == []
    # But visible in the pending queue.
    pend = loader.list_pending_skills()
    assert [p["slug"] for p in pend] == ["cand-one"]


def test_new_candidate_kind_defaults_and_no_update_fields(loader):
    """A plainly-staged candidate defaults to kind='new' with no target /
    base_version — the update fields are omitted for backward compatibility."""
    _stage(loader, "plainly")
    entry = [p for p in loader.list_pending_skills() if p["slug"] == "plainly"][0]
    assert entry["kind"] == "new"
    assert entry["target"] is None
    assert entry["base_version"] is None
    detail = loader.get_pending_skill("plainly")
    assert detail["kind"] == "new"
    assert detail["target"] is None
    assert detail["base_version"] is None
    # .meta.json records kind but omits target/base_version.
    import json as _json

    meta = _json.loads(
        (loader._pending_root() / "plainly" / ".meta.json").read_text(encoding="utf-8")
    )
    assert meta["kind"] == "new"
    assert "target" not in meta
    assert "base_version" not in meta


def test_get_pending_returns_body_and_scripts(loader):
    _stage(loader, "with-script", scripts=[{"filename": "run.py", "content": "print(1)\n"}])
    detail = loader.get_pending_skill("with-script")
    assert detail is not None
    assert "run it" in detail["content"]
    assert detail["scripts"] == [{"filename": "run.py", "content": "print(1)\n"}]
    # Pending scripts are NOT executable.
    sf = loader._pending_root() / "with-script" / "scripts" / "run.py"
    assert not (os.stat(sf).st_mode & 0o111)


def test_approve_promotes_and_marks_scripts_executable(loader):
    _stage(loader, "promote-me", scripts=[{"filename": "run.py", "content": "print(1)\n"}])
    name = loader.approve_pending_skill("promote-me")
    assert name == "auto/promote-me"
    # Now live and triggerable.
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/promote-me"]
    # Gone from pending.
    assert loader.list_pending_skills() == []
    # Script is now executable (POSIX only — Windows has no exec bit), and
    # .meta.json was dropped.
    live = loader._dir / "auto" / "promote-me"
    if os.name != "nt":
        assert os.stat(live / "scripts" / "run.py").st_mode & 0o111
    assert not (live / ".meta.json").exists()


def test_approve_refuses_when_live_exists(loader):
    _stage(loader, "dup")
    # Write the live skill straight to disk: the publish path declines a slug
    # that is awaiting review, so approval's own guard is exercised against a
    # live skill that arrives by another route (hand-authored, restored from
    # the archive, synced in).
    live = loader._dir / "auto" / "dup"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("---\nname: auto/dup\n---\nbody\n", encoding="utf-8")
    loader._invalidate_iter_cache()
    assert loader.approve_pending_skill("dup") is None
    # Candidate remains pending for the user to resolve.
    assert [p["slug"] for p in loader.list_pending_skills()] == ["dup"]


def test_dismiss(loader):
    _stage(loader, "toss")
    assert loader.dismiss_pending_skill("toss") is True
    assert loader.list_pending_skills() == []
    assert loader.dismiss_pending_skill("toss") is False


def test_prune_pending_by_ttl(loader):
    _stage(loader, "old-pending", days_ago=45)
    _stage(loader, "fresh-pending", days_ago=1)
    pruned = loader.prune_pending(ttl_days=30)
    assert pruned == 1
    assert [p["slug"] for p in loader.list_pending_skills()] == ["fresh-pending"]


def test_prune_ignores_llm_created_at(loader):
    """A fresh candidate stamped with an ancient ``created_at`` must NOT be
    pruned: pruning ages by filesystem mtime, so model-controlled metadata can't
    trick it into deleting fresh, unreviewed work."""
    import json as _json

    _stage(loader, "freshly-written", days_ago=0)
    meta = loader._pending_root() / "freshly-written" / ".meta.json"
    d = _json.loads(meta.read_text(encoding="utf-8"))
    d["created_at"] = "2000-01-01T00:00:00+00:00"
    meta.write_text(_json.dumps(d), encoding="utf-8")
    assert loader.prune_pending(ttl_days=30) == 0
    assert [p["slug"] for p in loader.list_pending_skills()] == ["freshly-written"]


def test_noncanonical_pending_dir_name_is_hidden(loader):
    """A crystallize direct-write could name the pending dir with
    credential-shaped text; a non-canonical slug must not surface in the list."""
    root = loader._pending_root()
    root.mkdir(parents=True, exist_ok=True)
    bad = root / "AKIAIOSFODNN7EXAMPLE"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# x", encoding="utf-8")
    assert loader.list_pending_skills() == []


def test_stage_rolls_back_claim_on_write_failure(loader, monkeypatch):
    """A partial write (e.g. disk full) must not leave a claimed-but-empty
    pending dir that blocks re-staging the slug."""
    import kiro_crew.skills as S

    def _boom(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(S, "_build_auto_skill_content", _boom)
    with pytest.raises(OSError):
        _stage(loader, "rollback")
    assert not (loader._pending_root() / "rollback").exists()


def test_approve_rejects_unexpected_candidate_file(loader):
    """An injected auxiliary file (outside SKILL.md/.meta.json/scripts/) must
    block approval so it can't be promoted live unredacted."""
    _stage(loader, "auxtest")
    (loader._pending_root() / "auxtest" / "leak.txt").write_text("secret", encoding="utf-8")
    assert loader.approve_pending_skill("auxtest") is None
    # Nothing moved: the candidate stays in the pending queue.
    assert (loader._pending_root() / "auxtest").is_dir()
    assert not (loader._dir / "auto" / "auxtest").exists()


def test_approve_rejects_regular_file_named_scripts(loader):
    """A regular FILE named 'scripts' (vs the scripts/ directory) must be
    refused — it would skip the dir-only script validation + redaction walk and
    could ride live unredacted."""
    _stage(loader, "scriptfile")
    (loader._pending_root() / "scriptfile" / "scripts").write_text(
        "AKIAIOSFODNN7EXAMPLE", encoding="utf-8"
    )
    assert loader.approve_pending_skill("scriptfile") is None
    assert (loader._pending_root() / "scriptfile").is_dir()
    assert not (loader._dir / "auto" / "scriptfile").exists()


def test_failed_approval_preserves_meta(loader, monkeypatch):
    """If redaction fails mid-approve, the candidate — including its
    .meta.json — must stay intact in the pending queue for re-review."""
    _stage(loader, "metakeep")
    meta = loader._pending_root() / "metakeep" / ".meta.json"
    assert meta.exists()
    monkeypatch.setattr(loader, "_redact_file_in_place", lambda *a, **k: False)
    assert loader.approve_pending_skill("metakeep") is None
    assert meta.exists()  # bookkeeping not destroyed by the failed approval
    assert not (loader._dir / "auto" / "metakeep").exists()


def test_redaction_breaking_script_aborts_promotion(loader, monkeypatch):
    """If redaction alters a script into invalid syntax, the post-redaction
    re-validation must abort promotion so a broken helper never goes live."""
    _stage(
        loader,
        "redactbreak",
        scripts=[{"filename": "run.py", "content": "import json\nprint(json.dumps({'a': 1}))\n"}],
    )
    orig = loader._redact_file_in_place

    def corrupt(fp):
        if fp.name == "run.py":
            fp.write_text("def (:\n", encoding="utf-8")  # invalid syntax
            return True
        return orig(fp)

    monkeypatch.setattr(loader, "_redact_file_in_place", corrupt)
    assert loader.approve_pending_skill("redactbreak") is None
    assert (loader._pending_root() / "redactbreak").is_dir()
    assert not (loader._dir / "auto" / "redactbreak").exists()
    # The pending script must be RESTORED to its original bytes, not left as the
    # corrupted (redaction-broken) version.
    restored = (loader._pending_root() / "redactbreak" / "scripts" / "run.py").read_text()
    assert restored == "import json\nprint(json.dumps({'a': 1}))\n"


def test_failed_move_restores_meta(loader, monkeypatch):
    """If the promotion move fails after .meta.json was removed, the metadata is
    restored so the candidate isn't stranded in pending without it."""
    import kiro_crew.skills as S

    _stage(loader, "movefail")
    meta = loader._pending_root() / "movefail" / ".meta.json"
    orig_bytes = meta.read_bytes()

    def _boom(*a, **k):
        raise OSError("dest unwritable")

    monkeypatch.setattr(S.shutil, "move", _boom)
    assert loader.approve_pending_skill("movefail") is None
    assert meta.exists() and meta.read_bytes() == orig_bytes
    assert not (loader._dir / "auto" / "movefail").exists()


def test_failed_meta_unlink_aborts_promotion(loader, monkeypatch):
    """If the pending .meta.json can't be removed, promotion must abort so the
    raw (possibly secret-bearing) metadata never rides into the live skill dir."""
    import pathlib

    _stage(loader, "metafail")
    orig = pathlib.Path.unlink

    def guarded(self, *a, **k):
        if self.name == ".meta.json":
            raise PermissionError("read-only pending dir")
        return orig(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", guarded)
    assert loader.approve_pending_skill("metafail") is None
    assert (loader._pending_root() / "metafail").is_dir()
    assert not (loader._dir / "auto" / "metafail").exists()


def test_meta_credential_key_is_redacted(loader):
    """A credential-shaped .meta.json KEY (not just a value) is redacted before
    the pending detail/list API can surface it to the dashboard."""
    import json as _json

    _stage(loader, "keytest")
    meta = loader._pending_root() / "keytest" / ".meta.json"
    d = _json.loads(meta.read_text(encoding="utf-8"))
    d["AKIAIOSFODNN7EXAMPLE"] = "v"
    meta.write_text(_json.dumps(d), encoding="utf-8")
    red = loader._read_pending_meta("keytest")
    assert not any("AKIA" in k for k in red)


def test_approve_refuses_symlink_in_candidate(loader):
    """A symlinked file in the candidate is refused at approve (TOCTOU guard)."""
    import os
    _stage(loader, "linky")
    pdir = loader._pending_root() / "linky"
    (pdir / "scripts").mkdir(parents=True, exist_ok=True)
    target = pdir / "real.txt"
    target.write_text("ok", encoding="utf-8")
    os.symlink(str(target), str(pdir / "scripts" / "evil.py"))
    assert loader.approve_pending_skill("linky") is None
    assert loader.list_auto_skills() == []


def test_stage_rejects_bad_slug_and_traversal_script(loader):
    assert _stage(loader, "a") is None  # too short for _AUTO_NAME_PATTERN
    # Traversal-y script filename is skipped, not written.
    _stage(loader, "ok-slug", scripts=[{"filename": "../evil.py", "content": "x"}])
    detail = loader.get_pending_skill("ok-slug")
    assert detail is not None
    assert detail["scripts"] == []


def test_approve_revalidates_scripts_written_directly(loader):
    """A candidate whose scripts bypassed stage validation (e.g. crystallize
    wrote them straight into .pending/) is re-scanned at the approve choke point
    and refused if any script is dangerous."""
    pdir = loader._pending_root() / "sneaky"
    (pdir / "scripts").mkdir(parents=True)
    (pdir / "SKILL.md").write_text("---\nname: auto/sneaky\n---\nbody", encoding="utf-8")
    (pdir / "scripts" / "wipe.py").write_text("import os\nos.system('rm -rf /')\n", encoding="utf-8")
    # Approve must refuse (dangerous script) and leave it live-free.
    assert loader.approve_pending_skill("sneaky") is None
    assert loader.list_auto_skills() == []


def test_approve_validates_nested_scripts(loader):
    """A dangerous script hidden in scripts/nested/ must not evade validation
    and be promoted (GPT HIGH: recurse scripts/ at the approve choke point)."""
    assert _stage(loader, "nest-cand") == "auto/nest-cand"
    pdir = loader._pending_root() / "nest-cand"
    nested = pdir / "scripts" / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "evil.py").write_text("import os\nos.system('rm -rf /')\n", encoding="utf-8")
    # Nested dangerous script is now visible to the detail view...
    detail = loader.get_pending_skill("nest-cand")
    assert any(s["filename"].endswith("evil.py") for s in detail["scripts"])
    # ...and blocks promotion.
    assert loader.approve_pending_skill("nest-cand") is None
    assert not (loader._dir / "auto" / "nest-cand").exists()


def test_dismiss_dot_slug_is_rejected(loader):
    """dismiss('.') must not collapse to the pending root and wipe the queue."""
    assert _stage(loader, "keep-me") == "auto/keep-me"
    assert loader.dismiss_pending_skill(".") is False
    assert loader.dismiss_pending_skill("") is False
    assert loader.dismiss_pending_skill("..") is False
    # Queue intact.
    assert any(s["slug"] == "keep-me" for s in loader.list_pending_skills())
    assert (loader._pending_root() / "keep-me").is_dir()


def test_direct_write_candidate_is_redacted_at_detail_and_approve(loader):
    """A candidate written straight into the pending queue (crystallize path,
    bypassing consolidation redaction) must not surface a credential to the
    dashboard detail API or promote it into a live skill (GPT HIGH)."""
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    slug = "leaky-cand"
    pdir = loader._pending_root() / slug
    (pdir / "scripts").mkdir(parents=True, exist_ok=True)
    (pdir / "SKILL.md").write_text(
        f"---\nname: auto/{slug}\ndescription: x\ntriggers: t\nsource: crystallize\n"
        f"---\n\n# {slug}\n\ntoken={secret}\n",
        encoding="utf-8",
    )
    (pdir / "scripts" / "run.py").write_text(f'TOKEN = "{secret}"\nprint("ok")\n', encoding="utf-8")

    detail = loader.get_pending_skill(slug)
    assert secret not in detail["content"]
    assert all(secret not in s["content"] for s in detail["scripts"])

    assert loader.approve_pending_skill(slug) == f"auto/{slug}"
    live = loader._dir / "auto" / slug
    assert secret not in (live / "SKILL.md").read_text(encoding="utf-8")
    assert secret not in (live / "scripts" / "run.py").read_text(encoding="utf-8")


def test_restage_does_not_clobber_candidate_under_review(loader):
    """A same-slug re-stage must NOT swap the bytes a human is reviewing
    (approval-integrity guard) AND must NOT silently drop the distinct candidate
    (consolidation advances its offset regardless): the reviewed candidate stays
    immutable and the distinct one is queued under a unique sibling slug."""
    assert _stage(loader, "race-cand", scripts=[{"filename": "a.py", "content": "print(1)\n"}]) \
        == "auto/race-cand"
    first = loader.get_pending_skill("race-cand")
    # Background consolidation re-detects a DISTINCT skill that slugifies the same.
    second_name = loader.stage_skill_candidate(
        "race-cand",
        description="DIFFERENT",
        triggers="race-cand",
        procedure_md="## Steps\n\ntotally different",
        provenance=_prov(),
        scripts=[{"filename": "b.py", "content": "print(2)\n"}],
    )
    # The reviewed candidate is unchanged — not swapped underneath the reviewer.
    second = loader.get_pending_skill("race-cand")
    assert second["content"] == first["content"]
    assert {s["filename"] for s in second["scripts"]} == {"a.py"}
    # The distinct candidate is queued under a unique slug, not lost.
    assert second_name == "auto/race-cand-2"
    distinct = loader.get_pending_skill("race-cand-2")
    assert "DIFFERENT" in distinct["content"]
    assert {s["filename"] for s in distinct["scripts"]} == {"b.py"}


def _stage_distinct(loader, slug, **kw):
    """Stage a candidate under ``slug`` without the ``_stage`` mtime handling."""
    return loader.stage_skill_candidate(
        slug,
        description=kw.pop("description", f"desc {slug}"),
        triggers=kw.pop("triggers", slug),
        procedure_md=kw.pop("procedure_md", "## Steps\n\nrun it"),
        provenance=_prov(),
        **kw,
    )


def test_publish_cannot_occupy_a_queued_candidates_destination(loader):
    """A live auto-publish must not take the slug a queued candidate will be
    promoted to. Occupying it makes approval refuse that candidate for as long
    as the live directory stands, and TTL pruning then deletes unreviewed work."""
    assert _stage(loader, "sib") == "auto/sib"
    # A distinct skill slugifying the same is queued as a sibling.
    assert _stage_distinct(loader, "sib", description="OTHER") == "auto/sib-2"
    # An auto-publish (approval off, prose-only) proposing that literal sibling
    # slug is refused rather than served.
    assert (
        loader.create_auto_skill(
            "sib-2",
            description="publisher",
            triggers="sib-2",
            procedure_md="body",
            provenance=_prov(),
        )
        is None
    )
    assert not (loader._dir / "auto" / "sib-2").exists()
    # The queued candidate keeps its destination and stays approvable.
    assert loader.approve_pending_skill("sib-2") == "auto/sib-2"


def test_staging_skips_a_sibling_whose_live_name_is_taken(loader):
    """The queue must not hand out a NEW-candidate slug whose live counterpart
    exists: approval refuses such a candidate, so it is queued unapprovable."""
    assert _stage(loader, "coord") == "auto/coord"
    assert (
        loader.create_auto_skill(
            "coord-2",
            description="live sibling",
            triggers="coord-2",
            procedure_md="body",
            provenance=_prov(),
        )
        == "auto/coord-2"
    )
    # -2 is live, so the distinct candidate lands on the next free name.
    assert _stage_distinct(loader, "coord", description="OTHER") == "auto/coord-3"
    assert loader.approve_pending_skill("coord-3") == "auto/coord-3"


def test_exhausted_pending_slugs_report_failure_not_success(loader):
    """With every sibling slug taken, staging must return ``None`` so the caller
    takes its rejection branch. A name returned without a write is recorded as a
    staged candidate that does not exist, and consolidation's offset advance
    makes that loss permanent."""
    root = loader._pending_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "full").mkdir()
    for n in range(2, 51):
        (root / f"full-{n}").mkdir()
    assert _stage_distinct(loader, "full") is None
    # Nothing was written under any of the occupied claims.
    assert not any((root / d.name / "SKILL.md").exists() for d in root.iterdir())


def test_overlong_sibling_slug_is_refused_not_orphaned(loader):
    """A sibling suffix that pushes the slug past the canonical length must not
    be claimed. ``list_pending_skills`` skips a non-canonical directory and
    ``prune_pending`` walks that list, so such a claim is invisible to the queue,
    the dashboard and pruning alike."""
    slug = "a" * 63  # canonical; "-2" would exceed the pattern's length
    assert _stage(loader, slug) == f"auto/{slug}"
    assert _stage_distinct(loader, slug, description="OTHER") is None
    # Exactly one pending directory, and it is listable.
    assert [d.name for d in loader._pending_root().iterdir()] == [slug]
    assert {p["slug"] for p in loader.list_pending_skills()} == {slug}


def test_update_candidate_may_share_its_live_targets_slug(loader):
    """An UPDATE candidate is promoted over the live ``target`` in its metadata
    by a path that never consults ``auto/<candidate-slug>``, so a live skill of
    that name must not divert it to a sibling."""
    assert (
        loader.create_auto_skill(
            "shared",
            description="live",
            triggers="shared",
            procedure_md="OLD",
            provenance=_prov(),
        )
        == "auto/shared"
    )
    assert (
        _stage_distinct(loader, "shared", kind="update", target="auto/shared", base_version=1)
        == "auto/shared"
    )


def test_pending_update_does_not_reserve_a_live_name(loader):
    """An UPDATE candidate is queued under a name derived from its target and is
    promoted to the target named in its metadata, so its own queue slug is never
    a live destination. Holding that name against a live create would refuse an
    unrelated skill and drop it, because consolidation advances its message
    offset whatever one candidate's outcome."""
    assert (
        _stage_distinct(
            loader, "topic-update", kind="update", target="auto/topic", base_version=1
        )
        == "auto/topic-update"
    )
    # A genuinely different skill slugifying to the queued update's name publishes.
    assert (
        loader.create_auto_skill(
            "topic-update",
            description="how to update a topic",
            triggers="topic-update",
            procedure_md="DISTINCT",
            provenance=_prov(),
        )
        == "auto/topic-update"
    )
    assert "DISTINCT" in (loader._dir / "auto" / "topic-update" / "SKILL.md").read_text()
    # The queued update is untouched and still promotes to its own target.
    assert loader._read_pending_meta("topic-update")["target"] == "auto/topic"


def test_pending_candidate_of_unknown_kind_still_reserves_its_slug(loader):
    """The kind test fails closed. A pending directory whose metadata is missing,
    unreadable or silent about ``kind`` keeps its slug reserved, so a candidate
    that may yet be promoted to ``auto/<slug>`` cannot be stranded."""
    pdir = loader._pending_root() / "opaque"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "SKILL.md").write_text("---\nname: auto/opaque\n---\n# x\n", encoding="utf-8")
    for meta in (None, "{ not json", '{"kind": "new"}', "[]"):
        if meta is None:
            (pdir / ".meta.json").unlink(missing_ok=True)
        else:
            (pdir / ".meta.json").write_text(meta, encoding="utf-8")
        assert not loader._auto_slug_available("opaque", claim="live")
        assert (
            loader.create_auto_skill(
                "opaque",
                description="d",
                triggers="opaque",
                procedure_md="P",
                provenance=_prov(),
            )
            is None
        )


def test_both_claim_paths_hold_the_shared_slug_lock(loader, monkeypatch):
    """The two paths allocate in different directories, so an atomic ``mkdir``
    cannot make the cross-namespace pair safe on its own. Both must test and
    claim inside the one shared lock."""
    held: list[str] = []
    depth = {"n": 0}
    real = SkillsLoader._auto_slug_claim_lock

    @contextlib.contextmanager
    def counting(self):
        depth["n"] += 1
        with real(self) as locked:
            yield locked
        depth["n"] -= 1

    def record(self, slug, *, claim="live"):
        held.append(f"{claim}:{depth['n']}")
        return True

    monkeypatch.setattr(SkillsLoader, "_auto_slug_claim_lock", counting)
    monkeypatch.setattr(SkillsLoader, "_auto_slug_available", record)
    assert _stage_distinct(loader, "locked") == "auto/locked"
    assert loader.create_auto_skill(
        "live-locked",
        description="d",
        triggers="t",
        procedure_md="P",
        provenance=_prov(),
    ) == "auto/live-locked"
    # Each availability test ran with the lock held (depth 1), one per path.
    assert held == ["pending-new:1", "live:1"]
    assert (loader._dir / ".auto-slug-claim.lock").exists()


def test_live_claim_refuses_a_directory_that_appears_under_it(loader):
    """The lock is advisory, so the live ``mkdir`` is the claim. A directory that
    appears in the window belongs to the other writer, and overwriting it would
    destroy their content, so this side refuses."""
    real_mkdir = Path.mkdir

    def racing(self, *a, **kw):
        if self.name == "raced" and not self.exists():
            real_mkdir(self, parents=True, exist_ok=True)
            (self / "SKILL.md").write_text("THEIRS", encoding="utf-8")
        return real_mkdir(self, *a, **kw)

    # Scoped context, not a shared-fixture patch plus undo: the patch is reverted
    # at the end of the with block and touches nothing the fixtures installed.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "mkdir", racing)
        assert (
            loader.create_auto_skill(
                "raced",
                description="d",
                triggers="t",
                procedure_md="MINE",
                provenance=_prov(),
            )
            is None
        )
    assert (loader._dir / "auto" / "raced" / "SKILL.md").read_text() == "THEIRS"


def test_claim_without_the_lock_is_refused_not_attempted(loader, monkeypatch):
    """The two claim paths allocate in different directories, so the atomic
    ``mkdir`` cannot make the cross-namespace pair safe. Without the lock a
    concurrent stage and publish can each pass the other half's test, which strands
    the queued candidate, so an unacquired lock refuses the claim. It returns
    ``None`` rather than raising, because the callers audit ``None`` as a rejection
    while an exception would abort the consolidation pass."""
    def refusing(fd, **kw):
        raise OSError("lock held by another process")

    monkeypatch.setattr("kiro_crew.skills.file_lock", refusing)
    assert _stage_distinct(loader, "unlocked") is None
    assert not (loader._pending_root() / "unlocked").exists()
    assert (
        loader.create_auto_skill(
            "live-unlocked",
            description="d",
            triggers="t",
            procedure_md="P",
            provenance=_prov(),
        )
        is None
    )
    assert not (loader._dir / "auto" / "live-unlocked").exists()


def test_claim_lock_that_cannot_be_opened_refuses_without_raising(loader, monkeypatch):
    """A lock file that cannot be opened is the other acquire failure, and it takes
    the same refusal path: a home this process cannot write a lock into is one it
    cannot coordinate in."""
    real_open = os.open

    def no_open(path, *a, **kw):
        if str(path).endswith(".auto-slug-claim.lock"):
            raise OSError("read-only home")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", no_open)
    assert _stage_distinct(loader, "unopenable") is None
    assert (
        loader.create_auto_skill(
            "live-unopenable",
            description="d",
            triggers="t",
            procedure_md="P",
            provenance=_prov(),
        )
        is None
    )


def test_restore_cannot_occupy_a_queued_candidates_destination(loader):
    """A restore is a third claim on the live half of the slug space. Moving an
    archived skill onto a queued NEW candidate's promotion destination strands that
    candidate exactly as a publish would, so it runs the same test under the same
    lock."""
    assert _stage(loader, "revived") == "auto/revived"
    archived = loader._archive_root() / "revived"
    archived.mkdir(parents=True, exist_ok=True)
    (archived / "SKILL.md").write_text("---\nname: auto/revived\n---\n# old\n", encoding="utf-8")
    assert loader.restore_auto_skill("revived") is None
    # The archive keeps its copy and the queued candidate still approves.
    assert (archived / "SKILL.md").exists()
    assert not (loader._dir / "auto" / "revived").exists()
    assert loader.approve_pending_skill("revived") == "auto/revived"


def test_pending_metadata_is_committed_under_the_claim_lock(loader):
    """A live publish reads the queued candidate's ``kind`` to decide whether the
    claim reserves its name, so the metadata carrying ``kind`` must land before the
    lock is released. A publish observing the claimed directory without it fails
    closed and refuses a name an UPDATE candidate never reserves."""
    seen: list[bool] = []
    real = SkillsLoader._auto_slug_claim_lock

    @contextlib.contextmanager
    def observing(self):
        with real(self) as locked:
            yield locked
        # At release, the claim and its metadata are both on disk.
        pdir = self._pending_root() / "committed-update"
        seen.append(pdir.is_dir() and (pdir / ".meta.json").exists())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SkillsLoader, "_auto_slug_claim_lock", observing)
        assert (
            _stage_distinct(
                loader,
                "committed-update",
                kind="update",
                target="auto/committed",
                base_version=1,
            )
            == "auto/committed-update"
        )
    assert seen == [True]
    assert loader._read_pending_meta("committed-update")["kind"] == "update"


def test_claim_is_the_mkdir_not_the_availability_test(loader, monkeypatch):
    """The availability test only picks a name; the ``mkdir`` is what claims it.
    When a directory appears in the window between the two, the walk moves to the
    next name rather than overwriting a candidate or failing the stage."""
    assert _stage(loader, "toctou") == "auto/toctou"
    before = loader.get_pending_skill("toctou")["content"]
    # Approve an occupied name, leaving mkdir as the only thing between two claims.
    monkeypatch.setattr(SkillsLoader, "_auto_slug_available", lambda self, slug, **kw: True)
    assert _stage_distinct(loader, "toctou", description="OTHER") == "auto/toctou-2"
    # The candidate under review keeps its bytes; the distinct one is queued.
    assert loader.get_pending_skill("toctou")["content"] == before
    assert "OTHER" in loader.get_pending_skill("toctou-2")["content"]


def test_meta_credentials_are_redacted_in_list_and_detail(loader):
    """LLM-written .meta.json (crystallize path) must not surface a credential
    through the pending list/detail API (GPT HIGH: redact metadata strings)."""
    import json as _json

    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    slug = "meta-leak"
    pdir = loader._pending_root() / slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "SKILL.md").write_text("---\nname: auto/meta-leak\n---\n# x\n", encoding="utf-8")
    (pdir / ".meta.json").write_text(
        _json.dumps({
            "slug": slug, "name": f"auto/{slug}", "source": "crystallize",
            "description": f"uses token {secret}", "triggers": "t",
        }),
        encoding="utf-8",
    )
    listed = [s for s in loader.list_pending_skills() if s["slug"] == slug][0]
    assert secret not in listed["description"]
    detail = loader.get_pending_skill(slug)
    assert secret not in detail["meta"]["description"]


def test_approve_marks_nested_scripts_executable(loader):
    """Nested scripts are validated + promoted, so approval must also chmod them
    executable (GPT MEDIUM: recursive chmod)."""
    slug = "nest-exec"
    pdir = loader._pending_root() / slug
    (pdir / "scripts" / "nested").mkdir(parents=True, exist_ok=True)
    (pdir / "SKILL.md").write_text("---\nname: auto/nest-exec\n---\n# x\n", encoding="utf-8")
    (pdir / "scripts" / "nested" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    assert loader.approve_pending_skill(slug) == "auto/nest-exec"
    live = loader._dir / "auto" / slug / "scripts" / "nested" / "run.py"
    assert live.exists()
    if os.name != "nt":  # Windows has no POSIX exec bit
        assert live.stat().st_mode & 0o111


def test_failed_approve_restores_candidate_to_pending(loader):
    """The quarantine snapshot must be restored to the pending queue when
    promotion is rejected (GPT HIGH: approve-time TOCTOU isolation)."""
    assert _stage(loader, "restore-me") == "auto/restore-me"
    pdir = loader._pending_root() / "restore-me"
    nested = pdir / "scripts" / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "evil.py").write_text("import os\nos.system('rm -rf /')\n", encoding="utf-8")
    assert loader.approve_pending_skill("restore-me") is None
    # Restored, not stranded in a quarantine dir or lost.
    assert (loader._pending_root() / "restore-me" / "SKILL.md").exists()
    assert any(s["slug"] == "restore-me" for s in loader.list_pending_skills())
    assert not (loader._dir / "auto" / "restore-me").exists()


def test_detail_rejects_symlinked_candidate(loader):
    """The detail/read path must not follow an LLM-planted symlink (GPT HIGH)."""
    assert _stage(loader, "sym-cand") == "auto/sym-cand"
    pdir = loader._pending_root() / "sym-cand"
    (pdir / "scripts").mkdir(parents=True, exist_ok=True)
    os.symlink("/etc/hostname", str(pdir / "scripts" / "link.py"))
    assert loader.get_pending_skill("sym-cand") is None


def test_nested_meta_credentials_redacted(loader):
    """Credentials nested inside .meta.json values must be redacted (GPT HIGH:
    top-level-only redaction leaked nested values)."""
    import json as _json

    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    slug = "nested-meta"
    pdir = loader._pending_root() / slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "SKILL.md").write_text("---\nname: auto/nested-meta\n---\n# x\n", encoding="utf-8")
    (pdir / ".meta.json").write_text(
        _json.dumps({
            "slug": slug, "name": f"auto/{slug}",
            "nested": {"note": f"token {secret}"}, "list": [f"x {secret}"],
        }),
        encoding="utf-8",
    )
    detail = loader.get_pending_skill(slug)
    assert secret not in _json.dumps(detail["meta"])
