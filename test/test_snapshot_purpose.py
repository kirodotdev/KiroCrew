"""Tests for the bundle purpose seam and the self-contained memory component.

The seam's value is entirely in its refusals, so each one is asserted directly
rather than inferred from a successful run: an unknown component name, and a
component that carries credential material riding a bundle meant to be shared.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew.snapshot import (
    COMPONENTS,
    ComponentRefused,
    Purpose,
    SecretPolicy,
    resolve_components,
    restore_main,
    snapshot_main,
)


def _snapshot(out: Path, extra: list[str]) -> Path:
    assert snapshot_main([str(out)] + extra + unpinnable_argv()) == 0
    tars = sorted(out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    assert tars
    return tars[-1]


def _extract(tarball: Path, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(tarball)) as tar:
        tar.extractall(into, filter=lambda t, _d="": t)
    # A selective bundle's root is `kirocrew-partial-`, which is how released
    # restores are made to refuse it; both are valid to this version.
    snaps = [
        d for d in into.iterdir() if d.name.startswith(("kirocrew-snapshot-", "kirocrew-partial-"))
    ]
    assert snaps
    return snaps[0]


@pytest.fixture
def src(tmp_path, monkeypatch):
    d = tmp_path / "src"
    _setup_fake_kirocrew(d)
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    return d


class TestPolicyDeclarations:
    def test_every_component_declares_a_policy(self):
        """The seam is only load-bearing if no component can skip the declaration.

        A new component added without one must not fall through to a permissive
        default — the dataclass has no default for ``policy``, so construction
        fails, and this asserts the invariant the registry relies on.
        """
        for name, spec in COMPONENTS.items():
            assert isinstance(spec.policy, SecretPolicy), name

    def test_component_spec_cannot_be_built_without_a_policy(self):
        from kiro_crew.snapshot import ComponentSpec

        with pytest.raises(TypeError):
            ComponentSpec(help="no policy declared")  # type: ignore[call-arg]

    def test_credential_bearing_components_are_marked_unresolved(self):
        """Components that can carry credential material; the policy is undecided.

        Fail closed until it is decided: they ride a backup unchanged and are
        refused outright in a share bundle. `crons` is here because `CronJob.env`
        is a persisted dict of per-job environment variables, so a job passing an
        API token to a command carries that token in crons.json.
        """
        assert COMPONENTS["config"].policy is SecretPolicy.UNRESOLVED
        assert COMPONENTS["security"].policy is SecretPolicy.UNRESOLVED
        assert COMPONENTS["crons"].policy is SecretPolicy.UNRESOLVED

    def test_no_component_claims_to_be_share_safe(self):
        """Whether a component is safe to share is a CONTENT question.

        A workspace file, a skill, a cron's env map or a pasted lesson can each hold
        a token, and staging cannot tell. Guessing per component was tried and two
        guesses were wrong, so nothing is certified until the redaction work exists.
        This is the assertion that stops the guessing from creeping back.
        """
        guessed_safe = [
            name for name, spec in COMPONENTS.items() if spec.policy is SecretPolicy.SHARE_SAFE
        ]
        assert (
            guessed_safe == []
        ), f"{guessed_safe} claim SHARE_SAFE without content redaction behind it"


class TestResolveComponents:
    def test_unknown_name_is_refused_not_silently_dropped(self):
        with pytest.raises(ComponentRefused) as e:
            resolve_components(["memory", "bogus"], Purpose.BACKUP)
        assert "bogus" in str(e.value)

    def test_share_refuses_an_unresolved_component(self):
        with pytest.raises(ComponentRefused) as e:
            resolve_components(["memory", "config"], Purpose.SHARE)
        msg = str(e.value)
        assert "config" in msg
        # The refusal has to say how to proceed, or it just blocks the operator.
        assert "--purpose backup" in msg or "--components" in msg

    def test_share_refuses_every_component_today(self):
        """The gate is live and nothing is certified, so share refuses each one.

        Asserted per component rather than once, so certifying one later fails here
        and forces this expectation to be revisited deliberately.
        """
        for name in COMPONENTS:
            with pytest.raises(ComponentRefused) as e:
                resolve_components([name], Purpose.SHARE)
            assert "no share-safe policy" in str(e.value)

    def test_the_share_refusal_explains_that_it_is_a_content_question(self):
        with pytest.raises(ComponentRefused) as e:
            resolve_components(["memory"], Purpose.SHARE)
        msg = str(e.value)
        assert "CONTENT" in msg
        assert "--purpose backup" in msg, "the refusal must say how to proceed"

    def test_backup_allows_everything(self):
        assert resolve_components(None, Purpose.BACKUP) == list(COMPONENTS)

    def test_a_full_share_bundle_is_refused_while_any_policy_is_unresolved(self):
        """`--purpose share` with no `--components` must not quietly ship config."""
        with pytest.raises(ComponentRefused):
            resolve_components(None, Purpose.SHARE)


class TestSelectiveStaging:
    def test_memory_only_bundle_omits_other_components(self, src, tmp_path):
        snap = _extract(_snapshot(tmp_path / "out", ["--components", "memory"]), tmp_path / "x1")
        assert (snap / "memory.db").is_file()
        # Not selected, so not staged. This is what makes a memory bundle 22 MB
        # instead of the whole data home.
        assert not (snap / "config.json").exists()
        assert not (snap / "crons.json").exists()
        assert not (snap / "skills").exists()

    def test_memory_component_is_self_contained(self, src, tmp_path):
        """Both halves of memory ride: the databases and the markdown files.

        Before the memory component named these trees they reached a bundle only
        through the whole-`workspace` component, so restoring memory meant
        restoring every unrelated working file too.
        """
        snap = _extract(_snapshot(tmp_path / "out", ["--components", "memory"]), tmp_path / "x2")
        assert (snap / "workspace/memory/preferences.md").is_file()
        assert (snap / "workspace/memory/projects.md").is_file()
        assert (snap / "workspace/memory/history/2026-01-01.md").is_file()
        assert (snap / "workspace/knowledge/kb.sqlite3").is_file()
        # Memory does NOT drag the rest of the workspace in.
        assert not (snap / "workspace/doc.md").exists()
        assert not (snap / "plan_memory").exists()

    def test_manifest_records_purpose_and_declared_policies(self, src, tmp_path):
        snap = _extract(
            _snapshot(tmp_path / "out", ["--components", "memory,crons"]), tmp_path / "x3"
        )
        m = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
        assert m["purpose"] == "backup"
        # crons rides a BACKUP unchanged, and its declaration says why a share
        # bundle would refuse it.
        assert m["components"] == {"memory": "unresolved", "crons": "unresolved"}

    def test_a_share_bundle_refuses_crons(self, src, tmp_path, capsys):
        out = tmp_path / "out"
        assert snapshot_main([str(out), "--components", "crons", "--purpose", "share"]) == 1
        assert "crons" in capsys.readouterr().out
        assert not list(out.glob("kirocrew-snapshot-*.tar.gz"))

    def test_a_file_only_component_with_nothing_to_stage_still_produces_a_bundle(
        self, src, tmp_path
    ):
        """An empty selection is a valid outcome; a crash is not.

        `crons` names only files, so a home without crons.json stages nothing --
        which used to leave the staging dir uncreated and fail the manifest write.
        """
        (src / "crons.json").unlink(missing_ok=True)
        snap = _extract(_snapshot(tmp_path / "out", ["--components", "crons"]), tmp_path / "x5")
        m = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
        assert m["components"] == {"crons": "unresolved"}
        assert not (snap / "crons.json").exists()

    def test_share_produces_no_bundle_while_nothing_is_certified(self, src, tmp_path, capsys):
        """`--purpose share` is wired end to end and refuses, writing nothing."""
        out = tmp_path / "out"
        assert snapshot_main([str(out), "--components", "memory", "--purpose", "share"]) == 1
        assert "no share-safe policy" in capsys.readouterr().out
        assert not list(out.glob("kirocrew-snapshot-*.tar.gz"))

    def test_share_of_a_credential_bearing_component_fails_and_writes_nothing(
        self, src, tmp_path, capsys
    ):
        out = tmp_path / "out"
        assert snapshot_main([str(out), "--components", "config", "--purpose", "share"]) == 1
        assert "config" in capsys.readouterr().out
        # A refusal must cost nothing — no partial bundle left behind.
        assert not list(out.glob("kirocrew-snapshot-*.tar.gz"))

    def test_unknown_purpose_is_refused(self, src, tmp_path, capsys):
        out = tmp_path / "out"
        assert snapshot_main([str(out), "--purpose", "sideways"]) == 1
        assert "sideways" in capsys.readouterr().out
        assert not list(out.glob("kirocrew-snapshot-*.tar.gz"))


class TestMemoryRoundTrip:
    def test_memory_only_restore_reproduces_both_halves(self, src, tmp_path, monkeypatch):
        """The M1 exit criterion: a memory bundle restored onto an empty home brings
        back every semantic key, episodic row and the markdown files — without the
        workspace component."""
        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])

        src_conn = sqlite3.connect(str(src / "memory.db"))
        want_semantic = src_conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0]
        want_episodic = src_conn.execute("SELECT count(*) FROM episodic_memories").fetchone()[0]
        src_conn.close()
        assert want_semantic > 0 and want_episodic > 0

        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        assert (
            restore_main(
                [str(tarball), "--components", "memory", "--mode", "replace", "--force"]
                + unpinnable_argv()
            )
            == 0
        )

        assert (dest / "memory.db").is_file()
        dest_conn = sqlite3.connect(str(dest / "memory.db"))
        assert (
            dest_conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == want_semantic
        )
        assert (
            dest_conn.execute("SELECT count(*) FROM episodic_memories").fetchone()[0]
            == want_episodic
        )
        dest_conn.close()

        assert (dest / "workspace/memory/preferences.md").read_text() == (
            src / "workspace/memory/preferences.md"
        ).read_text()
        assert (dest / "workspace/memory/history/2026-01-01.md").is_file()
        assert (dest / "workspace/knowledge/kb.sqlite3").is_file()

    def test_memory_restore_leaves_the_rest_of_the_workspace_alone(
        self, src, tmp_path, monkeypatch
    ):
        """Restoring memory in replace mode must not rmtree the whole workspace."""
        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])

        dest = tmp_path / "dest2"
        (dest / "workspace").mkdir(parents=True)
        keep = dest / "workspace" / "unrelated.md"
        keep.write_text("local work, not in the bundle\n")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        assert (
            restore_main(
                [str(tarball), "--components", "memory", "--mode", "replace", "--force"]
                + unpinnable_argv()
            )
            == 0
        )
        assert keep.read_text() == "local work, not in the bundle\n"
        assert (dest / "workspace/memory/preferences.md").is_file()
