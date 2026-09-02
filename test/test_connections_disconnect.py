"""Disconnect's local half: revoking the grant artifacts kiro-cli stored.

The slice these cover exists because removing the MCP entry alone left a usable
refresh token on disk, so a later reconnect resumed a grant the user believed was
gone. Every assertion here is about that gap or about telling the truth when the
removal only partly succeeds.

Boundary these tests also pin: the artifacts are stat-ed and unlinked, never
opened, so no token material can enter the process.
"""

from __future__ import annotations

import os
import pathlib
from urllib.parse import urlsplit

import pytest

from kiro_crew import mcp_grant
from kiro_crew.connections.tool_aliases import normalized_endpoint

_URL = "https://mcp.notion.com/mcp"


def _write_grant(directory: pathlib.Path, url: str = _URL) -> tuple[pathlib.Path, pathlib.Path]:
    """Lay down the paired artifacts kiro-cli writes for a granted provider."""
    token, registration = mcp_grant.grant_artifact_paths(url, cache_dir=directory)
    token.write_text("{}", encoding="utf-8")
    registration.write_text("{}", encoding="utf-8")
    return token, registration


def test_revoke_unlinks_both_paired_artifacts(tmp_path: pathlib.Path) -> None:
    token, registration = _write_grant(tmp_path)
    assert mcp_grant.grant_presence(_URL, cache_dir=tmp_path) is True

    removed = mcp_grant.revoke_local_grant(_URL, cache_dir=tmp_path)

    assert sorted(removed) == ["registration", "token"]
    assert not token.exists()
    assert not registration.exists()
    assert mcp_grant.grant_presence(_URL, cache_dir=tmp_path) is False


def test_revoke_is_idempotent_on_a_provider_with_no_grant(tmp_path: pathlib.Path) -> None:
    assert mcp_grant.revoke_local_grant(_URL, cache_dir=tmp_path) == []
    assert mcp_grant.surviving_grant_artifacts(_URL, cache_dir=tmp_path) == []


def test_revoke_leaves_the_aws_sso_single_file_form_alone(tmp_path: pathlib.Path) -> None:
    """The cache directory mixes in SSO's ``{sha256}.json``; it is not ours."""
    _write_grant(tmp_path)
    sso = tmp_path / f"{mcp_grant.grant_key(_URL)}.json"
    sso.write_text("{}", encoding="utf-8")

    mcp_grant.revoke_local_grant(_URL, cache_dir=tmp_path)

    assert sso.exists(), "revoke deleted an AWS SSO artifact it does not own"


def test_surviving_names_the_artifact_left_behind(tmp_path: pathlib.Path) -> None:
    token, _registration = mcp_grant.grant_artifact_paths(_URL, cache_dir=tmp_path)
    token.write_text("{}", encoding="utf-8")

    assert mcp_grant.surviving_grant_artifacts(_URL, cache_dir=tmp_path) == ["token"]


def test_a_half_removed_grant_is_reported_not_claimed_done(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honesty regression: an artifact that cannot be unlinked is surfaced.

    Reporting only what came off would let Disconnect delete the token, leave the
    registration behind, and still answer "done" -- which is the state a later
    reconnect resumes from.
    """
    _token, registration = _write_grant(tmp_path)
    real_unlink = pathlib.Path.unlink

    def refuse_registration(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        if self == registration:
            raise OSError("locked by another process")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", refuse_registration)

    removed = mcp_grant.revoke_local_grant(_URL, cache_dir=tmp_path)

    assert removed == ["token"], "only the token could be removed"
    assert mcp_grant.surviving_grant_artifacts(_URL, cache_dir=tmp_path) == ["registration"]
    assert registration.exists()


def test_revoke_never_opens_an_artifact(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grant lifecycle, not credential access: no read path may be taken."""
    _write_grant(tmp_path)

    def forbid_open(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        raise AssertionError(f"revoke opened {self.name}; it may only stat and unlink")

    monkeypatch.setattr(pathlib.Path, "open", forbid_open)
    monkeypatch.setattr(pathlib.Path, "read_text", forbid_open)
    monkeypatch.setattr(pathlib.Path, "read_bytes", forbid_open)

    assert sorted(mcp_grant.revoke_local_grant(_URL, cache_dir=tmp_path)) == [
        "registration",
        "token",
    ]


def test_labels_are_bound_to_the_right_files(tmp_path: pathlib.Path) -> None:
    """A reorder in ``grant_artifact_paths`` must not silently swap the labels."""
    token, registration = mcp_grant.grant_artifact_paths(_URL, cache_dir=tmp_path)
    labelled = dict(mcp_grant._labelled_grant_artifacts(_URL, cache_dir=tmp_path))

    assert labelled == {"token": token, "registration": registration}
    assert labelled["token"].name.endswith(".token.json")
    assert labelled["registration"].name.endswith(".registration.json")


# ── HTTP surface ──
#
# The handler imports its collaborators function-locally (the module's own
# boot-path convention), so patching them on their SOURCE modules is what takes
# effect at call time.

import contextlib  # noqa: E402
import json  # noqa: E402
import types  # noqa: E402

import pytest_asyncio  # noqa: E402  (imported for its marker plugin)
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from kiro_crew import agent as agent_mod  # noqa: E402
from kiro_crew import mcp_discovery  # noqa: E402
from kiro_crew.config import paths as connections_paths  # noqa: E402
from kiro_crew.connections import get_provider  # noqa: E402
from kiro_crew.connections import mint  # noqa: E402
from kiro_crew.connections import ownership  # noqa: E402
from kiro_crew.dashboard.handlers import connections  # noqa: E402
from kiro_crew.dashboard.handlers import mcp as mcp_handlers  # noqa: E402

_ = pytest_asyncio

_SLUG = "notion"


def _provider_url() -> str:
    provider = get_provider(_SLUG)
    assert provider is not None, "registry no longer ships the fixture provider"
    return str(provider["mcp_url"])


@contextlib.asynccontextmanager
async def _no_lock():
    yield


def _entry(name: str, url: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, url=url)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    removed: list[str],
    surviving: list[str],
    inventory: list[types.SimpleNamespace],
    purged: list[str],
    revoked: list[str] | None = None,
    audits: list[dict] | None = None,
    raw_specs: dict | None = None,
    unreadable: tuple[str, ...] = (),
    scopes: list[tuple[str, ...]] | None = None,
    real_census: bool = False,
) -> None:
    """Neutralize every side effect except the decisions under test.

    ``_offload_config_write`` is deliberately left REAL: it is the shielded wrapper
    the purge and the revoke must both go through, so letting it drive the fakes
    exercises that path rather than stubbing it away. ``raw_specs`` is the census
    the oracle reads, keyed by source label (disabled entries included); it defaults
    to mirroring the probe inventory so single-view tests stay unchanged.
    ``unreadable`` is the census's second answer, and ``scopes`` records the scope
    list each purge was restricted to.
    """
    seen_revokes = revoked if revoked is not None else []

    def _revoke(url: str) -> list[str]:
        seen_revokes.append(url)
        return list(removed)

    def _audit(**kwargs: object) -> None:
        if audits is not None:
            audits.append(dict(kwargs))

    monkeypatch.setattr(mcp_grant, "revoke_local_grant", _revoke)
    monkeypatch.setattr(mcp_grant, "surviving_grant_artifacts", lambda _url: list(surviving))

    async def _no_mint(_slug: str, _token: object) -> bool:
        return False

    monkeypatch.setattr(mint, "cancel_mint", _no_mint)
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: list(inventory))
    default_raw = {"kirocrew": {s.name: {"url": s.url} for s in inventory}}

    def _census(_project_dirs: tuple[pathlib.Path, ...] = ()) -> tuple[dict, tuple[str, ...]]:
        return (raw_specs if raw_specs is not None else default_raw, unreadable)

    if not real_census:
        monkeypatch.setattr(ownership, "spec_census", _census)
    monkeypatch.setattr(mcp_handlers, "_get_mcp_lock", _no_lock)

    def _purge(name: str, *, scopes: tuple[str, ...] | None = None) -> dict:
        purged.append(name)
        if scopes is not None:
            _purge.seen.append(tuple(scopes))  # type: ignore[attr-defined]
        return {}

    _purge.seen = scopes if scopes is not None else []  # type: ignore[attr-defined]
    monkeypatch.setattr(mcp_handlers, "_purge_server_config", _purge)
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", lambda: None)
    monkeypatch.setattr(connections, "sel", lambda: types.SimpleNamespace(log_api_access=_audit))


