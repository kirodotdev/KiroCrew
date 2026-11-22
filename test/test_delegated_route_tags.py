"""Every dashboard route carries an explicit delegated ``allow`` / ``deny`` tag.

A token minted through the ``dashboard_proxy`` grant (``via_app``) reaches only the routes
tagged ``allow``; a route with no tag is DENIED at runtime. This walk fails on any route
of the real table that no tag names, so a new route is tagged on purpose, and on a tag
that names no route, so the table cannot rot. It also pins the routes the spec names as
refused, whatever else the table says.
"""

from __future__ import annotations

import inspect
import re

import pytest
from aiohttp import web

from kiro_crew.dashboard.routes import register_all


def _table() -> web.Application:
    """Every route the gateway mounts: the page table and the MCP tool routes."""
    from kiro_crew.dashboard.server import _register_mcp_routes

    app = web.Application()
    _register_mcp_routes(app)
    register_all(app)
    return app


def _walk() -> list[tuple[str, str]]:
    app = _table()
    pairs = []
    for route in app.router.routes():
        resource = route.resource
        assert resource is not None
        pairs.append((route.method, resource.canonical))
    return pairs


def _tag(method: str, canonical: str) -> str | None:
    from kiro_crew.dashboard.delegated_scope import route_tag

    return route_tag(method, canonical)


def _allowed(method: str, canonical: str) -> bool:
    from kiro_crew.dashboard.delegated_scope import allowed

    return allowed(method, canonical)


def test_no_read_route_of_the_table_is_untagged() -> None:
    untagged = sorted(
        {
            f"{m} {p}"
            for m, p in _walk()
            if m in ("GET", "HEAD") and _tag(m, p) not in ("allow", "deny")
        }
    )
    assert not untagged, "read routes with no delegated allow/deny tag:\n" + "\n".join(untagged)


def test_the_tag_table_holds_read_entries_only() -> None:
    """A write is decided by ALLOW_WRITES alone; a write tag would be a second copy."""
    from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS

    extra = sorted(f"{m} {p}" for p, tags in ROUTE_TAGS.items() for m in tags if m != "GET")
    assert not extra, "non-GET tag entries:\n" + "\n".join(extra)


def test_no_tag_names_a_route_that_does_not_exist() -> None:
    from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS

    real = {p for _m, p in _walk()}
    stale = sorted(set(ROUTE_TAGS) - real)
    assert not stale, "tags for routes that are not registered:\n" + "\n".join(stale)


def test_an_unlisted_route_has_no_tag() -> None:
    assert _tag("GET", "/api/a-route-nobody-tagged") is None
    assert _tag("POST", "/api/status") is None, "a method the table does not list"


