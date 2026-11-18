"""LTS end to end: no local tier widens the remote ceiling for the KGL batch-1 terms.

RFC ``docs/request-for-change/rfc-crew-lts-edition.md``, D2, D3 and D13. The same path
as ``test_lts_remote_ceiling_outranks_local.py``: one fake control-plane (CPS) reply,
the real transport ``enterprise_policy_fetch.fetch``, the translator, the tier fold in
``governance.load_security_policy``, then the installed context. Here the reply speaks
the batch-1 terms (``harness_use``, ``app_use``, ``script_hook`` and the ``skill`` /
``mcp`` / ``subagent`` install rows), and the decision models are checked too: hosted
Jev and hand-written endpoints are locked off in an LTS build.

Each answer is read at the chokepoint a feature asks:
``agent_backend_governance.narrow_selectable_backends`` then
``agent_sdk.backends.resolve_selected_backend`` (harness), the app activation gate and
``extension_governance.app_version_denied`` (apps),
``hook_event_governance.event_refusal`` (script hooks),
``extension_governance.extension_install_denied`` (installs), and
``decisions.capability`` (decision models).

Three remote replies: one that grants a little of each term, one that denies each
term, and one that names none of them (the LTS baseline answers). The local tiers
tried: the env policy (``KIROCREW_SECURITY_POLICY``), the home policy file, a
governance profile bound to the host surface, and user config.

The install cases drive the Discover skill install (``/api/skills/-/discover/install``)
and the official MCP registry plan (``mcp_discover.official_install_plan``). User
config has no install setting, so only the three policy tiers are tried there.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import profiles_dir_fixture  # noqa: F401
from governance_distribution_helpers import _write_policy

from kiro_crew.agent_backend_governance import narrow_selectable_backends
from kiro_crew.agent_sdk import backends
from kiro_crew.apps.manager_activation import _app_activation_denied
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
)
from kiro_crew.dashboard.handlers import discover as discover_mod
from kiro_crew.dashboard.handlers import mcp_discover
from kiro_crew.decisions import capability
from kiro_crew.decisions import gate as decisions_gate
from kiro_crew.extension_governance import app_version_denied, extension_install_denied
from kiro_crew.hook_event_governance import event_refusal
from kiro_crew.mcp_providers.base import McpInstallPlan, McpServerDetail
from kiro_crew.mcp_providers.base import ProviderRegistry as McpProviderRegistry
from kiro_crew.platform import enterprise_policy_fetch as epf
from kiro_crew.platform import enterprise_policy_translate as ept
from kiro_crew.platform import governance
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.skill_providers.base import ProviderRegistry as SkillProviderRegistry
from kiro_crew.skills import SkillsLoader

_ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEFGHIJKL"
_HOST = gp.HOST_SESSION_KEY

#: The organisation grants a little of each term, in the control plane's shape for a
#: scoped grant (the allow and its ``deny exclude``).
_GRANTS = """\
version: "0.1"
rules:
  - {capability: harness_use, effect: allow, match: [kiro]}
  - {capability: harness_use, effect: deny, exclude: [kiro]}
  - {capability: app_use, effect: allow, match: ["file-explorer@1.*"]}
  - {capability: app_use, effect: deny, exclude: ["file-explorer@1.*"]}
  - {capability: script_hook, effect: allow, match: [PreToolUse]}
  - {capability: script_hook, effect: deny, exclude: [PreToolUse]}
  - {capability: skill, effect: allow}
  - {capability: mcp, effect: allow, match: [github]}
  - {capability: mcp, effect: deny, exclude: [github]}
  - {capability: subagent, effect: deny}
"""

#: The organisation denies each term outright.
_DENIES = """\
version: "0.1"
rules:
  - {capability: harness_use, effect: deny}
  - {capability: app_use, effect: deny}
  - {capability: script_hook, effect: deny}
  - {capability: skill, effect: deny}
  - {capability: mcp, effect: deny}
  - {capability: subagent, effect: deny}
"""

#: The organisation names none of the batch-1 terms.
_ABSENT = """\
version: "0.1"
rules:
  - {capability: theme_persona, effect: allow}
