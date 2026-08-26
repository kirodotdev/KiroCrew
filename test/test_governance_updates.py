"""Update pins: where new code may come from, and the minimum version.

Covers the two enterprise pins (``governance.UpdatePins``) and the shared seam
the three update paths call (``platform.update_governance``).
"""

from __future__ import annotations

import os
import pathlib

import pytest

from kiro_crew.platform import update_governance
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    UpdatePins,
    active_update_pins,
    parse_policy,
    parse_profile,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def _policy(**updates: str) -> dict:
    body: dict = {"version": 1, "boot": {}}
    if updates:
        body["updates"] = updates
    return body


class TestSourcePin:
    def test_unpinned_permits_anything(self):
        pins = UpdatePins()
        assert pins.permits_source("https://github.com/anyone/anything")
        assert pins.permits_source("")

    def test_glob_matches(self):
        pins = UpdatePins(source="https://github.com/acme/*")
        assert pins.permits_source("https://github.com/acme/kirocrew")
        assert not pins.permits_source("https://github.com/acme-evil/kirocrew")

    def test_unresolvable_source_is_denied_when_pinned(self):
        """An admin's pin must not be satisfied by "we could not tell"."""
        pins = UpdatePins(source="https://git.corp.example/*")
        assert not pins.permits_source("")
        assert not pins.permits_source("   ")

    def test_scp_and_path_remotes_are_matchable(self):
        """`updates.source` is a glob, so non-URL remote shapes work too."""
        assert UpdatePins(source="git@corp:*").permits_source("git@corp:team/repo")
        assert UpdatePins(source="/srv/repos/*").permits_source("/srv/repos/approved")

    @pytest.mark.parametrize(
        "url",
        [
            "/srv/repos/approved/../evil/repo.git",  # git resolves this outside
            "/srv/repos/approved/./ok.git",
            "/srv/repos/approved/..\\evil/repo.git",  # `\` separates on Windows
        ],
    )
    def test_traversal_cannot_escape_the_pin(self, url):
        """`*` spans separators, so a glob alone does not confine the path."""
        assert not UpdatePins(source="/srv/repos/approved/*").permits_source(url)

    def test_a_dot_inside_a_name_is_not_a_traversal(self):
        pins = UpdatePins(source="https://github.com/acme/*")
        assert pins.permits_source("https://github.com/acme/my.repo.git")
        assert pins.permits_source("https://github.com/acme/.hidden")

    def test_matching_is_case_sensitive_on_every_platform(self):
        """`fnmatch` normcases (lowercases on Windows); `fnmatchcase` does not.

        `…/APPROVED` must not satisfy an `…/approved` pin — git and every
        case-sensitive forge treat those as different repositories, and a ceiling
        must not change verdict with the OS. Fails on Windows if it regresses.
        """
        assert not UpdatePins(source="https://git.corp/approved").permits_source(
            "https://git.corp/APPROVED"
        )


class TestMinVersion:
    def test_unpinned_always_met(self):
        assert UpdatePins().meets_min_version("0.0.1")
        assert UpdatePins().meets_min_version("")

    @pytest.mark.parametrize(
        "current,floor,expected",
        [
            ("1.2.3", "1.2.3", True),
            ("1.2.4", "1.2.3", True),
            ("1.2.2", "1.2.3", False),
            ("2.0.0", "1.9.9", True),
            # Shorter tuples zero-extend: 1.2 == 1.2.0.
            ("1.2", "1.2.0", True),
            ("1.2", "1.2.1", False),
            ("1.10.0", "1.9.0", True),  # numeric, not lexical
        ],
    )
    def test_ordering(self, current, floor, expected):
        assert UpdatePins(min_version=floor).meets_min_version(current) is expected

    def test_prerelease_suffix_is_stripped_off_the_whole_string(self):
        """This project's CI stamps a dot INSIDE the pre-release.

        A per-component strip would leave `nightly.20260728t184500` as its own
        component and read every nightly build as version 0 — permanently
        non-compliant, which at boot means a forced-update loop.
        """
        pins = UpdatePins(min_version="0.2.0")
        assert pins.meets_min_version("0.2.0-nightly.20260728t184500")
        assert pins.meets_min_version("0.3.0-insider.2")
        assert pins.meets_min_version("0.2.0+build.7")
        assert not pins.meets_min_version("0.1.9-nightly.20260728t184500")

    def test_unparseable_floor_imposes_none(self):
        """A typo must not brick a fleet."""
        assert UpdatePins(min_version="not-a-version").meets_min_version("0.0.1")

    def test_unparseable_current_is_below_the_floor(self):
        """Take the update rather than sit on a build we cannot identify."""
        assert not UpdatePins(min_version="1.0.0").meets_min_version("dev")


class TestPolicyParsing:
    def test_absent_updates_is_unpinned(self):
        ceiling = parse_policy(_policy())
        assert ceiling.updates == UpdatePins()

    def test_pins_are_parsed(self):
        ceiling = parse_policy(_policy(source="https://git.corp/*", min_version="1.2.3"))
        assert ceiling.updates.source == "https://git.corp/*"
        assert ceiling.updates.min_version == "1.2.3"

    def test_unknown_key_fails_closed(self):
        with pytest.raises(PlatformCompositionError, match="unknown key"):
            parse_policy(_policy(sources="typo"))

    def test_non_object_fails_closed(self):
        with pytest.raises(PlatformCompositionError, match="must be an object"):
            parse_policy({"version": 1, "boot": {}, "updates": "https://git.corp"})

    def test_profile_may_not_set_updates(self):
        """Policy-only: a profile redirecting the source would be escalation."""
        with pytest.raises(PlatformCompositionError, match="policy-only"):
            parse_profile({"name": "app-x", "updates": {"source": "https://evil/*"}})

    def test_profile_without_updates_still_parses(self):
        assert parse_profile({"name": "app-x"}).name == "app-x"

    @pytest.mark.parametrize("bad", [False, 0, [], {}])
    def test_falsy_non_string_pin_is_rejected_not_coerced(self, bad):
        """`"source": false` must not silently mean "unpinned"."""
        with pytest.raises(PlatformCompositionError, match="must be a string"):
            parse_policy(_policy(source=bad))

    def test_null_is_a_valid_no_pin(self):
        ceiling = parse_policy({"version": 1, "boot": {}, "updates": {"source": None}})
        assert ceiling.updates.source == ""


class TestSeam:
    """The shared gate the API, CLI and boot paths call."""

    def test_ungoverned_host_is_unpinned(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins", lambda: UpdatePins()
        )
        assert update_governance.update_blocked_reason("https://anywhere") == ""
        assert update_governance.update_required("0.0.1") is False
        assert update_governance.min_version() == ""

    def test_source_mismatch_is_blocked_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins",
            lambda: UpdatePins(source="https://git.corp/*"),
        )
        reason = update_governance.update_blocked_reason("https://github.com/evil/x")
        # Names neither the remote nor the pin: both can embed a token.
        assert "does not match" in reason
        assert "github.com" not in reason and "git.corp" not in reason

    def test_unresolvable_source_reports_so(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins",
            lambda: UpdatePins(source="https://git.corp/*"),
        )
        assert "does not match" in update_governance.update_blocked_reason("")

    def test_below_floor_requires_an_update(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins",
            lambda: UpdatePins(min_version="2.0.0"),
        )
        assert update_governance.update_required("1.9.9") is True
        assert update_governance.update_required("2.0.0") is False

    def test_governance_error_does_not_block(self, monkeypatch):
        """A glitch must not strand a host on a build that may need a patch."""

        def _boom():
            raise RuntimeError("context unavailable")

        monkeypatch.setattr("kiro_crew.platform.context.current_context", _boom)
        assert active_update_pins() == UpdatePins()
        assert update_governance.update_blocked_reason("https://anywhere") == ""
        assert update_governance.update_required("0.0.1") is False


