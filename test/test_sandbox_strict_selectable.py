"""``agent.sandbox="strict"`` is a tier the config layer must let through.

``sandbox.py`` has carried a ``strict`` tier for as long as it has had tiers:
``_STRICT_DIRS`` masks ``~/.aws`` (and ``~/.ssh`` bar ``known_hosts``) where the
default ``standard`` tier deliberately leaves them visible, ``_mode_to_level``
maps the spelling to itself, and the product's own remedies name it
(``tool_gate``: "set agent.sandbox to 'standard' or 'strict'"; ``kirocrew
doctor`` on macOS: ``agent.sandbox="strict"``). The config layer did not admit
it: the ``agent.sandbox`` enum listed ``auto`` and ``off`` only, so
``kirocrew config set agent.sandbox strict`` was refused and a hand-written
``"strict"`` in ``config.json`` was degraded back to ``auto`` at load with an
enum-violation warning. An operator who wanted the tier the docs promised had
no path to it.

These tests pin the opt-in and, just as deliberately, pin that nothing else
moved: the shipped default is still ``auto``, ``off`` still loads as ``off``,
and an unrecognised spelling still degrades to the default exactly as before.
"""

from __future__ import annotations

import json
import logging
import sys
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.schema import SCHEMA_REGISTRY


def _load(tmp_path: Path, agent: dict | None) -> KiroCrewConfig:
    """Load a config whose ``agent`` section is *agent* (``None`` = key absent).

    Both config paths are patched so a developer's own ``config.local.json``
    cannot leak into the assertion.
    """
    cfg_file = tmp_path / "config.json"
    local_file = tmp_path / "config.local.json"
    if agent is not None:
        cfg_file.write_text(json.dumps({"agent": agent}), encoding="utf-8")
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
        unittest.mock.patch("kiro_crew.config.loader.config_local_path", return_value=local_file),
    ):
        return KiroCrewConfig.load()


def _declared_sandbox_enum() -> list[str]:
    return {entry.path: entry.enum_values for entry in SCHEMA_REGISTRY}["agent.sandbox"]


class TestStrictIsSelectable:
    def test_the_enum_admits_strict(self) -> None:
        assert "strict" in _declared_sandbox_enum()

    def test_config_set_accepts_strict(self) -> None:
        """``kirocrew config set agent.sandbox strict`` reaches the file.

        ``_declared_enum_value`` is the CLI write gate; a value it raises on is
        never written.
        """
        from kiro_crew.cli_config import _declared_enum_value

        assert _declared_enum_value("agent.sandbox", "strict") == "strict"
        # Spelling is the one leniency the gate grants every enum key.
        assert _declared_enum_value("agent.sandbox", "STRICT") == "strict"

    def test_the_loader_keeps_a_hand_written_strict(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config"):
            cfg = _load(tmp_path, {"sandbox": "strict"})
        assert cfg.agent.sandbox == "strict"
        assert "enum violation at 'agent.sandbox'" not in caplog.text

    def test_the_dashboard_schema_offers_the_same_tiers_as_the_config_schema(self) -> None:
        """Settings and the CLI must never disagree about which tiers exist.

        ``dashboard/handlers/core.py`` hand-maintains its own ``_EDITABLE_CONFIG``
        enum for ``agent.sandbox``; a tier admitted on one surface and refused on
        the other is the drift this pins against.
        """
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert _EDITABLE_CONFIG["agent.sandbox"]["values"] == _declared_sandbox_enum()


class TestSelectingStrictActuallyTightens:
    """The enum entry is only worth admitting if the tier behind it exists."""

    def test_strict_resolves_to_its_own_tier(self) -> None:
        assert sandbox._mode_to_level("strict") == "strict"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="the Linux namespace launcher reads os.getuid; Windows has no such tier",
    )
    def test_the_linux_launcher_masks_aws_under_strict_and_not_under_standard(
        self, monkeypatch
    ) -> None:
        # The builder asks the host's ``ssh -V`` for the accept-new flag; not what
        # this asserts, so the probe is pinned (same as test_cpp_wiring_enterprise).
        monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
        aws = json.dumps(str(Path(Path.home(), ".aws")))

        def _sensitive_dirs(script: str) -> str:
            lines = [ln for ln in script.splitlines() if ln.startswith("SENSITIVE_DIRS = ")]
            assert len(lines) == 1, "the launcher declares its mask list exactly once"
            return lines[0]

        assert aws in _sensitive_dirs(sandbox._build_launcher_script("strict"))
        assert aws not in _sensitive_dirs(sandbox._build_launcher_script("standard"))

    def test_the_seatbelt_profile_denies_aws_reads_under_strict_and_not_under_standard(
        self,
    ) -> None:
        aws = str(Path(Path.home(), ".aws"))
        strict_profile = sandbox._build_seatbelt_profile("strict")
        standard_profile = sandbox._build_seatbelt_profile("standard")
        assert f'(deny file-read* (subpath "{aws}"))' in strict_profile
        assert aws not in standard_profile


class TestNothingElseMoved:
    """Every operator who never wrote ``strict`` sees exactly what they saw before."""

    def test_the_shipped_default_is_still_auto(self, tmp_path: Path) -> None:
        assert _load(tmp_path, None).agent.sandbox == "auto"
        assert _load(tmp_path, {}).agent.sandbox == "auto"

    def test_auto_still_resolves_to_the_standard_tier(self) -> None:
        assert sandbox._mode_to_level("auto") == "standard"

    def test_off_still_loads_as_off(self, tmp_path: Path) -> None:
        assert _load(tmp_path, {"sandbox": "off"}).agent.sandbox == "off"

    def test_an_unknown_spelling_still_degrades_to_the_default(
        self, tmp_path: Path, caplog
    ) -> None:
        pytest.importorskip("jsonschema")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config"):
            cfg = _load(tmp_path, {"sandbox": "paranoid"})
        assert cfg.agent.sandbox == "auto"
        assert "enum violation at 'agent.sandbox'" in caplog.text

    def test_config_set_still_refuses_an_unknown_spelling(self) -> None:
        from kiro_crew.cli_config import _declared_enum_value

        with pytest.raises(ValueError):
            _declared_enum_value("agent.sandbox", "paranoid")

    def test_the_enum_grew_by_exactly_strict(self) -> None:
        """Widening is opt-in and one tier wide: no alias, no ``cc``, no rename."""
        assert _declared_sandbox_enum() == ["auto", "strict", "off"]
