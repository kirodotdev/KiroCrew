"""Kiro Cloud Sessions profile rules: repository entries and the choice lock.

``parse_repository`` is the one check a profile's ``repositories`` entries pass
before they are sent to Kiro Agent as the cloud checkout list. The choice lock
fixes a cloud chat's environment only on positive cloud evidence. Uses the
fixtures of ``test_kiro_cloud_session``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_kiro_cloud_session import (  # noqa: F401 -- fixtures, used by name
    _developer_flag_on,
    _section,
    handlers,
    section,
)

from kiro_crew.config.session_environments import (
    SessionEnvironmentsError,
    validate_session_environments,
)


@pytest.mark.parametrize(
    "value, parsed",
    [
        ("web", (None, "web")),
        ("acme/web", ("acme", "web")),
        ("  acme/web.js  ", ("acme", "web.js")),
        ("a1_b-c.d/x", ("a1_b-c.d", "x")),
        ("acme/.github", ("acme", ".github")),
        ("a" * 100, (None, "a" * 100)),
        ("a" * 100 + "/" + "b" * 100, ("a" * 100, "b" * 100)),
    ],
)
def test_parse_repository_accepts_a_name_or_owner_and_name(value, parsed):
    from kiro_crew.config.session_environments import parse_repository

    assert parse_repository(value) == parsed


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(".", id="dot-name"),
        pytest.param("..", id="dotdot-name"),
        pytest.param("acme/.", id="dot-part"),
        pytest.param("acme/..", id="dotdot-part"),
        pytest.param("./web", id="dot-owner"),
        pytest.param("../web", id="dotdot-owner"),
        pytest.param("a" * 101, id="name-too-long"),
        pytest.param("a" * 101 + "/web", id="owner-too-long"),
        pytest.param("acme/" + "b" * 101, id="part-too-long"),
        pytest.param("acme/", id="empty-name"),
        pytest.param("/web", id="empty-owner"),
        pytest.param("a/b/c", id="three-parts"),
        pytest.param("a b", id="space"),
        pytest.param("acme/w\u00e9b", id="non-ascii"),
        pytest.param("acme/web;rm", id="semicolon"),
        pytest.param("--flag", id="leading-dash"),
        pytest.param(".hidden/web", id="owner-leading-dot"),
        pytest.param("_acme/web", id="owner-leading-underscore"),
        pytest.param(None, id="not-a-string"),
    ],
)
def test_parse_repository_refuses_anything_else(value):
    from kiro_crew.config.session_environments import parse_repository

    with pytest.raises(ValueError):
        parse_repository(value)


@pytest.mark.parametrize("repo", [".", "..", "acme/.", "acme/..", "../web"])
def test_config_refuses_a_dot_or_dotdot_repository_part(repo):
    """A ``.`` or ``..`` part is a path step, not a repository: refused, same message."""
    profile = {"id": "c", "name": "C", "repositories": [repo]}
    with pytest.raises(
        SessionEnvironmentsError,
        match=r"kiro_cloud_profiles\[0\]\.repositories entries must be a repository name or owner/name",
    ):
        validate_session_environments(_section(kiro_cloud_profiles=[profile]))


@pytest.mark.asyncio
async def test_an_unreadable_choices_file_does_not_lock_a_chat_without_cloud_evidence(
    handlers, tmp_path  # noqa: F811
):
    """Only positive cloud evidence locks: an unreadable choices file leaves the
    decision to main's workspace check, which finds no workspace for this chat."""
    from kiro_crew.coder import session_launch as sl

    choices = handlers._choices()
    choices.set("dashboard:coder", "dev", sl.PROVIDER_ID)
    (tmp_path / "coder" / "session-choices.json").write_text("{not json", encoding="utf-8")
    started = SimpleNamespace(total_messages=1, messages=[])
    assert await handlers._choice_locked(started, "dashboard:coder", "dev") is False


@pytest.mark.asyncio
async def test_a_cloud_chat_with_a_recorded_sandbox_stays_locked(handlers):  # noqa: F811
    from kiro_crew.coder import session_launch as sl

    choices = handlers._choices()
    choices.set("dashboard:cloud", "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    assert choices.record_cloud_session("dashboard:cloud", "cloud-1", profile_id="cloud") is True
    started = SimpleNamespace(total_messages=1, messages=[])
    assert handlers._fixed_by_provider("cloud", "dashboard:cloud") is True
    assert await handlers._choice_locked(started, "dashboard:cloud", "cloud") is True
