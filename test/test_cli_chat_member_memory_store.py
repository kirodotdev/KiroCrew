"""``kirocrew chat --agent <bot>``: the Bot's memory tools work against its own store.

A plain run presents the fixed key ``cli_chat``. A run as a Bot with its own
memory store presents a key of its own, ``cli_chat:<run>``, bound to the Bot's
execution while the run lasts, so learn_add, learn_list and memory_recall read and
write the Bot's store and never the Global one. The binding is released when the
run ends.
"""

import json
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request

from kiro_crew import cli_chat, context
from kiro_crew.config.loader import _session_work_dir
from kiro_crew.dashboard.handlers import cron, memory_member
from kiro_crew.execution_context import read_session_execution
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.session_token_sig import publish_session_token, verify_session_token

env = _member_env

BOT = "alice"
BOT_STORE = "member-alice"
GLOBAL_MARKER = "global owner lesson kumquat"
RUN_MARKER = "terminal run lesson zebra"
#: The run's own token, and one minted for another session.
RUN_TOKEN = "r" * 64
OTHER_TOKEN = "o" * 64


class _StopAtStart(Exception):
    pass


async def _run_chat(
    monkeypatch, agent: str | None, during: Callable[[str], Awaitable[None]] | None = None
) -> tuple[str, dict]:
    """Run ``_chat`` to the backend start, call *during* with the live key, then stop it.

    Returns the session key the run presented and the arguments it built its
    provider with.
    """
    built: list[tuple[str, dict]] = []

    def factory(cfg):
        def build(session_key, **kwargs):
            built.append((session_key, kwargs))
            return MagicMock()

        return build

    async def start_run(provider, run):
        if during is not None:
            await during(built[0][0])
        raise _StopAtStart

    monkeypatch.setattr(cli_chat, "build_provider_factory", factory)
    monkeypatch.setattr(cli_chat, "start_run", start_run)
    monkeypatch.setattr("kiro_crew.agent_sdk.backends.ensure_kiro_cli_kas_verdict", lambda: None)
    with pytest.raises(_StopAtStart):
        await cli_chat._chat("hi", None, agent)
    assert len(built) == 1
    return built[0]


@pytest.fixture
def stores(env, tmp_path, monkeypatch):
    monkeypatch.setattr(context, "_vector_stores", dict(env.tiers))
    monkeypatch.setattr(context, "_memory_stores", {})
    env.state.push_refresh = lambda _resource: None
    env.state.get_slot = lambda *_args, **_kwargs: None
    env.state._background_tasks = set()
    env.state.lessons = LessonStore(base_dir=tmp_path / "global-lessons")
    env.state.lessons.save(
        Lesson(rule=GLOBAL_MARKER, category="knowledge", ts="2026-11-28T00:00:00Z")
    )
    return env


def _call(env, key: str, path: str, **kwargs):
    return make_request(env.state, path, session=key, internal=True, **kwargs)


def _bound_store(key: str) -> str | None:
    """The store *key*'s record binds, or None when no record binds it."""
    execution = read_session_execution(key)
    return execution.store.legacy_name if execution is not None else None


@pytest.mark.asyncio
async def test_a_bot_run_learns_and_recalls_in_its_own_store(stores, monkeypatch):
    env = stores
    global_vector_before = env.tiers[""].count_lessons()
    answers: dict = {}

    async def use_memory(key: str) -> None:
        answers["store"] = _bound_store(key)
        answers["added"] = await cron.api_lessons_create(
            _call(env, key, "/api/lessons", body={"rule": RUN_MARKER})
        )
        answers["listed"] = await cron.api_lessons(_call(env, key, "/api/lessons"))
        answers["recalled"] = await memory_member.api_memory_recall(
            _call(env, key, "/api/memory/recall", query={"q": RUN_MARKER})
        )

    key, _kwargs = await _run_chat(monkeypatch, BOT, use_memory)

    assert key.startswith("cli_chat:") and key != "cli_chat"
    assert answers["store"] == BOT_STORE
    assert answers["added"].status == 200, answers["added"].text
    assert answers["listed"].status == 200, answers["listed"].text
    rules = [row["rule"] for row in json.loads(answers["listed"].text)["lessons"]]
    assert RUN_MARKER in rules and GLOBAL_MARKER not in rules
    assert answers["recalled"].status == 200, answers["recalled"].text
    assert RUN_MARKER in answers["recalled"].text
    assert GLOBAL_MARKER not in answers["recalled"].text
    assert RUN_MARKER in json.dumps(env.tiers[BOT_STORE].get_lessons())
    assert env.tiers[""].count_lessons() == global_vector_before
    assert [lesson.rule for lesson in env.state.lessons.load_all()] == [GLOBAL_MARKER]


