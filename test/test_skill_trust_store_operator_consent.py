"""How the operator grants and revokes project-skill consent.

Covers the dashboard trust endpoints and their owner gate, the project a requesting
chat resolves to, the reviewed-key confirmation that ties consent to the directory the
operator saw, and the workspace skill catalog that offers rows for consent. It also
pins how grant, revoke, listing and the enforcement read behave when the store
cannot be locked or written, and that a revoke still lands when its audit fails.
The ``project`` and ``_isolated_home`` fixtures live in ``skill_trust_store_helpers.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from skill_trust_store_helpers import (  # noqa: F401 -- fixtures register by import or by name
    _isolated_home,
    project_fixture,
)

from kiro_crew import skill_trust

pytestmark = pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="project skills require no-follow directory-descriptor traversal",
)


class TestRequestingSlotProject:
    """The catalog, the trust read and the GRANT must resolve as the loader does."""

    @staticmethod
    def _state(**slot_projects):
        """A stand-in for DashboardState carrying just the slots mapping."""

        class _Slot:
            def __init__(self, project):
                self.project = project

        class _State:
            def __init__(self, mapping):
                self._slots = {name: _Slot(proj) for name, proj in mapping.items()}

        return _State(slot_projects)

    def test_an_unbound_chat_resolves_to_no_project(self, tmp_path):
        """The case that leaked consent: this chat has none, another chat has P.

        `active_project_dir` answers P here. The loader, reading
        ``slot.project or None``, answers None -- so a grant keyed on the former
        records consent for a directory this chat will never load from, and the
        catalog advertises skills whose $token expands to nothing.
        """
        from kiro_crew.dashboard.handlers._shared import (
            active_project_dir,
            requesting_slot_project,
        )

        project_p = tmp_path / "P"
        project_p.mkdir()
        state = self._state(**{"chat-1": "", "chat-2": str(project_p)})

        # The permissive helper still falls back -- unchanged, other surfaces rely on it.
        assert active_project_dir(state, "dashboard:chat-1") == project_p
        # The strict one refuses to answer for a chat that has no project.
        assert requesting_slot_project(state, "dashboard:chat-1") is None

    def test_a_bound_chat_resolves_to_its_own_project(self, tmp_path):
        from kiro_crew.dashboard.handlers._shared import requesting_slot_project

        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()
        state = self._state(**{"chat-1": str(a), "chat-2": str(b)})

        assert requesting_slot_project(state, "dashboard:chat-1") == a
        assert requesting_slot_project(state, "dashboard:chat-2") == b

    def test_no_session_key_resolves_to_no_project(self, tmp_path):
        """Without a key there is no requesting chat, so there is no answer."""
        from kiro_crew.dashboard.handlers._shared import requesting_slot_project

        project_p = tmp_path / "P"
        project_p.mkdir()
        state = self._state(**{"chat-1": str(project_p)})

        assert requesting_slot_project(state, "") is None
        assert requesting_slot_project(state, "dashboard:nonexistent") is None


class TestRevokeIsNotBlockedByItsAudit:
    """A revoke is a de-escalation: an unwritable audit must not preserve trust."""

    def test_an_audit_failure_still_revokes_and_does_not_raise(self, project, monkeypatch):
        """The direction of this decision is deliberate; see revoke_project_trust.

        Auditing first with critical=True would RAISE on an unwritable SEL and
        leave the grant in place -- so anyone able to make the SEL unwritable
        (fill the disk, chmod the log dir) could veto every revocation. The repo
        made the same call in safety_override.deactivate. Fail closed on
        escalation, fail open on de-escalation.

        Also pins the caller-facing half: the failure must not escape. An escaping
        failure surfaces as a 500 telling the operator the revoke had failed when
        it had durably succeeded -- and the retry then reports "nothing was
        revoked" while skipping the audit for good.
        """

        class _Boom:
            def log_governance_decision(self, **_kwargs):
                raise OSError("SEL directory is read-only")

        skill_trust.grant_project_trust(project)
        assert skill_trust.is_project_trusted(project) is True

        monkeypatch.setattr(skill_trust, "sel", lambda: _Boom())

        # Must not raise, and must report the revoke it actually performed.
        assert skill_trust.revoke_project_trust(project) is True

        skill_trust.reset_cache_for_tests()
        assert skill_trust.is_project_trusted(project) is False, (
            "an audit failure left trust in place -- a read-only SEL must not "
            "become a veto over revocation"
        )

    def test_a_successful_revoke_still_audits(self, project, monkeypatch):
        """Containment must not become 'never audits'."""
        calls = []

        class _Recorder:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        skill_trust.grant_project_trust(project)
        monkeypatch.setattr(skill_trust, "sel", lambda: _Recorder())

        assert skill_trust.revoke_project_trust(project) is True
        assert [c["outcome"] for c in calls] == ["denied"]
        assert calls[0]["critical"] is True

    def test_a_failed_revoke_audit_does_not_log_the_project_path(
        self, project, monkeypatch, caplog
    ):
        import logging

        class _Boom:
            def log_governance_decision(self, **_kwargs):
                raise OSError("SEL is read-only")

        skill_trust.grant_project_trust(project)
        monkeypatch.setattr(skill_trust, "sel", lambda: _Boom())

        with caplog.at_level(logging.ERROR, logger="kiro_crew.skill_trust"):
            assert skill_trust.revoke_project_trust(project) is True

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert str(project) not in messages


class TestLockFailuresAreFailClosed:
    """A store we cannot LOCK is a store we cannot trust to round-trip.

    `_locked_store` fails before any store I/O when the trust dir is not creatable,
    the lock file is not openable (read-only filesystem, permissions), or the lock
    call itself fails. Those must not escape as raw OSError: otherwise the
    read-only listing 500s a settings page it promises to degrade, and the
    grant/revoke handlers reach aiohttp unhandled instead of returning their 409.
    """

    def test_listing_degrades_when_the_store_cannot_be_locked(self, project, monkeypatch):
        from kiro_crew import skill_trust as st

        skill_trust.grant_project_trust(project)
        assert skill_trust.list_trusted_projects(), "precondition: a grant is listed"

        def boom(*_a, **_kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(st.Path, "touch", boom)

        # Degrades to an empty list, never raises: this powers a settings page.
        assert skill_trust.list_trusted_projects() == []

    def test_a_grant_refuses_when_the_store_cannot_be_locked(self, project, monkeypatch):
        from kiro_crew import skill_trust as st

        def boom(*_a, **_kw):
            raise OSError("permission denied")

        monkeypatch.setattr(st.Path, "touch", boom)

        # TrustStoreUnreadable, which the handler already turns into a 409 with a
        # machine-readable code -- not a bare OSError 500.
        with pytest.raises(st.TrustStoreUnreadable):
            skill_trust.grant_project_trust(project)

    @pytest.mark.parametrize("operation", ["grant", "revoke"])
    def test_mutators_normalize_store_write_failures(
        self, project, tmp_path, monkeypatch, operation
    ):
        from kiro_crew import skill_trust as st

        if operation == "revoke":
            skill_trust.grant_project_trust(project)
            target = project
        else:
            target = tmp_path / "new-project"
            target.mkdir()

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(st, "atomic_write", boom)

        with pytest.raises(st.TrustStoreUnreadable, match="is not writable: disk full"):
            if operation == "grant":
                skill_trust.grant_project_trust(target)
            else:
                skill_trust.revoke_project_trust(target)

    def test_enforcement_still_fails_closed_without_the_lock(self, project, monkeypatch):
        """The reader never takes the lock, and must keep answering.

        `trusted_keys` is the path that decides whether a project's skills load. It
        reads directly and fails closed on OSError, so a lock-path failure must not
        turn "may this load" into an exception on a hot path.
        """
        from kiro_crew import skill_trust as st

        skill_trust.grant_project_trust(project)
        skill_trust.reset_cache_for_tests()

        def boom(*_a, **_kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(st.Path, "touch", boom)

        # Answers without raising; the grant is still readable because the
        # enforcement path does not need the lock.
        assert skill_trust.is_project_trusted(project) is True


class TestCatalogOnlyOffersLoadableWorkspaceSkills:
    """A listed workspace row must be one the loader can actually serve."""

    def test_an_escaped_skill_is_not_listed_as_trusted(self, project, tmp_path, monkeypatch):
        """Project catalog assembly never routes through the unconfined scanner."""
        from kiro_crew.dashboard.handlers import _shared
        from kiro_crew.dashboard.handlers._shared import collect_skills_blocking
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside" / "escaped"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(
            "---\nname: escaped\ndescription: ESCAPED ROW\n---\n\nbody\n",
            encoding="utf-8",
        )
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        try:
            (skills_root / "escaped").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # A genuine in-project skill, to prove the filter is not simply dropping all.
        real = skills_root / "genuine"
        real.mkdir()
        (real / "SKILL.md").write_text(
            "---\nname: genuine\ndescription: GENUINE ROW\n---\n\nbody\n",
            encoding="utf-8",
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        legacy_list = _shared.list_kiro_skills

        def list_global_kiro_skills_only(project_dir=None):
            assert project_dir is None, "project rows must use confined loader enumeration"
            return legacy_list(project_dir)

        monkeypatch.setattr(_shared, "list_kiro_skills", list_global_kiro_skills_only)

        rows = collect_skills_blocking(loader, [], project)
        workspace = {
            str(r.get("key", "")).split("/", 1)[-1]
            for r in rows
            if r.get("source") == "kiro-workspace"
        }

        assert "genuine" in workspace, workspace
        assert "escaped" not in workspace, (
            "an unloadable row was offered; selecting it expands to nothing and no "
            "grant can fix it"
        )
        assert "ESCAPED ROW" not in str(rows)

    def test_a_genuine_workspace_row_is_still_marked_trusted(self, project, tmp_path):
        """The filter must not cost the marking it exists to protect."""
        from kiro_crew.dashboard.handlers._shared import collect_skills_blocking
        from kiro_crew.skills import SkillsLoader

        skills_root = project / ".kiro" / "skills" / "genuine"
        skills_root.mkdir(parents=True)
        (skills_root / "SKILL.md").write_text(
            "---\nname: genuine\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        rows = collect_skills_blocking(loader, [], project)
        row = next(r for r in rows if str(r.get("key", "")).endswith("/genuine"))
        assert row.get("trusted") is True, row

    def test_a_genuine_untrusted_workspace_row_is_offered_for_consent(self, project, tmp_path):
        from kiro_crew.dashboard.handlers._shared import collect_skills_blocking
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "genuine"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: genuine\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        rows = collect_skills_blocking(loader, [], project)
        row = next(r for r in rows if str(r.get("key", "")).endswith("/genuine"))

        assert row.get("trusted") is False, row

    def test_a_workspace_row_shadowed_by_a_global_skill_is_not_offered(self, project, tmp_path):
        from kiro_crew.dashboard.handlers._shared import collect_skills_blocking
        from kiro_crew.skills import SkillsLoader

        project_skill = project / ".kiro" / "skills" / "same-name"
        project_skill.mkdir(parents=True)
        (project_skill / "SKILL.md").write_text(
            "---\nname: same-name\ndescription: PROJECT\n---\n", encoding="utf-8"
        )
        global_root = tmp_path / "home-skills"
        global_skill = global_root / "same-name"
        global_skill.mkdir(parents=True)
        (global_skill / "SKILL.md").write_text(
            "---\nname: same-name\ndescription: GLOBAL\n---\n", encoding="utf-8"
        )
        loader = SkillsLoader(skills_path=global_root, install_builtins=False)

        rows = collect_skills_blocking(loader, [], project)
        workspace = [row for row in rows if row.get("source") == "kiro-workspace"]

        assert workspace == []

    def test_project_metadata_is_redacted_before_reaching_the_dashboard(self, project, tmp_path):
        from kiro_crew.dashboard.handlers._shared import collect_skills_blocking
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "hostile"
        skill_dir.mkdir(parents=True)
        credential = "AKIAIOSFODNN7EXAMPLE"
        exfil_url = f"http://evil.com/upload/{credential}/x"
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: hostile\n"
            f"description: {credential} {exfil_url}\n"
            f"repo_scope: {credential} {exfil_url}\n"
            "---\n",
            encoding="utf-8",
        )
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        rows = collect_skills_blocking(loader, [], project)
        row = next(r for r in rows if r.get("key") == "kiro-workspace/hostile")

        assert credential not in row["description"]
        assert exfil_url not in row["description"]
        assert "http://" not in row["description"]
        assert credential not in row["repo_scope"]
        assert exfil_url not in row["repo_scope"]
        assert "http://" not in row["repo_scope"]


class TestTrustEndpointAuthorization:
    def test_owner_authorization_is_audited(self, monkeypatch):
        from kiro_crew.dashboard.handlers import prompts

        audit = SimpleNamespace(log_api_access=Mock())
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: True, raising=False
        )
        monkeypatch.setattr(prompts, "_sel", lambda: audit)
        request = SimpleNamespace(get=lambda key, default=None: {"user": "owner"}.get(key, default))

        assert prompts._deny_non_owner_skill_operation(request, "skill_trust_read") is None
        assert audit.log_api_access.call_args.kwargs == {
            "caller": "owner",
            "operation": "skill_trust_read",
            "outcome": "allowed",
            "source": "dashboard",
        }

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_by_every_trust_endpoint(self, monkeypatch):
        from kiro_crew.dashboard.handlers import prompts

        audit = SimpleNamespace(log_api_access=Mock())

        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: False, raising=False
        )
        monkeypatch.setattr(prompts, "_sel", lambda: audit)
        request = SimpleNamespace(get=lambda key, default=None: {"user": "guest"}.get(key, default))

        for handler, operation in (
            (prompts.api_skills_trust, "skill_trust_read"),
            (prompts.api_skills_trust_grant, "skill_trust_grant"),
            (prompts.api_skills_trust_revoke, "skill_trust_revoke"),
        ):
            response = await handler(request)
            body = json.loads(response.body)
            assert response.status == 403, handler.__name__
            assert body["code"] == "dashboard_owner_required", handler.__name__

            call = audit.log_api_access.call_args
            assert call.kwargs == {
                "caller": "guest",
                "operation": operation,
                "outcome": "denied",
                "source": "dashboard",
                "error": "dashboard owner required",
            }
            audit.log_api_access.reset_mock()


class TestTrustGrantHandler:
    @pytest.mark.asyncio
    async def test_no_project_returns_a_coded_400(self, monkeypatch):
        from kiro_crew.dashboard.handlers import prompts

        request = SimpleNamespace(
            app={"state": object()},
            headers={"X-Session-Key": "dashboard:chat-7"},
        )
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: True, raising=False
        )
        monkeypatch.setattr(prompts, "requesting_slot_project", lambda *_args: None)

        response = await prompts.api_skills_trust_grant(request)

        assert response.status == 400
        assert json.loads(response.body)["code"] == "skill_trust_no_project"

    @pytest.mark.asyncio
    async def test_full_store_returns_a_coded_409_without_evicting(self, project, monkeypatch):
        from kiro_crew.dashboard.handlers import prompts

        entries = [
            {"path": f"/synthetic/project-{i}", "granted_at": i}
            for i in range(skill_trust._MAX_GRANT_ENTRIES)
        ]
        path = skill_trust.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        before = json.dumps({"version": 1, "granted": entries}) + "\n"
        path.write_text(before, encoding="utf-8")

        request = SimpleNamespace(
            app={"state": object()},
            headers={"X-Session-Key": "dashboard:chat-7"},
            json=AsyncMock(return_value={"expected_key": str(project.resolve())}),
        )
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: True, raising=False
        )
        monkeypatch.setattr(prompts, "requesting_slot_project", lambda *_args: project)

        response = await prompts.api_skills_trust_grant(request)

        assert response.status == 409
        assert json.loads(response.body)["code"] == "skill_trust_store_full"
        assert path.read_text(encoding="utf-8") == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["grant", "revoke"])
    async def test_write_failure_returns_a_coded_409(self, project, monkeypatch, operation):
        from kiro_crew import skill_trust as st
        from kiro_crew.dashboard.handlers import prompts

        monkeypatch.setattr(
            st,
            "sel",
            lambda: SimpleNamespace(log_governance_decision=Mock()),
        )
        if operation == "revoke":
            skill_trust.grant_project_trust(project)

        def boom(*_a, **_kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(st, "atomic_write", boom)
        request = SimpleNamespace(
            app={"state": object()},
            headers={"X-Session-Key": "dashboard:chat-7"},
            json=AsyncMock(return_value={"expected_key": str(project.resolve())}),
            query={"path": str(project)},
        )
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: True, raising=False
        )
        monkeypatch.setattr(prompts, "requesting_slot_project", lambda *_args: project)

        handler = (
            prompts.api_skills_trust_grant
            if operation == "grant"
            else prompts.api_skills_trust_revoke
        )
        response = await handler(request)

        assert response.status == 409
        assert json.loads(response.body)["code"] == "skill_trust_store_unreadable"


class TestReviewedProjectConfirmation:
    """Consent is recorded only for the directory the operator actually reviewed."""

    def test_a_matching_reviewed_key_grants(self, project):
        from kiro_crew.dashboard.handlers.prompts import _grant_reviewed_project

        key = _grant_reviewed_project(project, skill_trust.canonical_key(project), session_key="t")
        assert key == skill_trust.canonical_key(project)
        assert skill_trust.is_project_trusted(project) is True

    def test_a_non_identical_reviewed_key_is_refused(self, project):
        """The confirmation is an opaque echo, never another path to resolve."""
        from kiro_crew.dashboard.handlers.prompts import (
            _grant_reviewed_project,
            _ReviewedProjectChanged,
        )

        noisy = str(project) + "/./"
        with pytest.raises(_ReviewedProjectChanged):
            _grant_reviewed_project(project, noisy, session_key="t")
        assert skill_trust.is_project_trusted(project) is False

    def test_an_unmatched_network_path_is_never_resolved(self, project, monkeypatch):
        """Client-controlled confirmation text must not trigger an SMB probe."""
        from kiro_crew.dashboard.handlers import prompts

        canonical_key = skill_trust.canonical_key

        def reject_network_resolution(path):
            if str(path).replace("\\", "/").startswith("//"):
                raise AssertionError("client path reached filesystem resolution")
            return canonical_key(path)

        monkeypatch.setattr(skill_trust, "canonical_key", reject_network_resolution)

        with pytest.raises(prompts._ReviewedProjectChanged):
            prompts._grant_reviewed_project(project, r"\\server\share", session_key="t")

    def test_a_reviewed_alias_cannot_retarget_before_the_grant(self, tmp_path, monkeypatch):
        """The grant must persist the key compared, not resolve the alias again."""
        from kiro_crew.dashboard.handlers import prompts

        reviewed = tmp_path / "reviewed"
        unreviewed = tmp_path / "unreviewed"
        reviewed.mkdir()
        unreviewed.mkdir()
        alias = tmp_path / "project"
        try:
            alias.symlink_to(reviewed, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable on this platform")

        canonical_key = skill_trust.canonical_key
        resolutions = 0

        def resolve_then_retarget(path):
            nonlocal resolutions
            resolved = canonical_key(path)
            resolutions += 1
            if resolutions == 1:
                alias.unlink()
                alias.symlink_to(unreviewed, target_is_directory=True)
            return resolved

        monkeypatch.setattr(skill_trust, "canonical_key", resolve_then_retarget)

        key = prompts._grant_reviewed_project(alias, str(reviewed.resolve()), session_key="t")

        assert key == str(reviewed.resolve())
        skill_trust.reset_cache_for_tests()
        assert skill_trust.is_project_trusted(reviewed) is True
        assert skill_trust.is_project_trusted(unreviewed) is False

    def test_the_confirmed_identity_is_canonicalized_exactly_once(self, tmp_path, monkeypatch):
        """Replacing the canonical name after confirmation cannot redirect consent.

        Canonicalizing in the handler and again in the store leaves a race even
        when the handler passes the first resolved string: that directory name
        itself can be renamed and replaced by a symlink before the second call.
        The grant primitive must compare and persist one resolution.
        """
        reviewed = tmp_path / "reviewed"
        parked = tmp_path / "reviewed-original"
        unreviewed = tmp_path / "unreviewed"
        reviewed.mkdir()
        unreviewed.mkdir()
        reviewed_key = str(reviewed.resolve())
        original_canonical_key = skill_trust.canonical_key
        resolutions = 0

        def resolve_then_replace(path):
            nonlocal resolutions
            resolved = original_canonical_key(path)
            resolutions += 1
            if resolutions == 1:
                reviewed.rename(parked)
                try:
                    reviewed.symlink_to(unreviewed, target_is_directory=True)
                except OSError:
                    pytest.skip("symlinks unavailable on this platform")
            return resolved

        monkeypatch.setattr(skill_trust, "canonical_key", resolve_then_replace)

        key = skill_trust.grant_project_trust(
            reviewed,
            expected_key=reviewed_key,
            session_key="t",
        )

        assert key == reviewed_key
        assert resolutions == 1
        stored = json.loads(skill_trust.store_path().read_text(encoding="utf-8"))
        assert [entry["path"] for entry in stored["granted"]] == [reviewed_key]
        assert str(unreviewed.resolve()) not in {entry["path"] for entry in stored["granted"]}

    def test_a_changed_project_refuses_and_records_nothing(self, project, tmp_path):
        """The slot moved between rendering the dialog and clicking Trust."""
        from kiro_crew.dashboard.handlers.prompts import (
            _grant_reviewed_project,
            _ReviewedProjectChanged,
        )

        other = tmp_path / "somewhere-else"
        other.mkdir()

        with pytest.raises(_ReviewedProjectChanged):
            _grant_reviewed_project(project, str(other), session_key="t")

        # Neither directory ends up trusted: the reviewed one was not what this
        # chat is bound to, and the bound one was never reviewed.
        skill_trust.reset_cache_for_tests()
        assert skill_trust.is_project_trusted(project) is False
        assert skill_trust.is_project_trusted(other) is False

    def test_an_absent_expected_key_is_refused(self, project):
        from kiro_crew.dashboard.handlers.prompts import (
            _grant_reviewed_project,
            _ReviewedProjectChanged,
        )

        with pytest.raises(_ReviewedProjectChanged):
            _grant_reviewed_project(project, None, session_key="t")
        assert skill_trust.is_project_trusted(project) is False

    def test_a_project_alias_retargeted_after_snapshot_is_refused(self, tmp_path):
        from kiro_crew.dashboard.handlers.prompts import (
            _grant_reviewed_project,
            _ReviewedProjectChanged,
            _trust_snapshot,
        )

        reviewed = tmp_path / "reviewed"
        unreviewed = tmp_path / "unreviewed"
        reviewed.mkdir()
        unreviewed.mkdir()
        alias = tmp_path / "project"
        try:
            alias.symlink_to(reviewed, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable on this platform")

        snapshot = _trust_snapshot(alias)
        alias.unlink()
        alias.symlink_to(unreviewed, target_is_directory=True)

        with pytest.raises(_ReviewedProjectChanged):
            _grant_reviewed_project(alias, snapshot["project_key"], session_key="t")
        assert skill_trust.is_project_trusted(unreviewed) is False

    def test_the_handler_does_not_touch_the_filesystem_on_the_event_loop(self):
        """The finding was event-loop filesystem work, which behaviour cannot show.

        `canonical_key` realpaths (one lstat per component) and must run on the
        discovery executor like every other filesystem step in this handler. This
        guard fails if it is ever called directly from the async function again --
        which is exactly how it was introduced.
        """
        import ast
        import pathlib as _pathlib

        src_path = (
            _pathlib.Path(__file__).resolve().parents[1]
            / "src/kiro_crew/dashboard/handlers/prompts_skill_catalog.py"
        )
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_skills_trust_grant":
                target = node
        assert target is not None, "api_skills_trust_grant not found — did it move?"

        offenders = [
            f"line {n.lineno}: {n.func.id}()"
            for n in ast.walk(target)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"canonical_key", "grant_project_trust", "is_project_trusted"}
        ]
        assert offenders == [], (
            "these filesystem calls run on the event loop; offload them to "
            "discovery_executor() as the rest of this handler does: " + "; ".join(offenders)
        )
