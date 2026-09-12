"""Tests for the prepare-pr preflight.py write-permission gate (issue #9244).

The defect: a comment/issue-triggered agent run does all its work (clone, plan,
implement, test, review, commit) and only discovers at *push time* that the run
credential lacks write access to the target repo, stranding a fully completed
change on a local branch. preflight.py is the deterministic Phase-0 gate, and
before this fix it verified gh auth but NEVER verified the credential could
push to the target repo.

These tests run preflight.py as a subprocess (so no __pycache__ residue leaks
into the skill source tree) against a real bare-origin + clone git fixture (so
the existing repo/branch/base/fetch checks pass) with a FAKE ``gh`` injected on
PATH. The fake gh is a small python script the test writes to a tmp dir that is
prepended to PATH; it returns scripted JSON / exit codes / stderr per
invocation, keyed on the gh subcommand + flags.

Coverage:
- writer permission (ADMIN/MAINTAIN/WRITE)         -> no write-access blocker
- READ / NONE permission                           -> exit 30 BLOCKER (fork path)
- non-rate-limit HTTP 403                           -> definitive BLOCKER
- HTTP 404                                          -> definitive BLOCKER
- rate-limit 403 / 5xx (indeterminate)             -> WARNING, not a hard block
- REST permissions.push fallback                   -> writer / denied
- raw gh stderr must NOT appear in preflight output (credential-egress guard)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = str(
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "preflight.py"
)

# A stderr blob the fake gh emits on permission-lookup failures. It stands in
# for the credential-bearing free text real gh/git can print; preflight must
# never echo it back (round-13 credential-egress discipline).
SECRET_STDERR_TOKEN = "ghp_SUPERSECRETtoken1234567890LEAK"


def _fixture_git_env() -> dict[str, str]:
    """Env for a fixture git call: no host config/templates/hooks/identity bleed."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "init.templateDir",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    return env