"""

_REMOTES = {"grants": _GRANTS, "denies": _DENIES, "absent": _ABSENT}

#: Replies for the Discover install cases. ``skill`` names skills while the install id
#: is a package, so each skill a package holds is checked. An ``mcp`` grant names a
#: whole registry server (``<id>/*``). ``allow all`` and ``allow builtin`` grant
#: install (RFC D13, owner decision D1).
_INSTALL_REMOTES = {
    "install_deny": """\
version: "0.1"
rules:
  - {capability: skill, effect: allow}
  - {capability: skill, effect: deny, match: [evil]}
  - {capability: mcp, effect: allow}
  - {capability: mcp, effect: deny, match: ["io.github.acme/evil"]}
""",
    "install_allow": """\
version: "0.1"
rules:
  - {capability: skill, effect: allow, match: [fake-skill, good]}
  - {capability: skill, effect: deny, exclude: [fake-skill, good]}
  - {capability: mcp, effect: allow, match: ["io.github.acme/tool/*"]}
  - {capability: mcp, effect: deny, exclude: ["io.github.acme/tool/*"]}
""",
    "allow_all": """\
version: "0.1"
rules:
  - {capability: all, effect: allow}
""",
    "allow_builtin": """\
version: "0.1"
rules:
  - {capability: builtin, effect: allow}