async def _client(
    *, owner_id: str = "owner", user: str = "owner", project: pathlib.Path | None = None
) -> TestClient:
    """App with the token-middleware identity contract the owner gate reads.

    ``request["user"]`` / ``request["app"]`` are what the real token-auth
    middleware sets; ``app["state"].owner_id`` is what
    ``is_owner_dashboard_request`` compares against. Tests default to running
    as the owner; a request carrying ``X-Test-User`` impersonates that subject,
    mirroring ``test_agents_endpoints_owner_auth``.

    ``project`` binds one open chat slot to a project directory, which is how the
    dashboard records the checkout kiro-cli runs in and therefore the only place
    the handler can learn which ``<project>/.kiro/agents`` dirs the census must
    read. ``_ChatSlot.project`` is the real attribute name.
    """
    app = web.Application()
    app["state"] = types.SimpleNamespace(
        owner_id=owner_id,
        _slots=({"main": types.SimpleNamespace(project=project)} if project is not None else {}),
    )

    @web.middleware
    async def _identity(request: web.Request, handler):  # type: ignore[no-untyped-def]
        request["user"] = request.headers.get("X-Test-User", user)
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_identity)
    app.router.add_post("/api/connections/disconnect", connections.api_connections_disconnect)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _disconnect(project: pathlib.Path | None = None) -> dict:
    client = await _client(project=project)
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": _SLUG})
        assert resp.status == 200
        return await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disconnect_revokes_the_grant_and_removes_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
    )

    body = await _disconnect()

    assert body == {
        "ok": True,
        "grantRemoved": True,
        "grantSurviving": [],
        "entryRemoved": True,
        "grantSharedWith": [],
        "grantCensusIncomplete": False,
        "grantCensusUnreadable": [],
    }
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_disconnect_response_carries_no_slug_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response reports outcome facts only, never the caller's own slug.

    ``ok`` carries the verdict and the caller already knows which slug it
    asked about, so an echo field invites a client to read identity from the
    body instead of its own request. Pinned by name so the field cannot creep
    back in as a harmless-looking addition.
    """
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
    )

    body = await _disconnect()

    assert "disconnected" not in body


@pytest.mark.asyncio
async def test_disconnect_keeps_a_grant_another_entry_still_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The data-loss guard: a grant is keyed by ENDPOINT, not by entry.

    ``grant_key`` is a sha256 over the URL alone, so one artifact pair serves every
    entry talking to that endpoint. Revoking because *our* entry went would
    deauthorize a user's own separately-named server at the same URL, and its refresh
    token is not recoverable locally.
    """
    purged: list[str] = []
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[
            _entry(_SLUG, _provider_url()),
            _entry("notion-work", _provider_url()),
        ],
        purged=purged,
        revoked=revoked,
    )

    body = await _disconnect()

    assert revoked == [], "disconnect revoked a grant another entry still uses"
    assert body["grantRemoved"] is False
    assert body["grantSharedWith"] == ["notion-work"]
    # Our own entry still comes out -- only the shared credential is spared.
    assert body["entryRemoved"] is True
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_a_shared_endpoint_is_not_reported_as_a_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifacts surviving BY DESIGN are not the same event as a failed unlink."""
    audits: list[dict] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        inventory=[
            _entry(_SLUG, _provider_url()),
            _entry("notion-work", _provider_url()),
        ],
        purged=[],
        revoked=[],
        audits=audits,
    )

    await _disconnect()

    assert audits, "the disconnect wrote no audit event"
    assert audits[-1]["outcome"] == "ok", "a deliberately kept grant audited as partial"


@pytest.mark.asyncio
async def test_a_query_string_variant_shares_the_grant_and_blocks_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grant identity is ``grant_key``, which DROPS the query string.

    ``notion-work → <registry url>?workspace=team`` is a different endpoint by
    ``normalized_endpoint`` but names the SAME artifact pair, so revoking would
    delete the grant it authenticates with. The endpoint comparator would miss it.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"notion-work": {"url": _provider_url() + "?workspace=team"}},
        },
    )

    body = await _disconnect()

    assert revoked == [], "revoked a grant a query-variant entry still uses"
    assert body["grantSharedWith"] == ["notion-work"]


@pytest.mark.asyncio
async def test_a_trailing_slash_variant_owns_a_different_grant_and_never_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverse direction: same endpoint by comparator, DIFFERENT artifact pair.

    ``grant_key`` keeps the path verbatim, so ``/mcp/`` hashes differently from
    ``/mcp``. Treating it as a sharer would skip the revoke for a grant nobody
    else holds and report the survivor as a deliberate keep -- the silent-resume
    regression this endpoint exists to close, rendered as success.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"notion-slash": {"url": _provider_url() + "/"}},
        },
    )

    body = await _disconnect()

    assert revoked == [_provider_url()], "a non-sharing variant blocked the revoke"
    assert body["grantSharedWith"] == []
    assert body["grantRemoved"] is True


@pytest.mark.asyncio
async def test_a_disabled_entry_still_counts_as_a_grant_sharer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switched-off server still owns its grant.

    ``list_servers`` drops disabled entries outside the Kiro Crew scope, so the
    sharing sweep must read the raw specs -- otherwise disabling an entry makes
    its grant deletable, and re-enabling it demands a fresh consent with nothing
    having said so.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        # The probe view sees only our entry; the disabled sharer is invisible to it.
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"notion-work": {"url": _provider_url(), "disabled": True}},
        },
    )

    body = await _disconnect()

    assert revoked == [], "revoked a grant a disabled entry still owns"
    assert body["grantSharedWith"] == ["notion-work"]


@pytest.mark.asyncio
async def test_a_malformed_config_url_neither_crashes_nor_blocks_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One junk entry must not 500 every Disconnect or disable revocation.

    ``grant_key`` raises ``ValueError`` on an unparseable port, and scope files are
    hand-edited. An unparseable URL also cannot NAME our artifact pair -- the pair
    is a hash of a successful parse, and kiro-cli's own parser rejects the same
    shapes -- so it is skipped, not counted as a sharer: counting it would let one
    junk line permanently disable the trust fix under a false "shared" message.

    The idna shapes extend the same ruling on a different premise: Python's
    ``.encode("idna")`` raises where the WHATWG parser kiro-cli uses may accept
    (empty label, >63-char label). But an accepted-over-there URL hashes to ITS OWN
    origin string, never to the registry origin's key -- and the Unicode spellings
    that could map INTO a registry host are mapped (not raised) by Python's
    nameprep too. So no grant matching our key can live at a Python-unencodable
    URL, and skip stays the safe direction; fail-closed would let the junk line
    block every disconnect instead.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {
                "template-junk": {"url": "http://localhost:${PORT}/mcp"},
                "port-overflow": {"url": "https://host:99999/mcp"},
                "bad-bracket": {"url": "https://[::1/mcp"},
                # The screen parses a STRIPPED value; the hash must hash that same
                # string. urlsplit lstrips only, so a trailing space after an
                # explicit port passes the screen and raises in a raw-string hash.
                "trailing-space": {"url": "https://host:8080 "},
                # Both pass urlsplit + the port read, then raise UnicodeError in
                # grant_key's .encode("idna") -- the screen guarantees parsing,
                # not encodability.
                "idna-empty-label": {"url": "https://a..b/mcp"},
                "idna-long-label": {"url": "https://" + "a" * 64 + ".com/mcp"},
            },
        },
    )

    body = await _disconnect()

    assert body["ok"] is True, "a malformed sibling entry 500'd the disconnect"
    assert body["grantSharedWith"] == []
    assert revoked == [_provider_url()], "junk entries blocked the revoke"


@pytest.mark.asyncio
async def test_disconnect_reports_a_survivor_instead_of_claiming_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-revoked grant is named, not rounded up to "done"."""
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token"],
        surviving=["registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
    )

    body = await _disconnect()

    assert body["grantRemoved"] is True
    assert body["grantSurviving"] == ["registration"]
    # The entry still comes out: a half-revoked grant is reason enough to stop
    # advertising the server, and the response says which half held.
    assert body["entryRemoved"] is True
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_disconnect_leaves_a_same_named_server_on_another_endpoint_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity is the endpoint, never the entry name.

    A user-added server that merely happens to be called ``notion`` is not the
    Notion card's connection. Purging on the name alone would delete their server
    because they clicked Disconnect on ours.
    """
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, "https://notion.example.internal/mcp")],
        purged=purged,
    )

    body = await _disconnect()

    assert body["entryRemoved"] is False
    assert purged == [], "disconnect deleted a server it does not own"
    # The grant is keyed on the REGISTRY url, so it is still ours to remove.
    assert body["grantRemoved"] is True


@pytest.mark.asyncio
async def test_a_custom_agent_config_sharing_the_endpoint_blocks_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 1: the census must include agent specs Kiro Crew does not own.

    Discovery merges only ``kirocrew.json``, so a spec the user wrote by hand is
    invisible to the scope sweep -- while kiro-cli authorizes from it and shares
    the one artifact pair the endpoint names. Judging ownership without it deletes
    that agent's authorization, and the refresh token is not recoverable locally.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "agent:my-research.json": {"notion-research": {"url": _provider_url()}},
        },
    )

    body = await _disconnect()

    assert revoked == [], "revoked a grant a custom agent config still uses"
    assert body["grantSharedWith"] == ["notion-research"]
    assert body["grantRemoved"] is False


@pytest.mark.asyncio
async def test_a_mismatched_entry_in_another_scope_survives_the_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 2: the purge removes only the scopes ownership actually matched.

    Ownership used to be judged from the merged winner and acted on across EVERY
    scope, so a same-named entry in a lower-priority scope pointing somewhere else
    was deleted unseen -- config the user wrote, gone because a higher scope
    happened to win under the same name.
    """
    scopes: list[tuple[str, ...]] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        scopes=scopes,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "kiroGlobal": {_SLUG: {"url": "https://notion.example.internal/mcp"}},
        },
    )

    body = await _disconnect()

    assert body["entryRemoved"] is True
    assert scopes == [("kirocrew",)], "the purge reached a scope it never judged"


