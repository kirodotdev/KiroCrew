"""Line-coverage for the Library sync module (``backend/library.py``).

``test_aws_control_app.py::TestLibraryScan`` already pins the two push
REFUSALS (credential-bearing content, beacon URL). This file covers the
lines those cases leave cold: the ledger read/write shape guards, the
redacting ``list_pushable`` display path, the unpushable-kind refusal, and
the full happy-path push through the locked ledger write.

Comments explain WHY each case matters, matching the sibling file's style.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import library

ACCOUNT = "123456789012"


def _ledger_at(monkeypatch, tmp_path):
    """Point the module's ledger at a throwaway path and return it."""
    path = tmp_path / "library.json"
    monkeypatch.setattr(library, "_ledger_path", lambda: path)
    return path


# --------------------------------------------------------------------------
# read_ledger — the ledger is display state, so every decode failure MUST read
# as empty rather than 500 the Library list/push routes.
# --------------------------------------------------------------------------
class TestReadLedger:
    def test_missing_file_reads_as_empty(self, tmp_path, monkeypatch):
        # No push has happened yet: the file does not exist, and the list
        # route must still render (empty state), not raise FileNotFoundError.
        _ledger_at(monkeypatch, tmp_path)
        assert library.read_ledger() == {}

    def test_corrupt_json_reads_as_empty(self, tmp_path, monkeypatch):
        # A partially-written / hand-edited ledger must degrade to empty, not
        # propagate a JSONDecodeError into the route.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text("{not valid json", encoding="utf-8")
        assert library.read_ledger() == {}

    def test_non_dict_top_level_reads_as_empty(self, tmp_path, monkeypatch):
        # Truth is the bucket listing; a JSON list where a dict is expected is
        # coerced to {} so the ledger is never trusted into a crash.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text('["not", "a", "dict"]', encoding="utf-8")
        assert library.read_ledger() == {}

    def test_valid_dict_round_trips(self, tmp_path, monkeypatch):
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: {"s": {"version": 2}}}), encoding="utf-8")
        assert library.read_ledger() == {ACCOUNT: {"s": {"version": 2}}}


# --------------------------------------------------------------------------
# _write_ledger — creates the app data dir on first write and produces a
# ledger read_ledger can round-trip back.
# --------------------------------------------------------------------------
class TestWriteLedger:
    def test_write_creates_parent_and_round_trips(self, tmp_path, monkeypatch):
        # First push on a fresh install: the app data dir may not exist yet, so
        # the write must mkdir before atomic_write, then be readable back.
        path = tmp_path / "nested" / "library.json"
        monkeypatch.setattr(library, "_ledger_path", lambda: path)
        library._write_ledger({ACCOUNT: {"slug-a": {"version": 1}}})
        assert path.exists()
        assert library.read_ledger() == {ACCOUNT: {"slug-a": {"version": 1}}}


# --------------------------------------------------------------------------
# list_pushable — the display listing. Names are redacted on the way out, and a
# corrupted per-account / per-slug entry reads as empty push-state.
# --------------------------------------------------------------------------
class TestListPushable:
    def test_redacts_name_and_joins_push_state_sorted(self, tmp_path, monkeypatch):
        # Two artifacts, one already pushed. The listing must: redact the name
        # (an LLM-authored name can quote a secret), carry pushedVersion from
        # the ledger, and sort newest-updated first.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(
            json.dumps({ACCOUNT: {"pushed": {"version": 3, "pushedAt": "2026-01-01T00:00:00Z"}}}),
            encoding="utf-8",
        )
        older = NS(
            slug="pushed",
            name="clean name",
            kind="text",
            version=3,
            updated_at="2026-01-01T00:00:00Z",
            description="",
            tags=[],
        )
        newer = NS(
            slug="fresh",
            name="key=AKIAIOSFODNN7EXAMPLEKEYX secret",
            kind="markdown",
            version=1,
            updated_at="2026-06-01T00:00:00Z",
            description="",
            tags=[],
        )
        with mock.patch.object(library, "get_default_store") as store:
            store.return_value.list.return_value = [older, newer]
            rows = library.list_pushable(ACCOUNT)

        # Newest updatedAt sorts first.
        assert [r["slug"] for r in rows] == ["fresh", "pushed"]
        # The credential-shaped fragment in the name is redacted away.
        assert "AKIAIOSFODNN7EXAMPLEKEYX" not in rows[0]["name"]
        # An un-pushed artifact carries null push-state; the pushed one carries
        # the ledger's version.
        assert rows[0]["pushedVersion"] is None
        assert rows[1]["pushedVersion"] == 3
        assert rows[1]["pushedAt"] == "2026-01-01T00:00:00Z"

    def test_scalar_account_entry_reads_as_no_push_state(self, tmp_path, monkeypatch):
        # A corrupted per-account entry (a string where a dict is expected)
        # must not crash the list route — it reads as "nothing pushed".
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: "corrupt-not-a-dict"}), encoding="utf-8")
        art = NS(
            slug="a",
            name="n",
            kind="text",
            version=1,
            updated_at="2026-01-01T00:00:00Z",
            description="",
            tags=[],
        )
        with mock.patch.object(library, "get_default_store") as store:
            store.return_value.list.return_value = [art]
            rows = library.list_pushable(ACCOUNT)
        assert rows[0]["pushedVersion"] is None

    def test_scalar_slug_entry_reads_as_no_push_state(self, tmp_path, monkeypatch):
        # A corrupted per-SLUG entry (dict account, scalar slug value) is the
        # inner guard: it too degrades to empty push-state.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: {"a": "corrupt"}}), encoding="utf-8")
        art = NS(
            slug="a",
            name="n",
            kind="text",
            version=1,
            updated_at="2026-01-01T00:00:00Z",
            description="",
            tags=[],
        )
        with mock.patch.object(library, "get_default_store") as store:
            store.return_value.list.return_value = [art]
            rows = library.list_pushable(ACCOUNT)
        assert rows[0]["pushedVersion"] is None