""",
}

#: Harnesses a local tier tries to add: two in the closed list, two outside it.
_HARNESS_WIDEN = ["claude", "codex", "goose"]
_HARNESS_ALL = ["kas", "kiro", "claude", "codex", "goose", "opencode"]

#: What a local tier writes to try to open every batch-1 term back up. Jev and a
#: hand-written endpoint are named in the decision lists too.
_WIDENING = {
    "agent_backend": {"mode": "allow", "allow": _HARNESS_ALL},
    "apps": {"mode": "deny", "deny": []},
    "app_versions": {"mode": "deny", "deny": []},
    "capabilities": {
        "script_hooks": {"enabled": True, "scopes": {"events": {"mode": "deny", "deny": []}}},
        "extension_install": {
            "enabled": True,
            "scopes": {"extensions": {"mode": "allow", "allow": ["*"]}},
        },
        "decisions": {
            "enabled": True,
            "scopes": {
                "providers": {"mode": "allow", "allow": ["*", "jev", "custom"]},
                "endpoints": {"mode": "deny", "deny": []},
            },
        },
    },
}

#: What a local tier writes to narrow the remote ceiling.
_NARROWING = {
    "agent_backend": {"mode": "allow", "allow": ["kas"]},
    "apps": {"mode": "deny", "deny": ["file-explorer"]},
    "capabilities": {
        "script_hooks": {"enabled": False},
        "extension_install": {
            "enabled": True,
            "scopes": {"extensions": {"mode": "deny", "deny": ["skill:*"]}},
        },
        "decisions": {
            "enabled": True,
            "scopes": {"providers": {"mode": "deny", "deny": ["sagemaker"]}},
        },
    },
}

_TIERS = ["env", "home", "profile"]

_CLEF = (
    "https://api.cloudflare.com/client/v4/accounts/"
    + "0123abcd" * 4
    + "/ai/run/@cf/cloudflare/clef"
)
_HAND_WRITTEN = "https://decide.corp.example/v1/systemone"


def _envelope(policy: str) -> bytes:
    digest = epf.body_digest(policy, ())
    return json.dumps(
        {
            "enterpriseManagementStatus": "ENABLED",
            "manifest": {"hash": "sha256:" + digest, "versionId": "v1:" + digest},
            "body": {"policy": policy, "configuration": []},
        }
    ).encode("utf-8")


class _Resp:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(name="remote", params=sorted(_REMOTES))
def remote_fixture(request):
    """Which reply the control plane sends; a test may pin one by indirect param."""
    return request.param


@pytest.fixture(name="cps")
def cps_fixture(monkeypatch, remote):
    """The control plane: answers every request with the chosen remote reply."""
    requests: list = []

    def answer(req, timeout):
        requests.append(req)
        return _Resp(_envelope({**_REMOTES, **_INSTALL_REMOTES}[remote]))

    monkeypatch.setattr(pd, "_open", answer)
    return requests


@pytest.fixture(name="signed_in")
def signed_in_fixture(tmp_path, monkeypatch, _floor_monkeypatch):
    """One enterprise sign-in in a temporary vault, pinned as a fresh process pins it."""
    store = TokenStore(tmp_path / "data")
    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: store)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.setenv(epf.PROFILE_PIN_ENV, "")
    monkeypatch.delenv(epf.PROFILE_PIN_ENV)
    monkeypatch.setattr(epf, "_pinned", "")
    _floor_monkeypatch.setattr(epf.signal, "raise_signal", lambda sig: None)
    store.save(
        KasToken(
            access_token="fake-access-token-value",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="Enterprise",
            identity="identity_center",
            profile_arn=_ARN,
            auth_method="identity_center",
            token_endpoint="https://idp.example.invalid/token",
        )
    )


@pytest.fixture(name="lts")
def lts_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture(name="boot")
def boot_fixture(install_ceiling, lts, signed_in, cps, profiles_dir):
    """Boot as ``bootstrap_context`` does: load, check the profiles, install, narrow.

    ``install_ceiling`` is requested before ``lts`` so its base context is composed
    as oss, before the edition flips.
    """

    def boot() -> governance.GovernanceCeiling:
        ceiling = governance.load_security_policy()
        assert ceiling is not None and ceiling.tier == governance.TIER_CENTRAL
        assert len(cps) == 1
        gp.reset_store()
        gp.assert_profiles_within_ceiling(ceiling)
        install_ceiling(ceiling)
        narrow_selectable_backends()
        # Every local tier written was read, so no case passes because its tier was
        # skipped or refused.
        local_file = os.environ.get("KIROCREW_SECURITY_POLICY") or (
            governance._policy_home_path().exists()
        )
        assert ceiling.lower_tier_folded is bool(local_file)
        assert gp.fallback_profile_names() == frozenset()
        assert gp.unknown_profile_scopes() == {}
        bound = gp.resolve_active_scope(_HOST)
        assert (bound is not None) is any(profiles_dir.iterdir())
        return ceiling

    return boot


def _local_tier(tier: str, controls: dict, tmp_path, monkeypatch, profiles_dir) -> None:
    """Write *controls* into one local tier Crew reads."""
    if tier == "env":
        path = _write_policy(tmp_path / "env-policy.json", "local-env", **controls)
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(path))
    elif tier == "home":
        path = _write_policy(tmp_path / "home-policy.json", "local-home", **controls)
        monkeypatch.setattr(governance, "_policy_home_path", lambda: path)
    else:
        profile = {"name": "host", "bind": {"type": "surface", "id": "host"}, **controls}
        (profiles_dir / "host.json").write_text(json.dumps(profile), encoding="utf-8")


def _selectable() -> set:
    return set(backends.selectable_backends())


def _installs(kind: str, ident: str) -> bool:
    return extension_install_denied(kind, ident) is None


def _hook_runs(event: str) -> bool:
    return event_refusal(_HOST, event) is None


def _app_runs(name: str, version: str) -> bool:
    return _app_activation_denied(name, fail_closed=True) is None and (
        app_version_denied(name, version) is None
    )


def _decision_config(endpoint: str = "", model: str | None = None):
    kwargs: dict = {"timeout_ms": 1000}
    if endpoint:
        kwargs["endpoint"] = endpoint
    if model is not None:
        kwargs["model"] = model
    provider = DecisionProviderConfig(**kwargs)
    return SimpleNamespace(decisions=DecisionsConfig(bucket=100, provider=provider))


def _decision_refused(config) -> bool:
    """Whether the decision model *config* names is refused on the host surface."""
    endpoint = decisions_gate.configured_endpoint(config)
    model = decisions_gate.configured_provider_model(config)
    provider = capability.provider_id(endpoint, model)
    return capability.is_decisions_denied(_HOST, provider=provider) or capability.targets_denied(
        _HOST, point=None, model=model or None, endpoint=endpoint
    )


#: What the remote ceiling alone permits, per reply: the floor of what a local tier
#: may narrow to and the most it may ever reach.
_EXPECTED = {
    "grants": {
        "harness": {"kas", "kiro"},
        "apps": {("file-explorer", "1.2.0")},
        "events": {"PreToolUse"},
        "installs": {("skill", "owner/repo"), ("skill", "pdf"), ("mcp", "github")},
    },
    "denies": {"harness": {"kas"}, "apps": set(), "events": set(), "installs": set()},
    "absent": {"harness": {"kas"}, "apps": set(), "events": set(), "installs": set()},
}

_HARNESS_PROBE = ["kas", "kiro", "claude", "codex", "goose"]
_APP_PROBE = [("file-explorer", "1.2.0"), ("file-explorer", "2.0.0"), ("notes", "1.0.0")]
_EVENT_PROBE = ["PreToolUse", "PostToolUse", "Stop", "UserTriggered"]
_INSTALL_PROBE = [
    ("skill", "owner/repo"),
    ("skill", "pdf"),
    ("mcp", "github"),
    ("mcp", "filesystem"),
    ("agent", "reviewer"),
    ("plugin", "anything"),
]


def _harness_ids() -> set:
    return {b for b in _HARNESS_PROBE if b in backends.registered_backends()}


def _answers() -> dict:
    return {
        "harness": {b for b in _harness_ids() if b in _selectable()},
        "apps": {a for a in _APP_PROBE if _app_runs(*a)},
        "events": {e for e in _EVENT_PROBE if _hook_runs(e)},
        "installs": {i for i in _INSTALL_PROBE if _installs(*i)},
    }


def _expected(remote: str) -> dict:
    out = dict(_EXPECTED[remote])
    out["harness"] = out["harness"] & _harness_ids()
    return out


class TestTheRemoteReplyIsTheCeiling:
    def test_with_no_local_tier_the_remote_answers(self, boot, remote):
        boot()
        assert _answers() == _expected(remote)

    @pytest.mark.parametrize("remote", ["absent"], indirect=True)
    def test_no_rule_leaves_the_lts_baseline(self, boot):
        boot()
        assert _selectable() == {backends.governance_floor_backend()}
        # ``apps`` is open by default; ``app_versions`` is off, so nothing loads.
        assert _app_activation_denied("file-explorer", fail_closed=True) is None
        assert app_version_denied("file-explorer", "1.2.0") is not None
        assert not _hook_runs("PreToolUse")
        assert not _installs("skill", "pdf")

    def test_the_registry_holds_harnesses_outside_the_closed_list(self, boot):
        """The harness cases matter only because more than the list is registered."""
        boot()
        assert {"codex", "goose"} & set(backends.registered_backends())


class TestALocalTierCannotWiden:
    @pytest.mark.parametrize("tier", _TIERS)
    def test_every_batch1_term_stays_at_the_remote_ceiling(
        self, boot, remote, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, _WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        assert _answers() == _expected(remote), (remote, tier)

    @pytest.mark.parametrize("tier", _TIERS)
    def test_a_harness_outside_the_ceiling_resolves_to_the_floor(
        self, boot, remote, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, _WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        for backend in _HARNESS_WIDEN:
            assert backends.resolve_selected_backend(backend) == "kas", (remote, tier, backend)

    def test_user_config_cannot_pick_a_harness_the_remote_denies(self, boot, remote):
        boot()
        for backend in _HARNESS_WIDEN:
            # ``resolve_selected_backend`` is what ``agent.acp_backend`` in config.json
            # goes through on load.
            assert backends.resolve_selected_backend(backend) == "kas", (remote, backend)


class TestALocalTierCanNarrow:
    @pytest.mark.parametrize("remote", ["grants"], indirect=True)
    @pytest.mark.parametrize("tier", _TIERS)
    def test_narrowing_holds(self, boot, tier, tmp_path, monkeypatch, profiles_dir):
        _local_tier(tier, _NARROWING, tmp_path, monkeypatch, profiles_dir)
        boot()
        answers = _answers()
        assert answers["harness"] == {"kas"}, tier
        assert answers["apps"] == set(), tier
        assert answers["events"] == set(), tier
        assert answers["installs"] == {("mcp", "github")}, tier
        assert capability.is_decisions_denied(_HOST, provider="sagemaker"), tier
        assert not capability.is_decisions_denied(_HOST, provider="clef"), tier


class TestJevIsLockedOff:
    @pytest.mark.parametrize("tier", [None, *_TIERS])
    def test_no_local_tier_turns_hosted_jev_or_a_hand_written_endpoint_on(
        self, boot, remote, tier, tmp_path, monkeypatch, profiles_dir
    ):
        if tier is not None:
            _local_tier(tier, _WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        offered = capability.permitted_providers(_HOST)
        assert offered[capability.PROVIDER_HOSTED] is False, (remote, tier)
        assert capability.is_decisions_denied(_HOST, provider=capability.PROVIDER_HOSTED)
        assert capability.is_decisions_denied(_HOST, provider="custom")
        # Any endpoint on Jev's host is refused by its host as well.
        assert capability.targets_denied(
            _HOST, point=None, model=None, endpoint="https://api.typesafe.ai/v1/other"
        )
        assert capability.targets_denied(
            _HOST, point=None, model=None, endpoint="https://eu.api.typesafe.ai./v1/systemone"
        )
        # The other decision models stay open.
        for pid in ("sagemaker", "laya"):
            assert offered.get(pid) is True, (remote, tier, pid)
        for pid in ("sagemaker", "laya", "clef"):
            assert not capability.is_decisions_denied(_HOST, provider=pid), (remote, tier, pid)

    def test_user_config_cannot_point_decisions_at_jev_or_a_hand_written_endpoint(
        self, boot, remote
    ):
        boot()
        # Unset falls back to hosted Jev; each spelling is refused.
        assert _decision_refused(_decision_config())
        assert _decision_refused(_decision_config(DECISION_PROVIDER_ENDPOINT_DEFAULT))
        assert _decision_refused(_decision_config(_HAND_WRITTEN))
        # A Clef-shaped address with the wrong model is hand-written, so refused.
        assert _decision_refused(_decision_config(_CLEF, model="other"))
        assert not _decision_refused(_decision_config(_CLEF, model="clef"))
        assert not _decision_refused(_decision_config("sagemaker://us-east-1/endpoint-a"))


class TestOssIgnoresTheRemotePath:
    @pytest.mark.parametrize("remote", ["grants"], indirect=True)
    def test_boot_neither_fetches_nor_translates(self, signed_in, cps):
        with patch.object(ept, "translate", side_effect=AssertionError("translated")):
            assert governance.load_security_policy() is None
        assert cps == []

    @pytest.mark.parametrize("remote", ["grants"], indirect=True)
    def test_oss_has_no_jev_lock(self, signed_in, cps):
        assert capability.provider_id(_HAND_WRITTEN, "") is None
        assert not capability.edition_refuses(capability.PROVIDER_HOSTED)
        assert not capability.edition_refuses("custom")


# --- Discover install: each skill a package holds, and the official MCP registry ---

_GOOD = ("SKILL.md", "---\nname: fake-skill\ndescription: A fake skill\n---\n# Fake")
_GOOD_SUB = ("sub/SKILL.md", "---\nname: good\n---\n# Good")
_EVIL = ("extras/evil/SKILL.md", "---\nname: evil\n---\n# Evil")
_OTHER = ("extras/other/SKILL.md", "---\nname: other\n---\n# Other")
_NO_SKILL = ("README.md", "# no skill file")

#: What a local tier writes to try to open install back up.
_INSTALL_WIDENING = {
    "capabilities": {
        "extension_install": {
            "enabled": True,
            "scopes": {"extensions": {"mode": "allow", "allow": ["*"]}},
        }
    }
}

#: What a local tier writes to narrow install under a remote that allows all.
_INSTALL_NARROWING = {
    "capabilities": {
        "extension_install": {
            "enabled": True,
            "scopes": {
                "extensions": {"mode": "deny", "deny": ["skill:evil", "mcp:io.github.acme/tool"]}
            },
        }
    }
}

_INSTALL_TIERS = [None, *_TIERS]


@pytest.fixture(name="discover_env")
def discover_env_fixture(tmp_path, monkeypatch):
    """A temporary home for the skills directory, a stand-in SEL, and the provider
    registries put back after the test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(home / ".kiro" / "crew"))
    monkeypatch.setattr(Path, "home", lambda: home)
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    monkeypatch.setattr(discover_mod, "_sel", lambda: fake)
    monkeypatch.setattr(discover_mod, "_registry", discover_mod._registry)
    monkeypatch.setattr(mcp_discover, "_registry", mcp_discover._registry)
    return home