_REFUSED = [
    ("GET", "/api/token/local"),
    ("POST", "/api/auth/mobile-link"),
    ("POST", "/api/apps/{name}/token"),
    ("POST", "/api/apps/register"),
    ("POST", "/api/webhooks/tokens"),
    ("POST", "/api/relay-viewer/token"),
    ("POST", "/api/apps/install"),
    ("POST", "/api/apps/{name}/enable"),
    ("POST", "/api/apps/{name}/disable"),
    ("POST", "/api/apps/{name}/uninstall"),
    ("POST", "/api/apps/{name}/update"),
    ("PUT", "/api/apps/{name}/bot-grants"),
    ("PUT", "/api/config/kirocrew"),
    ("POST", "/api/security/tool-grants"),
    ("POST", "/api/security/tool-grants/remove"),
    ("POST", "/v1/crew/owner/approvals/{approval_id}/decision"),
    ("POST", "/v1/crew/owner/grant-requests/{request_id}/decision"),
    ("DELETE", "/v1/crew/owner/grants/{grant_id}"),
    ("POST", "/api/chat/mode"),
    ("POST", "/api/restart"),
    ("POST", "/api/update/approve"),
    # Terminal and approvals are local-only (consistent with the owner approval routes).
    ("POST", "/api/terminal/sessions"),
    ("POST", "/api/terminal/external"),
    ("POST", "/api/terminal/complete"),
    ("POST", "/api/approvals/{id}/{action}"),
    ("POST", "/api/chat/slots/{slot}/approve"),
    ("POST", "/api/channels/{id}/agents/{aid}/approve"),
    # Agent, skill and theme code that runs on the host.
    ("PUT", "/api/agent/config"),
    ("PUT", "/api/agents/b64/{name_b64}/capabilities"),
    ("POST", "/api/agents/b64/{name_b64}/capabilities/detach"),
    ("PUT", "/api/agents/b64/{name_b64}/worker-kit"),
    ("PUT", "/api/agents/{name}/worker-kit"),
    ("PUT", "/api/agents/{name}/capabilities"),
    ("POST", "/api/agents/{name}/capabilities/detach"),
    # Agent definitions (tools, MCP servers, permissions), themes and skills: code and
    # grants that run on the host.
    ("POST", "/api/agents"),
    ("PUT", "/api/agents"),
    ("PUT", "/api/agents/{name}"),
    ("PATCH", "/api/agents/detail/{name}"),
    ("POST", "/api/themes"),
    ("PUT", "/api/themes/{slug}"),
    ("POST", "/api/skills"),
    ("PUT", "/api/skills/{name}"),
    # A terminal is a shell on the host.
    ("GET", "/api/ws/terminal/{session_id}"),
    ("DELETE", "/api/terminal/sessions/{session_id}"),
    ("POST", "/api/terminal/redact"),
    ("POST", "/api/skills/-/trust"),
    ("DELETE", "/api/skills/-/trust"),
    ("POST", "/api/skills/-/pending/{slug}/approve"),
    ("POST", "/api/skills/-/discover/install"),
    ("POST", "/api/themes/install"),
    ("POST", "/api/themes/install-zip"),
    ("PUT", "/api/chat/slots/{slot}/environment"),
    ("PUT", "/api/chat/slots/{slot}/environment/file"),
    # Linking a chat to an outside account.
    ("POST", "/api/chat/slots/{name}/slack-link"),
    ("POST", "/api/chat/slots/{slot}/slack-link"),
    ("POST", "/api/chat/slots/{name}/mirror-link"),
    ("POST", "/api/chat/slots/{slot}/mirror-link"),
    ("PUT", "/api/notifications/channels/settings"),
    ("POST", "/api/wakatime/oauth/authorize"),
    ("POST", "/api/wakatime/oauth/disconnect"),
    # Chat is read-only through the tunnel: a message or a new slot starts an agent turn
    # (or a slot a turn can start in) that runs as the owner.
    ("POST", "/api/chat"),
    ("POST", "/api/chat/slots"),
]

#: Routes that serve host file content, list a directory outside the app, or export a
#: whole install or the memory. Each one is ``deny`` for a delegated token.
_FILE_AND_EXPORT_READS = [
    "/api/file-read",
    "/api/file-raw",
    "/api/file-download",
    "/api/file-diff",
    "/api/file-office-preview",
    "/api/file-office-slide",
    "/api/file-office-slides",
    "/api/file-search",
    "/api/file-sheet",
    "/api/file-stream",
    "/api/file-watch",
    "/api/browse-files",
    "/api/browse-dirs",
    "/api/path-complete",
    "/api/outbox/{filename}",
    "/api/diagnostics/download/{filename}",
    "/api/logs",
    "/api/project/tree",
    "/api/project/git",
    "/api/project/git/branch-diff",
    "/api/project/git/log",
    "/api/project/git/repos",
    "/api/project/git/status",
    "/api/project/session-files",
    "/api/project/session-file-diff",
    "/api/chat/slots/{slot}/environment/browse",
    "/api/skills/{name}/-/file",
    "/api/skills/{name}/-/tree",
    "/api/apps/blob",
    "/api/apps/papyrus/file",
    "/api/apps/papyrus/files",
    "/api/apps/papyrus/git",
    "/api/apps/papyrus/pdf",
    "/api/apps/spec-builder/browse",
    "/api/apps/aws-control/drive/{account}",
    "/api/apps/aws-control/drive/{account}/download",
    "/api/apps/aws-control/drive/{account}/list",
    "/api/apps/aws-control/drive/{account}/preview",
    "/api/apps/aws-control/drive/{account}/search",
    "/api/portability/export",
    "/api/memory/provider/export",
    "/api/wakatime/export",
]


@pytest.mark.parametrize("canonical", _FILE_AND_EXPORT_READS)
def test_the_file_read_and_export_routes_are_denied(canonical: str) -> None:
    assert ("GET", canonical) in set(_walk()), "the route this test pins does not exist"
    assert _tag("GET", canonical) == "deny"
    assert _allowed("GET", canonical) is False