@pytest.mark.asyncio
async def test_the_revoke_runs_inside_the_lock_that_judged_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 3: decide and act are ONE transaction, not two.

    The revoke used to run after the locked section returned, so an entry added at
    the same endpoint in that window lost a grant it had not used yet. The lock is
    instrumented rather than raced: a real concurrency test would have to win a
    scheduling race to fail, while ordering is the property the fix establishes and
    holds on every run.
    """
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def _tracking_lock():
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
    )
    monkeypatch.setattr(mcp_handlers, "_get_mcp_lock", _tracking_lock)
    monkeypatch.setattr(
        mcp_grant,
        "revoke_local_grant",
        lambda _url: events.append("revoke") or ["token", "registration"],
    )

    body = await _disconnect()

    assert body["grantRemoved"] is True
    assert events == ["lock", "revoke", "unlock"], "the revoke ran outside the lock"


@pytest.mark.asyncio
async def test_an_unreadable_census_source_keeps_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: the revoke needs the ABSENCE of a sharer, so it needs the census.

    A source that cannot be read is not a source with no entries. Guessing costs a
    grant that may be another server's only authorization, so an unreadable source
    keeps it -- and the response says why, since no sharer can be named.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        unreadable=("kiroGlobal",),
    )

    body = await _disconnect()

    assert revoked == [], "revoked on a census that could not be read"
    assert body["grantSharedWith"] == [], "invented a sharer it never read"
    assert body["grantCensusIncomplete"] is True
    # The purge acts on POSITIVE evidence only, so an unreadable sibling scope does
    # not block the entry removal the user asked for.
    assert body["entryRemoved"] is True


# ── The census reader itself ──


def _agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Point every census source at ``tmp_path`` and return the agents dir."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(mcp_discovery, "_MCP_SOURCES", ((tmp_path / "mcp.json", "kirocrew"),))
    monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", list)
    monkeypatch.setattr(connections_paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", list)
    return agents


def test_the_census_reads_agent_specs_kirocrew_does_not_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A hand-written agent spec is a census source, not an invisible one."""
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / "my-research.json").write_text(
        json.dumps({"mcpServers": {"notion-research": {"url": _URL}}}), encoding="utf-8"
    )

    specs, unreadable = ownership.spec_census()

    assert unreadable == ()
    assert specs["agent:my-research.json"] == {"notion-research": {"url": _URL}}


