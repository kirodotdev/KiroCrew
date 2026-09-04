"""Coverage tests for the skill-discovery handlers (``dashboard/handlers/discover.py``).

``test_skill_discover.py`` drives the happy paths through a live ``TestServer``
(install, overwrite, preview, installs count). What it leaves unexercised is the
guard-clause taxonomy in front of each route and the two fallback shapes behind
them — and those guards are the security-relevant part of this module:

  * **search** — the ``limit`` fallback on a non-numeric query param, the
    empty-query short circuit, and the registry singleton's lazy init;
  * **install** — malformed body, missing/unknown provider, an unusable slug, the
    empty-fetch 404, both size-limit 413s, the single-file (no-bundle) provider
    path in all three of its outcomes, the ``..`` bundle entry that is skipped,
    the ``AGENTS.md``-only bundle that is copied to ``SKILL.md``, and the
    containment refusal when the provider directory is a symlink out of the
    skills root;
  * **preview** — the missing-parameter 400, the unavailable-provider 404, and
    the two ``_empty`` responses (fetch raised, content empty).

Handlers are invoked through ``make_mocked_request`` (no socket bound), the
provider is a local double (no network), ``_skills_dir`` is pinned into
``tmp_path``, and ``_sel`` is replaced so the audit calls can be asserted on
instead of appended to a real event log.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import make_dir_link
from kiro_crew.dashboard.handlers import discover as h
from kiro_crew.skill_providers.base import ProviderRegistry, SkillSearchResult
from kiro_crew.skills import SkillsLoader

_SKILL_MD = "---\nname: cov-skill\ndescription: zzq nonsense payload\n---\n# body\n"


class _BundleProvider:
    """A provider that serves a bundle (the ``fetch_skill_bundle`` path)."""

    def __init__(self, bundle: list[tuple[str, str]] | None = None) -> None:
        self.bundle = bundle if bundle is not None else [("SKILL.md", _SKILL_MD)]
        self.search_calls: list[tuple[str, int]] = []
        self.results: list[SkillSearchResult] = []
        self.available = True

    @property
    def name(self) -> str:
        return "covprov"

    @property
    def display_name(self) -> str:
        return "Cov Provider"

    def is_available(self) -> bool:
        return self.available

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        self.search_calls.append((query, limit))
        return list(self.results)

    async def fetch_skill_bundle(self, skill_id: str) -> list[tuple[str, str]] | None:
        return list(self.bundle) if self.bundle is not None else None

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        return _SKILL_MD


class _SingleFileProvider:
    """A provider WITHOUT ``fetch_skill_bundle`` — the single-file install path."""

    def __init__(self, content: str | None = _SKILL_MD) -> None:
        self.content = content

    @property
    def name(self) -> str:
        return "covprov"

    @property
    def display_name(self) -> str:
        return "Cov Provider"

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        return []

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        return self.content


@pytest.fixture()
def skills_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the module's skills dir into tmp_path — nothing is written elsewhere."""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(h, "_skills_dir", lambda: root)
    return root


@pytest.fixture()
def sel_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    sink = MagicMock()
    monkeypatch.setattr(h, "_sel", lambda: sink)
    return sink


@pytest.fixture()
def state(skills_root: Path) -> MagicMock:
    st = MagicMock(context_builder=None)
    st._standalone_skills = SkillsLoader(skills_path=skills_root, install_builtins=False)
    return st


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> ProviderRegistry:
    """Inject a fresh registry and restore the singleton afterwards."""
    reg = ProviderRegistry()
    monkeypatch.setattr(h, "_registry", reg)
    return reg


def _mk(
    method: str,
    path: str,
    *,
    state: Any,
    body: Any = ...,
    internal_auth: bool = False,
) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, app=app)
    if internal_auth:
        req["internal_auth"] = True
    if body is not ...:
        if body is None:
            req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
        else:
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.StreamResponse) -> Any:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


# --- registry singleton ------------------------------------------------------


def test_build_registry_registers_skillsh() -> None:
    reg = h._build_registry()
    assert reg.provider_names == ["skillsh"]
    assert reg.get("skillsh") is not None


def test_get_registry_is_lazily_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_registry", None)
    first = h._get_registry()
    assert first is h._get_registry()
    assert first.provider_names == ["skillsh"]


def test_slugify_of_empty_string_is_empty() -> None:
    assert h._slugify("") == ""
    assert h._slugify("  My Skill!!  ") == "my-skill"


def test_display_name_falls_back_to_the_raw_name() -> None:
    reg = ProviderRegistry()
    assert h._display_name(reg, "nope") == "nope"


