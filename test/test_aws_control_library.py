"""Line-coverage for the Library sync module (``backend/library.py``).

``test_aws_control_app.py::TestLibraryScan`` already pins the two push
REFUSALS (credential-bearing content, beacon URL). This file covers the
lines those cases leave cold: the ledger read/write shape guards, the
redacting ``list_pushable`` display path, the unpushable-kind refusal, the
full happy-path push through the single ledger writer, and the removal side --
``_update_ledger``'s single-writer discipline, ``valid_slug``'s blast-radius
guard, ``reconcile``'s bucket-corrects-the-ledger direction, and
``library_remove``'s ordering.

Comments explain WHY each case matters, matching the sibling file's style.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import library
from kiro_crew.deploy.engine import AWSError

ACCOUNT = "123456789012"


def _ledger_at(monkeypatch, tmp_path):
    """Point the module's ledger at a throwaway path and return it."""
    path = tmp_path / "library.json"
    monkeypatch.setattr(library, "_ledger_path", lambda: path)
    return path


def _adds_a_record(slugs):
    """A mutation that really changes something -- `_update_ledger` skips the
    write when the callback reports no change, so a falsy edit would never
    reach the rewrite these tests are about."""
    slugs["new"] = {"version": 1}
    return True


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

    def test_a_non_utf8_ledger_reads_as_empty(self, tmp_path, monkeypatch):
        # New with #7805: UnicodeDecodeError previously escaped the display
        # read (it is a ValueError, not a JSONDecodeError). The lenient read
        # must treat a corrupt byte stream as one condition regardless of
        # which decoder noticed it.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_bytes(b"\xff\xfe not utf8")
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

    def test_key_layout_is_a_contract_the_drive_page_reads(self, tmp_path, monkeypatch):
        # THE FRONTEND DEPENDS ON THIS LAYOUT, so changing it needs a change here.
        #
        # The account console's Drive lists the `library` prefix and treats its
        # top level as one FOLDER PER SLUG: the folder name IS the slug, which is
        # what lets a card recover the artifact's name, kind and preview from the
        # local library (website/src/apps/aws-control/DrivePage.tsx). If a push
        # ever wrote a flat key, a differently-nested one, or a different first
        # path segment, every card would silently degrade to "In the cloud only"
        # and no test would go red. Hence: assert the SHAPE, not one literal pair.
        _ledger_at(monkeypatch, tmp_path)
        # A slug with a hyphen and a two-digit version, so a naive split or a
        # version-in-the-folder-name layout cannot pass by coincidence.
        art = NS(
            slug="my-notes",
            name="n",
            kind="markdown",
            version=12,
            description="",
            tags=[],
            content="body",
        )
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file") as put,
        ):
            store.return_value.get.return_value = art
            library.push_artifact("p", "us-west-2", "bucket", ACCOUNT, "my-notes")

        sections = {c.args[3] for c in put.call_args_list}
        keys = [c.args[4] for c in put.call_args_list]

        # One prefix, the one the page lists.
        assert sections == {"library"}
        # Every object sits UNDER a folder whose name is exactly the slug, so the
        # prefix's top level is slugs and nothing else.
        assert keys, "push uploaded nothing"
        for key in keys:
            assert key.startswith("my-notes/"), key
            assert key.split("/")[0] == "my-notes", key
        # The sidecar lives INSIDE that folder -- not beside it, where it would
        # show up as its own top-level entry and render as a bogus card.
        assert "my-notes/meta.json" in keys
        # And the content object is versioned within the folder.
        assert any(k.startswith("my-notes/v") for k in keys), keys

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

    def test_the_ledger_stamp_is_taken_after_the_uploads_not_before(self, tmp_path, monkeypatch):
        # The stamp is what _recorded_at_or_after compares a remote observation
        # against, so it has to mean "when this record came into existence". With
        # the metadata sidecar's PRE-upload stamp, a listing that ran during a
        # slow upload would postdate the stamp while predating the record, and a
        # reconcile on that listing would delete a record whose objects landed.
        _ledger_at(monkeypatch, tmp_path)
        art = NS(
            slug="doc",
            name="n",
            kind="text",
            version=1,
            description="",
            tags=[],
            content="plain body",
        )
        marks: dict[str, dt.datetime] = {}

        def _slow_upload(*_a, **_kw):
            # Sleep FIRST, then mark: the mark has to land in a LATER second than
            # the sidecar's pre-upload stamp, or second-flooring makes the two
            # indistinguishable and this test stops discriminating between them.
            # A listing taken at the mark is AFTER the push began and BEFORE the
            # record exists -- the window the pre-upload stamp got wrong.
            time.sleep(1.1)
            marks.setdefault("mid_upload", dt.datetime.now(dt.timezone.utc))

        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file", side_effect=_slow_upload),
        ):
            store.return_value.get.return_value = art
            entry = library.push_artifact("p", "us-west-2", "b", ACCOUNT, "doc")

        stamped = dt.datetime.fromisoformat(entry["pushedAt"])
        # The record's own stamp postdates the mid-upload instant, so a reconcile
        # on a listing taken then leaves it alone.
        assert stamped >= marks["mid_upload"].replace(microsecond=0)
        assert library.reconcile(ACCOUNT, set(), observed_at=marks["mid_upload"]) == []
        assert "doc" in library.read_ledger()[ACCOUNT]


