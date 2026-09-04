"""The agent's write path into an Issue Radar investigation record.

Regression suite for the 403 that made the whole findings feature dead: the
Investigate seed prompt told the agent to ``PUT
/api/apps/issue-radar/investigation``, but that path was in NEITHER the auth
bypass sets NOR the internal-path sets, and an agent session has no dashboard
credential to satisfy the cookie path with:

  * the access cookie is httpOnly (the frontend cannot hand it over),
  * ``KIROCREW_INTERNAL_SECRET`` is stripped from agent env by
    ``sandbox._AGENT_DENIED_ENV_KEYS``,
  * ``.local_secret`` — needed for the documented ``GET /api/token/local``
    bootstrap — is on the ``security.py`` sensitive-path denylist.

So the PUT returned ``403 {"error": "Token required"}`` every time and no
investigation record ever stored ``findings``. The fix routes the write through
the ``issue_radar_record_investigation`` MCP tool, whose server process holds
the internal secret legitimately.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_core
from kiro_crew.apps.builtins.issue_radar.backend import routes as ir_routes
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.validation import MCP_CORE_SCHEMAS, ValidationError, validate_tool_args

TOOL = "issue_radar_record_investigation"
RECORD_PATH = "/api/apps/issue-radar/investigation"


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


class TestGatewayPathIsReachableWithTheInternalSecret(unittest.TestCase):
    """Without this entry the tool's PUT gets the same 403 the prompt did."""

    def test_record_path_is_a_mixed_internal_path(self):
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        assert RECORD_PATH in _MIXED_INTERNAL_API_PATHS

    def test_the_apps_prefix_is_not_admitted_wholesale(self):
        """Only the record route — never the app's forge-write routes.

        ``token_auth`` prefix-matches these entries, so admitting
        ``/api/apps/issue-radar`` would also hand ``/labels/apply``,
        ``/issue/close`` and the comment routes to anything holding the internal
        secret. Those write to GitHub/GitLab; the record route is local triage
        state only.
        """
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        for entry in _MIXED_INTERNAL_API_PATHS | _STRICT_INTERNAL_API_PATHS:
            assert entry != "/api/apps"
            assert entry != "/api/apps/issue-radar"


class TestToolRegistration(unittest.TestCase):
    def test_schema_is_registered_so_bad_args_do_not_crash_the_server(self):
        # An unregistered tool's internal ValidationError escapes the stdio loop
        # and kills the whole kirocrew-core server (see test_mcp_core_arg_crash).
        assert TOOL in MCP_CORE_SCHEMAS
        required = {f.name for f in MCP_CORE_SCHEMAS[TOOL].fields if f.required}
        # provider/host/kind join owner/repo/number as required: they pick the
        # storage namespace, so a default would silently cross providers.
        assert required == {"owner", "repo", "number", "provider", "host", "kind"}

    def test_tool_is_advertised(self):
        listed = {t["name"] for t in mcp_core._list_tools()}
        assert TOOL in listed

    def test_description_steers_away_from_a_raw_http_call(self):
        spec = next(t for t in mcp_core._list_tools() if t["name"] == TOOL)
        assert "403" in spec["description"]


class TestArgValidation(unittest.TestCase):
    def _ok(self, **over):
        args = {
            "owner": "acme",
            "repo": "widget",
            "number": 7,
            "provider": "github",
            "host": "github.com",
            "kind": "issue",
        }
        args.update(over)
        return validate_tool_args(args, MCP_CORE_SCHEMAS[TOOL])

    def test_defaults_a_resolved_status(self):
        cleaned = self._ok()
        assert cleaned["status"] == "resolved"

    def test_requires_the_identity_that_selects_the_storage_namespace(self):
        # provider/host/kind must never default. owner/repo/provider/host pick
        # the record's directory (store.provider_root), so a GitLab call that
        # omitted them would land in the GitHub ledger and overwrite a
        # same-slug GitHub investigation — a GitLab group can share a name with
        # a GitHub owner.
        for missing in ("provider", "host", "kind"):
            args = {
                "owner": "acme",
                "repo": "widget",
                "number": 7,
                "provider": "gitlab",
                "host": "gitlab.acme.internal",
                "kind": "pull",
            }
            del args[missing]
            with pytest.raises(ValidationError):
                validate_tool_args(args, MCP_CORE_SCHEMAS[TOOL])

    def test_keeps_a_self_managed_gitlab_identity_intact(self):
        cleaned = self._ok(provider="gitlab", host="gitlab.acme.internal", kind="pull")
        assert cleaned["provider"] == "gitlab"
        assert cleaned["host"] == "gitlab.acme.internal"
        assert cleaned["kind"] == "pull"

    def test_rejects_an_unknown_provider(self):
        with pytest.raises(ValidationError):
            self._ok(provider="bitbucket")

    def test_rejects_an_unknown_status(self):
        with pytest.raises(ValidationError):
            self._ok(status="done")

    def test_rejects_a_number_that_would_blow_up_the_filename(self):
        # The number becomes part of investigation-<n>.json.
        with pytest.raises(ValidationError):
            self._ok(number=10**12)

    def test_rejects_a_non_positive_number(self):
        with pytest.raises(ValidationError):
            self._ok(number=0)

    def test_rejects_a_boolean_number(self):
        # bool is an int subclass; True would otherwise record against #1.
        with pytest.raises(ValidationError):
            self._ok(number=True)