def test_the_census_names_a_source_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Malformed or unreadable is reported, never silently treated as empty."""
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / "broken.json").write_text("{not json", encoding="utf-8")

    specs, unreadable = ownership.spec_census()

    assert unreadable == ("agent:broken.json",)
    assert specs["agent:broken.json"] == {}


@pytest.mark.asyncio
async def test_disconnect_rejects_a_provider_outside_the_registry() -> None:
    client = await _client()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": "not-a-provider"})
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()
    assert body["code"] == "unknown_provider"


@pytest.mark.asyncio
async def test_disconnect_refuses_a_non_owner_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect deletes machine-global config and OAuth artifacts: owner-only.

    A presigned dashboard link admits non-owner subjects through the token
    middleware, and this endpoint is what turns Disconnect from entry-removal
    into credential deletion -- the same server-side boundary every mutating
    agents route enforces via ``_require_owner``. The deny must happen before
    any parse or destructive act.
    """
    revoked: list[str] = []
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
        revoked=revoked,
    )
    client = await _client()
    try:
        resp = await client.post(
            "/api/connections/disconnect",
            json={"slug": _SLUG},
            headers={"X-Test-User": "someone-else"},
        )
        assert resp.status == 403
        body = await resp.json()
    finally:
        await client.close()
    assert body["code"] == "owner_only"
    assert revoked == []
    assert purged == []


@pytest.mark.asyncio
async def test_a_same_named_entry_at_a_query_variant_still_shares_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry NAMED like our slug but pointing at a query variant shares the pair.

    ``grant_key`` drops the query, so ``notion`` at ``...?workspace=acme`` holds
    the same artifact pair while failing the ownership endpoint test. A census
    that only sharer-tests entries with OTHER names lets this one fall through
    both branches: not owned, not counted -- and its live grant is revoked.
    Ownership is judged first; every non-owned entry takes the sharer test,
    regardless of its name.
    """
    revoked: list[str] = []
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {_SLUG: {"url": _provider_url() + "?workspace=acme"}},
        },
    )
    body = await _disconnect()
    assert revoked == []
    assert body["grantRemoved"] is False
    assert body["grantSharedWith"] == [_SLUG]
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_a_same_named_agent_spec_entry_still_shares_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent spec whose entry is NAMED like our slug still blocks the revoke.

    Agent scopes are never purge targets, but their entries hold grants like any
    other. The old branch shape (`elif not label.startswith("agent:")`) made a
    same-named agent entry invisible to BOTH the owned test and the sharer test.
    """
    revoked: list[str] = []
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "agent:custom.json": {_SLUG: {"url": _provider_url()}},
        },
    )
    body = await _disconnect()
    assert revoked == []
    assert body["grantSharedWith"] == [_SLUG]
    assert purged == [_SLUG]


