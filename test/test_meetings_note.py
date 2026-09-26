"""The user's own note for a meeting.

This is the first thing in the app the USER writes rather than an agent, and the
two properties worth pinning follow from that:

* **No agent can overwrite it.** Agent output files share the meeting directory and
  their names are derived from the agent id, so the note's filename has to be one
  the derivation provably cannot produce.
* **It is not redacted.** Every other text this app accepts is untrusted input on
  its way to an agent; this is the user's own writing on its way back to only
  themselves, and scrubbing it would silently corrupt what they typed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401 — fixtures are used by name
    app_fixture,
    client_for,
    enabled_fixture,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store


@pytest.fixture(name="_seed_meetings", autouse=True)
def seed_meetings_fixture(root: Path) -> None:
    """Create the meetings these tests address.

    The note mutations share deletion's existence transaction, so a save for a
    meeting that was never created is a 404 BY DESIGN (see
    ``TestAMutationCannotCreateOrRecreateAMeeting``). Seeding the metadata here keeps
    every other test about the note rather than about setup. Ids these tests expect to
    be absent (``missing``, ``never-existed``) are deliberately NOT seeded.
    """
    for meeting_id in ("m1", "m2"):
        store.write_meeting_meta(meeting_id, store.new_meeting_meta(meeting_id, meeting_id), root)


class TestFilenameCannotCollideWithAnAgent:
    def test_note_filename_is_unreachable_by_any_agent(self):
        """The leading underscore is the whole guarantee — pin it.

        An agent's output path is ``safe_agent_id(id) + WIDGET_EXT_MAP[type]``. If
        some id could yield ``_note.md`` then a user who named an agent that would
        have it silently overwrite their own note.
        """
        stem = k.NOTE_FILE.rsplit(".", 1)[0]
        assert stem.startswith("_")
        # The only way to produce this stem would be an agent id equal to it.
        with pytest.raises(store.MeetingsPathError):
            store.safe_agent_id(stem)

    def test_the_obvious_alternatives_would_have_collided(self):
        # Recorded so nobody "tidies" the underscore away: `note` and `notes` are
        # both legal agent ids, so note.md / notes.md are genuinely reachable.
        assert store.safe_agent_id("note") == "note"
        assert store.safe_agent_id("notes") == "notes"
        assert k.WIDGET_EXT_MAP["markdown"] == ".md"

    def test_agent_output_reader_ignores_the_note(self, root: Path):
        # `read_agent_outputs` iterates the CONFIGURED agents and reads only their
        # derived filenames, so the note must be invisible to it.
        store.write_note("m1", "my private note", root)
        config = store.read_config(root)
        outputs = store.read_agent_outputs("m1", config.get("meeting_agents", []), root)
        assert "my private note" not in "".join(outputs.values())


class TestStore:
    def test_missing_note_reads_as_empty(self, root: Path):
        note = store.read_note("never-existed", root)
        assert note["content"] == ""
        assert note["updated_at"] == ""
        # `path` is present even for a note that does not exist yet: the frontend
        # needs it to resolve relative image links the moment the first paste lands.
        assert note["path"].endswith(k.NOTE_FILE)

    def test_round_trips_content(self, root: Path):
        store.write_note("m1", "# Heading\n\n- a point", root)
        note = store.read_note("m1", root)
        assert note["content"] == "# Heading\n\n- a point"
        assert note["updated_at"]

    def test_an_empty_save_clears_the_note(self, root: Path):
        # Deleting everything is a legitimate edit, not a malformed request.
        store.write_note("m1", "something", root)
        store.write_note("m1", "", root)
        assert store.read_note("m1", root)["content"] == ""

    def test_writes_land_in_the_meeting_directory(self, root: Path):
        path = store.note_path("m1", root)
        assert path.parent == store.meetings_root(root).resolve() / "m1"
        assert path.name == k.NOTE_FILE

    def test_the_path_is_contained(self, root: Path):
        resolved = store.note_path("m1", root)
        assert resolved.is_relative_to(store.data_dir(root).resolve())

    def test_an_unsafe_meeting_id_is_refused(self, root: Path):
        with pytest.raises(store.MeetingsPathError):
            store.note_path("../escape", root)

    def test_unicode_survives_the_round_trip(self, root: Path):
        text = "決定: 金曜日にリリース\n\n> 引用\n\n\U0001f600"
        store.write_note("m1", text, root)
        assert store.read_note("m1", root)["content"] == text


class TestRoutes:
    @pytest.mark.asyncio
    async def test_get_is_empty_before_the_first_save(self, app):
        async with client_for(app) as client:
            resp = await client.get(f"{k.API_BASE}/meetings/m1/note")
            assert resp.status == 200
            body = await resp.json()
        assert body["content"] == ""
        assert body["updated_at"] == ""
        # Compared as path parts rather than a "/"-joined suffix: the route
        # returns a native filesystem path, which Windows separates with \\.
        assert Path(body["path"]).parts[-2:] == ("m1", k.NOTE_FILE)

    @pytest.mark.asyncio
    async def test_put_then_get(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{k.API_BASE}/meetings/m1/note", json={"content": "ship on Friday"}
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["content"] == "ship on Friday"
            assert body["updated_at"]

            resp = await client.get(f"{k.API_BASE}/meetings/m1/note")
            assert (await resp.json())["content"] == "ship on Friday"

    @pytest.mark.asyncio
    async def test_put_is_not_redacted(self, app):
        # The distinguishing property of this endpoint. A user pasting a key into
        # their OWN memo must get their text back verbatim — silently rewriting it
        # would corrupt a note they may be relying on.
        secret = "my aws key is AKIAIOSFODNN7EXAMPLE, do not lose it"
        async with client_for(app) as client:
            resp = await client.put(f"{k.API_BASE}/meetings/m1/note", json={"content": secret})
            assert (await resp.json())["content"] == secret

    @pytest.mark.asyncio
    async def test_put_rejects_an_oversized_note(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{k.API_BASE}/meetings/m1/note",
                json={"content": "x" * (k.MAX_NOTE_CHARS + 1)},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_accepts_a_note_at_the_cap(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{k.API_BASE}/meetings/m1/note", json={"content": "x" * k.MAX_NOTE_CHARS}
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_an_unpaired_surrogate_is_a_400_not_a_500(self, app):
        """JSON ``\\udXXX`` escapes decode into str that UTF-8 cannot encode.

        Without the route's own check the failure happened inside ``atomic_write``
        and surfaced as a 500 — a server fault for what is a malformed body. The
        minutes PUT already refuses this; the note PUT is the same shape.
        """
        async with client_for(app) as client:
            payload = '{"content": "broken \\ud800 here"}'
            resp = await client.put(
                f"{k.API_BASE}/meetings/m1/note",
                data=payload.encode("utf-8", "surrogatepass"),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "content_not_unicode"
            # The note must be untouched, not half-written.
            survived = await (await client.get(f"{k.API_BASE}/meetings/m1/note")).json()
            assert survived["content"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [17, None, [], {}, True])
    async def test_put_refuses_a_malformed_body_instead_of_erasing(self, app, bad):
        # The failure this guards: treating a non-string as "missing" would return
        # 200 having wiped a note the user cannot regenerate.
        async with client_for(app) as client:
            await client.put(f"{k.API_BASE}/meetings/m1/note", json={"content": "keep me"})
            resp = await client.put(f"{k.API_BASE}/meetings/m1/note", json={"content": bad})
            assert resp.status == 400
            survived = await (await client.get(f"{k.API_BASE}/meetings/m1/note")).json()
        assert survived["content"] == "keep me"

    @pytest.mark.asyncio
    async def test_put_refuses_a_body_with_no_content_field(self, app):
        async with client_for(app) as client:
            await client.put(f"{k.API_BASE}/meetings/m1/note", json={"content": "keep me"})
            resp = await client.put(f"{k.API_BASE}/meetings/m1/note", json={})
            assert resp.status == 400
            survived = await (await client.get(f"{k.API_BASE}/meetings/m1/note")).json()
        assert survived["content"] == "keep me"

    @pytest.mark.asyncio
    async def test_whitespace_the_user_typed_is_preserved(self, app):
        # Not `strip()`ped: a trailing blank line under a list, or an indented block,
        # is part of the note. Rewriting it on every autosave would feel broken.
        text = "  indented start\n\n- a list item\n\n\n"
        async with client_for(app) as client:
            resp = await client.put(f"{k.API_BASE}/meetings/m1/note", json={"content": text})
            assert (await resp.json())["content"] == text
            fetched = await (await client.get(f"{k.API_BASE}/meetings/m1/note")).json()
        assert fetched["content"] == text

    @pytest.mark.asyncio
    async def test_notes_are_per_meeting(self, app):
        async with client_for(app) as client:
            await client.put(f"{k.API_BASE}/meetings/m1/note", json={"content": "one"})
            await client.put(f"{k.API_BASE}/meetings/m2/note", json={"content": "two"})
            first = await (await client.get(f"{k.API_BASE}/meetings/m1/note")).json()
            second = await (await client.get(f"{k.API_BASE}/meetings/m2/note")).json()
        assert first["content"] == "one"
        assert second["content"] == "two"

    @pytest.mark.asyncio
    async def test_an_unsafe_meeting_id_is_refused(self, app):
        async with client_for(app) as client:
            resp = await client.get(f"{k.API_BASE}/meetings/..%2F..%2Fetc/note")
            assert resp.status in (400, 403, 404)


class TestAnUnreadableNoteIsNotReportedAsEmpty:
    """A read failure has to surface. Reporting it as "no note yet" deletes the note.

    The panel leaves the textarea EMPTY for an empty note, and its autosave replaces
    the file on the first keystroke, so a swallowed read error turns an unreadable
    note into an overwritten one. The neighbours in ``store`` are NOT the contract to
    copy: ``_read_json`` logs and still returns its default, and ``read_agent_edit``
    returns ``None``. Neither tells a caller anything, which is why this is pinned
    here.
    """

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_the_store_raises_instead_of_returning_an_empty_note(self, root: Path):
        if os.geteuid() == 0:  # pragma: no cover - root ignores permission bits
            pytest.skip("root can read a 0o000 file")
        store.write_note("m1", "the note I cannot regenerate", root)
        path = store.note_path("m1", root)
        path.chmod(0o000)
        try:
            with pytest.raises(OSError):
                store.read_note("m1", root)
        finally:
            path.chmod(0o600)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_missing_note_is_still_empty_rather_than_an_error(self, root: Path):
        # The other half of the same branch: absent is the normal first state for
        # every meeting, so only a real read FAILURE may propagate.
        assert store.read_note("never-existed", root)["content"] == ""

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    async def test_the_route_answers_500_and_the_file_survives(self, app, root: Path):
        if os.geteuid() == 0:  # pragma: no cover - root ignores permission bits
            pytest.skip("root can read a 0o000 file")
        store.write_note("m1", "the note I cannot regenerate", root)
        path = store.note_path("m1", root)
        before = path.read_bytes()
        path.chmod(0o000)
        try:
            async with client_for(app) as client:
                resp = await client.get(f"{k.API_BASE}/meetings/m1/note")
                assert resp.status == 500
                assert (await resp.json())["code"] == "note_unreadable"
        finally:
            path.chmod(0o600)
        # Nothing may have replaced it on the way through the failure.
        assert path.read_bytes() == before

    @pytest.mark.asyncio
    async def test_the_failed_read_writes_nothing(self, app, monkeypatch):
        """Portable half, on every platform including the Windows shards.

        ``atomic_write`` is booby-trapped rather than merely observed: the harm this
        pins is the WRITE that follows a swallowed read, so "nothing wrote" is the
        property under test, not a footnote.
        """

        def _refuse_to_write(*_args, **_kwargs):
            raise AssertionError("a failed note read must not write anything")

        def _unreadable(*_args, **_kwargs):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(store, "atomic_write", _refuse_to_write)
        monkeypatch.setattr(store, "read_note", _unreadable)
        async with client_for(app) as client:
            resp = await client.get(f"{k.API_BASE}/meetings/m1/note")
            assert resp.status == 500
            body = await resp.json()
        assert body["code"] == "note_unreadable"
        # The sentence reaches the panel verbatim (the hook carries
        # `(noteQuery.error as Error).message` into `loadError`), so it has to read
        # as something a person can act on.
        assert "note" in body["error"]


class TestAMutationCannotCreateOrRecreateAMeeting:
    """The note mutations share deletion's existence transaction.

    ``store.write_note`` does ``mkdir(parents=True)`` before writing, so without this
    a save for an unknown meeting created the directory, and a debounced autosave (or
    the panel's unmount flush) landing just after a delete recreated it holding
    nothing but ``_note.md`` and no ``session.json`` — an orphan the list cannot show
    and the user cannot delete again. Exactly the failure ``_save_edit`` was given the
    same guard for; these are the two writers that did not inherit it.
    """

    @pytest.mark.asyncio
    async def test_a_save_for_an_unknown_meeting_is_404_and_writes_nothing(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.put(f"{k.API_BASE}/meetings/missing/note", json={"content": "x"})
            assert resp.status == 404
            assert (await resp.json())["code"] == "meeting_not_found"
        assert not (store.meetings_root(root) / "missing").exists()

    @pytest.mark.asyncio
    async def test_a_save_after_a_delete_does_not_recreate_the_directory(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{k.API_BASE}/meetings/m9/init", json={"title": "Nine"})
            await client.put(f"{k.API_BASE}/meetings/m9/note", json={"content": "mine"})
            deleted = await client.delete(f"{k.API_BASE}/meetings/m9")
            assert deleted.status == 204
            directory = store.meetings_root(root) / "m9"
            assert not directory.exists()
            # The late autosave. This is the whole finding.
            late = await client.put(f"{k.API_BASE}/meetings/m9/note", json={"content": "late"})
            assert late.status == 404
        assert not directory.exists()

    def test_the_guard_is_one_delete_safe_unit(self):
        """Pinned by source, the way the minutes helpers are.

        The check and the write have to be in ONE transaction: two statements that
        merely run in order would still let a delete land between them.
        """
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml

        for helper in (ml._save_note, ml._save_note_image):
            src = inspect.getsource(helper)
            assert "with store.meta_transaction()" in src
            assert "store.read_meeting_meta" in src
