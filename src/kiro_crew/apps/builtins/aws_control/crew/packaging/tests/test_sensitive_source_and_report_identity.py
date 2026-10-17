"""A sensitive --source, and what identifies a report as ours.

Each was real, and the first was mine: the comment above it argued the fail-open was a
deliberate accommodation for standalone mode. That argument holds for refusing outright and
does not hold for skipping the check, which is what the code did -- so standalone was the one
mode where a sensitive ``--source`` was read and bundled.

This module keeps the sensitive-source fence, the order of promotion and report publication
(the report is published after the promotion and rolled back with a failed one, and a report
directory that cannot hard-link is refused before the promotion), and the identity checks --
on the report, a verified tree, the staging marker and the staging inode -- that decide what a
build may replace or delete. The rest of the suite is split by topic into
``test_sensitive_source_and_report_identity_credential_floor.py``,
``test_sensitive_source_and_report_identity_nofollow_reads.py`` and
``test_sensitive_source_and_report_identity_staging_and_disposal.py``; ``_build`` and
``_posix_only`` are shared from ``sensitive_source_and_report_identity_helpers.py``.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from .sensitive_source_and_report_identity_helpers import _build, _posix_only
from .test_producer import (
    load_build,
    make_crew,
    transaction_source,
)


def test_a_sensitive_source_is_refused_even_without_the_shared_validator() -> None:
    """The standalone fence answers the question the shared one cannot be asked.

    Drives the predicate directly for the paths, and the fence's placement is pinned by the
    build-level test below -- a predicate that is never consulted passes this and does
    nothing.
    """
    mod = load_build()
    for sensitive in (
        "/home/someone/.aws/credentials",
        "/home/someone/.ssh/id_rsa",
        "/home/someone/.config/gcloud/application_default_credentials.json",
        "/home/someone/.kube/config",
        "/home/someone/.kiro/crew-auth-staging/thing.json",
    ):
        assert mod._looks_sensitive_standalone(sensitive), sensitive


def test_the_standalone_fence_matches_components_not_substrings() -> None:
    """``~/projects/sshconfig-notes`` is not ``~/.ssh``.

    A substring test would refuse an operator's ordinary directory, and a fence that fires on
    innocent paths gets deleted rather than fixed.
    """
    mod = load_build()
    for innocent in (
        "/home/someone/projects/sshconfig-notes/agents/a.json",
        "/home/someone/awsnotes/agents/a.json",
        "/home/someone/my.ssh.backup.txt",
        "/home/someone/gnupg-docs/agents/a.json",
    ):
        assert not mod._looks_sensitive_standalone(innocent), innocent


def test_the_two_part_entries_need_consecutive_components() -> None:
    """``.config/gcloud`` is two components in order, not two names anywhere."""
    mod = load_build()
    assert mod._looks_sensitive_standalone("/home/x/.config/gcloud/creds.json")
    assert not mod._looks_sensitive_standalone("/home/x/.config/other/gcloud-notes/a.json")


def test_the_build_consults_the_standalone_fence_on_the_spec_path(
    tmp_path: pathlib.Path,
) -> None:
    """Driven through the real build, so the guard's PLACEMENT is what is tested.

    The lesson this pins: a fence proven only by calling its predicate says nothing about
    whether the read path reaches it. No hook is needed to simulate standalone mode, because
    the local fence now runs unconditionally -- which is the fix. Under the old code this
    same crew was read and bundled whenever the shared validator was unimportable.
    """
    mod = load_build()
    home = make_crew(tmp_path / ".aws" / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, tmp_path / "out", {"skills": {"faq"}})
    msg = str(caught.value)
    if os.name == "posix":
        assert "sensitive" in msg
    else:
        assert "POSIX-only" in msg


def test_a_foreign_json_carrying_the_version_key_is_still_refused(
    tmp_path: pathlib.Path,
) -> None:
    """``report_version`` alone authorised truncating unrelated data.

    It is a generic key. Any document that happens to carry ``"report_version": 1`` read as
    this tool's own output, and the build then replaced it.
    """
    mod = load_build()
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    foreign = out.parent / f"{out.name}.smc-bundle.json"
    foreign.write_text(
        json.dumps({"report_version": mod.REPORT_VERSION, "notes": "someone else's file"}),
        encoding="utf-8",
    )

    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unless_our_report(foreign, out)
    assert "did not write it" in str(caught.value)
    assert json.loads(foreign.read_text(encoding="utf-8"))["notes"] == "someone else's file"


def test_our_own_report_naming_this_bundle_is_accepted(tmp_path: pathlib.Path) -> None:
    """The other half: a rebuild over this tool's own report is the ordinary case.

    Without this the fix would read as "refuse everything", which no test above would catch.
    """
    mod = load_build()
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    ours = out.parent / f"{out.name}.smc-bundle.json"
    ours.write_text(
        json.dumps({"report_version": mod.REPORT_VERSION, "bundle_dir": str(out)}),
        encoding="utf-8",
    )
    mod._refuse_unless_our_report(ours, out)


def test_a_report_naming_a_different_bundle_is_refused(tmp_path: pathlib.Path) -> None:
    """Same version, different destination: not the report this build would replace."""
    mod = load_build()
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    stale = out.parent / f"{out.name}.smc-bundle.json"
    stale.write_text(
        json.dumps(
            {"report_version": mod.REPORT_VERSION, "bundle_dir": str(tmp_path / "elsewhere")}
        ),
        encoding="utf-8",
    )
    with pytest.raises(mod.ExportRefused):
        mod._refuse_unless_our_report(stale, out)


@_posix_only
def test_a_failed_promotion_leaves_no_report_behind(tmp_path: pathlib.Path, monkeypatch) -> None:
    """A rename failure rolls the report back with the bundle.

    The report is written before the swap on purpose, so a report failure cannot land after
    the previous bundle is gone. That ordering left the other hole: the swap failed, the
    previous bundle came back, and the report still described the bundle that never landed.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"

    real_rename = os.rename

    def _fail_the_promotion(src, dst, *args, **kwargs):
        if str(src).endswith(".staging"):
            raise OSError(13, "promotion refused")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _fail_the_promotion)
    with pytest.raises(OSError):
        _build(mod, home, out, {"skills": {"faq"}})

    report = out.parent / f"{out.name}.smc-bundle.json"
    assert not report.exists(), "the report describes a bundle that never landed"
    assert not out.exists(), "no bundle was installed"


