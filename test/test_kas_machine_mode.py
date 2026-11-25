"""KAS machine mode: Crew's own engine reads a host-issued token file, behind ``crew_client``.

Off (the preview off, or ``agent.kas_auth`` left at ``acp-callback``) a spawn is the
one ``main`` makes. On, the bundle argv carries the machine flags, Crew answers no
credential callback, the engine's env loses Crew's API key and the dropped names, a
missing file or cell refuses before any install, and the turn gate refuses a turn
the spawn would refuse before a rerun cuts history. Crew never opens the token file.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_transport import KAS_BUNDLE_AUTH_ARG
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.agent_sdk import kas_machine
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.dashboard import kiro_readiness

_ENDPOINT = "https://runtime.example.test"
_CONTROL = "https://control.example.test"
_REGION = "example-region-1"


def _agent(**over):
    base = dict(
        acp_backend=ACP_BACKEND_KAS,
        kas_bundle=True,
        kas_auth="machine",
        kas_token_path="",
        kas_endpoint=_ENDPOINT,
        kas_control_plane_endpoint=_CONTROL,
        kas_region=_REGION,
        kas_machine_env_drop=["EXAMPLE_MACHINE_TOKEN"],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _cfg(agent, on=True):
    previews = {"crew_client": True} if on else {}
    return SimpleNamespace(agent=agent, dashboard=SimpleNamespace(feature_previews=previews))


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "machine-token"
    path.write_text("x", encoding="utf-8")
    return str(path)


@pytest.fixture
def configured(monkeypatch):
    """Point every config read (live snapshot and ``config.json``) at *cfg*."""

    def set_cfg(cfg):
        monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: cfg)
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda *a, **k: cfg)

    return set_cfg


@pytest.fixture
def bundle_installed(monkeypatch, tmp_path):
    installed = SimpleNamespace(
        version="0.0.0",
        node_bin=str(tmp_path / "node"),
        acp_server=str(tmp_path / "acp-server.js"),
        node_version="0",
    )
    monkeypatch.setattr("kiro_crew.acp.kas_bundle.installed_bundle", lambda: installed)
    monkeypatch.setattr("kiro_crew.acp.harness.kas.ensure_engine_bundle_cache", lambda argv: None)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: False)


def _ctx(tmp_path):
    return SpawnContext(agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path)


def test_the_preview_name_is_the_sign_in_one():
    from kiro_crew.auth.login.endpoints import CREW_CLIENT_PREVIEW

    assert kas_machine.CREW_CLIENT_PREVIEW == CREW_CLIENT_PREVIEW


@pytest.mark.parametrize(
    "agent,on,expect",
    [
        (_agent(), True, True),
        (_agent(), False, False),  # the preview off: callback mode whatever kas_auth says
        (_agent(kas_auth="acp-callback"), True, False),
        (_agent(acp_backend="kiro"), True, False),
    ],
)
def test_machine_mode_needs_the_preview_the_kas_backend_and_machine(agent, on, expect):
    assert (kas_machine.machine_auth_in(_cfg(agent, on)) is not None) is expect


@pytest.mark.parametrize(
    "over,refusal",
    [
        (dict(kas_token_path=""), kas_machine.MACHINE_TOKEN_MISSING),
        (dict(kas_endpoint="http://runtime.example.test"), kas_machine.MACHINE_CELL_MISSING),
        (dict(kas_region=""), kas_machine.MACHINE_CELL_MISSING),
    ],
)
@pytest.mark.asyncio
async def test_a_missing_file_or_cell_refuses_before_any_install(
    configured, monkeypatch, token_file, tmp_path, over, refusal
):
    over.setdefault("kas_token_path", token_file)
    configured(_cfg(_agent(**over)))
    monkeypatch.setattr(
        "kiro_crew.acp.kas_bundle.installed_bundle", lambda: pytest.fail("install asked")
    )
    with pytest.raises(AcpRuntimeError, match=refusal):
        await KasHarness().resolve_spawn(_ctx(tmp_path))


def test_the_token_file_is_checked_without_being_opened(token_file, monkeypatch):
    import builtins

    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("token file opened"))
    assert kas_machine.token_file_present(token_file)
    assert not kas_machine.token_file_present("relative/path")


@pytest.mark.parametrize("on", [False, True], ids=["preview-off", "machine"])
@pytest.mark.asyncio
async def test_the_bundle_spawn(configured, bundle_installed, token_file, tmp_path, on):
    """Machine mode swaps the auth argument and strips the env; off is main's spawn."""
    configured(_cfg(_agent(kas_token_path=token_file), on))
    harness = KasHarness()
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    env = {"KIRO_API_KEY": "k", "EXAMPLE_MACHINE_TOKEN": "x", "KEEP": "z"}
    # As the runtime calls it (``runtime_spawn_launch``): cli-owned when Crew answers
    # no callback.
    harness.apply_spawn_env(env, spawned_binary=None, cli_owned_auth=not plan.host_auth)
    if not on:
        assert KAS_BUNDLE_AUTH_ARG in plan.argv and plan.host_auth is True
        assert plan.machine_auth is None and "EXAMPLE_MACHINE_TOKEN" in env
        return
    assert KAS_BUNDLE_AUTH_ARG not in plan.argv and plan.host_auth is False
    at = plan.argv.index("--auth=machine")
    assert plan.argv[at : at + 5] == [
        "--auth=machine",
        f"--token-path={token_file}",
        f"--endpoint={_ENDPOINT}",
        f"--control-plane-endpoint={_CONTROL}",
        f"--region={_REGION}",
    ]
    assert plan.machine_auth == kas_machine.machine_auth(_agent(kas_token_path=token_file))
    assert "KIRO_API_KEY" not in env and "EXAMPLE_MACHINE_TOKEN" not in env
    assert env["KEEP"] == "z"


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_the_env_scrub_matches_names_as_the_platform_does(monkeypatch, windows):
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    machine = kas_machine.machine_auth(_agent(kas_machine_env_drop=["Example_Token"]))
    env = {"EXAMPLE_TOKEN": "t", "KEEP": "k"}
    kas_machine.drop_engine_env(env, machine)
    assert env == ({"KEEP": "k"} if windows else {"EXAMPLE_TOKEN": "t", "KEEP": "k"})


