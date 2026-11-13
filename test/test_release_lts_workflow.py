"""The LTS edition's release legs (RFC crew-lts-edition D11, release-lts.yml).

An ``lts-vX.Y.Z`` tag builds, signs and publishes the LTS edition on its own
``lts`` channel through the same reusable workflows ``release.yml`` calls. These
tests pin the two halves of that promise: the LTS run reaches only lts names and
paths, and every ``v*`` release keeps calling the reusable workflows exactly as
before, so its oss output is unchanged.
"""

from __future__ import annotations

import configparser
import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# The workflow steps these tests run are bash on a Linux runner. On Windows,
# `bash` is often the WSL launcher, which gets neither these paths nor this env.
needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="runs a workflow step, which is bash on a Linux runner",
)

_BUILD_WORKFLOWS = ("build-wheel.yml", "build-desktop.yml", "build-windows.yml")
_EDITION_ENV = "${{ inputs.edition == 'lts' && 'lts' || '' }}"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare `on:` key as the boolean True (YAML 1.1).
    return doc[True] if True in doc else doc["on"]


def _calls(doc: dict) -> dict[str, dict]:
    return {
        name: job
        for name, job in doc["jobs"].items()
        if str(job.get("uses", "")).startswith("./.github/workflows/")
    }


def _version_script() -> str:
    steps = _workflow("release-lts.yml")["jobs"]["version"]["steps"]
    return next(step["run"] for step in steps if step.get("id") == "version")


class TestTriggers:
    def test_each_release_fires_only_on_its_own_tags(self) -> None:
        assert _triggers(_workflow("release.yml")) == {"push": {"tags": ["v*"]}}
        assert _triggers(_workflow("release-lts.yml")) == {"push": {"tags": ["lts-v*"]}}

    def test_both_releases_share_one_publish_queue(self) -> None:
        # Feed writes are last-writer-wins, so one release publishes at a time.
        expected = {"group": "release-publish", "cancel-in-progress": False}
        assert _workflow("release.yml")["concurrency"] == expected
        assert _workflow("release-lts.yml")["concurrency"] == expected


