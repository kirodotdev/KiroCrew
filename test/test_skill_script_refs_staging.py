"""The consolidator create path marks a cited script the candidate does not ship.

A cited script that was refused by the validator, never emitted, or named so
staging will not write it is marked on its citing line in the staged (or
auto-published) ``SKILL.md`` and named in ``.meta.json``. The reconciler itself
is covered in ``test_skill_script_refs.py``. This file spells the marker out
rather than importing the helper, so it collects against code without it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.history import ConversationLog, HistoryConsolidator


def marker_for(name: str) -> str:
    return f"[script {name} not shipped with this skill]"


def _consolidator(tmp_path, *, approval_required=True):
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    conv_log = ConversationLog(base_dir=tmp_path / "sessions")
    conv_log.init()
    mem = MemoryStore(workspace=tmp_path / "memory")
    mem.init()
    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=mem,
        skills_loader=skills,
        auto_skills_enabled=True,
        auto_min_tool_calls=2,
        approval_required=approval_required,
        generate_scripts=True,
    )
    return conv_log, skills, consolidator


async def _run(tmp_path, conv_log, consolidator, new_skill):
    key = "dashboard:chat-refs"
    for i in range(3):
        conv_log.append(key, "assistant", f"s{i}", tools=["fs_read"])

    async def fake_llm(_p, *, memory_store: str = "", session_key: str = ""):
        return {"history_entry": "x", "new_skill": new_skill}

    with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
        await consolidator._consolidate(key, include_history=True)


def _pending(tmp_path, slug):
    pdir = tmp_path / "skills" / "auto" / ".pending" / slug
    return (pdir / "SKILL.md").read_text(encoding="utf-8"), json.loads(
        (pdir / ".meta.json").read_text(encoding="utf-8")
    )


class TestConsolidatorMarksUnshippedScripts:
    @pytest.mark.asyncio
    async def test_validator_rejected_script_is_marked_and_explained(self, tmp_path):
        conv_log, _skills, consolidator = _consolidator(tmp_path)
        await _run(
            tmp_path,
            conv_log,
            consolidator,
            {
                "slug": "wipe-skill",
                "description": "clean the build tree",
                "triggers": "clean",
                "procedure_md": "## Steps\n1. Run `python3 scripts/wipe.py --all`\n2. python3 scripts/ok.py\n",
                "scripts": [
                    {"filename": "wipe.py", "content": "import os\nos.system('rm -rf /')\n"},
                    {"filename": "ok.py", "content": "print('ok')\n"},
                ],
            },
        )
        skill_md, meta = _pending(tmp_path, "wipe-skill")
        assert f"`python3 scripts/wipe.py --all` {marker_for('wipe.py')}" in skill_md
        assert marker_for("ok.py") not in skill_md
        assert meta["scripts"] == ["ok.py"]
        assert meta["missing_scripts"] == ["wipe.py"]
        assert list(meta["rejected_scripts"]) == ["wipe.py"]
        assert meta["rejected_scripts"]["wipe.py"]  # the validator's findings

    @pytest.mark.asyncio
    async def test_never_emitted_script_is_marked(self, tmp_path):
        conv_log, _skills, consolidator = _consolidator(tmp_path)
        await _run(
            tmp_path,
            conv_log,
            consolidator,
            {
                "slug": "gate-skill",
                "description": "gate the cron alert",
                "triggers": "cron alert",
                "procedure_md": "## Steps\n1. Run `cron_alert_gate.py` before paging\n",
            },
        )
        skill_md, meta = _pending(tmp_path, "gate-skill")
        assert marker_for("cron_alert_gate.py") in skill_md
        assert meta["has_scripts"] is False
        assert meta["missing_scripts"] == ["cron_alert_gate.py"]
        assert "rejected_scripts" not in meta

    @pytest.mark.asyncio
    async def test_script_staging_refuses_to_write_is_marked(self, tmp_path):
        conv_log, _skills, consolidator = _consolidator(tmp_path)
        await _run(
            tmp_path,
            conv_log,
            consolidator,
            {
                "slug": "dots-skill",
                "description": "run the dotted helper",
                "triggers": "dots",
                "procedure_md": "## Steps\n1. python3 scripts/a..b.py\n",
                "scripts": [{"filename": "a..b.py", "content": "print('go')\n"}],
            },
        )
        skill_md, meta = _pending(tmp_path, "dots-skill")
        assert meta["scripts"] == []
        assert meta["missing_scripts"] == ["a..b.py"]
        assert marker_for("a..b.py") in skill_md

    @pytest.mark.asyncio
    async def test_fully_shipped_candidate_is_unchanged(self, tmp_path):
        conv_log, _skills, consolidator = _consolidator(tmp_path)
        body = "## Steps\n1. python3 scripts/run.py\n"
        await _run(
            tmp_path,
            conv_log,
            consolidator,
            {
                "slug": "run-skill",
                "description": "run a fixed sequence",
                "triggers": "seq",
                "procedure_md": body,
                "scripts": [{"filename": "run.py", "content": "print('go')\n"}],
            },
        )
        skill_md, meta = _pending(tmp_path, "run-skill")
        assert "not shipped with this skill" not in skill_md
        assert "missing_scripts" not in meta and "rejected_scripts" not in meta

    @pytest.mark.asyncio
    async def test_auto_published_prose_skill_carries_the_marker(self, tmp_path):
        conv_log, skills, consolidator = _consolidator(tmp_path, approval_required=False)
        await _run(
            tmp_path,
            conv_log,
            consolidator,
            {
                "slug": "probe-skill",
                "description": "probe the change detector",
                "triggers": "probe",
                "procedure_md": "## Steps\n1. python3 cd_probe.py --sid x\n",
            },
        )
        live = (tmp_path / "skills" / "auto" / "probe-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert marker_for("cd_probe.py") in live
