"""A ``--test-mode`` gateway on its own data home gets its own Kiro home (``_isolate_test_mode_kiro_home``).

Without it, ``KIROCREW_HOME`` moved only the data home, and the Bots such a gateway
created were written into the real ``~/.kiro/mates/agents`` and its owner record.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from conftest import forget_env_at_teardown
from kiro_crew import cli, cli_gateway_boot, seed
from kiro_crew.config import paths as paths_mod


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A clean environment with both main gateway homes under *tmp_path*."""
    # The code under test writes KIRO_HOME itself; a bare delenv records no undo.
    forget_env_at_teardown(monkeypatch, "KIRO_HOME")
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    main = tmp_path / "main-home"
    monkeypatch.setattr(seed, "_default_home", lambda: main)
    monkeypatch.setattr(seed, "_legacy_home", lambda: tmp_path / "legacy-home")
    return monkeypatch, tmp_path, main


def _args(test_mode: bool) -> argparse.Namespace:
    return argparse.Namespace(command="gateway", test_mode=test_mode)


def test_a_test_mode_gateway_on_its_own_data_home_gets_a_kiro_home_under_it(env):
    monkeypatch, tmp_path, _main = env
    data_home = tmp_path / "throwaway"
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))

    kiro_home = cli._isolate_test_mode_kiro_home(_args(True))

    assert kiro_home == data_home.resolve() / "kiro"
    assert os.environ["KIRO_HOME"] == str(kiro_home)
    assert paths_mod.kiro_home() == kiro_home
    assert paths_mod.mate_agents_dir().is_relative_to(kiro_home)


def test_a_plain_gateway_on_its_own_data_home_keeps_the_shared_kiro_home(env):
    monkeypatch, tmp_path, _main = env
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "second-instance"))

    assert cli._isolate_test_mode_kiro_home(_args(False)) is None
    assert "KIRO_HOME" not in os.environ


def test_a_kiro_home_the_caller_set_is_kept(env):
    monkeypatch, tmp_path, _main = env
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "throwaway"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "chosen"))

    assert cli._isolate_test_mode_kiro_home(_args(True)) is None
    assert os.environ["KIRO_HOME"] == str(tmp_path / "chosen")


@pytest.mark.parametrize("data_home", ["unset", "main"])
def test_a_test_mode_gateway_on_no_or_the_main_data_home_keeps_the_kiro_home(env, data_home):
    monkeypatch, _tmp_path, main = env
    if data_home == "main":
        monkeypatch.setenv("KIROCREW_HOME", str(main))

    assert cli._isolate_test_mode_kiro_home(_args(True)) is None
    assert "KIRO_HOME" not in os.environ


def test_a_test_mode_gateway_on_an_invalid_data_home_keeps_the_kiro_home(env):
    monkeypatch, _tmp_path, _main = env
    monkeypatch.setenv("KIROCREW_HOME", "/")

    assert cli._isolate_test_mode_kiro_home(_args(True)) is None
    assert "KIRO_HOME" not in os.environ


def test_the_facade_and_the_owner_hold_one_object():
    assert cli._isolate_test_mode_kiro_home is cli_gateway_boot._isolate_test_mode_kiro_home


def test_main_isolates_the_kiro_home_before_the_platform_boot():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    isolate = source.index("_isolate_test_mode_kiro_home(args)")
    assert isolate < source.index("# ── Platform context boot")
