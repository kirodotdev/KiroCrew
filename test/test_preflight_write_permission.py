"""Tests for the prepare-pr preflight.py write-permission gate.

The gate closes a stranding failure mode: a comment/issue-triggered agent run
does all its work (clone, plan, implement, test, review, commit) and only
discovers at *push time* that the run credential lacks write access to the
target repo, leaving a fully completed change on a local branch. preflight.py
is the deterministic Phase-0 gate that verifies the run credential can push to
the target repo before any work is done.

These tests run preflight.py as a subprocess (so no __pycache__ residue leaks
into the skill source tree) against a real bare-origin + clone git fixture (so
the existing repo/branch/base/fetch checks pass) with a FAKE ``gh`` injected on
PATH. The fake gh is a small python script the test writes to a tmp dir that is
prepended to PATH; it returns scripted JSON / exit codes / stderr per
invocation, keyed on the gh subcommand + flags. Tests that exercise the push
transport probe also inject a FAKE ``git`` in the same dir that intercepts only
``git push --dry-run`` and hands every other git call through to the real
binary. On Windows each fake gets a ``.cmd`` launcher beside it, because
CreateProcess resolves only PATHEXT extensions and does not honor shebangs.

Coverage:
- writer permission (ADMIN/MAINTAIN/WRITE) + accepted dry-run -> READY
- READ / TRIAGE / NONE + transport-denied dry-run  -> exit 30 BLOCKER (fork path)
- gh lookup failure (HTTP 403/404) + denied dry-run -> exit 30 BLOCKER
- gh rate-limit blip + accepted dry-run            -> READY (transport decides)
- gh 5xx blip + inconclusive dry-run               -> WARNING, not a hard block
- writer gh verdict + push dry-run denied          -> exit 30 BLOCKER
- writer gh verdict + push dry-run inconclusive    -> WARNING, not a hard block
- non-writer gh verdict + accepted dry-run         -> READY (fork-clone layout:
  gh resolves the read-only parent while origin is the writable fork)
- non-writer gh verdict + inconclusive dry-run     -> still exit 30 (the
  definitive gh answer stands; an unreachable transport is not evidence)
- raw gh/git stderr must NOT appear in preflight output (credential-egress guard)
"""

from __future__ import annotations

import json
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
# never echo it back (credential-egress discipline).
SECRET_STDERR_TOKEN = "ghp_SUPERSECRETtoken1234567890LEAK"

# A git push dry-run refusal in GitHub's permission-denial shape, carrying the
# planted token so egress assertions can cover the transport path too. Denied
# gh verdicts are corroborated against the transport, so tests that pin the
# exit-30 blocker install a fake git that answers the dry-run with this.
_TRANSPORT_DENIED_STDERR = (
    "remote: Permission to octo/target.git denied to octo-forker. " + SECRET_STDERR_TOKEN
)


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
        encoding="utf-8",
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
# Missing keys default to a benign (rc 1, "", "") so unrelated calls no-op.


def _install_shim(bin_dir: Path, name: str, script: str) -> None:
    """Write an executable python shim named ``name`` into bin_dir.

    On Windows, CreateProcess resolves only PATHEXT extensions and does not
    honor shebangs, so an extensionless script on PATH is invisible to
    subprocess. A ``.cmd`` launcher beside the script hands the call to the
    python interpreter explicitly.
    """
    shim_path = bin_dir / name
    shim_path.write_text(script, encoding="utf-8")
    shim_path.chmod(0o755)
    if sys.platform == "win32":
        launcher = '@echo off\r\n"{python}" "%~dp0{name}" %*\r\n'.format(
            python=sys.executable, name=name
        )
        (bin_dir / (name + ".cmd")).write_text(launcher, encoding="utf-8")


def _install_fake_gh(bin_dir: Path, scenario: dict[str, tuple[int, str, str]]) -> None:
    """Write a fake `gh` executable into bin_dir that answers per scenario."""
    import json as _json

    payload = _json.dumps(scenario)
    script = textwrap.dedent("""\
        #!{python}
        import json
        import sys

        for _stream in (sys.stdout, sys.stderr):
            if hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8")

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

        # gh repo view --json nameWithOwner -q .nameWithOwner  (target-repo resolution)
        if args[:2] == ["repo", "view"] and "nameWithOwner" in args:
            respond("name_owner")

        # gh repo view <repo> --json viewerPermission
        if args[:2] == ["repo", "view"] and "viewerPermission" in args:
            respond("viewer_perm")

        sys.exit(1)
        """).format(python=sys.executable, payload=payload)
    _install_shim(bin_dir, "gh", script)


