"""Provider-qualified model ids at the spawn, task-batch, cron and session boundaries.

A backend that resolves its model from the list it advertises (OpenCode, Pi) spells
ids ``provider/model``, nested (``provider/org/model``) and with an optional ``:tag``
on the last segment (``ollama/qwen3:8b``). ``_MODEL_NAME_RE`` is the one grammar every
boundary shares; the Settings pins build their qualified branch from the same
constant, so a picker cannot offer an id that one of them refuses.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew import model_registry
from kiro_crew.acp_backends import model_registry_namespace
from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create
from kiro_crew.dashboard.handlers.core import _MODEL_PIN_PATTERN
from kiro_crew.validation import (
    _MODEL_NAME_RE,
    CRON_ADD_SCHEMA,
    MCP_CRON_SCHEMAS,
    SESSION_CREATE_SCHEMA,
    SPAWN_CONTINUE_SCHEMA,
    SPAWN_RUN_SCHEMA,
    ValidationError,
    validate_tool_args,
)

QUALIFIED = [
    "ollama/qwen3:8b",
    "provider/model",
    "provider/org/model",
    "provider/org/team/model-7b",
    "hf.co/org/Model-GGUF:Q4_K_M",
    "bedrock/us.vendor.model-v1:0",
]
UNCHANGED = [
    "deepseek-3.2",
    "auto",
    "glm-4.7",
    "model_1.v2-fast",
    "gpt-6-astra[high]",
    "model[A1._-]",
]

# One list per rule, and every entry breaks that rule alone, so a mutation of one
# rule fails only its own list.
SEGMENT_MUST_START_ALPHANUMERIC = [
    "-opt/model",
    "provider/-model",
    "provider/.hidden",
    "provider/_model",
    "provider/./model",
    "provider/../model",
    "../etc/passwd",
    "./model",
    "/abs/path",
    "~/model",
    "provider/~model",
    "@scope/model",
]
NO_EMPTY_SEGMENT = ["a//b", "a/", "provider//model:8b", "provider/model/", "provider/:8b"]
COLON_ONLY_AS_TRAILING_TAG_AFTER_A_SLASH = [
    "model:8b",
    "auto:jev",
    "provider:x/model",
    "provider/mo:del/x",
    "provider/model:",
    "provider/model:8b:9b",
    "provider/model:/x",
]
CHARSET_IS_CLOSED = [
    "provider/model name",
    "provider/model;x",
    "provider/$(x)",
    "provider/`x`",
    "provider/model\\x",
    "provider/model'x",
    'provider/model"x',
    "provider/model|x",
    "provider/model&x",
    "provider/model*",
    "provider/model?x",
    "provider/model%2f",
    "provider/model#x",
    "provider/model!",
    "provider/m\u00f8del",
    "provider/model[high]",
]
CONTROL_CHARACTERS = [
    "ollama/qwen3:8b\n",
    "ollama/qwen3:8b\r",
    "provider/mo\x00del",
    "provider/mo\x1bdel",
    "provider/mo\tdel",
    "provider/mo\x7fdel",
    "provider/model\u2028",
    "deepseek-3.2\n",
]


def _qualified_of_length(n: int) -> str:
    """A well-formed qualified id of exactly *n* characters."""
    head = "provider/"
    return head + "m" * (n - len(head))


class TestGrammar:
    @pytest.mark.parametrize("name", QUALIFIED)
    def test_qualified_nested_and_colon_ids_are_accepted(self, name):
        assert _MODEL_NAME_RE.match(name)

    @pytest.mark.parametrize("name", UNCHANGED)
    def test_legacy_and_bracket_names_are_unchanged(self, name):
        assert _MODEL_NAME_RE.match(name)

    @pytest.mark.parametrize("name", SEGMENT_MUST_START_ALPHANUMERIC)
    def test_a_segment_must_start_alphanumeric(self, name):
        assert not _MODEL_NAME_RE.match(name)

    @pytest.mark.parametrize("name", NO_EMPTY_SEGMENT)
    def test_no_segment_is_empty(self, name):
        assert not _MODEL_NAME_RE.match(name)

    @pytest.mark.parametrize("name", COLON_ONLY_AS_TRAILING_TAG_AFTER_A_SLASH)
    def test_a_colon_is_only_a_trailing_tag_of_a_qualified_id(self, name):
        assert not _MODEL_NAME_RE.match(name)

    @pytest.mark.parametrize("name", CHARSET_IS_CLOSED)
    def test_the_charset_is_closed(self, name):
        assert not _MODEL_NAME_RE.match(name)

    @pytest.mark.parametrize("name", CONTROL_CHARACTERS)
    def test_a_control_character_is_refused_even_at_the_end(self, name):
        assert not _MODEL_NAME_RE.match(name)
        assert not _MODEL_NAME_RE.fullmatch(name)

    def test_a_qualified_id_is_capped_at_the_advertised_id_bound(self):
        cap = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
        assert _MODEL_NAME_RE.match(_qualified_of_length(cap))
        assert not _MODEL_NAME_RE.match(_qualified_of_length(cap + 1))

    def test_a_long_near_miss_is_refused_without_backtracking_blowup(self):
        assert not _MODEL_NAME_RE.match("a/" * 5000 + "!")


class TestSettingsPinsShareTheQualifiedBranch:
    """``_MODEL_PIN_PATTERN`` (Settings pins) and ``_MODEL_NAME_RE`` agree on qualified ids."""

    @pytest.mark.parametrize("name", QUALIFIED)
    def test_a_pin_accepts_every_qualified_id(self, name):
        assert re.fullmatch(_MODEL_PIN_PATTERN, name)

    @pytest.mark.parametrize(
        "name",
        SEGMENT_MUST_START_ALPHANUMERIC
        + NO_EMPTY_SEGMENT
        + [n for n in COLON_ONLY_AS_TRAILING_TAG_AFTER_A_SLASH if "/" in n]
        + [n for n in CHARSET_IS_CLOSED if "[" not in n]
        + [n for n in CONTROL_CHARACTERS if "/" in n],
    )
    def test_a_pin_refuses_what_the_name_grammar_refuses(self, name):
        assert not re.fullmatch(_MODEL_PIN_PATTERN, name)

    @pytest.mark.parametrize("value", ["", "auto", "deepseek-3.2", "model[1m]", "a/b", "a/b/c"])
    def test_pins_accepted_before_are_still_accepted(self, value):
        assert re.fullmatch(_MODEL_PIN_PATTERN, value)

    def test_every_boundary_uses_the_one_grammar_object(self):
        from kiro_crew import cli_commands, validation
        from kiro_crew.dashboard import session_control
        from kiro_crew.dashboard.handlers import cron as cron_handlers

        for module in (cli_commands, cron_handlers, session_control):
            assert module._MODEL_NAME_RE is validation._MODEL_NAME_RE


def _spawn_args(model: str) -> dict[str, Any]:
    return {"task": "x", "model": model}


# (label, schema, args builder, cleaned-value reader)
SCHEMAS = [
    ("spawn_run", SPAWN_RUN_SCHEMA, _spawn_args, lambda c: c["model"]),
    (
        "spawn_run.tasks[]",
        SPAWN_RUN_SCHEMA,
        lambda m: {"tasks": [{"task": "x", "model": m}]},
        lambda c: c["tasks"][0]["model"],
    ),
    (
        "spawn_continue",
        SPAWN_CONTINUE_SCHEMA,
        lambda m: {"conversation": "c1", "task": "x", "model": m},
        lambda c: c["model"],
    ),
    (
        "cron_add",
        CRON_ADD_SCHEMA,
        lambda m: {"name": "j", "message": "m", "every": 300, "model": m},
        lambda c: c["model"],
    ),
    (
        "cron_update",
        MCP_CRON_SCHEMAS["cron_update"],
        lambda m: {"job_id": "abc123", "model": m},
        lambda c: c["model"],
    ),
    ("session_create", SESSION_CREATE_SCHEMA, lambda m: {"model": m}, lambda c: c["model"]),
]
SCHEMA_IDS = [s[0] for s in SCHEMAS]


class TestSchemas:
    @pytest.mark.parametrize("label,schema,make,read", SCHEMAS, ids=SCHEMA_IDS)
    @pytest.mark.parametrize("name", QUALIFIED)
    def test_a_qualified_id_passes_unchanged(self, label, schema, make, read, name):
        assert read(validate_tool_args(make(name), schema)) == name

    @pytest.mark.parametrize("label,schema,make,read", SCHEMAS, ids=SCHEMA_IDS)
    @pytest.mark.parametrize("name", UNCHANGED)
    def test_legacy_names_pass_unchanged(self, label, schema, make, read, name):
        assert read(validate_tool_args(make(name), schema)) == name

    @pytest.mark.parametrize("label,schema,make,read", SCHEMAS, ids=SCHEMA_IDS)
    @pytest.mark.parametrize("name", ["deepseek-3.2", "provider/model:8b"])
    def test_edge_whitespace_is_trimmed_before_the_pattern_runs(
        self, label, schema, make, read, name
    ):
        assert read(validate_tool_args(make(f" {name}\n"), schema)) == name

    @pytest.mark.parametrize("label,schema,make,read", SCHEMAS, ids=SCHEMA_IDS)
    @pytest.mark.parametrize(
        "name",
        [
            "../etc/passwd",
            "provider//model",
            "provider/model name",
            "provider/$(x)",
            "model:8b",
            _qualified_of_length(model_registry.ADVERTISED_MODEL_ID_MAX_CHARS + 1),
        ],
    )
    def test_a_malformed_or_oversized_id_is_refused(self, label, schema, make, read, name):
        with pytest.raises(ValidationError):
            validate_tool_args(make(name), schema)

    @pytest.mark.parametrize("label,schema,make,read", SCHEMAS, ids=SCHEMA_IDS)
    def test_a_control_character_never_survives_into_the_cleaned_value(
        self, label, schema, make, read
    ):
        # The field sanitizer drops control characters before the pattern runs, so a
        # value either fails the pattern or reaches the caller without one.
        try:
            cleaned = read(validate_tool_args(make("provider/mo\x00del"), schema))
        except ValidationError:
            return
        assert cleaned == "provider/model"


def _run_tool(args: dict[str, Any]) -> list[dict]:
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": f"a{len(bodies)}"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        mcp_core._call_tool("spawn_run", args)
    return bodies


class TestTaskBatchWire:
    def test_each_task_carries_its_own_qualified_model_and_backend_unchanged(self):
        bodies = _run_tool(
            {
                "tasks": [
                    {"task": "t1", "model": "ollama/qwen3:8b", "backend": "opencode"},
                    {"task": "t2", "model": "provider/org/model", "backend": "pi"},
                    "t3",
                ],
                "model": "provider/model",
                "backend": "opencode",
            }
        )
        assert [(b["model"], b["backend"]) for b in bodies] == [
            ("ollama/qwen3:8b", "opencode"),
            ("provider/org/model", "pi"),
            ("provider/model", "opencode"),
        ]


@pytest.fixture
def backends(monkeypatch):
    """Kiro, Claude, OpenCode and Pi selectable and installed; no model list advertised."""
    from kiro_crew import acp_backends
    from kiro_crew.agent_sdk import backend_install
    from kiro_crew.agent_sdk.backend_install import INSTALLED, BackendInstallState

    values = ["", "claude", "opencode", "pi"]
    monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: sorted(values))
    monkeypatch.setattr(
        backend_install,
        "probe_backend",
        lambda b: BackendInstallState(
            b,
            b or "kiro",
            INSTALLED,
            missing_components=(),
            install_command="",
            restart_required=False,
        ),
    )
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})


def _advertise(backend: str, ids: list[str]) -> None:
    model_registry._ADVERTISED_MODELS[model_registry_namespace(backend)] = list(ids)


def _request(body: dict) -> tuple[Any, MagicMock]:
    mgr = MagicMock()
    mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
    mgr.max_concurrent = 4
    state = SimpleNamespace(subagents=mgr, conversation_log=MagicMock())
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {}

    async def _json() -> dict:
        return body

    request.json = _json
    return request, mgr


@pytest.mark.usefixtures("healthy_host_memory", "backends")
class TestSpawnEndpoint:
    async def _spawn(self, **body):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x", **body})
        return await api_spawn(request), mgr

    @pytest.mark.asyncio
    async def test_a_qualified_id_reaches_the_named_backend_unchanged(self):
        resp, mgr = await self._spawn(backend="opencode", model="ollama/qwen3:8b")
        assert resp.status == 200
        assert mgr.spawn.call_args.kwargs["model"] == "ollama/qwen3:8b"
        assert mgr.spawn.call_args.kwargs["backend"] == "opencode"

    @pytest.mark.asyncio
    async def test_a_qualified_id_the_backend_advertised_is_accepted(self):
        _advertise("opencode", ["ollama/qwen3:8b", "provider/org/model"])
        resp, mgr = await self._spawn(backend="opencode", model="provider/org/model")
        assert resp.status == 200
        assert mgr.spawn.call_args.kwargs["model"] == "provider/org/model"

    @pytest.mark.asyncio
    async def test_a_qualified_id_the_backend_did_not_advertise_is_refused(self):
        _advertise("opencode", ["ollama/qwen3:8b"])
        resp, mgr = await self._spawn(backend="opencode", model="provider/org/model")
        assert resp.status == 400
        payload = json.loads(resp.body)
        assert payload["code"] == "model_unavailable"
        assert "advertises" in payload["error"]
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_each_backend_is_checked_against_its_own_list(self):
        _advertise("opencode", ["provider/org/model"])
        _advertise("pi", ["ollama/qwen3:8b"])
        resp, mgr = await self._spawn(backend="pi", model="provider/org/model")
        assert resp.status == 400
        payload = json.loads(resp.body)
        assert payload["code"] == "model_unavailable"
        assert "advertises" in payload["error"]
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_backend_that_does_not_resolve_from_a_list_refuses_a_qualified_id(self):
        resp, mgr = await self._spawn(backend="kiro", model="provider/model")
        assert resp.status == 400
        payload = json.loads(resp.body)
        assert payload["code"] == "model_unavailable"
        assert "does not take provider-qualified" in payload["error"]
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_named_backend_is_judged_on_shape_alone(self):
        resp, mgr = await self._spawn(model="ollama/qwen3:8b")
        assert resp.status == 200
        assert mgr.spawn.call_args.kwargs["model"] == "ollama/qwen3:8b"
        assert mgr.spawn.call_args.kwargs["backend"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["deepseek-3.2", "gpt-6-astra[high]", "never-advertised"])
    @pytest.mark.parametrize("backend", ["kiro", "opencode"])
    async def test_legacy_names_get_no_new_gate_on_any_backend(self, name, backend):
        _advertise("opencode", ["ollama/qwen3:8b"])
        resp, mgr = await self._spawn(backend=backend, model=name)
        assert resp.status == 200
        assert mgr.spawn.call_args.kwargs["model"] == name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["../etc/passwd", "provider//model", "provider/$(x)"])
    async def test_a_malformed_id_is_refused_before_the_backend_is_asked(self, name):
        resp, mgr = await self._spawn(backend="opencode", model=name)
        assert resp.status == 400
        mgr.spawn.assert_not_called()


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


def _update_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    request = _create_request(body, crons)
    request.match_info = {"job_id": job_id}
    return request


class TestCronEndpoints:
    @pytest.fixture(autouse=True)
    def _isolate_cron_store(self, _floor_monkeypatch, tmp_path):
        _floor_monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", QUALIFIED)
    async def test_create_persists_a_qualified_id_verbatim(self, tmp_path, name):
        crons = CronService(base_dir=tmp_path)
        resp = await api_crons_create(
            _create_request({"name": "j", "message": "m", "every": 3600, "model": name}, crons)
        )
        assert resp.status == 200
        assert crons.list_jobs()[0].model == name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", QUALIFIED)
    async def test_update_persists_a_qualified_id_verbatim(self, tmp_path, name):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"model": name}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].model == name

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["../etc/passwd", "provider//model", "provider/$(x)", "provider/model:"]
    )
    async def test_create_and_update_refuse_a_malformed_id(self, tmp_path, name):
        crons = CronService(base_dir=tmp_path)
        resp = await api_crons_create(
            _create_request({"name": "j", "message": "m", "every": 3600, "model": name}, crons)
        )
        assert resp.status == 400
        assert crons.list_jobs() == []
        job = crons.add_job(name="k", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"model": name}, crons, job.id))
        assert resp.status == 400
        assert crons.list_jobs()[0].model == ""
