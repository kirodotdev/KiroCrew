"""The report's ownership check and the scan budgets.

S1 the plan write followed a link -- ``write_text`` at the plan path, and a DANGLING link is
   the worst case because ``write_text`` creates the target. The staging marker and the report
   both went through ``_write_nofollow`` already; the plan did not.

S2 the report truncated any file at its name -- no-follow settles WHERE the write lands and
   says nothing about whether the file there is ours. Truncating on a name is the mistake the
   plan-only directory check already learned, which is why the payload now carries a version.

S3 a JUNCTION is not a symlink -- ``is_symlink()`` returns False for one, and
   ``shutil.rmtree`` TRAVERSES a junction on Windows rather than unlinking it as it does a
   symlink. Both the root check and the tree-wide shape predicate asked the narrow question.

S4 (mine, from an earlier finding) the base64 budget used ``break`` while scanning longest-run-first, so
   one oversized run exited the loop before anything was read. A memory bound became an off
   switch, and including a big blob is trivial.

S5 ``rglob("SKILL.md")`` matches a NAME -- a FIFO, a directory or a non-UTF-8 file all became
   candidates, and the credential scan skipped exactly the ones it could not read, so they
   shipped unblocked with no usable instructions.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)

_DOC_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_NO_REDACTOR = (
    "    _CANONICAL_REDACTOR: Callable[[str], tuple[str, list[str]]] | None = redact_credentials",
    "    _CANONICAL_REDACTOR = None",
)


def _build_py() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "build.py"


def _build(mod, home: pathlib.Path, work: pathlib.Path, select):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    work.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, work, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, work / "bundle")


# ---------------------------------------------------------------------------
# S1
# ---------------------------------------------------------------------------
@_posix_only
def test_the_plan_write_refuses_a_dangling_symlink(tmp_path: pathlib.Path) -> None:
    """A dangling link is the worst case: ``write_text`` would CREATE the target."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    work.mkdir()
    elsewhere = tmp_path / "elsewhere" / "planted.json"
    elsewhere.parent.mkdir()
    (work / mod.PLAN_FILENAME).symlink_to(elsewhere)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused):
        mod.write_plan(work / mod.PLAN_FILENAME, crew.name, mod.enumerate_all(crew, spec))
    assert not elsewhere.exists(), "the write followed the link and created its target"