class TestRemoteResolution:
    def test_reads_the_tracked_remote_not_origin(self, monkeypatch):
        """`git pull` follows branch.<name>.remote, so that is what we check."""
        calls: list[list[str]] = []

        class _R:
            returncode = 0

            def __init__(self, out: str) -> None:
                self.stdout = out

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "config" in argv:
                return _R("upstream\n")
            if "ls-remote" in argv:
                return _R("https://git.corp/team/repo\n")
            return _R("")

        monkeypatch.setattr("subprocess.run", fake_run)
        url = update_governance.resolve_remote_url("/proj", branch="main")
        assert url == "https://git.corp/team/repo"
        assert ["ls-remote", "--get-url", "--", "upstream"] in [c[1:] for c in calls]
        # argv[0] must be the resolved binary, never a bare `git` off PATH.
        assert all(c[0] != "git" for c in calls), calls

    def test_unknown_remote_echo_is_not_a_url(self, monkeypatch):
        """`--get-url` echoes its argument back for an unknown remote."""

        class _R:
            returncode = 0
            stdout = "origin\n"

        monkeypatch.setattr("subprocess.run", lambda argv, **kw: _R())
        assert update_governance.resolve_remote_url("/proj", branch="main") == ""

    def test_detached_head_is_unresolvable(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("must not run git"))
        assert update_governance.resolve_remote_url("/proj", branch="HEAD") == ""

    def test_fixed_remote_ignores_the_tracked_remote(self, monkeypatch):
        """CLI/boot fetch a hardcoded `origin`, so they must validate `origin`.

        Otherwise a branch tracking an approved upstream green-lights an `origin`
        fetch from elsewhere — approving one source and installing another.
        """
        calls: list[list[str]] = []

        class _R:
            returncode = 0
            stdout = "https://git.corp/origin-repo\n"

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return _R()

        monkeypatch.setattr("subprocess.run", fake_run)
        assert (
            update_governance.resolve_remote_url("/proj", remote="origin")
            == "https://git.corp/origin-repo"
        )
        # Neither the branch nor its tracked remote is consulted.
        assert [c[1:] for c in calls] == [["ls-remote", "--get-url", "--", "origin"]]
        assert all(c[0] != "git" for c in calls), calls

    def test_resolves_the_branch_itself_when_not_given(self, monkeypatch):
        """The API path passes no branch; the seam resolves it (one impl)."""
        calls: list[list[str]] = []

        class _R:
            returncode = 0

            def __init__(self, out: str) -> None:
                self.stdout = out

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "rev-parse" in argv:
                return _R("main\n")
            if "config" in argv:
                return _R("origin\n")
            return _R("https://git.corp/team/repo\n")

        monkeypatch.setattr("subprocess.run", fake_run)
        assert update_governance.resolve_remote_url("/proj") == "https://git.corp/team/repo"
        assert ["rev-parse", "--abbrev-ref", "HEAD"] in [c[1:] for c in calls]
        assert all(c[0] != "git" for c in calls), calls

    def test_missing_git_is_unresolvable_not_an_error(self, monkeypatch):
        def _no_git(*a, **k):
            raise FileNotFoundError("git")

        monkeypatch.setattr("subprocess.run", _no_git)
        assert update_governance.resolve_remote_url("/proj", branch="main") == ""


class TestPrimaryBranchResolution:
    """The gate on the unattended boot-time ``git reset --hard`` + reinstall.

    Two failure modes are asserted here, because the gate can be wrong in
    opposite directions and only one of them is loud:

    * Too narrow -> a whole install cohort silently never updates. A hardcoded
      ``mainline`` did that to every ``main`` checkout of this repo.
    * Too wide, or steerable -> unreviewed code is installed and executed, or a
      mandatory version floor is vetoed.
    """

    def test_main_is_primary(self):
        """The regression: this repo's primary line is `main`, not `mainline`."""
        assert update_governance.is_primary_branch("main")

    def test_mainline_is_primary(self):
        """Internal and mirror clones whose primary line carries that name."""
        assert update_governance.is_primary_branch("mainline")

    def test_feature_branch_is_not_primary(self):
        assert not update_governance.is_primary_branch("fix/some-thing")
        assert not update_governance.is_primary_branch("beta-braveheart")

    def test_detached_head_is_never_primary(self):
        """There is no branch to fast-forward, so there is nothing to apply.

        The old code fabricated `branch = "mainline"` here, which on an internal
        clone would have let a boot-time `git reset --hard` move a deliberately
        detached checkout.
        """
        assert not update_governance.is_primary_branch("HEAD")
        assert not update_governance.is_primary_branch("")

    def test_lookalike_names_are_not_primary(self):
        """Membership is exact — no prefix, suffix, or case leniency.

        A remote is free to carry a branch called `main-2`, and pushing one must
        not be enough to get it installed on boot.
        """
        for name in ("main-2", "mainline2", "Main", "MAIN", "origin/main", " main"):
            assert not update_governance.is_primary_branch(name), name

    def test_decision_reads_no_git_state_at_all(self, monkeypatch):
        """The security property, asserted directly: no local ref participates.

        `refs/remotes/<remote>/HEAD` is one `git remote set-head` away from being
        repointed by anything with write access to the checkout. Consulting it
        breaks in BOTH directions — obeying it aims the boot-time
        `git reset --hard` + `pip install` + `execv` at an arbitrary branch of the
        still-approved origin (the source pin cannot catch it, the remote URL is
        unchanged), while letting it merely narrow turns the same one-command
        repoint into a veto. So the gate runs no git at all, and this fails if a
        future change reintroduces one.
        """
        monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("must not run git"))
        assert update_governance.is_primary_branch("main")
        assert not update_governance.is_primary_branch("attacker-branch")

    def test_a_repointed_pointer_cannot_veto_a_mandatory_update(self):
        """A `main` checkout stays primary however `origin/HEAD` is aimed.

        `_auto_apply_update` is what an enterprise `min_version` floor calls on a
        checkout, so a vetoable gate would let a local repoint strand a host
        below the administrator's minimum version — the ceiling bypass the
        module docstring says a pin must not permit.
        """
        assert update_governance.is_primary_branch("main")
        assert update_governance.is_primary_branch("mainline")

    def test_unrelated_primary_fork_gets_no_unattended_update(self):
        """The accepted cost, asserted rather than left implicit.

        A fork whose primary line is named something else only gets the badge.
        `kirocrew update` and the dashboard apply path still serve it, and both
        have a human in the loop — the difference that makes wider trust
        acceptable there and not on an unauthenticated boot path.
        """
        assert not update_governance.is_primary_branch("develop")
        assert not update_governance.is_primary_branch("trunk")

    def test_allowlist_is_frozen(self):
        """A mutable set here would be writable by any import-time code."""
        assert isinstance(update_governance.PRIMARY_BRANCHES, frozenset)

    def test_this_repo_s_real_primary_branch_is_allowlisted(self):
        """No mock: the real checkout's default branch must be in the allowlist.

        This is the only test that can catch the original bug CLASS — a name that
        is simply wrong for this repo. Every assertion above would still pass if
        the allowlist held one wrong literal, because they choose their own
        inputs; this one reads the repo. Skips where the metadata is absent
        (`--single-branch` CI clones, exported tarballs).
        """
        import pathlib as _pathlib
        import subprocess as _subprocess

        root = _pathlib.Path(update_governance.__file__).resolve().parents[3]
        if not (root / ".git").exists():
            pytest.skip("not a git checkout")
        probe = _subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=root,
            # Sanitized like every other git call here, but rooted at the REAL
            # checkout: this test's subject is this repository, not a tmp_path one.
            env=_fixture_git_env(str(root)),
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            pytest.skip("clone published no origin/HEAD pointer")
        default = probe.stdout.strip().removeprefix("origin/")
        assert update_governance.is_primary_branch(default), (
            f"this repo's default branch {default!r} is not in PRIMARY_BRANCHES — "
            "auto-update would silently never run for any checkout of it"
        )


