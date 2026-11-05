"""Every skill-discovery error reply carries a stable ``code``.

``dashboard/handlers/discover.py`` answered fifteen refusals with prose only, so a
client could tell them apart by status alone or by matching English text. Each one
now carries a ``code`` next to the unchanged ``error`` and status: this file pins all
three per refusal, so a later edit cannot drop the code or reword the message
without a test saying so.

The ``'<field>' must be a string`` and ``'overwrite' must be a boolean`` refusals use
``invalid_field_type``, the code the other dashboard handlers give a wrong-typed
field (``_shared_request_body.require_str_field``).

Handlers are called through ``make_mocked_request``; the provider is a local
double, the skills dir is pinned into ``tmp_path`` and ``_sel`` is replaced, so no
network, no real skills dir and no real audit log are touched.
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

from kiro_crew.dashboard.handlers import discover as h
from kiro_crew.skill_providers.base import ProviderRegistry, SkillSearchResult
from kiro_crew.skills import SkillsLoader

_SKILL_MD = "---\nname: code-skill\ndescription: d\n---\n# body\n"
_TOO_BIG = 5 * 1024 * 1024 + 1


class _Provider:
    """A provider double; ``bundle=None`` drops the bundle method (single-file path)."""

    def __init__(
        self,
        *,
        bundle: list[tuple[str, str]] | None | str = "default",
        content: str | None = _SKILL_MD,
        available: bool = True,
    ) -> None:
        self._bundle = [("SKILL.md", _SKILL_MD)] if bundle == "default" else bundle
        self._content = content
        self._available = available

    @property
    def name(self) -> str:
        return "codeprov"

    @property
    def display_name(self) -> str:
        return "Code Provider"

    def is_available(self) -> bool:
        return self._available

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        return []

    async def fetch_skill_bundle(self, skill_id: str) -> list[tuple[str, str]] | None:
        return list(self._bundle) if isinstance(self._bundle, list) else None

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        return self._content


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(h, "_skills_dir", lambda: root)
    monkeypatch.setattr(h, "_sel", lambda: MagicMock())
    st = MagicMock(context_builder=None, owner_id="")
    st._standalone_skills = SkillsLoader(skills_path=root, install_builtins=False)
    return st


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> ProviderRegistry:
    reg = ProviderRegistry()
    monkeypatch.setattr(h, "_registry", reg)
    return reg


def _request(method: str, path: str, state: Any, body: Any = ...) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, app=app)
    # The owner: install is owner-gated, and these refusals lie behind the gate.
    req["user"] = "local-app"
    req["app"] = ""
    if body is not ...:
        if body is None:
            req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
        else:
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _json(response: web.StreamResponse) -> Any:
    assert isinstance(response, web.Response)
    assert isinstance(response.body, bytes)
    return json.loads(response.body.decode("utf-8"))


_OK = {"provider": "codeprov", "skill_id": "code-skill"}

# (id, request body, provider double or None, status, error, code)
_INSTALL_CASES = [
    ("undecodable", None, None, 400, "Invalid JSON body", "invalid_json"),
    ("not-object", [], None, 400, "Request body must be a JSON object", "body_not_object"),
    (
        "non-string",
        {"provider": 1, "skill_id": "x"},
        None,
        400,
        "'provider' must be a string",
        "invalid_field_type",
    ),
    (
        "non-bool-overwrite",
        {**_OK, "overwrite": "false"},
        None,
        400,
        "'overwrite' must be a boolean",
        "invalid_field_type",
    ),
    (
        "missing-id",
        {"provider": "codeprov"},
        None,
        400,
        "Both 'provider' and 'skill_id' are required",
        "provider_and_skill_id_required",
    ),
    (
        "unknown-provider",
        {"provider": "ghost", "skill_id": "x"},
        None,
        404,
        "Provider 'ghost' is not available",
        "provider_unavailable",
    ),
    (
        "unavailable-provider",
        _OK,
        _Provider(available=False),
        404,
        "Provider 'codeprov' is not available",
        "provider_unavailable",
    ),
    (
        "no-safe-slug",
        {"provider": "codeprov", "skill_id": "///"},
        _Provider(),
        400,
        "Cannot derive safe slug from '///'",
        "invalid_slug",
    ),
    (
        "empty-fetch",
        _OK,
        _Provider(bundle=[], content=None),
        404,
        "Skill 'code-skill' not found or empty on codeprov",
        "skill_not_found",
    ),
    (
        "bundle-too-large",
        _OK,
        _Provider(bundle=[("SKILL.md", "x" * _TOO_BIG)]),
        413,
        "Skill bundle exceeds size limit (5 MiB)",
        "skill_too_large",
    ),
    (
        "content-too-large",
        _OK,
        _Provider(bundle=None, content="y" * _TOO_BIG),
        413,
        "Skill content exceeds size limit",
        "skill_too_large",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "provider", "status", "error", "code"),
    [case[1:] for case in _INSTALL_CASES],
    ids=[case[0] for case in _INSTALL_CASES],
)
async def test_install_refusal_carries_its_code(
    state: MagicMock,
    registry: ProviderRegistry,
    body: Any,
    provider: _Provider | None,
    status: int,
    error: str,
    code: str,
) -> None:
    if provider is not None:
        registry.register(provider)
    response = await h.api_skills_discover_install(_request("POST", "/i", state, body))
    assert response.status == status
    assert _json(response) == {"error": error, "code": code}


@pytest.mark.asyncio
async def test_install_timeout_carries_fetch_timeout(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    provider = _Provider()
    provider.fetch_skill_bundle = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.TimeoutError()
    )
    registry.register(provider)
    response = await h.api_skills_discover_install(_request("POST", "/i", state, _OK))
    assert response.status == 504
    assert _json(response) == {"error": "Fetch timed out", "code": "fetch_timeout"}


@pytest.mark.asyncio
async def test_install_fetch_failure_carries_fetch_failed_and_no_exception_text(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    provider = _Provider()
    provider.fetch_skill_bundle = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("upstream said something private")
    )
    registry.register(provider)
    response = await h.api_skills_discover_install(_request("POST", "/i", state, _OK))
    assert response.status == 502
    assert _json(response) == {
        "error": "Failed to fetch skill from provider",
        "code": "fetch_failed",
    }


@pytest.mark.asyncio
async def test_install_create_failure_carries_skill_create_failed(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    registry.register(_Provider(bundle=None))
    state._standalone_skills.create_skill = MagicMock(return_value=False)
    response = await h.api_skills_discover_install(_request("POST", "/i", state, _OK))
    assert response.status == 500
    assert _json(response) == {
        "error": "Failed to create skill at 'codeprov/code-skill'",
        "code": "skill_create_failed",
    }


@pytest.mark.asyncio
async def test_preview_without_provider_and_id_carries_its_code(state: MagicMock) -> None:
    response = await h.api_skills_discover_preview(_request("GET", "/p?provider=codeprov", state))
    assert response.status == 400
    assert _json(response) == {
        "error": "Both 'provider' and 'id' are required",
        "code": "provider_and_id_required",
    }


@pytest.mark.asyncio
async def test_preview_unknown_provider_carries_provider_unavailable(
    state: MagicMock, registry: ProviderRegistry
) -> None:
    response = await h.api_skills_discover_preview(_request("GET", "/p?provider=ghost&id=x", state))
    assert response.status == 404
    assert _json(response) == {
        "error": "Provider 'ghost' is not available",
        "code": "provider_unavailable",
    }
