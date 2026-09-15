"""A revert that did not happen, and credential shapes the guarantee did not cover."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact


class TestAFailedRevertIsNotReportedAsSuccess:
    """The summary line is what the operator acts on, so it must not overstate."""

    def test_recovery_reports_which_targets_it_could_not_put_back(self, tmp_path: Path) -> None:
        backup = tmp_path / "rollback"
        backup.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        (backup / "workspace").mkdir()
        (backup / "workspace" / "note.md").write_text("saved\n", encoding="utf-8")

        failed = snap._restore_everything_from_rollback(
            backup, home, ["workspace"], {"workspace"}, allow_unpinned=bool(unpinnable_argv())
        )
        assert failed == [], "a clean revert reported failures"
        assert (home / "workspace" / "note.md").read_text(encoding="utf-8") == "saved\n"

    def test_a_missing_rollback_directory_is_a_failed_revert(self, tmp_path: Path) -> None:
        """Nothing to put back is not the same as everything put back."""
        home = tmp_path / "home"
        home.mkdir()
        failed = snap._restore_everything_from_rollback(
            home / "absent", home, ["workspace", "skills"], set()
        )
        assert sorted(failed) == ["skills", "workspace"]

    def test_a_target_that_cannot_be_put_back_is_returned(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        backup = tmp_path / "rollback"
        (backup / "workspace").mkdir(parents=True)
        (backup / "workspace" / "note.md").write_text("saved\n", encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()

        def refuse(*a, **k):
            raise OSError(28, "No space left on device")

        # Recovery restores a saved DIRECTORY through _copytree_safe now (the standalone
        # _copytree_rollback helper is gone); make that copy refuse.
        monkeypatch.setattr(snap, "_copytree_safe", refuse)
        failed = snap._restore_everything_from_rollback(backup, home, ["workspace"], {"workspace"})
        assert failed and "workspace" in failed[0]
        assert "No space left" in failed[0]

    def test_the_incomplete_signal_carries_what_the_operator_needs(self) -> None:
        cause = OSError(28, "No space left on device")
        e = snap.RollbackIncomplete(cause, ["workspace (denied)"], Path("/tmp/rb"))
        assert e.cause is cause
        assert e.failed == ["workspace (denied)"]
        assert e.backup == Path("/tmp/rb")
        # Still an OSError, so no existing handler on this path stops catching it.
        assert isinstance(e, OSError)

    def test_a_partial_revert_is_raised_rather_than_swallowed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Driven for real: the mutation fails, and putting things back fails too."""
        home = tmp_path / "home"
        (home / "workspace").mkdir(parents=True)
        snapdir = tmp_path / "bundle"
        snapdir.mkdir()

        def blow_up(*a, **k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(snap, "_do_replace_mutations", blow_up)
        monkeypatch.setattr(
            snap, "_restore_everything_from_rollback", lambda *a, **k: ["workspace (denied)"]
        )
        monkeypatch.setattr(snap, "_refuse_unsafe_destination_roots", lambda *a, **k: None)

        with pytest.raises(snap.RollbackIncomplete) as e:
            snap._do_replace(snapdir, home, ["workspace"], allow_unpinned=bool(unpinnable_argv()))
        assert e.value.failed == ["workspace (denied)"]
        assert isinstance(e.value.cause, OSError)

    def test_a_clean_revert_still_raises_the_original_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The other branch: the restore failed but the revert worked, so say that."""
        home = tmp_path / "home"
        (home / "workspace").mkdir(parents=True)
        snapdir = tmp_path / "bundle"
        snapdir.mkdir()

        def blow_up(*a, **k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(snap, "_do_replace_mutations", blow_up)
        monkeypatch.setattr(snap, "_restore_everything_from_rollback", lambda *a, **k: [])
        monkeypatch.setattr(snap, "_refuse_unsafe_destination_roots", lambda *a, **k: None)

        with pytest.raises(OSError) as e:
            snap._do_replace(snapdir, home, ["workspace"], allow_unpinned=bool(unpinnable_argv()))
        assert not isinstance(e.value, snap.RollbackIncomplete)

    def test_the_success_wording_is_not_printed_unconditionally(self) -> None:
        """The claim and the case it is true in must be in the same branch."""
        src = inspect.getsource(snap)
        claim = "Your previous state was put back"
        assert claim in src
        # The honest branch must exist and must be the one handling a partial revert.
        assert "did NOT fully succeed" in src
        assert src.index("except RollbackIncomplete") < src.index(claim), (
            "the partial-revert handler must be matched BEFORE the blanket OSError one, "
            "or the reassuring message wins"
        )


class TestTheOutboundScrubCoversWhatItClaims:
    # Assembled at runtime, never written as one literal: GitHub's own push protection
    # recognises this shape as a Discord bot token and blocks the push. That is the
    # clearest possible evidence the shape is a real credential format worth redacting,
    # and it is also why a fixture must not spell one out.
    _DOTTED = ".".join(
        ("MTIzNDU2Nzg5MDEyMzQ1Njc4", "Gh1j2K", "l3M4n5O6p7Q8r9S0t1U2v3W4x5Y6z7A8b9C")
    )

    def test_a_dotted_bearer_token_is_removed(self) -> None:
        cleaned, hits = redact._scrub(self._DOTTED)
        assert hits > 0
        assert cleaned == "[REDACTED: credential]"

    @pytest.mark.parametrize(
        "shape",
        [
            '{"api_key": "' + "k" * 40 + '"}',
            '{"password": "hunter2-correct-horse-battery"}',
            'bot_token = "' + "z" * 46 + '"',
            "client_secret: " + "q" * 32,
        ],
    )
    def test_a_shape_the_shared_redactors_miss_is_still_removed(self, shape: str) -> None:
        cleaned, hits = redact._scrub(shape)
        assert hits > 0, "no replacement was recorded"
        assert "REDACTED" in cleaned
        secret = max(shape.replace('"', " ").replace(":", " ").split(), key=len)
        assert secret not in cleaned

    @pytest.mark.parametrize(
        "content",
        [
            '{"version": 3, "components": {"memory": "unresolved"}}',
            "The token budget per turn is about 13k, mostly fixed overhead.",
            '{"password": ""}',
            '{"password": "changeme"}',
            '{"path": "workspace/memory/notes.md"}',
            "kiro_crew.snapshot_redact.redact_bundle_for_egress",
            '{"created_at": "2026-08-14T10:00:00Z"}',
            "CREATE TABLE semantic(id INTEGER PRIMARY KEY, key TEXT, value TEXT)",
        ],
    )
    def test_ordinary_content_is_left_exactly_as_it_was(self, content: str) -> None:
        """Over-reach here silently empties an operator's notes out of their backup."""
        cleaned, hits = redact._scrub(content)
        assert cleaned == content
        assert hits == 0

    def test_a_redacted_json_document_still_parses(self) -> None:
        """The tag carries no quote or backslash, and that is checked, not assumed."""
        doc = json.dumps({"telegram": {"bot_token": "8123456789:AAF" + "x" * 32}})
        cleaned, hits = redact._scrub(doc)
        assert hits > 0
        assert json.loads(cleaned) == {"telegram": {"bot_token": "[REDACTED: credential]"}}

    def test_an_already_redacted_value_is_not_redacted_twice(self) -> None:
        once, _ = redact._scrub('{"api_key": "' + "k" * 40 + '"}')
        twice, hits = redact._scrub(once)
        assert twice == once
        assert hits == 0, "a second pass must settle, or the fixpoint loop never terminates"

    def test_the_tag_cannot_be_matched_as_a_value(self) -> None:
        """This is WHY re-running settles, so it is pinned rather than left implicit.

        The row scan repeats until a pass changes nothing. A rule that rewrote its own
        replacement would never settle, and a non-settling database is refused -- so
        editing the tag into something the value class accepts would refuse every backup.
        """
        assert " " in redact._TAG, "the tag must contain whitespace, which the value class excludes"
        assert not redact._STRUCTURED_CREDENTIAL.search(f'{{"api_key": "{redact._TAG}"}}')

    def test_the_egress_pass_runs_after_the_shared_pair(self) -> None:
        """The shared warnings stay the primary signal; this only closes what they miss."""
        src = inspect.getsource(redact._scrub)
        assert src.index("redact_credentials(") < src.index("_scrub_unrecognised(")
        assert src.index("redact_exfiltration_urls(") < src.index("_scrub_unrecognised(")

    def test_the_shared_redactors_are_not_widened_by_this_pass(self) -> None:
        """Those run over live output, where a false positive corrupts what is read."""
        src = inspect.getsource(redact)
        assert "_SENSITIVE_FIELD" in src, "the extra shapes belong to the egress module"
