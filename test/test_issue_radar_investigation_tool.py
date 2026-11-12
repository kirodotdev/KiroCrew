"""The agent's write path into an Issue Radar investigation record.

An agent session holds no dashboard credential (the cookie is httpOnly, the internal
secret is never in its env, ``.local_secret`` is denylisted), so a raw ``PUT`` gets 403.
The write goes through the ``issue_radar_record_investigation`` agent tool: the app's
``app.json`` declares it, the ``kirocrew-core`` MCP server (which holds the internal
secret) forwards a validated call, and the app's ``backend/agent_tools.py`` writes the
record in-process. ``issue_radar_agent_tools_helpers`` drives that path.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner
from issue_radar_agent_tools_helpers import INVESTIGATION_TOOL as TOOL
from issue_radar_agent_tools_helpers import call, clean, descriptor, forwarded, listed, shipped

from kiro_crew import mcp_core
from kiro_crew.apps.builtins.issue_radar.backend import routes as ir_routes
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.validation import ValidationError

RECORD_PATH = "/api/apps/issue-radar/investigation"


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


class TestTheRecordRouteIsNotAdmittedToTheInternalSecret(unittest.TestCase):
    """The tool writes in-process from the app's own route; no app path is admitted."""

    def test_no_issue_radar_path_is_an_internal_path(self):
        """Not the record route, and never the app's forge-write routes.

        ``token_auth`` prefix-matches, so admitting ``/api/apps/issue-radar`` would also
        open ``/labels/apply``, ``/issue/close`` and the comment routes, which write to
        the forge.
        """
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        for entry in _MIXED_INTERNAL_API_PATHS | _STRICT_INTERNAL_API_PATHS:
            assert entry != "/api/apps"
            assert not (entry + "/").startswith("/api/apps/issue-radar/"), entry


class TestToolRegistration(unittest.TestCase):
    def test_the_declaration_requires_the_identity_that_selects_the_record(self):
        # provider/host/kind join owner/repo/number as required: they pick the
        # storage namespace, so a default would silently cross providers.
        assert set(descriptor(TOOL)["inputSchema"]["required"]) == {
            "owner",
            "repo",
            "number",
            "provider",
            "host",
            "kind",
        }
        # A call from a subagent may record its parent's investigation, as it could.
        assert shipped(TOOL).decl.strict_session is False

    def test_tool_is_advertised(self):
        with listed():
            names = {t["name"] for t in mcp_core._list_tools()}
        assert TOOL in names

    def test_description_steers_away_from_a_raw_http_call(self):
        assert "403" in descriptor(TOOL)["description"]


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
        return clean(TOOL, args)

    def test_defaults_a_resolved_status(self):
        captured, _ = _call_tool_capturing_put()
        assert captured["body"]["status"] == "resolved"

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
                clean(TOOL, args)

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
    """Invoke the tool through the core with the record write stubbed.

    Returns (captured, output); ``captured["body"]`` is what the write was handed.
    Module-level rather than a base-class method: a test class that inherited it
    would also inherit — and therefore re-run — that class's own tests.
    """
    args = {"owner": "acme", "repo": "widget", "number": 7, "provider": "github", "host": "github.com", "kind": "issue"}
    args.update(over)
    cleaned = clean(TOOL, args)

    def fake_write(body):
        return {"investigation": {"findings": body.get("findings")}}

    with forwarded(investigation=fake_write) as captured:
        out = call(TOOL, cleaned)
    return captured, out


class TestToolBody(unittest.TestCase):
    """What the tool actually PUTs."""

    def _call(self, **over):
        return _call_tool_capturing_put(**over)

    def test_is_forwarded_to_the_app_with_no_session_requirement(self):
        captured, _ = self._call()
        assert captured["post"]["path"] == "/api/app-tools/call"
        assert captured["post"]["body"]["tool"] == TOOL
        assert captured["post"]["session_key"] is None

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
        cleaned = clean(
            TOOL,
            {"owner": "acme", "repo": "widget", "number": 7, "provider": "github", "host": "github.com", "kind": "issue"},
        )
        with forwarded(investigation={"error": "not connected"}):
            out = call(TOOL, cleaned)
        assert out.startswith("Error:")
        assert "not connected" in out

    def test_reports_a_gitlab_merge_request_with_bang_notation(self):
        _, out = self._call(provider="gitlab", host="gitlab.com", kind="pull", verdict="bug")
        assert "!7" in out


