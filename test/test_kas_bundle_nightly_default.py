"""``agent.kas_bundle`` defaults on in a Nightly build, and only there.

The lane is a fact about the build's bytes: ``nightly.yml`` passes
``build_channel: nightly`` to every build workflow, ``scripts/stamp-distribution.sh``
bakes it into ``_build_info.BUILD_CHANNEL``, and :mod:`kiro_crew.build_channel`
reads it. An undeclared key resolves through
:func:`~kiro_crew.config.loader_document.kas_bundle_build_default`; a stored
value always wins, and ``save()`` keeps an undeclared key off disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kiro_crew import build_channel
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


class TestBakedBuildChannel:
    @pytest.mark.parametrize("value", ["stable", "insider", "nightly"])
    def test_a_known_lane_is_read_back(self, stamp, value):
        stamp(value)
        assert build_channel.baked_build_channel() == value

    @pytest.mark.parametrize("value", ["", "Nightly", "canary", None, 1])
    def test_anything_else_reads_as_no_lane(self, stamp, value):
        stamp(value)
        assert build_channel.baked_build_channel() == ""
        assert build_channel.is_nightly_build() is False

    def test_the_lanes_match_the_release_channels(self):
        from kiro_crew.platform.update_layout import RELEASE_CHANNELS

        assert build_channel.KNOWN_BUILD_CHANNELS == frozenset(RELEASE_CHANNELS)


class TestBuildDefault:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("nightly", True), ("insider", False), ("stable", False), ("", False)],
    )
    def test_only_a_nightly_build_defaults_on(self, stamp, value, expected):
        stamp(value)
        assert loader_document.kas_bundle_build_default() is expected


class TestBundleRouteReadsTheBuildDefaultWhenUndeclared:
    @pytest.mark.parametrize(
        ("lane", "stored", "expected"),
        [
            ("nightly", loader_document._ABSENT_AGENT_KEY, True),
            ("", loader_document._ABSENT_AGENT_KEY, False),
            ("nightly", False, False),
            ("", True, True),
            ("nightly", "true", True),
            ("", "true", False),
            ("nightly", None, True),
        ],
    )
    def test_stored_wins_absent_reads_the_build(self, stamp, monkeypatch, lane, stored, expected):
        stamp(lane)
        monkeypatch.setattr(loader_document, "_stored_agent_value", lambda key: stored)
        assert loader_document.acp_backend_bundle_route() is expected


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


class TestTheStoredValueWinsAtLoad:
    @pytest.mark.parametrize(
        ("lane", "agent", "expected"),
        [
            ("nightly", {}, True),
            ("nightly", {"kas_bundle": False}, False),
            ("stable", {}, False),
            ("stable", {"kas_bundle": True}, True),
            ("", {}, False),
        ],
    )
    def test_load_resolution(self, stamp, tmp_path, lane, agent, expected):
        from kiro_crew.config import loader

        stamp(lane)
        doc = loader.ConfigDocument(
            ticket=0,
            path=tmp_path / "config.json",
            data={"agent": agent},
            loaded=True,
            content_digest=None,
        )
        assert loader.build_config(doc).agent.kas_bundle is expected

    @pytest.mark.parametrize(("lane", "expected"), [("nightly", True), ("", False)])
    def test_a_home_with_no_config_file_takes_the_build_default(
        self, stamp, tmp_path, lane, expected
    ):
        from kiro_crew.config import loader

        stamp(lane)
        doc = loader.ConfigDocument(
            ticket=0, path=tmp_path / "config.json", data={}, loaded=False, content_digest=None
        )
        assert loader.build_config(doc).agent.kas_bundle is expected


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