@_posix_only
def test_a_failed_promotion_restores_a_previous_report_verbatim(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The rollback puts the earlier bytes back rather than deleting them.

    Distinguishes the two branches: deleting unconditionally would pass the test above and
    destroy the previous build's report here.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    report = out.parent / f"{out.name}.smc-bundle.json"
    first = report.read_bytes()
    assert json.loads(first.decode("utf-8"))["bundle_dir"] == str(out)

    real_rename = os.rename

    def _fail_the_promotion(src, dst, *args, **kwargs):
        if str(src).endswith(".staging"):
            raise OSError(13, "promotion refused")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _fail_the_promotion)
    with pytest.raises(OSError):
        _build(mod, home, out, {"skills": {"faq"}})

    assert report.read_bytes() == first, "the previous build's report was not restored"


# ---------------------------------------------------------------------------
# Round-21 promotion/report ordering: the report is the proof an operator reads
# instead of checking the bundle, so it is published AFTER promotion, never on an
# assumed outcome. A failed promotion must leave no success report and keep the
# prior bundle in place.
# ---------------------------------------------------------------------------
def test_the_report_is_published_after_the_promotion_not_before() -> None:
    """Source order: staging.rename(out_dir) precedes the report publish.

    Writing the report before the promotion left a report claiming success when the promotion
    then failed -- a lie in the one artifact offered as evidence the bundle exists.
    """
    src = transaction_source()
    promote_at = src.index("staging.rename(out_dir)")
    report_at = src.index("_publish_report(report_tmp, report_path")
    assert 0 <= promote_at < report_at, (
        "the report is published before the promotion completes, so a failed promotion can "
        "leave a report that falsely claims the bundle exists"
    )