class _SkillProvider:
    name = "fakeprov"
    display_name = "Fake Provider"

    def __init__(self, bundle: list) -> None:
        self._bundle = bundle
        self.fetched = 0

    def is_available(self) -> bool:
        return True

    async def search(self, query, *, limit=20):
        return []

    async def fetch_skill_content(self, skill_id):
        return None

    async def fetch_skill_bundle(self, skill_id):
        self.fetched += 1
        return list(self._bundle)


async def _install_skill(home: Path, bundle: list) -> tuple:
    """POST one Discover install of *bundle*; ``(status, body, installed)``."""
    skills_dir = home / ".kiro" / "crew" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    provider = _SkillProvider(bundle)
    registry = SkillProviderRegistry()
    registry.register(provider)
    discover_mod._registry = registry
    state = MagicMock(_slots={}, context_builder=None)
    state._standalone_skills = SkillsLoader(skills_path=skills_dir, install_builtins=False)
    state.owner_id = ""
    app = web.Application()
    app["state"] = state
    as_owner(app)
    app.router.add_post("/api/skills/-/discover/install", discover_mod.api_skills_discover_install)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/skills/-/discover/install",
            json={"provider": "fakeprov", "skill_id": "fake-skill"},
        )
        body = await resp.json()
    finally:
        await client.close()
    target = skills_dir / "fakeprov" / "fake-skill"
    installed = (target / "SKILL.md").is_file()
    if resp.status != 200:
        # A refusal writes nothing: no skill and no leftover stage.
        assert not target.exists()
        if target.parent.exists():
            assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]
        assert body["code"] == "blocked_by_policy"
    return resp.status, body, installed