def _install_fake_git(bin_dir: Path, dry_run_rc: int, dry_run_stderr: str) -> None:
    """Write a fake ``git`` into bin_dir that intercepts only ``push --dry-run``.

    Every other git invocation is handed through to the real binary (resolved
    now, before bin_dir shadows it on PATH), so the fixture repo keeps working;
    only the push transport probe sees the scripted rc/stderr. The intercepted
    call also dumps the probe-relevant env to ``probe-env.json`` beside the
    fake, so tests can assert what the probe subprocess actually inherited.
    """
    real_git = shutil.which("git")
    assert real_git, "real git not found on PATH"
    env_dump = str(bin_dir / "probe-env.json")
    script = textwrap.dedent("""\
        #!{python}
        import json
        import os
        import subprocess
        import sys

        for _stream in (sys.stdout, sys.stderr):
            if hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8")

        args = sys.argv[1:]
        if args[:1] == ["push"] and "--dry-run" in args:
            with open({env_dump!r}, "w", encoding="utf-8") as fh:
                json.dump(
                    {{
                        "argv": args,
                        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND"),
                        "GIT_TERMINAL_PROMPT": os.environ.get("GIT_TERMINAL_PROMPT"),
                        "LC_ALL": os.environ.get("LC_ALL"),
                    }},
                    fh,
                )
            sys.stderr.write({stderr!r})
            sys.exit({rc})
        raise SystemExit(subprocess.call([{real_git!r}, *args]))
        """).format(
        python=sys.executable,
        env_dump=env_dump,
        stderr=dry_run_stderr,
        rc=dry_run_rc,
        real_git=real_git,
    )
    _install_shim(bin_dir, "git", script)


