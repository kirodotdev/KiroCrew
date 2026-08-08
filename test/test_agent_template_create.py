"""Tests for authoring agent templates from the dashboard.

Two capabilities, both previously absent — the agents dir could only be written
by a package install, an app, or by hand:

* CREATE — ``POST /api/agents/detail`` writes ``<name>.json``, either from a
  conservative blank baseline or as a copy of an existing template.
* EDIT — ``PATCH /api/agents/detail/{name}`` accepts ``prompt`` and
  ``description``, so a created template can be corrected without hand-editing
  JSON.

The privilege surface (``tools`` / ``allowedTools`` / ``toolsSettings``) is
deliberately not writable through either verb; the tests below pin that.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.agent_discovery import clear_list_agents_cache
from kiro_crew.dashboard.handlers.agents import (
    _BLANK_TEMPLATE_ALLOWED_TOOLS,
    api_agent_create,
    api_agent_detail,
)


@pytest.fixture(autouse=True)
def _no_agent_cache():
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


def _request(method: str, body: dict | list | None = None, name: str = "") -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.method = method
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}
    request.get = lambda key, default=None: default

    async def _json():
        if body is None:
            raise json.JSONDecodeError("no body", "", 0)
        return body

    request.json = _json
    return request


async def _create(agents_dir: Path, body: dict | list | None) -> web.Response:
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        return await api_agent_create(_request("POST", body))


async def _patch(agents_dir: Path, name: str, body: dict) -> web.Response:
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        return await api_agent_detail(_request("PATCH", body, name=name))


async def _get(agents_dir: Path, name: str) -> web.Response:
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        return await api_agent_detail(_request("GET", None, name=name))


def _spec(resp: web.Response) -> dict:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


# ── CREATE: the blank baseline ──


class TestCreateBlank:
    @pytest.mark.asyncio
    async def test_writes_a_named_spec(self, agents_dir):
        resp = await _create(agents_dir, {"name": "researcher"})

        assert resp.status == 200
        written = json.loads((agents_dir / "researcher.json").read_text(encoding="utf-8"))
        assert written["name"] == "researcher"

    @pytest.mark.asyncio
    async def test_auto_approves_only_read_only_tools(self, agents_dir):
        """The blank baseline's whole security story: a useful tool surface, but
        nothing with a side effect is pre-approved."""
        await _create(agents_dir, {"name": "researcher"})

        written = json.loads((agents_dir / "researcher.json").read_text(encoding="utf-8"))
        assert set(written["allowedTools"]) == set(_BLANK_TEMPLATE_ALLOWED_TOOLS)
        assert "execute_bash" in written["tools"]
        assert "execute_bash" not in written["allowedTools"]
        assert "fs_write" not in written["allowedTools"]

    @pytest.mark.asyncio
    async def test_omits_kirocrew_mcp_servers(self, agents_dir):
        """Kiro Crew's own MCP surface (spawn / cron / computer use) is opt-in via
        a copy of ``kirocrew``, never a blank template's default."""
        await _create(agents_dir, {"name": "researcher"})

        written = json.loads((agents_dir / "researcher.json").read_text(encoding="utf-8"))
        assert not [t for t in written["tools"] if t.startswith("@kirocrew")]

    @pytest.mark.asyncio
    async def test_description_and_prompt_are_stored(self, agents_dir):
        await _create(
            agents_dir,
            {"name": "researcher", "description": "Digs through papers", "prompt": "Be rigorous."},
        )

        written = json.loads((agents_dir / "researcher.json").read_text(encoding="utf-8"))
        assert written["description"] == "Digs through papers"
        assert written["prompt"] == "Be rigorous."

    @pytest.mark.asyncio
    async def test_absent_optional_fields_are_omitted_not_empty(self, agents_dir):
        """kiro-cli reads the spec; an empty-string prompt is not the same as no
        prompt, and a minimal spec is the one least likely to be rejected."""
        await _create(agents_dir, {"name": "researcher"})

        written = json.loads((agents_dir / "researcher.json").read_text(encoding="utf-8"))
        assert "prompt" not in written
        assert "description" not in written

    @pytest.mark.asyncio
    async def test_tools_in_the_body_are_ignored(self, agents_dir):
        """The privilege surface is not settable through create — otherwise one
        call could mint a template that auto-approves everything."""
        await _create(
            agents_dir,
            {
                "name": "researcher",
                "tools": ["execute_bash"],
                "allowedTools": ["execute_bash", "fs_write"],
                "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            },
        )

        written = json.loads((agents_dir / "researcher.json").read_text(encoding="utf-8"))
        assert "execute_bash" not in written["allowedTools"]
        assert "toolsSettings" not in written


# ── CREATE: name validation ──


class TestCreateNameValidation:
    @pytest.mark.asyncio
    async def test_missing_name_is_rejected(self, agents_dir):
        resp = await _create(agents_dir, {})
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        [
            "../escape",
            "sub/dir",
            "back\\slash",
            "..",
            ".hidden",
            "has space",
            "-leading-dash",
            "sym*bol",
        ],
    )
    async def test_unsafe_names_are_rejected(self, agents_dir, name):
        resp = await _create(agents_dir, {"name": name})

        assert resp.status == 400
        assert list(agents_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_traversal_writes_nothing_outside_the_agents_dir(self, agents_dir, tmp_path):
        await _create(agents_dir, {"name": "../../pwned"})

        assert not (tmp_path / "pwned.json").exists()
        assert not (tmp_path.parent / "pwned.json").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["kirocrew", "kirocrew-lite", "kirocrew-knowledge", "default"]
    )
    async def test_managed_and_builtin_names_are_reserved(self, agents_dir, name):
        resp = await _create(agents_dir, {"name": name})

        assert resp.status == 400
        assert not (agents_dir / f"{name}.json").exists()

    @pytest.mark.asyncio
    async def test_overlong_name_is_rejected(self, agents_dir):
        resp = await _create(agents_dir, {"name": "a" * 65})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_string_name_is_rejected(self, agents_dir):
        resp = await _create(agents_dir, {"name": {"id": "x"}})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_object_body_is_rejected(self, agents_dir):
        resp = await _create(agents_dir, ["name"])
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_is_rejected(self, agents_dir):
        resp = await _create(agents_dir, None)
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,code",
        [
            ({}, "name_required"),
            ({"name": "../escape"}, "invalid_agent_name"),
            ({"name": "kirocrew"}, "agent_name_reserved"),
            ({"name": "a" * 65}, "name_too_long"),
            ({"name": "ok", "prompt": {"bad": 1}}, "prompt_invalid"),
            ({"name": "ok", "description": "d" * 501}, "description_too_long"),
            ({"name": "ok", "from": 7}, "invalid_from"),
        ],
    )
    async def test_every_rejection_carries_a_machine_readable_code(
        self, agents_dir, body, code
    ):
        """The dashboard branches on ``code``; the prose is advisory and localized
        away, so an un-coded rejection is untranslatable by construction."""
        resp = await _create(agents_dir, body)

        assert resp.status == 400
        assert _spec(resp)["code"] == code