def test_the_census_reports_the_unreadable_agents_dir_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The unreadable-directory sentinel must land in ``unreadable``, not be skipped.

    ``agent_spec_sources`` reports an unenumerable agents dir as a source whose
    path IS the directory. ``is_file()`` is False for it, so a bare
    ``continue`` silently discards the exact signal the sentinel exists to
    carry, and the fail-closed design defeats itself.
    """
    _agents_dir(monkeypatch, tmp_path)
    sentinel_dir = tmp_path / "unreadable-agents"
    sentinel_dir.mkdir()
    monkeypatch.setattr(
        ownership,
        "agent_spec_sources",
        lambda _projects=(): [("agent:unreadable-agents/", sentinel_dir)],
    )

    _specs, unreadable = ownership.spec_census()

    assert "agent:unreadable-agents/" in unreadable


def test_the_census_reports_structurally_invalid_json_as_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Valid JSON with the wrong shape means entries UNKNOWN, never absent.

    A hand-corrupted source holding ``[]`` (non-object document) or
    ``{"mcpServers": []}`` (non-object server map) can hide a sharer; reading
    either as "no entries" is the interpretation that deletes a grant.
    A genuinely missing file stays absent, and a document with no
    ``mcpServers`` key declares no entries.
    """
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / "non-object.json").write_text("[]", encoding="utf-8")
    (agents / "bad-servers.json").write_text('{"mcpServers": []}', encoding="utf-8")
    (agents / "no-key.json").write_text('{"name": "x"}', encoding="utf-8")

    _specs, unreadable = ownership.spec_census()

    assert "agent:non-object.json" in unreadable
    assert "agent:bad-servers.json" in unreadable
    assert "agent:no-key.json" not in unreadable


@pytest.mark.asyncio
async def test_a_percent_encoded_host_variant_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHATWG percent-decodes hosts; urlsplit does not. Equality is unprovable.

    kiro-cli parses ``https://%6dcp.notion.com/mcp`` to the REGISTRY host and
    mints under the registry pair, while our hash of the raw string lands on a
    different key -- a silent false negative with no exception anywhere. Any URL
    outside the provable set must fail closed, never be skipped or mis-keyed.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"pct-host": {"url": "https://%6dcp.notion.com/mcp"}},
        },
    )
    body = await _disconnect()
    assert revoked == []
    assert body["grantCensusIncomplete"] is True
    assert screenless_no_alert(body)


@pytest.mark.asyncio
async def test_a_dot_segment_path_variant_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHATWG normalizes ``/a/../mcp`` to ``/mcp``; urlsplit keeps it verbatim."""
    revoked: list[str] = []
    base = _provider_url()
    dotted = base.replace("/mcp", "/a/../mcp")
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, base)],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": base}},
            "global": {"dotted-path": {"url": dotted}},
        },
    )
    body = await _disconnect()
    assert revoked == []
    assert body["grantCensusIncomplete"] is True


@pytest.mark.asyncio
async def test_a_unicode_host_variant_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ASCII host may IDNA-map into the registry host over there; unprovable."""
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"invisible": {"url": "https://m\u2061cp.notion.com/mcp"}},
        },
    )
    body = await _disconnect()
    assert revoked == []
    assert body["grantCensusIncomplete"] is True


@pytest.mark.asyncio
async def test_an_owned_trailing_slash_variant_revokes_both_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership is slash-insensitive; the key is not. Revoke EVERY owned pair.

    An owned entry at ``<url>/`` matches the registry endpoint but its pair
    lives under a different key. Revoking only the registry key purges the
    entry while its real token pair survives -- "Disconnected locally" over a
    live credential, the silent-resume dishonesty this endpoint exists to end.
    """
    revoked: list[str] = []
    purged: list[str] = []
    base = _provider_url()
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, base + "/")],
        purged=purged,
        revoked=revoked,
        raw_specs={"kirocrew": {_SLUG: {"url": base + "/"}}},
    )
    body = await _disconnect()
    assert set(revoked) == {base, base + "/"}
    assert body["grantRemoved"] is True
    assert purged == [_SLUG]


def screenless_no_alert(body: dict) -> bool:
    """The census keep must never read as a partial failure at the API level."""
    return body["grantRemoved"] is False and body["ok"] is True


def test_the_census_reports_null_mcp_servers_as_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """An explicit ``"mcpServers": null`` is an invalid map, not a missing key."""
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / "null-servers.json").write_text('{"mcpServers": null}', encoding="utf-8")

    _specs, unreadable = ownership.spec_census()

    assert "agent:null-servers.json" in unreadable


def test_the_census_reports_an_unlistable_agents_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """``Path.glob`` suppresses scan errors; enumeration must surface them.

    An executable-but-unlistable agents dir returns ZERO glob entries with no
    raise, so a sharer inside it is silently treated as absent. Enumeration
    goes through an API that raises, and PermissionError yields the sentinel.
    """
    agents = _agents_dir(monkeypatch, tmp_path)
    real_listdir = os.listdir

    def _deny(path: object = None) -> list[str]:
        if str(path) == str(agents):
            raise PermissionError(13, "Permission denied", str(path))
        return real_listdir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "listdir", _deny)

    sources = ownership.agent_spec_sources()
    assert len(sources) >= 1
    label = sources[0][0]
    assert label.startswith("agent:") and label.endswith("/")

    _specs, unreadable = ownership.spec_census()
    assert any(u.startswith("agent:") and u.endswith("/") for u in unreadable)


def test_the_census_treats_a_missing_agents_dir_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A fresh machine has no agents dir; that is absence, not unreadability."""
    _agents_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(connections_paths, "kiro_agents_dir", lambda: tmp_path / "never-created")

    _specs, unreadable = ownership.spec_census()

    assert unreadable == ()


