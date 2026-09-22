"""A packaged install's cloud launch pins its own release tag on the clone path.

``kirocrew-ec2.yaml`` defaults ``KirocrewRef`` to ``main``. A checkout ships its
own source, so the default is inert for it; a packaged install has no checkout,
takes the public-repo clone, and with no ref at the call site the instance
installs ``main`` while this machine runs a release —
``remote_relay.ensure_version_parity`` then refuses every session between the
two on ``major.minor``. The fix lives in ``ec2.deploy`` because BOTH callers
that can reach the clone path (``launch_engine.RealLaunchEngine.provision`` for
the dashboard, ``wizard._deploy_with_progress`` for the CLI) pass no ref, and a
seam fix covers a caller that does not exist yet.

The tag probe is a ``git ls-remote`` round trip: never run here, always mocked.
"""

from __future__ import annotations

import subprocess

import pytest

import kiro_crew.cloud.source as source_mod
from kiro_crew.cloud import aws, ec2, launch_engine, sizes

_BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/kirocrew-instance-boundary"


def _template_default(param: str) -> str:
    text = (ec2.Path(ec2.__file__).parent / "templates" / "kirocrew-ec2.yaml").read_text(
        encoding="utf-8"
    )
    block = text.split(f"  {param}:\n", 1)[1]
    for line in block.splitlines():
        if line.strip().startswith("Default:"):
            return line.split("Default:", 1)[1].strip()
    raise AssertionError(f"{param} has no Default in the template")


def _real_deploy(monkeypatch, captured: dict) -> None:
    """Mock every AWS call ``deploy`` makes so the argv it builds can be read."""
    monkeypatch.setattr(source_mod, "find_repo_root", lambda: None)
    monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
    monkeypatch.setattr(source_mod, "ensure_instance_boundary", lambda *a, **k: _BOUNDARY_ARN)
    monkeypatch.setattr(
        source_mod, "upload_source", lambda *a, **k: pytest.fail("no checkout: nothing to upload")
    )
    monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1", "igw"))
    monkeypatch.setattr(ec2, "assert_download_hosts_resolvable", lambda *a, **k: None)

    def fake_run(argv, profile="", region="", *, timeout=ec2._DEPLOY_TIMEOUT, proc_sink=None):
        captured["argv"] = argv
        return (0, "ok", "")

    monkeypatch.setattr(aws, "run_aws", fake_run)
    monkeypatch.setattr(
        ec2, "describe", lambda *a, **k: {"instance_id": "i-1", "stack_status": "CREATE_COMPLETE"}
    )


def _overrides(argv: list[str]) -> dict[str, str]:
    """The ``Key=Value`` parameter overrides of a ``cloudformation deploy`` argv."""
    return dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("-"))


class TestPublicRepoUrl:
    def test_probe_asks_the_remote_the_template_clones(self) -> None:
        """A tag confirmed on one remote and cloned from another proves nothing."""
        assert ec2.PUBLIC_REPO_URL == _template_default("KirocrewRepo")

    def test_template_still_defaults_to_main(self) -> None:
        """The fallback the WARNING promises ("the instance will run main")."""
        assert _template_default("KirocrewRef") == "main"