# ── CREATE: collisions ──


class TestCreateCollisions:
    @pytest.mark.asyncio
    async def test_existing_filename_conflicts(self, agents_dir):
        (agents_dir / "researcher.json").write_text('{"name": "researcher"}', encoding="utf-8")

        resp = await _create(agents_dir, {"name": "researcher"})
        assert resp.status == 409
        assert _spec(resp)["code"] == "agent_template_exists"

    @pytest.mark.asyncio
    async def test_name_claimed_by_a_package_spec_conflicts(self, agents_dir):
        """kiro-cli resolves an agent by its ``name`` field, so a second spec
        declaring a package agent's name makes which one wins a coin flip — the
        filename being free is not enough."""
        (agents_dir / "somepkg-reviewer.json").write_text(
            '{"name": "reviewer"}', encoding="utf-8"
        )

        resp = await _create(agents_dir, {"name": "reviewer"})

        assert resp.status == 409
        assert not (agents_dir / "reviewer.json").exists()

    @pytest.mark.asyncio
    async def test_an_unparseable_neighbour_does_not_block_creation(self, agents_dir):
        (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")

        resp = await _create(agents_dir, {"name": "researcher"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_existing_file_is_never_overwritten(self, agents_dir):
        (agents_dir / "researcher.json").write_text(
            '{"name": "researcher", "prompt": "original"}', encoding="utf-8"
        )

        await _create(agents_dir, {"name": "researcher", "prompt": "replacement"})

        kept = json.loads((agents_dir / "researcher.json").read_text(encoding="utf-8"))
        assert kept["prompt"] == "original"

    @pytest.mark.asyncio
    async def test_a_file_appearing_after_the_scan_is_not_clobbered(self, agents_dir):
        """The TOCTOU window the exclusive create closes.

        Reading the dir and writing the file are two steps. A concurrent POST for
        the same name — or another tool writing the same path — lands in between,
        and only the kernel can make "create only if absent" atomic. Stubbing the
        scan to report an empty dir is exactly what the loser of that race sees,
        so this reaches the write with the file already present.
        """
        victim = agents_dir / "researcher.json"
        victim.write_text('{"name": "researcher", "prompt": "original"}', encoding="utf-8")

        with patch(
            "kiro_crew.dashboard.handlers.agents._read_agent_specs", return_value=[]
        ):
            resp = await _create(agents_dir, {"name": "researcher", "prompt": "replacement"})

        assert resp.status == 409
        assert json.loads(victim.read_text(encoding="utf-8"))["prompt"] == "original"


# ── CREATE: copying an existing template ──


class TestCreateFromCopy:
    @pytest.fixture
    def source(self, agents_dir) -> Path:
        path = agents_dir / "base.json"
        path.write_text(
            json.dumps(
                {
                    "name": "base",
                    "description": "the original",
                    "prompt": "original prompt",
                    "model": "some-model",
                    "tools": ["execute_bash", "fs_read", "@kirocrew-core"],
                    "allowedTools": ["fs_read", "@kirocrew-core"],
                    "mcpServers": {"kirocrew-core": {"command": "mcp", "args": []}},
                    "resources": ["skill://~/.kiro/skills/babysit/SKILL.md"],
                    "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /*"]}},
                }
            ),
            encoding="utf-8",
        )
        return path

    @pytest.mark.asyncio
    async def test_copy_inherits_the_privilege_surface(self, agents_dir, source):
        """A copy is how a user gets a privileged tool surface: inherited from a
        spec that already exists on disk, not assembled from a request body."""
        await _create(agents_dir, {"name": "clone", "from": "base"})

        written = json.loads((agents_dir / "clone.json").read_text(encoding="utf-8"))
        assert written["tools"] == ["execute_bash", "fs_read", "@kirocrew-core"]
        assert written["allowedTools"] == ["fs_read", "@kirocrew-core"]
        assert written["toolsSettings"]["execute_bash"]["deniedCommands"] == ["rm -rf /*"]
        assert written["mcpServers"] == {"kirocrew-core": {"command": "mcp", "args": []}}
        assert written["resources"] == ["skill://~/.kiro/skills/babysit/SKILL.md"]

    @pytest.mark.asyncio
    async def test_copy_takes_the_new_name(self, agents_dir, source):
        await _create(agents_dir, {"name": "clone", "from": "base"})

        written = json.loads((agents_dir / "clone.json").read_text(encoding="utf-8"))
        assert written["name"] == "clone"

    @pytest.mark.asyncio
    async def test_supplied_prompt_overrides_the_copied_one(self, agents_dir, source):
        await _create(agents_dir, {"name": "clone", "from": "base", "prompt": "mine"})

        written = json.loads((agents_dir / "clone.json").read_text(encoding="utf-8"))
        assert written["prompt"] == "mine"

    @pytest.mark.asyncio
    async def test_source_is_left_untouched(self, agents_dir, source):
        await _create(agents_dir, {"name": "clone", "from": "base", "prompt": "mine"})

        original = json.loads(source.read_text(encoding="utf-8"))
        assert original["name"] == "base"
        assert original["prompt"] == "original prompt"

    @pytest.mark.asyncio
    async def test_copy_resolves_a_package_spec_by_its_name_field(self, agents_dir):
        (agents_dir / "somepkg-reviewer.json").write_text(
            json.dumps({"name": "reviewer", "tools": ["fs_read"]}), encoding="utf-8"
        )

        resp = await _create(agents_dir, {"name": "myreviewer", "from": "reviewer"})

        assert resp.status == 200
        written = json.loads((agents_dir / "myreviewer.json").read_text(encoding="utf-8"))
        assert written["tools"] == ["fs_read"]

    @pytest.mark.asyncio
    async def test_unknown_source_is_a_404(self, agents_dir):
        resp = await _create(agents_dir, {"name": "clone", "from": "nope"})

        assert resp.status == 404
        assert _spec(resp)["code"] == "source_template_not_found"
        assert not (agents_dir / "clone.json").exists()

    @pytest.mark.asyncio
    async def test_bookkeeping_keys_are_not_inherited(self, agents_dir):
        """kiro-cli rejects unknown fields and then resolves no agent at all, so a
        copy of an older spec must not carry Kiro Crew's sidecar keys."""
        (agents_dir / "legacy.json").write_text(
            json.dumps({"name": "legacy", "model_managed": True, "cc_model": "x"}),
            encoding="utf-8",
        )

        await _create(agents_dir, {"name": "clone", "from": "legacy"})

        written = json.loads((agents_dir / "clone.json").read_text(encoding="utf-8"))
        assert "model_managed" not in written
        assert "cc_model" not in written


