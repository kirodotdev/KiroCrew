"""``kirocrew spawn run`` pins its run with ``--agent``, ``--crew`` and ``--model``.

The flags reach the same ``POST /api/spawn`` fields the native ``spawn_run``
tool sends, so the gateway applies one policy to both: ``crew`` selects that
member's memory store and template, ``agent`` names the template, ``model``
pins the model. An unknown agent or crew name is refused there; ``model`` is
judged on shape only. The CLI relays the refusal and never retries without
the value, so a mistyped agent or crew cannot quietly run as the default.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import patch

import pytest
from cli_commands_coverage_helpers import _FakeResponse, _http_error, _ns

from kiro_crew import cli_commands as cc


def _posted_body(urlopen_mock: Any) -> dict[str, Any]:
    request = urlopen_mock.call_args_list[0].args[0]
    return json.loads(request.data)


def _parse(argv: list[str]) -> Any:
    """Run ``cli.main`` on *argv* and return the namespace ``_spawn`` got."""
    seen: list[Any] = []
    with (
        patch.object(sys, "argv", ["kirocrew", *argv]),
        patch("kiro_crew.cli_commands._spawn", seen.append),
    ):
        from kiro_crew.cli import main

        main()
    assert len(seen) == 1
    return seen[0]


class TestSpawnRunParser:
    def test_agent_and_crew_parse(self) -> None:
        args = _parse(["spawn", "run", "--async", "--agent", "reviewer", "--crew", "ops", "t"])
        assert (args.agent, args.crew, args.task, args.fire_and_forget) == (
            "reviewer",
            "ops",
            "t",
            True,
        )

    def test_both_default_to_unset(self) -> None:
        args = _parse(["spawn", "run", "t"])
        assert args.agent is None and args.crew is None

    def test_model_parses_and_defaults_to_unset(self) -> None:
        assert _parse(["spawn", "run", "--model", "m1", "t"]).model == "m1"
        assert _parse(["spawn", "run", "t"]).model is None


class TestSpawnRunBody:
    def _run(self, **kw: Any) -> Any:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch(
                "kiro_crew.cli_commands._gateway_send",
                return_value=_FakeResponse({"id": "ag1", "task": "t"}),
            ) as uo,
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=True, **kw))
        return uo

    def test_agent_and_crew_are_sent(self) -> None:
        uo = self._run(agent="reviewer", crew="ops")
        assert _posted_body(uo) == {"task": "t", "agent": "reviewer", "crew": "ops"}

    def test_crew_alone_is_sent_without_an_agent(self) -> None:
        uo = self._run(agent=None, crew="ops")
        assert _posted_body(uo) == {"task": "t", "crew": "ops"}

    def test_model_is_sent_beside_the_crew(self) -> None:
        uo = self._run(agent=None, crew="ops", model="m1")
        assert _posted_body(uo) == {"task": "t", "crew": "ops", "model": "m1"}

    def test_no_flags_send_only_the_task(self) -> None:
        uo = self._run(agent=None, crew=None)
        assert _posted_body(uo) == {"task": "t"}

    def test_model_with_hidden_characters_is_posted_unsanitized(self) -> None:
        uo = self._run(agent=None, crew=None, model="op\u200bus")
        assert _posted_body(uo) == {"task": "t", "model": "op\u200bus"}

    @pytest.mark.parametrize("flag", ["agent", "crew", "model"])
    @pytest.mark.parametrize("blank", ["", "   ", "\u200b", "\ufeff"])
    def test_blank_value_is_refused_before_any_request(
        self, flag: str, blank: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch("kiro_crew.cli_commands._gateway_send") as uo,
            pytest.raises(SystemExit) as exc,
        ):
            cc._spawn(
                _ns(spawn_action="run", port=1, task="t", fire_and_forget=True, **{flag: blank})
            )
        assert exc.value.code == 1
        assert uo.call_count == 0
        assert f"Error: --{flag} needs a value" in capsys.readouterr().out


class TestSpawnRunRefusals:
    @pytest.mark.parametrize(
        ("kw", "status", "payload"),
        [
            (
                {"agent": "no-such-agent"},
                400,
                {"error": "Unknown agent 'no-such-agent'", "code": "agent_not_found"},
            ),
            (
                {"crew": "no-such-member"},
                404,
                {"error": "The target member does not exist.", "code": "unknown_member"},
            ),
        ],
    )
    def test_unknown_name_exits_1_without_a_default_retry(
        self,
        kw: dict[str, str],
        status: int,
        payload: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        err = _http_error(status, json.dumps(payload).encode())
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch("kiro_crew.cli_commands._gateway_send", side_effect=err) as uo,
            pytest.raises(SystemExit) as exc,
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=False, **kw))
        assert exc.value.code == 1
        assert uo.call_count == 1
        assert payload["error"] in capsys.readouterr().out


class TestCliBodyThroughTheGateway:
    """The body the CLI posts, driven through ``api_spawn`` as the CLI's caller.

    The CLI authenticates with the internal secret and carries no session key,
    so the request double sets ``internal_auth`` and no ``X-Session-Key``. The
    assertions read what the manager received, where a dropped field shows.
    """

    _CFG = {
        "default_agent": "kirocrew",
        "agents": {
            "kirocrew": {"kiro_agent": "kirocrew"},
            "coding": {"kiro_agent": "kirocrew", "triggers": "fix"},
        },
    }

    @staticmethod
    def _cli_body(**kw: Any) -> dict[str, Any]:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
            patch(
                "kiro_crew.cli_commands._gateway_send",
                return_value=_FakeResponse({"id": "ag1", "task": "t"}),
            ) as uo,
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=True, **kw))
        return _posted_body(uo)

    def _gateway(self, body: dict[str, Any], tmp_path: Any, monkeypatch: Any) -> Any:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps(self._CFG), encoding="utf-8")
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.memory_stores import provision_member_memory

        loader_mod._invalidate_config_cache()
        config = loader_mod.KiroCrewConfig.load()
        provision_member_memory(config, "coding")
        config.save()
        cov = pytest.importorskip("test_handlers_messaging_coverage")
        mgr = cov._mgr()
        mgr.spawn.return_value = cov._info()
        req = cov._Req(cov._state(subagents=mgr), body, extra={"internal_auth": True})
        resp = cov._run(cov.mod.api_spawn, req)
        loader_mod._invalidate_config_cache()
        return resp, mgr

    def test_crew_flag_spawns_into_that_members_store(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        resp, mgr = self._gateway(self._cli_body(crew="coding"), tmp_path, monkeypatch)
        assert resp.status == 200, resp
        stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        kwargs = mgr.spawn.call_args.kwargs
        assert kwargs["crew"] == "coding"
        assert kwargs["memory_store"] == stored["agents"]["coding"]["memory_store"]
        assert kwargs["memory_store"]

    def test_agent_flag_reaches_the_manager(self, tmp_path: Any, monkeypatch: Any) -> None:
        resp, mgr = self._gateway(self._cli_body(agent="kirocrew"), tmp_path, monkeypatch)
        assert resp.status == 200, resp
        assert mgr.spawn.call_args.kwargs["agent"] == "kirocrew"

    def test_unknown_crew_flag_is_refused_and_never_spawns(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        resp, mgr = self._gateway(self._cli_body(crew="ghost"), tmp_path, monkeypatch)
        assert resp.status == 404
        assert json.loads(resp.text)["code"] == "unknown_member"
        mgr.spawn.assert_not_called()

    def test_model_flag_reaches_the_manager_beside_the_crew(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        body = self._cli_body(crew="coding", model="m1")
        resp, mgr = self._gateway(body, tmp_path, monkeypatch)
        assert resp.status == 200, resp
        kwargs = mgr.spawn.call_args.kwargs
        assert (kwargs["model"], kwargs["crew"]) == ("m1", "coding")

    def test_malformed_model_flag_is_refused_and_never_spawns(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        resp, mgr = self._gateway(self._cli_body(model="a;b"), tmp_path, monkeypatch)
        assert resp.status == 400
        assert "model" in json.loads(resp.text)["error"]
        mgr.spawn.assert_not_called()
