"""Transaction cleanup, and a scan budget that fails closed.

X1 the report was written by the CALLER, after ``build_bundle`` had renamed the previous
   bundle aside and deleted it. So a report write that failed left a non-zero exit code, no
   report, and the operator's previous bundle gone -- a failure that had already replaced what
   it was going to replace. Everything the report says is known before the swap, so it is
   written there, inside the transaction that restores the previous bundle.

X2 the carried plan was read OUTSIDE the cleanup transaction, so an unreadable plan raised a
   bare ``OSError`` past every handler and stranded the staging tree AND its marker. The marker
   is the worse half: it is what authorises the next run's recursive delete.

X3 the base64 decode budget skipped what it could not afford and said nothing. ``break`` let
   one oversized run disable the scan and ``continue`` fixed that, but both reported unscanned
   content as clean. It now appends a finding naming what was not read.
"""

from __future__ import annotations

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


def _build(mod, home: pathlib.Path, out: pathlib.Path, select):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, out.parent, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, out)


# ---------------------------------------------------------------------------
# X1
# ---------------------------------------------------------------------------
@_posix_only
def test_the_report_exists_by_the_time_the_bundle_is_promoted(tmp_path: pathlib.Path) -> None:
    """``build_bundle`` writes it, so no later step can fail with the swap already done."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    report = _build(mod, home, out, {"skills": {"faq"}})

    written = out.parent / f"{out.name}.smc-bundle.json"
    assert written.is_file(), "build_bundle did not write the report"
    body = json.loads(written.read_text(encoding="utf-8"))
    assert body["report_version"] == mod.REPORT_VERSION
    assert body["digest"] == report.digest
    assert body["skill_count"] == report.skill_count == 1


@_posix_only
def test_a_failing_report_write_leaves_the_previous_bundle(tmp_path: pathlib.Path) -> None:
    """The property the move buys: a report failure must not have replaced anything.

    The write is made to fail by putting a DIRECTORY at the report path after the first build,
    which ``_write_nofollow`` refuses on shape. Then the second build must fail with the first
    bundle still in place and readable.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    first = _build(mod, home, out, {"skills": {"faq"}})

    report_path = out.parent / f"{out.name}.smc-bundle.json"
    report_path.unlink()
    report_path.mkdir()

    with pytest.raises(mod.ExportRefused):
        _build(mod, home, out, {"skills": {"faq"}})

    assert (out / "manifest.json").is_file(), "the previous bundle is gone"
    assert (
        json.loads((out / "manifest.json").read_text(encoding="utf-8"))["digest"] == first.digest
    ), "the bundle was replaced by a build that then failed"
    assert not (out.parent / f"{out.name}.previous").exists(), "the aside copy was stranded"
    assert not (out.parent / f"{out.name}.staging").exists(), "staging was stranded"


# ---------------------------------------------------------------------------
# X2
# ---------------------------------------------------------------------------
@_posix_only
def test_an_unreadable_carried_plan_refuses_and_cleans_up(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Refused, and neither the staging tree nor its marker is left behind.

    The marker matters more than the tree: a stranded marker is what authorises the NEXT run's
    recursive delete of whatever sits at that path.

    The read is made to fail by patching it rather than by ``chmod(0o000)``. Two reasons, both
    learned from CI: chmod does not make a file unreadable on Windows, so the Windows shard
    reported DID NOT RAISE -- the fixture was inert, not the handler wrong. And a bare
    ``chmod(0o000)`` is a lockdown-gate finding needing a written exemption, which is a lot of
    ceremony for a fixture whose only job is to make one read raise.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    plan = out / mod.PLAN_FILENAME
    plan.write_text(json.dumps({"plan_version": mod.PLAN_VERSION}), encoding="utf-8")
    real_read = mod._read_bytes_openat

    def _fail_on_the_plan(root, rel, *args, **kwargs):
        # The carried-plan read goes through the whole-window bytes reader now, not
        # Path.read_bytes; an unreadable-or-redirected plan surfaces as a None return there.
        if pathlib.Path(rel).name == mod.PLAN_FILENAME:
            return None
        return real_read(root, rel, *args, **kwargs)

    monkeypatch.setattr(mod, "_read_bytes_openat", _fail_on_the_plan)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})

    assert "cannot be read" in str(caught.value)
    assert not (out.parent / f"{out.name}.staging").exists(), "the staging tree was stranded"
    assert not (
        out.parent / f"{out.name}.staging.owned"
    ).exists(), "the marker was stranded, licensing the next run's delete"


@_posix_only
def test_a_readable_carried_plan_is_still_carried(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the ordinary two-verb flow must keep working."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})
    (out / mod.PLAN_FILENAME).write_text(json.dumps({"edited": True}) + "\n", encoding="utf-8")
    _build(mod, home, out, {"skills": {"faq"}})
    assert json.loads((out / mod.PLAN_FILENAME).read_text(encoding="utf-8")) == {"edited": True}


# ---------------------------------------------------------------------------
# X3
# ---------------------------------------------------------------------------
def test_runs_past_the_budget_are_reported_not_silently_skipped() -> None:
    """Unscanned must not be reported as clean."""
    mod = load_build(mutate=_NO_REDACTOR)
    blob = "A" * (mod._B64_DECODE_BUDGET + 16)
    leaks = mod.scan_text("\n".join([blob] * 3), "t")
    assert leaks, "the unscanned runs were accepted as clean"
    assert any(leak.kind == "unscannable-encoded" for leak in leaks)


def test_a_credential_is_still_found_alongside_an_oversized_run() -> None:
    """The earlier fix must survive: one big run cannot hide a smaller real finding."""
    mod = load_build(mutate=_NO_REDACTOR)
    import base64

    huge = "A" * (mod._B64_DECODE_BUDGET + 1024)
    encoded = base64.b64encode(f"aws_secret_access_key = {_DOC_SECRET}".encode()).decode()
    kinds = [leak.kind for leak in mod.scan_text(f"# notes\n{huge}\n{encoded}\n", "t")]
    assert any(k.startswith("encoded-") for k in kinds), kinds


def test_ordinary_text_is_still_clean() -> None:
    """Non-vacuity: failing closed must not mean refusing every build."""
    mod = load_build(mutate=_NO_REDACTOR)
    assert not mod.scan_text("# FAQ\nStore hours are 9 to 6.\n", "t")
    assert not mod.scan_text("digest: " + "9f8c2b1e" * 8 + "\n", "t")
