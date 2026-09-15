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
async def test_publish_rejects_a_name_a_crew_binding_dangles_at(tmp_path):
    """A crew bound to a MISSING name must not capture the published spec:
    the moment the file exists that binding resolves to it and the crew
    silently executes the published content. Publish never suffixes, so the
    name is refused."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "design-crew")
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")
    # other-crew's binding dangles at the requested publish name.
    cfg = KiroCrewConfig()
    cfg.agents = {
        "design-crew": KiroCrewAgentConfig(kiro_agent="design-crew"),
        "other-crew": KiroCrewAgentConfig(kiro_agent="reviewer-v2"),
    }
    cfg.default_agent = "design-crew"
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "name_bound"
    # Nothing was created; the private copy and its lineage are untouched.
    assert not (agents_dir / "reviewer-v2.json").exists()
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_publish_rejects_the_legacy_default_agent_fallback_name(tmp_path):
    """`agent.default_agent` is a resolvable reference like any crew binding —
    publishing under a name it dangles at is refused."""
    from kiro_crew.config.loader import config_path, update_config_locked

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    def _inject_fallback(data: dict) -> dict:
        data.setdefault("agent", {})["default_agent"] = "reviewer-v2"
        return data

    update_config_locked(config_path(), mutate=_inject_fallback)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "name_bound"
    assert not (agents_dir / "reviewer-v2.json").exists()


def test_rebind_refuses_a_vanished_target_file(tmp_path):
    """`require_path` rechecks the target file INSIDE the critical section: a
    cross-process delete between a handler's validation and the rebind must
    not persist a binding to nothing."""
    from kiro_crew.dashboard.handlers.agents import _rebind_crew_locked

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_config("design-crew", "design-crew")

    with pytest.raises(FileNotFoundError):
        _rebind_crew_locked("design-crew", ("design-crew",), "origin", agents_dir / "origin.json")
    # The binding is unchanged — nothing was persisted.
    cfg = KiroCrewConfig.load()
    assert cfg.agents["design-crew"].kiro_agent == "design-crew"


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
async def test_publish_reports_committed_binding_when_marking_shared_fails(tmp_path):
    """Once the crew is rebound the publish is committed, so a sidecar failure
    while marking the new template shared must NOT surface as an HTTP error:
    the client keys its editor off the returned template name, and an error
    would leave it editing the superseded copy while the crew runs the new
    one. The committed name comes back with a warning instead, and the new
    template remains recorded as this crew's private copy."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    real_clear = agent_state.clear_fork_info

    def _clear_unless_new(name: str) -> None:
        if name == "reviewer-v2":
            raise OSError("sidecar locked")
        real_clear(name)

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch.object(agent_state, "clear_fork_info", side_effect=_clear_unless_new),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["ok"] is True
    assert body["template"] == "reviewer-v2"
    assert body["warning"] == "publish_incomplete"
    # Committed: the crew runs the published name...
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "reviewer-v2"
    assert (agents_dir / "reviewer-v2.json").exists()
    # ...which is still recorded as its private copy rather than shared (the
    # create step's lineage names the source copy it was published from).
    assert agent_state.get_fork_info("reviewer-v2") == {
        "forked_from": "design-crew",
        "private_to": "design-crew",
    }
    # The superseded copy is still cleaned up.
    assert not (agents_dir / "design-crew.json").exists()


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
        patch(
            "kiro_crew.dashboard.handlers.agents.sanitize_agent_config_governance",
            fake_sanitize,
        ),
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
    an unbound published spec would surface as a shared template."""
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
    raises there, so the name is refused up front."""
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
    otherwise be lost when the source is deleted."""
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
    whose retry then 404s."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    # Scoped to the SUPERSEDED copy's post-commit prune: the destination's
    # PRE-commit clean-slate prune has the opposite contract (failure rolls
    # back), exercised by
    # test_publish_rolls_back_when_the_clean_slate_prune_fails.
    def _post_commit_failing_prune(name: str) -> None:
        if name == "design-crew":
            raise RuntimeError("sidecar unavailable")

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch(
            "kiro_crew.dashboard.handlers.agents.agent_state.prune",
            side_effect=_post_commit_failing_prune,
        ),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "published"})
        )

    assert resp.status == 200
    assert (agents_dir / "published.json").exists()
    assert not (agents_dir / "design-crew.json").exists()


@pytest.mark.asyncio
async def test_publish_cleanup_removes_resolved_file_not_declared_name(tmp_path):
    """A fork whose file STEM differs from its declared name must still have
    its file removed on publish — cleaning up `{declared}.json` would miss it
    and prune lineage anyway, surfacing the private copy as shared (GPT r11)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # File STEM 'renamed-fork' declares name 'design-crew' — resolution goes
    # by declared name, cleanup must go by the resolved file.
    _write_template(agents_dir, "renamed-fork", name="design-crew")
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")
    _seed_config("design-crew", "design-crew")

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "published"})
        )

    assert resp.status == 200
    assert (agents_dir / "published.json").exists()
    # The RESOLVED file is gone; nothing guessed a '{declared}.json' path.
    assert not (agents_dir / "renamed-fork.json").exists()
    assert agent_state.get_fork_info("design-crew") is None