def _git(cwd: str, *args: str) -> str:
    """Run a git command in cwd with the scrubbed fixture env; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=_fixture_git_env(),
    )
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def _repo_pair_template(tmp_path_factory) -> tuple[str, str]:
    """Build the bare origin + initial clone once per session; ``repo_pair`` copies it."""
    root = tmp_path_factory.mktemp("preflight-perm-seed")
    origin_dir = str(root / "origin.git")
    clone_dir = str(root / "work")

    os.makedirs(origin_dir)
    _git(origin_dir, "init", "--bare")
    _git(origin_dir, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(str(root), "clone", origin_dir, "work")
    _git(clone_dir, "checkout", "-b", "main")

    Path(clone_dir, "README.md").write_text("initial\n")
    _git(clone_dir, "add", "README.md")
    _git(clone_dir, "commit", "-m", "initial commit")
    _git(clone_dir, "push", "-u", "origin", "main")

    return clone_dir, origin_dir


@pytest.fixture
def feature_clone(tmp_path, _repo_pair_template) -> str:
    """A clone on a feature branch, one commit ahead of a fresh origin/main.

    Copied from the session template so every test gets its own origin. The
    checkout sits on a feature branch (not the protected base) so preflight's
    detached-HEAD / protected-branch / stale-base checks all pass and only the
    write-permission gate decides the verdict.
    """
    template_clone, template_origin = _repo_pair_template
    origin_dir = str(tmp_path / "origin.git")
    clone_dir = str(tmp_path / "work")
    shutil.copytree(template_origin, origin_dir)
    shutil.copytree(template_clone, clone_dir)
    _git(clone_dir, "remote", "set-url", "origin", origin_dir)
    _git(clone_dir, "reset", "--hard", "HEAD")

    _git(clone_dir, "checkout", "-b", "feature/perm-check")
    Path(clone_dir, "fix.py").write_text("# fix\n")
    _git(clone_dir, "add", "fix.py")
    _git(clone_dir, "commit", "-m", "fix: the bug")
    return clone_dir


# Each scenario is a mapping the fake gh consults. Keys describe the gh call:
#   "auth"          -> (rc, stdout, stderr) for `gh auth status`
#   "pr_view"       -> (rc, stdout, stderr) for `gh pr view --json ...`
#   "name_owner"    -> (rc, stdout, stderr) for `gh repo view --json nameWithOwner ...`
#   "viewer_perm"   -> (rc, stdout, stderr) for `gh repo view <repo> --json viewerPermission`
#   "rest_repo"     -> (rc, stdout, stderr) for `gh api repos/<repo> --jq .permissions.push`
#   "user"          -> (rc, stdout, stderr) for `gh api user --jq .login`
#   "fork_view"     -> (rc, stdout, stderr) for `gh repo view <viewer>/<name> --json nameWithOwner`
# Missing keys default to a benign (rc 1, "", "") so unrelated calls no-op.


def _install_fake_gh(bin_dir: Path, scenario: dict[str, tuple[int, str, str]]) -> None:
    """Write a fake `gh` executable into bin_dir that answers per scenario."""
    import json as _json

    payload = _json.dumps(scenario)
    script = textwrap.dedent("""\
        #!{python}
        import json
        import sys

        SCENARIO = json.loads({payload!r})
        args = sys.argv[1:]


        def respond(key):
            rc, out, errtext = SCENARIO.get(key, [1, "", ""])
            if out:
                sys.stdout.write(out)
            if errtext:
                sys.stderr.write(errtext)
            sys.exit(rc)


        # gh auth status
        if args[:2] == ["auth", "status"]:
            respond("auth")

        # gh pr view --json ...
        if args[:2] == ["pr", "view"]:
            respond("pr_view")

        # gh repo view --json nameWithOwner -q .nameWithOwner  (no positional repo)
        if args[:2] == ["repo", "view"] and "nameWithOwner" in args:
            # `gh repo view <repo> --json nameWithOwner` (fork existence probe)
            # has a positional repo arg before the flags; the target-repo
            # resolution call does not. Strip known option VALUES so only a
            # real positional repo arg remains.
            cleaned = []
            skip = False
            for i, a in enumerate(args[2:]):
                if skip:
                    skip = False
                    continue
                if a in ("--json", "-q", "--jq", "--template", "-t"):
                    skip = True
                    continue
                if a.startswith("-"):
                    continue
                cleaned.append(a)
            if cleaned:
                respond("fork_view")
            respond("name_owner")

        # gh repo view <repo> --json viewerPermission
        if args[:2] == ["repo", "view"] and "viewerPermission" in args:
            respond("viewer_perm")

        # gh api user --jq .login
        if args[:2] == ["api", "user"]:
            respond("user")

        # gh api repos/<repo> --jq .permissions.push
        if args[:1] == ["api"] and any(a.startswith("repos/") for a in args):
            respond("rest_repo")

        sys.exit(1)
        """).format(python=sys.executable, payload=payload)
    gh_path = bin_dir / "gh"
    gh_path.write_text(script)
    gh_path.chmod(0o755)


def _run_preflight(cwd: str, bin_dir: Path) -> tuple[int, str, str]:
    """Run preflight.py in cwd with bin_dir (holding the fake gh) prepended to PATH."""
    env = _fixture_git_env()
    env["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    # preflight.py imports its sibling push_guard as a top-level module; running
    # it as a subprocess would otherwise drop push_guard.pyc into the skill
    # source tree's __pycache__ (a persistent working-copy mutation the
    # no-test-side-effects rule forbids). Suppress bytecode writes in the child.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, PREFLIGHT],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --- Baseline scenario helpers: auth OK, PR none, repo resolves. -------------

_AUTH_OK = [0, "Logged in to github.com", ""]
_PR_NONE = [1, "", "no pull requests found"]
_NAME_OWNER = [0, "octo/target\n", ""]


def _base_scenario(**overrides) -> dict[str, tuple[int, str, str]]:
    scenario: dict = {
        "auth": _AUTH_OK,
        "pr_view": _PR_NONE,
        "name_owner": _NAME_OWNER,
    }
    scenario.update(overrides)
    return scenario


class TestWriterPermitted:
    """A write-capable credential raises no write-access blocker."""

    @pytest.mark.parametrize("perm", ["ADMIN", "MAINTAIN", "WRITE"])
    def test_writer_reaches_ready(self, feature_clone, tmp_path, perm):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "%s"}' % perm, ""],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "STATUS: READY" in stdout
        assert "write access:    yes" in stdout
        assert "no write access" not in stdout


class TestReadOnlyBlocks:
    """READ / NONE permission blocks at Phase 0 with an actionable fork message."""

    @pytest.mark.parametrize("perm", ["READ", "TRIAGE", "NONE"])
    def test_read_only_blocks(self, feature_clone, tmp_path, perm):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "%s"}' % perm, ""],
            user=[0, "octo-forker\n", ""],
            fork_view=[1, "", "HTTP 404"],  # no existing fork
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access to octo/target" in stdout
        # Actionable: names the fork path AND the read-only fallback.
        assert "fork" in stdout.lower()
        assert "read-only" in stdout.lower()
        assert "write access:    NO" in stdout

    def test_read_only_surfaces_existing_fork(self, feature_clone, tmp_path):
        """When a fork under the viewer already exists, route there explicitly."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "READ"}', ""],
            user=[0, "octo-forker\n", ""],
            fork_view=[0, '{"nameWithOwner": "octo-forker/target"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30
        assert "existing fork octo-forker/target" in stdout
        assert "fork path:" in stdout


class TestDefinitiveHttpErrors:
    """A definitive HTTP 403/404 on the permission lookup is a hard block."""

    def test_http_403_blocks(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 403: Resource not accessible " + SECRET_STDERR_TOKEN],
            rest_repo=[1, "", "gh: HTTP 403 " + SECRET_STDERR_TOKEN],
            user=[0, "octo-forker\n", ""],
            fork_view=[1, "", "HTTP 404"],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout

    def test_http_404_blocks(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 404: Not Found " + SECRET_STDERR_TOKEN],
            rest_repo=[1, "", "gh: HTTP 404 " + SECRET_STDERR_TOKEN],
            user=[0, "octo-forker\n", ""],
            fork_view=[1, "", "HTTP 404"],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout


class TestIndeterminateWarns:
    """A transient lookup failure warns and proceeds; it never hard-blocks."""

    def test_rate_limit_403_warns(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 403: API rate limit exceeded " + SECRET_STDERR_TOKEN],
            rest_repo=[1, "", "gh: HTTP 403: API rate limit exceeded " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "STATUS: READY" in stdout
        assert "WARNING: could not confirm write access" in stdout
        assert "BLOCKER: no write access" not in stdout

    def test_5xx_warns(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 502: Bad Gateway " + SECRET_STDERR_TOKEN],
            rest_repo=[1, "", "gh: HTTP 502: Bad Gateway " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "STATUS: READY" in stdout
        assert "WARNING: could not confirm write access" in stdout
        assert "BLOCKER: no write access" not in stdout


class TestRestFallback:
    """When viewerPermission is unavailable, fall back to permissions.push."""

    def test_push_true_is_writer(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, "{}", ""],  # rc 0 but no viewerPermission field
            rest_repo=[0, "true\n", ""],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "write access:    yes" in stdout
        assert "BLOCKER: no write access" not in stdout

    def test_push_false_blocks(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, "{}", ""],
            rest_repo=[0, "false\n", ""],
            user=[0, "octo-forker\n", ""],
            fork_view=[1, "", "HTTP 404"],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout


class TestCredentialEgressGuard:
    """Raw gh stderr must never appear in preflight output (round-13 lesson)."""

    def test_stderr_token_not_leaked(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 403: Forbidden " + SECRET_STDERR_TOKEN],
            rest_repo=[1, "", "gh: HTTP 403: Forbidden " + SECRET_STDERR_TOKEN],
            user=[0, "octo-forker\n", ""],
            fork_view=[1, "", "HTTP 404 " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30
        assert SECRET_STDERR_TOKEN not in stdout, "raw gh stderr token leaked into stdout"
        assert SECRET_STDERR_TOKEN not in stderr, "raw gh stderr token leaked into stderr"


class TestRegressionGuard:
    """The write-permission gate must not regress the existing Phase-0 checks."""

    def test_gh_not_authed_still_blocks_without_perm_check(self, feature_clone, tmp_path):
        """When gh is not authenticated the write-permission check does not run."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = {
            "auth": [1, "", "not logged in"],
        }
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30
        assert "BLOCKER: gh not authenticated" in stdout
        # The write-access line is not printed when gh is unauthenticated.
        assert "write access:" not in stdout
