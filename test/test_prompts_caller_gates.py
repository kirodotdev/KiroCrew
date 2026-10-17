"""Tests for who may reach the prompt API: app tokens, the dashboard placeholder session
key, and dashboard users who are not the configured owner.
"""

from __future__ import annotations

import asyncio
import json

from prompts_helpers import _isolate_home  # noqa: F401
from prompts_helpers import _no_turn_tail  # noqa: F401
from prompts_helpers import mock_sel_fixture  # noqa: F401
from prompts_helpers import (
    _api_request,
    _create_request,
    _list_request,
    _outcomes,
    _sha,
    _slot_state,
    _user_prompt,
    _write_request,
)

from kiro_crew.dashboard.handlers import api_prompt_detail, api_prompts, api_prompts_create


class TestAppTokenWriteGate:
    """App tokens must not reach prompt mutations (path-only grants are verb-blind)."""

    def _assert_forbidden(self, resp, mock_sel):
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "app_token_forbidden"
        # AUTOSDE backend-security-controls: every permission decision emits a
        # SEL audit event, coded with the same word the response carries.
        # "blocked" is this module's outcome vocabulary for every 403.
        assert _outcomes(mock_sel)[-1] == "blocked"
        assert (
            mock_sel.log_tool_invocation.call_args_list[-1][1]["metadata"]["reason"]
            == "app_token_forbidden"
        )

    def test_app_token_cannot_create(self, tmp_path, mock_sel):
        req = _create_request({"name": "x", "content": "b"}, app="someapp")
        self._assert_forbidden(asyncio.run(api_prompts_create(req)), mock_sel)
        assert not (tmp_path / ".kiro" / "prompts" / "x.md").exists()

    def test_app_token_cannot_update(self, tmp_path, mock_sel):
        """The body carries a VALID base_hash so that, ungated, this request
        would succeed and overwrite — making the file-integrity assertion
        load-bearing rather than satisfied by a downstream 400."""
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request(
            "PUT",
            "keep",
            body={"content": "clobbered", "base_hash": _sha("original")},
            app="someapp",
        )
        self._assert_forbidden(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").read_text(
            encoding="utf-8"
        ) == "original"

    def test_app_token_cannot_delete(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request("DELETE", "keep", app="someapp")
        self._assert_forbidden(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").exists()

    def test_absent_app_claim_fails_closed(self, tmp_path, mock_sel):
        """A request the auth middleware never touched is refused, not trusted."""
        req = _create_request({"name": "x", "content": "b"}, app=None)
        self._assert_forbidden(asyncio.run(api_prompts_create(req)), mock_sel)

    def test_app_token_can_still_read(self, tmp_path, mock_sel):
        """The gate is mutations-only, as the spec sentence scopes it: hoisting
        it above the GET dispatch would revoke app read access with nothing red."""
        _user_prompt(tmp_path, "readable", "body\n")
        req = _write_request("GET", "readable", app="someapp")
        resp = asyncio.run(api_prompt_detail(req))
        assert resp.status == 200
        assert json.loads(resp.body)["content"] == "body\n"

    def test_an_unwritable_sel_still_answers_the_denial(self, tmp_path, mock_sel):
        """The audit is best-effort: SEL failing must not turn the 403 into a 500."""
        mock_sel.log_tool_invocation.side_effect = RuntimeError("SEL unwritable")
        req = _create_request({"name": "x", "content": "b"}, app="someapp")
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "app_token_forbidden"

    def test_dashboard_user_still_writes(self, tmp_path, mock_sel):
        """The gate must not catch the "" dashboard-user claim (regression guard)."""
        req = _create_request({"name": "ok-prompt", "content": "b"})
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 201


class TestAppTokenLocalPromptIsolation:
    """An app token may READ prompts, so the header it sends must not let it pick
    which project's local prompts to read.

    App-token grants are path-only: an app permitted to reach ``/api/prompts``
    supplies its own ``X-Session-Key``, and a resolver that honours any slot would
    turn the endpoint into a content oracle for another slot's checkout — prompt
    names, descriptions, and (through the detail lookup) bodies. The app is
    narrowed to a slot it owns rather than refused outright, because the same
    response also carries package SOPs and global prompts, which are not
    slot-scoped and its grant does cover.
    """

    @staticmethod
    def _two_slots(owned_project, foreign_project):
        state = _slot_state(slots={"owned": owned_project, "foreign": foreign_project})
        state._slots["owned"]._app = "app-A"
        state._slots["foreign"]._app = "app-B"
        return state

    def test_app_cannot_list_a_foreign_slots_local_prompts(self, tmp_path, mock_sel):
        owned = tmp_path / "owned"
        owned.mkdir()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        _user_prompt(owned, "mine")
        _user_prompt(foreign, "theirs")
        _user_prompt(tmp_path, "everyones")  # ~/.kiro/prompts — not slot-scoped
        state = self._two_slots(owned, foreign)

        names = [
            p["name"]
            for p in json.loads(
                asyncio.run(
                    api_prompts(_list_request(session_key="foreign", state=state, app="app-A"))
                ).body
            )
        ]
        assert "theirs" not in names, "app-A read app-B's slot project"
        assert "mine" not in names, "a foreign key must not fall back to the app's own slot"
        # The grant it does hold is intact: global prompts still answer.
        assert "everyones" in names

    def test_app_reads_its_own_slots_local_prompts(self, tmp_path, mock_sel):
        """The narrowing is targeted, not a blanket revocation of local reads."""
        owned = tmp_path / "owned"
        owned.mkdir()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        _user_prompt(owned, "mine")
        _user_prompt(foreign, "theirs")
        state = self._two_slots(owned, foreign)

        names = [
            p["name"]
            for p in json.loads(
                asyncio.run(
                    api_prompts(_list_request(session_key="owned", state=state, app="app-A"))
                ).body
            )
        ]
        assert "mine" in names
        assert "theirs" not in names

    def test_app_gets_no_shared_project_fallback(self, tmp_path, mock_sel):
        """The dashboard's "single project every slot shares" step must not apply
        to an app: it would hand over a local project with no header at all."""
        shared = tmp_path / "shared"
        shared.mkdir()
        _user_prompt(shared, "shared-local")
        state = _slot_state(slots={"slot-a": shared, "slot-b": shared})

        names = [
            p["name"]
            for p in json.loads(
                asyncio.run(
                    api_prompts(_list_request(session_key="", state=state, app="appX"))
                ).body
            )
        ]
        assert "shared-local" not in names
        # Same state, dashboard claim: the fallback DOES apply, so this test is
        # pinning the app branch and not an unreachable code path.
        dash = [
            p["name"]
            for p in json.loads(
                asyncio.run(api_prompts(_list_request(session_key="", state=state))).body
            )
        ]
        assert "shared-local" in dash

    def test_app_cannot_resolve_a_foreign_local_prompt_by_name(self, tmp_path, mock_sel):
        """The unscoped detail lookup is the oracle that would leak CONTENT, not
        just a name, so it must narrow through the same seam as the lister."""
        owned = tmp_path / "owned"
        owned.mkdir()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        _user_prompt(foreign, "secret-sop", "CONFIDENTIAL BODY")
        state = self._two_slots(owned, foreign)

        resp = asyncio.run(
            api_prompt_detail(
                _api_request("secret-sop", session_key="foreign", state=state, app="app-A")
            )
        )
        assert resp.status == 404
        assert b"CONFIDENTIAL" not in resp.body

    def test_refused_slot_selection_is_audited(self, tmp_path, mock_sel):
        """A forged key from an app leaves an attributable line, so probing is
        visible rather than silent."""
        owned = tmp_path / "owned"
        owned.mkdir()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        state = self._two_slots(owned, foreign)
        asyncio.run(api_prompts(_list_request(session_key="foreign", state=state, app="app-A")))
        denials = [
            c.kwargs
            for c in mock_sel.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert denials, "a refused app slot selection was not audited"
        assert denials[0]["caller"] == "app-A"
        assert denials[0]["source"] == "app_isolation"

    def test_a_granted_slot_selection_is_audited(self, tmp_path, mock_sel):
        """The GRANT is the event an operator needs after a compromised app: every
        selection it made succeeded, so a log carrying only refusals cannot say
        which project it was served. Same operation and source as the denial, so
        one app request leaves exactly one attributable line either way."""
        owned = tmp_path / "owned"
        owned.mkdir()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        _user_prompt(owned, "mine")
        state = self._two_slots(owned, foreign)

        asyncio.run(api_prompts(_list_request(session_key="owned", state=state, app="app-A")))
        grants = [
            c.kwargs
            for c in mock_sel.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "allowed"
        ]
        assert grants, "an authorized app slot selection was not audited"
        assert grants[0]["caller"] == "app-A"
        assert grants[0]["source"] == "app_isolation"
        assert grants[0]["operation"] == "prompt_local_project"
        assert grants[0]["resources"] == "slot=owned"

    def test_an_unwritable_sel_still_serves_the_granted_slot(self, tmp_path, mock_sel):
        """The audit is best-effort in BOTH directions: an unwritable SEL must not
        withdraw an access the ownership check authorized."""
        mock_sel.log_api_access.side_effect = RuntimeError("SEL unwritable")
        owned = tmp_path / "owned"
        owned.mkdir()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        _user_prompt(owned, "mine")
        state = self._two_slots(owned, foreign)

        resp = asyncio.run(
            api_prompts(_list_request(session_key="owned", state=state, app="app-A"))
        )
        assert resp.status == 200
        assert "mine" in [p["name"] for p in json.loads(resp.body)]

    def test_an_unwritable_sel_still_narrows_the_answer(self, tmp_path, mock_sel):
        """The audit is best-effort; the isolation is not. SEL failing must not
        turn the narrowing into a 500 — nor into a leak."""
        mock_sel.log_api_access.side_effect = RuntimeError("SEL unwritable")
        owned = tmp_path / "owned"
        owned.mkdir()
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        _user_prompt(foreign, "theirs")
        state = self._two_slots(owned, foreign)
        resp = asyncio.run(
            api_prompts(_list_request(session_key="foreign", state=state, app="app-A"))
        )
        assert resp.status == 200
        assert "theirs" not in [p["name"] for p in json.loads(resp.body)]


class TestTheDashboardPlaceholderKeyNamesNoChat:
    """``dashboard:ui`` marks the SURFACE, so it must not select a chat slot.

    The dashboard's browser client sends that literal on every request with no
    chat to name — including this API's create, update and delete, which pass no
    slot at all — while the listing GET sends no header whatsoever. The slot-name
    split turns the literal into ``ui``, and a chat named ``ui`` is a name a user
    can pick, so honouring it would resolve "This project" from that chat: the
    listing would answer from the shared project while a write landed in the
    ``ui`` chat's checkout. That is the create/list disagreement this seam exists
    to prevent, and it is a write into a project the request never named.

    A chat genuinely named ``ui`` sends the same bytes (the real key is
    ``dashboard:<slot>``), so the two are indistinguishable on the wire. Folding
    to the slotless surface is the fail-safe side of that ambiguity: the shared
    project still answers for the ordinary install, and nothing is written to a
    checkout the request did not name.
    """

    @staticmethod
    def _ui_slot_and_a_neighbour(ui_project, other_project):
        """Two chats on DIFFERENT projects, one named ``ui``.

        Distinct projects on purpose: they make the shared-project step answer
        ``None``, so anything the placeholder resolves to can only have come from
        selecting a slot.
        """
        return _slot_state(slots={"ui": ui_project, "other": other_project})

    def test_the_placeholder_does_not_list_the_ui_slots_local_prompts(self, tmp_path, mock_sel):
        ui_proj = tmp_path / "ui-checkout"
        ui_proj.mkdir()
        other = tmp_path / "other-checkout"
        other.mkdir()
        _user_prompt(ui_proj, "ui-slot-local")
        _user_prompt(tmp_path, "everyones")  # ~/.kiro/prompts — not slot-scoped
        state = self._ui_slot_and_a_neighbour(ui_proj, other)

        names = [
            p["name"]
            for p in json.loads(
                asyncio.run(
                    api_prompts(_list_request(session_key="dashboard:ui", state=state))
                ).body
            )
        ]
        assert "ui-slot-local" not in names, "the UI placeholder selected the slot named ui"
        assert "everyones" in names

    def test_a_local_create_through_the_placeholder_refuses_instead_of_picking_that_slot(
        self, tmp_path, mock_sel
    ):
        """The damaging verb: a create must not land in a chat's checkout because
        that chat happens to be named after the placeholder."""
        ui_proj = tmp_path / "ui-checkout"
        ui_proj.mkdir()
        other = tmp_path / "other-checkout"
        other.mkdir()
        state = self._ui_slot_and_a_neighbour(ui_proj, other)

        resp = asyncio.run(
            api_prompts_create(
                _create_request(
                    {"name": "planted", "content": "BODY\n", "scope": "local"},
                    session_key="dashboard:ui",
                    state=state,
                )
            )
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "no_active_project"
        assert not (ui_proj / ".kiro" / "prompts" / "planted.md").exists()

    def test_a_local_delete_through_the_placeholder_leaves_that_slots_prompt_alone(
        self, tmp_path, mock_sel
    ):
        ui_proj = tmp_path / "ui-checkout"
        ui_proj.mkdir()
        other = tmp_path / "other-checkout"
        other.mkdir()
        victim = _user_prompt(ui_proj, "victim", "LOCAL BODY\n")
        state = self._ui_slot_and_a_neighbour(ui_proj, other)

        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "DELETE", "victim", scope="local", session_key="dashboard:ui", state=state
                )
            )
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "no_active_project"
        assert victim.exists()

    def test_the_shared_project_step_still_answers_through_the_placeholder(
        self, tmp_path, mock_sel
    ):
        """The fold narrows to the slotless surface, it does not withdraw "This
        project" from it: with one project open, the placeholder still resolves —
        which is what keeps the dashboard's own create/update/delete working, and
        what makes the tests above pin the fold rather than a dead branch."""
        shared = tmp_path / "shared"
        shared.mkdir()
        state = _slot_state(slots={"ui": shared, "other": shared})

        resp = asyncio.run(
            api_prompts_create(
                _create_request(
                    {"name": "ok", "content": "BODY\n", "scope": "local"},
                    session_key="dashboard:ui",
                    state=state,
                )
            )
        )
        assert resp.status == 201
        assert (shared / ".kiro" / "prompts" / "ok.md").exists()


class TestNonOwnerWriteGate:
    """Prompt mutations require the CONFIGURED OWNER, not any dashboard user.

    ``!dashboard`` links give allowed messaging users dashboard sessions, so
    "claim present-and-empty" alone would let a non-owner mutate the owner's
    agent instructions. Reads stay open to every dashboard user.
    """

    def _assert_owner_required(self, resp, mock_sel):
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "dashboard_owner_required"
        # Same audit contract as the app-token gate: every permission decision
        # emits a SEL event coded with the word the response carries.
        assert _outcomes(mock_sel)[-1] == "blocked"
        assert (
            mock_sel.log_tool_invocation.call_args_list[-1][1]["metadata"]["reason"]
            == "dashboard_owner_required"
        )

    def test_non_owner_cannot_create(self, tmp_path, mock_sel):
        req = _create_request({"name": "x", "content": "b"}, user="other-user")
        self._assert_owner_required(asyncio.run(api_prompts_create(req)), mock_sel)
        assert not (tmp_path / ".kiro" / "prompts" / "x.md").exists()

    def test_non_owner_cannot_update(self, tmp_path, mock_sel):
        """The body carries a VALID base_hash so that, ungated, this request
        would succeed and overwrite — the file-integrity assertion is
        load-bearing rather than satisfied by a downstream 400."""
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request(
            "PUT",
            "keep",
            body={"content": "clobbered", "base_hash": _sha("original")},
            user="other-user",
        )
        self._assert_owner_required(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").read_text(
            encoding="utf-8"
        ) == "original"

    def test_non_owner_cannot_delete(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request("DELETE", "keep", user="other-user")
        self._assert_owner_required(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").exists()

    def test_non_owner_can_still_read(self, tmp_path, mock_sel):
        """The owner gate is mutations-only, like the app-token gate above it."""
        _user_prompt(tmp_path, "readable", "body\n")
        req = _write_request("GET", "readable", user="other-user")
        resp = asyncio.run(api_prompt_detail(req))
        assert resp.status == 200

    def test_no_owner_configured_admits_local_subject(self, tmp_path, mock_sel):
        """A no-owner install keeps working: the signed local bootstrap subject
        passes the gate — the same rule every other owner-gated surface uses."""
        req = _create_request({"name": "boot", "content": "b"}, user="local-app", owner="")
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 201

    def test_no_owner_configured_still_refuses_unknown_subject(self, tmp_path, mock_sel):
        req = _create_request({"name": "x", "content": "b"}, user="random-user", owner="")
        self._assert_owner_required(asyncio.run(api_prompts_create(req)), mock_sel)