class TestReleaseTagExists:
    def test_asks_git_for_exactly_that_tag_without_prompting(self, monkeypatch) -> None:
        seen: dict = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            return subprocess.CompletedProcess(argv, 0, "abc\trefs/tags/v0.7.0\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert ec2.release_tag_exists("v0.7.0") is True
        assert seen["argv"] == [
            "git",
            "ls-remote",
            "--exit-code",
            "--tags",
            "--",
            ec2.PUBLIC_REPO_URL,
            "refs/tags/v0.7.0",
        ]
        assert seen["kw"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["kw"]["stdin"] is subprocess.DEVNULL
        assert seen["kw"]["timeout"] == ec2._REF_PROBE_TIMEOUT_SECONDS

    def test_explicit_repo_is_the_remote_probed(self, monkeypatch) -> None:
        seen: dict = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert ec2.release_tag_exists("v0.7.0", "https://example.com/fork.git") is True
        assert seen["argv"][-2] == "https://example.com/fork.git"

    @pytest.mark.parametrize("returncode", [2, 128])
    def test_non_zero_exit_is_a_miss(self, monkeypatch, returncode: int, caplog) -> None:
        """2 = no such ref; 128 = the remote could not be reached. Both: no tag."""
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, returncode, "", "fatal"),
        )
        with caplog.at_level("WARNING", logger="kiro_crew.cloud.ec2"):
            assert ec2.release_tag_exists("v0.7.0") is False
        assert "v0.7.0" in caplog.text

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("git"),
            subprocess.TimeoutExpired(["git"], 15.0),
            PermissionError("git"),
        ],
    )
    def test_no_git_no_network_no_time_is_a_miss(self, monkeypatch, exc, caplog) -> None:
        def raiser(argv, **kw):
            raise exc

        monkeypatch.setattr(subprocess, "run", raiser)
        with caplog.at_level("WARNING", logger="kiro_crew.cloud.ec2"):
            assert ec2.release_tag_exists("v0.7.0") is False
        assert "could not probe" in caplog.text