@_posix_only
def test_writing_a_plan_normally_still_works(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the ordinary plan write, and rewriting over our own plan."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    work.mkdir()
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    target = work / mod.PLAN_FILENAME
    for _ in range(2):
        mod.write_plan(target, crew.name, mod.enumerate_all(crew, spec))
        assert json.loads(target.read_text(encoding="utf-8"))["plan_version"] == mod.PLAN_VERSION


# ---------------------------------------------------------------------------
# S2
# ---------------------------------------------------------------------------
def test_a_foreign_file_at_the_report_path_is_refused(tmp_path: pathlib.Path) -> None:
    """A plain file with the report's name is not proof it is the report."""
    mod = load_build()
    report = tmp_path / "work" / "bundle.smc-bundle.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"something": "the operator wrote this"}', encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unless_our_report(report, tmp_path / "work" / "bundle")
    assert "report_version" in str(caught.value)
    assert "operator wrote this" in report.read_text(encoding="utf-8"), "it was truncated"


def test_our_own_report_is_replaced_without_complaint(tmp_path: pathlib.Path) -> None:
    """Rebuilding over the same --out is the ordinary case and must not refuse."""
    mod = load_build()
    report = tmp_path / "bundle.smc-bundle.json"
    out = tmp_path / "bundle"
    report.write_text(
        json.dumps({"report_version": mod.REPORT_VERSION, "bundle_dir": str(out)}),
        encoding="utf-8",
    )
    mod._refuse_unless_our_report(report, out)  # no raise
    mod._refuse_unless_our_report(tmp_path / "absent.smc-bundle.json", out)  # absent is fine


def test_a_report_with_the_wrong_version_is_refused(tmp_path: pathlib.Path) -> None:
    """The field has to MATCH, not merely be present."""
    mod = load_build()
    report = tmp_path / "bundle.smc-bundle.json"
    out = tmp_path / "bundle"
    report.write_text(
        json.dumps({"report_version": mod.REPORT_VERSION + 99, "bundle_dir": str(out)}),
        encoding="utf-8",
    )
    with pytest.raises(mod.ExportRefused):
        mod._refuse_unless_our_report(report, out)


@_posix_only
def test_the_build_itself_refuses_a_foreign_report(tmp_path: pathlib.Path) -> None:
    """Driven through ``main``, because the three tests above only prove the FUNCTION works.

    Removing the call from the writer left all of them green: they call the check directly, so
    they say nothing about whether anything reaches it. This one plants the file and runs the
    real command, so it fails if the call site is dropped.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    work.mkdir()
    foreign = work / "bundle.smc-bundle.json"
    foreign.write_text('{"something": "the operator wrote this"}', encoding="utf-8")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, work, select={"skills": {"faq"}})
    code = mod.main(
        [
            "build",
            "--crew",
            "frontdesk",
            "--source",
            str(home),
            "--allow",
            str(plan_path),
            "--out",
            str(work / "bundle"),
        ]
    )
    assert code != 0, "the build did not refuse"
    assert "operator wrote this" in foreign.read_text(encoding="utf-8"), "it was truncated"


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------
def test_the_shape_predicate_reports_a_symlink(tmp_path: pathlib.Path) -> None:
    """The POSIX half of the reparse question, which is all this host can plant."""
    mod = load_build()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert mod._is_shape_this_build_never_writes(link)
    assert not mod._is_shape_this_build_never_writes(target)


def test_the_shape_predicate_asks_the_reparse_question() -> None:
    """A SOURCE rule, because no test on this host can plant a junction.

    ``is_symlink()`` returns False for a Windows junction and ``shutil.rmtree`` traverses one
    there, so a symlink-only test would let the recursive delete loose on the junction's
    target. Only the source can say which question the code asks.
    """
    fn = next(
        n
        for n in ast.walk(ast.parse(_build_py().read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "_is_shape_this_build_never_writes"
    )
    attr_calls = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    name_calls = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_is_redirecting_entry" in name_calls, f"calls: {name_calls | attr_calls}"
    assert "is_symlink" not in attr_calls, "the narrow test is back; a junction would pass"


# ---------------------------------------------------------------------------
# S4
# ---------------------------------------------------------------------------
def test_an_oversized_blob_does_not_disable_the_encoded_scan() -> None:
    """One huge run must not stop the shorter one carrying the credential being read.

    The runs are examined longest-first, so the oversized one is seen BEFORE the credential.
    With ``break`` that ended the scan; with ``continue`` it is skipped and the rest is read.
    """
    mod = load_build(mutate=_NO_REDACTOR)
    assert mod._CANONICAL_REDACTOR is None, "the mutation did not take"
    huge = "A" * (mod._B64_DECODE_BUDGET + 1024)
    encoded = base64.b64encode(f"aws_secret_access_key = {_DOC_SECRET}".encode()).decode()

    kinds = [leak.kind for leak in mod.scan_text(f"# notes\n{huge}\n{encoded}\n", "t")]
    assert any(k.startswith("encoded-") for k in kinds), f"the scan was disabled: {kinds}"


def test_the_decode_budget_still_bounds_the_work() -> None:
    """The budget bounds the DECODING, and what it cannot read it reports.

    This test before this change asserted the result was clean, which was the flaw a later round
    named: a run past the budget went unscanned and the build said the content had been
    scanned. Bounding the work and reporting the gap are both required -- so the assertion is
    now that the finding names the unscanned runs rather than that there is no finding.
    """
    mod = load_build(mutate=_NO_REDACTOR)
    blob = "A" * (mod._B64_DECODE_BUDGET + 16)
    leaks = mod.scan_text("\n".join([blob] * 4), "t")
    assert leaks, "the oversized runs were silently accepted as clean"
    assert all(leak.kind == "unscannable-encoded" for leak in leaks), [x.kind for x in leaks]
    assert "NOT scanned" in leaks[0].snippet


def test_ordinary_text_does_not_trip_the_budget() -> None:
    """Non-vacuity: the report must fire on the BUDGET, not on every text.

    Without this, "fail closed" could mean refusing every build, which the earlier version of
    this test was implicitly guarding against by asserting cleanliness.
    """
    mod = load_build(mutate=_NO_REDACTOR)
    assert not mod.scan_text("# FAQ\nStore hours are 9 to 6.\n", "t")
    assert not mod.scan_text("A" * 64 + "\n", "t")


# ---------------------------------------------------------------------------
# S5
# ---------------------------------------------------------------------------
@_posix_only
def test_a_skill_whose_instructions_are_not_utf8_is_blocked(tmp_path: pathlib.Path) -> None:
    """It must be BLOCKED and named, not silently dropped or silently shipped."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    bad = home / "skills" / "binary"
    bad.mkdir()
    (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00not utf-8 at all\x00")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    entry = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "binary")
    assert entry.blocked, "a skill with unreadable instructions was selectable"
    assert "UTF-8" in entry.blocked


@_posix_only
def test_a_skill_md_that_is_a_directory_is_blocked(tmp_path: pathlib.Path) -> None:
    """``rglob`` matched the NAME, so a directory with that name became a candidate."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    (home / "skills" / "weird" / "SKILL.md").mkdir(parents=True)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    entry = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "weird")
    assert entry.blocked
    assert "regular file" in entry.blocked


@_posix_only
def test_a_symlinked_skill_md_is_blocked(tmp_path: pathlib.Path) -> None:
    """A link at SKILL.md is refused on shape, whatever it points at."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    real = tmp_path / "outside.md"
    real.write_bytes(b"# borrowed\n")
    linked = home / "skills" / "linked"
    linked.mkdir()
    (linked / "SKILL.md").symlink_to(real)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    entry = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "linked")
    assert entry.blocked
    # A SKILL.md that is itself a link is a redirecting component, caught by the pre-read
    # redirect guard (which fires before the resolving is_file() check and names the link).
    assert "link or junction" in entry.blocked or "regular file" in entry.blocked


@_posix_only
def test_an_ordinary_skill_is_still_selectable(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a plain UTF-8 SKILL.md must remain unblocked and shippable."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nplain text\n"}})
    work = tmp_path / "work"
    report = _build(mod, home, work, {"skills": {"faq"}})
    assert report.skill_count == 1
    assert (work / "bundle" / "skills" / "faq" / "SKILL.md").is_file()