class TestOssReleaseIsUnchanged:
    def test_release_yml_never_asks_for_lts(self) -> None:
        # Every new input defaults to oss, so a caller that never names it builds
        # and publishes exactly what it did before.
        for name, job in _calls(_workflow("release.yml")).items():
            with_ = job.get("with") or {}
            assert "edition" not in with_, f"release.yml {name} must not pass an edition"
            assert with_.get("channel") != "lts", f"release.yml {name} must not publish lts"
        assert "lts" not in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")

    @pytest.mark.parametrize("name", _BUILD_WORKFLOWS)
    def test_the_edition_input_defaults_to_oss(self, name: str) -> None:
        doc = _workflow(name)
        edition = _triggers(doc)["workflow_call"]["inputs"]["edition"]
        assert edition["default"] == "oss" and edition["required"] is False
        # Empty unless lts: setup.py and build-desktop.sh read empty as oss.
        assert doc["env"]["KIROCREW_BUILD_EDITION"] == _EDITION_ENV

    def test_every_lts_only_name_is_gated_on_the_lts_channel(self) -> None:
        linux = _workflow("publish-linux.yml")["jobs"]["publish-linux"]["env"]
        assert linux["PUBLISHED_STEM"] == (
            "${{ inputs.channel == 'lts' && 'KiroCrew-LTS' || 'KiroCrew' }}"  # brand-ok
        )
        windows = _workflow("publish-windows.yml")["jobs"]["publish-windows"]["env"]
        assert windows["PUBLISHED_BASENAME"] == (
            "${{ inputs.channel == 'lts' && 'KiroCrew-LTS-Setup' || 'KiroCrew-Setup' }}"  # brand-ok
        )
        mac = _workflow("sign-and-notarize.yml")["jobs"]
        for job in ("notarize", "publish"):
            assert mac[job]["env"]["ARTIFACT_BASENAME"] == (
                "${{ format('KiroCrew{0}{1}', inputs.channel == 'lts' && '-LTS' || '', "  # brand-ok
                "inputs.mac_variant != '' && format('-{0}', inputs.mac_variant) || '') }}"
            )
        wheel = _workflow("build-wheel.yml")["jobs"]["build-wheel"]["steps"]
        rename = next(step for step in wheel if step.get("name") == "Name the LTS distribution")
        assert rename["if"] == "inputs.edition == 'lts'"

    @needs_bash
    def test_the_rename_step_leaves_no_extra_naming_the_oss_distribution(
        self, tmp_path: Path
    ) -> None:
        # The voice extra names its own distribution; left alone,
        # kirocrew-lts[voice] would install the oss kirocrew beside it.
        wheel = _workflow("build-wheel.yml")["jobs"]["build-wheel"]["steps"]
        rename = next(step for step in wheel if step.get("name") == "Name the LTS distribution")
        for name in ("pyproject.toml", "setup.cfg"):
            (tmp_path / name).write_text(
                (ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        subprocess.run(["bash", "-eo", "pipefail", "-c", rename["run"]], cwd=tmp_path, check=True)
        assert 'name = "kirocrew-lts"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        cfg = (tmp_path / "setup.cfg").read_text(encoding="utf-8")
        assert "    kirocrew-lts[voice-aws]" in cfg
        assert not re.search(r"^\s+kirocrew\[", cfg, re.MULTILINE)


class TestLtsReleaseLegs:
    def test_every_leg_publishes_on_lts_with_the_lts_edition(self) -> None:
        calls = _calls(_workflow("release-lts.yml"))
        built = {
            name for name, job in calls.items() if job["uses"].split("/")[-1] in _BUILD_WORKFLOWS
        }
        assert built == {"build-wheel", "build-desktop", "build-windows"}
        for name in built:
            assert calls[name]["with"]["edition"] == "lts", name
        published = {name for name, job in calls.items() if "channel" in (job.get("with") or {})}
        assert len(published) == 11  # cli, six linux, windows, three mac legs
        for name in published:
            assert calls[name]["with"]["channel"] == "lts", name
            assert not (calls[name]["with"] or {}).get("promote"), name

    def test_the_install_smokes_expect_the_signed_out_lts_refusal(self) -> None:
        # The smokes have no sign-in, so for LTS they assert the refusal. Its text
        # must be the line lts_boot.refuse_boot logs, or every LTS smoke fails.
        source = (ROOT / "src" / "kiro_crew" / "lts_boot.py").read_text(encoding="utf-8")
        assert 'logger.critical("LTS boot refused: %s. %s"' in source
        mac = (ROOT / "scripts" / "smoke-macos-install.sh").read_text(encoding="utf-8")
        win = (ROOT / "scripts" / "smoke-windows-install.ps1").read_text(encoding="utf-8")
        assert 'LTS_REFUSAL="LTS boot refused"' in mac
        assert '[ "${KIROCREW_BUILD_EDITION:-}" = lts ]' in mac
        assert '$ltsRefusal = "LTS boot refused"' in win
        assert '$isLts = $env:KIROCREW_BUILD_EDITION -eq "lts"' in win
        # The Windows gateway starts as the desktop app starts it, so a signed-out
        # LTS build refuses instead of serving the sign-in-only screen.
        assert 'if ($isLts) { $gatewayEnv["KIROCREW_DESKTOP_GATEWAY"] = "1" }' in win
        for name in ("build-desktop.yml", "build-windows.yml"):
            assert _workflow(name)["env"]["KIROCREW_BUILD_EDITION"] == _EDITION_ENV, name

    def test_the_candidate_tests_run_the_remote_ceiling_proof(self) -> None:
        # The tagged commit must pass the end-to-end proof that no local tier widens
        # the organisation's remote policy. The step runs the whole suite split by
        # file, so the module runs as long as its shard is in the matrix and nothing
        # filters it out.
        from scripts.ci_file_shards import hash_shard

        module = "test/test_lts_remote_ceiling_outranks_local.py"
        assert (ROOT / module).is_file()
        cfg = configparser.ConfigParser()
        cfg.read(ROOT / "setup.cfg", encoding="utf-8")
        assert "test" in cfg["tool:pytest"]["testpaths"].split()
        job = _workflow("release-lts.yml")["jobs"]["release-candidate-tests"]
        count = int(job["env"]["SHARD_COUNT"])
        assert job["strategy"]["matrix"]["group"] == list(range(1, count + 1))
        run = next(s["run"] for s in job["steps"] if s.get("name") == "Run release candidate tests")
        args = run[run.index("python -m pytest") + len("python -m pytest") :].split()
        assert "--file-shards" in args and '"$SHARD_COUNT"' in args
        for narrowing in ("-k", "-m", "--deselect", "--ignore", "--file-shard-", "--lf", "test"):
            assert not any(a.startswith(narrowing) for a in args), narrowing
        assert 1 <= hash_shard(module, count) <= count

    def test_no_docker_image_and_no_github_release(self) -> None:
        text = (WORKFLOWS / "release-lts.yml").read_text(encoding="utf-8")
        assert "publish-docker.yml" not in text
        assert "github-release" not in _workflow("release-lts.yml")["jobs"]

    def test_nothing_publishes_before_the_tagged_commit_passes_the_tests(self) -> None:
        jobs = _workflow("release-lts.yml")["jobs"]
        for name in ("build-wheel", "build-desktop", "build-windows"):
            assert "release-candidate-tests" in jobs[name]["needs"], name
        assert "if" not in jobs["release-candidate-tests"]

    @needs_bash
    @pytest.mark.parametrize(
        ("tag", "version"),
        [("lts-v0.13.0", "0.13.0"), ("lts-v1.2.10", "1.2.10")],
    )
    def test_a_tag_maps_to_its_bare_version(self, tag: str, version: str) -> None:
        out = _run_version(tag)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == f"version={version}"

    @needs_bash
    @pytest.mark.parametrize(
        "tag", ["lts-v0.13", "lts-v0.13.0-rc.1", "lts-v0.13.0.1", "v0.13.0", "lts-vx.y.z"]
    )
    def test_any_other_tag_shape_is_refused(self, tag: str) -> None:
        assert _run_version(tag).returncode != 0


def _run_version(tag: str) -> subprocess.CompletedProcess[str]:
    script = _version_script().replace(
        '>> "$GITHUB_OUTPUT"', ""
    )  # print the output line instead of writing the runner file
    script = re.sub(r'^\s*echo "Releasing LTS.*$', "", script, flags=re.MULTILINE)
    return subprocess.run(
        ["bash", "-c", script],
        env={"TAG": tag, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class TestLtsArtifactNames:
    def _manifest_module(self):
        spec = importlib.util.spec_from_file_location(
            "cli_manifest_for_test", ROOT / "packaging" / "signing" / "cli-manifest.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_lts_publishes_its_own_wheel_name(self) -> None:
        from kiro_crew.platform.wheel_engine import wheel_filename

        manifest = self._manifest_module()
        for helper in (wheel_filename, manifest.wheel_filename):
            assert helper("lts", "0.13.0") == "kirocrew_lts-0.13.0-py3-none-any.whl"
            assert helper("stable", "0.13.0") == "kirocrew-0.13.0-py3-none-any.whl"
        assert "lts" in manifest.CHANNELS

    @needs_bash
    def test_publish_cli_pairs_the_wheel_stem_with_the_channel(self) -> None:
        steps = _workflow("publish-cli.yml")["jobs"]["publish-cli"]["steps"]
        run = next(step["run"] for step in steps if "STEM=kirocrew_lts" in str(step.get("run")))
        # The stem choice and the name check, exactly as the step runs them.
        match = re.search(
            r"^ *if \[ \"\$CHANNEL\" = lts \]; then STEM=.*?\n *fi\n", run, re.MULTILINE | re.DOTALL
        )
        assert match, "publish-cli.yml pins the wheel stem to the channel"
        check = match.group(0)
        cases = [
            ("lts", "kirocrew_lts-0.13.0-py3-none-any.whl", 0),
            ("lts", "kirocrew-0.13.0-py3-none-any.whl", 1),
            ("stable", "kirocrew-0.13.0-py3-none-any.whl", 0),
            ("stable", "kirocrew_lts-0.13.0-py3-none-any.whl", 1),
        ]
        for channel, wheel, code in cases:
            out = subprocess.run(
                ["bash", "-c", check],
                env={"CHANNEL": channel, "WHEEL_NAME": wheel, "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            assert out.returncode == code, (channel, wheel, out.stdout, out.stderr)

    def test_the_lts_build_reports_its_own_distribution(self, monkeypatch) -> None:
        from kiro_crew import release_channel

        installed: set[str] = {"kirocrew", "kirocrew-lts"}

        def distribution(name: str) -> object:
            if name not in installed:
                raise importlib.metadata.PackageNotFoundError(name)
            return object()

        monkeypatch.setattr(importlib.metadata, "distribution", distribution)
        monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        assert release_channel.distribution_name() == "kirocrew-lts"
        # The desktop backend installs the unrenamed source tree as kirocrew.
        installed.discard("kirocrew-lts")
        assert release_channel.distribution_name() == "kirocrew"
        monkeypatch.setattr("kiro_crew.edition.EDITION", "oss")
        installed.add("kirocrew-lts")
        assert release_channel.distribution_name() == "kirocrew"

    def test_an_oss_tag_never_holds_the_lts_feed(self, tmp_path) -> None:
        tags = tmp_path / "tags.txt"
        tags.write_text("v9.9.9\nv0.14.0\n", encoding="utf-8")
        out = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check_feed_advance.py"),
                "--new",
                "0.13.0",
                "--channel",
                "lts",
                "--tags-file",
                str(tags),
                "--self",
                "lts-v0.13.0",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert out.stdout.strip() == "advance", out.stderr