#: A path segment that names a file read, a listing or an export.
_CONTENT_SEGMENT = re.compile(
    r"(^|[/-])(file|files|export|download|browse|raw|blob|tree|outbox|git|pdf|watch|logs)($|[/-])"
)
#: A handler that hands back file bytes.
_CONTENT_SOURCE = re.compile(r"FileResponse|read_bytes\(|sendfile|\.read_text\(")


def test_a_get_that_serves_files_or_an_export_is_denied_or_reviewed() -> None:
    """Fail closed: an allowed GET that looks like a file read, a listing or an export
    must be listed in ``CONTENT_READ_REVIEWED`` with the reason it reaches only data the
    caller may see, or it is ``deny``."""
    from kiro_crew.dashboard.delegated_scope import CONTENT_READ_REVIEWED

    hits: set[str] = set()
    for route in _table().router.routes():
        if route.method != "GET":
            continue
        canonical = route.resource.canonical  # type: ignore[union-attr]
        if _tag("GET", canonical) != "allow":
            continue
        looks = bool(_CONTENT_SEGMENT.search(canonical))
        if not looks:
            try:
                source = inspect.getsource(inspect.unwrap(route.handler))
            except (OSError, TypeError):
                continue
            looks = bool(_CONTENT_SOURCE.search(source))
        if looks:
            hits.add(canonical)
    unreviewed = sorted(hits - set(CONTENT_READ_REVIEWED))
    assert not unreviewed, "allowed GETs that serve files or an export, with no reason:\n" + (
        "\n".join(unreviewed)
    )
    stale = sorted(set(CONTENT_READ_REVIEWED) - hits)
    assert not stale, "reviewed content reads that no longer match:\n" + "\n".join(stale)
    assert all(len(reason) > 20 for reason in CONTENT_READ_REVIEWED.values())


#: The write routes a delegated token may call. A row here is a security decision.
_EXPECTED_ALLOW_WRITES = {
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/logout"),
    ("*", "/apps/{name}/api/{path}"),
    ("*", "/app-frame-fetch/{name}/{path}"),
    ("POST", "/api/chat/slots/{slot}/stop"),
    ("POST", "/api/chat/slots/{slot}/interrupt"),
    ("POST", "/api/chat/pinned-order"),
    ("POST", "/api/chat/folders/reorder"),
    ("POST", "/api/chat/manual-order"),
}


def test_allow_writes_is_exactly_the_reviewed_set() -> None:
    """Any row whose handler can start an agent turn, queue a message or run a tool needs
    a security review before it is added here: adding one is a security decision."""
    from kiro_crew.dashboard.delegated_scope import ALLOW_WRITES

    assert set(ALLOW_WRITES) == _EXPECTED_ALLOW_WRITES