# --------------------------------------------------------------------------
# _update_ledger — the ONE writer. Push, removal and reconcile all edit the
# file through it, which is what stops two writers of one JSON document from
# dropping each other's records.
# --------------------------------------------------------------------------
class TestUpdateLedger:
    def test_a_mutation_that_changed_nothing_writes_no_file(self, tmp_path, monkeypatch):
        # The Library list reconciles on EVERY page render. A reconcile that
        # finds nothing stale must cost no disk write, or an idle console
        # rewrites this file forever.
        path = _ledger_at(monkeypatch, tmp_path)
        assert library._update_ledger(ACCOUNT, lambda slugs: False) is False
        assert not path.exists()

    def test_a_corrupted_account_entry_is_reset_before_the_callback_runs(
        self, tmp_path, monkeypatch
    ):
        # The callback is promised a dict. A scalar per-account entry (hand-edited
        # or half-written) must be reset first, not handed through as a str the
        # callback would raise on.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: "corrupt"}), encoding="utf-8")
        seen: list[object] = []

        def _mutate(slugs):
            # A COPY: the same dict is mutated below, so keeping the reference
            # would assert against the post-mutation state, not what was handed in.
            seen.append(dict(slugs))
            slugs["fresh"] = {"version": 1}
            return True

        assert library._update_ledger(ACCOUNT, _mutate) is True
        assert seen == [{}]
        assert library.read_ledger()[ACCOUNT] == {"fresh": {"version": 1}}

    def test_other_accounts_are_carried_through_untouched(self, tmp_path, monkeypatch):
        # The write rewrites the WHOLE document, so a mutation scoped to one
        # account must not drop a sibling account's records -- the exact loss the
        # single-writer rule exists to prevent.
        path = _ledger_at(monkeypatch, tmp_path)
        other = "999988887777"
        path.write_text(
            json.dumps({other: {"kept": {"version": 7}}, ACCOUNT: {"gone": {"version": 1}}}),
            encoding="utf-8",
        )
        library._update_ledger(ACCOUNT, lambda slugs: bool(slugs.pop("gone", None)))
        ledger = library.read_ledger()
        assert ledger[other] == {"kept": {"version": 7}}
        assert ledger[ACCOUNT] == {}

    def test_a_read_that_failed_is_never_published_over(self, tmp_path, monkeypatch):
        # `read_ledger` is a DISPLAY read: it collapses every failure to {} so a
        # render never crashes. As the base of this whole-document rewrite that
        # empty dict is not "nothing to carry forward", it is "delete every other
        # account's push state". A transient EACCES (a scanner holding the handle)
        # must abandon the mutation, not publish over state nobody read.
        path = _ledger_at(monkeypatch, tmp_path)
        other = "999988887777"
        path.write_text(json.dumps({other: {"kept": {"version": 7}}}), encoding="utf-8")
        real_read_text = Path.read_text
        broken = {"on": True}

        def guarded(path_self, *args, **kwargs):
            if broken["on"] and Path(path_self) == path:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)
        with contextlib.suppress(OSError):
            library._update_ledger(ACCOUNT, _adds_a_record)

        # The durable harm, asserted directly: the OTHER account's push state
        # must still be on disk, whatever this mutation did or did not do.
        broken["on"] = False
        assert library.read_ledger() == {other: {"kept": {"version": 7}}}

    def test_an_unreadable_ledger_refuses_the_mutation(self, tmp_path, monkeypatch):
        # The caller must be told, rather than being handed a silent no-op that
        # reads as success.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: {"kept": {"version": 7}}}), encoding="utf-8")
        real_read_text = Path.read_text

        def guarded(path_self, *args, **kwargs):
            if Path(path_self) == path:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)
        with pytest.raises(OSError):
            library._update_ledger(ACCOUNT, _adds_a_record)

    def test_a_missing_ledger_is_still_a_first_write(self, tmp_path, monkeypatch):
        # Absent is the one failure where {} is the truth. The guard above must
        # not turn the very first push into an error.
        path = _ledger_at(monkeypatch, tmp_path)
        assert not path.exists()
        assert library._update_ledger(ACCOUNT, _adds_a_record) is True
        assert library.read_ledger()[ACCOUNT] == {"new": {"version": 1}}

    def test_a_corrupt_ledger_refuses_the_write_and_is_left_intact(self, tmp_path, monkeypatch):
        # #7805: a corrupt ledger is refused, never rewritten. The old tolerance
        # read it as empty and let the whole-file rewrite drop every other
        # account's push state -- records a truncated JSON still held verbatim.
        path = _ledger_at(monkeypatch, tmp_path)
        corrupt = '{"acct": {"slug": {"version": 1}}'  # truncated
        path.write_text(corrupt, encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            library._update_ledger(ACCOUNT, _adds_a_record)
        assert (
            path.read_text(encoding="utf-8") == corrupt
        ), "the writer rewrote a corrupt ledger instead of refusing"
        # The display read stays lenient: the Library list renders on a ledger
        # it could not load rather than failing the route.
        assert library.read_ledger() == {}

    def test_a_ledger_that_is_not_utf8_takes_the_corruption_path(self, tmp_path, monkeypatch):
        # UnicodeDecodeError is a ValueError but NOT a JSONDecodeError; unwrapped
        # it would slip past every corruption clause at the callers.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_bytes(b"\xff\xfe not utf8")
        with pytest.raises(json.JSONDecodeError):
            library._update_ledger(ACCOUNT, _adds_a_record)
        assert path.read_bytes() == b"\xff\xfe not utf8"

    def test_a_ledger_that_parses_to_a_non_object_refuses_the_write(self, tmp_path, monkeypatch):
        # Valid JSON with the wrong root parses without raising, so coercing it
        # to {} would let the rewrite destroy a document nobody could read.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text('["not", "a", "dict"]', encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            library._update_ledger(ACCOUNT, _adds_a_record)
        assert path.read_text(encoding="utf-8") == '["not", "a", "dict"]'


# --------------------------------------------------------------------------
# valid_slug — the slug becomes an object PREFIX in library_remove, so the
# shape is checked against the artifact store's own validator.
# --------------------------------------------------------------------------
class TestValidSlug:
    def test_accepts_a_store_shaped_slug(self):
        assert library.valid_slug("my-artifact-2")

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # would widen the delete prefix to the whole section
            "/",  # ditto
            "..",  # traversal shape
            "a/b",  # a key, not a slug: reaches below one artifact
            "Upper",  # the store lowercases; an uppercase key is not ours
            "trailing-",  # the store never emits a trailing hyphen
        ],
    )
    def test_refuses_anything_the_store_could_not_have_written(self, bad):
        assert not library.valid_slug(bad)


