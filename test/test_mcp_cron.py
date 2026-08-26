"""Tests for mcp_cron channel auto-capture."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_cron import _call_tool_inner


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


class TestCronAddChannelCapture:
    def test_cron_add_captures_channel_from_env(self, monkeypatch, tmp_path):
        """KIROCREW_CHANNEL_ID env var is used as job channel."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_CHANNEL_ID", "C0ABC123")

        job_name = f"test-job-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "hello", "every": 120},
        )
        assert "Added job" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        jobs = svc.list_jobs()
        matching = [j for j in jobs if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].channel == "C0ABC123"

    def test_cron_add_no_env_channel_is_none(self, monkeypatch, tmp_path):
        """Without KIROCREW_CHANNEL_ID, job channel is None (DM fallback)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"test-no-channel-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "hello", "every": 120},
        )

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        jobs = svc.list_jobs()
        matching = [j for j in jobs if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].channel is None

    def test_cron_respects_kirocrew_home(self, monkeypatch, tmp_path):
        """CronService uses KIROCREW_HOME when set, not the default ~/.kirocrew."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"test-home-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "hello", "every": 120},
        )
        assert "Added job" in result

        # Job should be in tmp_path, not ~/.kirocrew
        crons_file = tmp_path / "crons.json"
        assert crons_file.exists(), "crons.json not written to KIROCREW_HOME directory"

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        jobs = svc.list_jobs()
        assert any(j.name == job_name for j in jobs)


class TestCronAddModel:
    """Test per-job model override on cron_add and cron_update."""

    def test_cron_add_with_valid_model(self, monkeypatch, tmp_path):
        """A recognized model is stored on the job."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-valid-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": "sonnet"},
        )
        assert "Added job" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        matching = [j for j in svc.list_jobs() if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].model != ""

    def test_cron_add_with_empty_model(self, monkeypatch, tmp_path):
        """Empty model string means inherit (no override stored)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-empty-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": ""},
        )
        assert "Added job" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        matching = [j for j in svc.list_jobs() if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].model == ""

    def test_cron_add_with_arbitrary_model_accepted(self, monkeypatch, tmp_path):
        """An arbitrary well-formed kiro id is accepted and persisted verbatim.

        There is no membership gate against the claude_code registry: the
        model list is sourced from the live kiro-cli --list-models, so any id
        the CLI advertises (not just the claude_code family) is valid. Matches
        the chat model path. (Malformed ids are rejected by the schema-level
        _MODEL_NAME_RE format gate in validation.py, which is retained.)
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-arb-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": "glm-4.7"},
        )
        assert "Added job" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        matching = [j for j in svc.list_jobs() if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].model == "glm-4.7"

    def test_cron_update_model(self, monkeypatch, tmp_path):
        """cron_update with a valid model stores it on the job."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-upd-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120},
        )
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = next(j for j in svc.list_jobs() if j.name == job_name)

        result = _call_tool_inner(
            "cron_update",
            {"job_id": job.id, "model": "sonnet"},
        )
        assert "Updated" in result or "updated" in result.lower()

        svc2 = CronService(base_dir=tmp_path)
        updated = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert updated.model != ""

    def test_cron_update_model_clear(self, monkeypatch, tmp_path):
        """cron_update with model='' clears the override."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-clr-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": "sonnet"},
        )
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = next(j for j in svc.list_jobs() if j.name == job_name)

        result = _call_tool_inner(
            "cron_update",
            {"job_id": job.id, "model": ""},
        )
        assert "Updated" in result or "updated" in result.lower()

        svc2 = CronService(base_dir=tmp_path)
        updated = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert updated.model == ""

    def test_cron_update_arbitrary_model_accepted(self, monkeypatch, tmp_path):
        """cron_update accepts an arbitrary well-formed kiro id and stores it.

        No membership gate against the claude_code registry — the model list
        comes from the live kiro-cli --list-models, so any advertised id is
        valid. Matches the chat model path.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-upd-arb-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120},
        )
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = next(j for j in svc.list_jobs() if j.name == job_name)

        result = _call_tool_inner(
            "cron_update",
            {"job_id": job.id, "model": "glm-4.7"},
        )
        assert "Updated" in result or "updated" in result.lower()

        svc2 = CronService(base_dir=tmp_path)
        updated = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert updated.model == "glm-4.7"