# ── EDIT: prompt and description ──


class TestPatchText:
    @pytest.fixture
    def template(self, agents_dir) -> Path:
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps({"name": "researcher", "prompt": "old", "description": "old desc"}),
            encoding="utf-8",
        )
        return path

    @pytest.mark.asyncio
    async def test_prompt_is_written(self, agents_dir, template):
        resp = await _patch(agents_dir, "researcher", {"prompt": "new prompt"})

        assert resp.status == 200
        assert json.loads(template.read_text(encoding="utf-8"))["prompt"] == "new prompt"
        assert _spec(resp)["prompt"] == "new prompt"

    @pytest.mark.asyncio
    async def test_description_is_written(self, agents_dir, template):
        resp = await _patch(agents_dir, "researcher", {"description": "new desc"})

        assert resp.status == 200
        assert json.loads(template.read_text(encoding="utf-8"))["description"] == "new desc"

    @pytest.mark.asyncio
    async def test_empty_value_drops_the_key(self, agents_dir, template):
        await _patch(agents_dir, "researcher", {"prompt": ""})

        assert "prompt" not in json.loads(template.read_text(encoding="utf-8"))

    @pytest.mark.asyncio
    async def test_a_file_prompt_can_be_replaced_with_inline_text(self, agents_dir):
        """A copied template inherits ``prompt: file://…`` from its source; the
        editor has to be able to detach it."""
        path = agents_dir / "clone.json"
        path.write_text(
            json.dumps({"name": "clone", "prompt": "file:///somewhere/prompt.md"}),
            encoding="utf-8",
        )

        await _patch(agents_dir, "clone", {"prompt": "inline now"})

        assert json.loads(path.read_text(encoding="utf-8"))["prompt"] == "inline now"

    @pytest.mark.asyncio
    async def test_non_string_prompt_is_rejected(self, agents_dir, template):
        resp = await _patch(agents_dir, "researcher", {"prompt": {"file": "x"}})

        assert resp.status == 400
        assert json.loads(template.read_text(encoding="utf-8"))["prompt"] == "old"

    @pytest.mark.asyncio
    async def test_overlong_prompt_is_rejected(self, agents_dir, template):
        resp = await _patch(agents_dir, "researcher", {"prompt": "x" * 100_001})

        assert resp.status == 400
        assert json.loads(template.read_text(encoding="utf-8"))["prompt"] == "old"

    @pytest.mark.asyncio
    async def test_a_rejected_combined_patch_applies_nothing(self, agents_dir, template):
        """Validation runs before any mutation, so a bad prompt cannot leave a
        half-applied model change behind."""
        resp = await _patch(
            agents_dir, "researcher", {"model": "new-model", "prompt": {"bad": 1}}
        )

        assert resp.status == 400
        written = json.loads(template.read_text(encoding="utf-8"))
        assert "model" not in written
        assert written["prompt"] == "old"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["prompt", "description"])
    async def test_managed_specs_refuse_the_edit(self, agents_dir, field):
        """Kiro Crew rewrites these fields on every install, so accepting the edit
        would show the user a save that silently reverts."""
        path = agents_dir / "kirocrew.json"
        path.write_text(
            json.dumps({"name": "kirocrew", "prompt": "file://p.md", "description": "d"}),
            encoding="utf-8",
        )

        resp = await _patch(agents_dir, "kirocrew", {field: "hijacked"})

        assert resp.status == 400
        assert _spec(resp)["code"] == "agent_template_managed"
        assert json.loads(path.read_text(encoding="utf-8"))[field] != "hijacked"

    @pytest.mark.asyncio
    async def test_managed_specs_still_accept_a_model_patch(self, agents_dir):
        """Only the rewritten fields are refused; the model pin is a real sidecar
        -backed setting and must keep working."""
        path = agents_dir / "kirocrew.json"
        path.write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")

        resp = await _patch(agents_dir, "kirocrew", {"model": "some-model"})

        assert resp.status == 200
        assert json.loads(path.read_text(encoding="utf-8"))["model"] == "some-model"


# ── The managed flag the editor gates on ──


class TestManagedFlag:
    @pytest.mark.asyncio
    async def test_a_created_template_is_not_managed(self, agents_dir):
        await _create(agents_dir, {"name": "researcher"})

        resp = await _get(agents_dir, "researcher")
        assert _spec(resp)["managed"] is False

    @pytest.mark.asyncio
    async def test_a_kirocrew_owned_spec_is_managed(self, agents_dir):
        (agents_dir / "kirocrew.json").write_text('{"name": "kirocrew"}', encoding="utf-8")

        resp = await _get(agents_dir, "kirocrew")
        assert _spec(resp)["managed"] is True

    @pytest.mark.asyncio
    async def test_the_builtin_default_is_managed(self, agents_dir):
        resp = await _get(agents_dir, "default")
        assert _spec(resp)["managed"] is True