@pytest.mark.parametrize("sharing", [False, True], ids=["relay", "sharing-preview-bundle"])
@pytest.mark.asyncio
async def test_the_relay_route_refuses(configured, monkeypatch, token_file, tmp_path, sharing):
    configured(_cfg(_agent(kas_token_path=token_file, kas_bundle=False)))
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: sharing)
    monkeypatch.setattr(
        "kiro_crew.acp.kas_route.sharing_preview_selects_bundle", lambda **_: sharing
    )
    with pytest.raises(AcpRuntimeError, match=kas_machine.MACHINE_ROUTE_NOT_BUNDLE):
        await KasHarness().resolve_spawn(_ctx(tmp_path))


def _rerun(cfg, live=None):
    request = mock.MagicMock()
    sessions = SimpleNamespace(_cfg=cfg, get_provider=lambda key: live)
    request.app = {"state": SimpleNamespace(sessions=sessions)}
    request.path = "/api/chat/rewind"
    return request


@pytest.mark.parametrize(
    "case,over,refusal",
    [
        ("token file missing", dict(kas_token_path=""), kas_machine.MACHINE_TOKEN_MISSING),
        ("kas_bundle off", dict(kas_bundle=False), kas_machine.MACHINE_ROUTE_NOT_BUNDLE),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_machine_turn_is_a_503_before_history_is_cut(
    configured, token_file, monkeypatch, case, over, refusal
):
    """The gate runs before a rerun cuts history: it refuses what the spawn refuses,
    with no engine installed and kiro-cli signed in, never asks kiro-cli, and names
    the machine cause, not the company sign-in."""
    from kiro_crew.auth.login.endpoints import LOCAL_AGENT_UNAVAILABLE

    over.setdefault("kas_token_path", token_file)
    cfg = _cfg(_agent(**over))
    configured(cfg)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.configured_backend_for_session",
        lambda key, c: c.agent.acp_backend,
    )
    monkeypatch.setattr("kiro_crew.agent_sdk.kas_bundle.installed_bundle", lambda: None)
    kiro_cli = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    resp = await kiro_readiness.reject_if_kiro_unverified(
        _rerun(cfg), harness_turn=True, session_key="chat-1", turn_reuses_session=False
    )
    assert resp is not None and resp.status == 503, case
    body = json.loads(resp.body)
    assert body["code"] == kiro_readiness.KAS_TURN_REFUSED_CODE
    assert body["error"].startswith(refusal) and body["error"] != LOCAL_AGENT_UNAVAILABLE
    kiro_cli.assert_not_called()


@pytest.mark.asyncio
async def test_a_live_session_keeps_the_token_file_it_started_with(
    configured, token_file, tmp_path, monkeypatch
):
    """kas_token_path moves from A to B, and the preview is then turned off, with no
    restart: the live process still reads A, so its catalog stamp stays under A and its
    next turn is ready on A, without the person vault or kiro-cli."""
    from kiro_crew.agent_sdk import kas_model_catalog
    from kiro_crew.auth import bridge

    started = _agent(kas_token_path=token_file)
    configured(_cfg(started))
    harness = KasHarness()
    harness.spawn_machine_auth = kas_machine.configured_machine_auth()
    other = tmp_path / "token-b"
    other.write_text("b", encoding="utf-8")
    configured(_cfg(_agent(kas_token_path=str(other))))
    extras = await harness.session_extras("", work_dir=None)
    assert extras.vault_identity == harness.spawn_machine_auth.catalog_identity
    assert kas_model_catalog.vault_identity() == (
        kas_machine.machine_auth(_agent(kas_token_path=str(other))).catalog_identity
    )
    # Another cell on the same token file is another catalog.
    moved = kas_machine.machine_auth(_agent(kas_token_path=token_file, kas_region="other-1"))
    assert moved.catalog_identity != harness.spawn_machine_auth.catalog_identity

    configured(_cfg(_agent(kas_token_path=str(other)), on=False))
    monkeypatch.setattr(bridge, "default_token_store", lambda: pytest.fail("person vault read"))
    kiro_cli = mock.AsyncMock(return_value=False)
    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", kiro_cli)
    runtime = SimpleNamespace(
        acp_backend=ACP_BACKEND_KAS,
        _kas_engine_bundle=True,
        _kas_machine_auth=harness.spawn_machine_auth,
    )
    request = _rerun(_cfg(started), live=SimpleNamespace(_runtime=runtime))
    resp = await kiro_readiness.reject_if_kiro_unverified(
        request, harness_turn=True, session_key="chat-1"
    )
    assert resp is None
    kiro_cli.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_machine_start_is_not_a_personal_sign_in(monkeypatch):
    """The engine refusing the host's token is worded as that, never as Crew signed out."""
    from kiro_crew.providers.acp_runtime_start import _kas_machine_refusal

    runtime = SimpleNamespace(_kas_machine_auth=kas_machine.machine_auth(_agent()))
    err = _kas_machine_refusal(runtime, RuntimeError("TokenExpiredError: expired"), True)
    assert err is not None and "Kiro refused the machine token: TokenExpiredError" in str(err)
    assert err.transient is False
    unclassed = _kas_machine_refusal(runtime, RuntimeError("process exited"), True)
    assert unclassed is not None and str(unclassed) == kas_machine.REFUSED_TEXT
    assert _kas_machine_refusal(runtime, RuntimeError("process exited"), False) is None
    callback = SimpleNamespace(_kas_machine_auth=None)
    assert _kas_machine_refusal(callback, RuntimeError(), True) is None
    # A runtime double that carries no MachineAuth is callback mode, not machine mode.
    assert _kas_machine_refusal(mock.MagicMock(), RuntimeError(), True) is None


@pytest.mark.parametrize("present", [True, False])
def test_kas_whoami_prints_no_token(configured, token_file, capsys, present):
    from kiro_crew import cli_kas_login

    configured(_cfg(_agent(kas_token_path=token_file if present else "")))
    code = cli_kas_login._whoami(argparse.Namespace(format="json"))
    out = capsys.readouterr()
    assert "x" not in out.out.replace("machine", "")
    if present:
        assert code == 0 and json.loads(out.out) == {"accountType": "machine"}
    else:
        assert code == 1 and kas_machine.MACHINE_TOKEN_MISSING in out.err


@pytest.mark.asyncio
async def test_the_usage_poll_reads_no_vault_in_machine_mode(configured, token_file, monkeypatch):
    from kiro_crew.auth import bridge
    from kiro_crew.dashboard.handlers import sessions

    configured(_cfg(_agent(kas_token_path=token_file)))
    for name in ("_usage_cache", "_usage_cache_ts", "_usage_cache_vault_fp"):
        monkeypatch.setattr(sessions, name, getattr(sessions, name))
    monkeypatch.setattr(bridge, "default_token_store", lambda: pytest.fail("person vault read"))
    await sessions._refresh_from_vault()
    assert sessions._usage_cache == {"available": False}


@pytest.mark.asyncio
async def test_a_cached_personal_reading_is_not_served_in_machine_mode(
    configured, token_file, monkeypatch
):
    """A reading the vault published before machine mode turned on is foreign at once,
    without waiting for the next refresh to replace it."""
    from kiro_crew.dashboard.handlers import sessions

    reading = {"credits_plan": "example-plan"}
    configured(_cfg(_agent(kas_token_path=token_file), on=False))
    monkeypatch.setattr("kiro_crew.auth.login.endpoints.stored_sign_in_registered", lambda: False)
    monkeypatch.setattr(sessions, "_cached_reading_is_foreign", mock.AsyncMock(return_value=False))
    assert await sessions._reading_not_servable(True, reading, "fp") == ""
    configured(_cfg(_agent(kas_token_path=token_file)))
    assert await sessions._reading_not_servable(True, reading, "fp") == "foreign"


def test_a_machine_mode_start_neither_founds_nor_joins_a_shared_runtime(
    configured, token_file, monkeypatch
):
    """The sharing key carries no machine settings, so a start under them is unshared."""
    from kiro_crew.acp import chat_runtime_sharing

    configured(_cfg(_agent(kas_token_path=token_file)))
    monkeypatch.setattr(
        "kiro_crew.acp.kas_route.resolve_kas_route", lambda **_: pytest.fail("route asked")
    )
    assert chat_runtime_sharing.kas_chat_sharing_ready() is False


@pytest.mark.parametrize(
    "data,expect",
    [
        ("HTTP 401 session expired", kas_machine.REFUSED_TEXT),
        (
            "TokenExpiredError: expired",
            "Kiro refused the machine token: TokenExpiredError (-32603)",
        ),
        ("ModelRegistryUnavailableError: upstream 503", None),
    ],
    ids=["sign-in-shaped", "engine-class", "registry-outage-is-not-the-token"],
)
def test_a_mid_turn_token_refusal_is_the_host_token_not_a_sign_in(data, expect):
    """A process on a host token is never offered the personal sign-in mid-session."""
    from kiro_crew.acp.client import AcpError

    frame = {"code": -32603, "message": "Internal error", "data": data}
    machine = SimpleNamespace(_kas_machine_auth=kas_machine.machine_auth(_agent()))
    with pytest.raises(AcpError) as caught:
        kas_machine.raise_turn_error(machine, frame, None, backend=ACP_BACKEND_KAS)
    err = caught.value
    if expect is None:
        assert "machine token" not in str(err)
        return
    assert str(err) == expect and err.transient is False and err.__cause__ is None
    assert not getattr(err, "auth_required", False) and not getattr(err, "sign_in_needed", False)
    # Callback mode (and a runtime double with no MachineAuth) keeps the sign-in card.
    signed_out = {"code": -32603, "message": "Internal error", "data": "HTTP 401 session expired"}
    for runtime in (SimpleNamespace(_kas_machine_auth=None), mock.MagicMock()):
        with pytest.raises(AcpError) as callback:
            kas_machine.raise_turn_error(runtime, signed_out, None, backend=ACP_BACKEND_KAS)
        assert callback.value.auth_required is True


@pytest.mark.parametrize("on", [True, False], ids=["machine", "preview-off"])
def test_the_agent_file_gate_fences_the_machine_token_file(
    configured, token_file, tmp_path, monkeypatch, on
):
    """Machine mode on: every agent file read refuses the token file, as itself, through a
    linked folder and as a directory holding it; file_send copies nothing. Off: not
    fenced."""
    from kiro_crew import mcp_core
    from kiro_crew.hook_runtime.safe_reads import safe_read_file_bytes, validate_file_path
    from kiro_crew.mcp_tools.messaging import file_send
    from kiro_crew.security import paths
    from kiro_crew.testing.links import make_dir_link

    configured(_cfg(_agent(kas_token_path=token_file), on))
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))
    # A junction on Windows, which needs no symlink privilege.
    alias = tmp_path / "alias-dir"
    make_dir_link(alias, tmp_path)
    linked = str(alias / "machine-token")
    assert (kas_machine.fenced_token_path() == token_file) is on
    for spelling in (token_file, linked):
        assert paths.is_sensitive_path(spelling) is on, spelling
        assert (validate_file_path(spelling) is None) is on, spelling
        assert (safe_read_file_bytes(spelling) is None) is on, spelling
    assert paths.path_contains_sensitive(str(tmp_path)) is on
    with (
        mock.patch.object(mcp_core, "_post", return_value={"ok": True}),
        mock.patch.object(
            mcp_core, "require_strict_session_key", return_value=("dashboard:s1", None)
        ),
        mock.patch.object(mcp_core, "_classify_slack_identity", return_value=("unresolved", None)),
    ):
        out = file_send("file_send", {"path": token_file})
    outbox = tmp_path / "ws" / "outbox"
    if on:
        assert out.startswith("Error: file not found or access denied")
        assert not any(outbox.glob("*")) if outbox.exists() else True
    else:
        assert (outbox / "machine-token").exists()