# ── Project-local agent specs ──


def _project_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    entry: str,
    url: str,
) -> pathlib.Path:
    """A REAL census: ``mcp.json`` owning the provider plus a project-local spec.

    Nothing about the census is faked here -- these files are the ones it reads --
    which is what lets these three prove a project-local sharer is actually seen
    rather than that a fixture said it would be. Returns the project directory an
    open chat slot is bound to.
    """
    _agents_dir(monkeypatch, tmp_path)
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {_SLUG: {"url": _provider_url()}}}), encoding="utf-8"
    )
    project = tmp_path / "checkout"
    specs = project / ".kiro" / "agents"
    specs.mkdir(parents=True)
    (specs / "dev.json").write_text(
        json.dumps({"mcpServers": {entry: {"url": url}}}), encoding="utf-8"
    )
    return project


def _deny_listdir(monkeypatch: pytest.MonkeyPatch, denied: pathlib.Path) -> None:
    """Make ``os.listdir`` refuse exactly ``denied``, leaving every other dir real.

    A whole-source read failure is what the census must fail closed on, and only a
    non-``FileNotFoundError`` ``OSError`` qualifies -- a merely MISSING directory is
    genuine absence. Shared by the two tests that need an unenumerable project
    agents dir so the refusal is spelled once.
    """
    real_listdir = os.listdir

    def _deny(
        path: str | bytes | int | os.PathLike[str] | os.PathLike[bytes] | None = None,
    ) -> list[str]:
        if str(path) == str(denied):
            raise PermissionError(13, "Permission denied", str(path))
        return real_listdir(path)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(os, "listdir", _deny)


@pytest.mark.asyncio
async def test_a_project_local_agent_spec_sharing_the_endpoint_blocks_the_revoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """kiro-cli resolves ``$PWD/.kiro/agents`` BEFORE the user-level directory.

    So a spec committed in the user's checkout authorizes like any other and holds
    the same endpoint-named artifact pair, while the census read only the
    user-level dir -- deleting the pair deauthorized an agent this Disconnect was
    never asked to touch, and the refresh token is not recoverable locally.
    """
    project = _project_spec(monkeypatch, tmp_path, entry="notion-project", url=_provider_url())
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        real_census=True,
    )

    body = await _disconnect(project)

    assert revoked == [], "revoked a grant a project-local agent spec still uses"
    assert body["grantSharedWith"] == ["notion-project"]
    assert body["grantRemoved"] is False
    # The user still gets what they asked for: the entry goes, the credential the
    # other agent needs stays.
    assert body["entryRemoved"] is True
    assert body["grantCensusIncomplete"] is False


@pytest.mark.asyncio
async def test_a_project_local_agent_spec_is_never_a_purge_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The user's checkout is not ours to rewrite, even at our own name.

    A project spec named ``notion`` at the exact endpoint is a SHARER, so it
    blocks the revoke -- but it must never join the scope list the purge is
    restricted to, because Kiro Crew does not write version-controlled files.
    """
    project = _project_spec(monkeypatch, tmp_path, entry=_SLUG, url=_provider_url())
    scopes: list[tuple[str, ...]] = []
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        scopes=scopes,
        real_census=True,
    )

    body = await _disconnect(project)

    assert scopes == [("kirocrew",)], "purge reached outside the mcp.json scopes"
    assert revoked == []
    assert body["grantSharedWith"] == [_SLUG]
    labels = [label for label, _path in ownership.agent_spec_sources((project,))]
    project_labels = [label for label in labels if "checkout" in label]
    assert project_labels, "the project spec never entered the source list"
    assert not any(ownership.is_scope_label(label) for label in project_labels)
    assert not any(label.startswith("mirror:") for label in project_labels)


@pytest.mark.asyncio
async def test_an_unreadable_project_agents_dir_makes_the_census_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """An unlistable project agents dir hides a sharer, so the grant is kept.

    Same asymmetry the user-level dir already follows: the revoke needs the
    ABSENCE of a sharer, which an unreadable source cannot establish. A dir that
    is merely MISSING is genuine absence -- most projects have no ``.kiro/agents``
    -- so only a non-``FileNotFoundError`` ``OSError`` may fail closed.
    """
    project = _project_spec(monkeypatch, tmp_path, entry="notion-project", url=_provider_url())
    _deny_listdir(monkeypatch, project / ".kiro" / "agents")
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        real_census=True,
    )

    body = await _disconnect(project)

    assert revoked == [], "revoked on a census that could not see a whole source"
    assert body["grantCensusIncomplete"] is True
    assert body["grantRemoved"] is False
    # No sharer can be NAMED -- that is the point -- so the honest answer is the
    # census gap, not an empty sharer list next to a deleted credential.
    assert body["grantSharedWith"] == []
    assert body["entryRemoved"] is True


@pytest.mark.asyncio
async def test_the_census_gap_names_the_source_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The keep must name the file to fix, not just say one exists.

    ``grantCensusIncomplete`` alone tells the user to repair "the unreadable file"
    while naming nothing, which is an instruction they cannot act on -- the census
    already knows the label, so the response carries it. The ``agent:`` prefix is
    census bookkeeping and is stripped, leaving the path a user can actually go
    and look at.
    """
    project = _project_spec(monkeypatch, tmp_path, entry="notion-project", url=_provider_url())
    denied = project / ".kiro" / "agents"
    _deny_listdir(monkeypatch, denied)
    _wire(
        monkeypatch,
        removed=[],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=[],
        real_census=True,
    )

    body = await _disconnect(project)

    assert body["grantCensusIncomplete"] is True
    assert body["grantCensusUnreadable"] == [f"{denied}/"], "the gap named no source"
    assert not any(
        label.startswith("agent:") for label in body["grantCensusUnreadable"]
    ), "leaked the census's own label prefix into a user-facing name"


