"""``kirocrew update --release`` on a git checkout detached at a release tag.

A cloud crew launched from a packaged install is ``git clone --depth 1 --branch
<release tag>``. Its history shares nothing with ``main``, so the bare update's
fetch of ``main`` pulls the whole history and its divergence guard then refuses.
``--release VERSION`` moves such a checkout to the release tag of VERSION instead,
fetching only that tag. These tests run real git against a local origin whose
release tags are not on ``main``; only the steps after the reset (frontend build,
release install, kiro-cli, node, the memory snapshot) are stubbed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew import cli_server, kiro_cli
from kiro_crew.frontend import FrontendBuildResult
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        encoding="utf-8",
        errors="replace",
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", f"{name}.txt")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _has_object(repo: Path, oid: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{oid}^{{commit}}"],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    return probe.returncode == 0


@pytest.fixture
def origin(tmp_path: Path) -> dict[str, str]:
    """A bare origin: ``main`` moved on past a release line whose tags it never got.

    ``v0.12.2`` is on the fork point; ``v0.13.0-rc.5`` and then ``v0.13.1`` (which
    shares its commit with ``v0.13.1-rc.2``) are on the release line only.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    oids = {"fork": _commit(work, "fork")}
    _git(work, "tag", "-a", "v0.12.2", "-m", "v0.12.2")
    _git(work, "checkout", "-q", "-b", "release")
    oids["rc5"] = _commit(work, "rc5")
    _git(work, "tag", "-a", "v0.13.0-rc.5", "-m", "v0.13.0-rc.5")
    oids["v0131"] = _commit(work, "v0131")
    _git(work, "tag", "-a", "v0.13.1-rc.2", "-m", "v0.13.1-rc.2")
    _git(work, "tag", "-a", "v0.13.1", "-m", "v0.13.1")
    _git(work, "checkout", "-q", "main")
    for n in range(4):
        oids["main"] = _commit(work, f"main{n}")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    oids["url"] = bare.as_uri()
    oids["work"] = str(work)
    return oids


@pytest.fixture
def crew(monkeypatch, tmp_path: Path):
    """Clone *ref* the way the launch template does and point the updater at it.

    The clone is depth-1 like the launch's; ``depth=None`` makes a complete one.
    """

    def _clone(url: str, ref: str, depth: int | None = 1) -> Path:
        proj = tmp_path / "crew"
        shape = ["--depth", str(depth)] if depth is not None else []
        _git(tmp_path, "clone", "-q", *shape, "--branch", ref, "--", url, str(proj))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        return proj

    # The updater takes the git lane only for the checkout it runs from.
    monkeypatch.setattr(
        "kiro_crew.platform.update_capability.running_from_checkout", lambda root, **kw: True
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.update_blocked_reason", lambda url: ""
    )
    monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: None)
    monkeypatch.setattr(cli_server, "build_frontend_sync", lambda p: FrontendBuildResult("built"))
    monkeypatch.setattr("kiro_crew.cli._ensure_node", lambda *a: None)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr(cli_server, "_snapshot_memory_or_exit", lambda: None)
    monkeypatch.setattr(cli_server, "_refresh_agent_config", lambda proj: None)
    monkeypatch.setattr(cli_server, "_revalidate_gateway_update_check", lambda: None)
    installed: list[object] = []

    def _apply(release: object) -> int:
        installed.append(release)
        return 0

    monkeypatch.setattr(cli_server.release_apply, "apply_release", _apply)
    _clone.installed = installed  # type: ignore[attr-defined]
    return _clone


def _refs(proj: Path) -> set[str]:
    return set(_git(proj, "for-each-ref", "--format=%(refname)").split())


