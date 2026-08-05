"""Tests for the prepare-pr project-profile mechanism.

Covers:
  * resolve_profile.py resolution order (config / kirocrew markers /
    auto-detect / generic) and the bundled KiroCrew profile contents.
  * pr_status.py readiness-context override (flag / env / default) and the
    positional-argument stripping that makes it work.

The scripts live under the packaged builtin skill and are NOT importable as a
package, so we load them by path with importlib. Everything here is stdlib and
runs on the full CI matrix (3.10 + 3.12); the TOML path is version-guarded
because tomllib is 3.11+.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr"
SCRIPTS_DIR = SKILL_DIR / "scripts"
PROFILES_DIR = SKILL_DIR / "profiles"


def _load(module_name, filename):
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_profile = _load("_pp_resolve_profile", "resolve_profile.py")
pr_status = _load("_pp_pr_status", "pr_status.py")


def _toml_available():
    try:
        import tomllib  # noqa: F401

        return True
    except ImportError:
        try:
            import tomli  # noqa: F401

            return True
        except ImportError:
            return False


# --------------------------------------------------------------------------
# resolve_profile.py
# --------------------------------------------------------------------------
def test_generic_fallback_on_empty_repo(tmp_path):
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "generic"
    assert prof["gates"] == []
    assert prof["reviewers"] == []
    assert prof["readiness"] == {"status_context": None, "defer_label": None}
    assert prof["single_commit"] is False


def test_autodetect_python_stack(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "auto-detect"
    assert "python -m pytest -q" in prof["gates"]


def test_autodetect_package_json_only_declared_scripts(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "auto-detect"
    assert "npm run build" in prof["gates"]
    assert "npm test" not in prof["gates"]  # no test script -> no test gate


def test_autodetect_package_json_no_scripts_emits_no_npm_gate(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "x"}')
    prof = resolve_profile.resolve(str(tmp_path))
    assert all(not g.startswith("npm") for g in prof["gates"])


def test_autodetect_reviewers_from_workflows(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "codex-review.yml").write_text("name: codex\n")
    (tmp_path / "go.mod").write_text("module x\n")
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "auto-detect"
    names = [r["name"] for r in prof["reviewers"]]
    assert "codex-review" in names
    assert prof["reviewers"][0]["contract"].endswith("codex-review.yml")


def test_kirocrew_markers_load_bundled_profile(tmp_path):
    (tmp_path / "AUTOSDE.yaml").write_text("rules: []\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "codex-review.yml").write_text("name: codex\n")
    (wf / "claude-review.yml").write_text("name: claude\n")
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "kirocrew"
    assert prof["single_commit"] is True
    assert prof["base_branch"] == "main"
    assert prof["readiness"]["status_context"] == "PR Readiness"
    models = {r["name"]: r["model"] for r in prof["reviewers"]}
    assert models["gpt"] == "gpt-5.6-sol"
    assert models["opus"] == "claude-opus-5"


def test_bundled_kirocrew_profile_is_valid_json():
    data = json.loads((PROFILES_DIR / "kirocrew.json").read_text())
    assert data["name"] == "kirocrew"
    # Every reviewer must carry a served model id (no bare gpt-5.6).
    for r in data["reviewers"]:
        assert r["model"] and r["model"] != "gpt-5.6"


def test_toml_config_path(tmp_path):
    toml = tmp_path / ".prepare-pr.toml"
    toml.write_text(
        "[project]\n"
        'base_branch = "trunk"\n'
        "single_commit = true\n\n"
        "[gates]\n"
        'commands = ["make check"]\n\n'
        "[review]\n"
        'rule_files = ["AGENTS.md"]\n\n'
        "[[review.reviewers]]\n"
        'name = "gpt"\n'
        'model = "gpt-5.6-sol"\n'
        "[readiness]\n"
        'status_context = "My Readiness"\n'
    )
    if _toml_available():
        prof = resolve_profile.resolve(str(tmp_path))
        assert prof["source"] == "config"
        assert prof["base_branch"] == "trunk"
        assert prof["gates"] == ["make check"]
        assert prof["rule_files"] == ["AGENTS.md"]
        assert prof["reviewers"][0]["model"] == "gpt-5.6-sol"
        assert prof["readiness"]["status_context"] == "My Readiness"
    else:
        # No TOML parser (Python < 3.11 without tomli): a present config is a
        # hard error, never silently ignored.
        try:
            resolve_profile.resolve(str(tmp_path))
        except RuntimeError as exc:
            assert "TOML parser" in str(exc)
        else:
            raise AssertionError("expected RuntimeError when no TOML parser")


def test_partial_toml_config_fills_gates_from_autodetect(tmp_path):
    if not _toml_available():
        return  # parse path only runs on 3.11+; covered on the 3.12 CI leg
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / ".prepare-pr.toml").write_text('[project]\nbase_branch = "trunk"\n')
    prof = resolve_profile.resolve(str(tmp_path))
    assert prof["source"] == "config"
    assert prof["base_branch"] == "trunk"
    assert "python -m pytest -q" in prof["gates"]  # filled from auto-detect


def test_normalize_defaults_fill_missing_keys():
    prof = resolve_profile.normalize({}, "generic")
    for key in ("source", "base_branch", "single_commit", "gates",
                "rule_files", "reviewers", "readiness"):
        assert key in prof


def test_single_commit_string_false_is_not_truthy():
    n = resolve_profile.normalize
    assert n({"single_commit": "false"}, "config")["single_commit"] is False
    assert n({"single_commit": True}, "config")["single_commit"] is True
    assert n({"single_commit": "true"}, "config")["single_commit"] is True


def test_symlinked_config_is_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token=abc\n")
    os.symlink(secret, tmp_path / ".prepare-pr.toml")
    prof = resolve_profile.resolve(str(tmp_path))
    # A symlinked config is refused -> resolution does not take the "config" path.
    assert prof["source"] != "config"


# --------------------------------------------------------------------------
# pr_status.py readiness-context override
# --------------------------------------------------------------------------
def test_readiness_context_default():
    ctx = pr_status.resolve_readiness_context(["pr_status.py", "662"], {})
    assert ctx == "PR Readiness"


def test_readiness_context_env_override():
    ctx = pr_status.resolve_readiness_context(
        ["pr_status.py"], {"PREPARE_PR_READINESS_CONTEXT": "Custom Gate"}
    )
    assert ctx == "Custom Gate"


def test_readiness_context_flag_beats_env():
    argv = ["pr_status.py", "662", "--readiness-context", "Flag Gate"]
    ctx = pr_status.resolve_readiness_context(
        argv, {"PREPARE_PR_READINESS_CONTEXT": "Env Gate"}
    )
    assert ctx == "Flag Gate"


def test_readiness_context_flag_equals_form():
    argv = ["pr_status.py", "--readiness-context=Eq Gate", "662"]
    assert pr_status.resolve_readiness_context(argv, {}) == "Eq Gate"


def test_positional_args_strip_flag():
    argv = ["662", "--readiness-context", "X"]
    assert pr_status.positional_args(argv) == ["662"]
    argv2 = ["--readiness-context=X", "700"]
    assert pr_status.positional_args(argv2) == ["700"]


if __name__ == "__main__":  # pragma: no cover - manual convenience
    sys.exit(os.system("pytest -q " + __file__))
