"""Unit tests for argument validation in attach_backend.py and detach_backend.py."""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "kiro_crew"
    / "deploy"
    / "skills"
    / "artifact-deploy"
    / "scripts"
)


def _load_script(name: str):
    """Import a script as a module via importlib (standalone-execution path)."""
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Don't pollute sys.modules permanently
    spec.loader.exec_module(mod)
    return mod


class TestAttachBackendValidation:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_script("attach_backend.py")

    def test_valid_args_pass(self):
        """Known-good inputs should not raise."""
        self.mod._validate_args("my-profile", "us-west-2", "E1A2B3C4D5E6F7", "my-app")

    def test_empty_profile_allowed(self):
        """Empty profile (default) should pass."""
        self.mod._validate_args("", "us-east-1", "ABCDEFGHIJKLM", "demo-app")

    def test_invalid_region_rejects(self):
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("", "INVALID", "E1A2B3C4D5E6F7", "slug")
        assert exc.value.code == 2

    def test_invalid_dist_id_rejects(self):
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("", "us-west-2", "too-short", "slug")
        assert exc.value.code == 2

    def test_invalid_slug_rejects(self):
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("", "us-west-2", "E1A2B3C4D5E6F7", "UPPERCASE")
        assert exc.value.code == 2

    def test_invalid_profile_rejects(self):
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("has spaces!", "us-west-2", "E1A2B3C4D5E6F7", "slug")
        assert exc.value.code == 2

    def test_14_char_dist_id_valid(self):
        """14-char dist IDs are valid."""
        self.mod._validate_args("", "eu-west-1", "E1A2B3C4D5E6F8", "app")

    def test_dist_id_lowercase_rejects(self):
        """Lowercase dist IDs should be rejected."""
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("", "us-west-2", "e1a2b3c4d5e6f7", "slug")
        assert exc.value.code == 2


class TestDetachBackendValidation:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_script("detach_backend.py")

    def test_valid_args_pass(self):
        self.mod._validate_args("my-profile", "us-west-2", "E1A2B3C4D5E6F7", "my-app")

    def test_empty_profile_allowed(self):
        self.mod._validate_args("", "ap-southeast-2", "ABCDEFGHIJKLM", "demo")

    def test_invalid_region_rejects(self):
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("", "not-a-region", "E1A2B3C4D5E6F7", "slug")
        assert exc.value.code == 2

    def test_invalid_dist_id_rejects(self):
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("", "us-west-2", "short", "slug")
        assert exc.value.code == 2

    def test_invalid_slug_rejects(self):
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_args("", "us-west-2", "E1A2B3C4D5E6F7", "-starts-with-dash")
        assert exc.value.code == 2


class TestAwsSpawnFlow:
    """The ``aws()`` helper in both scripts: timeout, temp-profile cleanup, exit codes.

    These scripts mutate a live CloudFront distribution, so the failure handling
    around the spawn must be pinned, not just inspected: the temp sandbox profile
    is unlinked on EVERY outcome, a hung AWS CLI is bounded by the timeout, and a
    nonzero CLI exit propagates as the script's own exit code with stderr shown.

    ``run_limited`` / ``sandboxed_spawn_argv`` are imported function-locally from
    ``kiro_crew.sandbox`` (the fail-closed import), so the patch point is the
    sandbox module itself, not the script module.
    """

    @pytest.fixture(params=["attach_backend.py", "detach_backend.py"])
    def mod(self, request):
        return _load_script(request.param)

    @pytest.fixture
    def profile_file(self, tmp_path):
        p = tmp_path / "sandbox.sb"
        p.write_text("(version 1)", encoding="utf-8")
        return p

    def _patch(self, monkeypatch, profile_file, run):
        import kiro_crew.sandbox as sandbox

        monkeypatch.setattr(
            sandbox, "sandboxed_spawn_argv", lambda cmd: (list(cmd), {}, str(profile_file))
        )
        monkeypatch.setattr(sandbox, "run_limited", run)

    def test_success_returns_stdout_and_cleans_up(self, mod, monkeypatch, profile_file):
        seen = {}

        def _run(argv, **kw):
            seen.update(kw)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        self._patch(monkeypatch, profile_file, _run)
        assert mod.aws("", "us-west-2", "cloudfront", "list-distributions") == "{}"
        # The unbounded-spawn fix: a stalled endpoint cannot hang the script.
        assert seen["timeout"] == 300
        assert not profile_file.exists(), "temp sandbox profile must be unlinked on success"

    def test_timeout_exits_1_and_cleans_up(self, mod, monkeypatch, profile_file, capsys):
        def _run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=kw["timeout"])

        self._patch(monkeypatch, profile_file, _run)
        with pytest.raises(SystemExit) as exc:
            mod.aws("", "us-west-2", "cloudfront", "get-distribution")
        assert exc.value.code == 1
        assert "timed out" in capsys.readouterr().err
        assert not profile_file.exists(), "temp sandbox profile must be unlinked on timeout"

    def test_cli_failure_propagates_exit_code_and_cleans_up(
        self, mod, monkeypatch, profile_file, capsys
    ):
        def _run(argv, **kw):
            return SimpleNamespace(returncode=7, stdout="", stderr="AccessDenied\n")

        self._patch(monkeypatch, profile_file, _run)
        with pytest.raises(SystemExit) as exc:
            mod.aws("", "us-west-2", "cloudfront", "get-distribution")
        assert exc.value.code == 7
        assert "AccessDenied" in capsys.readouterr().err
        assert not profile_file.exists(), "temp sandbox profile must be unlinked on CLI failure"
