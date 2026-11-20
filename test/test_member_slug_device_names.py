"""A member slug equal to a Windows device name is refused at the slug source.

``validate_slug`` is the one gate every writer of ``members/<slug>`` and
``member-records/<kind>/<slug>.json`` passes first, so one refusal there covers
them all. ``slug_for_name`` maps such a display name to a safe slug instead.
Runs under the autouse ``_isolate_kirocrew_home`` fixture, so paths are tmp.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import member_goals, member_notes, members
from kiro_crew.constants import WINDOWS_DEVICE_STEMS
from kiro_crew.members import MemberSlugError, slug_for_name, validate_slug

DEVICES = ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"]


def test_the_device_set_is_the_shared_one():
    assert {"con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"} <= WINDOWS_DEVICE_STEMS


@pytest.mark.parametrize("stem", sorted(WINDOWS_DEVICE_STEMS))
def test_validate_slug_refuses_every_device_stem(stem):
    with pytest.raises(MemberSlugError):
        validate_slug(stem)


@pytest.mark.parametrize("bad", ["CON", "Nul", "COM1", "lpt3", "con.txt", "CON.txt", "aux.json"])
def test_validate_slug_refuses_case_and_extension_variants(bad):
    with pytest.raises(MemberSlugError):
        validate_slug(bad)


@pytest.mark.parametrize("ok", ["com10", "lpt0", "console", "con-1", "nulls", "com"])
def test_validate_slug_still_accepts_near_misses(ok):
    assert validate_slug(ok) == ok


@pytest.mark.parametrize("name", ["CON", "con", "Nul", "COM1", "lpt9", "CON.txt", "aux.json"])
def test_slug_for_name_maps_a_device_name_to_a_safe_slug(name):
    slug = slug_for_name(name)
    assert slug not in WINDOWS_DEVICE_STEMS
    assert validate_slug(slug) == slug


def test_slug_for_name_suffixes_a_bare_device_name():
    assert slug_for_name("CON") == "con-bot"
    assert slug_for_name("COM1") == "com1-bot"


def test_member_slug_refuses_a_stored_device_member_id():
    config = SimpleNamespace(agents={"Bot": SimpleNamespace(member_id="con")})
    with pytest.raises(MemberSlugError):
        members.member_slug("Bot", config)


def test_member_slug_of_a_device_named_bot_without_id_is_safe():
    config = SimpleNamespace(agents={})
    assert members.member_slug("NUL", config) == "nul-bot"


@pytest.fixture
def no_filesystem(monkeypatch):
    """Any directory creation or atomic write fails the test: refusal comes first."""

    def boom(*_a, **_k):
        raise AssertionError("filesystem touched before the slug was refused")

    monkeypatch.setattr(os, "mkdir", boom)
    monkeypatch.setattr(os, "makedirs", boom)
    monkeypatch.setattr(Path, "mkdir", boom)


@pytest.mark.parametrize("slug", DEVICES)
def test_member_dir_refuses_a_device_slug(slug, no_filesystem):
    with pytest.raises(MemberSlugError):
        members.member_dir(slug)


@pytest.mark.parametrize("slug", ["con", "nul", "com1"])
def test_dm_binding_writer_refuses_a_device_slug(slug, no_filesystem):
    with pytest.raises(MemberSlugError):
        members.write_dm_binding(slug, member="Bot", slot_key=f"dm:{slug}")


@pytest.mark.parametrize("slug", ["con", "aux", "lpt1"])
def test_member_rules_writer_refuses_a_device_slug(slug, no_filesystem):
    with pytest.raises(MemberSlugError):
        members.write_member_rules(slug, member="Bot", text="be kind")


@pytest.mark.parametrize("slug", ["prn", "com9"])
def test_member_notes_writer_refuses_a_device_slug(slug, no_filesystem):
    with pytest.raises(MemberSlugError):
        member_notes.write_member_notes(slug, "hello")


@pytest.mark.parametrize("slug", ["con", "nul"])
def test_member_goals_writer_refuses_a_device_slug(slug, no_filesystem):
    with pytest.raises(MemberSlugError):
        member_goals.set_goal(slug, "Bot", "ship it", by="human")


@pytest.mark.parametrize("slug", ["con", "lpt2"])
def test_member_goals_path_refuses_a_device_slug(slug):
    with pytest.raises(MemberSlugError):
        member_goals.member_goals_path(slug)
