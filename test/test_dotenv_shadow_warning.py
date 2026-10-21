"""A ``.env`` line that loses to the inherited environment is named in the log.

The environment wins over ``.env`` by design, so ``load_credentials`` keeps a value
the gateway inherited. The ``.env`` line then has no effect, and the log says so once,
naming the key and never a value.

Each test drives the real ``load_credentials`` over a real ``.env`` file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig

_LOGGER = "kiro_crew.config.loader"


def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a fresh boot: nothing warned, nothing seeded."""
    monkeypatch.setattr(loader, "_shadow_checked_env_keys", set(), raising=False)


def _setup(monkeypatch: pytest.MonkeyPatch, root: Path, lines: list[str]) -> None:
    """Write a ``.env`` holding *lines*; every name it seeds starts unset."""
    dotenv = root / ".env"
    dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _reset(monkeypatch)
    monkeypatch.setattr(loader, "env_path", lambda: dotenv)
    for key in loader.CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Set first so the undo removes a name ``load_credentials`` writes itself.
    for line in lines:
        name = line.partition("=")[0]
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)


def _load() -> None:
    KiroCrewConfig.__new__(KiroCrewConfig).load_credentials()


def _shadow_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "in .env is not applied to the process environment" in r.getMessage()
    ]


def test_a_dotenv_edit_after_the_first_load_is_not_reported_as_shadowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _setup(monkeypatch, tmp_path, ["KC_TEST_EDITED=old"])
    _load()
    assert os.environ["KC_TEST_EDITED"] == "old"
    # The operator edits .env while the gateway runs; the old value came from
    # .env itself, so no outside variable shadows the new line.
    (tmp_path / ".env").write_text("KC_TEST_EDITED=new\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _load()
    assert _shadow_lines(caplog) == []


def test_a_secret_reference_is_not_compared_with_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key = sorted(loader.CREDENTIAL_KEYS)[0]
    _setup(monkeypatch, tmp_path, [f"{key}=secret://{key}"])
    monkeypatch.setenv(key, "plaintext-in-env")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _load()
    assert _shadow_lines(caplog) == []


def test_a_key_the_environment_holds_with_another_value_is_named_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _setup(monkeypatch, tmp_path, ["KC_TEST_SHADOW=from-dotenv", "KC_TEST_FRESH=only-in-dotenv"])
    monkeypatch.setenv("KC_TEST_SHADOW", "from-shell")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _load()
        _load()
    # Precedence is unchanged: the environment keeps its value.
    assert os.environ["KC_TEST_SHADOW"] == "from-shell"
    assert os.environ["KC_TEST_FRESH"] == "only-in-dotenv"
    lines = _shadow_lines(caplog)
    assert len(lines) == 1
    assert "KC_TEST_SHADOW" in lines[0]
    assert "KC_TEST_FRESH" not in lines[0]
    # Either value may be a secret, so neither is logged.
    assert "from-shell" not in lines[0]
    assert "from-dotenv" not in lines[0]


def test_a_credential_key_shadowed_by_the_environment_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key = sorted(loader.CREDENTIAL_KEYS)[0]
    _setup(monkeypatch, tmp_path, [f"{key}=from-dotenv"])
    monkeypatch.setenv(key, "from-shell")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _load()
    assert os.environ[key] == "from-shell"
    lines = _shadow_lines(caplog)
    assert len(lines) == 1 and key in lines[0]


def test_an_empty_credential_in_the_environment_is_named_though_the_gateway_uses_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key = sorted(loader.CREDENTIAL_KEYS)[0]
    _setup(monkeypatch, tmp_path, [f"{key}=from-dotenv"])
    monkeypatch.setenv(key, "")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        creds = KiroCrewConfig.__new__(KiroCrewConfig).load_credentials()
    # The overlay skips an empty environment value, so the gateway itself uses the
    # .env credential, but children still inherit the empty one: that is reported.
    assert creds[key] == "from-dotenv"
    assert os.environ[key] == ""
    lines = _shadow_lines(caplog)
    assert len(lines) == 1 and key in lines[0]
    # The advice never says to drop the .env line the gateway relies on.
    assert "drop" not in lines[0]


def test_an_in_app_restart_reports_an_edit_the_old_seed_still_shadows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _setup(monkeypatch, tmp_path, ["KC_TEST_RESTART=old"])
    _load()
    (tmp_path / ".env").write_text("KC_TEST_RESTART=new\n", encoding="utf-8")
    # A re-exec keeps os.environ (with the old seed) but starts a fresh image,
    # so the once-per-boot memory is empty again.
    _reset(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _load()
    # The edit really is not applied, and the warning says why and what fixes it.
    assert os.environ["KC_TEST_RESTART"] == "old"
    lines = _shadow_lines(caplog)
    assert len(lines) == 1 and "KC_TEST_RESTART" in lines[0]
    assert "in-app restart" in lines[0]
    assert "stop and start the gateway" in lines[0]


@pytest.mark.parametrize(
    ("lines", "preset"),
    [
        # The environment already holds the same value: nothing is lost.
        (["KC_TEST_SAME=same"], {"KC_TEST_SAME": "same"}),
        # The key is not in the environment: the .env value applies.
        (["KC_TEST_ABSENT=v"], {}),
        # An empty .env value seeds nothing, so there is nothing to lose.
        (["KC_TEST_EMPTY="], {"KC_TEST_EMPTY": "from-shell"}),
    ],
)
def test_no_warning_when_the_dotenv_value_is_not_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    lines: list[str],
    preset: dict[str, str],
) -> None:
    _setup(monkeypatch, tmp_path, lines)
    for name, value in preset.items():
        monkeypatch.setenv(name, value)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _load()
    assert _shadow_lines(caplog) == []


def test_a_path_line_is_not_reported_as_shadowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    extra = str(tmp_path / "extra")
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"PATH={extra}\n", encoding="utf-8")
    _reset(monkeypatch)
    monkeypatch.setattr(loader, "env_path", lambda: dotenv)
    monkeypatch.setenv("PATH", str(tmp_path / "sys"))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _load()
    assert _shadow_lines(caplog) == []
    assert extra in os.environ["PATH"].split(os.pathsep)
