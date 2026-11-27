"""A packaged build prefers Kiro Crew's own KAS engine for an unset ``agent.kas_bundle``.

The decision reads the distribution ``scripts/stamp-distribution.sh`` bakes
(``beacon.baked_distribution``), so nightly, insider and stable behave alike and a
source checkout keeps the relay (:mod:`kiro_crew.acp.kas_build_default`). A saved
value always wins, and the loaded field is only ever the saved value. The lane
stamp (``_build_info.BUILD_CHANNEL``) is still baked by every build workflow and
read by :mod:`kiro_crew.build_channel`; the tests at the end pin that wiring.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kiro_crew import beacon, build_channel
from kiro_crew.acp import kas_build_default
from kiro_crew.config import loader_document

_REPO = Path(__file__).resolve().parents[1]
_STAMP_SCRIPT = _REPO / "scripts" / "stamp-distribution.sh"
_WORKFLOWS = _REPO / ".github" / "workflows"
_BUILD_JOBS = ("build-wheel", "build-desktop", "build-windows")

_NO_BASH = sys.platform == "win32" or shutil.which("bash") is None


@pytest.fixture
def stamp(monkeypatch):
    """Pin the baked lane; the module binding is the seam the reader reads."""

    def _pin(value: object) -> None:
        monkeypatch.setattr(build_channel, "_BAKED_BUILD_CHANNEL", value)

    return _pin


@pytest.fixture
def dist(monkeypatch):
    """Pin the baked distribution, on a host the engine can run on."""
    from kiro_crew.agent_sdk import kas_bundle as sdk_kas_bundle

    monkeypatch.setattr(sdk_kas_bundle, "host_unsupported_reason", lambda: "")
    monkeypatch.setattr(sdk_kas_bundle, "installed_bundle", lambda: None)

    def _pin(value: str) -> None:
        monkeypatch.setattr(beacon, "_BAKED_DISTRIBUTION", value)

    return _pin


class TestBakedBuildChannel:
    @pytest.mark.parametrize("value", ["stable", "insider", "nightly"])
    def test_a_known_lane_is_read_back(self, stamp, value):
        stamp(value)
        assert build_channel.baked_build_channel() == value

    @pytest.mark.parametrize("value", ["", "Nightly", "canary", None, 1])
    def test_anything_else_reads_as_no_lane(self, stamp, value):
        stamp(value)
        assert build_channel.baked_build_channel() == ""

    def test_the_lanes_match_the_release_channels(self):
        from kiro_crew.platform.update_layout import RELEASE_CHANNELS

        assert build_channel.KNOWN_BUILD_CHANNELS == frozenset(RELEASE_CHANNELS)


class TestAPackagedBuildPrefersTheEngine:
    @pytest.mark.parametrize("value", ["dmg", "appimage", "deb", "rpm", "nsis", "wheel", "docker"])
    def test_every_packaged_distribution_prefers_it(self, dist, value):
        dist(value)
        assert kas_build_default.kas_bundle_preferred_by_build() is True

    @pytest.mark.parametrize("value", ["source", ""])
    def test_a_source_checkout_does_not(self, dist, value):
        dist(value)
        assert kas_build_default.kas_bundle_preferred_by_build() is False

    @pytest.mark.parametrize("lane", ["nightly", "insider", "stable", ""])
    def test_the_lane_does_not_decide(self, dist, stamp, lane):
        """Insider and stable ship the same bytes, so the lane cannot be the switch."""
        dist("dmg")
        stamp(lane)
        assert kas_build_default.kas_bundle_preferred_by_build() is True

    def test_a_host_the_engine_cannot_run_on_does_not(self, dist, monkeypatch):
        from kiro_crew.agent_sdk import kas_bundle as sdk_kas_bundle

        dist("deb")
        monkeypatch.setattr(sdk_kas_bundle, "installed_bundle", lambda: None)
        monkeypatch.setattr(sdk_kas_bundle, "host_unsupported_reason", lambda: "glibc 2.26")
        assert kas_build_default.kas_bundle_preferred_by_build() is False

    def test_an_installed_engine_needs_no_host_probe(self, dist, monkeypatch):
        from kiro_crew.agent_sdk import kas_bundle as sdk_kas_bundle

        dist("deb")
        monkeypatch.setattr(sdk_kas_bundle, "installed_bundle", lambda: object())
        monkeypatch.setattr(
            sdk_kas_bundle, "host_unsupported_reason", lambda: pytest.fail("probed the host")
        )
        assert kas_build_default.kas_bundle_preferred_by_build() is True


class TestTheLoadedFieldIsOnlyTheSavedValue:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (loader_document._ABSENT_AGENT_KEY, False),
            (False, False),
            (True, True),
            ("true", False),
            (None, False),
        ],
    )
    def test_the_bundle_route_reads_only_a_saved_true(self, dist, monkeypatch, stored, expected):
        dist("dmg")
        monkeypatch.setattr(loader_document, "_stored_agent_value", lambda key: stored)
        assert loader_document.acp_backend_bundle_route() is expected

    @pytest.mark.parametrize(
        ("agent", "expected"),
        [({}, False), ({"kas_bundle": False}, False), ({"kas_bundle": True}, True)],
    )
    def test_load_resolution(self, dist, tmp_path, agent, expected):
        from kiro_crew.config import loader

        dist("dmg")
        doc = loader.ConfigDocument(
            ticket=0,
            path=tmp_path / "config.json",
            data={"agent": agent},
            loaded=True,
            content_digest=None,
        )
        assert loader.build_config(doc).agent.kas_bundle is expected

    def test_a_home_with_no_config_file_reads_false(self, dist, tmp_path):
        from kiro_crew.config import loader

        dist("dmg")
        doc = loader.ConfigDocument(
            ticket=0, path=tmp_path / "config.json", data={}, loaded=False, content_digest=None
        )
        assert loader.build_config(doc).agent.kas_bundle is False


class TestSaveKeepsAnUndeclaredKeyOffDisk:
    @pytest.mark.parametrize("declared", [True, False])
    def test_the_key_is_kept_only_when_declared(self, monkeypatch, declared):
        monkeypatch.setattr(
            loader_document,
            "_agent_key_declared",
            lambda key: declared if key == "kas_bundle" else True,
        )
        document = {"agent": {"kas_bundle": True, "sandbox_allow_unsandboxed_exec": False}}
        loader_document.strip_undeclared_resolved_defaults(document)
        assert ("kas_bundle" in document["agent"]) is declared
        assert "sandbox_allow_unsandboxed_exec" in document["agent"]

    def test_a_document_without_an_agent_object_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(loader_document, "_agent_key_declared", lambda key: False)
        document: dict = {"agent": "not an object"}
        loader_document.strip_undeclared_resolved_defaults(document)
        assert document == {"agent": "not an object"}


def _run_stamp(pkg_dir: Path, lane: str | None) -> "subprocess.CompletedProcess[str]":
    env = {k: v for k, v in os.environ.items() if k != "KC_BUILD_CHANNEL"}
    if lane is not None:
        env["KC_BUILD_CHANNEL"] = lane
    return subprocess.run(
        ["bash", str(_STAMP_SCRIPT), "wheel", str(pkg_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,  # flake-ok: hang guard for a local bash script, not a sleep or barrier
    )


@pytest.mark.skipif(_NO_BASH, reason="the stamping script only runs on build hosts with bash")
class TestStampScript:
    @pytest.mark.parametrize("lane", ["nightly", "insider", "stable", ""])
    def test_the_lane_is_baked(self, tmp_path, lane):
        proc = _run_stamp(tmp_path, lane)
        assert proc.returncode == 0, proc.stderr
        assert f'BUILD_CHANNEL = "{lane}"' in (tmp_path / "_build_info.py").read_text()

    def test_no_lane_in_the_environment_bakes_none(self, tmp_path):
        proc = _run_stamp(tmp_path, None)
        assert proc.returncode == 0, proc.stderr
        assert 'BUILD_CHANNEL = ""' in (tmp_path / "_build_info.py").read_text()

    def test_an_unknown_lane_fails_the_build(self, tmp_path):
        proc = _run_stamp(tmp_path, "canary")
        assert proc.returncode != 0
        assert not (tmp_path / "_build_info.py").exists()


def test_docker_restamp_preserves_and_checks_the_wheel_lane():
    dockerfile = (_REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
    restamp = dockerfile.split("# Re-stamp the provenance", 1)[1].split("\n\n", 1)[0]
    capture = (
        "CHANNEL=\"$(python -c 'from kiro_crew.build_channel import baked_build_channel; "
        "print(baked_build_channel())')\""
    )
    write = '"BUILD_CHANNEL = \\"${CHANNEL}\\""'
    check = "assert baked_build_channel() == sys.argv[1], baked_build_channel()"
    assert capture in restamp
    assert write in restamp
    assert check in restamp
    assert restamp.index(capture) < restamp.index(write) < restamp.index(check)
    assert restamp.rstrip().endswith('\' "${CHANNEL}"')


def _jobs(name: str) -> dict:
    return yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]


class TestOnlyTheNightlyPipelineStampsNightly:
    @pytest.mark.parametrize("job", _BUILD_JOBS)
    def test_nightly_passes_the_lane_to_every_build(self, job):
        assert _jobs("nightly.yml")[job]["with"]["build_channel"] == "nightly"

    @pytest.mark.parametrize("job", _BUILD_JOBS)
    def test_release_passes_no_nightly_lane(self, job):
        assert _jobs("release.yml")[job].get("with", {}).get("build_channel", "") != "nightly"

    @pytest.mark.parametrize("workflow", [f"{job}.yml" for job in _BUILD_JOBS])
    def test_each_build_workflow_exports_the_lane(self, workflow):
        data = yaml.safe_load((_WORKFLOWS / workflow).read_text(encoding="utf-8"))
        assert data["env"]["KC_BUILD_CHANNEL"] == "${{ inputs.build_channel }}"
        # PyYAML reads the bare `on:` key as True.
        triggers = data.get("on", data.get(True))
        for trigger in triggers.values():
            assert trigger["inputs"]["build_channel"]["default"] == ""