# --- GET /api/skills/-/discover ---------------------------------------------


@pytest.mark.asyncio
async def test_search_with_no_query_short_circuits(state: MagicMock) -> None:
    request = _mk("GET", "/api/skills/-/discover?q=%20%20", state=state)
    assert _body(await h.api_skills_discover(request)) == {"results": [], "providers": []}


@pytest.mark.asyncio
async def test_search_falls_back_to_limit_20_on_a_non_numeric_limit(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    provider = _BundleProvider()
    registry.register(provider)
    request = _mk("GET", "/api/skills/-/discover?q=cov&limit=abc", state=state)
    assert _body(await h.api_skills_discover(request))["providers"] == ["covprov"]
    assert provider.search_calls == [("cov", 20)]


@pytest.mark.asyncio
async def test_search_clamps_a_zero_limit_up_to_one(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    provider = _BundleProvider()
    registry.register(provider)
    request = _mk("GET", "/api/skills/-/discover?q=cov&limit=0", state=state)
    await h.api_skills_discover(request)
    assert provider.search_calls == [("cov", 1)]


@pytest.mark.asyncio
async def test_search_drops_non_string_tags_and_audits_the_search(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    provider = _BundleProvider()
    provider.results = [
        SkillSearchResult(
            id="owner/repo",
            name="repo",
            description="d",
            provider="covprov",
            tags=["ok", 7],  # type: ignore[list-item]
            installs=3,
        )
    ]
    registry.register(provider)
    request = _mk("GET", "/api/skills/-/discover?q=cov", state=state)
    payload = _body(await h.api_skills_discover(request))
    item = payload["results"][0]
    assert item["tags"] == ["ok"]
    assert item["display_provider"] == "Cov Provider"
    assert item["installed"] is False
    assert item["installs"] == 3
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["tool_name"] == "discover_skills"
    assert kwargs["metadata"]["result_count"] == "1"


# --- POST /api/skills/-/discover/install ------------------------------------


@pytest.mark.asyncio
async def test_install_refuses_the_internal_secret_caller(
    state: MagicMock, sel_mock: MagicMock
) -> None:
    request = _mk("POST", "/i", state=state, internal_auth=True)
    response = await h.api_skills_discover_install(request)
    assert response.status == 403
    assert _body(response)["code"] == "human_only"
    assert sel_mock.log_tool_invocation.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_install_400_on_undecodable_body(state: MagicMock) -> None:
    response = await h.api_skills_discover_install(_mk("POST", "/i", state=state, body=None))
    assert response.status == 400
    assert _body(response) == {"error": "Invalid JSON body"}


@pytest.mark.asyncio
async def test_install_400_when_body_is_not_an_object(state: MagicMock) -> None:
    response = await h.api_skills_discover_install(_mk("POST", "/i", state=state, body=[]))
    assert response.status == 400
    assert "JSON object" in _body(response)["error"]


@pytest.mark.asyncio
async def test_install_400_on_a_non_string_field(state: MagicMock) -> None:
    request = _mk("POST", "/i", state=state, body={"provider": 1, "skill_id": "x"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 400
    assert _body(response)["error"] == "'provider' must be a string"


@pytest.mark.asyncio
async def test_install_400_on_a_non_boolean_overwrite(state: MagicMock) -> None:
    """bool("false") is True, so a string must be refused rather than coerced."""
    request = _mk(
        "POST", "/i", state=state, body={"provider": "p", "skill_id": "x", "overwrite": "false"}
    )
    response = await h.api_skills_discover_install(request)
    assert response.status == 400
    assert _body(response)["error"] == "'overwrite' must be a boolean"


@pytest.mark.asyncio
async def test_install_400_when_provider_or_skill_id_is_missing(state: MagicMock) -> None:
    request = _mk("POST", "/i", state=state, body={"provider": "covprov"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 400
    assert "required" in _body(response)["error"]


@pytest.mark.asyncio
async def test_install_404_on_an_unknown_provider(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    request = _mk("POST", "/i", state=state, body={"provider": "ghost", "skill_id": "x"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 404
    assert "not available" in _body(response)["error"]


@pytest.mark.asyncio
async def test_install_404_when_the_provider_reports_unavailable(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    provider = _BundleProvider()
    provider.available = False
    registry.register(provider)
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "x"})
    assert (await h.api_skills_discover_install(request)).status == 404


@pytest.mark.asyncio
async def test_install_400_when_no_safe_slug_can_be_derived(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    registry.register(_BundleProvider())
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "///"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 400
    assert "safe slug" in _body(response)["error"]


@pytest.mark.asyncio
async def test_install_404_when_the_provider_returns_nothing(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    provider = _BundleProvider(bundle=[])
    provider.fetch_skill_content = AsyncMock(return_value=None)  # type: ignore[method-assign]
    registry.register(provider)
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 404
    assert "not found or empty" in _body(response)["error"]


@pytest.mark.asyncio
async def test_install_413_when_the_bundle_exceeds_the_size_limit(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    big = "x" * (5 * 1024 * 1024 + 1)
    registry.register(_BundleProvider(bundle=[("SKILL.md", big)]))
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 413
    assert "5 MiB" in _body(response)["error"]


@pytest.mark.asyncio
async def test_install_413_when_single_file_content_exceeds_the_size_limit(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    registry.register(_SingleFileProvider(content="y" * (5 * 1024 * 1024 + 1)))
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 413
    assert _body(response)["error"] == "Skill content exceeds size limit"


@pytest.mark.asyncio
async def test_install_skips_a_traversal_entry_but_writes_the_rest(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock, skills_root: Path
) -> None:
    registry.register(
        _BundleProvider(
            bundle=[
                ("SKILL.md", _SKILL_MD),
                ("../escaped.md", "nope"),
                ("/abs.md", "nope"),
            ]
        )
    )
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    payload = _body(await h.api_skills_discover_install(request))
    assert payload["file_count"] == 1
    assert payload["kind"] == "created"
    assert (skills_root / "covprov" / "cov-skill" / "SKILL.md").exists()
    assert not (skills_root / "escaped.md").exists()
    assert not (skills_root.parent / "escaped.md").exists()


@pytest.mark.asyncio
async def test_install_copies_agents_md_to_skill_md(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock, skills_root: Path
) -> None:
    """The loader keys off SKILL.md, so an AGENTS.md-only bundle is copied across."""
    registry.register(_BundleProvider(bundle=[("AGENTS.md", _SKILL_MD)]))
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    payload = _body(await h.api_skills_discover_install(request))
    assert payload["file_count"] == 1
    installed = skills_root / "covprov" / "cov-skill"
    assert installed.joinpath("SKILL.md").read_text(encoding="utf-8") == _SKILL_MD


@pytest.mark.asyncio
async def test_install_refuses_to_write_through_a_symlinked_provider_dir(
    state: MagicMock,
    registry: ProviderRegistry,
    sel_mock: MagicMock,
    skills_root: Path,
    tmp_path: Path,
) -> None:
    """A pre-planted symlink parent must not redirect the bundle out of the root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    # make_dir_link, not symlink_to: a directory symlink needs
    # SeCreateSymbolicLinkPrivilege on Windows (WinError 1314 unelevated), while the
    # junction this helper falls back to needs none and is traversed by the same
    # reparse machinery -- so the containment behaviour stays exercised on Windows
    # instead of being skipped there.
    make_dir_link(skills_root / "covprov", outside)
    registry.register(_BundleProvider())
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    payload = _body(await h.api_skills_discover_install(request))
    assert payload["file_count"] == 0
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_install_single_file_provider_creates_the_skill(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock, skills_root: Path
) -> None:
    registry.register(_SingleFileProvider())
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    payload = _body(await h.api_skills_discover_install(request))
    assert payload == {
        "ok": True,
        "key": "covprov/cov-skill",
        "slug": "cov-skill",
        "provider": "covprov",
        "kind": "created",
        "file_count": 1,
    }
    assert (skills_root / "covprov" / "cov-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_install_single_file_provider_updates_an_existing_skill(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock, skills_root: Path
) -> None:
    registry.register(_SingleFileProvider(content="---\nname: n\n---\nsecond"))
    existing = skills_root / "covprov" / "cov-skill"
    existing.mkdir(parents=True)
    existing.joinpath("SKILL.md").write_text("first", encoding="utf-8")
    request = _mk(
        "POST",
        "/i",
        state=state,
        body={"provider": "covprov", "skill_id": "cov-skill", "overwrite": True},
    )
    payload = _body(await h.api_skills_discover_install(request))
    assert payload["kind"] == "updated"
    assert "second" in existing.joinpath("SKILL.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_install_500_when_the_loader_cannot_create_the_skill(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    registry.register(_SingleFileProvider())
    state._standalone_skills.create_skill = MagicMock(return_value=False)
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 500
    assert "Failed to create skill" in _body(response)["error"]


@pytest.mark.asyncio
async def test_install_409_without_overwrite_is_audited_as_denied(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock, skills_root: Path
) -> None:
    registry.register(_BundleProvider())
    (skills_root / "covprov" / "cov-skill").mkdir(parents=True)
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 409
    assert _body(response)["code"] == "exists"
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["outcome"] == "denied"
    assert kwargs["error"] == "already_installed_no_overwrite"


@pytest.mark.asyncio
async def test_install_504_on_a_provider_timeout(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    provider = _BundleProvider()
    provider.fetch_skill_bundle = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.TimeoutError()
    )
    registry.register(provider)
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 504
    assert sel_mock.log_tool_invocation.call_args.kwargs["error"] == "timeout"


@pytest.mark.asyncio
async def test_install_502_scrubs_the_provider_error(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    """A credential echoed in the provider's exception must not reach the audit log.

    The 502 path runs the two SHAPE-based scrubbers (``redact_credentials`` and
    ``redact_exfiltration_urls``), so this asserts on a credential shape. It does
    NOT run ``_redact_external``, so a short opaque value in a credential-named URL
    parameter (``?token=abc123``) survives into the audit record — see the report.
    """
    provider = _BundleProvider()
    provider.fetch_skill_bundle = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("fetch failed for key AKIAIOSFODNN7EXAMPLE")
    )
    registry.register(provider)
    request = _mk("POST", "/i", state=state, body={"provider": "covprov", "skill_id": "cov-skill"})
    response = await h.api_skills_discover_install(request)
    assert response.status == 502
    assert _body(response) == {"error": "Failed to fetch skill from provider"}
    logged = sel_mock.log_tool_invocation.call_args.kwargs["error"]
    assert "AKIAIOSFODNN7EXAMPLE" not in logged
    assert "REDACTED" in logged


# --- GET /api/skills/-/discover/preview -------------------------------------


@pytest.mark.asyncio
async def test_preview_400_without_provider_and_id(state: MagicMock) -> None:
    response = await h.api_skills_discover_preview(_mk("GET", "/p?provider=covprov", state=state))
    assert response.status == 400
    assert "required" in _body(response)["error"]


@pytest.mark.asyncio
async def test_preview_404_on_an_unknown_provider(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    request = _mk("GET", "/p?provider=ghost&id=x", state=state)
    response = await h.api_skills_discover_preview(request)
    assert response.status == 404
    assert "not available" in _body(response)["error"]


@pytest.mark.asyncio
async def test_preview_falls_back_to_single_file_fetch_when_the_bundle_is_empty(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    registry.register(_BundleProvider(bundle=[]))
    request = _mk("GET", "/p?provider=covprov&id=cov-skill", state=state)
    payload = _body(await h.api_skills_discover_preview(request))
    assert payload["name"] == "cov-skill"
    assert payload["description"] == "zzq nonsense payload"
    assert payload["files"] == [] and payload["file_count"] == 0
    assert sel_mock.log_tool_invocation.call_args.kwargs["outcome"] == "success"


@pytest.mark.asyncio
async def test_preview_returns_empty_when_the_fetch_raises(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    provider = _BundleProvider()
    provider.fetch_skill_bundle = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("down")
    )
    registry.register(provider)
    request = _mk("GET", "/p?provider=covprov&id=cov-skill", state=state)
    payload = _body(await h.api_skills_discover_preview(request))
    assert payload == {"description": "", "name": "", "content": "", "files": [], "file_count": 0}
    assert sel_mock.log_tool_invocation.call_args.kwargs["error"] == "fetch_failed"


@pytest.mark.asyncio
async def test_preview_returns_empty_when_the_content_is_blank(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    registry.register(_SingleFileProvider(content=""))
    request = _mk("GET", "/p?provider=covprov&id=cov-skill", state=state)
    payload = _body(await h.api_skills_discover_preview(request))
    assert payload["file_count"] == 0
    assert sel_mock.log_tool_invocation.call_args.kwargs["error"] == "empty_content"


@pytest.mark.asyncio
async def test_preview_prefers_agents_md_over_another_markdown_file(
    state: MagicMock, registry: ProviderRegistry, sel_mock: MagicMock
) -> None:
    """Install copies AGENTS.md to SKILL.md, so the preview must parse the same file."""
    registry.register(_BundleProvider(bundle=[("README.md", "# readme"), ("AGENTS.md", _SKILL_MD)]))
    request = _mk("GET", "/p?provider=covprov&id=cov-skill", state=state)
    payload = _body(await h.api_skills_discover_preview(request))
    assert payload["name"] == "cov-skill"
    assert payload["files"] == ["README.md", "AGENTS.md"]
    assert payload["file_count"] == 2