@pytest.mark.asyncio
async def test_an_uncomparable_url_reports_a_gap_with_no_source_to_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap the census cannot pin to a FILE must not fabricate one.

    ``census_incomplete`` is set by an unreadable source OR an entry whose URL
    could not be safely compared. The second has no file to fix, so the list stays
    empty and the card falls back to the source-less wording rather than
    interpolating a blank name into "fix the unreadable file".
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=[],
        # A dot-segment path: parseable, so not skippable junk, but WHATWG removes
        # the segment before hashing while urlsplit keeps it -- the shape whose key
        # can be neither proven nor refuted.
        inventory=[
            _entry(_SLUG, _provider_url()),
            _entry("other", "https://other.example.com/a/../mcp"),
        ],
        purged=[],
        revoked=revoked,
    )

    body = await _disconnect()

    assert revoked == [], "revoked while an entry's URL could not be compared"
    assert body["grantCensusIncomplete"] is True
    assert body["grantCensusUnreadable"] == [], "invented an unreadable file"


@pytest.mark.asyncio
async def test_a_backslash_path_variant_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHATWG folds ``\\`` to ``/`` and then removes the dot segment; urlsplit does not.

    ``/a\\..\\mcp`` is ONE segment to ``path.split("/")``, so the dot-segment
    guard never sees it, while kiro-cli authorizes the sibling under ``/mcp`` --
    the registry pair. The provable set's own docstring already excludes
    backslashes; the predicate has to enforce it.
    """
    revoked: list[str] = []
    base = _provider_url()
    parts = urlsplit(base)
    # Literal, NOT base.replace("/mcp", ...) -- "//mcp" in the scheme separator
    # contains "/mcp", so a replace mangles the host and the URL fails the screen
    # for an unrelated reason (which is how this test first passed vacuously).
    backslash = f"{parts.scheme}://{parts.hostname}/a\\..\\mcp"
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, base)],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": base}},
            "global": {"backslash-path": {"url": backslash}},
        },
    )
    body = await _disconnect()
    assert normalized_endpoint(backslash) is not None, "the shape must reach the hash"
    assert revoked == []
    assert body["grantCensusIncomplete"] is True


@pytest.mark.asyncio
async def test_the_rendered_agent_mirror_does_not_block_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The purge strips its own rendered mirror, so the mirror is not a sharer.

    ``_purge_server_config`` removes the entry from
    ``<agents>/kirocrew.json`` and every ``scope.agent_mcp_file`` in the SAME
    transaction. Counting those mirrored entries as independent grant holders
    makes ``shared`` non-empty on an ORDINARY disconnect, so the revoke is
    skipped forever and the stale grant stays reusable -- the feature never
    fires for anyone.
    """
    revoked: list[str] = []
    purged: list[str] = []
    scopes: list[tuple[str, ...]] = []
    base = _provider_url()
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, base)],
        purged=purged,
        revoked=revoked,
        scopes=scopes,
        raw_specs={
            "kirocrew": {_SLUG: {"url": base}},
            "mirror:kirocrew.json": {_SLUG: {"url": base}},
        },
    )
    body = await _disconnect()
    assert revoked == [base], "the rendered mirror blocked its own disconnect"
    assert body["grantSharedWith"] == []
    assert body["grantRemoved"] is True
    assert purged == [_SLUG]
    # A mirror is not a purgeable SCOPE either: only mcp.json scope labels are,
    # and the purge strips the rendered files unconditionally on its own.
    assert scopes == [("kirocrew",)], f"mirror label leaked into the purge scopes: {scopes}"


@pytest.mark.asyncio
async def test_a_mirror_entry_still_blocks_when_no_scope_is_purged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirror the purge will NOT run over is a real holder again.

    The exclusion is justified only by the purge removing that same entry in
    this transaction. With nothing owned, the purge never runs, so the mirrored
    entry keeps its grant and must block the revoke.
    """
    revoked: list[str] = []
    purged: list[str] = []
    base = _provider_url()
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[],
        purged=purged,
        revoked=revoked,
        raw_specs={"mirror:kirocrew.json": {_SLUG: {"url": base}}},
    )
    body = await _disconnect()
    assert revoked == []
    assert body["grantSharedWith"] == [_SLUG]
    assert purged == []


@pytest.mark.asyncio
async def test_a_sharer_of_one_owned_key_never_suppresses_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharers are per artifact key, not one flat flag over the whole disconnect.

    Ownership is slash-insensitive, so ``/mcp`` and ``/mcp/`` are BOTH ours --
    two different pairs. A third entry sharing only ``/mcp`` must keep that pair
    and leave ``/mcp/``'s revoke untouched; a flat flag skipped both, so the
    unshared credential survived its own entry's purge under
    "Disconnected locally."
    """
    revoked: list[str] = []
    purged: list[str] = []
    base = _provider_url()
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, base)],
        purged=purged,
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": base}},
            "kiroGlobal": {_SLUG: {"url": base + "/"}},
            "global": {"notion-work": {"url": base}},
        },
    )
    body = await _disconnect()
    assert revoked == [base + "/"], "the unshared owned pair was not revoked"
    assert body["grantSharedWith"] == ["notion-work"]
    assert body["grantRemoved"] is True


