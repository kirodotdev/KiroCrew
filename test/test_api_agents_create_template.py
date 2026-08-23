"""Tests for the ``kiro_agent`` template contract on POST /api/agents.

``kiro_agent`` used to default to ``"kirocrew"`` when a create request omitted
it. Because dispatch flattens a crew alias to its ``kiro_agent`` pointer
(``config.loader.resolve_agent_bindings``), such a crew was offered in the chat
picker and then the DEFAULT agent answered — the "picker reverts to default"
report behind #1684, with only a log line marking the substitution.

The contract these tests pin:

* an omitted, blank or null ``kiro_agent`` is REFUSED (400) — the caller must be
  explicit, so the silent alias-to-default state is unreachable;
* the name must satisfy the shared agent-name grammar, checked before it is used
  to look anything up;
* existence is resolved through ``list_agents()`` — the hardened spec reader —
  never a raw filesystem probe;
* ``"kirocrew"`` stays a perfectly legal explicit CHOICE, because a crew that
  boots the built-in agent against its own workspace/memory store is the common
  case and must keep working;
* an unknown template is accepted with a WARNING rather than refused, matching
  the sync path's EXECUTABLE INVARIANT posture — an edition may resolve a row
  this listing cannot see, and hard-refusing it would itself be a bug.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run as the dashboard owner: these tests exercise handler behavior PAST
    the owner boundary on the agents module's mutating endpoints, which has
    its own enumerate-the-invariant coverage in
    test_agents_endpoints_owner_auth.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.agents.is_owner_dashboard_request",
        lambda request: True,
    )


def _fake_config():
    """A stand-in KiroCrewConfig recording whether save() was reached."""
    saved: list[bool] = []
    return SimpleNamespace(
        agent=SimpleNamespace(provider="acp"),
        agents={},
        default_agent="kirocrew",
        save=lambda: saved.append(True),
        saved=saved,
    )


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents_create

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    return app


async def _post(body, cfg, installed=(), spy=None):
    """POST *body* with the installed-agent listing stubbed to *installed*.

    ``list_agents`` is patched rather than the agents directory: the handler
    resolves existence through it precisely so the read goes via the hardened
    spec reader, and stubbing it keeps the test off the real data home. Pass
    *spy* to observe whether the listing was consulted at all.
    """
    rows = [SimpleNamespace(name=n) for n in installed]

    def _list_agents(*args, **kwargs):
        if spy is not None:
            spy.append(True)
        return rows

    with (
        patch(
            "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
            return_value=cfg,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            new=_list_agents,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents._sel",
            return_value=SimpleNamespace(
                log_api_access=lambda **kwargs: None,
            ),
        ),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/agents", json=body)
            return resp.status, await resp.json()


class TestTemplateMustBeExplicit:
    @pytest.mark.asyncio
    async def test_omitted_template_is_refused(self):
        cfg = _fake_config()
        status, data = await _post({"name": "researcher"}, cfg)
        assert status == 400
        assert "kiro_agent" in data["error"]
        # The machine-readable code is the contract; `error` is advisory prose the
        # dashboard cannot localize (test_error_code_contract enforces this).
        assert data["code"] == "kiro_agent_required"
        # The crew must NOT exist: a 400 that still wrote the alias would leave
        # exactly the broken row the refusal exists to prevent.
        assert cfg.agents == {}
        assert cfg.saved == []

    @pytest.mark.asyncio
    async def test_blank_template_is_refused(self):
        cfg = _fake_config()
        status, data = await _post({"name": "researcher", "kiro_agent": "   "}, cfg)
        assert status == 400
        assert data["code"] == "kiro_agent_required"
        assert cfg.agents == {}

    @pytest.mark.asyncio
    async def test_null_template_is_refused(self):
        """``{"kiro_agent": null}`` must not slip past a `.get(...)` default."""
        cfg = _fake_config()
        status, data = await _post({"name": "researcher", "kiro_agent": None}, cfg)
        assert status == 400
        assert data["code"] == "kiro_agent_required"
        assert cfg.agents == {}


class TestTemplateNameGrammar:
    """The template name must satisfy the shared agent-name grammar.

    A name carrying path separators, traversal or wildcards cannot identify an
    agent, and storing it would leave the crew pointing at nothing. Refusing it
    here also keeps such a value away from every downstream lookup.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            "**",
            "*",
            "?",
            "[a-z]",
            "a/b",  # separator: would escape the agents dir
            "../etc",
            "-leading-hyphen",
            "trailing-hyphen-",
            "with space",
            "x" * 65,  # over the 64-char grammar ceiling
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_template_name_is_refused(self, bad):
        cfg = _fake_config()
        status, data = await _post({"name": "researcher", "kiro_agent": bad}, cfg)
        assert status == 400, f"{bad!r} produced {status}, not a refusal"
        assert data["code"] == "invalid_kiro_agent_name"
        assert cfg.agents == {}

    @pytest.mark.asyncio
    async def test_invalid_name_never_reaches_the_agent_listing(self):
        """Ordering matters: the grammar gate runs BEFORE the lookup.

        The lookup walks the agent directories, so a name that cannot name an
        agent should be rejected without touching it. This pins the ordering
        rather than only the status code, which a later edit could otherwise
        reverse while keeping every other test in this file green.
        """
        cfg = _fake_config()
        spy: list[bool] = []
        status, _ = await _post({"name": "researcher", "kiro_agent": "../etc"}, cfg, spy=spy)
        assert status == 400
        assert spy == [], "the agent listing was consulted for an invalid name"

    @pytest.mark.asyncio
    async def test_underscores_and_digits_remain_valid(self):
        """The guard must not narrow the legitimate name space."""
        cfg = _fake_config()
        status, _ = await _post(
            {"name": "crew-d", "kiro_agent": "my_agent2"}, cfg, installed=("my_agent2",)
        )
        assert status == 200
        assert cfg.agents["crew-d"].kiro_agent == "my_agent2"


class TestExplicitTemplateStillWorks:
    @pytest.mark.asyncio
    async def test_builtin_kirocrew_is_a_legal_choice(self):
        """Only the silent DEFAULT is refused — the value itself is legitimate."""
        cfg = _fake_config()
        status, data = await _post(
            {"name": "researcher", "kiro_agent": "kirocrew", "workspace": "research"},
            cfg,
            installed=("kirocrew",),
        )
        assert status == 200
        assert data["ok"] is True
        assert cfg.agents["researcher"].kiro_agent == "kirocrew"
        assert cfg.agents["researcher"].workspace == "research"

    @pytest.mark.asyncio
    async def test_listed_template_creates_without_warning(self, caplog):
        cfg = _fake_config()
        with caplog.at_level(logging.WARNING):
            status, _ = await _post(
                {"name": "crew-a", "kiro_agent": "reviewer"},
                cfg,
                installed=("kirocrew", "reviewer"),
            )
        assert status == 200
        assert "not in the installed agent listing" not in caplog.text


class TestMissingTemplateWarnsButCreates:
    @pytest.mark.asyncio
    async def test_unknown_template_warns_and_still_creates(self, caplog):
        cfg = _fake_config()
        with caplog.at_level(logging.WARNING):
            status, _ = await _post(
                {"name": "crew-c", "kiro_agent": "not-installed"},
                cfg,
                installed=("kirocrew",),
            )
        # Accepted (an edition may resolve it even when unlisted)…
        assert status == 200
        assert cfg.agents["crew-c"].kiro_agent == "not-installed"
        # …but the substitution risk is on the record rather than silent.
        assert "not in the installed agent listing" in caplog.text
        assert "not-installed" in caplog.text
