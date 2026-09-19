"""Jev loopback -> real gate -> real worker-thread context assembly.

Only the remote endpoint is substituted. Sampling, request mapping, response
validation, skill loading and the final message all run through production code.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import DecisionProviderConfig, DecisionsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.decisions import log
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,bucket,choice,expected,requests",
    [
        (True, 100, "semantic", "semantic", 1),
        (True, 100, "/no skill applies", None, 1),
        (True, 100, "unoffered", "lexical", 1),
        (False, 100, "semantic", "lexical", 0),
        (True, 0, "semantic", "lexical", 0),
    ],
)
async def test_jev_response_reaches_the_assembled_message(
    tmp_path, monkeypatch, enabled, bucket, choice, expected, requests
):
    received = []

    async def judge(request):
        received.append(await request.json())
        return web.json_response(
            {
                "answers": {
                    "pick": {"type": "choice", "choice": choice, "probabilities": {choice: 0.9}}
                }
            }
        )

    app = web.Application()
    app.router.add_post("/judge", judge)
    async with TestServer(app) as server:
        cfg = KiroCrewConfig.load()
        cfg.skills.max_triggered = 2
        # Consent is the keystone, not the config: written where the gate reads it.
        keystone = tmp_path / "decisions_consent.json"
        keystone.write_text(
            json.dumps({"enabled": enabled, "endpoint": str(server.make_url("/judge"))}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: keystone)
        cfg.decisions = DecisionsConfig(
            bucket=bucket,
            provider=DecisionProviderConfig(
                endpoint=str(server.make_url("/judge")),
                api_key="secret://TYPESAFE_API_KEY",
                timeout_ms=2000,
            ),
        )
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        # The vault is the only key source; the loopback judge accepts any bearer.
        import kiro_crew.secrets.vault as vault_mod

        class _Value:
            def reveal(self):
                return "loopback-test-key"

        class _Vault:
            def __init__(self, *_a, **_k):
                pass

            def get(self, name):
                return _Value() if name == "TYPESAFE_API_KEY" else None

        monkeypatch.setattr(vault_mod, "SecretVault", _Vault)
        # The configured home is the append helper's anchor and must exist; only
        # the `decisions/` directory under it is created on first write.
        (tmp_path / "data").mkdir()
        monkeypatch.setattr(log, "config_dir", lambda: tmp_path / "data")
        root = tmp_path / "skills"
        for key, triggers in [("lexical", "zebra"), ("semantic", "invoice")]:
            directory = root / key
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(
                f"---\nname: {key}\ndescription: {key} help\ntriggers: {triggers}\n---\n"
                f"{key} instructions\n",
                encoding="utf-8",
            )
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "workspace"),
            skills=SkillsLoader(skills_path=root, install_builtins=False, config=cfg),
            lessons=LessonStore(base_dir=tmp_path),
        )
        message, _ = await asyncio.to_thread(
            builder.build_message, "zebra please", False, "loopback-session"
        )

    assert len(received) == requests
    for key in ("lexical", "semantic"):
        assert (f"[Skill: {key}]" in message) is (key == expected)
    if received:
        assert received[0]["model"] == cfg.decisions.provider.model
        assert received[0]["state"]["message"] == "zebra please"
        assert set(received[0]["questions"]["pick"]["criteria"]) == {
            "lexical",
            "semantic",
            "/no skill applies",
        }
        rows = list((tmp_path / "data" / "decisions").glob("*.jsonl"))
        assert len(rows) == 1
        row = json.loads(rows[0].read_text(encoding="utf-8"))
        assert row["point"] == "skills.select"
        assert row["error"] == ("invalid-result" if choice == "unoffered" else None)
        assert "zebra please" not in json.dumps(row)
    else:
        assert not (tmp_path / "data" / "decisions").exists()
