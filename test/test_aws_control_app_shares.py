"""AWS Control's share ledger: what a recorded share keeps, lists and forgets."""

from __future__ import annotations

import pytest
from aws_control_app_helpers import (  # noqa: F401 -- fixtures are used by name
    ACCOUNT,
    _fresh_snapshot,
)

# ---------------------------------------------------------------------------
# Share ledger
# ---------------------------------------------------------------------------


class TestShares:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in TestShares calls monkeypatch.undo()
    def _isolated_store(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        monkeypatch.setattr(shares, "_store_path", lambda: tmp_path / "shares.json")
        yield

    def test_ledger_records_metadata_never_the_url(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        record = shares.record_share(
            account=ACCOUNT,
            section="drive",
            key="a.txt",
            expires_secs=3600,
            note="for alex",
        )
        raw = (tmp_path / "shares.json").read_text(encoding="utf-8")
        assert "https://" not in raw
        assert record["id"] in raw
        assert shares.list_shares(ACCOUNT)[0]["key"] == "a.txt"

    def test_a_read_that_failed_never_truncates_the_share_ledger(self, tmp_path, monkeypatch):
        # `_load` collapses every failure to []. As the base of `record_share`'s
        # whole-file rewrite that empty list means "forget every share already
        # recorded" -- and this ledger is the only local record of live presigned
        # URLs, which are unrevokable bearer grants. Under-reporting them is the
        # security-visible half of the loss.
        import contextlib
        from pathlib import Path

        from kiro_crew.apps.builtins.aws_control.backend import shares

        kept = shares.record_share(
            account=ACCOUNT, section="drive", key="live.txt", expires_secs=3600
        )
        store = tmp_path / "shares.json"
        real_read_text = Path.read_text
        broken = {"on": True}

        def guarded(path_self, *args, **kwargs):
            if broken["on"] and Path(path_self) == store:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)
        with contextlib.suppress(OSError):
            shares.record_share(
                account=ACCOUNT, section="drive", key="second.txt", expires_secs=3600
            )
        with contextlib.suppress(OSError):
            shares.forget_share(kept["id"])

        # The durable harm, asserted directly: the live bearer grant recorded
        # before the failure must still be in the ledger.
        broken["on"] = False
        assert [e["key"] for e in shares.list_shares(ACCOUNT)] == ["live.txt"]

    def test_an_unreadable_share_store_refuses_the_mutation(self, tmp_path, monkeypatch):
        # A mint that cannot be recorded must not answer as though it were.
        from pathlib import Path

        from kiro_crew.apps.builtins.aws_control.backend import shares

        shares.record_share(account=ACCOUNT, section="drive", key="live.txt", expires_secs=3600)
        store = tmp_path / "shares.json"
        real_read_text = Path.read_text

        def guarded(path_self, *args, **kwargs):
            if Path(path_self) == store:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)
        with pytest.raises(OSError):
            shares.record_share(account=ACCOUNT, section="drive", key="x.txt", expires_secs=3600)

    def test_a_missing_share_store_is_still_a_first_write(self, tmp_path):
        # Absent is the one failure where [] is the truth.
        from kiro_crew.apps.builtins.aws_control.backend import shares

        assert not (tmp_path / "shares.json").exists()
        shares.record_share(account=ACCOUNT, section="drive", key="first.txt", expires_secs=3600)
        assert [e["key"] for e in shares.list_shares(ACCOUNT)] == ["first.txt"]

    def test_a_corrupt_share_store_refuses_the_mutation_and_is_left_intact(self, tmp_path):
        # A corrupt ledger is refused, never rewritten. The old tolerance
        # read it as empty and let the whole-file rewrite destroy records a
        # truncated JSON still held verbatim -- and this ledger is the only local
        # record of live presigned URLs, which are unrevokable bearer grants.
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import shares

        corrupt = '[{"id": "recoverable-share", "key": "live.txt"}'  # truncated
        (tmp_path / "shares.json").write_text(corrupt, encoding="utf-8")
        with pytest.raises(_json.JSONDecodeError):
            shares.record_share(
                account=ACCOUNT, section="drive", key="after.txt", expires_secs=3600
            )
        with pytest.raises(_json.JSONDecodeError):
            shares.forget_share("recoverable-share")
        assert (tmp_path / "shares.json").read_text(
            encoding="utf-8"
        ) == corrupt, "a mutation rewrote a corrupt share ledger instead of refusing"
        # The display read stays lenient: the Access section renders (empty)
        # rather than failing on a file only a person can repair.
        assert shares.list_shares(ACCOUNT) == []

    def test_a_share_store_that_is_not_utf8_takes_the_corruption_path(self, tmp_path):
        # UnicodeDecodeError is a ValueError but NOT a JSONDecodeError; unwrapped
        # it would slip past every corruption clause at the callers.
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import shares

        (tmp_path / "shares.json").write_bytes(b"\xff\xfe not utf8")
        with pytest.raises(_json.JSONDecodeError):
            shares.record_share(account=ACCOUNT, section="drive", key="x.txt", expires_secs=3600)
        assert (tmp_path / "shares.json").read_bytes() == b"\xff\xfe not utf8"
        # And the DISPLAY read tolerates the same bytes (a
        # UnicodeDecodeError escaped it): the Access section renders
        # empty rather than failing on a file only a person can repair.
        assert shares.list_shares(ACCOUNT) == []

    def test_a_share_store_that_parses_to_a_non_array_refuses_the_mutation(self, tmp_path):
        # Valid JSON with the wrong root parses without raising, so coercing it
        # to [] would let the rewrite destroy a document nobody could read.
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import shares

        (tmp_path / "shares.json").write_text('{"not": "a list"}', encoding="utf-8")
        with pytest.raises(_json.JSONDecodeError):
            shares.record_share(account=ACCOUNT, section="drive", key="x.txt", expires_secs=3600)
        assert (tmp_path / "shares.json").read_text(encoding="utf-8") == '{"not": "a list"}'

    def test_damaged_rows_refuse_the_mutation_instead_of_being_pruned_away(self, tmp_path):
        # The loss arrives one call later than the parse: both mutations pipe
        # the ledger through _prune, whose damage path silently DROPS any row
        # it cannot read an expiresAt from, and the whole-file rewrite takes
        # those rows with it. Measured in review (Opus): a valid-JSON ledger
        # holding one non-object row, one row with no expiry, and one with a
        # mangled expiry lost all three to a single record_share. Every damaged
        # shape must refuse; the deliberate expiry drop stays retention.
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import shares

        for damaged in (
            "a-damaged-row-that-is-not-an-object",
            {"id": "no-expiry", "key": "secret-report.pdf", "account": "111"},
            {"id": "bad-expiry", "key": "b.pdf", "expiresAt": "NOT-A-DATE"},
        ):
            doc = _json.dumps(
                [damaged, {"id": "healthy", "key": "c.pdf", "expiresAt": "2999-01-01T00:00:00"}]
            )
            (tmp_path / "shares.json").write_text(doc, encoding="utf-8")
            with pytest.raises(_json.JSONDecodeError):
                shares.record_share(
                    account=ACCOUNT, section="drive", key="new.txt", expires_secs=3600
                )
            with pytest.raises(_json.JSONDecodeError):
                shares.forget_share("healthy")
            assert (tmp_path / "shares.json").read_text(encoding="utf-8") == doc, damaged

    def test_the_damaged_row_refusal_names_no_entry_content(self, tmp_path):
        # The refusal names the row's index and nothing else: a share note or
        # key from a hand-edited ledger must not ride on an exception that
        # crosses into responses and logs.
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import shares

        (tmp_path / "shares.json").write_text(
            _json.dumps([{"id": "x", "key": "leakable-object-key.pdf"}]), encoding="utf-8"
        )
        with pytest.raises(_json.JSONDecodeError) as excinfo:
            shares.record_share(account=ACCOUNT, section="drive", key="n.txt", expires_secs=3600)
        assert "leakable-object-key.pdf" not in str(excinfo.value)
        assert "leakable-object-key.pdf" not in excinfo.value.doc

    def test_an_expired_row_is_still_retention_not_damage(self, tmp_path):
        # The keep/expire decision must survive the strict per-row check: a
        # parseable stamp in the past is the deliberate drop, not a refusal.
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import shares

        (tmp_path / "shares.json").write_text(
            _json.dumps([{"id": "dead", "key": "old.txt", "expiresAt": "2000-01-01T00:00:00"}]),
            encoding="utf-8",
        )
        shares.record_share(account=ACCOUNT, section="drive", key="new.txt", expires_secs=3600)
        assert [e["key"] for e in shares.list_shares(ACCOUNT)] == ["new.txt"]

    def test_expired_shares_are_pruned_and_forget_removes(self):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        dead = shares.record_share(account=ACCOUNT, section="drive", key="old.txt", expires_secs=60)
        # Backdate the expiry.
        entries = shares._load()
        entries[0]["expiresAt"] = "2000-01-01T00:00:00+00:00"
        shares._save(entries)
        assert shares.list_shares() == []
        assert shares.forget_share(dead["id"]) is None  # already pruned

        live = shares.record_share(
            account=ACCOUNT, section="drive", key="new.txt", expires_secs=3600
        )
        assert shares.forget_share(live["id"]) is not None
        assert shares.list_shares() == []

    def test_a_row_whose_object_is_gone_is_marked_not_dropped(self, tmp_path):
        # The defect this fixes: a delete strands the row, and until now the
        # only exit was the user pressing forget per row. The row STAYS -- the
        # ledger records that an unexpired URL was minted, which a delete does
        # not undo -- and gains the flag the Access section renders.
        from kiro_crew.apps.builtins.aws_control.backend import shares

        shares.record_share(account=ACCOUNT, section="drive", key="gone.txt", expires_secs=3600)
        shares.record_share(
            account=ACCOUNT, section="library", key="slug/v1.html", expires_secs=3600
        )
        rows = shares.list_shares(ACCOUNT)
        marked = shares.mark_missing_objects(rows, {"artifacts/slug/v1.html"})

        assert len(marked) == len(rows)
        by_key = {row["key"]: row for row in marked}
        assert by_key["gone.txt"]["objectMissing"] is True
        # A row whose object IS there carries no flag at all, so "absent" and
        # "present but unchecked" never collapse into one rendering.
        assert "objectMissing" not in by_key["slug/v1.html"]
        # And the ledger on disk is untouched: the mark is a render-time fact
        # about the bucket, which would go stale the moment the key is recreated.
        assert all("objectMissing" not in row for row in shares._load())

    def test_the_key_a_row_is_matched_by_is_section_prefixed(self):
        # The row stores a SECTION plus a key; the object is at the section's
        # prefix. Matching on the bare key would find nothing in a listing of
        # real keys and mark every row missing.
        from kiro_crew.apps.builtins.aws_control.backend import shares

        row = {"id": "sh-1", "section": "drive", "key": "a.txt"}
        # Compared as WHOLE LISTS rather than indexed and subscripted. A
        # regression that drops the row, or leaves it unmarked, then fails on an
        # assertion naming the expected rows -- an `[0]["objectMissing"]` reads
        # the same regression out as IndexError or KeyError, which states
        # nothing about what the function answered.
        assert shares.mark_missing_objects([row], {"a.txt"}) == [{**row, "objectMissing": True}]
        assert shares.mark_missing_objects([row], {"drive/a.txt"}) == [row]

    def test_a_row_naming_no_real_section_is_marked(self):
        # A section with no prefix cannot address an object, so no listing can
        # ever back the row. Reporting that beats rendering it as reachable.
        from kiro_crew.apps.builtins.aws_control.backend import shares

        row = {"id": "sh-1", "section": "nowhere", "key": "a.txt"}
        assert shares.mark_missing_objects([row], {"nowhere/a.txt"}) == [
            {**row, "objectMissing": True}
        ]
