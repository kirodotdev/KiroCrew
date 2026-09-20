"""Create of a release-channel worktree, and how the fleet publishes it.

Two classes of defect are guarded here, both of which produce a tree that looks
healthy:

* **Resolving stale.** ``create`` must fetch BEFORE resolving. The
  background refresher keeps the fleet ROWS current, but it runs on its own
  schedule — a mutation that resolved first would pin whatever the tag mirror held
  at that moment and report the result as the channel tip.
* **Adopting a tree that is not ours.** ``release-channel-stable`` is a reserved
  name, and a user's own branch checkout under that name must never have its HEAD
  moved. Adoption requires the SHAPE (detached), never the name.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    release_channel_pin,
    repository,
    runtime,
    worktree_ops,
)

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

_TIP_OID = "1" * 40
_OTHER_OID = "2" * 40
_TAG_OBJECT_OID = "3" * 40


class _Git:
    """Records every argv and answers the reads these ops make."""

    def __init__(
        self,
        *,
        tags=("v0.5.0",),
        head="old-oid",
        head_tag="v0.4.9",
        status="",
        fail=None,
        config=None,
        worktree_config=False,
        gitdir="/nonexistent-gitdir",
        advertised=None,
        peeled=None,
    ):
        self.calls: list[list[str]] = []
        self._tags = list(tags)
        self._head = head
        # What the mirror answers when narrowed to HEAD: the release the worktree
        # is ACTUALLY on. Deliberately a different release from the lane tip in
        # `tags`, because a fake that answered with the tip could not tell a row
        # showing its own version from one showing the tip's.
        self._head_tag = head_tag
        self._status = status
        self._fail = fail or {}
        # Config keys per scope, as `--name-only --list` prints them. A scope
        # ABSENT here answers the way git answers a scope holding nothing --
        # non-zero, no output, no error -- which is a real state a fresh
        # `config.worktree` is in, and distinct from a probe that failed.
        self._config = {"--local": ["core.bare"]} if config is None else config
        self._worktree_config = worktree_config
        # Where `rev-parse --absolute-git-dir` points. The production probe reads the
        # worktree scope only when `<gitdir>/config.worktree` EXISTS, because the
        # extension being on does not create the file and probing an absent one is a
        # fatal git error. Default is a path that does not exist, which is the state
        # of a repo that never enabled the extension.
        self._gitdir = gitdir
        # What the REMOTE advertises per tag, as `ls-remote` prints it. Default: every
        # tag in `_tags` advertises the same oid `rev-parse` answers with, i.e. a
        # checkout whose mirror agrees with the remote. `peeled` adds `^{}` lines, which
        # is what an ANNOTATED tag looks like -- plain entry = tag object, peel = commit.
        self._advertised = {t: _TIP_OID for t in self._tags} if advertised is None else advertised
        self._peeled = peeled or {}
        self.modes: list[str] = []

    async def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        self.modes.append(kw.get("mode", "standard"))
        for needle, result in self._fail.items():
            if needle in cmd:
                return result
        if "rev-parse" in cmd and "--absolute-git-dir" in cmd:
            return 0, f"{self._gitdir}\n", ""
        if "rev-parse" in cmd and "--show-toplevel" in cmd:
            # A clean checkout's working tree is the directory `-C` named; the
            # redirect test overrides this to point somewhere else.
            return 0, cmd[cmd.index("-C") + 1] + "\n", ""
        if "fetch" in cmd:
            return 0, "", ""
        if "for-each-ref" in cmd and "--points-at" in cmd:
            return (0, self._head_tag + "\n", "") if self._head_tag else (0, "", "")
        if "for-each-ref" in cmd:
            return 0, "\n".join(self._tags) + "\n", ""
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"  # detached
        if "status" in cmd:
            return 0, self._status, ""
        if "rev-parse" in cmd:
            if cmd[-1] == "HEAD":
                return 0, self._head + "\n", ""
            return 0, _TIP_OID + "\n", ""
        if "ls-remote" in cmd:
            lines = [f"{oid}\trefs/tags/{t}" for t, oid in self._advertised.items()]
            lines += [f"{oid}\trefs/tags/{t}^{{}}" for t, oid in self._peeled.items()]
            return 0, ("\n".join(lines) + "\n" if lines else ""), ""
        if "config" in cmd:
            if "extensions.worktreeConfig" in cmd:
                # Real git with `--bool` answers `true` for `yes`/`on`/`1`/valueless
                # too, so the fake normalizes the same way rather than echoing the
                # raw spelling -- a fake that echoed it would let the production
                # `--bool` flag be dropped without any test noticing.
                if self._worktree_config is False:
                    return 1, "", ""
                return (
                    (0, "true\n", "") if "--bool" in cmd else (0, f"{self._worktree_config}\n", "")
                )
            scope = "--worktree" if "--worktree" in cmd else "--local"
            keys = self._config.get(scope)
            if keys is None:
                if scope == "--worktree" and self._worktree_config is not False:
                    # Real git: the extension is on but nobody wrote the file yet.
                    return (
                        128,
                        "",
                        f"fatal: unable to read config file '{self._gitdir}/config.worktree': "
                        "No such file or directory\n",
                    )
                return 1, "", ""
            return 0, "\n".join(keys) + "\n", ""
        if "worktree" in cmd and "add" in cmd:
            return 0, "", ""
        if "reset" in cmd and "--hard" in cmd:
            return 0, "", ""
        if "worktree" in cmd and "move" in cmd:
            return 0, "", ""
        return 1, "", f"unexpected argv: {cmd}"

    def argv_with(self, needle: str) -> list[str] | None:
        for c in self.calls:
            if needle in c:
                return c
        return None

    def order(self, *needles: str) -> list[int]:
        """Index of the first call containing each needle."""
        out = []
        for needle in needles:
            out.append(next(i for i, c in enumerate(self.calls) if needle in c))
        return out


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A primary checkout path whose sibling lane dirs genuinely do not exist."""
    # A DIRECTORY name, not prose: it mirrors the real primary checkout's own
    # folder, and `worktree_path` derives every lane dir as that folder's
    # sibling — so respelling it would stop the fixture matching the layout
    # under test.
    checkout = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    checkout.mkdir()
    monkeypatch.setattr(repository, "_repo", lambda: str(checkout))
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    # Each test owns its own lock, so a refusal in one cannot leak into the next.
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    return str(checkout)


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_refuses_when_the_worktree_already_exists(repo, monkeypatch):
    """Refuse, and send the operator only to controls that exist.

    This is the one collision an operator is guaranteed to reach, so the message
    is the whole remedy they get. It named "Advance" while that mutation existed;
    the assertion is written as a floor against naming any control the page does
    not render, because a message that sends someone hunting for a missing button
    is worse than git's own error.
    """
    monkeypatch.setattr(
        repository, "_find_worktree", _found({"path": "/somewhere/release-channel-stable"})
    )
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert "remove" in got["error"].lower()
    assert "advance" not in got["error"].lower()


