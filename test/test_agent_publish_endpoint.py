"""Tests for api_agent_publish — POST /api/agents/detail/{name}/publish.

The counterpart of the invisible fork: publishes a crew's private copy under a
user-chosen name as a REAL template (no fork lineage), rebinds the crew, and
removes the superseded copy. Only a private copy can be published, the name is
validated (a filename is a template's permanent identity), and collisions are
refused rather than suffixed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import api_agent_publish


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run past the owner boundary; owner-auth has its own coverage elsewhere."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _publish_request(name: str, body, *, bad_json: bool = False):
    request = MagicMock(spec=web.Request)
    request.method = "POST"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        if bad_json:
            raise ValueError("not json")
        return body

    request.json = _json
    return request


def _write_template(agents_dir, stem: str, **extra) -> None:
    spec = {"name": stem, "model": "claude-x", "tools": ["ReadFile"]}
    spec.update(extra)
    (agents_dir / f"{stem}.json").write_text(json.dumps(spec), encoding="utf-8")


def _seed_config(crew: str, kiro_agent: str) -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {crew: KiroCrewAgentConfig(kiro_agent=kiro_agent)}
    cfg.default_agent = crew
    cfg.save()


def _seed_private_copy(agents_dir, crew: str, copy: str, origin: str) -> None:
    """A fork as api_agent_fork leaves it: copy file + lineage + rebound crew."""
    _write_template(agents_dir, copy)
    agent_state.set_fork_info(copy, forked_from=origin, private_to=crew)
    _seed_config(crew, copy)


@pytest.mark.asyncio
async def test_publish_happy_path(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body == {"ok": True, "template": "reviewer-v2", "filename": "reviewer-v2.json"}

    # New template exists, declared name equals the stem, content carried over.
    published = json.loads((agents_dir / "reviewer-v2.json").read_text(encoding="utf-8"))
    assert published["name"] == "reviewer-v2"
    assert published["model"] == "claude-x"
    # It is a REAL template: no fork lineage.
    assert agent_state.get_fork_info("reviewer-v2") is None
    # The superseded private copy is gone — file and sidecar record.
    assert not (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is None
    # Crew rebound to the published template.
    cfg = KiroCrewConfig.load()
    assert cfg.agents["design-crew"].kiro_agent == "reviewer-v2"


@pytest.mark.asyncio
async def test_publish_copies_model_tracking(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "c", "c", "kirocrew")
    agent_state.set_model_managed("c", False)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(_publish_request("c", {"crew": "c", "name": "published"}))

    assert resp.status == 200
    assert agent_state.get_model_managed("published") is False


@pytest.mark.asyncio
async def test_publish_refuses_name_collision(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "taken")
    _seed_private_copy(agents_dir, "c", "c", "kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(_publish_request("c", {"crew": "c", "name": "taken"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "name_taken"
    # Nothing changed: copy still present and bound.
    assert (agents_dir / "c.json").exists()
    assert KiroCrewConfig.load().agents["c"].kiro_agent == "c"


@pytest.mark.asyncio
async def test_publish_refuses_non_private_source(tmp_path):
    """Publishing a SHARED template would silently duplicate it — refused."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "shared")
    _seed_config("c", "shared")  # bound, but NO fork lineage

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(_publish_request("shared", {"crew": "c", "name": "newname"}))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "not_a_private_copy"


@pytest.mark.asyncio
async def test_publish_refuses_another_crews_copy(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "owner-crew", "owner-crew", "kirocrew")
    cfg = KiroCrewConfig.load()
    cfg.agents["intruder"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("owner-crew", {"crew": "intruder", "name": "stolen"})
        )

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "not_a_private_copy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["", "has space", "../escape", "a" * 64, "-leadingdash"],
)
async def test_publish_rejects_invalid_names(tmp_path, name):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "c", "c", "kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(_publish_request("c", {"crew": "c", "name": name}))

    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_template_name"