@pytest.mark.asyncio
async def test_a_token_attested_bot_run_reaches_its_own_store(
    stores, monkeypatch, sel_private_root
):
    """The run's MCP calls carry only the signed token the runtime publishes for its key.

    No kernel peer attestation: a terminal chat's process tree is not one the
    gateway placed, so the token is the whole proof of which session is calling.
    """
    env = stores
    answers: dict = {}

    async def call_with_tokens(key: str) -> None:
        publish_session_token(RUN_TOKEN, key)
        publish_session_token(OTHER_TOKEN, "dashboard:someone-else")
        answers["verified"] = verify_session_token(RUN_TOKEN)
        answers["own"] = await cron.api_lessons_create(
            _call(
                env,
                key,
                "/api/lessons",
                body={"rule": RUN_MARKER},
                attested=False,
                session_token=RUN_TOKEN,
            )
        )
        answers["foreign"] = await cron.api_lessons_create(
            _call(
                env,
                key,
                "/api/lessons",
                body={"rule": "foreign token lesson"},
                attested=False,
                session_token=OTHER_TOKEN,
            )
        )

    key, _kwargs = await _run_chat(monkeypatch, BOT, call_with_tokens)

    assert answers["verified"] == key
    assert answers["own"].status == 200, answers["own"].text
    assert RUN_MARKER in json.dumps(env.tiers[BOT_STORE].get_lessons())
    assert answers["foreign"].status == 503, answers["foreign"].text
    assert json.loads(answers["foreign"].text)["code"] == "store_unavailable"
    assert "foreign token lesson" not in json.dumps(env.tiers[BOT_STORE].get_lessons())
    assert [lesson.rule for lesson in env.state.lessons.load_all()] == [GLOBAL_MARKER]


@pytest.mark.asyncio
async def test_the_run_key_is_released_when_the_run_ends(stores, monkeypatch):
    env = stores

    key, _kwargs = await _run_chat(monkeypatch, BOT)

    assert read_session_execution(key) is None
    refusal = await cron._recognize_session(
        env.state, key, "test", blocks_persisted_mode=lambda _mode: False
    )
    assert refusal is not None and json.loads(refusal.text)["code"] == "unknown_session"


@pytest.mark.asyncio
async def test_each_bot_run_gets_a_key_of_its_own(stores, monkeypatch):
    bound: list[str | None] = []

    async def note_store(key: str) -> None:
        bound.append(_bound_store(key))

    first, _ = await _run_chat(monkeypatch, BOT, note_store)
    second, _ = await _run_chat(monkeypatch, BOT, note_store)

    assert first != second
    assert bound == [BOT_STORE, BOT_STORE]


@pytest.mark.asyncio
async def test_a_bot_run_shares_the_chat_work_directory(stores, monkeypatch):
    _plain_key, plain = await _run_chat(monkeypatch, None)
    _bot_key, bot = await _run_chat(monkeypatch, BOT)

    shared = str(_session_work_dir(cli_chat._CLI_SESSION_KEY))
    assert plain.get("cwd") == shared
    assert bot.get("cwd") == shared


@pytest.mark.asyncio
async def test_a_bot_on_a_legacy_v1_store_is_recognized_in_its_run(stores, monkeypatch):
    """A V1 named store carries no member id; the run is still its Bot's own."""
    from kiro_crew.config.loader import _invalidate_config_cache

    env = stores
    config_file = env.home / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    (env.home / "memory_stores" / "team-notes").mkdir(parents=True)
    config["memory_stores"]["team-notes"] = {"memory_version": 1}
    config["agents"]["carol"] = {"memory_store": "team-notes"}
    config_file.write_text(json.dumps(config), encoding="utf-8")
    _invalidate_config_cache()
    seen: dict = {}

    async def recognize(key: str) -> None:
        execution = read_session_execution(key)
        seen["binding"] = execution and (execution.member_id, execution.store.legacy_name)
        seen["refusal"] = await cron._recognize_session(
            env.state, key, "test", blocks_persisted_mode=lambda _mode: False
        )

    await _run_chat(monkeypatch, "carol", recognize)

    assert seen["binding"] == (None, "team-notes")
    assert seen["refusal"] is None


@pytest.mark.asyncio
async def test_a_plain_run_keeps_the_fixed_key_and_binds_nothing(stores, monkeypatch):
    key, _kwargs = await _run_chat(monkeypatch, None)

    assert key == cli_chat._CLI_SESSION_KEY
    assert read_session_execution(key) is None


@pytest.mark.asyncio
async def test_a_bot_that_cannot_be_bound_never_starts(stores, monkeypatch, capsys):
    from kiro_crew.memory_stores import UnknownMemoryStore

    def refuse(*_args, **_kwargs):
        raise UnknownMemoryStore("member identity is missing or ambiguous")

    async def start_run(provider, run):
        pytest.fail("the backend started for a Bot that could not be bound")

    monkeypatch.setattr("kiro_crew.execution_context.resolve_member_execution", refuse)
    monkeypatch.setattr(cli_chat, "build_provider_factory", lambda cfg: lambda *a, **k: MagicMock())
    monkeypatch.setattr(cli_chat, "start_run", start_run)
    monkeypatch.setattr("kiro_crew.agent_sdk.backends.ensure_kiro_cli_kas_verdict", lambda: None)
    with pytest.raises(SystemExit) as exited:
        await cli_chat._chat("hi", None, BOT)

    assert exited.value.code == 1
    assert "member identity is missing" in capsys.readouterr().err