class _Official:
    name = "official"
    display_name = "MCP Registry"

    def __init__(self) -> None:
        self.fetched: list = []

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[Any]:
        return []

    async def fetch_detail(self, server_id: str) -> McpServerDetail | None:
        self.fetched.append(server_id)
        plan = McpInstallPlan(method="uvx", spec={"command": "uvx", "args": [server_id]})
        return McpServerDetail(
            id=server_id, name=server_id, description="", provider="official", install_plan=plan
        )


async def _plans_mcp(server_id: str) -> bool:
    """Whether the official registry plan installs *server_id*; a refusal fetches nothing."""
    official = _Official()
    registry = McpProviderRegistry()
    registry.register(official)
    mcp_discover._registry = registry
    try:
        await mcp_discover.official_install_plan(server_id)
    except mcp_discover.OfficialPlanError as err:
        assert (err.status, err.code) == (403, "blocked_by_policy")
        assert official.fetched == []
        return False
    assert official.fetched == [server_id]
    return True


#: Per install reply: bundles that install, bundles that are refused, and the
#: registry servers that are planned and refused.
_INSTALL_EXPECTED = {
    "install_deny": {
        "installs": [[_GOOD], [_GOOD, _GOOD_SUB, _OTHER]],
        "refused": [[_GOOD, _EVIL], [_GOOD_SUB, _EVIL, _OTHER], [_NO_SKILL]],
        "planned": ["io.github.acme/tool", "github"],
        "denied": ["io.github.acme/evil"],
    },
    "install_allow": {
        "installs": [[_GOOD], [_GOOD, _GOOD_SUB]],
        "refused": [[_GOOD, _OTHER], [_GOOD, _EVIL], [_NO_SKILL]],
        "planned": ["io.github.acme/tool"],
        "denied": ["io.github.acme/other", "github"],
    },
    "allow_all": {
        "installs": [[_GOOD, _EVIL, _OTHER]],
        "refused": [[_NO_SKILL]],
        "planned": ["io.github.acme/tool", "io.github.acme/evil"],
        "denied": [],
    },
    "allow_builtin": {
        "installs": [[_GOOD, _EVIL, _OTHER]],
        "refused": [[_NO_SKILL]],
        # ``builtin`` does not reach ``mcp``, so MCP install stays off.
        "planned": [],
        "denied": ["io.github.acme/tool"],
    },
}


