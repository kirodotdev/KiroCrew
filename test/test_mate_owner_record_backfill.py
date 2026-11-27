"""A Bot file written before the owner record existed (``mate_config.crewmate_file_on_record``).

Such a file has no entry in ``owners.json``. It counts as this instance's only
when this instance's roster names it, and it is then recorded once, so the
gateway gives it an overlay and its shared servers stay shared.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_config
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture
def instance(monkeypatch, tmp_path):
    """A Kiro home with one unrecorded Bot file, and this instance's data home."""
    kiro = tmp_path / "kiro"
    (kiro / "mates" / "agents").mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    this_home = str(tmp_path / "this-instance")
    monkeypatch.setattr(mate_config, "_this_home", lambda: this_home)
    mate_config.mate_config_path("slack").write_text(
        json.dumps({"name": "slack"}), encoding="utf-8"
    )
    return SimpleNamespace(home=this_home, owners=mate_config._owners_path())


def _roster(monkeypatch, **bindings: tuple[str, str]) -> None:
    """``config.agents`` as ``alias=(member_id, kiro_agent)``."""
    config = SimpleNamespace(
        agents={
            alias: SimpleNamespace(member_id=mid, kiro_agent=file)
            for alias, (mid, file) in bindings.items()
        }
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **kw: config))


def test_an_unrecorded_file_this_roster_lists_is_this_instances_and_is_recorded(
    instance, monkeypatch
):
    _roster(monkeypatch, slack=("slack", "slack"))

    assert mate_config.crewmate_file_on_record("slack") is True
    assert json.loads(instance.owners.read_text(encoding="utf-8")) == {"slack": instance.home}


def test_the_alias_binding_that_names_the_file_owns_it_whatever_its_member_id(
    instance, monkeypatch
):
    _roster(monkeypatch, slack=("slack-0a1b2c3d4e5f", "slack"))

    assert mate_config.crewmate_file_on_record("slack") is True


def test_a_binding_whose_member_id_matches_but_points_elsewhere_claims_nothing(
    instance, monkeypatch
):
    _roster(monkeypatch, other=("slack", "kirocrew"))

    assert mate_config.crewmate_file_on_record("slack") is False
    assert not instance.owners.exists()


def test_a_template_binding_with_no_member_id_claims_nothing(instance, monkeypatch):
    _roster(monkeypatch, slack=("", "slack"))

    assert mate_config.crewmate_file_on_record("slack") is False
    assert not instance.owners.exists()


def test_a_file_another_instance_recorded_is_not_this_instances(instance, monkeypatch, tmp_path):
    _roster(monkeypatch, slack=("slack", "slack"))
    other = {"slack": str(tmp_path / "other-instance")}
    instance.owners.write_text(json.dumps(other), encoding="utf-8")

    assert mate_config.crewmate_file_on_record("slack") is False
    assert json.loads(instance.owners.read_text(encoding="utf-8")) == other


def test_an_unrecorded_file_this_roster_does_not_list_stays_unowned(instance, monkeypatch):
    _roster(monkeypatch, scout=("scout", "scout"))

    assert mate_config.crewmate_file_on_record("slack") is False
    assert not instance.owners.exists()


def test_an_unreadable_config_leaves_the_file_unowned(instance, monkeypatch):
    def _unreadable(cls, *a, **kw):
        raise OSError("config.json unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_unreadable))

    assert mate_config.crewmate_file_on_record("slack") is False
    assert not instance.owners.exists()