@pytest.mark.asyncio
async def test_publish_rejects_reserved_name(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "c", "c", "kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(_publish_request("c", {"crew": "c", "name": "kirocrew"}))

    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "template_name_reserved"


@pytest.mark.asyncio
async def test_publish_404_on_unknown_template_and_crew(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "c", "c", "kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        missing_template = await api_agent_publish(
            _publish_request("ghost", {"crew": "c", "name": "x1"})
        )
        missing_crew = await api_agent_publish(
            _publish_request("c", {"crew": "nobody", "name": "x2"})
        )

    assert missing_template.status == 404
    assert missing_crew.status == 404


@pytest.mark.asyncio
async def test_publish_400_on_bad_body(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        bad_json = await api_agent_publish(_publish_request("c", None, bad_json=True))
        not_object = await api_agent_publish(_publish_request("c", ["crew"]))
        no_crew = await api_agent_publish(_publish_request("c", {"name": "x"}))

    assert bad_json.status == 400
    assert not_object.status == 400
    assert no_crew.status == 400


@pytest.mark.asyncio
async def test_publish_stale_binding_409(tmp_path):
    """Publishing a private copy the crew has since moved off is refused,
    so a stale publish cannot rebind over the newer binding."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")
    _write_template(agents_dir, "elsewhere")
    _seed_config("design-crew", "elsewhere")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "stale_binding"
    assert not (agents_dir / "reviewer-v2.json").exists()
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "elsewhere"


@pytest.mark.asyncio
async def test_publish_keeps_lineage_when_delete_fails(tmp_path):
    """A copy file that cannot be removed must keep its fork lineage: pruning
    it would surface the private customization as a shared template."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch("pathlib.Path.unlink", side_effect=OSError("locked")),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 200
    # The publish itself succeeded and the crew was rebound...
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "reviewer-v2"
    # ...but the undeletable copy stays recorded as private, not shared.
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") == {
        "forked_from": "kirocrew",
        "private_to": "design-crew",
    }


@pytest.mark.asyncio
async def test_publish_refuses_differing_case_collision(tmp_path):
    """'Reviewer' must not truncate an existing 'reviewer.json': names are
    reserved case-insensitively, matching APFS/NTFS default semantics."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "reviewer")
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "Reviewer"})
        )

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "name_taken"
    # The pre-existing template is untouched.
    assert (
        json.loads((agents_dir / "reviewer.json").read_text(encoding="utf-8"))["name"] == "reviewer"
    )


@pytest.mark.asyncio
async def test_publish_sanitizes_governance_before_write(tmp_path):
    """The shared spec writer must run sanitize_agent_config_governance: a
    copied spec carries its source's allowedTools/autoApprove verbatim, and
    those two routes skip the PreToolUse gate (security-class regression)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    def fake_sanitize(config):
        config["allowedTools"] = ["governance-filtered"]

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch("kiro_crew.platform.governance.sanitize_agent_config_governance", fake_sanitize),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "published"})
        )

    assert resp.status == 200
    written = json.loads((agents_dir / "published.json").read_text(encoding="utf-8"))
    assert written["allowedTools"] == ["governance-filtered"]


@pytest.mark.asyncio
async def test_publish_generic_rebind_failure_compensates(tmp_path):
    """A rebind failure that is NOT a stale binding (e.g. the config write
    itself fails) must undo the published file and lineage, then 500 with a
    code — a leaked orphan would block every retry with name_taken."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch(
            "kiro_crew.dashboard.handlers.agents._rebind_crew_locked",
            side_effect=RuntimeError("disk full"),
        ),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "published"})
        )

    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "rebind_failed"
    # Compensated: no orphan file, no lineage for the never-bound name.
    assert not (agents_dir / "published.json").exists()
    assert agent_state.get_fork_info("published") is None


@pytest.mark.asyncio
async def test_publish_bookkeeping_failure_compensates(tmp_path):
    """A sidecar failure AFTER the spec is created must undo the file too —
    an unbound published spec would surface as a shared template (GPT round-8)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch(
            "kiro_crew.dashboard.handlers.agents.agent_state.get_model_managed",
            side_effect=RuntimeError("sidecar unavailable"),
        ),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "published"})
        )

    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "bookkeeping_failed"
    assert not (agents_dir / "published.json").exists()
    assert agent_state.get_fork_info("published") is None


@pytest.mark.asyncio
async def test_publish_rejects_reserved_windows_name(tmp_path):
    """CON/NUL/COM1… are filesystem-reserved on Windows: creating CON.json
    raises there, so the name is refused up front (GPT round-10)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "CON"})
        )

    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_template_name"


@pytest.mark.asyncio
async def test_publish_copies_in_lock_reread_not_stale_snapshot(tmp_path):
    """The published file must carry the source's CURRENT content: a
    concurrent refresh between the pre-lock scan and the locked create would
    otherwise be lost when the source is deleted (GPT round-10)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    from kiro_crew.dashboard.handlers import agents as agents_mod

    real_read = agents_mod._read_agent_spec
    src = agents_dir / "design-crew.json"

    def racing_read(path, **kwargs):
        result = real_read(path, **kwargs)
        # After the pre-lock scan read of the source, simulate a concurrent
        # refresh updating it before the locked create re-reads.
        if path == src and kwargs.get("operation") == "api_agent_publish" and result is not None:
            if "refreshedHook" not in result:
                updated = dict(result)
                updated["refreshedHook"] = True
                src.write_text(json.dumps(updated))
        return result

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch("kiro_crew.dashboard.handlers.agents._read_agent_spec", side_effect=racing_read),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "published"})
        )

    assert resp.status == 200
    written = json.loads((agents_dir / "published.json").read_text(encoding="utf-8"))
    assert written.get("refreshedHook") is True


@pytest.mark.asyncio
async def test_publish_succeeds_when_post_commit_prune_fails(tmp_path):
    """After the rebind persisted and the superseded file was removed, the
    publish is committed: a sidecar prune failure must not turn it into a 500
    whose retry then 404s (GPT round-10)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch(
            "kiro_crew.dashboard.handlers.agents.agent_state.prune",
            side_effect=RuntimeError("sidecar unavailable"),
        ),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "published"})
        )

    assert resp.status == 200
    assert (agents_dir / "published.json").exists()
    assert not (agents_dir / "design-crew.json").exists()
