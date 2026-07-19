"""Unit tests for argument validation in attach_backend.py and detach_backend.py."""
import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "src" / "kiro_crew" / "deploy" / "skills" / "artifact-deploy" / "scripts"


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