class TestResolvePublicRef:
    def test_hit_returns_this_builds_tag(self, monkeypatch) -> None:
        monkeypatch.setattr(ec2, "__version__", "0.7.0.5")
        monkeypatch.setattr(ec2.release_channel, "__version__", "0.7.0.5")
        probed: list = []

        def fake_exists(ref, repo=""):
            probed.append((ref, repo))
            return True

        monkeypatch.setattr(ec2, "release_tag_exists", fake_exists)
        assert ec2.resolve_public_ref() == "v0.7.0"
        assert probed == [("v0.7.0", "")]

    def test_miss_is_empty_and_says_main(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(ec2, "__version__", "0.7.0")
        monkeypatch.setattr(ec2.release_channel, "__version__", "0.7.0")
        monkeypatch.setattr(ec2, "release_tag_exists", lambda ref, repo="": False)
        with caplog.at_level("WARNING", logger="kiro_crew.cloud.ec2"):
            assert ec2.resolve_public_ref() == ""
        assert "v0.7.0" in caplog.text and "will run main" in caplog.text

    def test_nightly_never_probes(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(ec2, "__version__", "0.8.0.dev20260922")
        monkeypatch.setattr(ec2.release_channel, "__version__", "0.8.0.dev20260922")
        monkeypatch.setattr(
            ec2, "release_tag_exists", lambda *a, **k: pytest.fail("nightly has no tag to probe")
        )
        with caplog.at_level("WARNING", logger="kiro_crew.cloud.ec2"):
            assert ec2.resolve_public_ref() == ""
        assert "0.8.0.dev20260922" in caplog.text and "will run main" in caplog.text

    def test_probe_targets_the_callers_repo(self, monkeypatch) -> None:
        monkeypatch.setattr(ec2.release_channel, "__version__", "0.7.0")
        probed: list = []
        monkeypatch.setattr(
            ec2, "release_tag_exists", lambda ref, repo="": probed.append(repo) or True
        )
        ec2.resolve_public_ref("https://example.com/fork.git")
        assert probed == ["https://example.com/fork.git"]


class TestDeployPinsTheRef:
    """The seam every launcher passes through, on the real (non-dry) path."""

    def test_public_clone_pins_the_release_tag(self, monkeypatch) -> None:
        captured: dict = {}
        _real_deploy(monkeypatch, captured)
        monkeypatch.setattr(ec2, "resolve_public_ref", lambda repo="": "v0.7.0")

        ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")

        overrides = _overrides(captured["argv"])
        assert overrides["KirocrewRef"] == "v0.7.0"
        assert "SourceBucket" not in overrides

    def test_probe_miss_keeps_the_template_default(self, monkeypatch) -> None:
        """A parity refusal must never become a boot failure: no ref, not a bad one."""
        captured: dict = {}
        _real_deploy(monkeypatch, captured)
        monkeypatch.setattr(ec2, "resolve_public_ref", lambda repo="": "")

        ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")

        assert "KirocrewRef" not in _overrides(captured["argv"])

    def test_explicit_ref_wins_over_the_release_tag(self, monkeypatch) -> None:
        captured: dict = {}
        _real_deploy(monkeypatch, captured)
        monkeypatch.setattr(
            ec2, "resolve_public_ref", lambda repo="": pytest.fail("explicit ref: no probe")
        )

        ec2.deploy(
            tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1", ref="my-branch"
        )

        assert _overrides(captured["argv"])["KirocrewRef"] == "my-branch"

    def test_checkout_ships_source_and_never_probes(self, monkeypatch) -> None:
        captured: dict = {}
        _real_deploy(monkeypatch, captured)
        monkeypatch.setattr(source_mod, "find_repo_root", lambda: object())
        monkeypatch.setattr(source_mod, "upload_source", lambda *a, **k: ("bucket", "key"))
        monkeypatch.setattr(
            ec2, "resolve_public_ref", lambda repo="": pytest.fail("checkout: no probe")
        )

        ec2.deploy(tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1")

        overrides = _overrides(captured["argv"])
        assert overrides["SourceBucket"] == "bucket"
        assert "KirocrewRef" not in overrides

    def test_dry_run_stays_offline(self, monkeypatch) -> None:
        monkeypatch.setattr(source_mod, "find_repo_root", lambda: None)
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: pytest.fail("dry run must not hit AWS"))
        monkeypatch.setattr(
            ec2, "resolve_public_ref", lambda repo="": pytest.fail("dry run must not probe")
        )
        r = ec2.deploy(tag="t1", tier=sizes.default_tier(), dry_run=True)
        assert not any(a.startswith("KirocrewRef=") for a in r.argv)

    def test_probe_repo_is_the_clone_repo(self, monkeypatch) -> None:
        captured: dict = {}
        _real_deploy(monkeypatch, captured)
        probed: list = []
        monkeypatch.setattr(
            ec2, "resolve_public_ref", lambda repo="": probed.append(repo) or "v0.7.0"
        )

        ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            region="us-east-1",
            repo="https://example.com/fork.git",
        )

        assert probed == ["https://example.com/fork.git"]
        overrides = _overrides(captured["argv"])
        assert overrides["KirocrewRepo"] == "https://example.com/fork.git"
        assert overrides["KirocrewRef"] == "v0.7.0"


class TestLaunchersReachTheSeam:
    """Both entry points that can take the clone path leave the ref to ``deploy``."""

    def test_dashboard_provision_passes_no_ref(self, monkeypatch) -> None:
        seen: dict = {}

        def fake_deploy(**kw):
            seen.update(kw)
            return ec2.DeployResult(
                tag="t1", stack_name="s", region="us-east-1", status="ok", instance_id="i-1"
            )

        monkeypatch.setattr(launch_engine.ec2, "deploy", fake_deploy)
        iid = launch_engine.RealLaunchEngine().provision(
            tag="t1", size_key=sizes.default_tier().key, profile="dev", region="us-east-1"
        )
        assert iid == "i-1"
        assert "ref" not in seen and "ship_source" not in seen

    def test_cli_wizard_passes_no_ref(self, monkeypatch) -> None:
        from kiro_crew.cloud import wizard

        seen: dict = {}

        def fake_deploy(**kw):
            seen.update(kw)
            return ec2.DeployResult(
                tag="t1", stack_name="s", region="us-east-1", status="ok", instance_id="i-1"
            )

        monkeypatch.setattr(wizard.ec2, "deploy", fake_deploy)
        monkeypatch.setattr(wizard, "_stream_progress", lambda *a, **k: None)
        wizard._deploy_with_progress(
            tag="t1", tier=sizes.default_tier(), profile="dev", region="us-east-1"
        )
        assert "ref" not in seen and "ship_source" not in seen
