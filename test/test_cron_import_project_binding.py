"""Import-sanitizer rule 4: a restored cron job's project binding.

Lives in its own module rather than beside the other portability tests: that file
is not black-clean, so touching it would make this change reformat it whole. The
two fixtures it needs are imported from it, as 136 other test modules do with
their siblings.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from test_portability import _cron_job, _make_cron_import_zip


def test_an_imported_message_only_job_with_a_sensitive_project_path_is_cleared_and_paused(tmp_path):
    """GPT 5.6 Review F1: a message-only job binds an agent to a directory and
    runs no code of its own, so rule 3 (execute-only) never touched its
    ``project_path`` before this fix -- `_job_from_record` trusts it as a bare
    string, and the fire-time guard (``_project_path_still_canonical``) only
    re-checks existence/canonicality, never sensitivity. A crafted archive
    containing an ENABLED message-only job whose ``project_path`` names a
    credential home would survive import untouched and schedule an agent
    against it on the very next fire. Fixed: rule 4 re-validates
    ``project_path`` exactly as ``cron_add`` would, clearing a sensitive
    binding rather than dropping the whole job, and pausing anything that
    still names a directory afterward.
    """
    import kiro_crew.portability as port

    z = _make_cron_import_zip(
        tmp_path / "sensitive-path.zip",
        [
            _cron_job(
                "m1",
                "sensitive-binding",
                message="summarize",
                project_path="/var/lib/kirocrew-secrets",
            )
        ],
    )
    target = tmp_path / "target_sensitive_path"
    target.mkdir()

    class _SensitiveVerdict:
        sensitive = True

    with patch.object(port, "resolve_project_path", return_value=_SensitiveVerdict()):
        with patch.object(port, "config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = port.apply_import_zip(z, mode="merge")

    jobs = {j["name"]: j for j in json.loads((target / "crons.json").read_text())["jobs"]}
    job = jobs["sensitive-binding"]
    # The binding is cleared, not the whole job dropped.
    assert "rejected_crons" not in summary
    assert job["project_path"] == ""
    # A cleared binding needs no pause: it is now an ordinary global job.
    assert job.get("user_paused", False) is False
    assert job.get("enabled", True) is True


def test_an_imported_message_only_job_with_a_benign_project_path_is_kept_but_paused(tmp_path):
    """A NON-sensitive, resolvable ``project_path`` on a message-only job is
    kept (not cleared) but still arrives paused, same outcome as rule 3's
    command/script jobs -- re-arming an imported project binding is always an
    explicit human action, even for a directory that is itself perfectly
    safe, since the archive's own origin machine chose it unreviewed.
    """
    import kiro_crew.portability as port

    real_dir = str(tmp_path / "some-project")
    os.makedirs(real_dir, exist_ok=True)
    z = _make_cron_import_zip(
        tmp_path / "benign-path.zip",
        [_cron_job("m2", "benign-binding", message="summarize", project_path=real_dir)],
    )
    target = tmp_path / "target_benign_path"
    target.mkdir()

    class _BenignVerdict:
        resolved = real_dir  # already canonical, so the kept binding is unchanged
        sensitive = False
        is_dir = True

    with patch.object(port, "resolve_project_path", return_value=_BenignVerdict()):
        with patch.object(port, "config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = port.apply_import_zip(z, mode="merge")

    jobs = {j["name"]: j for j in json.loads((target / "crons.json").read_text())["jobs"]}
    job = jobs["benign-binding"]
    assert "rejected_crons" not in summary
    assert job["project_path"] == real_dir
    assert sorted(summary.get("paused_crons", [])) == ["benign-binding"]
    assert job["user_paused"] is True
    assert job["enabled"] is False


def test_an_imported_job_with_a_merely_absent_project_path_is_kept_and_paused(tmp_path):
    """Accepted-mechanism decision (Security Scope Review): a resolvable,
    non-sensitive ``project_path`` that simply does not exist as a directory
    on the TARGET machine yet (the ordinary "restore settings before
    re-cloning the repo" migration -- the archive's origin machine had it,
    this one hasn't checked it out yet) is NOT the same hazard as a sensitive
    path, and clearing it would force every restored project-bound job to be
    re-bound by hand even once the checkout shows up. It is left INTACT and
    falls straight through to rule 3's own outcome -- paused, awaiting an
    explicit human re-enable -- exactly like any other bound job. Only a
    SENSITIVE or genuinely UNRESOLVABLE path clears the binding (see the
    sibling sensitive/unresolvable tests).
    """
    import kiro_crew.portability as port

    z = _make_cron_import_zip(
        tmp_path / "absent-path.zip",
        [_cron_job("m4", "absent-binding", message="summarize", project_path="/not/a/real/dir")],
    )
    target = tmp_path / "target_absent_path"
    target.mkdir()

    class _AbsentVerdict:
        resolved = "/not/a/real/dir"  # already canonical, so the binding is kept verbatim
        sensitive = False
        is_dir = False

    with patch.object(port, "resolve_project_path", return_value=_AbsentVerdict()):
        with patch.object(port, "config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = port.apply_import_zip(z, mode="merge")

    jobs = {j["name"]: j for j in json.loads((target / "crons.json").read_text())["jobs"]}
    job = jobs["absent-binding"]
    assert "rejected_crons" not in summary
    # Binding stays intact -- NOT cleared -- and is paused, not dropped.
    assert job["project_path"] == "/not/a/real/dir"
    assert sorted(summary.get("paused_crons", [])) == ["absent-binding"]
    assert job["user_paused"] is True
    assert job["enabled"] is False


def test_an_imported_job_with_a_relative_project_path_is_cleared(tmp_path):
    """GPT 5.6 Review: a RELATIVE ``project_path`` is not a binding this machine
    can honor -- it would resolve against whatever cwd the gateway happens to
    have -- so it is held to the same absolute-path gate the create surfaces
    apply. This became reachable only once a merely-absent path started KEEPING
    its binding instead of being cleared: before that, every non-existent
    spelling was cleared anyway and the gap could not surface.
    """
    import kiro_crew.portability as port

    z = _make_cron_import_zip(
        tmp_path / "relative-path.zip",
        [_cron_job("m9", "relative-binding", message="summarize", project_path="relative/path")],
    )
    target = tmp_path / "target_relative_path"
    target.mkdir()

    class _RelativeVerdict:
        resolved = "/some/cwd/relative/path"
        sensitive = False
        is_dir = False

    with patch.object(port, "resolve_project_path", return_value=_RelativeVerdict()):
        with patch.object(port, "config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = port.apply_import_zip(z, mode="merge")

    jobs = {j["name"]: j for j in json.loads((target / "crons.json").read_text())["jobs"]}
    job = jobs["relative-binding"]
    assert job["project_path"] == "", (
        "a relative project_path survived the import -- it would resolve "
        "against the gateway's cwd, binding the job to whatever directory "
        "the process happened to start in"
    )
    assert "rejected_crons" not in summary, "the job itself was dropped, not just its binding"


def test_a_kept_imported_binding_is_stored_in_its_canonical_form(tmp_path):
    """GPT 5.6 Review: the fire-time guard (`_project_path_still_canonical`)
    requires the stored string to equal its own ``realpath`` EXACTLY -- that is
    how it detects a symlink retargeted under a saved binding -- and the create
    surfaces satisfy it by storing the resolved value. So a kept binding must be
    rewritten to canonical form; keeping the archive's spelling (a `~`, a
    symlinked parent, a trailing separator) would hand the operator a job that
    can be re-enabled but can never fire, which is a worse outcome than the
    clearing this rule was just relaxed away from.
    """
    import kiro_crew.portability as port

    z = _make_cron_import_zip(
        tmp_path / "noncanonical-path.zip",
        [_cron_job("m10", "noncanonical", message="summarize", project_path="/link/to/repo")],
    )
    target = tmp_path / "target_noncanonical"
    target.mkdir()

    class _SymlinkedVerdict:
        resolved = "/real/repo"  # realpath resolved the symlinked parent away
        sensitive = False
        is_dir = True

    with patch.object(port, "resolve_project_path", return_value=_SymlinkedVerdict()):
        with patch.object(port, "config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                port.apply_import_zip(z, mode="merge")

    jobs = {j["name"]: j for j in json.loads((target / "crons.json").read_text())["jobs"]}
    job = jobs["noncanonical"]
    assert job["project_path"] == "/real/repo", (
        "the archive's non-canonical spelling was stored verbatim -- the "
        "fire-time canonical guard compares the stored string against its own "
        "realpath, so this job would be re-enablable but permanently unfirable"
    )
    # Still paused: canonicalizing a binding is not the same as approving it.
    assert job["user_paused"] is True
    assert job["enabled"] is False


def test_an_imported_job_with_an_unresolvable_project_path_is_cleared_not_dropped(tmp_path):
    """An embedded-null-byte (or otherwise unresolvable) ``project_path`` must
    fail CLOSED like a sensitive one, not crash the import or leave the raw
    value in place -- `resolve_project_path` calls `os.path.realpath` with no
    guard of its own (GPT 5.6 Review F3's exact crash surface, reached here
    instead of the live HTTP endpoint), so the sanitizer's own call is
    wrapped to treat any resolution failure as "not a usable binding" and
    clear it, same as a confirmed-sensitive one.
    """
    import kiro_crew.portability as port

    z = _make_cron_import_zip(
        tmp_path / "unresolvable-path.zip",
        [_cron_job("m3", "unresolvable-binding", message="summarize", project_path="/tmp/\x00bad")],
    )
    target = tmp_path / "target_unresolvable_path"
    target.mkdir()

    def _boom(_raw):
        raise ValueError("embedded null byte")

    with patch.object(port, "resolve_project_path", side_effect=_boom):
        with patch.object(port, "config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = port.apply_import_zip(z, mode="merge")

    jobs = {j["name"]: j for j in json.loads((target / "crons.json").read_text())["jobs"]}
    job = jobs["unresolvable-binding"]
    assert "rejected_crons" not in summary
    assert job["project_path"] == ""
    assert job.get("user_paused", False) is False