# --------------------------------------------------------------------------
# reconcile — the direction that was missing. The bucket corrects the ledger;
# a record the bucket does not back is dropped rather than believed.
# --------------------------------------------------------------------------
#: A listing taken "now". Records stamped before it are judged against it; a
#: record stamped at or after it was written too late for that listing to have
#: seen, so it is off limits.
NOW = dt.datetime(2026, 8, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
BEFORE = "2026-08-30T11:59:00+00:00"
AFTER = "2026-08-30T12:00:30+00:00"


class TestReconcile:
    def test_drops_records_the_bucket_does_not_back(self, tmp_path, monkeypatch):
        # The symptom this repairs: a stale record made the console refuse the
        # very push that would have restored a cloud copy deleted elsewhere.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(
            json.dumps(
                {
                    ACCOUNT: {
                        "still-there": {"version": 1, "pushedAt": BEFORE},
                        "deleted-elsewhere": {"version": 2, "pushedAt": BEFORE},
                    }
                }
            ),
            encoding="utf-8",
        )
        forgotten = library.reconcile(ACCOUNT, {"still-there"}, observed_at=NOW)
        assert forgotten == ["deleted-elsewhere"]
        assert library.read_ledger()[ACCOUNT] == {"still-there": {"version": 1, "pushedAt": BEFORE}}

    def test_a_record_written_after_the_listing_is_not_pruned(self, tmp_path, monkeypatch):
        # THE race both GPT and Design flagged. The listing is a snapshot; a push
        # that completes after it -- objects uploaded, record written -- is absent
        # from the snapshot while the bucket DOES back it. Pruning on the stale
        # set would delete a live record, breaking reconcile's own rule that it
        # only drops what the bucket has disproven.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(
            json.dumps({ACCOUNT: {"pushed-mid-render": {"version": 1, "pushedAt": AFTER}}}),
            encoding="utf-8",
        )
        assert library.reconcile(ACCOUNT, set(), observed_at=NOW) == []
        assert "pushed-mid-render" in library.read_ledger()[ACCOUNT]

    def test_a_record_stamped_in_the_listings_own_second_is_not_pruned(self, tmp_path, monkeypatch):
        # pushedAt is written with timespec="seconds", so a push at 12:00:00.9
        # stores "12:00:00". Judged against an unfloored 12:00:00.1 cutoff it
        # would look older than the snapshot and be pruned; flooring plus >=
        # absorbs exactly that truncation.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(
            json.dumps(
                {ACCOUNT: {"same-second": {"version": 1, "pushedAt": "2026-08-30T12:00:00+00:00"}}}
            ),
            encoding="utf-8",
        )
        mid_second = NOW.replace(microsecond=100000)
        assert library.reconcile(ACCOUNT, set(), observed_at=mid_second) == []
        assert "same-second" in library.read_ledger()[ACCOUNT]

    @pytest.mark.parametrize("stamp", [None, "not-a-timestamp", 12345])
    def test_an_undatable_record_stays_prunable(self, stamp, tmp_path, monkeypatch):
        # Every record this module writes carries a parseable pushedAt, so one
        # without it is not a push racing the listing. Protecting it would make a
        # corrupted record permanently unrepairable, which is the opposite of
        # what reconcile is for.
        path = _ledger_at(monkeypatch, tmp_path)
        entry: dict = {"version": 1}
        if stamp is not None:
            entry["pushedAt"] = stamp
        path.write_text(json.dumps({ACCOUNT: {"undatable": entry}}), encoding="utf-8")
        assert library.reconcile(ACCOUNT, set(), observed_at=NOW) == ["undatable"]

    def test_a_naive_stamp_is_read_as_utc_rather_than_crashing(self, tmp_path, monkeypatch):
        # A tz-naive stamp (hand-edited, or an older writer) must not raise on
        # the aware/naive comparison. Read as this machine's UTC, an AFTER stamp
        # is still protected.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(
            json.dumps({ACCOUNT: {"naive": {"version": 1, "pushedAt": "2026-08-30T12:00:30"}}}),
            encoding="utf-8",
        )
        assert library.reconcile(ACCOUNT, set(), observed_at=NOW) == []

    def test_a_cloud_copy_with_no_record_is_not_invented(self, tmp_path, monkeypatch):
        # Version and push time live in the copy's meta.json, one GET per slug.
        # Reconcile reports the gap to its caller instead of fabricating a
        # record it cannot know.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: {}}), encoding="utf-8")
        assert library.reconcile(ACCOUNT, {"pushed-from-another-machine"}, observed_at=NOW) == []
        assert library.read_ledger()[ACCOUNT] == {}

    def test_an_agreeing_ledger_is_left_alone_and_unwritten(self, tmp_path, monkeypatch):
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: {"a": {"version": 1}}}), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        assert library.reconcile(ACCOUNT, {"a"}, observed_at=NOW) == []
        assert path.read_text(encoding="utf-8") == before

    def test_another_accounts_records_are_not_pruned(self, tmp_path, monkeypatch):
        # The listing is ONE account's bucket. Pruning on it must never reach a
        # second account's records, which no part of this observation covers.
        path = _ledger_at(monkeypatch, tmp_path)
        other = "999988887777"
        path.write_text(
            json.dumps({other: {"theirs": {"version": 1}}, ACCOUNT: {"ours": {"version": 1}}}),
            encoding="utf-8",
        )
        assert library.reconcile(ACCOUNT, set(), observed_at=NOW) == ["ours"]
        assert library.read_ledger()[other] == {"theirs": {"version": 1}}

    def test_a_freshly_pushed_record_survives_a_reconcile_on_a_stale_listing(
        self, tmp_path, monkeypatch
    ):
        # End to end on the real writers rather than a hand-built ledger: take
        # the snapshot, push (which stamps pushedAt = now), then reconcile on the
        # snapshot that predates it. The record must survive.
        _ledger_at(monkeypatch, tmp_path)
        art = NS(
            slug="doc",
            name="n",
            kind="text",
            version=1,
            description="",
            tags=[],
            content="plain body",
        )
        observed_at = dt.datetime.now(dt.timezone.utc)
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file"),
        ):
            store.return_value.get.return_value = art
            library.push_artifact("p", "us-west-2", "b", ACCOUNT, "doc")
        assert library.reconcile(ACCOUNT, set(), observed_at=observed_at) == []
        assert "doc" in library.read_ledger()[ACCOUNT]