@pytest.mark.asyncio
class TestDiscoverInstallFollowsTheRemote:
    @pytest.mark.parametrize("remote", sorted(_INSTALL_REMOTES), indirect=True)
    @pytest.mark.parametrize("tier", _INSTALL_TIERS)
    async def test_a_local_tier_cannot_widen_skill_install(
        self, discover_env, boot, remote, tier, tmp_path, monkeypatch, profiles_dir
    ):
        if tier is not None:
            _local_tier(tier, _INSTALL_WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        expected = _INSTALL_EXPECTED[remote]
        for bundle in expected["installs"]:
            status, body, installed = await _install_skill(discover_env, bundle)
            assert (status, installed) == (200, True), (remote, tier, bundle, body)
            # Remove it, so the next install starts from nothing.
            shutil.rmtree(discover_env / ".kiro" / "crew" / "skills" / "fakeprov")
        for bundle in expected["refused"]:
            status, body, installed = await _install_skill(discover_env, bundle)
            assert (status, installed) == (403, False), (remote, tier, bundle)

    @pytest.mark.parametrize("remote", ["install_deny"], indirect=True)
    @pytest.mark.parametrize("tier", _INSTALL_TIERS)
    async def test_the_refusal_names_the_denied_skill_or_the_unread_package(
        self, discover_env, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        if tier is not None:
            _local_tier(tier, _INSTALL_WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        _status, body, _installed = await _install_skill(discover_env, [_GOOD, _EVIL])
        assert "'evil'" in body["error"], tier
        _status, body, _installed = await _install_skill(discover_env, [_NO_SKILL])
        assert "cannot list the skills" in body["error"], tier

    @pytest.mark.parametrize("remote", sorted(_INSTALL_REMOTES), indirect=True)
    @pytest.mark.parametrize("tier", _INSTALL_TIERS)
    async def test_a_local_tier_cannot_widen_official_mcp_install(
        self, discover_env, boot, remote, tier, tmp_path, monkeypatch, profiles_dir
    ):
        if tier is not None:
            _local_tier(tier, _INSTALL_WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        expected = _INSTALL_EXPECTED[remote]
        for server in expected["planned"]:
            assert await _plans_mcp(server), (remote, tier, server)
        for server in expected["denied"]:
            assert not await _plans_mcp(server), (remote, tier, server)

    @pytest.mark.parametrize("remote", ["absent"], indirect=True)
    @pytest.mark.parametrize("tier", _INSTALL_TIERS)
    async def test_with_no_install_rule_nothing_installs(
        self, discover_env, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        if tier is not None:
            _local_tier(tier, _INSTALL_WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        status, _body, installed = await _install_skill(discover_env, [_GOOD])
        assert (status, installed) == (403, False), tier
        assert not await _plans_mcp("io.github.acme/tool"), tier


@pytest.mark.asyncio
class TestDiscoverInstallCanNarrow:
    @pytest.mark.parametrize("remote", ["allow_all"], indirect=True)
    @pytest.mark.parametrize("tier", _TIERS)
    async def test_a_local_deny_narrows_allow_all(
        self, discover_env, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, _INSTALL_NARROWING, tmp_path, monkeypatch, profiles_dir)
        boot()
        status, _body, installed = await _install_skill(discover_env, [_GOOD, _OTHER])
        assert (status, installed) == (200, True), tier
        shutil.rmtree(discover_env / ".kiro" / "crew" / "skills" / "fakeprov")
        status, body, installed = await _install_skill(discover_env, [_GOOD, _EVIL])
        assert (status, installed) == (403, False), tier
        assert "'evil'" in body["error"], tier
        assert not await _plans_mcp("io.github.acme/tool"), tier
        assert await _plans_mcp("io.github.acme/other"), tier


@pytest.mark.asyncio
class TestOssDiscoverInstallIgnoresTheRemote:
    @pytest.mark.parametrize("remote", ["install_deny"], indirect=True)
    async def test_no_fetch_and_no_package_check(self, discover_env, signed_in, cps):
        assert governance.load_security_policy() is None
        status, _body, installed = await _install_skill(discover_env, [_GOOD, _EVIL])
        assert (status, installed) == (200, True)
        assert await _plans_mcp("io.github.acme/evil")
        assert cps == []