@pytest.mark.asyncio
async def test_publish_rolls_back_when_the_clean_slate_prune_fails(tmp_path, monkeypatch):
    """a failed destination prune must roll the publish back —
    shipping a template that inherits a dead entry's lineage/model state is
    silent corruption. (The fork recorder follows the same rule.)"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    real_prune = agent_state.prune

    def _failing_prune(name: str) -> None:
        if name == "reviewer-v2":
            raise OSError("sidecar lock unavailable")
        real_prune(name)

    monkeypatch.setattr(agent_state, "prune", _failing_prune)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "bookkeeping_failed"
    # Rolled back: no published file, the private copy and its binding survive.
    assert not (agents_dir / "reviewer-v2.json").exists()
    assert (agents_dir / "design-crew.json").exists()
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "design-crew"


@pytest.mark.asyncio
async def test_publish_keeps_copy_bound_by_another_crew(tmp_path):
    """a pre-existing foreign binding to the private copy must
    block the cleanup unlink — deleting it would break that crew's sessions
    with "Mode not found". The publish itself still commits."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")
    cfg = KiroCrewConfig.load()
    cfg.agents["other-crew"] = KiroCrewAgentConfig(kiro_agent="design-crew")
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 200
    # The publish committed: new template exists, publishing crew rebound.
    assert (agents_dir / "reviewer-v2.json").exists()
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "reviewer-v2"
    # The superseded copy survives, lineage intact, because other-crew still
    # resolves it — pruning lineage alone would surface it as shared.
    assert (agents_dir / "design-crew.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_publish_keeps_copy_bound_by_file_stem(tmp_path):
    """a crew bound by the file STEM (where it differs from the
    declared name) resolves the same file, so it too must block the unlink."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "design-crew-file", name="design-crew")
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")
    cfg = KiroCrewConfig()
    cfg.agents = {
        "design-crew": KiroCrewAgentConfig(kiro_agent="design-crew"),
        "other-crew": KiroCrewAgentConfig(kiro_agent="design-crew-file"),
    }
    cfg.default_agent = "design-crew"
    cfg.save()

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 200
    assert (agents_dir / "design-crew-file.json").exists()
    assert agent_state.get_fork_info("design-crew") is not None


@pytest.mark.asyncio
async def test_publish_cleanup_checks_and_unlinks_under_config_lock(tmp_path, monkeypatch):
    """the reference check and the unlink are ONE critical
    section under the config advisory lock, so a binding writer queued on the
    lock cannot land between them. Observed by wrapping the locked mutate and
    recording the copy file's existence on entry and exit."""
    import kiro_crew.dashboard.handlers.agents as handlers

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    real = handlers.update_config_locked
    events: list[tuple[str, bool]] = []

    def _recording(*args, **kwargs):
        mutate = kwargs["mutate"]

        def _wrapped(data):
            events.append(("enter", (agents_dir / "design-crew.json").exists()))
            out = mutate(data)
            events.append(("exit", (agents_dir / "design-crew.json").exists()))
            return out

        kwargs["mutate"] = _wrapped
        return real(*args, **kwargs)

    monkeypatch.setattr(handlers, "update_config_locked", _recording)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 200
    # The cleanup's critical section entered with the copy present and left
    # with it deleted — the unlink happened INSIDE the locked callback, never
    # between a check and a later, unlocked delete.
    assert ("enter", True) in events
    assert ("exit", False) in events
    assert not (agents_dir / "design-crew.json").exists()


@pytest.mark.asyncio
async def test_publish_rollback_keeps_destination_bound_by_another_crew(tmp_path, monkeypatch):
    """a rollback firing after another crew bound the freshly
    created destination must keep the file AS A SHARED TEMPLATE — deleting it
    breaks that crew's sessions, and marking it private misattributes a
    template in live use."""
    import kiro_crew.dashboard.handlers.agents as handlers

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    # Force the rollback path: the rebind raises after dest was created, and
    # by then another crew has bound the destination name.
    def _failing_rebind(crew, expected, new_target):
        cfg = KiroCrewConfig.load()
        cfg.agents["other-crew"] = KiroCrewAgentConfig(kiro_agent="reviewer-v2")
        cfg.save()
        raise RuntimeError("simulated rebind failure")

    monkeypatch.setattr(handlers, "_rebind_crew_locked", _failing_rebind)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 500
    # The destination survives for the crew that bound it, and it is NOT
    # marked as anyone's private copy.
    assert (agents_dir / "reviewer-v2.json").exists()
    assert agent_state.get_fork_info("reviewer-v2") is None


@pytest.mark.asyncio
async def test_publish_rollback_retained_destination_is_never_shared(tmp_path):
    """A rollback whose unlink AND private re-marking both fail must still
    leave the retained destination recorded as the crew's private copy: the
    lineage is written before the file exists, so no failure path between
    create and rebind can expose it as a shared template."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_private_copy(agents_dir, "design-crew", "design-crew", "kirocrew")

    real_set_fork_info = agent_state.set_fork_info
    calls = {"n": 0}

    def _fail_on_reassert(name, *, forked_from, private_to):
        calls["n"] += 1
        if name == "reviewer-v2" and calls["n"] > 1:
            raise OSError("sidecar locked")
        real_set_fork_info(name, forked_from=forked_from, private_to=private_to)

    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch("pathlib.Path.unlink", side_effect=OSError("locked")),
        patch(
            "kiro_crew.dashboard.handlers.agents.agent_state.get_model_managed",
            side_effect=RuntimeError("sidecar unavailable"),
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.agent_state.set_fork_info",
            side_effect=_fail_on_reassert,
        ),
    ):
        resp = await api_agent_publish(
            _publish_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
        )

    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "bookkeeping_failed"
    # The destination could not be removed and the rollback's re-marking
    # failed, yet it is still this crew's private copy, not a shared template.
    assert (agents_dir / "reviewer-v2.json").exists()
    assert agent_state.get_fork_info("reviewer-v2") == {
        "forked_from": "design-crew",
        "private_to": "design-crew",
    }
    # The crew's binding never moved.
    assert KiroCrewConfig.load().agents["design-crew"].kiro_agent == "design-crew"