# --------------------------------------------------------------------------
# push_artifact — the unpushable-kind refusal (image plumbing deferred) and the
# full happy path through the locked ledger read-modify-write.
# --------------------------------------------------------------------------
class TestPushArtifact:
    def test_unpushable_kind_is_refused_before_any_upload(self, tmp_path, monkeypatch):
        # An artifact kind with no text extension (e.g. an image) is refused
        # with a plain ValueError, before a byte reaches the bucket.
        _ledger_at(monkeypatch, tmp_path)
        art = NS(
            slug="pic",
            name="n",
            kind="image",
            version=1,
            description="",
            tags=[],
            content="",
        )
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file") as put,
        ):
            store.return_value.get.return_value = art
            with pytest.raises(ValueError, match="not pushable yet"):
                library.push_artifact("p", "us-west-2", "b", ACCOUNT, "pic")
        put.assert_not_called()

    def test_clean_artifact_uploads_both_objects_and_records_ledger(self, tmp_path, monkeypatch):
        # Happy path: a clean artifact uploads the versioned content object AND
        # a meta.json sidecar, then records a per-account ledger entry. The
        # returned entry carries slug + account for the caller's response.
        _ledger_at(monkeypatch, tmp_path)
        art = NS(
            slug="doc",
            name="a clean doc",
            kind="markdown",
            version=4,
            description="a description",
            tags=["one", "two"],
            content="# hello\nplain body, no secrets",
        )
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file") as put,
        ):
            store.return_value.get.return_value = art
            entry = library.push_artifact("p", "us-west-2", "bucket", ACCOUNT, "doc")

        # Two uploads: the versioned content key and the metadata sidecar.
        assert put.call_count == 2
        keys = [c.args[4] for c in put.call_args_list]
        assert keys == ["doc/v4.md", "doc/meta.json"]

        # The returned entry is the ledger record fused with slug + account.
        assert entry["slug"] == "doc"
        assert entry["account"] == ACCOUNT
        assert entry["version"] == 4
        assert entry["kind"] == "markdown"
        assert "pushedAt" in entry

        # The ledger persisted the same record under this account/slug.
        persisted = library.read_ledger()[ACCOUNT]["doc"]
        assert persisted["version"] == 4
        assert persisted["kind"] == "markdown"

    def test_corrupt_account_entry_is_reset_before_ledger_write(self, tmp_path, monkeypatch):
        # If the ledger already holds a corrupted per-account entry (a scalar),
        # the locked write must reset it to a dict rather than raising when it
        # sets the new slug key.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: "corrupt-scalar"}), encoding="utf-8")
        art = NS(
            slug="doc",
            name="n",
            kind="text",
            version=1,
            description="",
            tags=[],
            content="plain body",
        )
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file"),
        ):
            store.return_value.get.return_value = art
            entry = library.push_artifact("p", "us-west-2", "b", ACCOUNT, "doc")

        assert entry["slug"] == "doc"
        # The corrupted scalar was replaced by a dict carrying the new record.
        persisted = library.read_ledger()[ACCOUNT]
        assert isinstance(persisted, dict)
        assert persisted["doc"]["version"] == 1