def test_release_moves_a_tag_checkout_to_that_tag_and_fetches_only_it(origin, crew, capsys) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5")

    cli_server._update(release="0.13.1")

    out = capsys.readouterr().out
    assert "updating to release tag v0.13.1" in out
    assert "origin/main" not in out
    assert _git(proj, "rev-parse", "HEAD") == origin["v0131"]
    refs = _refs(proj)
    # The tag is written, so the next update recognises the checkout.
    assert "refs/tags/v0.13.1" in refs
    # Nothing of main's history came down, and the clone stays depth-1.
    assert "refs/remotes/origin/main" not in refs
    assert not _has_object(proj, origin["main"])
    assert not _has_object(proj, origin["fork"])
    assert _git(proj, "rev-parse", "--is-shallow-repository") == "true"
    assert crew.installed, "the post-reset install step was not reached"
    assert "✅ Kiro Crew updated!" in out


def test_release_keeps_a_complete_clone_complete(origin, crew, capsys) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5", depth=None)
    assert _git(proj, "rev-parse", "--is-shallow-repository") == "false"

    cli_server._update(release="0.13.1")

    assert "updating to release tag v0.13.1" in capsys.readouterr().out
    assert _git(proj, "rev-parse", "HEAD") == origin["v0131"]
    # `--depth 1` is for a shallow clone only: it would make this one shallow.
    assert _git(proj, "rev-parse", "--is-shallow-repository") == "false"
    assert crew.installed, "the post-reset install step was not reached"


@pytest.mark.parametrize(
    ("start", "release"),
    [
        ("v0.13.0-rc.5", "0.13.0-rc.5"),  # the release it is on
        ("v0.13.0-rc.5", "0.12.2"),  # an older one
        # HEAD carries v0.13.1-rc.2 AND v0.13.1: the rc is older than one of them.
        ("v0.13.1", "0.13.1-rc.2"),
    ],
    ids=["same", "older", "older-than-one-tag-at-head"],
)
def test_release_never_moves_to_an_older_or_the_same_release(
    origin, crew, capsys, start, release
) -> None:
    proj = crew(origin["url"], start)
    head, refs = _git(proj, "rev-parse", "HEAD"), _refs(proj)

    cli_server._update(release=release)

    assert "already on release" in capsys.readouterr().out
    assert _git(proj, "rev-parse", "HEAD") == head
    assert _refs(proj) == refs
    assert not crew.installed


def test_a_newer_tag_on_the_same_commit_is_up_to_date_when_the_tree_check_times_out(
    origin, crew, capsys, monkeypatch
) -> None:
    proj = crew(origin["url"], "v0.13.1-rc.2")
    # Launched before the stable tag was cut on the rc's commit.
    _git(proj, "tag", "-d", "v0.13.1")
    assert {r for r in _refs(proj) if r.startswith("refs/tags/")} == {"refs/tags/v0.13.1-rc.2"}
    head = _git(proj, "rev-parse", "HEAD")
    real_run = subprocess.run

    def _run(argv, *args, **kwargs):
        # Only the tree check times out: on this commit it would answer before the guard.
        if list(argv[:2]) == ["git", "diff"] and "--quiet" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)

    cli_server._update(release="0.13.1")

    out = capsys.readouterr().out
    assert "git diff timed out" in out
    assert "Already up to date" in out
    assert _git(proj, "rev-parse", "HEAD") == head
    assert not crew.installed
    assert "Kiro Crew updated!" not in out


@pytest.mark.parametrize("release", ["0.13.2", "0.14.0.dev3", "junk", ""])
def test_release_with_no_tag_on_origin_exits_1_touching_nothing(
    origin, crew, capsys, release
) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5")
    head, refs = _git(proj, "rev-parse", "HEAD"), _refs(proj)

    with pytest.raises(SystemExit) as exc:
        cli_server._update(release=release)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    if release == "0.13.2":
        assert "origin has none of the release tags" in out and "v0.13.2" in out
    else:
        assert "names no release tag" in out
    assert _git(proj, "rev-parse", "HEAD") == head
    assert _refs(proj) == refs


def test_a_local_tag_of_that_name_is_never_overwritten(origin, crew, capsys) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5")
    head = _git(proj, "rev-parse", "HEAD")
    # A local v0.13.1 naming some other commit than origin's.
    other = _git(proj, "commit-tree", "-p", head, "-m", "local", f"{head}^{{tree}}")
    _git(proj, "tag", "v0.13.1", other)

    with pytest.raises(SystemExit) as exc:
        cli_server._update(release="0.13.1")

    assert exc.value.code == 1
    assert "git fetch failed" in capsys.readouterr().out
    assert _git(proj, "rev-parse", "refs/tags/v0.13.1") == other
    assert _git(proj, "rev-parse", "HEAD") == head