@pytest.mark.asyncio
async def test_the_engine_still_reads_its_fenced_token_file(
    configured, bundle_installed, token_file, tmp_path
):
    """The fence is for agent file tools only: the spawn still checks the file and hands
    the engine its path, and the engine opens it itself."""
    configured(_cfg(_agent(kas_token_path=token_file)))
    assert kas_machine.fenced_token_path() == token_file
    plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
    assert f"--token-path={token_file}" in plan.argv
    assert kas_machine.token_file_present(token_file)


def test_a_personal_sign_out_does_not_retire_a_machine_mode_session():
    """The identity sweep asks ``uses_kiro_identity_store``: a KAS process on a host
    token file reads no personal store, so it answers False at every layer."""
    from kiro_crew.acp.runtime_class import AcpRuntime
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    machine = kas_machine.machine_auth(_agent())
    for auth, expect in ((machine, False), (None, True)):
        runtime = SimpleNamespace(
            _acp_backend=ACP_BACKEND_KAS, acp_backend=ACP_BACKEND_KAS, _kas_machine_auth=auth
        )
        assert AcpRuntime.uses_kiro_identity_store.fget(runtime) is expect
        handle = SimpleNamespace(_runtime=runtime)
        assert AcpSessionProvider.uses_kiro_identity_store.fget(handle) is expect
        client = SimpleNamespace(backend=ACP_BACKEND_KAS, _runtime=runtime)
        provider = SimpleNamespace(_client=client)
        assert AcpProvider.uses_kiro_identity_store.fget(provider) is expect