def _run_preflight(
    cwd: str, bin_dir: Path, extra_env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run preflight.py in cwd with bin_dir (holding the fake gh) prepended to PATH."""
    env = _fixture_git_env()
    env["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    # preflight.py imports its sibling push_guard as a top-level module; running
    # it as a subprocess would otherwise drop push_guard.pyc into the skill
    # source tree's __pycache__ (a persistent working-copy mutation the
    # no-test-side-effects rule forbids). Suppress bytecode writes in the child.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, PREFLIGHT],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
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
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(bin_dir, dry_run_rc=128, dry_run_stderr=_TRANSPORT_DENIED_STDERR)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access to octo/target" in stdout
        # Actionable: names the fork path AND the read-only fallback.
        assert "fork" in stdout.lower()
        assert "read-only" in stdout.lower()
        assert "write access:    NO" in stdout


class TestDefinitiveHttpErrors:
    """A gh lookup failure reads as unknown; the transport's denial blocks."""

    def test_http_403_blocks(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 403: Resource not accessible " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(bin_dir, dry_run_rc=128, dry_run_stderr=_TRANSPORT_DENIED_STDERR)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout

    def test_http_404_blocks(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 404: Not Found " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(bin_dir, dry_run_rc=128, dry_run_stderr=_TRANSPORT_DENIED_STDERR)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout


class TestIndeterminateWarns:
    """A gh blip never hard-blocks: the transport decides, or preflight warns."""

    def test_rate_limit_403_with_accepting_transport_is_writer(self, feature_clone, tmp_path):
        """gh rate-limited but the real transport accepts: READY as a writer."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 403: API rate limit exceeded " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "STATUS: READY" in stdout
        assert "write access:    yes" in stdout
        assert "origin accepts push dry-run" in stdout
        assert "BLOCKER: no write access" not in stdout
        assert SECRET_STDERR_TOKEN not in stdout

    def test_5xx_with_inconclusive_transport_warns(self, feature_clone, tmp_path):
        """gh 5xx and an unreachable transport: WARNING, never a hard block."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 502: Bad Gateway " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(
            bin_dir,
            dry_run_rc=128,
            dry_run_stderr="fatal: unable to access remote: connection reset "
            + SECRET_STDERR_TOKEN,
        )

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "STATUS: READY" in stdout
        assert "WARNING: could not confirm write access" in stdout
        assert "BLOCKER: no write access" not in stdout
        assert SECRET_STDERR_TOKEN not in stdout


class TestPushTransportCorroboration:
    """A writer gh verdict is corroborated against the origin push transport."""

    def test_transport_denied_blocks(self, feature_clone, tmp_path):
        """Writer gh token + read-only push transport is a Phase-0 blocker."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "WRITE"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(
            bin_dir,
            dry_run_rc=128,
            dry_run_stderr="remote: Permission to octo/target.git denied to octo-forker. "
            + SECRET_STDERR_TOKEN,
        )

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout
        assert "write access:    NO" in stdout
        assert (
            "origin push transport denied the dry-run (viewerPermission=WRITE on gh probe)"
            in stdout
        )
        # Credential-egress: the transport stderr never surfaces.
        assert SECRET_STDERR_TOKEN not in stdout
        assert SECRET_STDERR_TOKEN not in stderr

    @pytest.mark.parametrize(
        ("shape", "remedy"),
        [
            ("ERROR: Repository not found.", "create or reuse a fork"),
            (
                "fatal: could not read Username for 'https://github.com': "
                "terminal prompts disabled",
                "configure git push credentials",
            ),
            ("git@github.com: Permission denied (publickey).", "configure git push credentials"),
        ],
    )
    def test_definitive_transport_shapes_block(self, feature_clone, tmp_path, shape, remedy):
        """Each definitive denial shape blocks with the remedy that fits it.

        Repository-not-found is a repo-permission face, so the remedy is the
        fork path. A missing non-interactive credential (`could not read
        Username` under disabled prompts, `Permission denied (publickey)`
        under batch mode) blocks with a credential-setup remedy instead - a
        fork push would ride the same absent credential.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "WRITE"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(bin_dir, dry_run_rc=128, dry_run_stderr=shape + " " + SECRET_STDERR_TOKEN)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout
        assert remedy in stdout
        assert SECRET_STDERR_TOKEN not in stdout

    def test_non_fast_forward_confirms_writer(self, feature_clone, tmp_path):
        """A non-fast-forward rejection proves write access, not the lack of it.

        The remote only evaluates a ref update for an accepted credential, so
        a diverged remote branch on a re-run reads as writer instead of
        raising a spurious warning.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "WRITE"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(
            bin_dir,
            dry_run_rc=1,
            dry_run_stderr="! [rejected] feature/perm-check -> feature/perm-check "
            "(non-fast-forward)\nerror: failed to push some refs " + SECRET_STDERR_TOKEN,
        )

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "write access:    yes" in stdout
        assert "rejected non-fast-forward (write access confirmed)" in stdout
        assert SECRET_STDERR_TOKEN not in stdout

    def test_pre_push_hook_output_is_not_permission_evidence(self, feature_clone, tmp_path):
        """The probe pushes with --no-verify, so hook output cannot fake a denial.

        A pre-push hook that prints "permission denied" and fails would
        otherwise read as a definitive transport denial (real git, fixture
        bare origin accepts the push itself).
        """
        if sys.platform == "win32":
            pytest.skip("hook shebang scripts are not executable on Windows")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "WRITE"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)
        hook = Path(feature_clone, ".git", "hooks", "pre-push")
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'permission denied by policy hook' >&2\nexit 1\n")
        hook.chmod(0o755)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "write access:    yes" in stdout
        assert "push dry-run ok" in stdout

    def test_probe_preserves_inherited_ssh_command(self, feature_clone, tmp_path):
        """The probe appends batch mode to the SSH command the push would use.

        A per-repo identity in GIT_SSH_COMMAND must survive into the probe
        subprocess - replacing it would test default keys instead of the ones
        the real push rides.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "WRITE"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(bin_dir, dry_run_rc=128, dry_run_stderr=_TRANSPORT_DENIED_STDERR)

        rc, stdout, stderr = _run_preflight(
            feature_clone, bin_dir, extra_env={"GIT_SSH_COMMAND": "ssh -i /custom/deploy_key"}
        )
        assert rc == 30
        dump = json.loads((bin_dir / "probe-env.json").read_text(encoding="utf-8"))
        assert dump["GIT_SSH_COMMAND"] == "ssh -i /custom/deploy_key -oBatchMode=yes"
        assert dump["GIT_TERMINAL_PROMPT"] == "0"
        assert dump["LC_ALL"] == "C"
        assert "--no-verify" in dump["argv"]

    def test_transport_inconclusive_warns(self, feature_clone, tmp_path):
        """A non-permission dry-run failure (network blip) warns and proceeds."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "WRITE"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(
            bin_dir,
            dry_run_rc=128,
            dry_run_stderr="fatal: Could not resolve host: github.example " + SECRET_STDERR_TOKEN,
        )

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "STATUS: READY" in stdout
        assert "WARNING: could not confirm write access" in stdout
        assert "push dry-run inconclusive" in stdout
        assert "BLOCKER: no write access" not in stdout
        assert SECRET_STDERR_TOKEN not in stdout

    def test_transport_confirms_writer(self, feature_clone, tmp_path):
        """rc 0 from the dry-run (real git, fixture bare origin) confirms the writer."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "WRITE"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "write access:    yes" in stdout
        assert "push dry-run ok" in stdout

    def test_fork_clone_layout_reaches_writer(self, feature_clone, tmp_path):
        """A non-writer gh verdict with an accepted dry-run reads as writer.

        The fork-clone layout: gh resolves the read-only parent repo, but
        origin points at the run's own writable fork (real git, fixture bare
        origin accepts the dry-run). The transport's accept is authoritative.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "READ"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 0, f"Expected READY (0), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "STATUS: READY" in stdout
        assert "write access:    yes" in stdout
        assert "origin accepts push dry-run" in stdout
        assert "BLOCKER: no write access" not in stdout

    def test_denied_gh_with_inconclusive_transport_still_blocks(self, feature_clone, tmp_path):
        """An unreachable transport does not overturn a definitive gh denial."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[0, '{"viewerPermission": "READ"}', ""],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(
            bin_dir,
            dry_run_rc=128,
            dry_run_stderr="fatal: no route to host github.example " + SECRET_STDERR_TOKEN,
        )

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30, f"Expected BLOCKER (30), got {rc}.\nstdout:\n{stdout}\nstderr:\n{stderr}"
        assert "BLOCKER: no write access" in stdout
        assert SECRET_STDERR_TOKEN not in stdout


class TestCredentialEgressGuard:
    """Raw gh stderr must never appear in preflight output."""

    def test_stderr_token_not_leaked(self, feature_clone, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        scenario = _base_scenario(
            viewer_perm=[1, "", "gh: HTTP 403: Forbidden " + SECRET_STDERR_TOKEN],
        )
        _install_fake_gh(bin_dir, scenario)
        _install_fake_git(bin_dir, dry_run_rc=128, dry_run_stderr=_TRANSPORT_DENIED_STDERR)

        rc, stdout, stderr = _run_preflight(feature_clone, bin_dir)
        assert rc == 30
        assert SECRET_STDERR_TOKEN not in stdout, "raw gh stderr token leaked into stdout"
        assert SECRET_STDERR_TOKEN not in stderr, "raw gh stderr token leaked into stderr"


class TestBatchLauncherGuard:
    """A .cmd/.bat launcher makes cmd.exe parse the argument list, and cmd.exe
    expands metacharacters inside arguments (CVE-2024-24576).  run() must fail
    closed instead of passing attacker-influenceable values through."""

    def _preflight_module(self):
        import importlib.util

        preflight_dir = str(Path(PREFLIGHT).parent)
        spec = importlib.util.spec_from_file_location("preflight_under_test", PREFLIGHT)
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, preflight_dir)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(preflight_dir)
        return mod

    @pytest.mark.parametrize("arg", ["a&b", "a|b", "a%PATH%b", 'x"y', "a\r\nb"])
    def test_metachar_arg_refused_for_cmd_launcher(self, tmp_path, monkeypatch, arg):
        mod = self._preflight_module()
        fake = tmp_path / "gh.cmd"
        fake.write_text("@echo off\r\n", encoding="utf-8")
        monkeypatch.setattr(mod.shutil, "which", lambda name: str(fake))
        rc, out, err = mod.run(["gh", arg])
        assert rc == 126
        assert "metacharacters" in err

    def test_clean_args_pass_for_non_batch_target(self, monkeypatch):
        mod = self._preflight_module()
        rc, out, err = mod.run([sys.executable, "-c", "print('ok')"])
        assert rc == 0
        assert out == "ok"

    def test_repo_local_resolution_refused(self, tmp_path, monkeypatch):
        """Windows PATH search visits the current directory, and preflight
        runs inside the cloned repo - a checkout carrying its own gh must
        never win the lookup."""
        mod = self._preflight_module()
        work = tmp_path / "clone"
        work.mkdir()
        planted = work / "gh.exe"
        planted.write_bytes(b"MZ")
        monkeypatch.chdir(work)
        monkeypatch.setattr(mod.shutil, "which", lambda name: str(planted))
        rc, out, err = mod.run(["gh", "auth", "status"])
        assert rc == 126
        assert "working tree" in err

    def test_resolution_outside_worktree_allowed(self, tmp_path, monkeypatch):
        mod = self._preflight_module()
        work = tmp_path / "clone"
        work.mkdir()
        monkeypatch.chdir(work)
        rc, out, err = mod.run([sys.executable, "-c", "print('ok')"])
        assert rc == 0
        assert out == "ok"


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
