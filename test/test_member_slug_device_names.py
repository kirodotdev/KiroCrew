"""Windows device stems are refused where a member id is MINTED, never on a read.

A POSIX install can already hold a member whose slug is a device stem: the
upgrade keeps one on purpose (``memory_stores._allocate_member_id`` with
``refuse_damaged=False``) because that member's records are keyed by it. So
``validate_slug``, ``slug_for_name`` and ``member_slug`` must keep answering such
a slug, and only the minting of a NEW id may refuse it. Runs under the autouse
``_isolate_kirocrew_home`` fixture, so paths are tmp.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from kiro_crew import member_goals, member_notes, members
from kiro_crew.constants import WINDOWS_DEVICE_STEMS
from kiro_crew.members_parts.member_sessions import member_slot_key
from kiro_crew.memory_stores import _allocate_member_id

_requires_nofollow = pytest.mark.skipif(
    not members.member_briefing_supported(),
    reason="briefing reads and writes fail closed without O_NOFOLLOW",
)

# On Windows `con.json` and `members/con/` are the device, not a file: the cases
# that touch the filesystem for a device stem only make sense on POSIX.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="a device-stem path is the device on Windows"
)

DEVICES =["con", "nul", "com1", "lpt9"]


def _config(**agents: str) -> SimpleNamespace:
    return SimpleNamespace(
        agents={name: SimpleNamespace(member_id=mid) for name, mid in agents.items()},
        memory_stores={},
    )


@pytest.mark.parametrize("slug", DEVICES)
def test_an_existing_device_slug_still_validates(slug):
    assert members.validate_slug(slug) == slug


@_posix_only
@pytest.mark.parametrize("slug", DEVICES)
def test_an_existing_device_slug_still_resolves_to_its_directory(slug):
    assert members.member_dir(slug).name == slug


@pytest.mark.parametrize("name", ["Con", "NUL", "Com1"])
def test_a_legacy_member_keeps_its_name_derived_slug(name):
    assert members.slug_for_name(name) == name.lower()
    assert members.member_slug(name, _config()) == name.lower()


def test_a_member_with_a_stored_device_id_keeps_it():
    assert members.member_slug("Bot", _config(Bot="con")) == "con"


@_posix_only
def test_an_existing_device_slug_can_be_written_and_read_back():
    members.write_member_rules("con", member="Con", text="be kind")
    assert members.read_member_rules("con", "Con") == "be kind"
    members.write_dm_binding("con", member="Con", slot_key=member_slot_key("con"))
    assert members.read_dm_binding("con") is not None
    member_goals.set_goal("con", "Con", "ship it", by="human")
    assert member_goals.read_goals("con", "Con")["goals"]


@_posix_only
@_requires_nofollow
def test_an_existing_device_slug_keeps_its_notes():
    member_notes.write_member_notes("con", "hello")
    assert members.read_member_briefing("con") == "hello"


@pytest.mark.parametrize("name", ["CON", "con", "Nul", "COM1", "lpt9"])
def test_a_new_bot_named_for_a_device_gets_a_non_device_member_id(name):
    member_id = _allocate_member_id(_config(), name)
    assert member_id not in WINDOWS_DEVICE_STEMS
    assert members.validate_slug(member_id) == member_id


@pytest.mark.parametrize("stem", sorted(WINDOWS_DEVICE_STEMS))
def test_no_new_member_id_is_ever_a_device_stem(stem):
    assert _allocate_member_id(_config(), stem) not in WINDOWS_DEVICE_STEMS