#: Write routes that start a turn, queue or edit a message, run a tool or a job, or
#: start an agent. None is open to a delegated token (the F4-4 list of the PR body).
_TURN_STARTERS = [
    ("POST", "/api/chat"),
    ("POST", "/api/chat/slots"),
    ("POST", "/api/chat/slots/{slot}/regenerate"),
    ("POST", "/api/chat/slots/{slot}/edit-resend"),
    ("POST", "/api/chat/slots/{slot}/continue"),
    ("POST", "/api/chat/slots/{slot}/limit-continue"),
    ("POST", "/api/chat/slots/{slot}/resume"),
    ("POST", "/api/chat/slots/resume-all"),
    ("POST", "/api/chat/slots/{slot}/followup"),
    ("POST", "/api/chat/slots/{slot}/rewind"),
    ("POST", "/api/chat/slots/{slot}/fork"),
    ("POST", "/api/chat/slots/{slot}/side/turn"),
    ("POST", "/api/chat/slots/{slot}/terminal-run"),
    ("POST", "/api/chat/slots/{slot}/question/answer"),
    ("POST", "/api/chat/slots/{slot}/question/steer"),
    ("POST", "/api/chat/slots/{slot}/crew-workers/{worker}/steer"),
    ("POST", "/api/chat/slots/{slot}/switch-variant"),
    ("POST", "/api/chat/threads/{mid}/reply"),
    ("PATCH", "/api/chat/slots/{slot}/queue/{queue_id}"),
    ("PUT", "/api/chat/slots/{slot}/queue/order"),
    ("PUT", "/api/chat/slots/{slot}/steering"),
    ("POST", "/api/steering"),
    ("POST", "/api/send-message"),
    ("POST", "/api/session-control/send"),
    ("POST", "/api/session-control/queue"),
    ("POST", "/api/session-control/run-queued"),
    ("POST", "/api/session-control/retry"),
    ("POST", "/api/session-control/broadcast"),
    ("POST", "/api/instances/{id}/send-session"),
    ("POST", "/api/channels/{id}/messages"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/api/crons"),
    ("POST", "/api/crons/{job_id}/run"),
    ("POST", "/api/crons/{job_id}/to-chat"),
    ("POST", "/api/voice/synthesize"),
    ("POST", "/api/work-ledger/record"),
    ("POST", "/api/work-ledger/report"),
    ("POST", "/api/work-ledger/rebuild"),
    ("POST", "/api/spawn"),
    ("POST", "/api/spawn/{agent_id}/steer"),
    ("POST", "/api/spawn/{agent_id}/retry"),
    ("POST", "/api/taskrunner"),
    ("POST", "/api/taskrunner/{task_id}/execute"),
    ("POST", "/api/workflows/run"),
    ("POST", "/api/workflows/author"),
    ("POST", "/api/autonudge"),
    ("POST", "/api/autonudge/{loop_id}/fire"),
    ("POST", "/api/monitors"),
    ("POST", "/api/apps/meetings/meetings/{meeting_id}/dispatch"),
    ("POST", "/api/apps/spec-builder/specs/{name}/message"),
    ("POST", "/api/apps/ops-mission-control/dispatch"),
]


@pytest.mark.parametrize("method,canonical", _TURN_STARTERS)
def test_no_turn_starting_write_is_open_to_a_delegated_token(method: str, canonical: str) -> None:
    assert (method, canonical) in set(_walk()), "the route this test pins does not exist"
    assert _allowed(method, canonical) is False


@pytest.mark.parametrize("method,canonical", _REFUSED)
def test_the_credential_and_control_plane_routes_are_denied(method: str, canonical: str) -> None:
    assert (method, canonical) in set(_walk()), "the route this test pins does not exist"
    assert _allowed(method, canonical) is False
    if method == "GET":
        assert _tag(method, canonical) == "deny"


@pytest.mark.parametrize(
    "method,canonical",
    [
        ("GET", "/api/status"),
        ("GET", "/api/sessions"),
        ("POST", "/api/chat/slots/{slot}/stop"),
        ("POST", "/api/chat/slots/{slot}/interrupt"),
    ],
)
def test_the_plain_dashboard_routes_are_allowed(method: str, canonical: str) -> None:
    assert _allowed(method, canonical) is True


def test_every_route_sharing_a_handler_and_method_shares_a_tag() -> None:
    app = _table()
    groups: dict[tuple[object, str], set[tuple[str, str | None]]] = {}
    for route in app.router.routes():
        resource = route.resource
        assert resource is not None
        if route.method == "*":
            continue
        tag = _tag(route.method, resource.canonical)
        groups.setdefault((route.handler, route.method), set()).add((resource.canonical, tag))
    split = sorted(
        f"{method} {sorted(pairs)}"
        for (_h, method), pairs in groups.items()
        if len({tag for _p, tag in pairs}) > 1
    )
    assert not split, "one handler, two tags (deny must win on every spelling):\n" + "\n".join(
        split
    )


def test_every_b64_route_carries_the_tag_of_its_name_twin() -> None:
    from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS

    real = {p for _m, p in _walk()}
    checked = 0
    for canonical, tags in ROUTE_TAGS.items():
        if "/b64/{name_b64}" not in canonical:
            continue
        twin = canonical.replace("/b64/{name_b64}", "/{name}")
        if twin not in real:
            continue
        for method, tag in tags.items():
            if method in ROUTE_TAGS[twin]:
                assert ROUTE_TAGS[twin][method] == tag, f"{method} {canonical} vs {twin}"
                checked += 1
    assert checked


def test_a_star_route_is_open_to_every_concrete_method_only_where_listed() -> None:
    star = sorted({p for m, p in _walk() if m == "*"})
    assert "/apps/{name}/api/{path}" in star
    from kiro_crew.dashboard.delegated_scope import ALLOW_WRITES

    for canonical in star:
        listed = ("*", canonical) in ALLOW_WRITES
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            if method in ("GET", "HEAD"):
                assert _allowed(method, canonical) == (_tag(method, canonical) == "allow")
            else:
                assert _allowed(method, canonical) is listed, f"{method} {canonical}"


_READS = ("GET", "HEAD")


def _write_routes() -> list[tuple[str, str]]:
    return sorted({(m, p) for m, p in _walk() if m not in _READS})


def test_a_delegated_token_may_write_only_where_allow_writes_says() -> None:
    """Writes are deny by default: a tag alone never allows one."""
    from kiro_crew.dashboard.delegated_scope import ALLOW_WRITES, write_allowed

    walked = set(_write_routes())
    open_writes = sorted(f"{m} {p}" for m, p in walked if write_allowed(m, p))
    listed = sorted(
        f"{m} {p}" for m, p in walked if (m, p) in ALLOW_WRITES or ("*", p) in ALLOW_WRITES
    )
    assert open_writes == listed
    assert not write_allowed("POST", "/api/file-write")
    assert not write_allowed("POST", "/api/a-route-nobody-tagged")


def test_allow_writes_is_the_day_one_list_with_an_effect_note_each() -> None:
    from kiro_crew.dashboard.delegated_scope import ALLOW_WRITES

    real = set(_walk())
    star = {p for m, p in real if m == "*"}
    for (method, canonical), note in ALLOW_WRITES.items():
        assert method in ("POST", "PUT", "PATCH", "DELETE", "*"), (method, canonical)
        assert (method, canonical) in real or (method == "*" and canonical in star), canonical
        assert len(note) > 20 and "\n" not in note, canonical
    assert len(ALLOW_WRITES) <= 14
    forbidden = (
        "file",
        "config",
        "credential",
        "install",
        "grant",
        "approv",
        "terminal",
        "consent",
        "provider",
        "mcp",
        "shar",
        "egress",
        "secret",
        "token",
    )
    for method, canonical in ALLOW_WRITES:
        if canonical.startswith(("/apps/", "/app-frame-fetch/")):
            continue
        assert not any(word in canonical for word in forbidden), canonical


_CREDENTIAL_KEY = re.compile(
    r"""["']([A-Za-z0-9_]*(secret|token|password|passwd|credential|cookie|authorization"""
    r"""|api_?key|private_?key|access_?key)[A-Za-z0-9_]*)["']""",
    re.IGNORECASE,
)


def test_a_get_that_names_a_credential_field_is_denied_or_reviewed() -> None:
    """A GET whose handler names a secret-like key is denied unless listed with a reason."""
    from kiro_crew.dashboard.delegated_scope import READ_REVIEWED

    hits: dict[str, list[str]] = {}
    for route in _table().router.routes():
        if route.method != "GET":
            continue
        canonical = route.resource.canonical  # type: ignore[union-attr]
        if _tag("GET", canonical) != "allow":
            continue
        try:
            source = inspect.getsource(inspect.unwrap(route.handler))
        except (OSError, TypeError):
            continue
        names = sorted({m.group(1) for m in _CREDENTIAL_KEY.finditer(source)})
        if names:
            hits[canonical] = names
    unreviewed = sorted(set(hits) - set(READ_REVIEWED))
    assert not unreviewed, "GETs naming a credential field, neither denied nor reviewed:\n" + (
        "\n".join(f"{p} {hits[p]}" for p in unreviewed)
    )
    stale = sorted(set(READ_REVIEWED) - set(hits))
    assert not stale, "reviewed reads that no longer name a credential field:\n" + "\n".join(stale)
    assert all(len(reason) > 20 for reason in READ_REVIEWED.values())


@pytest.mark.parametrize(
    "canonical",
    [
        "/api/mcp/custom/{name}",
        "/api/connections/mint",
        "/api/config/kirocrew",
        "/api/webhooks",
        "/api/slack/config",
        "/api/discord/config",
        "/api/telegram/config",
        "/api/webex/config",
        "/api/wecom/config",
        "/api/feishu/config",
        "/api/teams/config",
    ],
)
def test_the_stored_credential_reads_are_denied(canonical: str) -> None:
    assert ("GET", canonical) in set(_walk()), "the route this test pins does not exist"
    assert _tag("GET", canonical) == "deny"


def test_no_secrets_route_is_allowed_to_a_delegated_token() -> None:
    open_secrets = [
        f"{m} {p}" for m, p in _walk() if p.startswith("/api/secrets") and _tag(m, p) == "allow"
    ]
    assert not open_secrets