@pytest.mark.asyncio
async def test_create_refuses_an_ambiguous_name_before_any_git_mutation(repo, monkeypatch):
    """An out-of-band duplicate basename is not absence and must fail closed."""
    git = _Git()

    async def ambiguous(_name):
        return (
            None,
            "ambiguous worktree name 'release-channel-stable' matches multiple "
            "checkouts: /a, /b",
        )

    monkeypatch.setattr(repository, "_find_worktree", ambiguous)
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert "ambiguous worktree name 'release-channel-stable'" in got["error"]
    assert git.calls == [], "ambiguity must refuse before any git mutation"


@pytest.mark.asyncio
async def test_create_refuses_an_occupied_path_by_name(repo, monkeypatch):
    """Refuse and NAME the path rather than letting git talk about it.

    ``git worktree add`` fails on a non-empty path anyway, but its message is
    about a directory the operator may not know is involved.
    """
    Path(release_channel_pin.worktree_path(repo)).mkdir()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert "already exists on disk" in got["error"]
    assert "release-channel-stable" in got["error"]


@pytest.mark.asyncio
async def test_create_stages_the_worktree_then_adopts_the_lane_path(repo, monkeypatch):
    """Built at a staging path, then moved into place.

    The add must NOT target the lane path directly. Doing so is what made the
    failure cleanup unsafe: it inferred ownership from an earlier existence check,
    which only holds for a single writer, and the locks here are per-event-loop.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True
    assert got["version"] == "0.5.0"
    assert got["ref"] == "refs/tags/v0.5.0"
    final = release_channel_pin.worktree_path(repo)

    add = git.argv_with("add")
    assert add is not None
    assert "--no-checkout" in add
    assert "--detach" in add
    assert add[-1] == _TIP_OID
    staged = add[-2]
    assert staged != final
    assert staged.startswith(final + ".staging.")

    populate = git.argv_with("reset")
    assert populate == ["git", "-C", staged, "reset", "--hard", _TIP_OID]

    move = git.argv_with("move")
    assert move is not None
    assert move[-2:] == [staged, final]
    # The directory basename IS the lane's name, and the row's label is read from
    # the fleet payload rather than from this call's answer -- so the destination
    # git is handed is where the naming rule has to hold.
    assert os.path.basename(final) == release_channel_pin.WORKTREE_NAME


@pytest.mark.asyncio
async def test_create_refuses_when_the_working_tree_is_redirected_and_never_populates(
    repo, monkeypatch
):
    """`core.worktree` pointing elsewhere: no `reset --hard`, staging discarded, error names it."""

    class _Redirected(_Git):
        async def __call__(self, cmd, **kw):
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                self.calls.append(list(cmd))
                return 0, "/somewhere/else\n", ""
            return await super().__call__(cmd, **kw)

    git = _Redirected()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert "/somewhere/else" in got["error"]
    assert "write outside the worktree" in got["error"]
    assert git.argv_with("reset") is None, "the populate must not run against a redirected tree"
    assert git.argv_with("move") is None
    # The staging worktree this call created is torn down, admin record included.
    removal = [c for c in git.calls if "worktree" in c and "remove" in c]
    assert removal and "--force" in removal[0]
    # And ONLY the staging record: a repository-wide prune would also unregister
    # any unrelated worktree whose directory is momentarily absent.
    assert not any("worktree" in c and "prune" in c for c in git.calls)


@pytest.mark.asyncio
async def test_a_failed_create_never_force_removes_the_lane_path(repo, monkeypatch):
    """The destructive cleanup may only ever name a path this call staged.

    A second process sharing the repo can create the lane path between our
    existence guard and our add. If cleanup targeted the lane path, that process's
    populated worktree would be deleted, and untracked files are in no reflog.
    """
    git = _Git(fail={"add": (1, "", "fatal: boom")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    final = release_channel_pin.worktree_path(repo)
    removes = [c for c in git.calls if "worktree" in c and "remove" in c]
    assert removes, "a failed create must still clean up its own residue"
    for argv in removes:
        assert final not in argv
        assert any(a.startswith(final + ".staging.") for a in argv)


@pytest.mark.asyncio
async def test_create_fetches_before_it_resolves(repo, monkeypatch):
    """Order is the guard against pinning a stale tag as the channel tip.

    The listing it must follow is the MIRROR's. Resolution reads only
    ``refs/dev-fleet/release-tags``, so a fetch that ran after the read would leave
    Create pinning whatever the mirror held a cycle ago.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    fetch_at, tag_at = git.order("fetch", "for-each-ref")
    assert fetch_at < tag_at


