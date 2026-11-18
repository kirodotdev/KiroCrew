"""LTS: a Discover install asks the ``skill`` and ``mcp`` install terms (RFC D13).

``/api/skills/-/discover/install`` checks each skill the fetched bundle holds before
anything is written; the official MCP registry plan (``official_install_plan``, read
by ``/api/mcp/discover/install`` and by ``resolve_server``) checks the registry id
before the entry is fetched. With no rule the LTS baseline keeps install off. The
``oss`` edition asks neither.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import discover as discover_mod
from kiro_crew.dashboard.handlers import mcp_discover
from kiro_crew.dashboard.handlers.member_capability_catalog import resolve_server
from kiro_crew.mate_capability_catalog import AdditionError
from kiro_crew.mcp_providers.base import McpInstallPlan, McpServerDetail, ProviderRegistry
from kiro_crew.platform.governance import parse_policy
from kiro_crew.skill_providers.base import ProviderRegistry as SkillProviderRegistry

pytestmark = pytest.mark.usefixtures("fake_sel")

_GOOD = ("SKILL.md", "---\nname: fake-skill\ndescription: A fake skill\n---\n# Fake")
_EVIL = ("extras/evil/SKILL.md", "---\nname: evil\ndescription: x\n---\n# Evil")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kiro" / "crew"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def fake_sel(monkeypatch):
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    monkeypatch.setattr(discover_mod, "_sel", lambda: fake)
    return fake


@pytest.fixture
def lts(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture(autouse=True)
def restore_registries():
    old_skill, old_mcp = discover_mod._registry, mcp_discover._registry
    yield
    discover_mod._registry, mcp_discover._registry = old_skill, old_mcp


def _policy(monkeypatch, body: dict | None) -> None:
    from kiro_crew.platform import context as pc

    ceiling = parse_policy({"version": 1, "boot": {}, **body}) if body is not None else None
    monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=ceiling))


def _extensions(mode: str, items: list[str]) -> dict:
    return {
        "capabilities": {
            "extension_install": {"scopes": {"extensions": {"mode": mode, mode: items}}}
        }
    }


class _SkillProvider:
    name = "fakeprov"
    display_name = "Fake Provider"

    def __init__(self, bundle: list[tuple[str, str]]) -> None:
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


async def _install(fake_home: Path, provider: _SkillProvider) -> tuple[int, dict, Path]:
    from kiro_crew.skills import SkillsLoader

    skills_dir = fake_home / ".kiro" / "crew" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    state = MagicMock(_slots={}, context_builder=None)
    state._standalone_skills = SkillsLoader(skills_path=skills_dir, install_builtins=False)
    state.owner_id = ""
    registry = SkillProviderRegistry()
    registry.register(provider)
    discover_mod._registry = registry
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
        return resp.status, await resp.json(), skills_dir / "fakeprov" / "fake-skill"
    finally:
        await client.close()


@pytest.mark.asyncio
class TestDiscoverSkillInstall:
    async def test_lts_with_no_rule_refuses_before_the_fetch(self, lts, fake_home, monkeypatch):
        _policy(monkeypatch, {})
        provider = _SkillProvider([_GOOD])
        status, body, target = await _install(fake_home, provider)
        assert (status, body["code"]) == (403, "blocked_by_policy")
        assert provider.fetched == 0 and not target.exists()

    async def test_lts_a_denied_skill_in_the_bundle_refuses_it(self, lts, fake_home, monkeypatch):
        _policy(monkeypatch, _extensions("deny", ["skill:evil", "plugin:*"]))
        status, body, target = await _install(fake_home, _SkillProvider([_GOOD, _EVIL]))
        assert (status, body["code"]) == (403, "blocked_by_policy")
        assert "'evil'" in body["error"]
        assert not target.exists()

    async def test_lts_allowed_skills_install(self, lts, fake_home, monkeypatch):
        _policy(monkeypatch, _extensions("allow", ["skill:fake-skill"]))
        status, body, target = await _install(fake_home, _SkillProvider([_GOOD]))
        assert status == 200 and body["ok"] is True
        assert (target / "SKILL.md").is_file()

    async def test_lts_allow_all_installs(self, lts, fake_home, monkeypatch):
        _policy(monkeypatch, _extensions("allow", ["skill:*", "mcp:*", "agent:*"]))
        status, _body, target = await _install(fake_home, _SkillProvider([_GOOD, _EVIL]))
        assert status == 200 and (target / "SKILL.md").is_file()

    async def test_lts_a_bundle_with_no_readable_skill_is_refused(
        self, lts, fake_home, monkeypatch
    ):
        _policy(monkeypatch, _extensions("allow", ["skill:*"]))
        status, body, target = await _install(fake_home, _SkillProvider([("README.md", "# x")]))
        assert (status, body["code"]) == (403, "blocked_by_policy")
        assert "cannot list the skills" in body["error"]
        assert not target.exists()

    async def test_oss_with_no_policy_is_unchanged(self, fake_home, monkeypatch):
        _policy(monkeypatch, None)
        provider = _SkillProvider([_GOOD, _EVIL])
        status, _body, target = await _install(fake_home, provider)
        assert status == 200 and (target / "SKILL.md").is_file()


_EVIL_MD = "---\nname: evil\n---\n"
_GOOD_SUB = ("sub/SKILL.md", "---\nname: good\n---\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bundle",
    [
        # The writer lands ``./AGENTS.md`` as AGENTS.md and copies it to SKILL.md.
        [("./AGENTS.md", _EVIL_MD), _GOOD_SUB],
        # The writer drops a trailing slash.
        [("SKILL.md/", _EVIL_MD), _GOOD_SUB],
        # The loader strips a byte-order mark.
        [("SKILL.md", "\ufeff" + _EVIL_MD)],
        # A case variant is still a skill file.
        [_GOOD, ("x/skill.md", _EVIL_MD)],
    ],
    ids=["dot-agents", "trailing-slash", "bom", "case"],
)
async def test_lts_names_are_read_as_installed(lts, fake_home, monkeypatch, bundle):
    _policy(monkeypatch, _extensions("deny", ["skill:evil", "plugin:*"]))
    status, body, target = await _install(fake_home, _SkillProvider(bundle))
    assert (status, body["code"]) == (403, "blocked_by_policy")
    assert "'evil'" in body["error"]
    assert not target.exists()
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]


@pytest.mark.asyncio
async def test_lts_a_refusal_redacts_provider_text(lts, fake_home, monkeypatch):
    from kiro_crew.testing.synthetic_credentials import github_classic

    secret = github_classic()
    _policy(monkeypatch, _extensions("allow", ["skill:fake-skill"]))
    bundle = [("SKILL.md", f"---\nname: x-{secret}\n---\n")]
    status, body, _target = await _install(fake_home, _SkillProvider(bundle))
    assert status == 403 and secret not in body["error"]


def test_staged_skill_names(tmp_path) -> None:
    (tmp_path / "SKILL.md").write_text("# no frontmatter")
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "SKILL.md").write_text("---\nname: nested\n---\n")
    (tmp_path / "a" / "README.md").write_text("# x")
    assert sorted(discover_mod._staged_skill_names(tmp_path, "prov/slug")) == [
        "nested",
        "prov/slug",
    ]
    (tmp_path / "a" / "b" / "SKILL.md").write_text("# no name")
    assert sorted(discover_mod._staged_skill_names(tmp_path, "prov/slug")) == [
        "prov/slug",
        "prov/slug/a/b",
    ]


@pytest.mark.asyncio
async def test_lts_a_nameless_skill_is_judged_by_its_catalog_key(lts, fake_home, monkeypatch):
    """The loader names a skill with no frontmatter ``name`` by its catalog key."""
    _policy(monkeypatch, _extensions("deny", ["skill:fakeprov/fake-skill", "plugin:*"]))
    status, body, target = await _install(fake_home, _SkillProvider([("SKILL.md", "# x")]))
    assert (status, body["code"]) == (403, "blocked_by_policy")
    assert not target.exists()


class _Official:
    name = "official"
    display_name = "MCP Registry"

    def __init__(self) -> None:
        self.fetched: list[str] = []

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


def _official(monkeypatch) -> _Official:
    official = _Official()
    registry = ProviderRegistry()
    registry.register(official)
    monkeypatch.setattr(mcp_discover, "_registry", registry)
    return official


@pytest.mark.asyncio
class TestOfficialMcpPlan:
    async def test_lts_with_no_rule_refuses_before_the_fetch(self, lts, fake_home, monkeypatch):
        _policy(monkeypatch, {})
        official = _official(monkeypatch)
        with pytest.raises(mcp_discover.OfficialPlanError) as err:
            await mcp_discover.official_install_plan("io.github.acme/tool")
        assert (err.value.status, err.value.code) == (403, "blocked_by_policy")
        assert official.fetched == []

    async def test_lts_a_named_server_is_planned(self, lts, fake_home, monkeypatch):
        _policy(monkeypatch, _extensions("allow", ["mcp:io.github.acme/tool"]))
        official = _official(monkeypatch)
        await mcp_discover.official_install_plan("io.github.acme/tool")
        assert official.fetched == ["io.github.acme/tool"]
        with pytest.raises(mcp_discover.OfficialPlanError):
            await mcp_discover.official_install_plan("io.github.acme/other")

    async def test_lts_refusal_reaches_a_server_request(self, lts, fake_home, monkeypatch):
        _policy(monkeypatch, {})
        _official(monkeypatch)
        with pytest.raises(AdditionError) as err:
            await resolve_server(None, "registry", "io.github.acme/tool", settings_hint="Settings")
        assert err.value.code == "blocked_by_policy"

    async def test_oss_with_no_policy_is_unchanged(self, fake_home, monkeypatch):
        _policy(monkeypatch, None)
        official = _official(monkeypatch)
        await mcp_discover.official_install_plan("io.github.acme/tool")
        assert official.fetched == ["io.github.acme/tool"]

    async def test_lts_a_refusal_redacts_the_registry_id(self, lts, fake_home, monkeypatch):
        from kiro_crew.testing.synthetic_credentials import github_classic

        secret = github_classic()
        _policy(monkeypatch, _extensions("allow", ["mcp:github"]))
        _official(monkeypatch)
        with pytest.raises(mcp_discover.OfficialPlanError) as err:
            await mcp_discover.official_install_plan(f"x-{secret}")
        assert err.value.code == "blocked_by_policy" and secret not in err.value.message