class TestCronAddPersistenceOwner:
    """#391: the MCP create path folds ALL first-save fields into add_job's
    single locked _save(), instead of the old create-then-mutate + second
    unlocked _save() (which could persist a job missing its agent_id/model)."""

    def test_cron_add_persists_all_fields_in_one_save(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:slot9")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        from kiro_crew.cron import CronService

        # Count _save() calls made during the create. The old fold path saved
        # TWICE (add_job's first save + the post-hoc svc._save()); the fix saves
        # exactly once on a fresh (empty) home.
        save_calls = {"n": 0}
        orig_save = CronService._save

        def counting_save(self):
            save_calls["n"] += 1
            return orig_save(self)

        monkeypatch.setattr(CronService, "_save", counting_save)

        job_name = f"owner-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {
                "name": job_name,
                "message": "go",
                "every": 120,
                "agent": "kirocrew",
                "model": "sonnet",
                "silent": True,
                "approval_mode": "auto",
                "minimal_context": True,
                "timeout": 45,
            },
        )
        assert "Added job" in result
        # Single locked persist — the second unlocked _save() is gone.
        assert save_calls["n"] == 1

        svc = CronService(base_dir=tmp_path)
        matching = [j for j in svc.list_jobs() if j.name == job_name]
        assert len(matching) == 1
        job = matching[0]
        assert job.agent_id == "kirocrew"
        assert job.model != ""
        assert job.silent is True
        assert job.approval_mode == "auto"
        assert job.minimal_context is True
        assert job.timeout == 45
        assert job.session_key == "dashboard:slot9"

    def test_cron_add_explicit_null_fields_do_not_abort(self, monkeypatch, tmp_path):
        """An explicit null optional field (normalized to None by validate_field)
        must not abort creation or persist JSON null -- the always-pass fold
        normalizes falsy values back to '' (regression guard for the
        conditional-set -> always-pass conversion)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"nullfields-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {
                "name": job_name,
                "message": "go",
                "every": 120,
                "approval_mode": None,
                "agent": None,
                "command": None,
                "script": None,
            },
        )
        assert "Added job" in result
        assert "Invalid approval_mode" not in result

        from kiro_crew.cron import CronService

        matching = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == job_name]
        assert len(matching) == 1
        job = matching[0]
        assert job.approval_mode == ""
        assert job.agent_id == ""
        assert job.command == ""
        assert job.script == ""


class TestCronRemoveAudit:
    """Single-job ``cron_remove`` must be SEL-audited like its neighbours.

    ``cron.create`` / ``cron.update`` / ``cron.trigger`` and the plural
    ``cron_remove_all`` all leave audit records; a single delete without one
    makes a deliberate removal indistinguishable from data loss in the trail.
    """

    @pytest.fixture()
    def sel_events(self, monkeypatch):
        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kw):
                events.append(kw)

            def log_tool_invocation(self, **kw):
                events.append(kw)

        import kiro_crew.cron as cron_mod
        import kiro_crew.mcp_cron as mcp_cron_mod

        monkeypatch.setattr(mcp_cron_mod, "sel", lambda: _FakeSel())
        monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=lambda: _FakeSel()))
        return events

    def test_cron_remove_emits_sel_audit(self, monkeypatch, tmp_path, sel_events):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"audit-rm-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner("cron_add", {"name": job_name, "message": "hello", "every": 120})
        assert "Added job" in result

        from kiro_crew.cron import CronService

        jobs = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == job_name]
        assert len(jobs) == 1
        jid = jobs[0].id

        result = _call_tool_inner("cron_remove", {"job_id": jid})
        assert result == f"Removed job: {jid}"

        removes = [e for e in sel_events if e.get("operation") == "cron.remove"]
        assert len(removes) == 1
        assert removes[0]["caller"] == "mcp"
        assert removes[0]["outcome"] == "allowed"
        assert removes[0]["source"] == "mcp"
        assert f"job_id={jid}" in removes[0]["resources"]

    def test_cron_remove_missing_job_is_not_audited_as_removed(
        self, monkeypatch, tmp_path, sel_events
    ):
        # An unknown id never reaches the store mutation: the ownership gate
        # answers its anti-enumeration refusal first, so no cron.remove event
        # (with any outcome) may claim a delete that did not happen.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        result = _call_tool_inner("cron_remove", {"job_id": "no-such-job"})
        assert "Removed job" not in result

        removes = [e for e in sel_events if e.get("operation") == "cron.remove"]
        assert not [e for e in removes if e.get("outcome") == "allowed"]

    def test_cron_remove_succeeds_when_audit_raises(self, monkeypatch, tmp_path):
        # The first sel() of a process constructs the log and can raise; the
        # job is already removed by then, so the tool must still report the
        # completed delete instead of surfacing an error.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"audit-raise-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner("cron_add", {"name": job_name, "message": "hello", "every": 120})
        assert "Added job" in result

        from kiro_crew.cron import CronService

        jobs = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == job_name]
        assert len(jobs) == 1
        jid = jobs[0].id

        import kiro_crew.cron as cron_mod
        import kiro_crew.mcp_cron as mcp_cron_mod

        def _raising_sel():
            raise RuntimeError("SEL trust root unavailable")

        monkeypatch.setattr(mcp_cron_mod, "sel", _raising_sel)
        monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=_raising_sel))
        result = _call_tool_inner("cron_remove", {"job_id": jid})
        assert result == f"Removed job: {jid}"
        assert not [j for j in CronService(base_dir=tmp_path).list_jobs() if j.id == jid]