@pytest.mark.asyncio
async def test_a_deliberately_kept_pair_is_not_reported_as_surviving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``grantSurviving`` reports FAILED unlinks only, never deliberate keeps.

    Only the pairs this Disconnect actually tried to remove are re-stat'd, so a
    survivor is unambiguously a failure -- which is what lets the card render it
    as an alert without a precedence ladder deciding whether it is real.
    """
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"notion-work": {"url": _provider_url()}},
        },
    )
    body = await _disconnect()
    assert body["grantSurviving"] == []
    assert body["grantSharedWith"] == ["notion-work"]


def test_agent_sources_separate_purge_owned_mirrors_from_user_specs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The rendered Kiro Crew agent file is a MIRROR; a user's agent spec is not.

    ``_purge_server_config`` strips ``<agents>/kirocrew.json`` itself, so its
    entries are reflections of the scopes being purged rather than independent
    grant holders. A hand-written ``custom.json`` is the opposite: Kiro Crew
    never touches it, so its entry keeps its grant and must block a revoke.
    Labelling them identically is what made an ordinary Disconnect see its own
    reflection as a sharer.
    """
    agents = _agents_dir(monkeypatch, tmp_path)
    (agents / "kirocrew.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (agents / "custom.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    labels = dict((label, path.name) for label, path in ownership.agent_spec_sources())

    assert "mirror:kirocrew.json" in labels, f"rendered mirror not labelled: {labels}"
    assert "agent:custom.json" in labels, f"user spec mislabelled: {labels}"


@pytest.mark.asyncio
async def test_a_differently_named_mirror_entry_still_blocks_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The purge strips only ``slug`` from a mirror, so other names survive it.

    ``_remove_from_agent_file(<mirror>, name)`` is called with the SLUG. A
    mirrored entry under a DIFFERENT name at the same endpoint is therefore
    still configured after this transaction and still holds the grant -- so it
    must block the revoke. Excluding every mirror entry wholesale (rather than
    only the one the purge removes) deleted a grant a surviving entry needed.
    A disabled row makes it concrete: the probe view omits it, so the raw
    census is the only place it appears.
    """
    revoked: list[str] = []
    purged: list[str] = []
    base = _provider_url()
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, base)],
        purged=purged,
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": base}},
            "mirror:kirocrew.json": {
                _SLUG: {"url": base},
                "notion-work": {"url": base},
            },
        },
    )
    body = await _disconnect()
    assert revoked == [], "a surviving mirrored entry lost its grant"
    assert body["grantSharedWith"] == ["notion-work"]
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_a_mirrored_slug_variant_contributes_its_own_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirrored ``slug`` entry at a variant spelling still owns ITS pair.

    Excluding the mirrored ``slug`` entry as a holder is right -- the purge
    removes it -- but it must first contribute its artifact key, because
    ownership is slash-insensitive while the pair is not. Skipping it outright
    left that pair unrevoked while its entry was purged: a credential nobody
    reads and nobody removed, which is the silent-resume state this endpoint
    exists to end.
    """
    revoked: list[str] = []
    base = _provider_url()
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, base)],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": base}},
            "mirror:kirocrew.json": {_SLUG: {"url": base + "/"}},
        },
    )
    body = await _disconnect()
    assert set(revoked) == {base, base + "/"}, f"a mirrored variant pair survived: {revoked}"
    assert body["grantRemoved"] is True


@pytest.mark.asyncio
async def test_a_probe_row_mirroring_a_census_entry_does_not_vote_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe row already present in the raw census carries no second vote.

    ``list_servers`` is a MERGED view whose sources include the rendered agent
    config, so a mirrored entry reappears there as a provenance-free row. Judged
    again as a plain ``probe``, a mirrored same-name query variant (same artifact
    key, different endpoint) is labelled a sharer -- so the purge removes both
    entries and the grant is left behind, which is the silent-resume state this
    endpoint exists to end. The raw census is the provenance-carrying source, so
    a row it already represents must not be re-judged without that provenance.
    """
    revoked: list[str] = []
    base = _provider_url()
    variant = base + "?workspace=acme"
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        # The probe view surfaces the MIRROR's variant spelling, not the scope's.
        inventory=[_entry(_SLUG, variant)],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": base}},
            "mirror:kirocrew.json": {_SLUG: {"url": variant}},
        },
    )
    body = await _disconnect()
    assert revoked == [base], f"a mirrored entry re-entered as a probe and blocked: {revoked}"
    assert body["grantSharedWith"] == []
    assert body["grantRemoved"] is True


def test_the_mirror_strip_serializes_against_the_agent_files_own_lock(
    tmp_path: pathlib.Path,
) -> None:
    """The purge's rendered-mirror strip runs under that FILE's sidecar lock.

    ``_purge_server_config`` does not only write mcp.json scopes: it strips the
    entry from ``<agents>/kirocrew.json`` too (``_remove_from_agent_file``), and
    that file has a SECOND independent writer -- ``apps/bridges.py`` does
    whole-file read-modify-writes of it under ``bridges._mcp_lock``
    (``kirocrew.lock``). The MCP transaction lock the purge runs under is a
    DIFFERENT sidecar (``settings/mcp.lock``), so it excludes nothing here: an
    app registration that read the file before the strip and wrote it back after
    resurrects the purged entry -- and Disconnect has by then deleted the grant,
    leaving a configured provider with a dead credential. It does not self-heal,
    because ``rebuild_agent_config`` uses the existing rendered file as its merge
    base and only reconciles ``app:server`` keys under that lock.

    So the read AND the write must sit inside the file's own lock. Held from this
    thread, the strip must WAIT; pre-fix it completes straight through.
    """
    import threading

    from kiro_crew.apps.bridges import _mcp_lock
    from kiro_crew.dashboard.handlers.mcp import _remove_from_agent_file

    mirror = tmp_path / "kirocrew.json"
    mirror.write_text(json.dumps({"mcpServers": {_SLUG: {"url": _URL}}}), encoding="utf-8")

    done = threading.Event()
    result: list[bool] = []

    def _strip() -> None:
        result.append(_remove_from_agent_file(mirror, _SLUG))
        done.set()

    worker = threading.Thread(target=_strip, daemon=True)
    with _mcp_lock(target=mirror):
        worker.start()
        assert not done.wait(1.0), (
            "the rendered-mirror strip completed while the file's own lock was "
            "held: its read-modify-write does not serialize against "
            "apps/bridges.py, so a concurrent app registration can resurrect "
            "the purged entry after Disconnect deleted its grant"
        )
    worker.join(timeout=10)

    assert done.is_set(), "the strip never completed after the lock was released"
    assert result == [True]
    assert json.loads(mirror.read_text(encoding="utf-8"))["mcpServers"] == {}