def _call_tool_capturing_put(**over):
    """Invoke the tool with ``_put`` stubbed; return (captured_request, output).

    Module-level rather than a base-class method: a test class that inherited it
    would also inherit — and therefore re-run — that class's own tests.
    """
    args = {"owner": "acme", "repo": "widget", "number": 7, "provider": "github", "host": "github.com", "kind": "issue"}
    args.update(over)
    cleaned = validate_tool_args(args, MCP_CORE_SCHEMAS[TOOL])
    captured: dict = {}

    def fake_put(path, body=None):
        captured["path"] = path
        captured["body"] = body
        return {"investigation": {"findings": body.get("findings")}}

    with patch.object(mcp_core, "_put", side_effect=fake_put):
        out = mcp_core._call_tool_inner(TOOL, cleaned)
    return captured, out


class TestToolBody(unittest.TestCase):
    """What the tool actually PUTs."""

    def _call(self, **over):
        return _call_tool_capturing_put(**over)

    def test_targets_the_record_route(self):
        captured, _ = self._call()
        assert captured["path"] == RECORD_PATH

    def test_assembles_the_findings_object_from_the_flat_args(self):
        captured, _ = self._call(
            verdict="bug",
            root_cause="stale gate in execution.py",
            suggested_labels=["bug", "area:apps", "bug"],
            next_action="fix the gate",
            summary="one paragraph",
        )
        findings = captured["body"]["findings"]
        assert findings["verdict"] == "bug"
        assert findings["root_cause"] == "stale gate in execution.py"
        assert findings["next_action"] == "fix the gate"
        assert findings["summary"] == "one paragraph"
        assert findings["suggested_labels"] == ["bug", "area:apps", "bug"]

    def test_omits_empty_fields_so_a_merge_cannot_clobber_them(self):
        # The store merges findings per key and reads an empty value as "leave
        # alone" (store._merge_findings), so a partial call must send only what
        # it is actually asserting.
        captured, _ = self._call(verdict="bug")
        assert captured["body"]["findings"] == {"verdict": "bug"}

    def test_sends_no_findings_key_at_all_for_a_status_only_update(self):
        captured, out = self._call(status="investigating")
        assert "findings" not in captured["body"]
        assert "investigating" in out

    def test_always_sends_provider_host_and_kind_explicitly(self):
        # Leaving them to the server default records a GitLab item into a
        # same-slug GitHub repo's ledger.
        captured, _ = self._call(provider="gitlab", host="gitlab.acme.internal", kind="pull")
        body = captured["body"]
        assert body["provider"] == "gitlab"
        assert body["host"] == "gitlab.acme.internal"
        assert body["kind"] == "pull"

    def test_surfaces_a_gateway_error_instead_of_claiming_success(self):
        cleaned = validate_tool_args(
            {"owner": "acme", "repo": "widget", "number": 7, "provider": "github", "host": "github.com", "kind": "issue"}, MCP_CORE_SCHEMAS[TOOL]
        )
        with patch.object(mcp_core, "_put", return_value={"error": "not connected"}):
            out = mcp_core._call_tool_inner(TOOL, cleaned)
        assert out.startswith("Error:")
        assert "not connected" in out

    def test_reports_a_gitlab_merge_request_with_bang_notation(self):
        _, out = self._call(provider="gitlab", host="gitlab.com", kind="pull", verdict="bug")
        assert "!7" in out