def _skill_source(root: pathlib.Path) -> pathlib.Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("reviewed instructions\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    return root


def test_a_file_only_in_the_staging_tree_changes_the_approval_hash(
    tmp_path: pathlib.Path,
) -> None:
    """A path in staging that the source lacks is caught: its bytes ship, so it hashes.

    The scenario is a file added to the source mid-copy, shipped by ``_copy_skill``, then
    removed from the source before the hash runs. The verification set is derived from what
    ships, so a staged-only file contributes its own row and the tainted tree hashes
    differently from the clean one -- the caller's pin comparison then refuses the build.

    The constraint this must not break: for a legitimate copy (a SUBSET of the source) the
    value still equals ``_tree_hash(source)``, pinned by
    ``test_the_hash_equals_a_source_pin_for_a_legitimate_build``.
    """
    mod = load_build()
    source = _skill_source(tmp_path / "source")

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "SKILL.md").write_text("reviewed instructions\n", encoding="utf-8")

    tainted = tmp_path / "tainted"
    tainted.mkdir()
    (tainted / "SKILL.md").write_text("reviewed instructions\n", encoding="utf-8")
    (tainted / "EXTRA.md").write_text("unreviewed instructions\n", encoding="utf-8")

    assert mod._staged_tree_hash(clean, source, {"SKILL.md"}) != mod._staged_tree_hash(
        tainted, source, {"SKILL.md", "EXTRA.md"}
    )


def test_the_hash_equals_a_source_pin_for_a_legitimate_build(tmp_path: pathlib.Path) -> None:
    """The constraint the union fix violated, stated as a test.

    ``_staged_tree_hash`` is compared against ``_tree_hash(source)``. For a skill whose
    copy shipped everything, the two must agree -- so any future change that adds rows the
    source walk does not produce reddens here instead of on a Windows shard.
    """
    mod = load_build()
    source = tmp_path / "source"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("reviewed instructions\n", encoding="utf-8")

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SKILL.md").write_text("reviewed instructions\n", encoding="utf-8")

    assert mod._staged_tree_hash(staged, source, {"SKILL.md"}) == mod._tree_hash(source)


def test_a_binary_the_copy_drops_still_hashes_from_the_source(tmp_path: pathlib.Path) -> None:
    """Guards the union walk from being satisfied by refusing every legitimate skill.

    ``_copy_skill`` drops a file it cannot decode, so the staged tree is a SUBSET of the
    source for any skill carrying an image. Hashing the staged tree alone would refuse
    those skills, which an earlier version of this check did.
    """
    mod = load_build()
    source = _skill_source(tmp_path / "source")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SKILL.md").write_text("reviewed instructions\n", encoding="utf-8")

    first = mod._staged_tree_hash(staged, source, {"SKILL.md"})
    assert first == mod._staged_tree_hash(staged, source, {"SKILL.md"})

    (source / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00DIFFERENT")
    assert mod._staged_tree_hash(staged, source, {"SKILL.md"}) != first


# ---------------------------------------------------------------------------
# Round-25: values the build wrote and reads back cannot be assumed unchanged.
# A staged file _copy_skill wrote that then vanished must REFUSE, not fall back
# to source bytes (which counts the disappearance as reviewed).
# ---------------------------------------------------------------------------
def test_a_written_then_vanished_staged_file_is_refused_not_counted_reviewed(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("reviewed\n", encoding="utf-8")
    (source / "extra.md").write_text("also reviewed\n", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SKILL.md").write_text("reviewed\n", encoding="utf-8")
    # extra.md was WRITTEN by the copy but is absent from staging now (vanished mid-build).
    with pytest.raises(mod.ExportRefused) as caught:
        mod._staged_tree_hash(staged, source, {"SKILL.md", "extra.md"})
    assert "extra.md" in str(caught.value) and "missing" in str(caught.value)


def test_a_source_file_the_copy_never_wrote_still_hashes_from_source(
    tmp_path: pathlib.Path,
) -> None:
    """Non-vacuity: a not-written (nested-excluded) source file is NOT a tamper -- source bytes."""
    mod = load_build()
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("reviewed\n", encoding="utf-8")
    (source / "nested.md").write_text("belongs to an unselected nested skill\n", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SKILL.md").write_text("reviewed\n", encoding="utf-8")
    # nested.md not in the written set -> legitimately absent -> hashed from source, no refuse.
    h = mod._staged_tree_hash(staged, source, {"SKILL.md"})
    assert h  # returns a hash rather than raising