def test_each_test_starts_with_no_reused_fence_config():
    """The per-test reset clears the config read the file gate reuses."""
    assert kas_machine._fence_config is None


def test_with_no_watcher_the_fence_reads_config_json_without_the_loader(token_file, monkeypatch):
    """An MCP server or hook child has no live config: the fence reads config.json as
    JSON (the loader can write the file, and must not run on a file-gate call), and
    re-reads it only when the file changes."""
    from kiro_crew.config.loader_locations import config_path

    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: None)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", lambda *a, **k: pytest.fail("loader")
    )
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "agent": {"kas_auth": "machine", "kas_token_path": token_file},
        "dashboard": {"feature_previews": {"crew_client": True}},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    before = path.read_bytes()
    assert kas_machine.fenced_token_path() == token_file
    assert path.read_bytes() == before
    doc["dashboard"]["feature_previews"]["crew_client"] = False
    path.write_text(json.dumps(doc) + " ", encoding="utf-8")
    assert kas_machine.fenced_token_path() == ""


def test_with_no_watcher_the_fence_follows_config_local_json(token_file, tmp_path, monkeypatch):
    """config.local.json overlays config.json for the fence as it does for the engine."""
    from kiro_crew.config.loader_locations import config_local_path, config_path

    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: None)
    other = tmp_path / "token-b"
    other.write_text("b", encoding="utf-8")
    base = config_path()
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        json.dumps(
            {
                "agent": {"kas_auth": "machine", "kas_token_path": token_file},
                "dashboard": {"feature_previews": {"crew_client": True}},
            }
        ),
        encoding="utf-8",
    )
    assert kas_machine.fenced_token_path() == token_file
    local = config_local_path()
    local.write_text("\ufeff" + json.dumps({"agent": {"kas_token_path": str(other)}}), "utf-8")
    assert kas_machine.fenced_token_path() == str(other)


@pytest.mark.parametrize("broken", ["config.json", "config.local.json"])
def test_a_malformed_config_keeps_the_last_fence(token_file, monkeypatch, broken):
    """A typo in either file mid-session must not lift the fence the engine's token is
    under; a clean read with machine mode off does."""
    from kiro_crew.config.loader_locations import config_local_path, config_path

    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: None)
    base = config_path()
    base.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "agent": {"kas_auth": "machine", "kas_token_path": token_file},
        "dashboard": {"feature_previews": {"crew_client": True}},
    }
    base.write_text(json.dumps(doc), encoding="utf-8")
    assert kas_machine.fenced_token_path() == token_file
    target = base if broken == "config.json" else config_local_path()
    target.write_text("{ not json", encoding="utf-8")
    assert kas_machine.fenced_token_path() == token_file
    if broken == "config.local.json":
        target.unlink()
    doc["agent"]["kas_auth"] = "acp-callback"
    base.write_text(json.dumps(doc), encoding="utf-8")
    assert kas_machine.fenced_token_path() == ""
