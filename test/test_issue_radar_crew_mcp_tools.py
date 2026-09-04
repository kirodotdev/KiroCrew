"""The agent's read/write path into its own Issue Radar crew ledger.

An Issue Radar crew is an unattended agent whose context does not survive
compaction, its per-turn ceiling, or a gateway restart. The ledger does — so
these two MCP tools are the crew's memory, and every property asserted here is
load-bearing for a cold resume:

  * the ledger must be REACHABLE from an agent session, which has no dashboard
    credential (httpOnly cookie, ``KIROCREW_INTERNAL_SECRET`` stripped from agent
    env, ``.local_secret`` on the sensitive-path denylist) — hence the
    internal-secret allowlist entries, and hence a raw HTTP call earning 403;
  * a partial write must never erase what an earlier write stored, or a resume
    reads back a ledger emptier than the work it describes;
  * a phase must never move without a logged reason, which is why upsert and
    append are ONE tool;
  * the public strings must not carry this machine's identity, because the event
    log feeds a comment on github.com as well as the crew page.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_core
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.validation import MCP_CORE_SCHEMAS, ValidationError, validate_tool_args

READ_TOOL = "issue_radar_crew_read"
RECORD_TOOL = "issue_radar_crew_record"
READ_PATH = "/api/apps/issue-radar/crew"
WORK_PATH = "/api/apps/issue-radar/crew/work"

#: What the read route answers with. Identity lives here and ONLY here — no tool
#: argument can select a crew — so the fixture doubles as the contract.
#:
#: The top-level ``owner``/``repo`` are the route's own echo, and they are the pair
#: that is ALWAYS present: a crew with no work items yet has no other source. The
#: copies on the crew record and the item mirror what the store holds, so this
#: fixture also covers ``_crew_identity``'s tolerance of all three locations.
#: ``test_issue_radar_crew_routes.TestAgentIdentityIsTheSession`` asserts the real
#: handler produces this shape, so the stub cannot drift from the route.
CREW_PAYLOAD = {
    "owner": "acme",
    "repo": "widget",
    "provider": "github",
    "host": "github.com",
    "crew": {"id": "c_7f3a", "name": "Whirlpool", "owner": "acme", "repo": "widget"},
    "settings": {"claim_ttl_hours": 48, "needs_human_label": "crew: needs human"},
    "items": [{"crew_id": "c_7f3a", "owner": "acme", "repo": "widget", "number": 12}],
    "events": [],
    "skipped_numbers": [4, 91],
    "recent_skips": [{"number": 91, "reason": "needs a design decision", "scope": "needs-design"}],
    "counts": {"open": 1},
}


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _clean(**over):
    args = {"number": 12}
    args.update(over)
    return validate_tool_args(args, MCP_CORE_SCHEMAS[RECORD_TOOL])


def _record(payload: dict | None = None, put_result: dict | None = None, **over):
    """Invoke the record tool with the HTTP legs stubbed.

    Returns ``(captured, output)`` where ``captured`` holds the GET path and the
    PUT path/body. Module-level rather than a base-class method: a class that
    inherited it would also inherit and re-run that class's tests.
    """
    cleaned = _clean(**over)
    captured: dict = {}

    def fake_get(path, session_key=None):
        captured["get_path"] = path
        captured["get_session_key"] = session_key
        return CREW_PAYLOAD if payload is None else payload

    def fake_put(path, body=None, session_key=None):
        captured["put_path"] = path
        captured["body"] = body
        captured["put_session_key"] = session_key
        if put_result is not None:
            return put_result
        return {"item": {"phase": body.get("phase") or "claimed", "next": body.get("next") or ""}}

    # These tools require a DIRECTLY identified session (see the strict-identity
    # gate in `_call_tool_inner`), so the harness has to model one. Stubbing only
    # the HTTP legs left identity unexercised — the same blind spot that once let a
    # 400 on the read route pass every test on this feature.
    with patch.object(mcp_core, "_resolve_session_key_strict", return_value="crew-c_7f3a"):
        with patch.object(mcp_core, "_get", side_effect=fake_get):
            with patch.object(mcp_core, "_put", side_effect=fake_put):
                out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
    return captured, out


class TestGatewayPathsAreReachableWithTheInternalSecret(unittest.TestCase):
    def test_an_explicit_session_key_beats_the_lenient_resolver(self):
        """The other half of the identity fix: the helpers must HONOUR the key.

        The tool threading its verified key through is only half the guarantee —
        if ``_get``/``_put`` ignored the argument and resolved their own, the wire
        would still carry the lenient answer. So the lenient resolver is stubbed to
        a DIFFERENT session here: the header must be the passed key, which is the
        only way to tell "honoured" from "coincidentally equal".
        """
        import urllib.request

        for helper, kwargs in ((mcp_core._get, {}), (mcp_core._put, {"body": {}})):
            with self.subTest(helper=helper.__name__):
                seen: dict = {}

                class _Resp:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self):
                        return b"{}"

                def _capture(req: urllib.request.Request, timeout=None, unix_socket_path=None):
                    seen["key"] = req.headers.get("X-session-key")
                    return _Resp()

                with patch.object(mcp_core, "_resolve_session_key", return_value="OTHER"):
                    with patch.object(mcp_core, "_internal_secret", return_value="s"):
                        # `_API` is pinned rather than inherited: the helpers build a
                        # urllib Request from it and swallow any construction failure
                        # into `{"error": ...}`, so an unset or malformed base URL in
                        # the environment would skip the transport entirely and this
                        # test would report a missing capture instead of the identity
                        # behaviour it exists to check (which is what it did in CI).
                        # Patch the transport the helpers ACTUALLY call. This was
                        # `loopback_urlopen` on the branch's old base and is
                        # `_api_urlopen` now; the module still exports the old name,
                        # so patching it succeeded while the real request went out
                        # unpatched, got swallowed into `{"error": ...}`, and the
                        # capture never ran — green locally, red on the merge.
                        # It now also carries the attempt's resolved socket path
                        # (#4106 item 1), so the fake must accept it or the same
                        # swallow-into-error failure returns.
                        with patch.object(mcp_core, "_api_urlopen", _capture):
                            got = helper("/api/x", session_key="MINE", **kwargs)

                # Fail on the real reason, not on a KeyError three lines later.
                assert "error" not in got, f"{helper.__name__} never sent a request: {got}"
                assert seen["key"] == "MINE", (
                    f"{helper.__name__} ignored the verified key and re-resolved one"
                )

    def test_both_crew_routes_are_mixed_internal_paths(self):
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        assert READ_PATH in _MIXED_INTERNAL_API_PATHS
        assert WORK_PATH in _MIXED_INTERNAL_API_PATHS

    def test_entries_are_full_paths_not_the_apps_prefix(self):
        """A prefix entry would hand the app's forge-write routes to the secret.

        ``token_auth`` matches ``path == p or path.startswith(p + "/")``, so an
        ``/api/apps/issue-radar`` entry would also admit ``/labels/apply``,
        ``/issue/close`` and the comment routes — which write to GitHub — to
        anything holding the internal secret.
        """
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        for entry in _MIXED_INTERNAL_API_PATHS | _STRICT_INTERNAL_API_PATHS:
            assert entry != "/api/apps"
            assert entry != "/api/apps/issue-radar"
            assert not entry.startswith("/api/apps/issue-radar/labels")
            assert not entry.startswith("/api/apps/issue-radar/issue")
            assert not entry.startswith("/api/apps/issue-radar/comment")

    def test_the_paths_the_tools_call_are_the_paths_allowlisted(self):
        # A route constant that drifts from the allowlist entry is a silent 403
        # for an unattended agent — nobody is watching the turn it happens on.
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        assert mcp_core._CREW_READ_PATH == READ_PATH
        assert mcp_core._CREW_WORK_PATH == WORK_PATH
        assert mcp_core._CREW_READ_PATH in _MIXED_INTERNAL_API_PATHS
        assert mcp_core._CREW_WORK_PATH in _MIXED_INTERNAL_API_PATHS


class TestToolRegistration(unittest.TestCase):
    def test_both_schemas_are_registered(self):
        # An unregistered tool's args pass through raw and its ValidationError
        # escapes the stdio loop, killing kirocrew-core for the whole session.
        assert READ_TOOL in MCP_CORE_SCHEMAS
        assert RECORD_TOOL in MCP_CORE_SCHEMAS

    def test_both_tools_are_advertised(self):
        listed = {t["name"] for t in mcp_core._list_tools()}
        assert READ_TOOL in listed
        assert RECORD_TOOL in listed

    def test_read_takes_no_arguments_at_all(self):
        # Identity is resolved from the session; if the model could name a crew
        # it could read, then write against, another crew's ledger.
        spec = next(t for t in mcp_core._list_tools() if t["name"] == READ_TOOL)
        assert spec["inputSchema"]["properties"] == {}
        assert MCP_CORE_SCHEMAS[READ_TOOL].fields == []

    def test_record_requires_nothing_unconditionally(self):
        # `number` USED to be required, which left a crew that swept an empty
        # queue no way to record the cycle without inventing an issue number. The
        # coupling that replaced the requirement (a missing number is valid only
        # with `sweep`, and `sweep` only without one) is a relation between two
        # fields, which neither schema can express — it lives on the write route
        # and in the store, so both schemas must agree that nothing is required.
        required = {f.name for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields if f.required}
        assert required == set()
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert spec["inputSchema"]["required"] == []

    def test_record_advertises_the_crew_level_sweep_kind(self):
        # Advertised, or the model cannot discover the one kind that lets it
        # report a cycle it did no work in.
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert "sweep" in spec["inputSchema"]["properties"]["event_kind"]["enum"]

    def test_record_advertises_no_identity_arguments(self):
        props = next(
            t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL
        )["inputSchema"]["properties"]
        for forbidden in ("owner", "repo", "crew_id", "id"):
            assert forbidden not in props
        assert {f.name for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields}.isdisjoint(
            {"owner", "repo", "crew_id"}
        )

    def test_advertised_schema_matches_the_validated_schema(self):
        # A property the model is told about but the validator rejects is a
        # guaranteed error mid-turn; the reverse is an undiscoverable field.
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert set(spec["inputSchema"]["properties"]) == {
            f.name for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields
        }

    def test_advertised_enums_are_the_validated_enums(self):
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        props = spec["inputSchema"]["properties"]
        by_name = {f.name: f for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields}
        assert set(props["phase"]["enum"]) == set(by_name["phase"].allowed)
        assert set(props["event_kind"]["enum"]) == set(by_name["event_kind"].allowed)

    def test_descriptions_warn_that_the_progress_line_is_public(self):
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert "PUBLIC" in spec["description"]
        assert "403" in spec["description"]

    def test_the_read_description_tells_the_crew_the_skip_list_is_shared(self):
        # The index is only worth having if a crew CONSULTS it before
        # investigating. Nothing enforces that, so the tool description is the
        # only place the instruction can live.
        spec = next(t for t in mcp_core._list_tools() if t["name"] == READ_TOOL)
        assert "skipped_numbers" in spec["description"]
        assert "SHARED" in spec["description"]
        assert "BEFORE you investigate" in spec["description"]

    def test_the_record_description_names_skip_scope(self):
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert "skip_scope" in spec["description"]
        assert "skip_scope" in spec["inputSchema"]["properties"]

    def test_the_skip_scope_vocabulary_is_advertised_and_mirrors_the_store(self):
        # validation.py cannot import an app package, so it mirrors the store's
        # tuple. Advertised as an enum so the model picks a real one, but NOT
        # enforced as ``allowed=``: refusing an odd label would fail the write
        # that indexes the skip, and an unindexed skip is the waste the index
        # exists to remove. The store coerces to ``other`` instead.
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store

        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        advertised = set(spec["inputSchema"]["properties"]["skip_scope"]["enum"])
        assert advertised == set(crew_store.SKIP_SCOPES)
        by_name = {f.name: f for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields}
        assert by_name["skip_scope"].allowed is None

    def test_the_phase_vocabulary_mirrors_the_store_exactly(self):
        # validation.py cannot import an app package, so it mirrors these. Drift
        # would silently reject a legitimate phase at the tool boundary.
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store

        by_name = {f.name: f for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields}
        assert set(by_name["phase"].allowed) == set(crew_store.PHASES)
        assert set(by_name["event_kind"].allowed) == set(crew_store.EVENT_KINDS)


class TestArgValidation(unittest.TestCase):
    def test_rejects_an_unknown_arg(self):
        # Fail closed on a hallucinated field rather than dropping it silently:
        # a crew that thinks it recorded `blocked_on` has recorded nothing.
        with pytest.raises(ValidationError):
            _clean(blocked_on="review")

    def test_rejects_identity_args(self):
        # Not merely absent from the schema — actively refused, so an attempt to
        # aim a write at another repo or crew errors instead of being ignored.
        for arg, value in (("owner", "other"), ("repo", "other"), ("crew_id", "c_dead")):
            with pytest.raises(ValidationError):
                _clean(**{arg: value})

    def test_read_rejects_any_arg(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"crew_id": "c_7f3a"}, MCP_CORE_SCHEMAS[READ_TOOL])

    def test_rejects_an_unknown_phase(self):
        with pytest.raises(ValidationError):
            _clean(phase="thinking", event="x", event_kind="ci")

    def test_rejects_an_unknown_event_kind(self):
        with pytest.raises(ValidationError):
            _clean(event="did a thing", event_kind="musing")

    def test_a_phase_write_must_carry_its_reason(self):
        # The point of merging upsert+append into one tool: two tools are what
        # let a phase move with nothing logged.
        with pytest.raises(ValidationError):
            _clean(phase="implementing")

    def test_event_and_kind_travel_together(self):
        with pytest.raises(ValidationError):
            _clean(event="CI round 3 — 41/47 green")
        with pytest.raises(ValidationError):
            _clean(event_kind="ci")

    def test_a_complete_progress_step_validates(self):
        cleaned = _clean(phase="awaiting-ci", event="pushed round 3", event_kind="ci")
        assert cleaned["phase"] == "awaiting-ci"
        assert cleaned["event_kind"] == "ci"

    def test_rejects_a_number_that_would_blow_up_the_filename(self):
        # The number becomes crews/<crew_id>/<n>.json.
        with pytest.raises(ValidationError):
            _clean(number=10**12)

    def test_rejects_a_boolean_number(self):
        # bool is an int subclass; True would otherwise record against #1.
        with pytest.raises(ValidationError):
            _clean(number=True)

    def test_rejects_a_base_sha_that_is_not_a_git_object_name(self):
        with pytest.raises(ValidationError):
            _clean(base_sha="origin/main")

    def test_accepts_an_abbreviated_sha(self):
        assert _clean(base_sha="f2aa4c8")["base_sha"] == "f2aa4c8"


class TestIdentityComesFromTheSession(unittest.TestCase):
    def test_the_put_body_names_owner_repo_and_crew_explicitly(self):
        # Explicit identity is what stops a same-numbered issue in another repo
        # being the record that gets written.
        captured, _ = _record()
        assert captured["get_path"] == READ_PATH
        assert captured["put_path"] == WORK_PATH
        assert captured["body"]["owner"] == "acme"
        assert captured["body"]["repo"] == "widget"
        assert captured["body"]["crew_id"] == "c_7f3a"
        assert captured["body"]["number"] == 12

    def test_identity_is_read_from_a_work_item_when_the_crew_record_omits_it(self):
        payload = {
            "crew": {"id": "c_7f3a", "name": "Whirlpool"},
            "items": [{"crew_id": "c_7f3a", "owner": "acme", "repo": "widget", "number": 12}],
        }
        captured, _ = _record(payload=payload)
        assert (captured["body"]["owner"], captured["body"]["repo"]) == ("acme", "widget")

    def test_a_subagent_resolved_identity_cannot_read_or_write_the_crew(self):
        """A subagent must not inherit its parent crew's authority.

        ``_get``/``_put`` attach ``X-Session-Key`` using the LENIENT resolver, which
        falls back to a /proc ancestor walk. A subagent spawned with ``spawn_run``
        runs under its parent slot's process tree, so that walk resolves it to the
        PARENT — and the route derives which crew is calling from exactly that
        header. Without a strict gate the subagent reads the parent crew's ledger
        and writes its work items.

        Both tools are asserted, and both are asserted to refuse BEFORE any HTTP
        leg: a refusal that still issued the request would already have leaked the
        crew's identity, worktree path and claim state.
        """
        for tool, args in ((READ_TOOL, {}), (RECORD_TOOL, _clean())):
            reached: dict = {}

            def _boom(*a, **k):
                reached["called"] = True
                raise AssertionError("must not reach the HTTP leg")

            with patch.object(
                mcp_core, "_resolve_session_key_strict", return_value=""
            ), patch.object(mcp_core, "_get", side_effect=_boom), patch.object(
                mcp_core, "_put", side_effect=_boom
            ):
                out = mcp_core._call_tool_inner(tool, args)

            assert out.startswith("Error:"), (tool, out)
            assert "directly-identified" in out, (tool, out)
            assert "called" not in reached, tool

    def test_a_non_crew_session_is_refused_before_any_write(self):
        captured, out = _record(payload={"crew": {}, "items": []})
        assert out.startswith("Error:")
        assert "not bound to an Issue Radar crew" in out
        assert "put_path" not in captured

    def test_a_read_error_is_surfaced_instead_of_writing_blind(self):
        cleaned = _clean()
        with patch.object(
            mcp_core, "_resolve_session_key_strict", return_value="crew-c_7f3a"
        ):
            with patch.object(mcp_core, "_get", return_value={"error": "not connected"}):
                with patch.object(mcp_core, "_put") as put:
                    out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
            assert out.startswith("Error:")
            put.assert_not_called()

    def test_a_write_error_is_surfaced_instead_of_claiming_success(self):
        cleaned = _clean(next="rebase onto main")
        with patch.object(
            mcp_core, "_resolve_session_key_strict", return_value="crew-c_7f3a"
        ):
            with patch.object(mcp_core, "_get", return_value=CREW_PAYLOAD):
                with patch.object(mcp_core, "_put", return_value={"error": "unknown crew"}):
                    out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
            assert out.startswith("Error:")
            assert "unknown crew" in out


class TestEmptyFieldsAreDropped(unittest.TestCase):
    """A partial patch must preserve what an earlier write stored."""

    def test_a_minimal_call_sends_identity_and_nothing_else(self):
        captured, _ = _record()
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number"}

    def test_an_explicitly_empty_field_is_not_sent(self):
        # "" would still be a merge key on the way through; dropping keeps the
        # request honest about what this call is actually asserting.
        captured, _ = _record(next="", decision="", worktree="", branch="")
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number"}

    def test_only_the_supplied_fields_are_sent(self):
        captured, _ = _record(next="add the Windows branch to _safe_chmod")
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number", "next"}
        assert captured["body"]["next"] == "add the Windows branch to _safe_chmod"

    def test_a_skip_carries_its_scope_to_the_route(self):
        captured, _ = _record(
            phase="skipped", event="passing — needs a design call", event_kind="skip",
            why="the fix changes the on-disk shape", skip_scope="needs-design",
        )
        assert captured["body"]["phase"] == "skipped"
        assert captured["body"]["skip_scope"] == "needs-design"
        # `why` is what the shared index stores as the reason, so it must arrive.
        assert captured["body"]["why"] == "the fix changes the on-disk shape"

    def test_a_skip_without_a_scope_sends_none_and_lets_the_route_default_it(self):
        captured, _ = _record(phase="skipped", event="passing", event_kind="skip")
        assert "skip_scope" not in captured["body"]

    def test_a_rejected_approach_carries_its_reason(self):
        captured, _ = _record(
            tried_approach="patch os.fchmod directly",
            tried_rejected_because="Windows has no fchmod",
        )
        body = captured["body"]
        assert body["tried_approach"] == "patch os.fchmod directly"
        assert body["tried_rejected_because"] == "Windows has no fchmod"

    def test_a_reason_with_no_approach_is_not_sent_alone(self):
        # The store only appends a `tried` entry when it sees an approach; a
        # lone reason would be silently discarded there.
        captured, _ = _record(tried_rejected_because="Windows has no fchmod")
        assert "tried_rejected_because" not in captured["body"]

    def test_the_local_resume_fields_survive_verbatim(self):
        captured, _ = _record(
            worktree="/home/user/src/project",
            branch="fix/os-fchmod-windows",
            base_sha="f2aa4c8bb",
        )
        body = captured["body"]
        # NOT scrubbed: an absolute path is the point of this field, it stays
        # local, and a scrubbed one is a resume that cannot find its worktree.
        assert body["worktree"] == "/home/user/src/project"
        assert body["branch"] == "fix/os-fchmod-windows"
        assert body["base_sha"] == "f2aa4c8bb"


class TestACrewCanReportAnEmptyQueue(unittest.TestCase):
    """The numberless call — `event_kind: sweep` with no `number`.

    `number` was the tool's one required field, so a crew that checked its queue
    and took nothing could only record the cycle by naming an issue it never
    acted on. These assert the absence travels as an ABSENCE: the route reads a
    missing key as "this step has no issue", and a `0` standing in for it would
    be rejected as a malformed issue number instead.
    """

    @staticmethod
    def _sweep(put_result: dict | None = None, **over):
        args = {"event": "checked 42 open issues, took none", "event_kind": "sweep"}
        args.update(over)
        cleaned = validate_tool_args(args, MCP_CORE_SCHEMAS[RECORD_TOOL])
        captured: dict = {}

        def fake_put(path, body=None, session_key=None):
            captured["body"] = body
            return put_result if put_result is not None else {
                "item": None, "skip": None,
                "event": {"text": body.get("event") or ""},
            }

        with patch.object(
            mcp_core, "_resolve_session_key_strict", return_value="crew-c_7f3a"
        ):
            with patch.object(mcp_core, "_get", return_value=CREW_PAYLOAD):
                with patch.object(mcp_core, "_put", side_effect=fake_put):
                    out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
        return captured, out

    def test_a_sweep_validates_without_a_number(self):
        cleaned = validate_tool_args(
            {"event": "queue empty", "event_kind": "sweep"},
            MCP_CORE_SCHEMAS[RECORD_TOOL],
        )
        assert "number" not in cleaned

    def test_the_number_key_is_omitted_from_the_request(self):
        captured, _ = self._sweep()
        # Omitted, not zero: the route reads a present-but-invalid number as a
        # 400, so sending 0 would fail the write rather than record the sweep.
        assert "number" not in captured["body"]
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "event", "event_kind"}

    def test_the_summary_names_the_crew_rather_than_a_bare_hash(self):
        _, out = self._sweep()
        assert "#" not in out.splitlines()[0]
        assert "no issue" in out
        assert "checked 42 open issues, took none" in out

    def test_a_numbered_call_still_sends_and_reports_its_number(self):
        # The guard is on presence, so the ordinary path must be untouched.
        captured, out = _record(number=12, event="took it", event_kind="claim")
        assert captured["body"]["number"] == 12
        assert "#12" in out


class TestCiStateAssembly(unittest.TestCase):
    """The flat ci_* args become the store's ``ci_state`` dict."""

    def test_assembles_every_ci_field(self):
        captured, _ = _record(
            phase="awaiting-ci",
            event="CI round 3 — 41/47 green, 6 inherited",
            event_kind="ci",
            ci_state="failure",
            ci_passed=41,
            ci_total=47,
            ci_round=3,
            ci_inherited_reds=6,
        )
        assert captured["body"]["ci_state"] == {
            "state": "failure",
            "passed": 41,
            "total": 47,
            "round": 3,
            "inherited_reds": 6,
        }

    def test_a_partial_ci_reading_sends_only_what_was_read(self):
        captured, _ = _record(ci_state="pending")
        assert captured["body"]["ci_state"] == {"state": "pending"}

    def test_no_ci_key_at_all_when_nothing_was_read(self):
        captured, _ = _record(next="waiting")
        assert "ci_state" not in captured["body"]

    def test_a_zero_reading_is_kept_not_dropped(self):
        # 0/47 green and 0 inherited reds are both real, load-bearing readings:
        # dropping them on falsiness would leave the previous round's numbers in
        # place and a crew would read them as still-green.
        captured, _ = _record(ci_passed=0, ci_total=47, ci_inherited_reds=0)
        assert captured["body"]["ci_state"] == {
            "passed": 0,
            "total": 47,
            "inherited_reds": 0,
        }

    def test_the_flat_ci_args_never_leak_through_as_themselves(self):
        captured, _ = _record(ci_state="success", ci_passed=47, ci_total=47)
        for leaked in ("ci_passed", "ci_total", "ci_round", "ci_inherited_reds"):
            assert leaked not in captured["body"]


