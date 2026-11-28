"""A Channels room seat bound to no Bot store gets no lessons from any lesson or recall route.

Its add, remove, update and recall calls were already refused as an unknown
session, but the list route read the Global lessons for it, so a room seat could
post the owner's Global lessons into a shared room. Every route now answers it
the same way, and the Global store is never read or written on its behalf.
"""

import json

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request

from kiro_crew.channel import ChannelManager
from kiro_crew.dashboard.handlers import cron, memory_member
from kiro_crew.learn import Lesson, LessonStore

env = _member_env

GLOBAL_MARKER = "global owner lesson kumquat"
ROOM_MARKER = "room seat lesson zebra"
#: A Global-store template session with a live dashboard slot: the control.
RECOGNIZED = "dashboard:carol"


@pytest.fixture
def global_lessons(env, tmp_path) -> LessonStore:
    from types import SimpleNamespace

    store = LessonStore(base_dir=tmp_path / "global-lessons")
    store.save(Lesson(rule=GLOBAL_MARKER, category="knowledge", ts="2026-11-28T00:00:00Z"))
    env.state.lessons = store
    env.state.get_slot = lambda *_args, **_kwargs: None
    env.state._slots["carol"] = SimpleNamespace(
        key="carol", is_restricted=False, blocks_reads=False
    )
    return store


def _keys(env, tmp_path) -> dict[str, str]:
    manager = ChannelManager(channels_dir=str(tmp_path / "channels"))
    room = manager.create("incident")
    env.state.channel_manager = manager
    template = room.add_agent(role="Helper", agent_name="", task="t")
    bot = room.add_agent(role="Reviewer", agent_name="alice", task="t")
    return {
        "template seat": template.session_key,
        "unstarted Bot seat": bot.session_key,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["template seat", "unstarted Bot seat"])
async def test_no_lesson_or_recall_route_reaches_the_global_store(
    env, tmp_path, global_lessons, caller
):
    key = _keys(env, tmp_path)[caller]
    before = [lesson.rule for lesson in global_lessons.load_all()]

    responses = {
        "list": await cron.api_lessons(
            make_request(env.state, "/api/lessons", session=key, internal=True)
        ),
        "add": await cron.api_lessons_create(
            make_request(
                env.state,
                "/api/lessons",
                body={"rule": ROOM_MARKER, "category": "knowledge"},
                session=key,
                internal=True,
            )
        ),
        "update": await cron.api_lessons_update(
            make_request(
                env.state,
                "/api/lessons",
                method="PATCH",
                body={"rule": GLOBAL_MARKER, "new_rule": ROOM_MARKER},
                session=key,
                internal=True,
            )
        ),
        "remove": await cron.api_lessons_delete(
            make_request(
                env.state,
                "/api/lessons",
                method="DELETE",
                body={"query": GLOBAL_MARKER},
                session=key,
                internal=True,
            )
        ),
        "recall": await memory_member.api_memory_recall(
            make_request(
                env.state,
                "/api/memory/recall",
                query={"q": GLOBAL_MARKER},
                session=key,
                internal=True,
            )
        ),
    }

    for route, response in responses.items():
        assert response.status == 400, (route, response.status, response.text)
        assert json.loads(response.text)["code"] == "unknown_session", route
        assert GLOBAL_MARKER not in response.text, route
    assert [lesson.rule for lesson in global_lessons.load_all()] == before


@pytest.mark.asyncio
async def test_a_recognized_session_still_lists_the_global_lessons(env, global_lessons):
    response = await cron.api_lessons(
        make_request(env.state, "/api/lessons", session=RECOGNIZED, internal=True)
    )

    assert response.status == 200, response.text
    assert [row["rule"] for row in json.loads(response.text)["lessons"]] == [GLOBAL_MARKER]