# --------------------------------------------------------------------------
# library_remove — objects and record removed together, in the order whose
# only reachable divergence is the one reconcile repairs.
# --------------------------------------------------------------------------
class TestLibraryRemove:
    def test_removes_the_whole_slug_prefix_and_forgets_the_record(self, tmp_path, monkeypatch):
        # The whole `<slug>/` prefix, not the recorded version: a slug pushed at
        # v1 and again at v2 has two content keys plus the sidecar, and deleting
        # only the recorded one leaves a paid object no surface lists.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(
            json.dumps({ACCOUNT: {"doc": {"version": 2}, "other": {"version": 1}}}),
            encoding="utf-8",
        )
        with (
            mock.patch.object(library.storage, "delete_prefix", return_value=3) as rm,
            mock.patch.object(library.storage, "list_library_folders", return_value=["other"]),
        ):
            result = library.library_remove("p", "us-west-2", "bucket", ACCOUNT, "doc")

        assert rm.call_args.args == ("p", "us-west-2", "bucket", "library", "doc")
        assert rm.call_args.kwargs == {"account": ACCOUNT}
        assert result == {
            "slug": "doc",
            "account": ACCOUNT,
            "objects": 3,
            "forgotten": True,
        }
        # The record is gone and the sibling slug is untouched.
        assert library.read_ledger()[ACCOUNT] == {"other": {"version": 1}}

    def test_a_prefix_still_present_after_the_delete_raises_and_keeps_the_record(
        self, tmp_path, monkeypatch
    ):
        # delete_prefix DELIBERATELY degrades on a garbled listing page: it stops
        # the walk and returns the count so far, so it can under-delete. Forgetting
        # the record on that would drop a copy still in the bucket and call the
        # removal done, so the absence is confirmed first -- and confirmed BEFORE
        # the ledger is touched, so a failed check cannot forget anything.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: {"doc": {"version": 2}}}), encoding="utf-8")
        with (
            mock.patch.object(library.storage, "delete_prefix", return_value=1),
            mock.patch.object(library.storage, "list_library_folders", return_value=["doc"]),
        ):
            with pytest.raises(AWSError, match="still present after the delete"):
                library.library_remove("p", "us-west-2", "bucket", ACCOUNT, "doc")
        # Record intact: the copy is still there, so the ledger is still right.
        assert library.read_ledger()[ACCOUNT]["doc"]["version"] == 2

    def test_objects_are_deleted_before_the_record_is_forgotten(self, tmp_path, monkeypatch):
        # ORDER is the whole design: if the ledger were cleared first, a failing
        # delete would leave an UNTRACKED cloud copy -- objects nothing points
        # at, invisible to the surface that would remove them. Failing this way
        # round leaves only "the ledger claims a copy the bucket lacks", which
        # reconcile repairs on the next listing.
        path = _ledger_at(monkeypatch, tmp_path)
        path.write_text(json.dumps({ACCOUNT: {"doc": {"version": 1}}}), encoding="utf-8")
        with mock.patch.object(
            library.storage, "delete_prefix", side_effect=AWSError("access denied")
        ):
            with pytest.raises(AWSError):
                library.library_remove("p", "us-west-2", "bucket", ACCOUNT, "doc")
        # Still recorded: nothing was forgotten on the strength of a delete that
        # did not happen.
        assert library.read_ledger()[ACCOUNT] == {"doc": {"version": 1}}

    def test_an_untracked_cloud_copy_is_removable_and_reported_as_such(self, tmp_path, monkeypatch):
        # A copy pushed from another machine has objects and no local record.
        # It must still be removable -- that is the "empty what it fills" case --
        # and `forgotten` tells the caller no record was involved.
        _ledger_at(monkeypatch, tmp_path)
        with (
            mock.patch.object(library.storage, "delete_prefix", return_value=2),
            mock.patch.object(library.storage, "list_library_folders", return_value=[]),
        ):
            result = library.library_remove("p", "us-west-2", "bucket", ACCOUNT, "elsewhere")
        assert result["objects"] == 2
        assert result["forgotten"] is False

    def test_a_record_written_after_the_sweep_is_not_forgotten(self, tmp_path, monkeypatch):
        # The cross-process race GPT found: a second gateway pushes this slug
        # after the delete sweep has stopped looking, so its record names objects
        # that really are in the bucket. Popping it would leave objects with no
        # record and report a removal that did not fully happen.
        path = _ledger_at(monkeypatch, tmp_path)

        def _sweep(*_a, **_kw):
            # Stands in for the other gateway's push landing past the sweep's
            # final listing. The record is written, and only THEN does the sweep
            # return -- which is what discriminates the cutoff's direction: read
            # before the delete the record is protected, read after the delete
            # returns it compares as older than the cutoff and is forgotten.
            path.write_text(
                json.dumps(
                    {
                        ACCOUNT: {
                            "doc": {
                                "version": 9,
                                "pushedAt": dt.datetime.now(dt.timezone.utc).isoformat(
                                    timespec="seconds"
                                ),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            time.sleep(1.1)
            return 1

        with (
            mock.patch.object(library.storage, "delete_prefix", side_effect=_sweep),
            mock.patch.object(library.storage, "list_library_folders", return_value=[]),
        ):
            result = library.library_remove("p", "us-west-2", "bucket", ACCOUNT, "doc")
        # The newer record survives, and the report says no record was dropped
        # rather than claiming a clean removal.
        assert result["forgotten"] is False
        assert library.read_ledger()[ACCOUNT]["doc"]["version"] == 9

    @pytest.mark.parametrize("bad", ["", "/", "..", "a/b"])
    def test_a_slug_that_would_widen_the_prefix_is_refused_before_any_delete(
        self, bad, tmp_path, monkeypatch
    ):
        # The blast-radius guard, checked HERE and not only at the route: an
        # empty or '/'-bearing value would turn the delete prefix into the whole
        # artifacts/ section.
        _ledger_at(monkeypatch, tmp_path)
        with mock.patch.object(library.storage, "delete_prefix") as rm:
            with pytest.raises(ValueError, match="not an artifact slug"):
                library.library_remove("p", "us-west-2", "bucket", ACCOUNT, bad)
        rm.assert_not_called()