class TestPublicStringsAreSanitizedOnTheWayIn(unittest.TestCase):
    """``event`` reaches a comment on the forge.

    The investigation tool redacts because its prose is re-rendered on a card;
    here the same prose is published, so credentials AND this machine's identity
    must both be gone before the write, not before the render.
    """

    def test_a_credential_quoted_into_a_progress_line_is_not_published(self):
        captured, _ = _record(
            event="log had AKIAIOSFODNN7EXAMPLE in it",
            event_kind="investigate",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["event"]

    def test_a_credential_in_the_pass_reason_is_not_stored(self):
        """``why`` is what the shared skip index keeps as the reason.

        Its audience grew: it is no longer only the crew page, it is the line every
        other crew in the repository reads before deciding not to investigate an
        issue. It takes ``redact`` rather than the stricter public sanitizer because
        it stays on this machine — but a credential quoted out of a log must not be
        the thing that gets persisted and re-read.
        """
        captured, _ = _record(
            phase="skipped",
            skip_scope="needs-decision",
            why="the failing config had AKIAIOSFODNN7EXAMPLE hardcoded",
            event="passed: needs a decision on the credential",
            event_kind="skip",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["why"]

    def test_the_home_directory_is_scrubbed_from_a_progress_line(self):
        # Redaction alone covers credentials and exfil URLs, NOT paths — and a
        # path is the thing this line must not publish.
        from pathlib import Path

        home = str(Path.home())
        captured, _ = _record(
            event=f"ran the gate in {home}/src/project",
            event_kind="implement",
        )
        assert home not in captured["body"]["event"]
        assert "<home>" in captured["body"]["event"]

    def test_the_kirocrew_home_collapses_before_the_user_home(self):
        # Longest-first ordering: scrubbing the home first would leave
        # "<home>/.kiro/crew/..." — still this machine's directory layout.
        from kiro_crew.config.loader import config_dir

        captured, _ = _record(
            event=f"read the ledger under {config_dir()}/workspace",
            event_kind="investigate",
        )
        assert str(config_dir()) not in captured["body"]["event"]

    def test_the_hostname_is_scrubbed_from_a_progress_line(self):
        with patch.object(mcp_core.socket, "gethostname", return_value="dev-dsk-example-1a"):
            captured, _ = _record(
                event="the build only fails on dev-dsk-example-1a",
                event_kind="ci",
            )
        assert "dev-dsk-example-1a" not in captured["body"]["event"]
        assert "<host>" in captured["body"]["event"]

    def test_a_short_hostname_is_left_alone(self):
        # Substring-replacing a short hostname corrupts ordinary prose, which is
        # a worse outcome than a name the brief already forbids writing.
        with patch.object(mcp_core.socket, "gethostname", return_value="dev"):
            captured, _ = _record(event="dev server restarted", event_kind="implement")
        assert captured["body"]["event"] == "dev server restarted"

    def test_ordinary_prose_is_left_alone(self):
        captured, _ = _record(
            phase="awaiting-ci",
            event="CI round 3 — 41/47 green, 6 inherited from main",
            event_kind="ci",
            next="rerun the flaky Windows shard",
        )
        body = captured["body"]
        assert body["event"] == "CI round 3 — 41/47 green, 6 inherited from main"
        assert body["next"] == "rerun the flaky Windows shard"

    def test_the_local_worktree_path_is_not_scrubbed(self):
        # The pass that removes the home from a public line must not touch the
        # field whose whole purpose is to carry it.
        from pathlib import Path

        worktree = f"{Path.home()}/src/project"
        captured, _ = _record(worktree=worktree, event="checked out", event_kind="implement")
        assert captured["body"]["worktree"] == worktree

    def test_a_label_carrying_a_credential_is_not_persisted(self):
        captured, _ = _record(labels_applied=["crew: in progress", "AKIAIOSFODNN7EXAMPLE"])
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["labels_applied"][1]

    def test_removing_the_last_label_is_forwarded_as_an_empty_list(self):
        """`labels_applied: []` means "I now hold none", not "I did not say".

        REGRESSION: the field was forwarded only when the list was non-empty, so
        the one report that matters — a crew that has just taken its last label
        OFF the issue — was indistinguishable from a call that never mentioned
        labels. The store treats absence as "leave as-is", so the record went on
        claiming labels the crew had already removed from the forge, and the
        release of a claim is exactly what other crews read to decide the issue
        is free.
        """
        captured, _ = _record(labels_applied=[])
        assert "labels_applied" in captured["body"], (
            "an emptied label set was dropped, so the store keeps the stale one"
        )
        assert captured["body"]["labels_applied"] == []

    def test_omitting_labels_entirely_still_leaves_the_field_alone(self):
        """The other half of the distinction: silence must stay silence.

        Forwarding `[]` for a call that never mentioned labels would erase the
        crew's real labels on every unrelated progress update.
        """
        captured, _ = _record(event="pushed", event_kind="implement")
        assert "labels_applied" not in captured["body"]

    def test_the_verified_identity_is_the_one_sent_on_the_wire(self):
        """The gate's key must be the request's key — not a second resolution.

        REGRESSION: the tool proved a strict identity and then called helpers that
        resolved one AGAIN through the lenient ``/proc`` ancestor walk. That is a
        check-then-use split across two different sources: whatever the walk
        answered at request time went on the wire, so a caller whose PID chain
        resolves differently after the check would have its read and its WRITE
        attributed to another crew — disclosing or corrupting that crew's ledger.
        Both legs are asserted because the read is the write's precondition.
        """
        captured, _ = _record()
        assert captured["get_session_key"] == "crew-c_7f3a", (
            "the precondition read re-resolved its own identity"
        )
        assert captured["put_session_key"] == "crew-c_7f3a", (
            "the ledger write re-resolved its own identity"
        )


class TestRecordOutput(unittest.TestCase):
    def test_a_skip_is_confirmed_back_with_the_scope_that_was_STORED(self):
        """The crew must see that the shared index took its pass.

        Recording ``phase: skipped`` is itself what stops every other crew in the
        repository re-investigating the issue, so a silent success leaves the one
        guarantee that prevents the fleet looping invisible to the only party that
        can act on it.

        The STORED scope is echoed, not the argument: an unrecognised scope is
        filed as ``other``, and a crew that believed it recorded ``architecture``
        has to see what was actually kept.
        """
        captured, out = _record(
            phase="skipped",
            skip_scope="architecture",
            # A phase write must carry its reason (schema invariant).
            event="passed: needs the retry loop moved, which changes three callers",
            event_kind="skip",
            put_result={
                "item": {"phase": "skipped"},
                # The store coerced the scope — the reply must show that.
                "skip": {"number": 12, "scope": "other"},
            },
        )
        assert "Shared skip index: #12" in out
        assert "`other`" in out
        assert "architecture" not in out.split("Shared skip index")[1]

    def test_no_skip_line_when_the_write_did_not_index_anything(self):
        _, out = _record(
            phase="implementing", event="starting the fix", event_kind="implement"
        )
        assert "Shared skip index" not in out

    def test_echoes_the_stored_event_not_the_argument(self):
        # If a sanitizer pass changed the line, the crew must see what actually
        # became public — otherwise it believes it published something else.
        cleaned = _clean(event="pushed round 3", event_kind="ci")
        with patch.object(
            mcp_core, "_resolve_session_key_strict", return_value="crew-c_7f3a"
        ):
            with patch.object(mcp_core, "_get", return_value=CREW_PAYLOAD):
                with patch.object(
                    mcp_core,
                    "_put",
                    return_value={"item": {"phase": "awaiting-ci"}, "event": {"text": "stored line"}},
                ):
                    out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
            assert "stored line" in out
            assert "awaiting-ci" in out

    def test_reports_the_next_step_back_so_a_resume_can_be_trusted(self):
        _, out = _record(next="add the Windows branch to _safe_chmod")
        assert "add the Windows branch to _safe_chmod" in out


class TestReadOutput(unittest.TestCase):
    def _read(self, payload):
        with patch.object(
            mcp_core, "_resolve_session_key_strict", return_value="crew-c_7f3a"
        ), patch.object(mcp_core, "_get", return_value=payload):
            return mcp_core._call_tool_inner(READ_TOOL, {})

    def test_returns_the_crew_settings_and_open_items(self):
        out = json.loads(self._read(CREW_PAYLOAD))
        assert out["crew"]["name"] == "Whirlpool"
        assert out["settings"]["claim_ttl_hours"] == 48
        assert out["open_items"][0]["number"] == 12
        assert out["counts"]["open"] == 1

    def test_bounds_the_event_log_to_the_NEWEST_events_chronologically(self):
        """The window must contain the newest events, oldest-first inside it.

        This test previously built its fixture oldest-first, which is the opposite
        of what ``crew_store.read_events`` returns ("Newest first" — it walks the
        append-only log in reverse). Against that unrealistic fixture the old
        ``events[-N:]`` looked right, so the test went green while the real code
        handed every crew past its first N events the N OLDEST ones — labelled by
        ``recent_events_note`` as the newest.

        So the fixture here is newest-first, matching the route, and the assertions
        pin both ends of the window: the newest event must be IN it, which is what
        the bug lost, and the order inside must be chronological, which is what the
        crew reads it as.
        """
        payload = dict(CREW_PAYLOAD)
        # Newest first, exactly as the route hands them over.
        payload["events"] = [{"text": f"line {i}"} for i in range(59, -1, -1)]
        out = json.loads(self._read(payload))
        window = out["recent_events"]

        assert len(window) == mcp_core._CREW_MAX_EVENTS
        # The newest event is present and last: the window is chronological.
        assert window[-1]["text"] == "line 59"
        assert window[0]["text"] == f"line {60 - mcp_core._CREW_MAX_EVENTS}"
        # And the oldest events are the ones dropped, not the newest.
        assert not any(e["text"] == "line 0" for e in window)
        assert "60" in out["recent_events_note"]

    def test_a_non_crew_session_is_told_so(self):
        out = self._read({"crew": {}, "items": []})
        assert out.startswith("Error:")

    def test_a_gateway_error_is_surfaced(self):
        out = self._read({"error": "not connected"})
        assert out.startswith("Error:")
        assert "not connected" in out

    def test_the_shared_skip_index_reaches_the_crew_intact(self):
        # The membership list must survive the projection COMPLETE: trimming it
        # here would answer "not skipped" for an issue that is, which is the
        # duplicated investigation the index removes.
        out = json.loads(self._read(CREW_PAYLOAD))
        assert out["skipped_numbers"] == [4, 91]
        assert out["recent_skips"][0]["scope"] == "needs-design"

    def test_an_older_payload_without_the_skip_fields_still_reads(self):
        # A gateway that predates the index answers without these keys; the
        # projection must degrade to "nothing known skipped", not KeyError.
        payload = {k: v for k, v in CREW_PAYLOAD.items()
                   if k not in ("skipped_numbers", "recent_skips")}
        out = json.loads(self._read(payload))
        assert out["skipped_numbers"] == []
        assert out["recent_skips"] == []

    def test_the_worktree_path_survives_the_read(self):
        # The resume needs it; output redaction removes credentials, not paths.
        payload = dict(CREW_PAYLOAD)
        payload["items"] = [
            {
                "crew_id": "c_7f3a",
                "owner": "acme",
                "repo": "widget",
                "number": 12,
                "worktree": "/home/user/src/project",
            }
        ]
        out = json.loads(self._read(payload))
        assert out["open_items"][0]["worktree"] == "/home/user/src/project"


class TestMiddlewareDecision:
    """Drive the real auth middleware over the crew paths.

    Set membership alone would still pass if the prefix matcher changed, so
    assert the decision: a credential-less call is refused, the tools' call is
    granted, and the forge-write routes stay closed.
    """

    @staticmethod
    def _request(path: str, method: str, headers: dict | None = None, remote: str = "127.0.0.1"):
        req = MagicMock(spec=web.Request)
        req.path = path
        req.query = {}
        req.cookies = {}
        req.remote = remote
        req.headers = headers or {}
        req.method = method
        return req

    def _mw(self, secret: str = "s3cret"):
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        return token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
            internal_secret=secret,
        )

    @pytest.mark.asyncio
    async def test_a_credentialless_read_is_refused(self):
        resp = await self._mw()(self._request(READ_PATH, "GET"), _ok_handler)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_credentialless_write_is_refused(self):
        resp = await self._mw()(self._request(WORK_PATH, "PUT"), _ok_handler)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_both_tool_calls_are_granted(self):
        for path, method in ((READ_PATH, "GET"), (WORK_PATH, "PUT")):
            resp = await self._mw()(
                self._request(path, method, headers={"X-Internal-Secret": "s3cret"}),
                _ok_handler,
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_loopback_with_the_secret_is_refused(self):
        # The secret is a machine-local handshake and a forwarder can make
        # remote traffic look loopback, so a genuinely remote peer never gets in.
        resp = await self._mw()(
            self._request(
                WORK_PATH, "PUT", headers={"X-Internal-Secret": "s3cret"}, remote="10.0.0.1"
            ),
            _ok_handler,
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_forge_write_routes_are_not_reachable_with_the_secret(self):
        for path, method in (
            ("/api/apps/issue-radar/labels/apply", "POST"),
            ("/api/apps/issue-radar/issue/close", "POST"),
            ("/api/apps/issue-radar/comment", "POST"),
        ):
            resp = await self._mw()(
                self._request(path, method, headers={"X-Internal-Secret": "s3cret"}),
                _ok_handler,
            )
            assert resp.status == 403

    @staticmethod
    def _real_request(path: str, method: str, headers: dict):
        """A genuine request object, for the chained middleware + handler test.

        The ``MagicMock`` above cannot be used there: ``request.get("internal_auth")``
        on a mock is truthy for EVERY request, so the app's agent gate would refuse
        the credential-less control case too and the test would pass without the
        middleware ever having granted anything.

        The transport is stubbed only to give the request a loopback ``remote`` —
        ``make_mocked_request`` leaves it ``None``, which the middleware reads as a
        non-loopback peer and denies before any handler runs.
        """
        transport = MagicMock()
        transport.get_extra_info.side_effect = lambda name, default=None: (
            ("127.0.0.1", 51234) if name == "peername" else default
        )
        return make_mocked_request(method, path, headers=headers, transport=transport)

    @pytest.mark.asyncio
    async def test_the_crew_sibling_routes_are_not_reachable_with_the_secret(self):
        """``/crew/pause`` and ``PUT``/``DELETE /crew`` are closed.

        The middleware ADMITS all three: its allowlist is matched
        ``path == p or path.startswith(p + "/")``, so the ``/crew`` entry covers the
        whole segment, and the entries carry no method, so no allowlist edit could
        separate ``GET /crew`` from ``PUT``/``DELETE /crew``. The refusal is the
        app's, at the handler, which is the only layer that sees the method.

        Chained here — real middleware in front of the real registered handler — so
        the assertion is about end-to-end reachability rather than about either
        layer's own opinion. Without it, an agent holding the internal secret could
        pause or retire a crew.

        ``/crew/guidance`` used to be in this list. It is not merely closed now, it
        is GONE: a crew never waits for a human, so there is no guidance to inject.
        Asserting a deleted route is refused would pass for the wrong reason — a
        route that does not exist cannot be registered, so the lookup below would
        fail before the gate was ever consulted. The deletion is asserted separately.
        """
        from kiro_crew.apps.builtins.issue_radar.backend import crew_routes, routes

        app = web.Application()
        crew_routes.register_crew_routes(app)
        handlers = {
            (route.method, str(route.resource.canonical)): route.handler
            for route in app.router.routes()
        }
        closed = (
            ("/api/apps/issue-radar/crew/pause", "POST"),
            ("/api/apps/issue-radar/crew", "PUT"),
            ("/api/apps/issue-radar/crew", "DELETE"),
        )
        # is_app_enabled patched True so a 403 can only be the agent gate's — the
        # app-disabled gate answers 403 as well and would fake every assertion here.
        with patch.object(routes, "is_app_enabled", return_value=True):
            for path, method in closed:
                req = self._real_request(path, method, {"X-Internal-Secret": "s3cret"})
                resp = await self._mw()(req, handlers[(method, path)])
                assert resp.status == 403, f"{method} {path} was reachable"
                assert json.loads(resp.body)["code"] == "agent_route_denied"

            # Control: the read leg IS reachable, so the gate is discriminating by
            # method and path rather than refusing every secret-bearing call.
            req = self._real_request(READ_PATH, "GET", {"X-Internal-Secret": "s3cret"})
            resp = await self._mw()(req, handlers[("GET", READ_PATH)])
            assert json.loads(resp.body).get("code") != "agent_route_denied"

    @pytest.mark.asyncio
    async def test_the_guidance_route_no_longer_exists_at_all(self):
        """Not closed — absent.

        A crew never holds an issue waiting for a person, so there is nothing for a
        human to inject guidance into. Asserting this by registration rather than by
        response code is deliberate: a 403 or a 404 would also be produced by a route
        that exists and refuses, and "the endpoint is gone" is the property that
        actually stops anyone building on it again.
        """
        from kiro_crew.apps.builtins.issue_radar.backend import crew_routes

        app = web.Application()
        crew_routes.register_crew_routes(app)
        paths = {str(route.resource.canonical) for route in app.router.routes()}
        assert not any(p.endswith("/crew/guidance") for p in paths), sorted(paths)
