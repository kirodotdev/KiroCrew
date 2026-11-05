"""A third-party MCP tool's message-body field is scanned as a document.

``security.mcp_body_fields`` names ``(server, tool, field)`` rows: the reviewed
defaults plus rows the dashboard owner allowed. For a call whose server AND tool
identity are trusted, a listed field skips the shell matcher and the deny-rule
list, keeps the path, credential and exfiltration-URL detectors, and has the
256 KiB ceiling instead of 20 KiB. Everything else keeps the full scan, and a
field refused for size names the tool and field and is remembered for the
owner's allow button.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.llm_helpers import (
    _MAX_SCANNABLE_TOOL_INPUT_CHARS,
    ToolApprovalPolicy,
    _resolve_permission,
)
from kiro_crew.llm_helpers_parts import tool_input_denials
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.security import mcp_body_fields as mbf

_SERVER = "gmail"
_TOOL = "send_email"
_HTML_ROW = '<tr><td style="padding:4px">Quarterly item</td><td>Status: on track</td></tr>\n'


def _html(size: int) -> str:
    return (_HTML_ROW * (size // len(_HTML_ROW) + 1))[:size]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(mbf, "store_path", lambda: tmp_path / "mcp_body_fields.json")
    with mbf._REFUSALS_LOCK:
        mbf._REFUSALS.clear()
    yield
    with mbf._REFUSALS_LOCK:
        mbf._REFUSALS.clear()


class _Provider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _event(
    params: dict, *, server: str = _SERVER, tool: str = _TOOL, identity: bool = True
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=f"@{server}/{tool}",
        request_id="r1",
        tool_kind="other",
        tool_input=json.dumps(params),
        raw_tool_params=params,
        raw_params_trusted=True,
        shell_classified=True,
        is_shell=False,
        mcp_identity_trusted=identity,
        mcp_server_name=server,
        tool_name=tool,
    )


async def _resolve(event: LLMEvent) -> tuple[bool, str]:
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            _Provider(), event, ToolApprovalPolicy.AUTO_APPROVE, None  # type: ignore[arg-type]
        )
    return approved, " ".join(str(r.get("error") or "") for r in rows)


def _mail(body: str, **extra: str) -> dict:
    return {"to": "team@example.com", "subject": "Weekly report", "body": body, **extra}


class TestABodyFieldIsADocument:
    @pytest.mark.asyncio
    async def test_a_200_kib_body_is_allowed_fast_and_never_reaches_the_shell_matcher(
        self,
    ) -> None:
        shell = MagicMock(return_value=None)
        deny = MagicMock(return_value=None)
        with (
            patch.object(tool_input_denials, "is_sensitive_bash_command", shell),
            patch.object(tool_input_denials, "is_denied", deny),
        ):
            started = time.monotonic()
            approved, error = await _resolve(_event(_mail(_html(200 * 1024))))
            elapsed = time.monotonic() - started
        assert approved is True, error
        assert elapsed < 10
        subjects = [c.args[0] for c in shell.call_args_list + deny.call_args_list]
        assert all(len(s) < 1024 for s in subjects), [len(s) for s in subjects]

    @pytest.mark.asyncio
    async def test_a_credential_in_the_body_is_refused(self) -> None:
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        approved, error = await _resolve(_event(_mail(f"<p>token: {secret}</p>")))
        assert approved is False
        assert "credential" in error

    @pytest.mark.asyncio
    async def test_a_credential_path_in_the_body_is_refused(self) -> None:
        # The path tier judges a string that IS a path, as for the Kiro Crew rows.
        approved, error = await _resolve(_event(_mail("~/.aws/credentials")))
        assert approved is False
        assert "sensitive path" in error

    @pytest.mark.asyncio
    async def test_a_body_over_256_kib_is_refused(self) -> None:
        approved, error = await _resolve(_event(_mail(_html(mbf.MAX_BODY_FIELD_CHARS + 1))))
        assert approved is False
        assert "too large" in error


class TestEverythingElseKeepsTheFullScan:
    @pytest.mark.asyncio
    async def test_an_untrusted_server_with_the_same_names_keeps_the_20_kib_refusal(
        self,
    ) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event(_mail(body), server="other-mail"))
        assert approved is False
        assert f"> {_MAX_SCANNABLE_TOOL_INPUT_CHARS} limit" in error

    @pytest.mark.asyncio
    async def test_an_untrusted_identity_keeps_the_20_kib_refusal(self) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event(_mail(body), identity=False))
        assert approved is False
        assert "too large" in error
        assert mbf.size_refusals() == []

    @pytest.mark.asyncio
    async def test_a_non_body_field_is_still_size_capped(self) -> None:
        subject = "s" * (_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event(_mail("<p>hi</p>", subject=subject)))
        assert approved is False
        assert "'subject' field of @gmail/send_email" in error

    @pytest.mark.asyncio
    async def test_a_non_body_field_is_still_command_scanned(self) -> None:
        approved, _error = await _resolve(_event(_mail("<p>hi</p>", subject="rm -rf /")))
        assert approved is False


class TestTheSizeRefusalIsNamedAndOffered:
    @pytest.mark.asyncio
    async def test_an_unlisted_field_names_the_tool_and_field_and_is_recorded(self) -> None:
        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, error = await _resolve(_event({"to": "a@example.com", "html": body}))
        assert approved is False
        assert "'html' field of @gmail/send_email" in error
        assert "Settings > Security" in error
        assert [(r["server"], r["tool"], r["field"]) for r in mbf.size_refusals()] == [
            (_SERVER, _TOOL, "html")
        ]

    @pytest.mark.asyncio
    async def test_the_size_refusal_links_to_the_settings_section(self) -> None:
        from kiro_crew.llm_helpers_parts import tool_approval

        body = _html(_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        with patch.object(
            tool_approval, "_body_field_link_origin", return_value="https://crew.example.com"
        ):
            approved, error = await _resolve(_event({"to": "a@example.com", "html": body}))
        assert approved is False
        assert "'html' field of @gmail/send_email" in error
        assert (
            "Settings > Security > Message body fields "
            "(https://crew.example.com/settings/security/bodyFields)"
        ) in error
        assert "token=" not in error
        assert "kirocrew " not in error

    def test_without_a_dashboard_origin_the_refusal_names_the_section_only(self) -> None:
        reason = mbf.size_refusal_reason("mail", "send", "html", 30000, 20480)
        assert "'html' field of @mail/send" in reason
        assert reason.endswith("under Settings > Security > Message body fields")
        assert "http" not in reason

    def test_the_link_origin_follows_the_dashboard_url(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from kiro_crew.config import loader
        from kiro_crew.llm_helpers_parts import tool_approval

        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(url="http://127.0.0.1:8765/"),
            slack=SimpleNamespace(use_tunnel_url=False),
        )
        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        assert tool_approval._body_field_link_origin() == "http://127.0.0.1:8765"
        monkeypatch.setattr(
            loader.KiroCrewConfig, "load", classmethod(lambda cls: (_ for _ in ()).throw(OSError))
        )
        assert tool_approval._body_field_link_origin() == ""

    @pytest.mark.asyncio
    async def test_an_owner_row_lets_the_field_through(self) -> None:
        body = _html(60 * 1024)
        params = {"to": "a@example.com", "html": body}
        approved, _ = await _resolve(_event(params, server="mail", tool="mail___send"))
        assert approved is False
        mbf.add_owner_row(mbf.BodyField("mail", "send", "html"))
        assert mbf.size_refusals() == []
        approved, error = await _resolve(_event(params, server="mail", tool="mail___send"))
        assert approved is True, error
        assert mbf.remove_owner_row(mbf.BodyField("mail", "send", "html")) is True
        approved, _ = await _resolve(_event(params, server="mail", tool="mail___send"))
        assert approved is False


class TestTheStore:
    def test_a_malformed_store_reads_as_no_rows(self, tmp_path) -> None:
        (tmp_path / "mcp_body_fields.json").write_text("{not json", encoding="utf-8")
        assert mbf.owner_rows() == []

    def test_invalid_rows_are_dropped_on_read(self, tmp_path) -> None:
        rows = [
            {"server": "mail", "tool": "send", "field": "html"},
            {"server": "mail", "tool": "send", "field": "a.b"},
            {"server": "", "tool": "send", "field": "html"},
        ]
        (tmp_path / "mcp_body_fields.json").write_text(json.dumps({"fields": rows}))
        assert [r["field"] for r in mbf.owner_rows()] == ["html"]

    def test_a_default_row_is_not_stored_twice(self) -> None:
        assert mbf.add_owner_row(mbf.DEFAULT_BODY_FIELDS[0]) is False
        assert mbf.owner_rows() == []

    def test_the_store_leaf_is_fenced(self) -> None:
        from kiro_crew import sandbox
        from kiro_crew.security.paths_lists import _CREW_SECRET_LEAVES

        assert "mcp_body_fields.json" in _CREW_SECRET_LEAVES
        assert "mcp_body_fields.json" in sandbox._CREW_READONLY_LEAVES
        assert "mcp_body_fields.json" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


class TestAnHtmlMailIsNotACredential:
    @pytest.mark.asyncio
    async def test_an_inline_image_is_allowed(self) -> None:
        import base64
        import hashlib

        blob = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(256))
        image = base64.b64encode(blob).decode()
        body = f'<p>Chart:</p><img src="data:image/png;base64,{image}">' + _html(30 * 1024)
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is True, error

    @pytest.mark.asyncio
    async def test_a_bare_key_in_an_html_body_is_refused(self) -> None:
        # Only the entropy guess finds this one: no fixed-format prefix.
        assert not mbf._contains_fixed_credential(_BARE_KEY)
        body = f"<p>Use this key: {_BARE_KEY}</p>" + _html(30 * 1024)
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is False
        assert "credential" in error

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "template",
        [
            '<img src="data:image/png;base64,{image}" alt="{key}">',
            '<img src="data:image/png;base64,{image}">{key}',
            '<a href="#">{key}</a><img src="data:image/png;base64,{image}">',
        ],
    )
    async def test_a_key_next_to_an_inline_image_is_still_refused(self, template) -> None:
        body = template.format(image=_image(), key=_BARE_KEY) + _html(30 * 1024)
        approved, error = await _resolve(_event(_mail(body)))
        assert approved is False
        assert "credential" in error


_BARE_KEY = "Kx3Q51tPusV/D0URlGfM" "mNbVc7Z8yJhLpQrStUwZ"


def _image() -> str:
    import base64
    import hashlib

    blob = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(256))
    return base64.b64encode(blob).decode()


# --------------------------------------------------------------- the routes


def _request(*, method: str = "POST", user: str = "owner-1", body=None, query=None):
    from unittest.mock import AsyncMock

    req = MagicMock()
    req.method = method
    req.path = "/api/security/mcp-body-fields"
    store = {"app": "", "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.query = query or {}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def _routes(monkeypatch):
    from kiro_crew.dashboard.handlers import mcp_body_fields as routes

    events: list[tuple[str, bool]] = []

    def _record(*, outcome, caller, detail="", critical=False):
        events.append((outcome, critical))
        return True

    monkeypatch.setattr(routes, "_audit", _record)
    monkeypatch.setattr(
        routes, "is_owner_dashboard_request", lambda req: req.get("user") == "owner-1"
    )
    monkeypatch.setattr(
        routes,
        "_owner_denial_response",
        lambda req, msg, code: routes.web.json_response({"code": code}, status=403),
    )
    return routes, events


_ROW = {"server": "mail", "tool": "send", "field": "html"}


class TestTheOwnerRoutes:
    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused_on_every_verb(self, _routes) -> None:
        routes, events = _routes
        mbf.record_size_refusal("mail", "send", "html")
        for handler, method in (
            (routes.api_mcp_body_fields_get, "GET"),
            (routes.api_mcp_body_fields_add, "POST"),
            (routes.api_mcp_body_fields_remove, "DELETE"),
        ):
            resp = await handler(_request(method=method, user="someone", body=_ROW, query=_ROW))
            assert resp.status == 403
        assert mbf.owner_rows() == []
        assert [e[0] for e in events] == ["denied", "denied", "denied"]

    @pytest.mark.asyncio
    async def test_only_a_refused_field_can_be_allowed(self, _routes) -> None:
        routes, _events = _routes
        resp = await routes.api_mcp_body_fields_add(_request(body=_ROW))
        assert resp.status == 409
        assert mbf.owner_rows() == []

    @pytest.mark.asyncio
    async def test_the_owner_allows_and_removes_a_refused_field(self, _routes) -> None:
        routes, events = _routes
        mbf.record_size_refusal("mail", "send", "html")
        listing = json.loads((await routes.api_mcp_body_fields_get(_request(method="GET"))).text)
        assert listing["refused"][0]["field"] == "html"
        resp = await routes.api_mcp_body_fields_add(_request(body=_ROW))
        assert resp.status == 200
        listing = json.loads(resp.text)
        assert [r["field"] for r in listing["owner"]] == ["html"]
        assert listing["refused"] == []
        assert ("added", True) in events
        resp = await routes.api_mcp_body_fields_remove(_request(method="DELETE", query=_ROW))
        assert resp.status == 200
        assert mbf.owner_rows() == []

    @pytest.mark.asyncio
    async def test_an_add_whose_audit_fails_does_not_happen(self, _routes, monkeypatch) -> None:
        routes, _events = _routes
        monkeypatch.setattr(routes, "_audit", lambda **kw: False)
        mbf.record_size_refusal("mail", "send", "html")
        resp = await routes.api_mcp_body_fields_add(_request(body=_ROW))
        assert resp.status == 503
        assert mbf.owner_rows() == []

    @pytest.mark.asyncio
    async def test_a_malformed_row_is_refused(self, _routes) -> None:
        routes, _events = _routes
        resp = await routes.api_mcp_body_fields_add(_request(body={**_ROW, "field": "a.b"}))
        assert resp.status == 400

    def test_no_mcp_tool_writes_the_store(self) -> None:
        from pathlib import Path

        import kiro_crew

        pkg = Path(kiro_crew.__file__).resolve().parent
        writers = {
            f.relative_to(pkg).as_posix()
            for f in pkg.rglob("*.py")
            if "add_owner_row(" in f.read_text(encoding="utf-8")
        }
        assert writers == {
            "dashboard/handlers/mcp_body_fields.py",
            "security/mcp_body_fields.py",
        }, writers