def _fixture_git_env(repo: str) -> dict:
    """Env for a fixture git call: no inherited templates, hooks, or identity.

    A developer (or CI image) with ``GIT_TEMPLATE_DIR`` set, or a global
    ``core.hooksPath``, would otherwise have those hooks COPIED into every
    fixture repo and executed by the ``git commit`` below — host-side side
    effects from running the test suite. ``init.templateDir`` and the exec
    vectors are pinned through the same mechanism the production path uses, and
    the committer identity is supplied so the call cannot depend on, or fall
    back to, the developer's global config.
    """
    # `git_command_env()` rather than a merge over `os.environ`: an exported
    # GIT_DIR would otherwise point these fixture mutations at unrelated
    # metadata, so `git commit` / `update-ref` could write an operator's real
    # refs while the test believes it is working in tmp_path.
    env = {
        **update_governance.git_command_env(),
        "GIT_TEMPLATE_DIR": "",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    count = int(env["GIT_CONFIG_COUNT"])
    env[f"GIT_CONFIG_KEY_{count}"] = "init.templateDir"
    env[f"GIT_CONFIG_VALUE_{count}"] = ""
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _git_update_ref(repo: str, ref: str, sha: str) -> None:
    """Point *ref* at *sha* without needing a real remote."""
    import subprocess

    done = subprocess.run(
        ["git", "update-ref", ref, sha],
        cwd=repo,
        env=_fixture_git_env(repo),
        capture_output=True,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr


def _init_repo(path) -> str:
    """A real one-commit git repo at *path*, or a skip when git is unusable."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    repo = str(path)
    env = _fixture_git_env(repo)
    for argv in (
        ["git", "init", "-q", "--template=", "."],
        ["git", "commit", "-q", "--no-verify", "--allow-empty", "-m", "x"],
    ):
        done = subprocess.run(argv, cwd=repo, env=env, capture_output=True, **UTF8_TEXT)
        if done.returncode != 0:
            pytest.skip(f"git unusable: {(done.stderr or '').strip()[:120]}")
    return repo


def _git_config(repo: str, *args: str) -> None:
    import subprocess

    done = subprocess.run(
        ["git", "config", *args],
        cwd=repo,
        env=_fixture_git_env(repo),
        capture_output=True,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr


class TestGitExecNeutralizers:
    """The update path runs git on a tree the agent can write.

    `git status` and `git diff` do not just READ config — they exec the program a
    repo names in `core.fsmonitor`, and a reset runs hooks. These assert the
    fixed-key vectors are pinned back, against real git rather than a mock,
    because the whole question is what git actually does with the config.
    """

    @staticmethod
    def _pinned() -> dict:
        env = update_governance.git_neutralizer_env()
        return {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }

    def test_env_pins_the_named_exec_vectors(self):
        pinned = self._pinned()
        assert pinned["core.fsmonitor"] == "false"
        assert pinned["core.hooksPath"] == os.devnull
        assert pinned["credential.helper"] == ""
        assert pinned["protocol.ext.allow"] == "never"
        assert pinned["diff.external"] == ""

    def test_every_exec_capable_fixed_key_is_pinned(self):
        """The list's criterion is "git may exec this value, key name is fixed".

        Asserted as a set rather than one key at a time because the first version
        of the list was an enumeration with no criterion, and was missing
        `core.gitProxy` for exactly that reason.
        """
        pinned = self._pinned()
        for key in (
            "core.fsmonitor",
            "core.hooksPath",
            "core.sshCommand",
            "core.askPass",
            "core.alternateRefsCommand",
            "core.pager",
            "core.editor",
            "sequence.editor",
            "credential.helper",
            "diff.external",
            "gpg.program",
            "uploadpack.packObjectsHook",
            "protocol.ext.allow",
        ):
            assert key in pinned, f"{key} is exec-capable but not pinned"

    def test_no_pinned_value_is_a_repo_supplied_program(self):
        """A pin must not itself name something the repo could control."""
        for key, value in update_governance._GIT_EXEC_NEUTRALIZERS:
            assert not value.startswith("!"), (key, value)
            assert "$" not in value, (key, value)

    def test_count_matches_the_entries(self):
        """A stale GIT_CONFIG_COUNT silently drops the tail of the list."""
        env = update_governance.git_neutralizer_env()
        count = int(env["GIT_CONFIG_COUNT"])
        assert count == len(update_governance._GIT_EXEC_NEUTRALIZERS) + len(
            update_governance._GIT_BLAST_RADIUS_PINS
        )
        assert f"GIT_CONFIG_KEY_{count}" not in env
        for i in range(count):
            assert f"GIT_CONFIG_KEY_{i}" in env
            assert f"GIT_CONFIG_VALUE_{i}" in env

    def test_fsmonitor_program_does_not_run(self, tmp_path):
        """The finding, reproduced: `git status` execs a repo-named program.

        Asserted in both directions — without the neutralizer the program runs,
        with it it does not — so the test fails if the pin ever stops working
        rather than passing vacuously on a git that never ran the hook at all.
        """
        import subprocess

        repo = _init_repo(tmp_path / "repo")
        marker = tmp_path / "FSMONITOR_RAN"
        hook = tmp_path / "fsmon.sh"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        hook.chmod(0o755)
        _git_config(repo, "--local", "core.fsmonitor", str(hook))

        def _status(env):
            if marker.exists():
                marker.unlink()
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                env=env,
                capture_output=True,
                **UTF8_TEXT,
            )
            return marker.exists()

        # BOTH probe environments are built from the sanitized fixture env: a
        # raw `os.environ` carrying an exported GIT_DIR would point this `git
        # status` at another checkout entirely, and then execute THAT
        # repository's configured fsmonitor -- a host-side side effect from
        # running the test suite, outside tmp_path.
        protected = _fixture_git_env(repo)

        # The control needs the fsmonitor pin OFF while keeping the sanitizing.
        # Flip that one pinned value rather than dropping to `os.environ`, so the
        # two probes differ only in the thing under test.
        control = dict(protected)
        count = int(control["GIT_CONFIG_COUNT"])
        for i in range(count):
            if control[f"GIT_CONFIG_KEY_{i}"] == "core.fsmonitor":
                control[f"GIT_CONFIG_VALUE_{i}"] = str(hook)

        if not _status(control):
            pytest.skip("this git does not spawn core.fsmonitor")
        assert not _status(protected), "core.fsmonitor still executed with the neutralizer applied"


class TestNoTestSideEffects:
    """Every git subprocess in THIS file must carry the sanitized environment.

    `no-test-side-effects`, and it has now recurred four times across review rounds
    (GIT_TEMPLATE_DIR, then GIT_DIR in the fixtures, then a control probe, then
    seven bare calls at once). The failure is always the same: an inherited
    ``GIT_DIR`` points a probe at an operator's real repository, where `git status`
    can invoke THAT repo's configured fsmonitor -- a host-side side effect from
    running the test suite, outside ``tmp_path``.

    Fixing instances one at a time is what produced four rounds of it, so this
    asserts the property over the whole file. A new bare call fails here instead of
    in someone's review.
    """

    def test_every_subprocess_run_passes_a_sanitized_env(self):
        import re

        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        lines = source.splitlines()

        offenders: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            if "subprocess.run(" not in line:
                continue
            # Walk to the end of the call expression by paren depth.
            chunk: list[str] = []
            depth = 0
            for j in range(index, min(index + 25, len(lines))):
                chunk.append(lines[j])
                depth += lines[j].count("(") - lines[j].count(")")
                if depth <= 0 and j > index:
                    break
            body = "\n".join(chunk)
            if not re.search(r"\benv\s*=", body):
                offenders.append((index + 1, lines[index].strip()))

        assert not offenders, (
            "these subprocess.run calls inherit the ambient environment; pass "
            f"env=_fixture_git_env(repo): {offenders}"
        )


class TestWorktreeRedirectRefusal:
    """`core.worktree` is not an exec vector -- it is a data-loss vector.

    Repo config can point the work tree at another directory, and the unattended
    `git reset --hard` then overwrites matching files THERE. Nothing is executed,
    so no exec-key pin covers it, and it cannot be pinned away either: git
    ignores `core.worktree` from `-c`/`GIT_CONFIG_*`, and the `GIT_WORK_TREE`
    that does override it is refused without a matching `GIT_DIR`. So it is
    refused.
    """

    def test_ordinary_checkout_is_allowed(self, tmp_path):
        repo = _init_repo(tmp_path / "plain")
        assert update_governance.repo_exec_config_reason(repo) == ""

    def test_redirected_worktree_is_refused(self, tmp_path):
        """Against real git, with the redirect proven to take effect first."""
        import subprocess

        repo = _init_repo(tmp_path / "wt")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        _git_config(repo, "--local", "core.worktree", str(decoy))
        proof = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo,
            env=_fixture_git_env(repo),
            capture_output=True,
            **UTF8_TEXT,
        )
        if str(decoy) not in (proof.stdout or ""):
            pytest.skip("this git does not honour core.worktree here")
        assert "redirected" in update_governance.repo_exec_config_reason(repo)

    def test_a_symlinked_checkout_is_not_a_redirect(self, tmp_path):
        """`realpath` both sides, or every symlinked install reads as redirected.

        This repo is itself reached through a symlinked path, so a naive string
        compare would refuse the update on the developer's own checkout.
        """
        repo = _init_repo(tmp_path / "real")
        link = tmp_path / "link"
        try:
            link.symlink_to(repo)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        assert update_governance.repo_exec_config_reason(str(link)) == ""

    def test_unresolvable_work_tree_is_refused(self, monkeypatch):
        """Cannot prove where a write would land, so do not write."""
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance._git_probe",
            lambda proj, *a: "" if a[:1] == ("config",) else None,
        )
        assert update_governance.repo_exec_config_reason("/proj") != ""


class TestRepoExecConfigRefusal:
    """Drivers named BY THE REPOSITORY have no fixed key, so they are refused.

    Real repos, not mocks: two of these cases exist only because git resolves
    config in a way a mock would not reproduce.
    """

    def test_clean_repo_is_allowed(self, tmp_path):
        repo = _init_repo(tmp_path / "clean")
        assert update_governance.repo_exec_config_reason(repo) == ""

    def test_local_filter_driver_is_refused(self, tmp_path):
        repo = _init_repo(tmp_path / "local")
        _git_config(repo, "--local", "filter.evil.smudge", "sh -c ':'")
        assert "filter.evil.smudge" in update_governance.repo_exec_config_reason(repo)

    def test_worktree_scoped_driver_is_refused(self, tmp_path):
        """A `--local` listing does NOT report worktree-scoped keys.

        So probing only `--local` let a repo with `extensions.worktreeConfig`
        hide a driver that still resolved when the command ran.
        """
        repo = _init_repo(tmp_path / "wt")
        _git_config(repo, "--local", "extensions.worktreeConfig", "true")
        _git_config(repo, "--worktree", "filter.evil.process", "sh -c ':'")
        assert "filter.evil.process" in update_governance.repo_exec_config_reason(repo)

    def test_included_driver_is_refused(self, tmp_path):
        """For a SPECIFIC scope query git defaults include-following OFF.

        Without `--includes` a driver reached through `include.path` is invisible
        to the probe yet still resolves when git runs. The include path is
        relative to the config file's own directory, i.e. `.git/`.
        """
        import subprocess

        repo = _init_repo(tmp_path / "inc")
        (pathlib.Path(repo) / ".git" / "hostile.cfg").write_text(
            "[filter \"evil\"]\n\tclean = sh -c ':'\n"
        )
        _git_config(repo, "--local", "include.path", "hostile.cfg")
        # Precondition: git itself must resolve it, or the case proves nothing.
        resolved = subprocess.run(
            ["git", "-C", repo, "config", "--includes", "--get", "filter.evil.clean"],
            capture_output=True,
            env=_fixture_git_env(repo),
            **UTF8_TEXT,
        )
        if resolved.returncode != 0:
            pytest.skip("this git does not follow include.path here")
        assert "filter.evil.clean" in update_governance.repo_exec_config_reason(repo)

    def test_textconv_driver_is_refused(self, tmp_path):
        """`git diff` runs a textconv driver, and its name is repo-chosen too."""
        repo = _init_repo(tmp_path / "tc")
        _git_config(repo, "--local", "diff.evil.textconv", "sh -c ':'")
        assert "diff.evil.textconv" in update_governance.repo_exec_config_reason(repo)

    def test_external_diff_command_is_refused(self, tmp_path):
        """`diff.<driver>.command` REPLACES the diff with an external program.

        `textconv` only converts a blob to text; `command` runs instead of the
        diff itself. Both are repo-named and both are reached by the `git diff`
        the update path runs, so refusing only `textconv` left this open.
        """
        repo = _init_repo(tmp_path / "extdiff")
        _git_config(repo, "--local", "diff.evil.command", "sh -c ':'")
        assert "diff.evil.command" in update_governance.repo_exec_config_reason(repo)

    def test_git_proxy_is_refused_not_pinned(self, tmp_path):
        """`core.gitProxy` has a fixed name but pinning it does not work.

        Verified: with `core.gitProxy=""` supplied through `GIT_CONFIG_*`,
        `git config --get` reports the empty value and the repository's proxy
        program still runs on a `git://` fetch -- it is consulted as a first-match
        list, so a higher-priority empty entry does not suppress the repo's own.
        Refused for the same reason `core.worktree` is.
        """
        repo = _init_repo(tmp_path / "proxy")
        _git_config(repo, "--local", "core.gitProxy", "sh -c ':'")
        # git's own `--name-only --list` lowercases the key, so compare that way.
        assert "gitproxy" in update_governance.repo_exec_config_reason(repo).lower()

    def test_the_ineffective_pin_was_removed(self):
        """Keeping it in the pin list would imply the hazard is handled there."""
        pinned = {k.lower() for k, _ in update_governance._GIT_EXEC_NEUTRALIZERS}
        assert "core.gitproxy" not in pinned
        assert "core.gitproxy" in update_governance._REPO_UNPINNABLE_KEYS

    def test_a_rename_hides_from_diff_filter_a_without_no_renames(self, tmp_path):
        """Why the added-paths query carries `--no-renames`, against real git.

        This asserts git's BEHAVIOUR rather than the gateway's argv, because the
        argv is only correct for as long as this remains true. If a future git
        stops classifying a pure `git mv` as `R` here, this test says so instead
        of the flag quietly becoming cargo.
        """
        import subprocess

        repo = _init_repo(tmp_path / "rename")

        def run(*argv):
            return subprocess.run(
                argv,
                cwd=repo,
                capture_output=True,
                env=_fixture_git_env(repo),
                **UTF8_TEXT,
            )

        src_file = pathlib.Path(repo) / "a.txt"
        src_file.write_text("line one\nline two\nline three\nline four\n")
        run("git", "add", "a.txt")
        run("git", "commit", "-qm", "base")
        base = run("git", "rev-parse", "HEAD").stdout.strip()

        run("git", "mv", "a.txt", "b.txt")
        run("git", "commit", "-qm", "rename")
        target = run("git", "rev-parse", "HEAD").stdout.strip()
        run("git", "reset", "-q", "--hard", base)

        with_renames = run(
            "git", "diff", "--name-only", "--diff-filter=A", "-z", "HEAD", target
        ).stdout
        without = run(
            "git",
            "diff",
            "--name-only",
            "--diff-filter=A",
            "--no-renames",
            "-z",
            "HEAD",
            target,
        ).stdout

        names = run("git", "diff", "--name-status", "HEAD", target).stdout
        if not names.startswith("R"):
            pytest.skip(f"this git did not detect the rename: {names!r}")

        # The bug: the destination is invisible to the guard's own query...
        assert with_renames.strip("\0").strip() == ""
        # ...and visible once renames are decomposed.
        assert "b.txt" in without

    def test_assume_unchanged_edit_is_invisible_to_status_but_found(self, tmp_path):
        """The hazard and the detection, both against real git.

        `git update-index --assume-unchanged` tells git to stop checking a path,
        and git obeys it thoroughly: for an EDITED file both `status --porcelain`
        and `diff --quiet HEAD` report clean, while `reset --hard` still overwrites
        it. Asserting the blindness here is what makes the detection meaningful --
        without it this test would not show why the check exists.
        """
        import subprocess

        repo = _init_repo(tmp_path / "assume")

        def run(*argv):
            return subprocess.run(
                argv,
                cwd=repo,
                capture_output=True,
                env=_fixture_git_env(repo),
                **UTF8_TEXT,
            )

        target = pathlib.Path(repo) / "tracked.txt"
        target.write_text("original\n")
        run("git", "add", "tracked.txt")
        run("git", "commit", "-qm", "base")

        run("git", "update-index", "--assume-unchanged", "tracked.txt")
        target.write_text("MY PRECIOUS EDIT\n")

        # The blindness: git itself reports nothing to lose.
        assert run("git", "status", "--porcelain").stdout.strip() == ""
        assert run("git", "diff", "--quiet", "HEAD").returncode == 0

        # The detection finds it anyway.
        assert update_governance.hidden_worktree_edits(repo) == ["tracked.txt"]

    def test_an_unmodified_assume_unchanged_file_does_not_refuse(self, tmp_path):
        """Only an ACTUAL difference is a reason to stop.

        `assume-unchanged` is commonly used for local config overrides. Refusing on
        the mere presence of the bit would disable auto-update for those checkouts
        -- reintroducing the silent no-op this change exists to remove.
        """
        import subprocess

        repo = _init_repo(tmp_path / "assume-clean")

        def run(*argv):
            return subprocess.run(
                argv,
                cwd=repo,
                capture_output=True,
                env=_fixture_git_env(repo),
                **UTF8_TEXT,
            )

        (pathlib.Path(repo) / "cfg.txt").write_text("same\n")
        run("git", "add", "cfg.txt")
        run("git", "commit", "-qm", "base")
        run("git", "update-index", "--assume-unchanged", "cfg.txt")

        # Bit set, content identical -> nothing at risk.
        assert update_governance.hidden_worktree_edits(repo) == []

    def test_autocrlf_does_not_cause_a_false_refusal(self, tmp_path):
        """`--no-filters` would refuse an UNMODIFIED file on Windows.

        `core.autocrlf` is the normal Windows posture: the blob stores LF and the
        working file holds CRLF. `--no-filters` suppresses git's BUILT-IN
        conversions too, so a raw-byte hash mismatches for a file nobody touched
        and the check refuses -- disabling auto-update for any such checkout.

        Sets autocrlf explicitly so the Windows condition is reproduced on every
        platform; this failed only on the Windows shard when the flag was present.
        """
        import subprocess

        repo = _init_repo(tmp_path / "crlf")

        def run(*argv):
            return subprocess.run(
                argv,
                cwd=repo,
                capture_output=True,
                env=_fixture_git_env(repo),
                **UTF8_TEXT,
            )

        run("git", "config", "core.autocrlf", "true")
        cfg = pathlib.Path(repo) / "cfg.txt"
        cfg.write_bytes(b"line1\nline2\n")
        run("git", "add", "cfg.txt")
        run("git", "commit", "-qm", "base")

        # The checked-out (Windows) form, byte-for-byte what git would produce.
        cfg.write_bytes(b"line1\r\nline2\r\n")
        run("git", "update-index", "--assume-unchanged", "cfg.txt")

        # Nobody edited it -> must NOT refuse.
        assert update_governance.hidden_worktree_edits(repo) == []

        # And a real edit under the same config is still caught.
        cfg.write_bytes(b"line1\r\nCHANGED\r\n")
        assert update_governance.hidden_worktree_edits(repo) == ["cfg.txt"]

    def test_the_detection_does_not_write_the_index(self, tmp_path):
        """Read-only by construction, unlike `update-index --really-refresh`.

        The documented remedy surfaces the edit but WRITES the index. A check whose
        only purpose is to decide whether mutating the checkout is safe must not
        mutate it to find out, and unattended it would disturb the developer's own
        index state.
        """
        import subprocess

        repo = _init_repo(tmp_path / "assume-ro")

        def run(*argv):
            return subprocess.run(
                argv,
                cwd=repo,
                capture_output=True,
                env=_fixture_git_env(repo),
                **UTF8_TEXT,
            )

        target = pathlib.Path(repo) / "tracked.txt"
        target.write_text("original\n")
        run("git", "add", "tracked.txt")
        run("git", "commit", "-qm", "base")
        run("git", "update-index", "--assume-unchanged", "tracked.txt")
        target.write_text("MY PRECIOUS EDIT\n")

        index = pathlib.Path(repo) / ".git" / "index"
        before = index.read_bytes()
        assert update_governance.hidden_worktree_edits(repo) == ["tracked.txt"]
        assert index.read_bytes() == before, "the detection wrote the index"

    def test_an_unreadable_index_listing_is_unsafe(self, monkeypatch):
        """`None` means "could not determine", which the caller treats as unsafe."""
        monkeypatch.setattr(update_governance, "_git_probe", lambda *a, **k: None)
        assert update_governance.hidden_worktree_edits("/tmp/nope") is None

    def test_no_pinned_value_is_a_bare_program_name(self, monkeypatch):
        """A bare name hands the exec back to the agent-writable PATH.

        Round 17 resolved `git` itself off PATH; pinning `core.sshCommand` to the
        bare string "ssh" then let git resolve the TRANSPORT helper through the
        same PATH, which is most of that hole reopened. Every program-valued pin
        must therefore reach git as an absolute path.
        """
        monkeypatch.setattr(
            update_governance.platform_compat,
            "trusted_system_bin",
            lambda name: f"/usr/bin/{name}",
        )
        env = update_governance.git_neutralizer_env()
        count = int(env["GIT_CONFIG_COUNT"])
        seen = 0
        for i in range(count):
            key = env[f"GIT_CONFIG_KEY_{i}"].lower()
            if key in update_governance._PROGRAM_VALUED_PINS:
                seen += 1
                value = env[f"GIT_CONFIG_VALUE_{i}"]
                assert os.path.isabs(value), f"{key} pinned to a bare name: {value!r}"
        assert seen == len(update_governance._PROGRAM_VALUED_PINS)

    def test_an_unresolvable_helper_fails_closed(self, monkeypatch):
        """No trusted helper means do not run one at all.

        Falling back to the bare name would reinstate the PATH hazard, so the pin
        degrades to a value that cannot exec. For this path that is the right
        direction: pager/editor/gpg are unreachable anyway, and a transport helper
        that cannot be trusted should stop an unattended update.
        """
        monkeypatch.setattr(
            update_governance.platform_compat, "trusted_system_bin", lambda _n: None
        )
        env = update_governance.git_neutralizer_env()
        count = int(env["GIT_CONFIG_COUNT"])
        for i in range(count):
            key = env[f"GIT_CONFIG_KEY_{i}"].lower()
            if key in update_governance._PROGRAM_VALUED_PINS:
                assert env[f"GIT_CONFIG_VALUE_{i}"] == os.devnull

    def test_every_program_valued_pin_names_a_real_key(self):
        """The marker set must not drift from the pin list it annotates.

        A typo here silently stops resolving that key, which looks like nothing --
        the bare name just goes back to being PATH-resolved.
        """
        pinned = {k.lower() for k, _ in update_governance._GIT_EXEC_NEUTRALIZERS}
        assert update_governance._PROGRAM_VALUED_PINS <= pinned

    def test_url_insteadof_is_refused(self, tmp_path):
        """`url.<base>.insteadOf` redirects the fetch itself.

        Verified against real git: with the rewrite in repo config, a fetch given
        the honest URL EXPLICITLY still delivered the attacker's commit -- the
        rewrite applies below the URL argument. So validating the remote's URL
        cannot survive one of these existing, and the base is repo-chosen so there
        is no key name to pin.
        """
        repo = _init_repo(tmp_path / "rewrite")
        _git_config(repo, "--local", "url.https://evil.invalid/.insteadOf", "https://good.invalid/")
        reason = update_governance.repo_exec_config_reason(repo).lower()
        assert "insteadof" in reason

    def test_url_pushinsteadof_is_refused(self, tmp_path):
        """The push-side spelling is the same mechanism and the same class."""
        repo = _init_repo(tmp_path / "rewrite-push")
        _git_config(
            repo, "--local", "url.https://evil.invalid/.pushInsteadOf", "https://good.invalid/"
        )
        reason = update_governance.repo_exec_config_reason(repo).lower()
        assert "insteadof" in reason

    def test_loggable_path_is_always_utf8_encodable(self):
        """A surrogate from `os.fsdecode` must not survive into a log record.

        This is the property the whole helper exists for: `logging` cannot encode
        a lone surrogate, does not propagate the error, and DROPS the record --
        so the refusal line that proves an unattended update protected the user's
        file is exactly the one that would vanish.
        """
        # Precondition: the raw form really is un-encodable, so this test cannot
        # pass vacuously on a platform where fsdecode returned something tame --
        # and `os.fsdecode` itself RAISES on Windows, where UTF-8 +
        # surrogatepass cannot decode an invalid start byte, so it is inside the
        # guard rather than above it.
        try:
            surrogate = os.fsdecode(b"bad\xffname.txt")
            surrogate.encode("utf-8")
        except UnicodeDecodeError:
            pytest.skip("this platform cannot represent a non-UTF-8 name")
        except UnicodeEncodeError:
            pass
        else:
            pytest.skip("this platform's fsdecode produced an encodable name")

        safe = update_governance.loggable_path(surrogate)
        safe.encode("utf-8")  # must not raise
        # The operator sees the REAL on-disk byte, not a lossy replacement char.
        assert "\\xff" in safe
        assert "\ufffd" not in safe

    def test_loggable_path_leaves_ordinary_names_alone(self):
        """Escaping must cost nothing for names that are already encodable.

        Including non-ASCII ones -- mangling a CJK or accented path would trade a
        rare crash for everyday unreadability.
        """
        for name in (
            "pkg/mod.py",
            "\u7b14\u8bb0/\u8bf4\u660e.md",
            "caf\u00e9/re\u0301sume\u0301.txt",
        ):
            assert update_governance.loggable_path(name) == name

    def test_git_is_resolved_off_path(self, monkeypatch):
        """A planted `git` shim on PATH must not be what the update seam runs.

        `AGENTS.md` requires system tools to resolve through the trusted
        resolver; this path is the strongest case for it, because what git
        reports here decides which code gets installed and re-executed.
        """
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))

            class R:
                returncode = 0
                stdout = "main\n"
                stderr = ""

            return R()

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr(
            update_governance.platform_compat,
            "trusted_git_bin",
            lambda: "/trusted/bin/git",
        )
        update_governance._git_probe("/tmp/proj", "rev-parse", "HEAD")
        assert calls, "no git call was made"
        assert calls[0][0] == "/trusted/bin/git"

    def test_an_unresolvable_git_refuses_rather_than_falling_back(self, monkeypatch):
        """`None` from the resolver means DO NOT SPAWN -- not "use a bare name".

        Falling back to `"git"` would reinstate exactly the hazard the resolver
        exists to close, so the probe must answer "could not determine" (which
        every caller already treats as the unsafe direction) and spawn nothing.
        """
        spawned: list[list[str]] = []

        def fake_run(argv, **kwargs):
            spawned.append(list(argv))
            raise AssertionError("git was spawned despite an unresolvable binary")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr(update_governance.platform_compat, "trusted_git_bin", lambda: None)
        assert update_governance._git_probe("/tmp/proj", "rev-parse", "HEAD") is None
        assert spawned == []

    def test_replace_objects_are_disabled(self):
        """A replace ref makes a pinned OID resolve to different content.

        The reset target is pinned to an OID precisely so it cannot change; a
        `refs/replace/<oid>` entry defeats that by substituting what the id
        RESOLVES TO. The opt-out must be PRESENT (unlike the location variables,
        whose safe state is absence) because replace refs live in the repository.
        """
        env = update_governance.git_command_env()
        assert env.get("GIT_NO_REPLACE_OBJECTS") == "1"

    def test_a_replace_ref_cannot_substitute_the_reset_target(self, tmp_path):
        """End-to-end against real git, both directions.

        Without the variable the reset to the captured OID checks out the
        attacker's tree; with it, the honest one. Asserting the control direction
        keeps this from passing vacuously on a git that behaves differently.
        """
        import subprocess

        repo = _init_repo(tmp_path / "replace")

        def run(*argv, env=None):
            return subprocess.run(
                argv,
                cwd=repo,
                capture_output=True,
                env=env if env is not None else _fixture_git_env(repo),
                **UTF8_TEXT,
            )

        payload = pathlib.Path(repo) / "payload.txt"
        payload.write_text("good\n")
        run("git", "add", "payload.txt")
        run("git", "commit", "-qm", "good")
        good = run("git", "rev-parse", "HEAD").stdout.strip()

        payload.write_text("EVIL\n")
        run("git", "add", "payload.txt")
        run("git", "commit", "-qm", "evil")
        evil = run("git", "rev-parse", "HEAD").stdout.strip()
        run("git", "reset", "-q", "--hard", good)

        made = run("git", "replace", good, evil)
        if made.returncode != 0:
            pytest.skip(f"git replace unavailable: {made.stderr[:200]}")

        # Control: the sanitized fixture env with the opt-out REMOVED, so the two
        # probes differ only in the thing under test.
        control = {k: v for k, v in _fixture_git_env(repo).items() if k != "GIT_NO_REPLACE_OBJECTS"}
        run("git", "reset", "-q", "--hard", good, env=control)
        if payload.read_text() != "EVIL\n":
            pytest.skip("this git does not honour replace refs on reset here")

        # Protected: the seam's env.
        run("git", "reset", "-q", "--hard", good, env=update_governance.git_command_env())
        assert payload.read_text() == "good\n"

    def test_submodule_recurse_is_pinned_off(self):
        """`submodule.recurse` widens the reset past the superproject tree.

        Pinned rather than refused because the pin was verified to work -- see
        `test_the_submodule_pin_actually_protects_submodule_edits`, which
        exercises real git rather than asserting the constant.
        """
        pins = dict(update_governance._GIT_BLAST_RADIUS_PINS)
        assert pins.get("submodule.recurse") == "false"
        # It execs nothing, so it must NOT be smuggled into the exec list, whose
        # membership criterion is "git may exec this value".
        assert "submodule.recurse" not in {
            k.lower() for k, _ in update_governance._GIT_EXEC_NEUTRALIZERS
        }

    def test_the_pinning_env_carries_every_list(self):
        """A second pin list is only useful if the env builder actually folds it in.

        `GIT_CONFIG_COUNT` must cover both lists -- an undercount silently drops
        the trailing pins, which is the failure this guards.
        """
        env = update_governance.git_neutralizer_env()
        total = len(update_governance._GIT_EXEC_NEUTRALIZERS) + len(
            update_governance._GIT_BLAST_RADIUS_PINS
        )
        assert env["GIT_CONFIG_COUNT"] == str(total)
        keys = {env[f"GIT_CONFIG_KEY_{i}"] for i in range(total)}
        assert "submodule.recurse" in keys
        for key, _ in update_governance._GIT_EXEC_NEUTRALIZERS:
            assert key in keys

    def test_the_submodule_pin_actually_protects_submodule_edits(self, tmp_path):
        """End-to-end against real git: the reset must not eat submodule work.

        The hazard needs two repo settings together: `submodule.recurse` makes
        `reset --hard` recurse, and `submodule.<name>.ignore=all` makes
        `git status --porcelain` report a COMPLETELY CLEAN tree while the
        submodule holds uncommitted edits -- so the work-tree refusal upstream of
        the reset cannot see it. This asserts the control direction too: without
        the pin the edit is destroyed, so the test cannot pass vacuously.
        """
        import subprocess

        def run(*argv, cwd, env=None):
            return subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                env=env if env is not None else _fixture_git_env(str(cwd)),
                **UTF8_TEXT,
            )

        sub = tmp_path / "sub"
        sub.mkdir()
        run("git", "init", "-q", ".", cwd=sub)
        (sub / "s.txt").write_text("v1\n")
        run("git", "add", "s.txt", cwd=sub)
        run("git", "commit", "-qm", "s1", cwd=sub)

        super_ = tmp_path / "super"
        super_.mkdir()
        run("git", "init", "-q", ".", cwd=super_)
        add = run(
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub),
            "sub",
            cwd=super_,
        )
        if add.returncode != 0:
            pytest.skip(f"submodule add unavailable here: {add.stderr[:200]}")
        run("git", "commit", "-qm", "add submodule", cwd=super_)
        base = run("git", "rev-parse", "HEAD", cwd=super_).stdout.strip()
        (super_ / "top.txt").write_text("top\n")
        run("git", "add", "top.txt", cwd=super_)
        run("git", "commit", "-qm", "top", cwd=super_)
        target = run("git", "rev-parse", "HEAD", cwd=super_).stdout.strip()

        run("git", "config", "submodule.recurse", "true", cwd=super_)
        run("git", "config", "submodule.sub.ignore", "all", cwd=super_)

        precious = "PRECIOUS LOCAL WORK\n"

        def reset_with(env):
            run("git", "reset", "-q", "--hard", base, cwd=super_)
            (super_ / "sub" / "s.txt").write_text(precious)
            # The refusal upstream of the reset reads this, and it is why the pin
            # is needed: the tree looks clean.
            status = run("git", "status", "--porcelain", cwd=super_).stdout.strip()
            run("git", "reset", "-q", "--hard", target, cwd=super_, env=env)
            return status, (super_ / "sub" / "s.txt").read_text()

        # Control: the SAME env with only this pin flipped back on. It cannot be
        # `_fixture_git_env` alone -- that already builds from
        # `git_command_env()`, so it carries the pin under test and the control
        # would be silently pre-protected, passing the test vacuously.
        protected = update_governance.git_command_env()
        recursing = dict(protected)
        count = int(recursing["GIT_CONFIG_COUNT"])
        flipped = [
            i for i in range(count) if recursing[f"GIT_CONFIG_KEY_{i}"] == "submodule.recurse"
        ]
        assert flipped, "the pin under test is not in the env at all"
        for i in flipped:
            recursing[f"GIT_CONFIG_VALUE_{i}"] = "true"

        status, unrecursed = reset_with(recursing)
        if unrecursed == precious:
            pytest.skip(
                "this git does not recurse the reset here, so the pin's effect "
                "cannot be demonstrated"
            )
        assert status == "", f"expected a clean-looking tree, got {status!r}"

        # Protected: the seam's own env pins submodule.recurse=false.
        _, pinned = reset_with(protected)
        assert pinned == precious

    def test_recent_objects_hook_is_refused(self, tmp_path):
        """`gc.recentObjectsHook` is a literal name git execs "using the shell".

        By the pin list's own membership criterion it is an exec key, but it is
        REFUSED rather than pinned because it is multi-valued -- see
        `test_a_pin_cannot_suppress_the_recent_objects_hook`, which reproduces
        the pin's failure instead of reasoning about it.
        """
        repo = _init_repo(tmp_path / "gchook")
        _git_config(repo, "--local", "gc.recentObjectsHook", "sh -c ':'")
        reason = update_governance.repo_exec_config_reason(repo).lower()
        assert "recentobjectshook" in reason

    def test_a_pin_cannot_suppress_the_recent_objects_hook(self, tmp_path):
        """The reason this key is refused instead of pinned.

        git documents the key as multi-valued ("Multiple hooks are supported"),
        so an empty value from a higher-priority scope is an ADDITIONAL list
        entry rather than an override: the repository's own value is still in the
        list git would act on. `--get` hides this by reporting only the last
        value, which is exactly how an ineffective pin looks like a working one --
        the `core.gitProxy` mistake. This test fails if the key is ever
        "handled" by moving it back to the pin list.
        """
        import subprocess

        repo = _init_repo(tmp_path / "gchook-pin")
        _git_config(repo, "--local", "gc.recentObjectsHook", "sh -c ':'")

        # Built from the fixture env so the pin is the ONLY thing added -- an
        # inherited GIT_DIR would otherwise point this probe at another repo.
        env = _fixture_git_env(repo)
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "gc.recentObjectsHook",
                "GIT_CONFIG_VALUE_0": "",
            }
        )
        out = subprocess.run(
            ["git", "config", "--get-all", "gc.recentObjectsHook"],
            cwd=repo,
            capture_output=True,
            env=env,
            **UTF8_TEXT,
        ).stdout.splitlines()

        # The repo's value survives the pin -- so the pin is not a fix.
        assert "sh -c ':'" in out, out
        assert "gc.recentobjectshook" not in {
            k.lower() for k, _ in update_governance._GIT_EXEC_NEUTRALIZERS
        }

    def test_tls_verification_off_is_refused(self, tmp_path):
        """`http.sslVerify=false` in the checkout makes a forged update genuine.

        Combined with a hostile proxy there is nothing left distinguishing the real
        download from an attacker's, and the update path then installs and re-execs
        whatever arrived.
        """
        repo = _init_repo(tmp_path / "tls")
        _git_config(repo, "--local", "http.sslVerify", "false")
        reason = update_governance.repo_exec_config_reason(repo).lower()
        assert "sslverify" in reason

    def test_per_url_tls_and_proxy_spellings_are_refused(self, tmp_path):
        """The per-URL forms are repo-NAMED, so they cannot be pinned.

        Same reason `credential.<url>.helper` is refused: there is no fixed key to
        override. Both spellings are covered so they cannot diverge.
        """
        for key, needle in (
            ("http.https://example.invalid/.sslVerify", "sslverify"),
            ("http.https://example.invalid/.proxy", "proxy"),
            ("http.proxy", "proxy"),
            ("http.sslCAInfo", "sslcainfo"),
        ):
            repo = _init_repo(tmp_path / f"tls-{needle}-{abs(hash(key)) % 9999}")
            _git_config(repo, "--local", key, "x")
            reason = update_governance.repo_exec_config_reason(repo).lower()
            assert needle in reason, (key, reason)

    def test_ordinary_http_settings_are_not_refused(self, tmp_path):
        """Only TRUST-relevant keys refuse.

        `http.postBuffer` and friends are performance knobs; refusing on any
        `http.*` key would stop updates for checkouts that merely tuned one.
        """
        repo = _init_repo(tmp_path / "http-benign")
        _git_config(repo, "--local", "http.postBuffer", "524288000")
        assert update_governance.repo_exec_config_reason(repo) == ""

    def test_remote_vcs_helper_is_refused(self, tmp_path):
        """`remote.<name>.vcs` makes the FETCH exec a repo-chosen program.

        git runs `git-remote-<value>` as the transport helper, resolved through
        PATH -- verified: with `remote.origin.vcs=evil` and `git-remote-evil` on
        PATH, the helper executed.

        Refused rather than pinned, and that is not a stylistic choice: an empty
        pin suppresses the repo's helper but leaves git treating "" as a helper
        NAME, so every fetch dies with `remote helper '' aborted session`. Pinning
        would disable the update path rather than protect it.
        """
        repo = _init_repo(tmp_path / "vcs")
        _git_config(repo, "--local", "remote.origin.vcs", "evil")
        reason = update_governance.repo_exec_config_reason(repo).lower()
        assert "vcs" in reason

    def test_remote_vcs_is_not_in_the_pin_list(self):
        """A pin here would break fetching, so it must not appear as one."""
        pinned = {k.lower() for k, _ in update_governance._GIT_EXEC_NEUTRALIZERS}
        assert not any(k.endswith(".vcs") for k in pinned)

    def test_namespaced_credential_helper_is_refused(self, tmp_path):
        """`credential.<url>.helper` is per-URL, so pinning the bare key misses it.

        The pinned `credential.helper` does not reach a per-URL form, and the URL
        is repo-chosen, so there is no key name to override — refuse instead.
        """
        repo = _init_repo(tmp_path / "cred")
        _git_config(repo, "--local", "credential.https://example.invalid.helper", "!sh -c ':'")
        assert "credential" in update_governance.repo_exec_config_reason(repo)

    def test_unreadable_config_refuses(self, monkeypatch):
        """Cannot prove a repo driver-free, so do not proceed."""
        monkeypatch.setattr("kiro_crew.platform.update_governance._git_probe", lambda *a, **k: None)
        assert update_governance.repo_exec_config_reason("/proj") != ""

    def test_driver_regex_agrees_with_the_worktree_gate(self):
        """The two copies of this rule must not drift apart.

        `worktree._FILTER_KEY_RE` guards the same class for `worktree add`; this
        module carries its own because `platform/` must not import a dashboard
        handler. Parity is asserted rather than assumed.
        """
        from kiro_crew.dashboard.handlers.worktree import _FILTER_KEY_RE

        for key in (
            "filter.evil.process",
            "filter.evil.smudge",
            "filter.evil.clean",
            "filter.a.b.smudge",
        ):
            assert _FILTER_KEY_RE.match(key), key
            assert update_governance._REPO_EXEC_DRIVER_RE.match(key), key
        for key in ("filter.evil.required", "core.fsmonitor", "diff.external"):
            assert not _FILTER_KEY_RE.match(key), key
            assert not update_governance._REPO_EXEC_DRIVER_RE.match(key), key


class TestTracksUpstream:
    """The apply resets to `origin/<branch>`; the check measures `@{u}`.

    When those are not the same ref the check measures one thing and a `--hard`
    reset applies another, so the gap is lost commits. BOTH halves of the
    upstream are checked, and each has its own case here because either alone
    leaves the gap open.
    """

    def test_branch_tracking_the_same_ref_is_accepted(self, tmp_path):
        repo = _init_repo(tmp_path / "o")
        _git_config(repo, "--local", "branch.main.remote", "origin")
        _git_config(repo, "--local", "branch.main.merge", "refs/heads/main")
        assert update_governance.tracks_upstream(repo, "main")

    def test_branch_tracking_another_remote_is_refused(self, tmp_path):
        """The fork case: `main` tracks `upstream`, `origin` is a stale fork."""
        repo = _init_repo(tmp_path / "u")
        _git_config(repo, "--local", "branch.main.remote", "upstream")
        _git_config(repo, "--local", "branch.main.merge", "refs/heads/main")
        assert not update_governance.tracks_upstream(repo, "main")

    def test_branch_tracking_another_branch_is_refused(self, tmp_path):
        """Right remote, wrong branch: `@{u}` is `origin/other`, reset is `origin/main`."""
        repo = _init_repo(tmp_path / "b")
        _git_config(repo, "--local", "branch.main.remote", "origin")
        _git_config(repo, "--local", "branch.main.merge", "refs/heads/other")
        assert not update_governance.tracks_upstream(repo, "main")

    def test_untracked_branch_is_refused(self, tmp_path):
        """No upstream at all means the check had nothing to compare either."""
        repo = _init_repo(tmp_path / "n")
        assert not update_governance.tracks_upstream(repo, "main")

    def test_remote_without_merge_is_refused(self, tmp_path):
        """A half-configured upstream is not an upstream."""
        repo = _init_repo(tmp_path / "h")
        _git_config(repo, "--local", "branch.main.remote", "origin")
        assert not update_governance.tracks_upstream(repo, "main")

    def test_detached_head_is_refused(self, tmp_path):
        repo = _init_repo(tmp_path / "d")
        assert not update_governance.tracks_upstream(repo, "HEAD")
        assert not update_governance.tracks_upstream(repo, "")


class TestFixtureGitHygiene:
    """The fixtures must not run anything the developer's environment supplies.

    `git init` COPIES a template directory's hooks into the new repo and the
    following `git commit` runs them, so an inherited `GIT_TEMPLATE_DIR` turned
    running this suite into host-side execution.
    """

    def test_fixture_env_disables_templates_and_hooks(self, tmp_path):
        repo = _init_repo(tmp_path / "hyg")
        env = _fixture_git_env(repo)
        keys = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert env["GIT_TEMPLATE_DIR"] == ""
        assert keys["init.templateDir"] == ""
        assert keys["core.hooksPath"] == os.devnull

    def test_a_template_hook_does_not_run(self, tmp_path, monkeypatch):
        """The finding, reproduced end to end against real git."""
        import subprocess

        marker = tmp_path / "TEMPLATE_HOOK_RAN"
        template = tmp_path / "tpl" / "hooks"
        template.mkdir(parents=True)
        hook = template / "post-commit"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o755)

        # Control: with the template inherited, the hook DOES run -- otherwise
        # this test would pass vacuously on a git that never ran it.
        loose = tmp_path / "loose"
        loose.mkdir()
        subprocess.run(
            ["git", "init", "-q", f"--template={template.parent}", "."],
            cwd=loose,
            env=update_governance.git_command_env(),
            capture_output=True,
            **UTF8_TEXT,
        )
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "x"],
            cwd=loose,
            env={
                **update_governance.git_command_env(),
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            },
            capture_output=True,
            **UTF8_TEXT,
        )
        if not marker.exists():
            pytest.skip("this git did not run the template post-commit hook")
        marker.unlink()

        # The fixture helper, with GIT_TEMPLATE_DIR pointing at the same hooks.
        # `monkeypatch.setenv` so a developer's pre-existing GIT_TEMPLATE_DIR is
        # RESTORED afterwards -- a bare `os.environ` write plus `pop` would delete
        # a value this test never owned and leak that into later tests.
        monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template.parent))
        _init_repo(tmp_path / "guarded")
        assert not marker.exists(), "fixture ran an inherited template hook"


class TestCommitsAhead:
    """Local commits must survive an unattended `reset --hard`.

    The two checks that look like they cover this do not: `git status
    --porcelain` reports working-tree edits rather than commits, and the
    availability probe is satisfied by a difference in either direction.
    """

    def _repo_with_upstream(self, tmp_path, name):
        repo = _init_repo(tmp_path / name)
        _git_config(repo, "--local", "branch.main.remote", "origin")
        _git_config(repo, "--local", "branch.main.merge", "refs/heads/main")
        return repo

    def test_level_checkout_is_zero(self, tmp_path):
        import subprocess

        repo = self._repo_with_upstream(tmp_path, "level")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=_fixture_git_env(repo),
            capture_output=True,
            **UTF8_TEXT,
        ).stdout.strip()
        _git_update_ref(repo, "refs/remotes/origin/main", head)
        assert update_governance.commits_ahead(repo, "origin/main") == 0

    def test_local_commit_is_counted(self, tmp_path):
        """The finding: a committed change is invisible to the other two checks."""
        import subprocess

        repo = self._repo_with_upstream(tmp_path, "ahead")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=_fixture_git_env(repo),
            capture_output=True,
            **UTF8_TEXT,
        ).stdout.strip()
        _git_update_ref(repo, "refs/remotes/origin/main", head)
        done = subprocess.run(
            ["git", "commit", "-q", "--no-verify", "--allow-empty", "-m", "local"],
            cwd=repo,
            env=_fixture_git_env(repo),
            capture_output=True,
            **UTF8_TEXT,
        )
        assert done.returncode == 0, done.stderr
        assert update_governance.commits_ahead(repo, "origin/main") == 1

        # And the checks it exists to backstop really are blind to it.
        assert (
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                env=_fixture_git_env(repo),
                capture_output=True,
                **UTF8_TEXT,
            ).stdout.strip()
            == ""
        ), "status sees a local commit, so this guard would be redundant"

    def test_missing_upstream_ref_is_unknown_not_zero(self, tmp_path):
        """`None`, so the caller refuses rather than reading it as 'nothing to lose'."""
        repo = self._repo_with_upstream(tmp_path, "noref")
        assert update_governance.commits_ahead(repo, "origin/main") is None

    def test_empty_upstream_is_unknown(self, tmp_path):
        repo = self._repo_with_upstream(tmp_path, "det")
        assert update_governance.commits_ahead(repo, "") is None

    def test_an_oid_upstream_is_accepted(self, tmp_path):
        """The caller passes the captured OID, so that form must work.

        And it is the form that matters: counting against a ref while resetting
        to an OID is the race this signature exists to remove.
        """
        import subprocess

        repo = self._repo_with_upstream(tmp_path, "oid")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=_fixture_git_env(repo),
            capture_output=True,
            **UTF8_TEXT,
        ).stdout.strip()
        assert update_governance.commits_ahead(repo, head) == 0


class TestGitCommandEnv:
    """Inherited git LOCATION variables retarget every command in the sequence.

    This is the environment twin of the `core.worktree` redirect: an exported
    `GIT_DIR` points the calls at unrelated metadata while `cwd` still says the
    project directory. It has to be ABSENT, which is why the env is built rather
    than merged over `os.environ` -- a merge can only add or overwrite keys.
    """

    def test_location_vars_are_stripped(self, monkeypatch):
        for var in sorted(update_governance._GIT_LOCATION_VARS):
            monkeypatch.setenv(var, "/attacker/controlled")
        env = update_governance.git_command_env()
        leaked = sorted(v for v in update_governance._GIT_LOCATION_VARS if v in env)
        assert leaked == [], leaked

    def test_the_exec_pins_are_still_present(self, monkeypatch):
        """Stripping must not have dropped the neutralizers it composes with."""
        monkeypatch.setenv("GIT_DIR", "/attacker/controlled")
        env = update_governance.git_command_env()
        keys = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert keys["core.fsmonitor"] == "false"
        assert keys["core.hooksPath"] == os.devnull

    def test_unrelated_environment_survives(self, monkeypatch):
        """PATH and friends must come through, or git does not run at all."""
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
        env = update_governance.git_command_env()
        assert env["PATH"] == "/usr/bin"
        assert env["SOME_UNRELATED_VAR"] == "keep-me"

    def test_a_merge_would_not_have_worked(self):
        """Documents WHY this builds instead of merging, as an assertion.

        `git_neutralizer_env` names no location variable, so the old
        `{**os.environ, **git_neutralizer_env()}` form could not have removed an
        inherited one. If a future change tries to close this by adding a pin
        there instead, this fails and points at the right mechanism.
        """
        assert not (
            set(update_governance.git_neutralizer_env()) & update_governance._GIT_LOCATION_VARS
        )

    def test_an_inherited_git_dir_does_not_retarget_a_real_probe(self, tmp_path, monkeypatch):
        """End to end against real git, with the control direction asserted."""
        import subprocess

        repo = _init_repo(tmp_path / "target")
        decoy = _init_repo(tmp_path / "decoy")

        def _toplevel(env):
            done = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=repo,
                env=env,
                capture_output=True,
                **UTF8_TEXT,
            )
            return (done.stdout or "").strip()

        hijacked = _toplevel({**os.environ, "GIT_DIR": str(pathlib.Path(decoy) / ".git")})
        if str(decoy) not in hijacked:
            pytest.skip("this git does not honour GIT_DIR here")

        # `monkeypatch.setenv` so a pre-existing GIT_DIR is RESTORED: a bare
        # write plus `pop` would delete a value this test never owned.
        monkeypatch.setenv("GIT_DIR", str(pathlib.Path(decoy) / ".git"))
        assert str(decoy) not in _toplevel(update_governance.git_command_env())