@pytest.mark.skipif(os.name != "posix", reason="drives the builder; POSIX-only per the guard")
def test_a_failed_promotion_writes_no_success_report_and_keeps_the_previous_bundle(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """If the promotion rename fails, no report claims success and the prior bundle stays."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build: a real bundle + report
    report = out.parent / (out.name + ".smc-bundle.json")
    first_report = report.read_bytes()
    first_digest = json.loads(first_report)["digest"]

    # Make the PROMOTION rename fail, after the report tmp is written and shape-checked.
    # Key on the SOURCE being the staging tree, so the rollback's previous->out restore
    # (also dst==out) is left to succeed -- otherwise the test would block its own recovery.
    real_rename = os.rename

    def _fail_promotion(src, dst, *a, **k):
        if str(src).endswith(".staging"):
            raise OSError("promotion blocked")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _fail_promotion)
    with pytest.raises((mod.ExportRefused, OSError)):
        _build(mod, home, out, {"skills": {"faq"}})

    # The previous bundle is still reachable and unchanged.
    assert (out / "skills" / "faq" / "SKILL.md").is_file(), "the prior bundle was left in place"
    # The report was not overwritten to claim the failed build: it still describes the first.
    assert report.read_bytes() == first_report, "no report claims the failed promotion"
    assert json.loads(report.read_bytes())["digest"] == first_digest


@_posix_only
def test_a_failed_report_publication_does_not_leave_the_previous_report_behind(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A report that cannot be published must be absent, never the previous build's.

    The ordering here promotes first and publishes after, and accepts a MISSING report as the
    cost, because a missing one is regenerated. On a rebuild the outcome without this cleanup is
    different in kind: the earlier build's report stays on disk and now describes a bundle that
    is gone. Measured before the cleanup -- the file was byte-identical to the first build's,
    digest included, while the second bundle was promoted.

    The publication failure is injected at ``os.link`` because nothing about a real
    filesystem makes the publish fail on demand. What is asserted is the state left behind.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    first = _build(mod, home, out, {"skills": {"faq"}})
    report = out.parent / f"{out.name}.smc-bundle.json"
    before = report.read_text(encoding="utf-8")
    assert first.digest in before, "the fixture must start from a report describing build one"

    (home / "skills" / "faq" / "SKILL.md").write_text("# FAQ v2\n", encoding="utf-8")
    real_link = mod.os.link

    def _link(src, dst, *args, **kwargs):
        if str(dst).endswith(".smc-bundle.json"):
            raise OSError(5, "Input/output error")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(mod.os, "link", _link)
    with pytest.raises(OSError):
        _build(mod, home, out, {"skills": {"faq"}})
    monkeypatch.undo()

    assert not report.exists(), (
        "the previous build's report survived a failed publication, so it now describes a "
        "bundle that is no longer there"
    )


# ---------------------------------------------------------------------------
# Round-23 GPT: the ownership verifier must check the ANCHOR (the root) itself,
# not only what is relative to it. A symlinked root would have its TARGET verified
# and then the recursive delete keyed to that verdict would run through the link.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_the_ownership_verifier_refuses_a_symlinked_root(tmp_path: pathlib.Path) -> None:
    mod = load_build()
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "out"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unless_this_build_wrote_it(link, "--out", "frontdesk")
    assert "symlink or reparse point" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_MUTATION_without_the_anchor_check_a_symlinked_root_is_verified_by_its_target(
    tmp_path: pathlib.Path,
) -> None:
    """Remove the anchor check and the verifier follows the link, judging the target instead.

    With the anchor check gone, a symlinked root is not refused up front; the verifier
    proceeds to ``d.exists()``/``iterdir()``, which follow the link and validate the TARGET --
    the exact "verified the wrong tree" the anchor check prevents. Proven because the refusal
    does not name a symlink (it gets past the anchor line to a downstream verdict instead).
    """
    mod = load_build(
        mutate=(
            "    if _pinned._is_redirecting_entry(d):\n        # The ANCHOR, before anything relative to it.",
            "    if False and _pinned._is_redirecting_entry(d):\n        # The ANCHOR, before anything relative to it.",
        )
    )
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "out"
    link.symlink_to(target, target_is_directory=True)
    # With the guard off, the symlinked root is NOT refused as a symlink; it follows into the
    # (empty, build-unowned) target and refuses for a different reason, or passes -- either way
    # not the anchor refusal, proving the anchor check is what catches the link.
    try:
        mod._refuse_unless_this_build_wrote_it(link, "--out", "frontdesk")
    except mod.ExportRefused as exc:
        assert "symlink or reparse point" not in str(exc), (
            "anchor check removed: the verifier followed the link to its target instead of "
            "refusing the root, proving the anchor check is load-bearing"
        )


# ---------------------------------------------------------------------------
# Round-24 GPT: an unreadable marker must not escape _marker_is_ours as a raw
# OSError -- the function's contract is a bool, and "cannot read it" answers no
# (not confirmably ours), which makes the caller refuse rather than delete.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make a marker unreadable")
def test_an_unreadable_marker_answers_not_ours_rather_than_crashing(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    marker = tmp_path / "bundle.staging.owned"
    marker.write_text("kiro-crew-bundle-staging-marker/1\n", encoding="utf-8")
    os.chmod(marker, 0o000)  # lockdown-ok: a test making its own tmp file unreadable to exercise EACCES; no payload, not published  # noqa: E501  # fmt: skip
    try:
        # No raise; an unreadable marker is not confirmably this run's, so the answer is False.
        assert mod._marker_is_ours(marker) is False
    finally:
        os.chmod(marker, 0o600)  # lockdown-ok: test permission restore of a tmp file this test created, not a publish  # noqa: E501  # fmt: skip


@pytest.mark.skipif(
    os.name != "posix", reason="uses chmod 000 to make an existing report unreadable"
)
def test_an_unreadable_report_refusal_releases_the_staging_tree_and_marker(
    tmp_path: pathlib.Path,
) -> None:
    """The refusal on an unreadable existing report must not leak the staging tree/marker.

    The marker is worse than the tree: the next run reads a stray marker as another build's
    claim and refuses on it, turning one refusal into a standing one.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build writes a report
    report = out.parent / (out.name + ".smc-bundle.json")
    os.chmod(report, 0o000)  # lockdown-ok: a test making its own tmp report unreadable to exercise EACCES; not published  # noqa: E501  # fmt: skip
    try:
        with pytest.raises(mod.ExportRefused) as caught:
            _build(mod, home, out, {"skills": {"faq"}})
        assert "cannot be read" in str(caught.value)
        leftovers = [
            q.name
            for q in out.parent.iterdir()
            if q.name.endswith(".staging") or q.name.endswith(".staging.owned")
        ]
        assert leftovers == [], f"the refusal leaked staging/marker: {leftovers}"
    finally:
        os.chmod(report, 0o644)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # lockdown-ok: test permission restore, not a publish  # noqa: E501  # fmt: skip


# ---------------------------------------------------------------------------
# Same object, different content: the fourth property. A concurrent process that
# edits the report IN PLACE leaves the same readable object with different bytes;
# a shape check alone says fine and the publish would supersede that edit. The build
# owns the report exclusively for one build, so drift is REFUSED, not overwritten.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_concurrent_in_place_report_edit_is_refused_not_overwritten(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build writes a legitimate report
    report = out.parent / (out.name + ".smc-bundle.json")
    original = report.read_bytes()

    # A foreign editor rewrites the report IN PLACE (same inode) during the second build,
    # landed at the report-path shape check that runs immediately before the pre-promotion
    # content guard -- so the bytes at report_path differ from what the build read at its start,
    # and the guard fires BEFORE anything destructive (no promotion, no rollback unlink).
    real_redirect = mod._is_redirecting_entry
    tampered = b'{"tampered":"by another process"}\n'
    report_calls = {"n": 0}

    def _edit_report_mid_build(path):
        if str(path).endswith(".smc-bundle.json"):
            report_calls["n"] += 1
            # The FIRST report-path check is the report_before capture guard; tamper only on
            # the SECOND (the pre-promotion shape check), after report_before is already read,
            # so the edit lands inside the write-then-read-back window the guard covers.
            if report_calls["n"] == 2:
                report.write_bytes(tampered)
        return real_redirect(path)

    monkeypatch.setattr(mod, "_is_redirecting_entry", _edit_report_mid_build)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "edited by another process" in str(caught.value)
    # The foreign edit survives: it was refused before any destructive step, not clobbered.
    assert report.read_bytes() == tampered
    assert report.read_bytes() != original


@_posix_only
def test_the_rollback_preserves_a_drifted_report_instead_of_unlinking_it(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The promoted-not-written rollback removes only the STALE report it owns.

    ``_publish_report`` refuses to overwrite a concurrent foreign in-place edit; the rollback
    that runs after must not then destroy that same foreign write on the way out. The delete is
    conditional on the report still matching ``report_before`` -- a drifted report is left in
    place. Distinguishes the branches: an unconditional unlink would pass the stale-report tests
    above and silently delete the foreign write here.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build leaves a report (report_before)
    report = out.parent / f"{out.name}.smc-bundle.json"
    foreign = b'{"foreign":"a concurrent editor rewrote this after the build started"}\n'

    # Let the pre-promotion content check pass (report unchanged then), promotion succeed, then
    # a concurrent editor rewrites the report IN PLACE and publish refuses the drift -- landing
    # in the promoted-not-written rollback with a report whose bytes != report_before.
    real_publish = mod._publish_report

    def _drift_then_refuse(report_tmp, report_path, report_before):
        report.write_bytes(foreign)
        return real_publish(report_tmp, report_path, report_before)

    monkeypatch.setattr(mod, "_publish_report", _drift_then_refuse)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "edited by another process" in str(caught.value)
    # The foreign write survives the rollback: it was preserved, not unlinked.
    assert report.read_bytes() == foreign


# ---------------------------------------------------------------------------
# Opus 4.8: the crew spec leaf agents/<name>.json is operator-named and must not
# be caught by the final-component credential-name rule -- the shared validator
# does not treat it as sensitive, so the standalone floor must not be stricter.
# ---------------------------------------------------------------------------
def test_the_floor_does_not_refuse_a_crew_named_like_a_credential() -> None:
    """A crew the operator named ``credentials`` (or ``client_secret``, ``service_account``)
    has a spec at ``agents/<name>.json`` whose basename matches the credential-name rule -- but
    it is the crew's own spec, not a credential file, and the shared validator returns False for
    it. The floor must agree, or a legitimately named crew can never be built in standalone mode.
    """
    mod = load_build()
    for name in ("credentials", "client_secret", "service_account", ".env"):
        spec = f"home/user/.kiro/agents/{name}.json"
        assert mod._looks_sensitive_standalone(spec) is False, spec


def test_the_floor_still_catches_credential_leaves_and_non_json_under_agents() -> None:
    """Non-vacuity: the exemption is narrow -- only agents/<name>.json.

    A real credential leaf elsewhere, and a non-``.json`` credential file even directly under
    ``agents``, are still caught, so the exemption did not open a hole.
    """
    mod = load_build()
    assert mod._looks_sensitive_standalone("home/user/.kiro/crew/.env") is True
    assert mod._looks_sensitive_standalone("home/user/project/secret.pem") is True
    # A .pem under agents is not the .json spec leaf, so the name rule still fires.
    assert mod._looks_sensitive_standalone("home/user/.kiro/agents/id_rsa.pem") is True


# ---------------------------------------------------------------------------
# GPT 5.6 / Opus 4.8: a settle (disposal) that RAISES must not let the finally
# recursively delete the verified tree in the private aside. On a rebuild that
# tree is the operator's current bundle; a concurrent nonempty <out>.previous
# makes the settling os.rename fail (ENOTEMPTY), which must NOT destroy it.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_raising_settle_restores_the_verified_tree_instead_of_deleting_it(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("the operator's current bundle\n", encoding="utf-8")

    def _accept(parent_fd, moved_rel) -> None:
        return None  # verify passes: this is a build-written tree

    def _settle_that_fails(moved_rel: str, pfd: int) -> None:
        # Stand-in for os.rename(moved, previous) hitting a concurrent nonempty <out>.previous.
        raise OSError("settlement rename failed: destination not empty")

    # The settle failure re-raises after restoring the tree (the restore itself succeeds here),
    # so the original error surfaces -- what must NOT happen is a silent recursive delete.
    with pytest.raises(OSError) as caught:
        mod._dispose_via_private_aside(target, _accept, _settle_that_fails)
    assert "settlement rename failed" in str(caught.value)
    # The verified tree survives -- restored to its original path, never recursively deleted.
    assert target.is_dir()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "the operator's current bundle\n"


@_posix_only
def test_a_settle_failure_with_a_blocked_restore_retains_the_aside(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """If BOTH the settle and the restore-back fail, the tree is retained (aside kept), not deleted."""
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data\n", encoding="utf-8")

    real_rename = os.rename
    calls = {"n": 0}

    def _first_rename_ok_then_restore_fails(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:  # the restore rename (private -> target) fails
            raise OSError("restore blocked")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _first_rename_ok_then_restore_fails)

    def _accept(parent_fd, moved_rel) -> None:
        return None

    def _settle_that_fails(moved_rel: str, pfd: int) -> None:
        raise OSError("settlement failed")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._dispose_via_private_aside(target, _accept, _settle_that_fails)
    msg = str(caught.value)
    assert "NOT been deleted" in msg and "restoring it failed" in msg


# ---------------------------------------------------------------------------
# Round-GPT :5571 -- the publish side of the same inode rule the delete side earned.
# The promote rename re-derives the staging entry by NAME under the pinned parent, so a
# staging leaf swapped for another directory since staging_fd was opened would be promoted.
# Verify the name still resolves to the captured inode BEFORE the rename.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_swapped_staging_entry_is_refused_before_promotion(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A staging leaf whose inode does not match ``staging_fd`` is refused before promotion.

    The promote fstats the retained ``staging_fd`` and the freshly-reopened staging leaf and
    compares (st_dev, st_ino). Simulate the leaf having been swapped since capture by tampering
    the SECOND of that consecutive pair (the reopened leaf) so its inode differs. The check must
    refuse BEFORE the rename, leaving the previous bundle in place.
    """
    mod = load_build()
    _run_inode_mismatch_promotion(mod, tmp_path, monkeypatch, expect_refusal=True)


@_posix_only
def test_MUTATION_promotion_without_the_inode_check_ignores_a_swapped_staging(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Drop the pre-rename inode check and a mismatched staging inode is not refused.

    Proves the check is load-bearing: with it the tampered inode refuses; without it the same
    tampering promotes without complaint (the build completes rather than raising the inode
    refusal).
    """
    mod = load_build(
        mutate=(
            "            if staging_fd != -1:\n"
            "                try:\n"
            "                    check_fd = os.open(",
            "            if False:\n"
            "                try:\n"
            "                    check_fd = os.open(",
        )
    )
    _run_inode_mismatch_promotion(mod, tmp_path, monkeypatch, expect_refusal=False)


def _run_inode_mismatch_promotion(mod, tmp_path, monkeypatch, *, expect_refusal: bool) -> None:
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build: a real bundle
    assert (out / "skills" / "faq" / "SKILL.md").is_file()

    # ``os.open`` must NOT be patched (``_dir_fd_supported`` checks os.open membership in
    # os.supports_dir_fd, and a wrapper would fail that and route to the Windows path). Arm
    # deterministically instead: the pre-promote hard-link probe runs immediately before the
    # promote block, so wrap it to set a flag; then tamper the SECOND ``os.fstat`` after arming
    # -- the check does ``fstat(staging_fd)`` then ``fstat(check_fd)`` with nothing between, so
    # the second is the reopened leaf. Tampering its inode makes the leaf look swapped.
    real_fstat = os.fstat
    real_probe = mod._refuse_report_dir_without_hard_link_support
    state = {"armed": False, "count": 0, "tampered": False}

    class _Stat:
        def __init__(self, base, st_ino):
            self._base = base
            self.st_ino = st_ino

        def __getattr__(self, name):
            return getattr(self._base, name)

    def _armed_probe(report_path):
        real_probe(report_path)
        state["armed"] = True

    def _fstat(fd):
        st = real_fstat(fd)
        if state["armed"] and not state["tampered"]:
            state["count"] += 1
            if state["count"] == 2:
                state["tampered"] = True
                return _Stat(st, st.st_ino ^ 0xABCD)
        return st

    monkeypatch.setattr(mod, "_refuse_report_dir_without_hard_link_support", _armed_probe)
    monkeypatch.setattr(os, "fstat", _fstat)
    if expect_refusal:
        with pytest.raises(mod.ExportRefused) as caught:
            _build(mod, home, out, {"skills": {"faq"}})
        assert "inode changed" in str(caught.value)
        monkeypatch.undo()
        assert (out / "skills" / "faq" / "SKILL.md").read_text(encoding="utf-8") == "# FAQ\n"
    else:
        # Under the mutant the inode comparison is gone, so the tampering raises no inode
        # refusal -- the build either completes or fails for an unrelated reason, never the
        # inode message.
        raised = None
        try:
            _build(mod, home, out, {"skills": {"faq"}})
        except mod.ExportRefused as exc:  # pragma: no cover - defensive
            raised = str(exc)
        monkeypatch.undo()
        assert raised is None or "inode changed" not in raised


@_posix_only
def test_a_report_dir_without_hard_link_support_is_refused_before_promotion(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A report directory that cannot hard-link refuses BEFORE promotion, prior bundle intact.

    ``_publish_report`` installs by exclusive hard link, a filesystem capability. On a mount
    without it ``os.link`` raises EPERM/EOPNOTSUPP/ENOSYS, and because publish runs after
    ``promoted = True`` an unguarded failure would unwind a good promotion. The capability is
    probed before the irreversible rename; simulate an unsupported mount by making the probe
    link raise EOPNOTSUPP, and assert the build refuses with the previous bundle untouched.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build: a real bundle + report
    report = out.parent / (out.name + ".smc-bundle.json")
    first = report.read_bytes()

    real_link = os.link
    import errno as _errno

    def _no_hard_links(src, dst, *a, **k):
        # Fail the capability PROBE (its names carry the run id + linkprobe marker).
        if "linkprobe" in str(dst) or "linkprobe" in str(src):
            raise OSError(getattr(_errno, "EOPNOTSUPP", _errno.EPERM), "operation not supported")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", _no_hard_links)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "does not support hard links" in str(caught.value)
    monkeypatch.undo()
    # The promotion never ran: the first bundle and its report are exactly as they were.
    assert (out / "skills" / "faq" / "SKILL.md").read_text(encoding="utf-8") == "# FAQ\n"
    assert report.read_bytes() == first, "the prior report is untouched by a pre-promotion refusal"


@_posix_only
def test_MUTATION_no_capability_probe_crashes_raw_when_the_link_is_unsupported(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Remove the pre-promotion probe and an unsupported hard link crashes raw after promotion.

    Proves the probe is load-bearing. WITH the probe (the paired positive test), an
    unsupported-link filesystem is caught before promotion and refused cleanly with guidance,
    the prior bundle untouched. WITHOUT it, the capability failure surfaces from inside
    ``_publish_report`` -- after ``promoted = True`` -- as a raw ``OSError`` that is NOT the
    builder's clean ``ExportRefused``, leaving the operator a stack trace and a promoted bundle
    with no report instead of a recoverable refusal.
    """
    mod = load_build(
        mutate=(
            "        _report._refuse_report_dir_without_hard_link_support(report_path)\n",
            "        pass  # probe removed by mutation\n",
        )
    )
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    import errno as _errno

    real_link = os.link

    def _no_hard_links(src, dst, *a, **k):
        # Now that the probe is gone, only the REAL publish links (to the report leaf and its
        # aside) fire; fail them with an unsupported-capability errno.
        if ".smc-bundle.json" in str(dst):
            raise OSError(getattr(_errno, "EOPNOTSUPP", _errno.EPERM), "operation not supported")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", _no_hard_links)
    with pytest.raises(OSError) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    monkeypatch.undo()
    # The mutant leaks a RAW OSError, not the builder's clean ExportRefused with guidance.
    assert not isinstance(caught.value, mod.ExportRefused)
    assert "does not support hard links" not in str(caught.value)
