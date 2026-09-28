"""The residual ``aws`` probe/spawn sites route through the shared resolver.

The deploy engine's ``resolve_aws_bin()`` is the repo's single ``aws``-CLI
resolution chokepoint. A probe that asks ``shutil.which("aws")`` with the bare
name disagrees with the resolved spawn sites under a GUI-launched gateway's
minimal PATH: the probe answers "not installed" while the resolved spawn
succeeds, or the reverse. These tests pin each converted site in the
feature-demo-recording reference scripts to the resolver; the cloud doctor's
converted probe is pinned beside its siblings in ``test_cloud_cli.py``.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from skill_script_helpers import load_skill_script

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REFS = (
    _REPO_ROOT / "src/kiro_crew/apps/builtins/dev_fleet/skills/feature-demo-recording/references"
)

_RESOLVED = "/opt/aws-cli/aws"


def _recording_which(probed: list[str]):
    """A ``shutil.which`` stand-in that recognises only the resolved path."""

    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        probed.append(name)
        return name if name == _RESOLVED else None

    return fake_which


@pytest.fixture
def narrate_mod():
    """Load narrate.py, undoing its module-level ``sys.path`` insert and the
    ``_pathcheck`` registration so neither leaks into later tests."""
    path_before = list(sys.path)
    had_pathcheck = "_pathcheck" in sys.modules
    try:
        yield load_skill_script("kc_video_narrate_aws_probe", _REFS / "narrate.py")
    finally:
        sys.path[:] = path_before
        if not had_pathcheck:
            sys.modules.pop("_pathcheck", None)


class TestDepsSpeechProbe:
    def test_check_speech_probes_resolved_binary(self, monkeypatch):
        deps = load_skill_script("kc_video_deps_aws_probe", _REFS / "deps.py")
        monkeypatch.delenv("KC_VIDEO_PIPER_MODEL", raising=False)
        monkeypatch.setattr(deps, "resolve_aws_bin", lambda: _RESOLVED)
        probed: list[str] = []
        monkeypatch.setattr(shutil, "which", _recording_which(probed))

        result = deps.check_speech()

        assert result["ok"]
        assert "polly" in result["detail"]
        assert _RESOLVED in probed
        assert "aws" not in probed  # the bare-name probe is the defect

    def test_resolver_degrades_to_bare_name_without_kiro_crew(self, monkeypatch):
        # A None entry makes ``from kiro_crew.deploy.engine import ...`` raise
        # ImportError, which is the standalone-interpreter environment the
        # doctor must keep diagnosing instead of crashing on import.
        monkeypatch.setitem(sys.modules, "kiro_crew.deploy.engine", None)
        deps = load_skill_script("kc_video_deps_aws_fallback", _REFS / "deps.py")
        assert deps.resolve_aws_bin() == "aws"

        # And the degraded probe is a WORKING bare-name PATH lookup: the
        # doctor's speech check stays green when plain "aws" is on PATH.
        monkeypatch.delenv("KC_VIDEO_PIPER_MODEL", raising=False)
        probed: list[str] = []

        def bare_which(name: str, *args: object, **kwargs: object) -> str | None:
            probed.append(name)
            return "/usr/bin/aws" if name == "aws" else None

        monkeypatch.setattr(shutil, "which", bare_which)
        result = deps.check_speech()
        assert result["ok"]
        assert "polly" in result["detail"]
        assert "aws" in probed


class TestNarrateAwsResolution:
    def test_resolve_provider_probes_resolved_binary(self, monkeypatch, narrate_mod):
        monkeypatch.setattr(narrate_mod, "resolve_aws_bin", lambda: _RESOLVED)
        probed: list[str] = []
        monkeypatch.setattr(shutil, "which", _recording_which(probed))

        assert narrate_mod.resolve_provider("auto", "", "") == "polly"
        assert _RESOLVED in probed
        assert "aws" not in probed

    def test_synthesize_polly_argv_head_is_resolved(self, monkeypatch, tmp_path, narrate_mod):
        monkeypatch.setattr(narrate_mod, "resolve_aws_bin", lambda: _RESOLVED)
        # safe_output_path confines outputs to the CWD, so stand inside tmp_path.
        monkeypatch.chdir(tmp_path)
        captured: dict[str, list[str]] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            # The CLI writes the staged output itself; emulate the success shape
            # so _staged_output publishes and synthesize returns normally.
            pathlib.Path(argv[-1]).write_bytes(b"mp3")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        out = narrate_mod.synthesize(
            "polly",
            "hello world",
            tmp_path / "line00",
            voice="Matthew",
            piper_binary="",
            piper_model="",
            aws_profile="",
            aws_region="",
        )

        assert out.suffix == ".mp3"
        assert captured["argv"][0] == _RESOLVED
        assert captured["argv"][1:3] == ["polly", "synthesize-speech"]