class TestFindingsAreRedactedBeforePersisting(unittest.TestCase):
    """Findings are LLM prose about UNTRUSTED issue text, stored verbatim and
    re-rendered on the item's card — so they go through the canonical redaction
    passes on the way IN, not only on the way out."""

    def _call(self, **over):
        return _call_tool_capturing_put(**over)

    def test_a_credential_quoted_into_a_finding_is_not_persisted(self):
        captured, _ = self._call(
            root_cause="the issue pasted AKIAIOSFODNN7EXAMPLE into the log",
            summary="aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        )
        findings = captured["body"]["findings"]
        assert "AKIAIOSFODNN7EXAMPLE" not in findings["root_cause"]
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in findings["summary"]

    def test_a_credential_in_a_label_is_not_persisted(self):
        captured, _ = self._call(suggested_labels=["AKIAIOSFODNN7EXAMPLE"])
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["findings"]["suggested_labels"][0]

    def test_ordinary_prose_is_left_alone(self):
        # Redaction must not mangle a legitimate finding: only suspicious URLs
        # and credential patterns are touched.
        captured, _ = self._call(
            verdict="bug",
            root_cause="off-by-one in _parse_item_number",
            summary="See https://github.com/kirodotdev/KiroCrew/issues/1039 for the thread.",
        )
        findings = captured["body"]["findings"]
        assert findings["root_cause"] == "off-by-one in _parse_item_number"
        assert "github.com/kirodotdev/KiroCrew/issues/1039" in findings["summary"]


class TestMiddlewareDecision:
    """Drive the real auth middleware over the record path.

    Set membership alone would still pass if the middleware's prefix matcher
    changed, so assert the actual decision: the credential-less call the seed
    prompt used to ask for is refused, and the tool's internal-secret call is
    granted.
    """

    @staticmethod
    def _request(headers: dict | None = None, remote: str = "127.0.0.1"):
        req = MagicMock(spec=web.Request)
        req.path = RECORD_PATH
        req.query = {}
        req.cookies = {}
        req.remote = remote
        req.headers = headers or {}
        req.method = "PUT"
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
    async def test_a_credentialless_put_is_still_refused(self):
        # This is the exact 403 the Investigate prompt used to earn. Admitting
        # the path for the internal secret must NOT have opened it up generally.
        resp = await self._mw()(self._request(), _ok_handler)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_wrong_secret_is_refused(self):
        resp = await self._mw()(
            self._request(headers={"X-Internal-Secret": "wrong"}), _ok_handler
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_tools_call_is_granted(self):
        resp = await self._mw()(
            self._request(headers={"X-Internal-Secret": "s3cret"}), _ok_handler
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_loopback_with_the_secret_is_refused(self):
        # local_only default: the secret is a machine-local handshake, and a port
        # forwarder can make remote traffic look like 127.0.0.1 — so a genuinely
        # non-loopback peer never gets in on the secret alone.
        resp = await self._mw()(
            self._request(headers={"X-Internal-Secret": "s3cret"}, remote="10.0.0.1"),
            _ok_handler,
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_forge_write_route_is_not_reachable_with_the_secret(self):
        # The companion to the prefix test above, at the middleware level.
        req = self._request(headers={"X-Internal-Secret": "s3cret"})
        req.path = "/api/apps/issue-radar/labels/apply"
        req.method = "POST"
        resp = await self._mw()(req, _ok_handler)
        assert resp.status == 403


class TestPutHandlerBoundsTheItemNumber:
    """The write route now enforces the same bound the read routes do."""

    async def _put(self, body: dict):
        request = make_mocked_request("PUT", RECORD_PATH)

        async def _json():
            return body

        request.json = _json  # type: ignore[method-assign]
        return await ir_routes._handle_put_investigation(request)

    @pytest.mark.asyncio
    async def test_rejects_an_absurd_number_before_touching_the_store(self):
        # Unbounded, this became investigation-<huge>.json — an ENAMETOOLONG
        # write, not merely a miss. Checked before the connected-repo gate, so
        # no store fixture is needed.
        resp = await self._put({"owner": "acme", "repo": "widget", "number": 10**12})
        assert resp.status == 400
        assert str(ir_routes.MAX_ITEM_NUMBER) in resp.text

    @pytest.mark.asyncio
    async def test_accepts_the_boundary_value(self):
        # At the bound the number check passes, so the request proceeds and is
        # stopped by the connected-repo gate instead (404, not 400).
        resp = await self._put(
            {"owner": "acme", "repo": "widget", "number": ir_routes.MAX_ITEM_NUMBER}
        )
        assert resp.status == 404