def test_a_commit_made_at_the_prompt_is_refused_not_reset(
    origin, crew, capsys, monkeypatch
) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5")
    (proj / "rc5.txt").write_text("edited\n", encoding="utf-8")
    committed: list[str] = []

    def _commit_then_continue(prompt: str = "") -> str:
        # The natural rescue: commit the listed change in another terminal.
        _git(proj, "commit", "-q", "-am", "rescue my edit")
        committed.append(_git(proj, "rev-parse", "HEAD"))
        return "y"

    monkeypatch.setattr("builtins.input", _commit_then_continue)

    with pytest.raises(SystemExit) as exc:
        cli_server._update(release="0.13.1")

    assert exc.value.code == 1
    assert "Refusing to reset" in capsys.readouterr().out
    assert committed and _git(proj, "rev-parse", "HEAD") == committed[0]
    assert not crew.installed


@pytest.mark.parametrize(
    ("force", "rerun"),
    [
        (False, "kirocrew update --release 0.13.1"),
        (True, "kirocrew update --release 0.13.1 --force"),
    ],
    ids=["plain", "force"],
)
def test_the_stash_retry_at_the_prompt_keeps_release_and_force(
    origin, crew, capsys, monkeypatch, force, rerun
) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5")
    head = _git(proj, "rev-parse", "HEAD")
    (proj / "rc5.txt").write_text("edited\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    with pytest.raises(SystemExit) as exc:
        cli_server._update(force=force, release="0.13.1")

    assert exc.value.code == 0
    lines = capsys.readouterr().out.splitlines()
    # Without `--release` the printed retry would follow main, not this release.
    stash = lines.index("      git stash")
    assert lines[stash + 1 : stash + 3] == [f"      {rerun}", "      git stash pop"]
    assert _git(proj, "rev-parse", "HEAD") == head
    assert (proj / "rc5.txt").read_text(encoding="utf-8") == "edited\n"
    assert not crew.installed


@pytest.mark.parametrize(
    ("release", "retry"),
    [("0.13.1", "`kirocrew update --release 0.13.1`"), (None, "`kirocrew update`")],
    ids=["release", "bare"],
)
def test_the_fetch_timeout_retry_keeps_release(
    origin, crew, capsys, monkeypatch, release, retry
) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5")
    head, refs = _git(proj, "rev-parse", "HEAD"), _refs(proj)
    real_run = subprocess.run

    def _run(argv, *args, **kwargs):
        # Only the update's fetch times out; every other git call is real.
        if list(argv[:2]) == ["git", "fetch"]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(SystemExit) as exc:
        cli_server._update(release=release)

    assert exc.value.code == 1
    assert f"Run {retry} again to retry the fetch." in capsys.readouterr().out
    assert _git(proj, "rev-parse", "HEAD") == head
    assert _refs(proj) == refs


def test_a_branch_checkout_ignores_release(origin, crew, capsys) -> None:
    proj = crew(origin["url"], "main")
    work = Path(origin["work"])
    tip = _commit(work, "main-next")
    _git(work, "push", "-q", origin["url"], "main")

    cli_server._update(release="0.13.1")

    out = capsys.readouterr().out
    assert "tag v0.13.1" not in out
    assert _git(proj, "rev-parse", "HEAD") == tip
    assert "refs/tags/v0.13.1" not in _refs(proj)


def test_the_bare_update_on_a_tag_checkout_still_follows_main(origin, crew, capsys) -> None:
    proj = crew(origin["url"], "v0.13.0-rc.5")
    head = _git(proj, "rev-parse", "HEAD")

    with pytest.raises(SystemExit) as exc:
        cli_server._update()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "updating from origin/main" in out
    assert "diverged from origin/main" in out
    assert _git(proj, "rev-parse", "HEAD") == head
