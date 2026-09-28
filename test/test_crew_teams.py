"""Crewmate teams: the ``crew-teams/teams.json`` store and the ``/api/teams`` routes.

``KIROCREW_HOME`` is pinned per test by the autouse ``_isolate_kirocrew_home``
fixture in conftest, so every store read and write here lands in a temp home.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import crew_teams as teams
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewAgentConfig

RADAR = "Radar"
FIXER = "Fixer"
SCRIBE = "Scribe"
KNOWN = {RADAR, FIXER, SCRIBE}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class TestTeamsStore:
    def test_absent_file_reads_as_no_teams(self):
        assert teams.read_teams() == []
        assert not teams.teams_path().exists()

    def test_create_persists_in_the_masked_directory(self):
        team = teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)
        assert team.name == "Issue triage"
        assert team.members == [RADAR, FIXER]
        assert teams.teams_path().parent.name == teams.TEAMS_DIR_NAME
        stored = teams.read_teams()
        assert [t.to_dict() for t in stored] == [team.to_dict()]

    def test_name_is_stripped_and_bounded(self):
        team = teams.create_team("  Docs  ", [], known=lambda: KNOWN)
        assert team.name == "Docs"
        with pytest.raises(teams.TeamError) as exc:
            teams.create_team("   ", [], known=lambda: KNOWN)
        assert exc.value.code == "invalid_team_name"
        with pytest.raises(teams.TeamError) as exc:
            teams.create_team("x" * (teams.TEAM_NAME_MAX_CHARS + 1), [], known=lambda: KNOWN)
        assert exc.value.code == "team_name_too_long"

    def test_control_characters_and_lone_surrogates_are_refused(self):
        with pytest.raises(teams.TeamError):
            teams.create_team("Docs\x1b[2J", [], known=lambda: KNOWN)
        with pytest.raises(teams.TeamError):
            teams.create_team("Docs\u2028Two", [], known=lambda: KNOWN)
        with pytest.raises(teams.TeamError):
            teams.create_team("Docs\ud800", [], known=lambda: KNOWN)
        # A refused write leaves no file behind.
        assert teams.read_teams() == []

    def test_format_characters_and_no_break_space_are_names(self):
        """A ZWJ emoji sequence and a pasted no-break space are legitimate in a
        name; only control characters and line separators are refused."""
        name = "Ops\u00a0\U0001f469\u200d\U0001f4bb"
        assert teams.create_team(name, [], known=lambda: KNOWN).name == name

    def test_store_directory_is_masked_and_fenced(self):
        """The document is the owner's grouping of the crewmates it names, so the
        crewmates must not be able to rewrite it: masked from every sandboxed
        process (and pre-created so the mask is never vacuous) and fenced from
        agent file tools. Under ``trust/`` it would be sandbox read-write."""
        from kiro_crew import sandbox
        from kiro_crew.security import paths as security_paths

        leaf = teams.TEAMS_DIR_NAME
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert leaf in security_paths._CREW_SECRET_LEAVES
        assert "trust" not in teams.teams_path().parts

    def test_a_valid_write_can_never_outgrow_the_read_cap(self):
        """The largest document the caps allow must still read back; a write
        that would exceed the read cap is refused BEFORE it lands, so a valid
        sequence of writes cannot leave the store unreadable."""
        widest = "x" * (teams.MEMBER_NAME_MAX_CHARS - 1)
        assert teams.max_document_bytes() <= teams.TEAMS_FILE_MAX_BYTES
        crews = {f"{widest[:-4]}{i:04d}" for i in range(teams.TEAM_MEMBERS_MAX)}
        team = teams.create_team(
            "w" * teams.TEAM_NAME_MAX_CHARS, sorted(crews), known=lambda: crews
        )
        assert len(teams.read_teams()[0].members) == teams.TEAM_MEMBERS_MAX
        # Pin the cap AT the current document (it still reads), then grow it by
        # one byte per name character: the write is refused with a coded error
        # and the document on disk is untouched.
        before = teams.teams_path().read_bytes()
        with patch.object(teams, "TEAMS_FILE_MAX_BYTES", len(before)):
            with pytest.raises(teams.TeamError) as exc:
                teams.update_team(
                    team.id, name="\u00e9" * teams.TEAM_NAME_MAX_CHARS, known=lambda: crews
                )
        assert exc.value.code == "teams_too_large"
        assert teams.teams_path().read_bytes() == before

    def test_member_names_are_display_names_not_identifiers(self):
        """A crew is whatever the roster lists: ``dr. eggbot`` (a space, a dot)
        and a name past the old 64-character identifier grammar are members;
        the bound is the display-name cap the roster itself applies."""
        long_ok = "n" * teams.MEMBER_NAME_MAX_CHARS
        crews = {"dr. eggbot", "雷达", long_ok}
        team = teams.create_team("Docs", sorted(crews), known=lambda: crews)
        assert set(team.members) == crews
        assert set(teams.read_teams()[0].members) == crews
        assert teams.MEMBER_NAME_MAX_CHARS == members_mod.MEMBER_NAME_MAX_CHARS

    def test_prune_unknown_drops_names_no_registry_holds(self):
        """A crew removed by a path that did not call drop_member (the CLI, a
        package prune) must never be ANSWERED as a member: the routes reconcile
        the document against the registry on every read."""
        team = teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)
        pruned = teams.prune_unknown([team], {RADAR})
        assert pruned[0].members == [RADAR]
        # The document itself is left alone: reconciliation is a read-side view.
        assert teams.read_teams()[0].members == [RADAR, FIXER]

    def test_a_member_the_reader_would_refuse_is_never_written(self):
        """The write side enforces the read side's member bounds BEFORE the
        registry lookup: a registry key past MEMBER_NAME_MAX_CHARS, or one
        UTF-8 cannot encode, is refused as invalid even when "known", so no
        write can leave a document the next read refuses whole."""
        long_name = "a" * (teams.MEMBER_NAME_MAX_CHARS + 1)
        for bad in (long_name, "ghost\ud800"):
            with pytest.raises(teams.TeamError) as exc:
                teams.create_team("Docs", [bad], known=lambda: KNOWN | {bad})
            assert exc.value.code == "invalid_members"
        assert teams.read_teams() == []

    def test_membership_deltas_never_write_a_stale_snapshot_back(self):
        """Two editors, one team. The second holds a snapshot taken BEFORE the
        first moved Fixer to Docs; sending its change as a delta touches only
        Scribe, so Fixer stays where the newer write put it -- a replace with
        the snapshot would have moved Fixer back."""
        triage = teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)
        docs = teams.create_team("Docs", [FIXER], known=lambda: KNOWN)
        teams.update_team(triage.id, add=[SCRIBE], known=lambda: KNOWN)
        stored = {t.id: t for t in teams.read_teams()}
        assert stored[triage.id].members == [RADAR, SCRIBE]
        assert stored[docs.id].members == [FIXER]
        # A removal need not name a registered crew (a deleted crew's stale entry).
        teams.update_team(triage.id, remove=["Ghost", RADAR], known=lambda: KNOWN)
        assert {t.id: t for t in teams.read_teams()}[triage.id].members == [SCRIBE]
        # Adding a crewmate moves it off its current team, as a replace would.
        teams.update_team(triage.id, add=[FIXER], known=lambda: KNOWN)
        stored = {t.id: t for t in teams.read_teams()}
        assert stored[triage.id].members == [SCRIBE, FIXER]
        assert stored[docs.id].members == []
        # The two shapes are exclusive, and an unknown addition is still refused.
        with pytest.raises(teams.TeamError) as exc:
            teams.update_team(triage.id, members=[RADAR], add=[SCRIBE], known=lambda: KNOWN)
        assert exc.value.code == "invalid_members"
        with pytest.raises(teams.TeamError) as exc:
            teams.update_team(triage.id, add=["Ghost"], known=lambda: KNOWN)
        assert exc.value.code == "unknown_member"

    def test_unknown_member_is_refused(self):
        with pytest.raises(teams.TeamError) as exc:
            teams.create_team("Docs", ["Ghost"], known=lambda: KNOWN)
        assert exc.value.code == "unknown_member"
        assert teams.read_teams() == []

    def test_a_crewmate_is_on_at_most_one_team(self):
        triage = teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)
        docs = teams.create_team("Docs", [FIXER, SCRIBE], known=lambda: KNOWN)
        stored = {t.id: t for t in teams.read_teams()}
        # Creating Docs with Fixer moved Fixer OUT of Issue triage in the same write.
        assert stored[triage.id].members == [RADAR]
        assert stored[docs.id].members == [FIXER, SCRIBE]
        # Re-membering a team moves the newcomers out of their current team too.
        teams.update_team(triage.id, members=[RADAR, SCRIBE], known=lambda: KNOWN)
        stored = {t.id: t for t in teams.read_teams()}
        assert stored[triage.id].members == [RADAR, SCRIBE]
        assert stored[docs.id].members == [FIXER]

    def test_duplicate_members_collapse_in_order(self):
        team = teams.create_team("Docs", [SCRIBE, RADAR, SCRIBE], known=lambda: KNOWN)
        assert team.members == [SCRIBE, RADAR]

    def test_update_renames_without_touching_members(self):
        team = teams.create_team("Docs", [SCRIBE], known=lambda: KNOWN)
        renamed = teams.update_team(team.id, name="Documentation", known=lambda: KNOWN)
        assert renamed.name == "Documentation"
        assert renamed.members == [SCRIBE]
        with pytest.raises(teams.TeamError) as exc:
            teams.update_team(team.id, known=lambda: KNOWN)
        assert exc.value.code == "nothing_to_update"

    def test_unknown_team_id(self):
        with pytest.raises(teams.TeamError) as exc:
            teams.update_team("deadbeef0000", name="x", known=lambda: KNOWN)
        assert exc.value.code == "team_not_found"
        with pytest.raises(teams.TeamError) as exc:
            teams.delete_team("deadbeef0000")
        assert exc.value.code == "team_not_found"

    def test_delete_removes_only_that_team(self):
        triage = teams.create_team("Issue triage", [RADAR], known=lambda: KNOWN)
        docs = teams.create_team("Docs", [SCRIBE], known=lambda: KNOWN)
        teams.delete_team(triage.id)
        assert [t.id for t in teams.read_teams()] == [docs.id]

    def test_drop_member_removes_a_deleted_crew(self):
        team = teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)
        assert teams.drop_member(FIXER) is True
        assert teams.read_teams()[0].members == [RADAR]
        assert teams.read_teams()[0].id == team.id
        # Nothing to drop: no write.
        assert teams.drop_member("Ghost") is False

    def test_document_carries_the_schema_version(self):
        teams.create_team("Docs", [SCRIBE], known=lambda: KNOWN)
        doc = json.loads(teams.teams_path().read_text(encoding="utf-8"))
        assert doc["version"] == teams.TEAMS_SCHEMA_VERSION
        # A document from a NEWER build is refused whole, never half-read.
        doc["version"] = teams.TEAMS_SCHEMA_VERSION + 1
        teams.teams_path().write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(teams.TeamsUnreadable):
            teams.read_teams()

    def test_drop_member_swallows_a_failed_rewrite(self):
        """The caller is the crew-delete path, whose config write has already
        committed; a disk-full on crew-teams/ must not turn that into a 500."""
        teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)

        def _boom(_teams):
            raise OSError("disk full")

        with patch.object(teams, "write_teams", _boom):
            assert teams.drop_member(FIXER) is False
        # Nothing was lost: the document still names both crewmates.
        assert teams.read_teams()[0].members == [RADAR, FIXER]

    def test_writers_hold_a_cross_process_file_lock(self):
        """``kirocrew agents delete`` writes from ANOTHER process, which the
        threading lock cannot reach: every read -> mutate -> rewrite also holds
        the advisory lock on the directory's lock file. A real second process
        holds it here, on every platform, and the store's write fails closed
        instead of racing; once the holder exits the same write lands."""
        import os
        import subprocess
        import sys

        teams.create_team("Issue triage", [RADAR], known=lambda: KNOWN)
        lock_path = teams.teams_path().parent / teams.LOCK_FILE_NAME
        assert lock_path.exists()
        holder_src = (
            "import sys\n"
            "from kiro_crew import platform_compat\n"
            "with platform_compat.open_lock_file(sys.argv[1]) as fd:\n"
            "    with platform_compat.file_lock(fd, exclusive=True, wait=True):\n"
            "        print('held', flush=True)\n"
            "        sys.stdin.readline()\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p
            for p in (os.path.dirname(os.path.dirname(teams.__file__)), env.get("PYTHONPATH", ""))
            if p
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_src, str(lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # The child opens the lock file with O_CREAT: its CWD must be under
            # tmp_path so a relative resolution can never touch the repository.
            cwd=lock_path.parent,
            env=env,
            text=True,
            encoding="utf-8",
        )
        try:
            assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
            with patch.object(teams, "LOCK_TIMEOUT_SECS", 0.5):
                with pytest.raises(OSError):
                    teams.create_team("Docs", [SCRIBE], known=lambda: KNOWN)
            # The refused write left the document as it was.
            assert [t.name for t in teams.read_teams()] == ["Issue triage"]
        finally:
            assert holder.stdin is not None
            holder.stdin.write("\n")
            holder.stdin.close()
            holder.wait(timeout=30)
        # Holder gone: the same write goes through.
        teams.create_team("Docs", [SCRIBE], known=lambda: KNOWN)
        assert [t.name for t in teams.read_teams()] == ["Issue triage", "Docs"]

    def test_release_name_purges_a_recreated_crews_stale_membership(self):
        """Every removal path drops best-effort, so a name may still sit on a
        team when a crew is created under it again; the create path releases
        it first, and the new crew starts on no team."""
        team = teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)
        assert teams.release_name(FIXER) is True
        assert teams.read_teams()[0].members == [RADAR]
        assert teams.read_teams()[0].id == team.id
        # Nothing to release: no write.
        assert teams.release_name("Ghost") is False

    def test_release_name_without_a_store_writes_no_document(self):
        """The common case -- no team was ever made -- answers False and writes
        no document; only the store directory and its lock file appear, because
        absence is decided under the lock (below)."""
        assert teams.release_name(FIXER) is False
        assert not teams.teams_path().exists()
        assert sorted(p.name for p in teams.teams_path().parent.iterdir()) == [teams.LOCK_FILE_NAME]

    def test_release_name_decides_absence_under_the_lock(self, monkeypatch):
        """A restore installing a document while a crew is being created must
        not slip between "no document" and the registration: the absence check
        runs inside the same lock the installer takes. Blind: a lock holder
        that installs the document BEFORE yielding must be seen by the purge."""
        store = teams.teams_path().parent
        real_lock = teams.document_lock

        @contextlib.contextmanager
        def _install_then_yield(directory=None):
            with real_lock(directory):
                if not teams.teams_path().exists():
                    teams._write_document(
                        teams.teams_path(),
                        [teams.Team(id="a" * 12, name="Issue triage", members=[FIXER])],
                    )
                yield

        monkeypatch.setattr(teams, "document_lock", _install_then_yield)
        # The document appears only once the lock is held; a pre-lock stat would
        # have answered "absent" and let FIXER inherit the membership.
        assert teams.release_name(FIXER) is True
        assert teams.read_teams()[0].members == []
        assert (store / teams.LOCK_FILE_NAME).exists()

    def test_release_name_does_not_read_an_unstatable_store_as_absent(self, monkeypatch):
        """Only a MISSING document is "no teams". A directory this process
        cannot stat into must refuse the create, not let the name inherit."""
        import pathlib

        real_stat = pathlib.Path.stat

        def _denied(self, *a, **kw):
            if self.name == teams.TEAMS_FILE_NAME:
                raise PermissionError(13, "Permission denied", str(self))
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "stat", _denied)
        with pytest.raises(PermissionError):
            teams.release_name(FIXER)
        with pytest.raises(teams.TeamsUnavailable):
            teams.release_for_create(FIXER)

    def test_release_name_propagates_a_failure_so_the_create_is_refused(self, monkeypatch):
        """Unlike drop_member this is NOT best-effort: registering a name while
        its stale membership cannot be purged is what would expose it."""
        teams.create_team("Issue triage", [RADAR, FIXER], known=lambda: KNOWN)

        def _boom(_teams):
            raise OSError("disk full")

        with patch.object(teams, "write_teams", _boom):
            with pytest.raises(OSError):
                teams.release_name(FIXER)
        assert teams.read_teams()[0].members == [RADAR, FIXER]
        teams.teams_path().write_text("{not json", encoding="utf-8")
        with pytest.raises(teams.TeamsUnreadable):
            teams.release_name(FIXER)

    def test_registry_is_read_inside_the_document_lock(self):
        """The ``known`` callable runs while the lock is held, so the registry
        snapshot a write validates against is the freshest one available."""
        seen: list[bool] = []

        def known() -> set[str]:
            seen.append(teams._WRITE_LOCK.locked())
            return KNOWN

        teams.create_team("Issue triage", [RADAR], known=known)
        assert seen == [True]

    def test_concurrent_writers_never_lose_an_update(self):
        """Every writer runs read -> mutate -> rewrite off-loop; without the
        store lock two interleaved creates would have the second drop the
        first's team from the document."""
        import threading

        errors: list[BaseException] = []

        def _create(i: int) -> None:
            try:
                teams.create_team(f"Team {i}", [], known=lambda: KNOWN)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert sorted(t.name for t in teams.read_teams()) == sorted(f"Team {i}" for i in range(12))

    def test_unparseable_file_raises_rather_than_reading_empty(self):
        path = teams.teams_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(teams.TeamsUnreadable):
            teams.read_teams()
        # A write path must not erase the unreadable document either.
        with pytest.raises(teams.TeamsUnreadable):
            teams.create_team("Docs", [], known=lambda: KNOWN)
        assert path.read_text(encoding="utf-8") == "{not json"

    def test_read_refuses_a_document_past_the_caps(self):
        """Bounds are enforced on what is RETAINED, not only on what is
        written: a restored or hand-edited document past a cap is refused
        whole rather than served past the limit."""
        path = teams.teams_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        def _write(doc: dict) -> None:
            path.write_text(json.dumps(doc), encoding="utf-8")

        ok = {"id": "aaaaaaaaaaaa", "name": "A", "members": [RADAR]}
        _write({"version": 1, "teams": [ok]})
        assert teams.read_teams()[0].name == "A"
        for bad in (
            {**ok, "id": "not-hex!"},
            # `$` alone would admit this; the id must fullmatch.
            {**ok, "id": "aaaaaaaaaaaa\n"},
            {**ok, "name": "x" * (teams.TEAM_NAME_MAX_CHARS + 1)},
            {**ok, "name": "   "},
            {**ok, "members": [f"m{i}" for i in range(teams.TEAM_MEMBERS_MAX + 1)]},
            {**ok, "members": ["y" * (teams.MEMBER_NAME_MAX_CHARS + 1)]},
            {**ok, "members": [""]},
            # Escaped lone surrogates: legal JSON, unencodable as UTF-8. Refused
            # on read so the next mutation cannot raise UnicodeEncodeError
            # mid-write on a document this module's writers never produce.
            {**ok, "name": "A\ud800"},
            {**ok, "members": [RADAR, "\udc00ghost"]},
        ):
            _write({"version": 1, "teams": [bad]})
            with pytest.raises(teams.TeamsUnreadable):
                teams.read_teams()
        _write(
            {
                "version": 1,
                "teams": [{**ok, "id": f"{i:012x}"} for i in range(teams.TEAMS_MAX + 1)],
            }
        )
        with pytest.raises(teams.TeamsUnreadable):
            teams.read_teams()

    def test_an_oversized_document_is_refused_without_being_read_whole(self, monkeypatch):
        """The size cap is enforced by a BOUNDED read: a file left at the path
        by a restore, however large, is refused after ``cap + 1`` bytes and is
        never allocated whole on the way to the refusal."""
        path = teams.teams_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        cap = 64
        path.write_bytes(b"[" + b" " * (cap * 8))
        bounds: list[int | None] = []
        real = teams.read_bytes_with_retry

        def bounded(target, *, max_bytes=None):
            bounds.append(max_bytes)
            out = real(target, max_bytes=max_bytes)
            assert len(out) <= cap + 1
            return out

        monkeypatch.setattr(teams, "read_bytes_with_retry", bounded)
        with patch.object(teams, "TEAMS_FILE_MAX_BYTES", cap):
            with pytest.raises(teams.TeamsUnreadable):
                teams.read_teams()
        assert bounds == [cap + 1]

    def test_a_failed_directory_sync_does_not_fail_a_committed_write(self, monkeypatch):
        """The rename has already published the document; reporting the
        parent-directory sync as a failed save would make the route answer
        500 and a retry create the team twice."""

        def _boom(path, *, best_effort=False):
            if not best_effort:
                raise OSError("EIO")

        monkeypatch.setattr(teams, "fsync_dir", _boom)
        team = teams.create_team("Docs", [SCRIBE], known=lambda: KNOWN)
        assert [t.id for t in teams.read_teams()] == [team.id]

    def test_foreign_document_keeps_first_team_for_a_doubled_member(self):
        path = teams.teams_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"teams": [{"id": "aaaaaaaaaaaa", "name": "A", "members": ["Radar"]},'
            ' {"id": "bbbbbbbbbbbb", "name": "B", "members": ["Radar", "Fixer"]}]}',
            encoding="utf-8",
        )
        stored = teams.read_teams()
        assert stored[0].members == [RADAR]
        assert stored[1].members == [FIXER]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers.teams import (
        api_teams_create,
        api_teams_delete,
        api_teams_list,
        api_teams_update,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app.router.add_get("/api/teams", api_teams_list)
    app.router.add_post("/api/teams", api_teams_create)
    app.router.add_put("/api/teams/{id}", api_teams_update)
    app.router.add_delete("/api/teams/{id}", api_teams_delete)
    return app


def _fake_config():
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agents = {name: KiroCrewAgentConfig(kiro_agent="kirocrew") for name in sorted(KNOWN)}
    return cfg


def _patched_config():
    return patch(
        "kiro_crew.dashboard.handlers.teams.KiroCrewConfig.load", return_value=_fake_config()
    )


def _as_owner():
    """The write routes are owner-only (``require_owner_dashboard_request``);
    the deny path is exercised by the repo-wide owner-gate invariant walk, so
    these tests patch the gate open to test the handlers' own contracts."""
    return patch(
        "kiro_crew.dashboard.handlers.teams.require_owner_dashboard_request",
        new=AsyncMock(return_value=None),
    )


class TestBackupCoverage:
    """The document rides every backup path: a snapshot component of its own, the
    dashboard export zip, and both import modes. A restore onto a replacement home
    that silently dropped every team is the failure these pin."""

    def test_the_store_is_a_snapshot_component_restored_as_a_whole_tree(self):
        from kiro_crew import snapshot as snap

        assert snap.COMPONENTS["crew-teams"].trees == (teams.TEAMS_DIR_NAME,)
        assert "crew-teams" in snap._WHOLE_TREE_COMPONENTS
        # The lock file is this host's runtime state and never rides, in either direction.
        assert snap._never_ships(f"bundle/{teams.TEAMS_DIR_NAME}/{teams.LOCK_FILE_NAME}")
        assert not snap._never_ships(f"bundle/{teams.TEAMS_DIR_NAME}/{teams.TEAMS_FILE_NAME}")
        root = teams.teams_path().parent
        ignore = snap._staging_ignore(teams.TEAMS_DIR_NAME, root)
        assert ignore(str(root), [teams.LOCK_FILE_NAME, teams.TEAMS_FILE_NAME]) == {
            teams.LOCK_FILE_NAME
        }

    def test_a_restore_refuses_exactly_what_the_live_read_refuses(self, tmp_path):
        """The restore validates the document with the store's OWN reader, so a
        file the shape check would admit but `read_teams` refuses -- a bad id, an
        over-cap name, a newer version -- never installs. Absent destination on
        merge installs and is checked; a present destination is left alone."""
        from kiro_crew import snapshot as snap

        bundle = tmp_path / "bundle"
        (bundle / teams.TEAMS_DIR_NAME).mkdir(parents=True)
        doc = bundle / teams.TEAMS_DIR_NAME / teams.TEAMS_FILE_NAME
        ok = {"id": "aaaaaaaaaaaa", "name": "A", "members": [RADAR]}
        for bad in (
            [ok],
            {"version": 1, "teams": "not a list"},
            {"version": 1, "teams": [{**ok, "id": "not-hex!"}]},
            {"version": 1, "teams": [{**ok, "name": "x" * (teams.TEAM_NAME_MAX_CHARS + 1)}]},
            {"version": 2, "teams": [ok]},
        ):
            doc.write_text(json.dumps(bad), encoding="utf-8")
            with pytest.raises(snap.SourceComponentUnsound):
                snap._refuse_corrupt_source_databases(bundle, ["crew-teams"], mc_for_merge=None)
            with pytest.raises(snap.SourceComponentUnsound):
                snap._refuse_corrupt_source_databases(
                    bundle, ["crew-teams"], mc_for_merge=tmp_path / "empty-home"
                )
        doc.write_text(json.dumps({"version": 1, "teams": [ok]}), encoding="utf-8")
        snap._refuse_corrupt_source_databases(bundle, ["crew-teams"], mc_for_merge=None)
        # A merge onto a home that already has a document leaves it alone, so the
        # bundle's copy is not what reaches the reader and is not checked.
        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        doc.write_text("garbage", encoding="utf-8")
        snap._refuse_corrupt_source_databases(
            bundle, ["crew-teams"], mc_for_merge=teams.teams_path().parent.parent
        )

    def test_a_restore_refuses_a_directory_or_link_at_the_documents_name(self, tmp_path):
        """A crafted bundle with `crew-teams/teams.json/` (a directory) or a link at
        that name is refused in every mode, never read as "absent" and installed over
        the live document."""
        from kiro_crew import snapshot as snap

        bundle = tmp_path / "bundle"
        (bundle / teams.TEAMS_DIR_NAME / teams.TEAMS_FILE_NAME).mkdir(parents=True)
        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        home = teams.teams_path().parent.parent
        for mc_for_merge in (None, home, tmp_path / "empty-home"):
            with pytest.raises(snap.SourceComponentUnsound):
                snap._refuse_corrupt_source_databases(
                    bundle, ["crew-teams"], mc_for_merge=mc_for_merge
                )

    def test_a_removal_is_durable_before_the_lock_is_released(self):
        """Unlinking the document is not enough: without a directory sync a
        power loss before the metadata flush brings the removed document back.
        Blind: the sync must target the store directory and run while the lock
        is still held, exactly as the two sibling writers do."""
        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        store = teams.teams_path().parent
        held: list[bool] = []
        synced: list[Path] = []
        real_lock = teams.document_lock
        real_sync = teams.fsync_dir

        @contextlib.contextmanager
        def observing_lock(directory=None):
            held.append(True)
            with real_lock(directory):
                yield
            held.pop()

        def observing_sync(path, **kw):
            synced.append((Path(path), bool(held)))
            return real_sync(path, **kw)

        with (
            patch.object(teams, "document_lock", observing_lock),
            patch.object(teams, "fsync_dir", observing_sync),
        ):
            assert teams.remove_document(store) is True
        assert not teams.teams_path().exists()
        assert (store, True) in synced, synced

    def test_install_and_remove_hold_the_store_lock_and_keep_the_lock_file(self, tmp_path):
        """The restore writer takes the same lock every writer takes and replaces
        ONLY the document: the directory and its lock file survive, so a writer
        already holding the lock still holds THE lock, not an orphaned file."""
        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        store = teams.teams_path().parent
        lock_before = (store / teams.LOCK_FILE_NAME).stat().st_ino
        dir_before = store.stat().st_ino
        src = tmp_path / teams.TEAMS_FILE_NAME
        src.write_text(
            json.dumps(
                {"version": 1, "teams": [{"id": "bbbbbbbbbbbb", "name": "Triage", "members": []}]}
            ),
            encoding="utf-8",
        )
        locked: list[Path] = []
        real_lock = teams.document_lock

        @contextlib.contextmanager
        def recording_lock(directory=None):
            locked.append(directory)
            with real_lock(directory):
                yield

        with patch.object(teams, "document_lock", recording_lock):
            # A present document is left alone when only_if_absent.
            assert teams.install_document(src, store, only_if_absent=True) is False
            assert teams.read_teams()[0].name == "Docs"
            assert teams.install_document(src, store) is True
            assert teams.read_teams()[0].name == "Triage"
            assert teams.remove_document(store) is True
            assert teams.read_teams() == []
            assert teams.remove_document(store) is False
        assert locked == [store] * 4
        assert (store / teams.LOCK_FILE_NAME).stat().st_ino == lock_before
        assert store.stat().st_ino == dir_before
        # A document the reader refuses is refused here too, and installs nothing.
        src.write_text("[]", encoding="utf-8")
        with pytest.raises(teams.TeamsUnreadable):
            teams.install_document(src, store)
        assert not teams.teams_path().exists()

    def test_a_document_nested_past_the_stack_is_unreadable_not_a_crash(self):
        path = teams.teams_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
        with pytest.raises(teams.TeamsUnreadable):
            teams.read_teams()

    def test_the_rollback_copy_is_taken_inside_the_lock_that_replaces_the_document(self, tmp_path):
        """The installer saves the live document and replaces it in ONE lock hold, so
        a write cannot land between the copy and the install and vanish from both."""
        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        store = teams.teams_path().parent
        src = tmp_path / teams.TEAMS_FILE_NAME
        src.write_text(json.dumps({"version": 1, "teams": []}), encoding="utf-8")
        held: list[bool] = []
        saved: list[str] = []
        real_lock = teams.document_lock

        @contextlib.contextmanager
        def recording_lock(directory=None):
            with real_lock(directory):
                held.append(True)
                try:
                    yield
                finally:
                    held.append(False)

        def save(live: Path) -> None:
            saved.append(live.read_text(encoding="utf-8"))
            assert held and held[-1] is True, "the copy ran outside the lock hold"

        with patch.object(teams, "document_lock", recording_lock):
            assert teams.install_document(src, store, save_existing=save) is True
            assert teams.remove_document(store, save_existing=save) is True
            # Nothing to save when the document is absent; the callback is not called.
            assert teams.remove_document(store, save_existing=save) is False
        assert len(saved) == 2 and "Docs" in saved[0] and '"teams": []' in saved[1]
        assert held == [True, False] * 3

    def test_rollback_puts_the_saved_document_back_verbatim_under_the_lock(self, tmp_path):
        """Recovery restores the operator's pre-restore bytes as they were -- even a
        document the reader refuses -- and never raises anything the rollback loop
        does not name per target."""
        store = teams.teams_path().parent
        store.mkdir(parents=True, exist_ok=True)
        teams.teams_path().write_text("{}", encoding="utf-8")
        saved = tmp_path / "saved.json"
        saved.write_text("not even json", encoding="utf-8")
        locked: list[Path] = []
        real_lock = teams.document_lock

        @contextlib.contextmanager
        def recording_lock(directory=None):
            locked.append(directory)
            with real_lock(directory):
                yield

        with patch.object(teams, "document_lock", recording_lock):
            teams.restore_document(saved, store)
        assert locked == [store]
        assert teams.teams_path().read_text(encoding="utf-8") == "not even json"

    def test_an_irregular_entry_at_the_document_name_is_refused_not_read_as_absent(self, tmp_path):
        """A directory (or a link) where the document belongs is not "no document":
        a replace must fail on it and roll back, never report success with the
        entry still there and every team route still refusing."""
        store = teams.teams_path().parent
        store.mkdir(parents=True, exist_ok=True)
        (store / teams.TEAMS_FILE_NAME).mkdir()
        src = tmp_path / teams.TEAMS_FILE_NAME
        src.write_text(json.dumps({"version": 1, "teams": []}), encoding="utf-8")
        saved: list[Path] = []
        with pytest.raises(OSError):
            teams.remove_document(store, save_existing=saved.append)
        with pytest.raises(OSError):
            teams.install_document(src, store, only_if_absent=True)
        with pytest.raises(OSError):
            teams.install_document(src, store, save_existing=saved.append)
        assert saved == [] and (store / teams.TEAMS_FILE_NAME).is_dir()
        # A dangling link is an entry too, not an absence.
        (store / teams.TEAMS_FILE_NAME).rmdir()
        (store / teams.TEAMS_FILE_NAME).symlink_to(tmp_path / "nowhere.json")
        with pytest.raises(OSError):
            teams.remove_document(store)
        with pytest.raises(OSError):
            teams.install_document(src, store)
        assert (store / teams.TEAMS_FILE_NAME).is_symlink()

    def test_rollback_streams_the_saved_copy_instead_of_reading_it_whole(self, tmp_path):
        """The saved copy can be any size the live store held; recovery copies it in
        chunks into a temp file beside the document and publishes by rename, so an
        oversized document cannot make the rollback allocate it whole."""
        store = teams.teams_path().parent
        store.mkdir(parents=True, exist_ok=True)
        teams.teams_path().write_text("{}", encoding="utf-8")
        saved = tmp_path / "saved.json"
        # Sized from the platform's copy buffer (1 MiB on Windows, 64 KiB
        # elsewhere) so the default chunk is strictly smaller than the file
        # on every runner -- a fixed 512 KiB was ONE chunk on Windows.
        payload = b"x" * (4 * shutil.COPY_BUFSIZE)
        saved.write_bytes(payload)
        chunks: list[int] = []
        real_copy = shutil.copyfileobj

        def chunked_copy(src, dst, length=shutil.COPY_BUFSIZE):
            chunks.append(length)
            return real_copy(src, dst, 64 * 1024)

        def whole_read(self: Path) -> bytes:
            pytest.fail(f"{self.name} was read whole")

        with (
            patch.object(teams.shutil, "copyfileobj", chunked_copy),
            patch.object(teams.Path, "read_bytes", whole_read),
        ):
            teams.restore_document(saved, store)
        assert chunks and all(0 < c < len(payload) for c in chunks)
        assert teams.teams_path().read_bytes() == payload
        assert sorted(p.name for p in store.iterdir()) == sorted(
            [teams.LOCK_FILE_NAME, teams.TEAMS_FILE_NAME]
        )

    def test_a_rollback_copy_that_fails_midway_is_never_published(self, tmp_path, monkeypatch):
        """The rollback copy is staged beside its name and renamed in only when the
        copy completed, so a failed copy leaves NO file where recovery reads -- and
        the live document, which the install never reached, stays as it was."""
        from kiro_crew import pinned_fs
        from kiro_crew import snapshot as snap

        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        store = teams.teams_path().parent
        backup = tmp_path / "rollback"
        backup.mkdir()
        src = tmp_path / teams.TEAMS_FILE_NAME
        src.write_text(json.dumps({"version": 1, "teams": []}), encoding="utf-8")

        def torn_copy(source, target, **_kw):
            Path(target).write_bytes(b'{"version": 1, "tea')
            raise OSError("disk full")

        monkeypatch.setattr(pinned_fs, "copy_file_pinned", torn_copy)
        save = snap._save_locked_document_to(backup, "crew-teams/teams.json")
        with pytest.raises(OSError):
            teams.install_document(src, store, save_existing=save)
        assert not (backup / "crew-teams" / "teams.json").exists()
        assert list((backup / "crew-teams").iterdir()) == []
        assert teams.read_teams()[0].name == "Docs"

    def test_replace_restores_the_document_without_swapping_the_directory(self, tmp_path):
        """The snapshot's replace mutation goes through the locked installer: the
        document changes, the directory and lock file are the same inodes, and a
        bundle tree that carries no document removes the live one."""
        from kiro_crew import snapshot as snap

        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        store = teams.teams_path().parent
        home = store.parent
        lock_before = (store / teams.LOCK_FILE_NAME).stat().st_ino
        dir_before = store.stat().st_ino
        bundle = tmp_path / "bundle"
        (bundle / teams.TEAMS_DIR_NAME).mkdir(parents=True)
        (bundle / teams.TEAMS_DIR_NAME / teams.TEAMS_FILE_NAME).write_text(
            json.dumps(
                {"version": 1, "teams": [{"id": "bbbbbbbbbbbb", "name": "Triage", "members": []}]}
            ),
            encoding="utf-8",
        )
        installed: set[str] = set()
        rollback = tmp_path / "rollback"
        rollback.mkdir()
        snap._do_replace_mutations(bundle, home, rollback, ["crew-teams"], [], installed)
        assert installed == {"crew-teams/teams.json"}
        assert teams.read_teams()[0].name == "Triage"
        # The rollback copy is the pre-restore document, saved by the installer.
        assert "Docs" in (rollback / "crew-teams" / "teams.json").read_text(encoding="utf-8")
        assert (store / teams.LOCK_FILE_NAME).stat().st_ino == lock_before
        assert store.stat().st_ino == dir_before
        (bundle / teams.TEAMS_DIR_NAME / teams.TEAMS_FILE_NAME).unlink()
        rollback2 = tmp_path / "rollback2"
        rollback2.mkdir()
        snap._do_replace_mutations(bundle, home, rollback2, ["crew-teams"], [], set())
        assert teams.read_teams() == []
        assert "Triage" in (rollback2 / "crew-teams" / "teams.json").read_text(encoding="utf-8")
        assert store.stat().st_ino == dir_before

    def test_replace_from_a_bundle_with_no_team_tree_removes_the_live_document(self, tmp_path):
        """A home that never made a team exports NO `crew-teams/` at all -- the ordinary
        shape, not a legacy one. A full replace from it must leave the destination
        with no teams either: the old document goes, is saved for rollback, and the
        directory and lock file stay put."""
        from kiro_crew import snapshot as snap

        teams.create_team("Docs", [RADAR], known=lambda: KNOWN)
        store = teams.teams_path().parent
        home = store.parent
        lock_before = (store / teams.LOCK_FILE_NAME).stat().st_ino
        dir_before = store.stat().st_ino
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        assert not (bundle / teams.TEAMS_DIR_NAME).exists()
        installed: set[str] = set()
        rollback = tmp_path / "rollback"
        rollback.mkdir()
        snap._do_replace_mutations(bundle, home, rollback, ["crew-teams"], [], installed)
        assert teams.read_teams() == []
        assert not (store / teams.TEAMS_FILE_NAME).exists()
        assert installed == {"crew-teams/teams.json"}
        assert "Docs" in (rollback / "crew-teams" / "teams.json").read_text(encoding="utf-8")
        assert (store / teams.LOCK_FILE_NAME).stat().st_ino == lock_before
        assert store.stat().st_ino == dir_before

    def test_a_replace_import_refuses_a_document_the_reader_would(self, tmp_path):
        """The dashboard import's replace mode validates `crew-teams` before
        `_do_replace` moves anything -- the same reader, the same refusal."""
        import zipfile

        from kiro_crew import portability

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("kirocrew-export-x/config.json", json.dumps({"agents": {}}))
            zf.writestr(
                f"kirocrew-export-x/{teams.TEAMS_DIR_NAME}/{teams.TEAMS_FILE_NAME}",
                json.dumps({"version": 1, "teams": [{"id": "bad", "name": "A", "members": []}]}),
            )
        home = teams.teams_path().parent.parent
        zip_path = tmp_path / "import.zip"
        zip_path.write_bytes(buf.getvalue())
        with patch.object(portability, "_mc_dir", return_value=home):
            with pytest.raises(Exception) as exc:
                portability.apply_import_zip(zip_path, mode="replace")
        assert "crew-teams" in str(exc.value)
        assert not teams.teams_path().exists()

    def test_the_export_zip_carries_the_document_and_not_the_lock(self):
        import zipfile

        from kiro_crew import portability

        teams.create_team("Issue triage", [RADAR], known=lambda: KNOWN)
        home = teams.teams_path().parent.parent
        assert (home / teams.TEAMS_DIR_NAME / teams.LOCK_FILE_NAME).exists()
        for d in ("workspace", "plan_memory", "skills"):
            (home / d).mkdir(exist_ok=True)
        with patch.object(portability, "_mc_dir", return_value=home):
            zip_bytes, manifest = portability.create_export_zip()
        names = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
        tails = {n.split("/", 1)[1] for n in names if "/" in n}
        assert f"{teams.TEAMS_DIR_NAME}/{teams.TEAMS_FILE_NAME}" in tails
        assert f"{teams.TEAMS_DIR_NAME}/{teams.LOCK_FILE_NAME}" not in tails
        assert manifest["contents"]["crew-teams/teams.json"] > 0


class TestTeamsRoutes:
    @pytest.mark.asyncio
    async def test_list_is_empty_before_any_team(self):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/teams")
            assert resp.status == 200
            assert await resp.json() == {"teams": []}

    @pytest.mark.asyncio
    async def test_create_update_delete_round_trip(self):
        with _as_owner(), _patched_config():
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post(
                    "/api/teams", json={"name": "Issue triage", "members": [RADAR, FIXER]}
                )
                assert resp.status == 201
                team = (await resp.json())["team"]
                assert team["members"] == [RADAR, FIXER]

                resp = await client.put(
                    f"/api/teams/{team['id']}", json={"name": "Triage", "members": [RADAR]}
                )
                assert resp.status == 200
                assert (await resp.json())["team"] == {
                    "id": team["id"],
                    "name": "Triage",
                    "members": [RADAR],
                }

                resp = await client.get("/api/teams")
                assert (await resp.json())["teams"][0]["name"] == "Triage"

                resp = await client.delete(f"/api/teams/{team['id']}")
                assert resp.status == 200
                resp = await client.get("/api/teams")
                assert await resp.json() == {"teams": []}

    @pytest.mark.asyncio
    async def test_registry_view_admits_roster_names_and_refuses_unsafe_ones(self):
        """``_known_crews`` is the roster's filter: a free-form display name is
        a known crew; a stored name the roster hides (credential-shaped, or one
        failing the display-name rule) is ``unknown_member`` on every write."""
        from kiro_crew.config.loader import KiroCrewConfig

        eggbot, hidden = "dr. eggbot", "AKIAIOSFODNN7EXAMPLE"
        cfg = KiroCrewConfig()
        cfg.agents = {
            name: KiroCrewAgentConfig(kiro_agent="kirocrew") for name in (eggbot, hidden, RADAR)
        }
        patched = patch("kiro_crew.dashboard.handlers.teams.KiroCrewConfig.load", return_value=cfg)
        with _as_owner(), patched:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post(
                    "/api/teams", json={"name": "Lab", "members": [eggbot, RADAR]}
                )
                assert resp.status == 201, await resp.text()
                assert (await resp.json())["team"]["members"] == [eggbot, RADAR]
                resp = await client.post("/api/teams", json={"name": "Vault", "members": [hidden]})
                assert resp.status == 400, await resp.text()
                assert (await resp.json())["code"] == "unknown_member"

    @pytest.mark.asyncio
    async def test_an_owner_write_records_the_allow_in_sel(self):
        """The shared gate audits only a denial; the owner's allow on a team
        write is a permission decision too and is recorded (non-fatally)."""
        with _as_owner(), _patched_config(), patch("kiro_crew.sel.sel") as mock_sel:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/teams", json={"name": "Docs", "members": [RADAR]})
                assert resp.status == 201
                team = (await resp.json())["team"]
                resp = await client.put(f"/api/teams/{team['id']}", json={"add": [FIXER]})
                assert resp.status == 200
                assert (await resp.json())["team"]["members"] == [RADAR, FIXER]
                resp = await client.put(f"/api/teams/{team['id']}", json={"remove": [RADAR]})
                assert (await resp.json())["team"]["members"] == [FIXER]
        outcomes = [c.kwargs for c in mock_sel.return_value.log_api_access.call_args_list]
        assert len(outcomes) == 3
        assert {o["outcome"] for o in outcomes} == {"allowed"}
        assert {o["operation"] for o in outcomes} == {"teams.write"}
        assert {o["source"] for o in outcomes} == {"dashboard"}

    @pytest.mark.asyncio
    async def test_list_never_names_a_crew_the_registry_lacks(self):
        teams.create_team("Issue triage", [RADAR, "Ghost"], known=lambda: KNOWN | {"Ghost"})
        with _patched_config():
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/teams")
                assert resp.status == 200
                assert (await resp.json())["teams"][0]["members"] == [RADAR]

    @pytest.mark.asyncio
    async def test_unknown_member_is_400_with_code(self):
        with _as_owner(), _patched_config():
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/teams", json={"name": "Docs", "members": ["Ghost"]})
                assert resp.status == 400
                assert (await resp.json())["code"] == "unknown_member"

    @pytest.mark.asyncio
    async def test_bad_bodies_are_coded_400s(self):
        with _as_owner(), _patched_config():
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/teams", data="not json")
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_json"
                resp = await client.post("/api/teams", json=["a", "list"])
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_json"
                resp = await client.post("/api/teams", json={"members": []})
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_team_name"
                resp = await client.put("/api/teams/not-hex!", json={"name": "x"})
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_team_id"
                # Hex but not the store's 12-char spelling: malformed, not a miss.
                for bad in ("abc", "deadbeef00001", "DEADBEEF0000"):
                    resp = await client.put(f"/api/teams/{bad}", json={"name": "x"})
                    assert resp.status == 400, bad
                    assert (await resp.json())["code"] == "invalid_team_id"

    @pytest.mark.asyncio
    async def test_unknown_team_is_404(self):
        with _as_owner(), _patched_config():
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.put("/api/teams/deadbeef0000", json={"name": "x"})
                assert resp.status == 404
                assert (await resp.json())["code"] == "team_not_found"
                resp = await client.delete("/api/teams/deadbeef0000")
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_app_callers_are_denied_with_404(self):
        teams.create_team("Docs", [SCRIBE], known=lambda: KNOWN)
        headers = {"X-Test-App": "some-app"}
        with _as_owner(), _patched_config():
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/teams", headers=headers)
                assert resp.status == 404
                assert "Docs" not in await resp.text()
                resp = await client.post(
                    "/api/teams", json={"name": "X", "members": []}, headers=headers
                )
                assert resp.status == 404
        assert len(teams.read_teams()) == 1

    @pytest.mark.asyncio
    async def test_non_owner_write_is_refused_before_validation(self):
        # A fresh Response per call: an aiohttp response can be sent once, and
        # returning one prepared object to two requests hangs the second.
        def _refuse(*_a, **_k):
            return web.json_response({"error": "forbidden", "code": "owner_only"}, status=403)

        with (
            patch(
                "kiro_crew.dashboard.handlers.teams.require_owner_dashboard_request",
                new=AsyncMock(side_effect=_refuse),
            ),
            _patched_config(),
        ):
            async with TestClient(TestServer(_make_app())) as client:
                # An INVALID body still gets the gate's answer, never a 400 that
                # tells a non-owner what would have validated.
                resp = await client.post("/api/teams", data="not json")
                assert resp.status == 403
                resp = await client.delete("/api/teams/not-hex!")
                assert resp.status == 403
        assert teams.read_teams() == []

    @pytest.mark.asyncio
    async def test_unreadable_document_is_500_not_empty_list(self):
        path = teams.teams_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with _as_owner(), _patched_config():
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/teams")
                assert resp.status == 500
                assert (await resp.json())["code"] == "teams_unreadable"
                resp = await client.post("/api/teams", json={"name": "X", "members": []})
                assert resp.status == 500
        assert path.read_text(encoding="utf-8") == "{not json"