@pytest.mark.asyncio
async def test_create_reports_a_failed_fetch_instead_of_resolving_locally(repo, monkeypatch):
    git = _Git(fail={"fetch": (1, "", "fatal: unable to access remote")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert "cannot refresh release refs" in got["error"]
    assert git.argv_with("add") is None


@pytest.mark.asyncio
async def test_create_reports_a_failed_worktree_add(repo, monkeypatch):
    git = _Git(fail={"add": (128, "", "fatal: invalid reference")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert "git worktree add failed" in got["error"]


@pytest.mark.asyncio
async def test_a_failed_create_does_not_leave_the_lane_uncreatable(repo, monkeypatch):
    """``worktree add`` can fail AFTER registering the worktree.

    The leftover then has to go, or a retry meets a half-registered worktree.
    Cleaning up is safe because the target is the STAGING path, which carries this
    process's pid and a random suffix, so it is ours by construction -- not because
    an earlier existence check suggested it was.
    """
    git = _Git(fail={"add": (1, "", "fatal: could not create work tree dir")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    removed = git.argv_with("remove")
    assert removed is not None and "--force" in removed
    assert git.argv_with("prune") is None, "cleanup is scoped to the staging record"
    # The cleanup is best-effort and must never replace the real cause: git's own
    # message is what the operator needs, not "cleanup failed".
    assert "could not create work tree dir" in got["error"]


@pytest.mark.asyncio
async def test_a_cancelled_create_discards_the_staging_tree_on_the_way_out(repo, monkeypatch):
    """A RETURN is not the only way out of Create, and the other way left residue.

    `_run_uninterruptible` shields each git child but re-raises `CancelledError`
    once it returns, so an ordinary backend shutdown unwinds the frame between a
    successful `worktree add` and the `worktree move` that adopts it. The `rc != 0`
    cleanups are reached only by a returning failure, never by that unwind, so the
    staging worktree stayed registered on disk.
    """
    git = _Git()
    real_call = git.__call__

    async def _cancel_on_move(cmd, **kw):
        if "move" in cmd:
            raise asyncio.CancelledError()
        return await real_call(cmd, **kw)

    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", _cancel_on_move)

    # The cancellation must still propagate: swallowing it would make a shutdown
    # look like a completed Create.
    with pytest.raises(asyncio.CancelledError):
        await worktree_ops._release_channel_create()

    removed = git.argv_with("remove")
    assert removed is not None and "--force" in removed
    # Aimed at the staging path this call named, never at the lane's own path.
    assert any(".staging." in a for a in removed), removed


@pytest.mark.asyncio
async def test_create_checks_out_without_credential_helpers(repo, monkeypatch):
    """Both staging creation and population run in the strict tier.

    ``worktree add --no-checkout`` creates only the staging context; ``reset
    --hard`` then MATERIALIZES repo-controlled content after the filter probe.
    Strict is the SECOND layer: a declared content filter is refused outright,
    while the tier bounds a driver that reaches execution anyway.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True
    for needle in ("add", "reset"):
        idx = next(i for i, c in enumerate(git.calls) if needle in c)
        assert git.modes[idx] == "strict"


@pytest.mark.asyncio
async def test_create_refuses_a_checkout_that_declares_a_content_filter(repo, monkeypatch):
    """A declared filter stops population and discards the empty staging tree.

    ``worktree add --no-checkout`` creates the gitdir context without writing
    repo-controlled files. The staging-context probe then sees the arbitrary
    command, refuses it, and cleanup removes the empty linked worktree.
    """
    git = _Git(config={"--local": ["core.bare", "filter.evil.process"]})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert "filter.evil.process" in got["error"]
    add = git.argv_with("add")
    assert add is not None and "--no-checkout" in add
    assert git.argv_with("reset") is None, "refused, so nothing may be populated"
    assert git.argv_with("remove") is not None, "the empty staging worktree is discarded"


@pytest.mark.asyncio
async def test_create_redacts_the_offending_filter_key_before_it_reaches_the_dashboard(
    repo, monkeypatch
):
    """The refused key NAME is repository-authored and is quoted to the operator.

    ``filter.<name>.smudge`` has an attacker-chosen ``<name>`` segment, and the
    refusal string lands in the dashboard's ErrorNotice and the agent hand-off
    unmodified. It therefore runs through the same redaction every other quoted
    git output on this path does, so a credential planted in the key name is
    never displayed.
    """
    token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD"
    git = _Git(config={"--local": [f"filter.{token}.smudge"]})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert token not in got["error"]
    assert "[REDACTED" in got["error"]
    assert got["error"].startswith("refusing: this checkout's git config carries filter.")


@pytest.mark.asyncio
async def test_the_filter_probe_lands_with_no_network_call_before_the_populate(repo, monkeypatch):
    """The staging-context probe runs after add and immediately before populate.

    ``worktree add --no-checkout`` creates the gitdir context needed for
    ``includeIf gitdir:`` matching without writing repo-controlled files. Config
    stays writable, so no fetch or ``ls-remote`` may separate the clean probe from
    ``reset --hard``, the operation that can run a filter driver.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    await worktree_ops._release_channel_create()

    def first_index(predicate):
        return next(i for i, c in enumerate(git.calls) if predicate(c))

    add = first_index(lambda c: "worktree" in c and "add" in c)
    probe = first_index(lambda c: "config" in c)
    populate = first_index(lambda c: "reset" in c and "--hard" in c)
    assert add < probe < populate, "add --no-checkout must precede probe, then populate"
    assert "--no-checkout" in git.calls[add]
    staged = git.calls[add][-2]
    assert git.calls[probe][2] == staged
    assert git.calls[populate][2] == staged
    between = git.calls[probe + 1 : populate]
    assert not [c for c in between if "fetch" in c or "ls-remote" in c], (
        "no network call may separate the probe from the populate -- "
        f"found {[c for c in between if 'fetch' in c or 'ls-remote' in c]}"
    )


@pytest.mark.asyncio
async def test_a_filter_planted_after_the_fetch_still_refuses(repo, monkeypatch):
    """A driver written during the network work is still caught.

    This is the defect the relocation closes, and it is what makes the placement
    load-bearing rather than cosmetic: with the probe above the fetch, the config
    written here lands after the probe read a clean repo and is executed by the
    checkout.
    """
    git = _Git()
    planted = {"done": False}
    real_ls_remote = git.__call__

    def plant_during_network(argv, **kw):
        if "ls-remote" in argv and not planted["done"]:
            planted["done"] = True
            git._config = {"--local": ["filter.evil.process"]}
        return real_ls_remote(argv, **kw)

    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", plant_during_network)

    got = await worktree_ops._release_channel_create()

    assert planted["done"], "the fixture must have planted during the network step"
    assert got["ok"] is False
    assert "filter.evil.process" in got["error"]
    add = git.argv_with("add")
    assert add is not None and "--no-checkout" in add
    assert git.argv_with("reset") is None, "the populate must not run once the driver is seen"


@pytest.mark.asyncio
async def test_the_filter_probe_follows_config_includes(repo, monkeypatch):
    """``--includes`` on every scope query, or an included driver stays invisible.

    For a SPECIFIC-scope query git defaults include-following off, so a driver
    reached through ``include.path`` resolves at checkout time while a probe without
    the flag reports the repo clean.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    await worktree_ops._release_channel_create()

    probes = [c for c in git.calls if "config" in c]
    assert probes, "the probe must run"
    assert all("--includes" in c for c in probes)


@pytest.mark.asyncio
async def test_the_probe_reads_the_worktree_scope_only_when_it_is_live(repo, tmp_path, monkeypatch):
    """``config.worktree`` is a real driver source, and only when git will read it.

    Probing ``--local`` alone let a repo with ``extensions.worktreeConfig`` hide a
    driver in ``config.worktree``: a ``--local`` listing does not report
    worktree-scoped keys. Probing unconditionally would instead query a scope whose
    file may not exist, which is fatal.
    """
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()
    (gitdir / "config.worktree").write_text("")
    live = _Git(
        worktree_config="true",
        gitdir=str(gitdir),
        config={"--worktree": ["filter.evil.clean"]},
    )
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", live)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert "filter.evil.clean" in got["error"]

    off = _Git(worktree_config=False, config={"--local": ["core.bare"]})
    monkeypatch.setattr(runtime, "_run_cmd", off)
    assert (await worktree_ops._release_channel_create())["ok"] is True
    assert not any("--worktree" in c for c in off.calls)


@pytest.mark.asyncio
async def test_every_git_boolean_spelling_enables_the_worktree_probe(repo, tmp_path, monkeypatch):
    """``yes``/``on``/``1``/valueless all enable the extension, so all must probe.

    A raw ``--get`` returns each spelling verbatim, so comparing the text against
    "true" skipped the scope for four spellings that enable it -- and a driver in
    ``config.worktree`` then executed unexamined. ``--bool`` normalizes them, and
    this asserts the flag is actually passed.
    """
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()
    (gitdir / "config.worktree").write_text("")
    for spelling in ("true", "yes", "on", "1", ""):
        git = _Git(
            worktree_config=spelling,
            gitdir=str(gitdir),
            config={"--worktree": ["filter.evil.process"]},
        )
        monkeypatch.setattr(repository, "_find_worktree", _missing())
        monkeypatch.setattr(runtime, "_run_cmd", git)

        got = await worktree_ops._release_channel_create()

        assert got["ok"] is False, f"{spelling!r} must enable the worktree probe"
        probe = git.argv_with("extensions.worktreeConfig")
        assert probe is not None and "--bool" in probe


@pytest.mark.asyncio
async def test_the_extension_without_its_file_is_not_a_failed_probe(repo, tmp_path, monkeypatch):
    """Enabling the extension does not create ``config.worktree``, and the scope is
    still PROBED.

    Gating the probe on the file existing would leave a window: a file written after
    the check and before the populate is a scope the probe never read and git still
    honours. So the scope is always asked; git's specific "unable to read config
    file '...config.worktree': No such file or directory" is the one answer read as
    an empty scope, and every other failure still refuses (see the malformed test).
    """
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()  # extension on, but no config.worktree written
    git = _Git(worktree_config="true", gitdir=str(gitdir), config={"--local": ["core.bare"]})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    assert (await worktree_ops._release_channel_create())["ok"] is True
    assert any("--worktree" in c for c in git.calls), "the worktree scope is always probed"


@pytest.mark.asyncio
async def test_a_malformed_worktree_config_still_refuses(repo, tmp_path, monkeypatch):
    """Exit 128 is also what a MALFORMED ``config.worktree`` produces; only the
    missing-file message is benign, so this one refuses."""

    class _Malformed(_Git):
        async def __call__(self, cmd, **kw):
            if "config" in cmd and "--worktree" in cmd and "--list" in cmd:
                self.calls.append(list(cmd))
                return 128, "", "fatal: bad config line 1 in file config.worktree\n"
            return await super().__call__(cmd, **kw)

    git = _Malformed(
        worktree_config="true", gitdir=str(tmp_path), config={"--local": ["core.bare"]}
    )
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert release_channel_pin._FILTER_PROBE_FAILED in got["error"]
    assert git.argv_with("reset") is None


@pytest.mark.asyncio
async def test_the_probe_never_reads_the_operators_own_machine_config(repo, monkeypatch):
    """Global and system config are the operator's, not the repository's.

    ``git lfs install`` writes ``filter.lfs.*`` globally, so probing that scope
    would refuse every create on a host with git-lfs installed while proving
    nothing about the repo.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    assert (await worktree_ops._release_channel_create())["ok"] is True

    for call in (c for c in git.calls if "config" in c):
        assert "--global" not in call and "--system" not in call


@pytest.mark.asyncio
async def test_an_unreadable_config_scope_refuses_the_create(repo, monkeypatch):
    """Fail CLOSED: a scope that will not answer is not a clean scope."""
    git = _Git(fail={"config": (1, "", "fatal: bad config line 3")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert "unreadable git config" in got["error"]
    add = git.argv_with("add")
    assert add is not None and "--no-checkout" in add
    assert git.argv_with("reset") is None


@pytest.mark.asyncio
async def test_the_probe_matches_only_executable_filter_keys(repo, monkeypatch):
    """``filter.<name>.required`` names no command, so it is not a refusal.

    Only ``process``/``smudge``/``clean`` name a program git runs. Refusing on any
    ``filter.*`` key would block a repo that merely marks a filter required while
    defining nothing to execute.
    """
    git = _Git(config={"--local": ["filter.lfs.required", "core.bare"]})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    assert (await worktree_ops._release_channel_create())["ok"] is True


@pytest.mark.asyncio
async def test_create_refuses_a_tag_the_remote_does_not_publish(repo, monkeypatch):
    """A mirror ref planted after the fetch cannot become the channel tip.

    ``--prune`` on the fetch deletes only refs the remote does not advertise AT FETCH
    TIME; it cannot touch one written after that subprocess exits. Such a ref outranks
    every real release by semver and its oid would be checked out, then run by the lane's
    pod. Asking the remote what it publishes closes the timing window entirely.
    """
    git = _Git(tags=("v0.5.0", "v999.0.0"), advertised={"v0.5.0": _TIP_OID})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert "v999.0.0" in got["error"]
    assert git.argv_with("add") is None, "refused, so nothing may be checked out"


@pytest.mark.asyncio
async def test_create_refuses_a_published_tag_whose_local_oid_was_rewritten(repo, monkeypatch):
    """Same guard catches a rewrite that keeps a REAL release name.

    Forging a high version is the loud case; quietly repointing an existing release is
    the quiet one, and a name-only check would pass it.
    """
    git = _Git(tags=("v0.5.0",), advertised={"v0.5.0": _OTHER_OID})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert "does not match what the remote publishes" in got["error"]
    assert git.argv_with("add") is None


@pytest.mark.asyncio
async def test_an_annotated_release_tag_resolves_through_its_peel_line(repo, monkeypatch):
    """An ANNOTATED tag advertises a tag OBJECT, and its commit only on the ``^{}`` line.

    This is how a release is normally cut, so comparing against the plain entry would
    reject every annotated release. The peel line carries the commit a checkout resolves
    to, and it must win. Verified against real git before this guard existed.
    """
    git = _Git(
        tags=("v0.5.0",),
        advertised={"v0.5.0": _TAG_OBJECT_OID},
        peeled={"v0.5.0": _TIP_OID},
    )
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    assert (await worktree_ops._release_channel_create())["ok"] is True


@pytest.mark.asyncio
async def test_create_refuses_when_the_remote_cannot_be_asked(repo, monkeypatch):
    """Fail CLOSED: an unanswerable remote is not a confirmed release."""
    git = _Git(fail={"ls-remote": (1, "", "fatal: could not read from remote")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert "cannot confirm the release with the remote" in got["error"]
    assert git.argv_with("add") is None


@pytest.mark.asyncio
async def test_the_remote_is_asked_for_only_the_resolved_tag_and_its_peel(repo, monkeypatch):
    """The provenance query is exact and still asks git for the annotated-tag peel."""
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    await worktree_ops._release_channel_create()

    assert git.argv_with("ls-remote") == [
        "git",
        "-C",
        repo,
        "ls-remote",
        "--tags",
        "origin",
        "refs/tags/v0.5.0",
        "refs/tags/v0.5.0^{}",
    ]


@pytest.mark.asyncio
async def test_advertised_release_commit_retains_only_the_requested_tag(monkeypatch):
    """Even a non-conforming remote response cannot grow retained provenance state."""

    async def noisy_remote(cmd, **kw):
        unrelated = [f"{_OTHER_OID}\trefs/tags/v9.{i}.0" for i in range(5_000)]
        return 0, "\n".join([*unrelated, f"{_TIP_OID}\trefs/tags/v0.5.0"]) + "\n", ""

    async def origin():
        return "origin"

    monkeypatch.setattr(runtime, "_run_cmd", noisy_remote)
    monkeypatch.setattr(repository, "_upstream_remote", origin)

    got, err = await release_channel_pin.advertised_release_commit("/repo", "v0.5.0")

    assert err is None
    assert got == _TIP_OID
    assert isinstance(got, str), "the result is one bounded oid, never a tag population"


@pytest.mark.asyncio
async def test_advertised_release_commit_refuses_a_non_release_tag_before_any_network_call(
    monkeypatch,
):
    """The tag is re-checked where it becomes remote-facing argv, not trusted from the caller."""
    calls: list[list[str]] = []

    async def remote(cmd, **kw):
        calls.append(list(cmd))
        return 0, "", ""

    async def origin():
        return "origin"

    monkeypatch.setattr(runtime, "_run_cmd", remote)
    monkeypatch.setattr(repository, "_upstream_remote", origin)

    got_error: ValueError | None = None
    try:
        await release_channel_pin.advertised_release_commit("/repo", "v0.6.0-insider.1")
    except ValueError as exc:
        got_error = exc

    assert got_error is not None and "not a release tag" in str(got_error)
    assert calls == [], "a non-release tag never becomes an ls-remote argument"


@pytest.mark.asyncio
@requires_git
async def test_a_core_worktree_redirect_is_refused_before_the_populate_can_write_outside(
    tmp_path, monkeypatch
):
    """`core.worktree` honoured by a linked worktree would send `reset --hard` elsewhere.

    With ``extensions.worktreeConfig`` on, a ``core.worktree`` in the repository's
    config redirects a linked worktree's working tree to an arbitrary directory, so
    the populate would overwrite files there and report success (measured against
    git 2.50). The check asks git where the tree is and refuses unless it is the
    staging directory; a clean staging worktree answers with itself.
    """

    async def real_git(cmd, **kw):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    primary = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    primary.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (primary / "payload.txt").write_text("release payload\n")
    env = ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    for argv in (
        ["git", "-C", str(primary), "init", "-q"],
        ["git", "-C", str(primary), "symbolic-ref", "HEAD", "refs/heads/main"],
        ["git", "-C", str(primary), "add", "payload.txt"],
        ["git", "-C", str(primary), *env, "commit", "-q", "-m", "one"],
        ["git", "-C", str(primary), "config", "extensions.worktreeConfig", "true"],
    ):
        rc, _out, err = await real_git(argv)
        assert rc == 0, f"fixture setup failed: {argv} -> {err}"
    monkeypatch.setattr(runtime, "_run_cmd", real_git)

    clean = tmp_path / "release-channel-stable.staging.clean"
    rc, _out, err = await real_git(
        [
            "git",
            "-C",
            str(primary),
            "worktree",
            "add",
            "--no-checkout",
            "--detach",
            str(clean),
            "HEAD",
        ]
    )
    assert rc == 0, err
    assert await worktree_ops._worktree_redirected_away_from(str(clean)) == ""

    rc, _out, err = await real_git(
        ["git", "-C", str(primary), "config", "--local", "core.worktree", str(outside)]
    )
    assert rc == 0, err
    redirected = tmp_path / "release-channel-stable.staging.redirected"
    rc, _out, err = await real_git(
        [
            "git",
            "-C",
            str(primary),
            "worktree",
            "add",
            "--no-checkout",
            "--detach",
            str(redirected),
            "HEAD",
        ]
    )
    assert rc == 0, err
    where = await worktree_ops._worktree_redirected_away_from(str(redirected))
    assert where and os.path.realpath(where) == os.path.realpath(str(outside))
    # Nothing was written anywhere: the check is read-only.
    assert not (outside / "payload.txt").exists()
    assert not (redirected / "payload.txt").exists()


@pytest.mark.asyncio
@requires_git
async def test_staging_probe_catches_a_gitdir_conditional_filter(tmp_path, monkeypatch):
    """The primary probe misses a staging-only include that the staging probe sees."""

    async def real_git(cmd, **kw):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    primary = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    primary.mkdir()
    (primary / "payload.txt").write_text("release payload\n")
    env = [
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
    ]
    for argv in (
        ["git", "-C", str(primary), "init", "-q"],
        ["git", "-C", str(primary), "symbolic-ref", "HEAD", "refs/heads/main"],
        ["git", "-C", str(primary), "add", "payload.txt"],
        ["git", "-C", str(primary), *env, "commit", "-q", "-m", "one"],
    ):
        rc, _out, err = await real_git(argv)
        assert rc == 0, f"fixture setup failed: {argv} -> {err}"

    included = tmp_path / "staging-filter.inc"
    included.write_text('[filter "evil"]\n\tsmudge = false\n')
    include_key = "includeIf.gitdir:**/worktrees/release-channel-stable.staging.**.path"
    rc, _out, err = await real_git(
        ["git", "-C", str(primary), "config", "--local", include_key, str(included)]
    )
    assert rc == 0, err

    monkeypatch.setattr(runtime, "_run_cmd", real_git)
    assert await release_channel_pin.repo_supplied_filter(str(primary)) == ""

    staging = tmp_path / "release-channel-stable.staging.real"
    rc, _out, err = await real_git(
        [
            "git",
            "-C",
            str(primary),
            "worktree",
            "add",
            "--no-checkout",
            "--detach",
            str(staging),
            "HEAD",
        ]
    )
    assert rc == 0, err
    assert await release_channel_pin.repo_supplied_filter(str(staging)) == "filter.evil.smudge"

    rc, _out, err = await real_git(
        ["git", "-C", str(primary), "config", "--local", "--unset-all", include_key]
    )
    assert rc == 0, err
    rc, oid, err = await real_git(["git", "-C", str(primary), "rev-parse", "HEAD"])
    assert rc == 0, err
    rc, _out, err = await real_git(["git", "-C", str(staging), "reset", "--hard", oid.strip()])
    assert rc == 0, err
    assert (staging / "payload.txt").read_text() == "release payload\n"
    rc, status, err = await real_git(["git", "-C", str(staging), "status", "--porcelain"])
    assert rc == 0 and status == "", err
    rc, _out, _err = await real_git(["git", "-C", str(staging), "symbolic-ref", "--quiet", "HEAD"])
    assert rc == 1, "the populated staging worktree must remain detached"


@pytest.mark.asyncio
@requires_git
async def test_refused_create_removes_staging_directory_and_admin_record(tmp_path, monkeypatch):
    """A staging-context filter refusal leaves neither form of worktree residue."""
    calls: list[list[str]] = []

    async def real_git(cmd, **kw):
        calls.append(list(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    primary = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    primary.mkdir()
    env = [
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
    ]
    for argv in (
        ["git", "-C", str(primary), "init", "-q"],
        ["git", "-C", str(primary), "symbolic-ref", "HEAD", "refs/heads/main"],
        ["git", "-C", str(primary), *env, "commit", "-q", "--allow-empty", "-m", "one"],
        ["git", "-C", str(primary), "tag", "v0.5.0"],
        ["git", "-C", str(primary), "remote", "add", "origin", str(primary)],
    ):
        rc, _out, err = await real_git(argv)
        assert rc == 0, f"fixture setup failed: {argv} -> {err}"

    included = tmp_path / "staging-filter.inc"
    included.write_text('[filter "evil"]\n\tsmudge = false\n')
    include_key = "includeIf.gitdir:**/worktrees/release-channel-stable.staging.**.path"
    rc, _out, err = await real_git(
        ["git", "-C", str(primary), "config", "--local", include_key, str(included)]
    )
    assert rc == 0, err

    monkeypatch.setattr(repository, "_repo", lambda: str(primary))
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    monkeypatch.setattr(worktree_ops, "_GIT_MUTATION_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_run_cmd", real_git)

    got = await worktree_ops._release_channel_create()

    assert got["ok"] is False
    assert "filter.evil.smudge" in got["error"]
    assert not [c for c in calls if "reset" in c], "refusal must precede population"
    assert not list(tmp_path.glob("release-channel-stable.staging.*"))
    rc, listing, err = await real_git(
        ["git", "-C", str(primary), "worktree", "list", "--porcelain"]
    )
    assert rc == 0, err
    assert "release-channel-stable.staging." not in listing


@pytest.mark.asyncio
@requires_git
async def test_a_real_git_annotated_tag_advertises_its_commit_on_the_peel_line(
    tmp_path, monkeypatch
):
    """The exact-ref/peel behaviour against REAL git, not against this file's fake.

    Every other test here mocks ``_run_cmd``, so they pin what the code ASKS for and
    what it does with a transcript this file wrote. That cannot catch the case where
    git's actual output shape differs from the shape assumed, and the provenance gate
    is exactly where that would be fatal: it compares the resolved oid against what
    the remote advertises, so if an annotated tag advertised only its tag OBJECT the
    gate would refuse every real release. An annotated tag is how a release is
    normally cut, which makes the fake's fidelity here load-bearing rather than
    cosmetic.

    Builds a throwaway repository, cuts an ANNOTATED tag, and asserts the answer is
    the COMMIT -- so the tag object's own oid must NOT be the answer. Skipped where git
    is unavailable, following ``requires_git``.
    """

    async def real_git(cmd, **kw):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    origin = tmp_path / "origin"
    origin.mkdir()
    env = [
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
    ]
    for argv in (
        ["git", "-C", str(origin), "init", "-q"],
        ["git", "-C", str(origin), *env, "commit", "-q", "--allow-empty", "-m", "one"],
        # -a is the whole point: an ANNOTATED tag creates a tag object, so
        # refs/tags/v0.5.0 does NOT name the commit.
        ["git", "-C", str(origin), *env, "tag", "-a", "v0.5.0", "-m", "release"],
    ):
        rc, _out, err = await real_git(argv)
        assert rc == 0, f"fixture setup failed: {argv} -> {err}"

    rc, commit_oid, _err = await real_git(
        ["git", "-C", str(origin), "rev-parse", "v0.5.0^{commit}"]
    )
    assert rc == 0
    rc, tag_object_oid, _err = await real_git(["git", "-C", str(origin), "rev-parse", "v0.5.0"])
    assert rc == 0
    commit_oid, tag_object_oid = commit_oid.strip(), tag_object_oid.strip()
    # If these matched, the fixture cut a lightweight tag and the test would prove
    # nothing about the peel line.
    assert commit_oid != tag_object_oid, "an annotated tag must not resolve to its own object"

    async def _origin_is_the_remote():
        return str(origin)

    monkeypatch.setattr(runtime, "_run_cmd", real_git)
    monkeypatch.setattr(repository, "_upstream_remote", _origin_is_the_remote)

    got, err = await release_channel_pin.advertised_release_commit(str(origin), "v0.5.0")
    assert err is None
    assert got == commit_oid


@pytest.mark.asyncio
@requires_git
async def test_discard_drops_only_the_staging_record_and_spares_an_unmounted_worktree(
    tmp_path, monkeypatch
):
    """Cleanup after a failed lane create never unregisters a worktree it did not make.

    Two worktrees, both with their directories gone: the staging one this call
    owns, and an unrelated, unlocked checkout whose directory is merely absent --
    the shape of a worktree on unmounted removable or network media. After the
    discard, the staging admin record is gone (so ``remove --force`` alone
    covers the vanished-directory case) and the unrelated record is still there,
    which a repository-wide ``worktree prune`` would have deleted with no undo.
    """

    async def real_git(cmd, **kw):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    primary = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    primary.mkdir()
    env = ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    staging = tmp_path / "release-channel-stable.staging.real"
    unrelated = tmp_path / "someone-elses-checkout"
    for argv in (
        ["git", "-C", str(primary), "init", "-q"],
        ["git", "-C", str(primary), *env, "commit", "-q", "--allow-empty", "-m", "one"],
        [
            "git",
            "-C",
            str(primary),
            "worktree",
            "add",
            "-q",
            "--no-checkout",
            "--detach",
            str(staging),
            "HEAD",
        ],
        ["git", "-C", str(primary), "worktree", "add", "-q", "--detach", str(unrelated), "HEAD"],
    ):
        rc, _out, err = await real_git(argv)
        assert rc == 0, f"fixture setup failed: {argv} -> {err}"
    # Both directories vanish: the staging one as a failed add leaves it, the
    # unrelated one as an unmounted volume leaves it. Only the records remain.
    shutil.rmtree(staging)
    shutil.rmtree(unrelated)
    rc, listed, _err = await real_git(
        ["git", "-C", str(primary), "worktree", "list", "--porcelain"]
    )
    assert rc == 0
    listed_norm = listed.replace("\\", "/")
    assert staging.as_posix() in listed_norm and unrelated.as_posix() in listed_norm, "fixture: both records must exist"

    monkeypatch.setattr(runtime, "_run_cmd", real_git)
    monkeypatch.setattr(worktree_ops, "_GIT_MUTATION_LOCK", asyncio.Lock())

    await worktree_ops._discard_failed_lane_creation(str(primary), str(staging))

    rc, listed, _err = await real_git(
        ["git", "-C", str(primary), "worktree", "list", "--porcelain"]
    )
    assert rc == 0
    listed_norm = listed.replace("\\", "/")
    assert staging.as_posix() not in listed_norm, "the staging record is this call's to drop"
    assert unrelated.as_posix() in listed_norm, "an unrelated worktree's record must survive the cleanup"


@pytest.mark.asyncio
async def test_the_filter_probe_runs_without_credential_helpers(repo, monkeypatch):
    """The probe reads repo-controlled config, so it must not carry credentials.

    ``--includes`` exists so the probe FOLLOWS repository-controlled ``include.path``,
    which makes these the reads most exposed to config this checkout did not author.
    They need no network, so the standard tier would hand them the gateway's trusted
    credential helpers for nothing. The remote-facing calls are the opposite case and
    are asserted separately.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    await worktree_ops._release_channel_create()

    for idx, call in enumerate(git.calls):
        if "config" in call or ("rev-parse" in call and "--absolute-git-dir" in call):
            assert git.modes[idx] == "strict", f"{call} must not run in the credential tier"


@pytest.mark.asyncio
async def test_the_remote_facing_calls_keep_their_credentials(repo, monkeypatch):
    """`fetch` and `ls-remote` authenticate to the remote, so they stay standard.

    Pinned alongside the probe's tier because the asymmetry is the point: making
    everything strict would break the network calls, and making everything standard is
    the defect the probe's tier closes.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)

    await worktree_ops._release_channel_create()

    for idx, call in enumerate(git.calls):
        if "fetch" in call or "ls-remote" in call:
            assert git.modes[idx] == "standard", f"{call} needs the credential helper"


@pytest.mark.asyncio
async def test_the_result_carries_no_field_no_caller_reads(repo, monkeypatch):
    """The result keys are exactly what the HTTP caller types.

    A field kept "for diagnostics" that no surface shows is a claim about the
    payload's contract that no consumer keeps: `path` was a redacted string
    nothing rendered, and `from_oid` a sha the toast never named. Pinned as a set
    so re-adding one has to come with the reader that justifies it.

    ``name`` is absent for that same reason and is the case this pin missed while
    asserting it: the frontend types the response as
    ``{ok?, lane?, version?, ref?, error?}`` and reads only those, so the row's
    label comes from the fleet payload rather than from Create's answer.
    """
    git = _Git(head="old-oid")
    monkeypatch.setattr(runtime, "_run_cmd", git)
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    created = await worktree_ops._release_channel_create()
    assert set(created) == {"ok", "lane", "ref", "version"}


@pytest.mark.asyncio
async def test_fleet_publishes_a_channel_with_no_worktree_as_a_placeholder(repo, monkeypatch):
    """A channel the operator has not materialized is still published.

    Without the placeholder there is nowhere on the page the feature is
    discoverable — there is no header control.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    row = await fleet_state._release_channel([])
    assert row is not None
    assert row["lane"] == release_channel_pin.CHANNEL
    assert row["worktree"] is None
    assert row["version"] == "0.5.0"
    assert row["ref"] == "refs/tags/v0.5.0"
    assert row["name_taken_by_branch"] is False
    assert set(row) == {
        "lane",
        "name",
        "worktree",
        "ref",
        "version",
        "tip_version",
        "unpublished",
        "error",
        "at_tip",
        "name_taken_by_branch",
    }


@pytest.mark.asyncio
async def test_fleet_adopts_a_detached_worktree_and_reports_currency(repo, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head="old-oid"))
    row = await fleet_state._release_channel(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    assert row is not None
    assert row["worktree"] == "release-channel-stable"
    assert row["at_tip"] is False
    # The row names the release the TREE holds, and the tip separately. Feeding
    # the resolved version into `version` made the badge flip to each new release
    # as it shipped while the checkout stayed on the old one.
    assert row["version"] == "0.4.9"
    assert row["tip_version"] == "0.5.0"


@pytest.mark.asyncio
async def test_an_adopted_row_on_no_release_tag_reports_no_version(repo, monkeypatch):
    """``None`` rather than borrowing the tip's version to look complete.

    The worktree is adopted for being DETACHED, not for being at a release, so an
    operator who checked out an arbitrary commit in it is on no release — and the
    row has to say so instead of naming a build the tree does not contain.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head="old-oid", head_tag=""))
    row = await fleet_state._release_channel(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    assert row is not None
    assert row["worktree"] == "release-channel-stable"
    assert row["version"] is None
    assert row["tip_version"] == "0.5.0"


@pytest.mark.asyncio
async def test_fleet_adopts_a_detached_worktree_whose_release_listing_overflows(repo, monkeypatch):
    """Overflow keeps the adopted row but refuses to name its current release."""
    git = _Git(head="old-oid")
    overflow = [f"v9.{i}.0" for i in range(release_channel_pin._MAX_RELEASE_REFS + 1)]

    async def overflowing_points_at(cmd, **kw):
        if "for-each-ref" in cmd and "--points-at" in cmd:
            return 0, "\n".join(overflow) + "\n", ""
        return await git(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", overflowing_points_at)
    row = await fleet_state._release_channel(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )

    assert row is not None
    assert row["worktree"] == release_channel_pin.WORKTREE_NAME
    assert row["version"] is None
    assert row["error"]
    assert release_channel_pin.WORKTREE_NAME in row["error"]
    assert str(release_channel_pin._MAX_RELEASE_REFS) in row["error"]
    assert "refusing to name the release this tree holds" in row["error"]


@pytest.mark.asyncio
async def test_worktree_overflow_does_not_overwrite_a_resolver_error(repo, monkeypatch):
    """The earlier resolver diagnosis remains the row's primary error."""
    resolver_error = "cannot list tags (git tag failed)"

    async def failed_resolve(*, repo):
        return {"ok": False, "error": resolver_error}

    async def overflow(*args, **kwargs):
        raise release_channel_pin.MirrorOverflow("too many points-at refs")

    monkeypatch.setattr(release_channel_pin, "resolve", failed_resolve)
    monkeypatch.setattr(release_channel_pin, "worktree_state", overflow)
    row = await fleet_state._release_channel(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )

    assert row is not None
    assert row["worktree"] == release_channel_pin.WORKTREE_NAME
    assert row["version"] is None
    assert row["error"] == resolver_error


@pytest.mark.asyncio
async def test_fleet_does_not_adopt_a_branch_checkout_that_shares_the_name(repo, monkeypatch):
    """The reserved name must not confer channel controls on somebody's branch."""

    async def attached(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        return await _Git()(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", attached)
    row = await fleet_state._release_channel(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    assert row is not None
    assert row["worktree"] is None
    assert row["name_taken_by_branch"] is True


@pytest.mark.asyncio
async def test_fleet_does_not_claim_a_branch_when_the_probe_could_not_read_head(repo, monkeypatch):
    """An unreadable HEAD is a THIRD state, not "on a branch".

    Collapsing it into ``name_taken_by_branch`` asserts a git fact about a tree
    nobody read, and because that flag also suppresses the placeholder row, the
    channel would vanish from the page behind a fabricated explanation.
    """

    async def unreadable(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", "fatal: not a git repository"
        if "rev-parse" in cmd and cmd[-1] == "HEAD":
            return 1, "", "fatal: bad revision"
        return await _Git()(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", unreadable)
    monkeypatch.setattr(
        release_channel_pin,
        "worktree_state",
        _async_return({"at_tip": False, "detached": None, "version": None}),
    )
    row = await fleet_state._release_channel(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    assert row is not None
    assert row["worktree"] is None
    assert row["name_taken_by_branch"] is False
    assert row["error"] and "could not be read" in row["error"]


@pytest.mark.asyncio
async def test_fleet_publishes_the_worktree_basename(repo, monkeypatch):
    """The frontend labels the not-yet-created row from this field.

    Re-deriving the name in the frontend would put the naming rule on both sides
    of the boundary, where a change to WORKTREE_NAME desyncs the label from the
    directory that actually gets created.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    row = await fleet_state._release_channel([])
    assert row is not None
    assert row["name"] == release_channel_pin.WORKTREE_NAME == "release-channel-stable"


@pytest.mark.asyncio
async def test_fleet_flags_an_empty_channel_as_unpublished_not_an_error(repo, monkeypatch):
    """An empty channel is benign, so the row carries ``unpublished``, not ``error``.

    A checkout that has published no stable release yet — a fork, a fresh clone —
    is a documented state, so the row renders it as information and leaves Create
    enabled. The genuine git failure below is the state that carries ``error``, and
    the two are told apart only by this flag.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git(tags=["v0.6.0-insider.6"]))
    row = await fleet_state._release_channel([])
    assert row is not None
    assert row["unpublished"] is True
    assert row["error"] is None
    assert row["version"] is None
    assert row["ref"] is None


@pytest.mark.asyncio
async def test_fleet_reports_a_git_failure_as_an_error_not_unpublished(repo, monkeypatch):
    """A failed tag listing is a genuine error the operator is shown.

    It shares ``ok: False`` with the empty-channel state and is separated only by
    ``unpublished``: a git failure carries ``error`` and leaves ``unpublished``
    false, so the frontend routes it through the shared error surface and blocks
    Create.
    """
    monkeypatch.setattr(
        runtime,
        "_run_cmd",
        _Git(fail={"for-each-ref": (1, "", "fatal: not a git repository")}),
    )
    row = await fleet_state._release_channel([])
    assert row is not None
    assert row["unpublished"] is False
    assert row["error"] and "cannot list tags" in row["error"]
    assert row["ref"] is None


@pytest.mark.asyncio
async def test_fleet_release_channel_is_none_when_resolution_raises(repo, monkeypatch):
    """This rides on the cached fleet snapshot; a failed resolve must not blank it."""

    async def boom(*a, **kw):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(release_channel_pin, "resolve", boom)
    assert await fleet_state._release_channel([]) is None


@pytest.mark.asyncio
async def test_fleet_release_channel_is_none_without_a_checkout(monkeypatch):
    monkeypatch.setattr(repository, "_repo", _raise(repository.RepoNotConfigured("no checkout")))
    assert await fleet_state._release_channel([]) is None


# --------------------------------------------------------------------------
# route surface
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_mutation_route_takes_no_request_argument():
    """Nothing in the request selects what this route acts on.

    There is one release channel and one mutation, so the endpoint is
    argument-free and the handler passes nothing through. That is what retires the
    validation the older shape needed: a value that cannot be sent cannot be
    rejected, sanitized, or smuggled into a git ref or a directory name.
    """
    import inspect

    assert list(inspect.signature(worktree_ops._release_channel_create).parameters) == []
    assert not hasattr(http_api, "_lane_action")


@pytest.mark.asyncio
async def test_a_release_channel_route_audits_the_worktree_it_acts_on(monkeypatch):
    """The audit target is the CONSTANT the route acts on, never a body field.

    A first-match scan over the union of every route's target field let a body
    carry a stray `name` and have the tamper-evident trail record a mutation
    against THAT — while the handler acted on something else. With no request
    argument there is nothing to read, and an empty target would name nothing at
    all, so the route declares the worktree it touches. The body below carries a
    decoy `name`: a record naming it would mean a client can choose what its own
    mutation is recorded against.
    """
    seen: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    monkeypatch.setattr(runtime, "_redact", lambda s: s)

    @http_api._audited(
        "dev_fleet_release_channel_create",
        static_target=release_channel_pin.WORKTREE_NAME,
    )
    async def handler(_request):
        return web.json_response({"ok": True})

    await handler(_FakeRequest({"name": "kirocrew-wt-something-else"}))
    assert [r["resources"] for r in seen] == ["release-channel-stable"]


@pytest.mark.asyncio
async def test_a_worktree_route_cannot_be_audited_against_a_channel(monkeypatch):
    """The mirror case, asserted on BEHAVIOUR rather than on a literal.

    A worktree mutation reads `name`/`names`/`path`. If a channel field were
    admitted into that chain, a body naming the channel would have the trail record
    it as the target of a mutation that never touched it. So a body carrying ONLY a
    channel field must audit against nothing at all -- an empty target is honest,
    where a borrowed one is not.
    """
    seen: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    monkeypatch.setattr(runtime, "_redact", lambda s: s)

    @http_api._audited("dev_fleet_worktree_remove")
    async def handler(_request):
        return web.json_response({"ok": True})

    await handler(_FakeRequest({"lane": release_channel_pin.CHANNEL}))
    assert [r["resources"] for r in seen] == [""]


# --------------------------------------------------------------------------
# prune
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_never_offers_a_detached_tree_however_it_was_made(tmp_path, monkeypatch):
    """``empty`` is withheld from a BRANCHLESS tree, not from a matching name.

    The trap is that every ORDINARY prune signal says "delete me": detached (so
    no branch and no PR), clean, and holding no commits of its own because a
    release tag is an ancestor of the base branch. That is the ``empty`` verdict,
    and past 48h ``empty`` is a candidate the preview PRESELECTS.

    Both rows below are that state and neither may be offered. The second is the
    one a basename guard could not reach: a tree the operator detached BY HAND at
    a release tag -- the workflow this feature automates -- carries no lane name,
    so a prefix match left it preselected while claiming pins were safe.
    """
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_git", _async_return("a" * 40))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((None, [])))
    # Age the trees past the 48h preselection threshold: without this the verdict
    # is `fresh` for a second reason and the test would pass without the guard.
    monkeypatch.setattr(worktree_ops, "time", SimpleNamespace(time=lambda: time.time() + 400_000))

    for name in (release_channel_pin.WORKTREE_NAME, "kirocrew-wt-hand-detached"):
        path = tmp_path / name
        path.mkdir()
        got = await worktree_ops._prunable(str(path), None)
        assert got["ok"] is False, name
        assert got["code"] == "fresh", name


@pytest.mark.asyncio
async def test_prune_still_offers_a_branch_checkout_holding_the_reserved_name(
    tmp_path, monkeypatch
):
    """The reserved name confers nothing on a tree that is ON A BRANCH.

    A basename guard hid this row from Prune merged entirely: an ordinary feature
    worktree, its PR merged, that happened to be called `release-channel-stable`
    became permanently unprunable. Keying on the shape lets it fall through to the
    ordinary merged logic, which is where it belongs.
    """
    path = tmp_path / release_channel_pin.WORKTREE_NAME
    path.mkdir()
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_git", _async_return("a" * 40))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((None, [])))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return({"state": "MERGED"}))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", _async_return("a" * 40))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", _async_return(True))

    got = await worktree_ops._prunable(str(path), "feat/x")
    assert got["ok"] is True
    assert got["code"] == "merged"


@pytest.mark.asyncio
async def test_prune_candidates_keeps_a_lane_worktree_out_of_the_selection(tmp_path, monkeypatch):
    """End to end: the lane row lands in ``kept``, never in ``candidates``.

    Asserted through ``_prune_candidates`` and not just the verdict because the
    preselection the operator confirms is built from ``candidates`` — a verdict
    that was right but reached the wrong list would still delete the pin.
    """
    lane_dir = tmp_path / release_channel_pin.WORKTREE_NAME
    lane_dir.mkdir()
    feature = {"path": "/repos/kirocrew-wt-feature", "branch": "feat-x", "is_main": False}
    lane = {"path": str(lane_dir), "is_main": False}
    monkeypatch.setattr(repository, "_discover_worktrees", _async_return([feature, lane]))
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_git", _async_return("a" * 40))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((None, [])))
    monkeypatch.setattr(worktree_ops, "time", SimpleNamespace(time=lambda: time.time() + 400_000))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return({"state": "MERGED"}))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", _async_return("a" * 40))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", _async_return(True))

    got = await worktree_ops._prune_candidates()
    names = [row["name"] for row in got["candidates"]]
    assert names == ["kirocrew-wt-feature"]
    kept = {row["name"]: row["code"] for row in got["kept"]}
    assert kept[release_channel_pin.WORKTREE_NAME] == "fresh"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body
        self.content_length = 1
        # `_audited` reads the raw stream (cached, so a handler can re-parse it).
        # A route declaring `static_target` never reads it at all, which is what
        # the audit test below pins with a decoy body.
        self.can_read_body = True

    async def read(self):
        return json.dumps(self._body).encode()

    async def json(self):
        return self._body


def _found(entry: dict):
    async def _f(name):
        return entry, None

    return _f


def _missing():
    async def _f(name):
        return None, f"worktree not found: {name}"

    return _f


def _async_return(value):
    async def _f(*a, **kw):
        return value

    return _f


def _raise(exc):
    def _f(*a, **kw):
        raise exc

    return _f