class TestTheToolsAnswer:
    """What the tool writes and says for the minimal argument set and the edge shapes."""

    _BASE = {"owner": "o", "repo": "r", "number": 7}

    @staticmethod
    def _run(args: dict, saved: dict | None = None):
        with forwarded(investigation=saved if saved is not None else {"investigation": {}}) as cap:
            out = call(TOOL, args)
        return cap, out

    def test_identity_fields_are_sent_explicitly_with_defaults(self) -> None:
        captured, out = self._run(dict(self._BASE))
        assert captured["body"] == {
            "owner": "o",
            "repo": "r",
            "number": 7,
            "provider": "github",
            "host": "github.com",
            "kind": "issue",
            "status": "resolved",
        }
        assert out.startswith("Recorded status `resolved` for o/r#7")
        assert "status badge only" in out

    def test_empty_finding_fields_are_dropped_so_a_partial_update_keeps_prior_text(self) -> None:
        args = dict(self._BASE, verdict="real bug", root_cause="", summary="", next_action="fix")
        saved = {"investigation": {"findings": {"verdict": "real bug"}}}
        captured, out = self._run(args, saved)
        assert set(captured["body"]["findings"]) == {"verdict", "next_action"}
        assert "verdict `real bug`" in out

    def test_findings_and_labels_are_redacted_on_the_way_in(self) -> None:
        args = dict(
            self._BASE,
            verdict="leaks AKIAIOSFODNN7EXAMPLE",
            suggested_labels=["", "bug AKIAIOSFODNN7EXAMPLE"],
        )
        captured, _ = self._run(args)
        findings = captured["body"]["findings"]
        assert "AKIAIOSFODNN7EXAMPLE" not in findings["verdict"]
        # The falsy label is dropped, the redacted one survives.
        assert len(findings["suggested_labels"]) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in findings["suggested_labels"][0]

    def test_a_gitlab_merge_request_is_referenced_with_a_bang(self) -> None:
        args = dict(self._BASE, provider="gitlab", host="gitlab.com", kind="pull", verdict="ok")
        _, out = self._run(args, {"investigation": {"findings": {"verdict": "ok"}}})
        assert "for o/r!7:" in out

    def test_a_github_pull_request_keeps_the_hash_form(self) -> None:
        args = dict(self._BASE, kind="pull", verdict="ok")
        _, out = self._run(args, {"investigation": {"findings": {"verdict": "ok"}}})
        assert "for o/r#7:" in out

    def test_saved_findings_without_a_verdict_are_labelled(self) -> None:
        _, out = self._run(
            dict(self._BASE, summary="s"), {"investigation": {"findings": {"summary": "s"}}}
        )
        assert "verdict `(no verdict)`" in out

    def test_a_backend_error_is_surfaced(self) -> None:
        _, out = self._run(dict(self._BASE), {"error": "HTTP 409"})
        assert out == "Error: HTTP 409"


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
    prompt asks for is refused, and so is a direct internal-secret call: the
    tool writes in-process.
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
        # This is the exact 403 the Investigate prompt earns. Admitting
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
    async def test_the_secret_alone_does_not_open_the_route(self):
        resp = await self._mw()(
            self._request(headers={"X-Internal-Secret": "s3cret"}), _ok_handler
        )
        assert resp.status == 403

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
        request.app["state"] = NoConfiguredOwner()
        request["user"] = "local-app"
        request["app"] = ""

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


class TestTheToolWritesThroughTheRealRoute:
    """The tool's write is the route's own writer, with its gates, in-process."""

    @pytest.mark.asyncio
    async def test_a_record_for_a_repo_that_is_not_connected_is_refused(self):
        from kiro_crew.apps.builtins.issue_radar.backend import agent_tools

        request = make_mocked_request("POST", "/api/app-tools/call")
        request["internal_auth"] = True
        out = await agent_tools.record_investigation(
            request,
            {"owner": "acme", "repo": "widget", "number": 7, "provider": "github",
             "host": "github.com", "kind": "issue"},
        )
        assert out == "Error: acme/widget is not connected — call /connect first"
