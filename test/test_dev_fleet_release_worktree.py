"""The release worktree action: create one detached worktree at the newest
stable release the remote advertises.

Two layers of test:

* **Composition**, over a fake git layer (``runtime._run_cmd`` and the two reused
  ``worktree.py`` helpers replaced): the action resolves the newest stable tag by
  SEMVER, checks out the commit the remote ADVERTISES for it, refuses an
  unadvertised or retracted tag, refuses a repo that declares a checkout filter,
  and cleans up a half-made worktree.

* **Real git**, driving ``create_release_worktree`` end to end against a local
  origin with direct (non-sandboxed) git runners that apply the SAME ``-c``
  overrides and ``--includes`` filter probe the gateway's strict-tier path does:
  a planted ``filter.<name>.smudge`` is refused and never runs, a ``post-checkout``
  hook does not run, and a clean repo lands detached on the tag's peeled commit.

The one property a unit test cannot exercise -- that the checkout runs at the
gateway's STRICT tier, so ``~/.aws`` and an ``include.path`` pointing at it are
bind-masked from the git child wherever an OS sandbox backend runs it -- is pinned by
identity: the action uses ``worktree.py``'s strict-tier ``_run_git`` and
filter-refusing ``_checkout_filter`` verbatim
(``test_the_checkout_uses_the_strict_tier_gateway_primitives``).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.apps.builtins.dev_fleet import release_worktree, repository, runtime
from kiro_crew.dashboard.handlers import worktree as dash_worktree
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

_PEELED = "1" * 40  # the commit an annotated tag peels to -- what a checkout lands on
_TAGOBJ = "2" * 40  # the tag object's own oid (the non-peel ls-remote line)


class _Completed:
    """A stand-in for ``subprocess.CompletedProcess`` as ``_run_git`` returns it."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeGit:
    """Records every git call the action makes and answers the reads it needs.

    ``runtime._run_cmd`` carries the network calls (ls-remote, fetch); ``_run_git``
    carries the strict-tier local calls (the worktree-list probe, rev-parse, worktree
    add, symbolic-ref).
    Both are recorded so a test can assert that a refusal happened BEFORE a fetch or
    a checkout.
    """

    def __init__(
        self,
        *,
        tags: tuple[str, ...] = ("v0.5.0", "v0.10.0", "v0.9.0"),
        advertised: str | None = _PEELED,
        present_oid: str | None = _PEELED,
        add_rc: int = 0,
        add_exc: BaseException | None = None,
        fetch_rc: int = 0,
        detached: bool = True,
        worktrees: tuple[str, ...] = (),
        locked: tuple[str, ...] = (),
        list_rc: int = 0,
        times_out_on: str | None = None,
        add_loses_path: bool = False,
        remove_rc: int = 0,
        sandbox_missing_on: str | None = None,
        promisor: str = "",
        config_rc: int = 0,
    ) -> None:
        self._tags = tags
        self._advertised = advertised  # peeled commit the confirm ls-remote returns
        self._present_oid = present_oid  # what rev-parse --verify answers (None => absent)
        self._add_rc = add_rc
        self._add_exc = add_exc  # raised from `worktree add` (a timeout / OS error)
        self._fetch_rc = fetch_rc
        self._detached = detached
        # Paths `git worktree list --porcelain` reports BESIDE the primary checkout,
        # for the basename probe. Default: none, so only the primary exists.
        self._worktrees = worktrees
        self._locked = locked  # of those, the ones `git worktree list` reports as locked
        self._list_rc = list_rc  # the exit status of that listing (non-zero => unreadable)
        # A local step ("worktree-list" or "verify") whose git `_run_git` reports as timed out.
        self._times_out_on = times_out_on
        # The add refuses because another writer took the path after the claim and
        # populated it: git exits non-zero and leaves that writer's tree in place.
        self._add_loses_path = add_loses_path
        self._remove_rc = (
            remove_rc  # the exit status of `git worktree remove` (non-zero => refused)
        )
        # The one step that meets a missing sandbox: a network step (see `_step`) gets
        # `_run_cmd`'s no-sandbox refusal, the `worktree-list` probe `_run_git`'s raise.
        self._sandbox_missing_on = sandbox_missing_on
        # What `git config --list -z` prints: NUL-ended `key\nvalue` entries, of which only
        # the promisor keys matter. Empty is a clone with none set (a full clone).
        self._promisor = promisor
        self._config_rc = config_rc  # the exit status of that listing (non-zero => unreadable)
        self.run_cmd_calls: list[list[str]] = []
        self.run_cmd_modes: list[str | None] = []  # the `mode=` each network call ran at
        self.kwargs_by_step: dict[str, list[dict[str, Any]]] = {}  # every call's keywords
        self.run_git_calls: list[list[str]] = []
        # The calls (as recorded, without the flag) that ran with `--no-replace-objects`.
        self.no_replace_calls: list[list[str]] = []

    @staticmethod
    def _step(cmd: list[str]) -> str:
        """Which ``_run_cmd`` network step *cmd* is: either ls-remote read, or the fetch."""
        if "ls-remote" in cmd:
            return "list" if "--refs" in cmd else "confirm"
        return "fetch" if "fetch" in cmd else "other"

    def _porcelain(self) -> str:
        """``git worktree list --porcelain`` for the primary plus ``self._worktrees``."""
        paths = ("/fleet/primary-checkout", *self._worktrees)
        blocks = []
        for i, path in enumerate(paths):
            head = "branch refs/heads/main" if i == 0 else "detached"
            lock = "locked initializing\n" if path in self._locked else ""
            blocks.append(f"worktree {path}\nHEAD {'a' * 40}\n{head}\n{lock}")
        return "\n".join(blocks) + "\n"

    async def run_cmd(self, cmd: list[str], **_kw: Any) -> tuple[int, str, str]:
        self.run_cmd_calls.append(list(cmd))
        step = self._step(cmd)
        self.kwargs_by_step.setdefault(step, []).append(dict(_kw))
        if step == self._sandbox_missing_on:
            # `_run_cmd`'s own wording when it refuses a step for want of an OS sandbox
            # backend (none exists, and unsandboxed execution is not permitted).
            return -1, "", "sandbox unavailable: no OS sandbox backend"
        self.run_cmd_modes.append(_kw.get("mode"))
        if step == "list":
            lines = [f"{'a'*40}\trefs/tags/{t}" for t in self._tags]
            return 0, "\n".join(lines) + "\n", ""
        if step == "confirm":  # the advertised-commit confirm (direct + peel)
            if self._advertised is None:
                # The remote does not carry the tag: ls-remote exits 0, prints nothing.
                return 0, "", ""
            tag = next((c for c in cmd if c.startswith("refs/tags/") and "^{}" not in c), "")
            lines = [f"{_TAGOBJ}\t{tag}", f"{self._advertised}\t{tag}^{{}}"]
            return 0, "\n".join(lines) + "\n", ""
        if step == "fetch":
            return (
                self._fetch_rc,
                "",
                ("" if self._fetch_rc == 0 else "fatal: Authentication failed"),
            )
        return 1, "", f"unexpected run_cmd argv: {cmd}"

    def run_git(self, args: list[str], cwd: str, **_kw: Any) -> _Completed:
        if args[:1] == ["--no-replace-objects"]:
            args = args[1:]
            self.no_replace_calls.append(list(args))
        self.run_git_calls.append(list(args))
        if args == ["worktree", "list", "--porcelain"]:
            if self._times_out_on == "worktree-list":
                raise subprocess.TimeoutExpired(
                    cmd="git worktree list", timeout=FALLBACK_TIMEOUT_SECS
                )
            if self._sandbox_missing_on == "worktree-list":
                # `_run_git`'s own signal when it refuses a spawn for want of an OS
                # sandbox backend (none exists, and unsandboxed execution is not permitted).
                raise release_worktree.SandboxUnavailable("no OS sandbox backend")
            if self._list_rc:
                return _Completed(self._list_rc, "", "fatal: cannot read worktree records")
            return _Completed(0, self._porcelain(), "")
        if args == ["config", "--list", "-z"]:
            if self._times_out_on == "promisor":
                raise subprocess.TimeoutExpired(cmd="git config", timeout=FALLBACK_TIMEOUT_SECS)
            if self._config_rc:
                return _Completed(self._config_rc, "", "fatal: bad config line 1")
            return _Completed(0, self._promisor, "")
        if args[:1] == ["rev-parse"] and "--verify" in args:
            if self._times_out_on == "verify":
                raise subprocess.TimeoutExpired(cmd="git rev-parse", timeout=FALLBACK_TIMEOUT_SECS)
            if self._present_oid is None:
                return _Completed(1, "", "")
            return _Completed(0, self._present_oid + "\n", "")
        if args == ["rev-parse", "HEAD"]:
            return _Completed(0, _PEELED + "\n", "")
        if args[:1] == ["symbolic-ref"]:
            return _Completed(1 if self._detached else 0, "", "")
        if args[:2] == ["worktree", "add"]:
            if self._add_exc is not None:
                raise self._add_exc
            if self._add_loses_path:
                (Path(args[-2]) / "theirs.txt").write_text("another writer's tree\n", "utf-8")
                return _Completed(128, "", f"fatal: '{args[-2]}' already exists")
            return _Completed(self._add_rc, "", "" if self._add_rc == 0 else "fatal: boom")
        if args[:2] == ["worktree", "remove"]:
            return _Completed(self._remove_rc, "", "" if not self._remove_rc else "fatal: busy")
        if args[:2] == ["worktree", "prune"]:
            return _Completed(0, "", "")
        return _Completed(1, "", f"unexpected run_git argv: {args}")

    def added_worktree_argv(self) -> list[str] | None:
        for a in self.run_git_calls:
            if a[:2] == ["worktree", "add"]:
                return a
        return None


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo path whose sibling ``release-channel-stable`` does not yet exist."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(repository, "_repo", lambda: str(checkout))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    return str(checkout)


def _install(
    monkeypatch,
    fake: _FakeGit,
    *,
    filter_key: str = "",
    filter_raises: bool = False,
    filter_exc: BaseException | None = None,
):
    monkeypatch.setattr(runtime, "_run_cmd", fake.run_cmd)
    monkeypatch.setattr(release_worktree, "_run_git", fake.run_git)

    def _filter(_root: str) -> str:
        if filter_raises:
            raise release_worktree.SandboxUnavailable("no sandbox backend")
        if filter_exc is not None:
            raise filter_exc
        return filter_key

    monkeypatch.setattr(release_worktree, "_checkout_filter", _filter)


@pytest.fixture(autouse=True)
def _default_agent_tier(_floor_monkeypatch):
    """Resolve the agent tier to ``standard`` by default, so the composition tests do
    not depend on the host's ``agent.sandbox`` (and skip a real config load). The two
    tier-specific tests override this with :func:`_set_agent_tier`.

    Patches through ``_floor_monkeypatch`` (D11): an autouse fixture must not use the
    shared ``monkeypatch``, which a test calling ``monkeypatch.undo()`` would lift."""
    _set_agent_tier(_floor_monkeypatch, "standard")


def _set_agent_tier(monkeypatch, tier: str) -> None:
    """Pin what :func:`release_worktree._network_tier` reads as the effective agent tier.

    ``effective_sandbox_mode`` is stubbed to identity (its governance-floor clamp is the
    sandbox layer's own contract, tested there); this fixes the *input* so the test
    exercises the threading of that tier into the network calls, which is this module's
    job."""
    monkeypatch.setattr(release_worktree.sandbox_tiers, "configured_sandbox_mode", lambda: tier)
    monkeypatch.setattr(release_worktree.sandbox_tiers, "effective_sandbox_mode", lambda m: m)


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_creates_detached_at_the_advertised_peeled_commit_newest_by_semver(repo, monkeypatch):
    """v0.10.0 wins over v0.9.0 by semver (not string order), and the oid checked out
    is the peeled commit the remote advertises for it."""
    fake = _FakeGit()
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert got["version"] == "0.10.0"
    assert got["ref"] == "refs/tags/v0.10.0"
    assert got["oid"] == _PEELED
    assert os.path.basename(got["path"]) == "release-channel-stable"
    add = fake.added_worktree_argv()
    assert add is not None and "--detach" in add and add[-1] == _PEELED
    # Every local step that reads or checks out the release's objects ignores refs/replace.
    assert add in fake.no_replace_calls
    assert ["rev-parse", "HEAD"] in fake.no_replace_calls
    assert any("--verify" in c for c in fake.no_replace_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "other",
    [
        "v01.9.0",  # a leading zero, which no release carries
        "v\u0669.0.0",  # ARABIC-INDIC DIGIT NINE: a bare `\d` matches it and int() reads 9
        "v\uff11.0.0",  # FULLWIDTH DIGIT ONE: the same, in another script
        "v1234567890.0.0",  # a part longer than nine digits
    ],
)
async def test_a_tag_outside_the_ascii_release_shape_never_outranks_the_tip(
    repo, monkeypatch, other
):
    """Only ``vX.Y.Z`` with ASCII digits, no leading zero and at most nine digits per
    part names a release. Each tag here would sort above ``v0.5.0`` if it counted, so
    a checkout of ``0.5.0`` is what shows it was ignored."""
    fake = _FakeGit(tags=("v0.5.0", other))
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert got["version"] == "0.5.0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "noise",
    [
        ("v" + "x" * 80,),
        tuple(f"vendor-{i}" for i in range(2100)),
    ],
    ids=["one-long-name", "many-names"],
)
async def test_a_tag_that_is_no_release_does_not_refuse_the_action(repo, monkeypatch, noise):
    """``refs/tags/v*`` also lists names that are no release. They are dropped before
    anything is counted or measured, so neither one long name nor a great many of them
    refuses the action; the network output cap is what bounds the listing."""
    fake = _FakeGit(tags=("v0.5.0", "v0.10.0", *noise))
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert got["version"] == "0.10.0"


@pytest.mark.parametrize(
    ("tag", "is_release"),
    [
        ("v0.0.0", True),
        ("v1.2.3", True),
        ("v10.20.30", True),
        ("v999999999.0.0", True),  # nine digits, the most a part may carry
        ("v1234567890.0.0", False),  # ten
        ("1.2.3", False),  # no "v"
        ("V1.2.3", False),
        ("v1.2", False),  # two parts
        ("v1.2.3.4", False),  # four
        ("v1..3", False),  # an empty part
        ("v1.2.", False),
        ("v01.2.3", False),  # a leading zero
        ("v1.2.03", False),
        ("v+1.2.3", False),  # int() reads these three; a release part does not
        ("v 1.2.3", False),
        ("v1_0.2.3", False),
        ("v\u0661.2.3", False),  # ARABIC-INDIC DIGIT ONE
        ("v1.2.3-rc.1", False),  # a prerelease
        ("v1.2.3\n", False),  # read whole: anything after the third part is no release
    ],
)
def test_a_release_tag_is_v_and_three_plain_ascii_parts(tag, is_release):
    """A release is ``v`` and exactly three dot-separated parts, each one to nine ASCII
    digits, either ``0`` or with no leading zero."""
    assert release_worktree.is_release_tag(tag) is is_release


@pytest.mark.asyncio
async def test_a_remote_with_no_stable_tag_is_unpublished_and_touches_nothing(repo, monkeypatch):
    fake = _FakeGit(tags=())
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "unpublished"
    assert not any("fetch" in c for c in fake.run_cmd_calls)
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_a_tag_the_confirm_no_longer_advertises_is_refused(repo, monkeypatch):
    """The list named it; the advertised-commit confirm returns nothing -> retracted
    between the two reads -> refuse, no fetch, no checkout."""
    fake = _FakeGit(advertised=None)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "unresolved"
    assert not any("fetch" in c for c in fake.run_cmd_calls)
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_a_commit_the_fetch_did_not_provide_is_a_provenance_mismatch(repo, monkeypatch):
    """The advertised oid is absent after the fetch (the remote served something else,
    or the tag moved): refuse rather than check out an unverified commit."""
    fake = _FakeGit(present_oid=None)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "provenance_mismatch"
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_a_declared_checkout_filter_is_refused_before_fetch_or_checkout(repo, monkeypatch):
    fake = _FakeGit()
    _install(monkeypatch, fake, filter_key="filter.evil.smudge")
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "filter_declared"
    assert "filter.evil.smudge" in got["error"]
    assert not any("fetch" in c for c in fake.run_cmd_calls), "must refuse before fetching"
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "promisor,partial",
    [
        ("remote.origin.promisor\ntrue\0", True),
        ("remote.origin.promisor\0", True),  # a key with no value is true in git
        ("remote.origin.promisor\n2\0", True),  # git reads any non-zero integer as true
        ("extensions.partialclone\norigin\0", True),
        ("core.bare\nfalse\0remote.Origin.promisor\ntrue\0", True),  # among other keys
        ("remote.origin.promisor\nfalse\0", False),  # a hydrated clone keeps the key
        ("remote.origin.promisor\nOff\0", False),
        ("remote.origin.promisor\n\0", False),  # an empty value is false in git
        ("alias.x\nfoo\nremote.origin.promisor\0", False),  # a value's newline is no key
        ("remote.origin.partialclonefilter\nblob:none\0", False),  # not a promisor key
    ],
)
async def test_a_partial_clone_is_refused_before_fetch_or_checkout(
    repo, monkeypatch, promisor, partial
):
    """A promisor remote in force makes the tag's files arrive lazily, from inside the
    checkout, so the clone is refused before any of it runs. The keys are matched exactly
    in the whole `git config --list -z` listing and read by value."""
    fake = _FakeGit(promisor=promisor)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    if not partial:
        assert got["ok"] is True, got
        return
    assert got["ok"] is False and got["code"] == "partial_clone", got
    assert not any("fetch" in c for c in fake.run_cmd_calls), "must refuse before fetching"
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_an_unreadable_config_listing_fails_closed_before_fetch(repo, monkeypatch):
    """`git config --list` exits 0 whatever keys are set, so any other exit means the
    promisor keys could not be read: the clone is refused `git_failed`, never assumed full."""
    fake = _FakeGit(config_rc=1)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "git_failed", got
    assert "promisor config" in got["error"]
    assert not any("fetch" in c for c in fake.run_cmd_calls), "must refuse before fetching"
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("length", "kept"),
    [(40, True), (64, True), (41, False), (50, False), (63, False), (39, False), (65, False)],
)
async def test_only_a_sha1_or_sha256_length_oid_is_read_from_the_advertisement(
    repo, monkeypatch, length, kept
):
    """git prints a sha1 (40 hex digits) or a sha256 (64) object id and no length in
    between, so a peel line whose oid has any other length names no commit and is
    skipped, leaving the tag line's own oid."""
    peel = "a" * length
    fake = _FakeGit(advertised=peel)
    _install(monkeypatch, fake)
    oid, err = await release_worktree.advertised_release_commit(repo, "v0.10.0", mode="standard")
    assert err is None
    assert oid == (peel if kept else _TAGOBJ)


@pytest.mark.asyncio
async def test_no_sandbox_backend_is_a_503_shaped_refusal(repo, monkeypatch):
    fake = _FakeGit()
    _install(monkeypatch, fake, filter_raises=True)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "sandbox_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["list", "confirm", "worktree-list", "fetch"])
async def test_a_missing_sandbox_answers_sandbox_unavailable_on_every_step(repo, monkeypatch, step):
    """A host with no OS sandbox backend that does not permit unsandboxed execution answers
    ``sandbox_unavailable`` (the route's 503) whichever step meets it first -- either
    ls-remote read or the fetch (``_run_cmd``'s refusal), or the strict worktree-list probe
    (``_run_git``'s ``SandboxUnavailable``) -- with the same bare reason. Nothing is
    checked out."""
    fake = _FakeGit(sandbox_missing_on=step)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got == {"ok": False, "code": "sandbox_unavailable", "error": "no OS sandbox backend"}
    assert fake.added_worktree_argv() is None
    if step != "fetch":
        assert not any(c for c in fake.run_cmd_calls if fake._step(c) == "fetch")


@pytest.mark.asyncio
async def test_every_network_step_engages_the_sandboxed_scan_and_the_output_cap(repo, monkeypatch):
    """The release path passes both ``_run_cmd`` opt-ins on all three network steps --
    the two ls-remote reads and the fetch -- and the local worktree-list probe runs
    strict through ``_run_git``, never through ``_run_cmd``."""
    fake = _FakeGit()
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    network = [kw for step in ("list", "confirm", "fetch") for kw in fake.kwargs_by_step[step]]
    assert len(network) == 3
    for kw in network:
        assert kw.get("sandboxed_hook_scan") is True
        assert kw.get("max_output_bytes") == release_worktree._MAX_NETWORK_OUTPUT_BYTES
    assert ["worktree", "list", "--porcelain"] in fake.run_git_calls
    assert not any("worktree" in c for c in fake.run_cmd_calls)


@pytest.mark.asyncio
async def test_an_existing_destination_is_refused(repo, monkeypatch):
    (Path(repo).parent / release_worktree.WORKTREE_NAME).mkdir()
    fake = _FakeGit()
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "path_exists"
    assert not any("fetch" in c for c in fake.run_cmd_calls)


@pytest.mark.asyncio
async def test_a_worktree_under_the_same_name_elsewhere_is_refused_before_fetch(repo, monkeypatch):
    """A worktree already registered under the `release-channel-stable` basename at a
    DIFFERENT path owns the fleet-row identity this action would claim, so it refuses
    `name_taken` before the fetch -- `_dest_refusal`'s path-only check would miss it."""
    other = str(Path(repo).parent / "elsewhere" / release_worktree.WORKTREE_NAME)
    fake = _FakeGit(worktrees=(other,))
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "name_taken"
    assert release_worktree.WORKTREE_NAME in got["error"]
    assert not any("fetch" in c for c in fake.run_cmd_calls), "must refuse before fetching"
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_the_registered_path_comparison_runs_off_the_event_loop(repo, monkeypatch):
    """`repository._same_path` resolves both paths, a stat per component that blocks in
    the kernel for as long as an NFS or FUSE mount takes to answer. On the gateway's one
    loop that stalls every session, so it runs on a worker thread, like the module's
    other filesystem checks."""
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real_same_path = repository._same_path

    def _recording_same_path(a: str, b: str) -> bool:
        threads.append(threading.get_ident())
        return real_same_path(a, b)

    monkeypatch.setattr(repository, "_same_path", _recording_same_path)
    other = str(Path(repo).parent / "elsewhere" / release_worktree.WORKTREE_NAME)
    fake = _FakeGit(worktrees=(other,))
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["code"] == "name_taken"
    assert threads, "the registered path was never compared"
    assert loop_thread not in threads, "the path comparison ran on the event loop"


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [False, True])
async def test_a_record_left_at_the_reserved_path_is_refused_with_how_to_clear_it(
    repo, monkeypatch, locked
):
    """A `release-channel-stable` record at the reserved path ITSELF, with no directory
    there -- what a create killed mid-checkout leaves once the tree is deleted -- is
    refused `name_taken` before the fetch, naming the git command that clears it (an
    unlock first when git kept the add's lock). git would not add over it, and the
    cleanup's forced remove must only ever meet a record this request made."""
    dest = release_worktree.worktree_path(repo)
    fake = _FakeGit(worktrees=(dest,), locked=(dest,) if locked else ())
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "name_taken"
    assert "git worktree prune" in got["error"]
    assert ("git worktree unlock" in got["error"]) is locked
    assert not any("fetch" in c for c in fake.run_cmd_calls), "must refuse before fetching"
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_an_unreadable_worktree_list_fails_closed_before_fetch(repo, monkeypatch):
    """The name check cannot clear a basename it could not read, so a listing that fails
    refuses `git_failed`, with git's reason, before the fetch and the checkout."""
    fake = _FakeGit(list_rc=128)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "git_failed"
    assert "cannot read worktree records" in got["error"]
    assert not any("fetch" in c for c in fake.run_cmd_calls), "must refuse before fetching"
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_a_failed_worktree_add_releases_only_the_claim(repo, monkeypatch):
    """A `worktree add` that exits non-zero has already removed what it created, so the
    action releases the directory it claimed and removes no worktree record."""
    fake = _FakeGit(add_rc=128)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "git_failed"
    assert not Path(release_worktree.worktree_path(repo)).exists(), "the claim was not released"
    assert not any(a[:2] == ["worktree", "remove"] for a in fake.run_git_calls)


@pytest.mark.asyncio
async def test_an_add_that_lost_the_path_to_another_writer_leaves_that_tree_alone(
    repo, monkeypatch
):
    """Another writer takes the claimed empty directory before this request's add runs;
    git then refuses this add and leaves the other tree in place. The action refuses and
    touches nothing of that writer's: no forced remove, and its file survives."""
    fake = _FakeGit(add_loses_path=True)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "git_failed"
    theirs = Path(release_worktree.worktree_path(repo)) / "theirs.txt"
    assert theirs.read_text(encoding="utf-8") == "another writer's tree\n"
    assert not any(a[:2] == ["worktree", "remove"] for a in fake.run_git_calls)


@pytest.mark.asyncio
async def test_a_tree_that_did_not_land_detached_is_removed(repo, monkeypatch):
    """The add succeeded, so the record is this request's own: a readback that finds the
    tree not detached at the resolved commit removes it and refuses."""
    fake = _FakeGit(detached=False)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "git_failed"
    dest = release_worktree.worktree_path(repo)
    assert ["worktree", "remove", "--force", "--force", dest] in fake.run_git_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["worktree-list", "filter", "promisor", "verify"])
async def test_a_local_step_that_cannot_complete_refuses_instead_of_raising(
    repo, monkeypatch, step
):
    """`_run_git` raises when git times out or cannot be spawned. A local step before the
    checkout that meets that refuses `git_failed` -- it never escapes as a 500 -- and
    nothing is checked out."""
    timeout = subprocess.TimeoutExpired(cmd="git", timeout=FALLBACK_TIMEOUT_SECS)
    fake = _FakeGit(times_out_on=step if step != "filter" else None)
    _install(monkeypatch, fake, filter_exc=timeout if step == "filter" else None)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "git_failed", got
    assert fake.added_worktree_argv() is None


@pytest.mark.asyncio
async def test_a_racing_destination_claim_is_refused_without_clobbering(repo, monkeypatch):
    """The atomic `os.mkdir` claim: if the destination appears after `_dest_refusal`'s
    check but before the checkout, the mkdir fails and the action refuses `path_exists`
    WITHOUT running `worktree add` or cleanup -- so a tree a concurrent writer put at the
    reserved name is never force-removed. The race is real: the check passes, then a
    writer creates the directory, with a file in it that must survive."""
    fake = _FakeGit()
    _install(monkeypatch, fake)
    dest = Path(release_worktree.worktree_path(repo))
    passes = release_worktree._dest_refusal

    def _check_then_lose_the_race(repo_path: str, path: str):
        verdict = passes(repo_path, path)  # nothing there yet: the check passes
        dest.mkdir()
        (dest / "theirs.txt").write_text("a concurrent writer's tree\n", encoding="utf-8")
        return verdict

    monkeypatch.setattr(release_worktree, "_dest_refusal", _check_then_lose_the_race)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "path_exists"
    assert fake.added_worktree_argv() is None
    assert not any(a[:2] == ["worktree", "remove"] for a in fake.run_git_calls)
    assert (dest / "theirs.txt").read_text(encoding="utf-8") == "a concurrent writer's tree\n"


@pytest.mark.asyncio
async def test_a_worktree_add_timeout_removes_nothing_and_says_how_to_clear_it(repo, monkeypatch):
    """On a timeout the checkout may still be finishing, so a removal now would race it.
    The action removes nothing, refuses `git_failed` rather than escaping as a 500, and
    names the command that clears the tree once it stops."""
    fake = _FakeGit(
        add_exc=subprocess.TimeoutExpired(cmd="git worktree add", timeout=FALLBACK_TIMEOUT_SECS)
    )
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    dest = release_worktree.worktree_path(repo)
    assert got["ok"] is False and got["code"] == "git_failed"
    assert "may still be finishing" in got["error"]
    assert f"git worktree remove -f -f {dest}" in got["error"]
    assert not any(
        a[:2] in (["worktree", "remove"], ["worktree", "prune"]) for a in fake.run_git_calls
    )
    assert Path(dest).is_dir(), "the timeout path must not touch the destination"


@pytest.mark.asyncio
async def test_a_refused_tree_git_cannot_remove_is_left_with_how_to_clear_it(repo, monkeypatch):
    """The readback refused a tree the add made, and git cannot remove it: nothing
    deletes the path by name -- a tree another writer put there would go with it -- so
    the tree stays and the refusal names the command that clears it."""
    fake = _FakeGit(detached=False, remove_rc=1)
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    dest = release_worktree.worktree_path(repo)
    assert got["ok"] is False and got["code"] == "git_failed"
    assert f"git worktree remove -f -f {dest}" in got["error"]
    assert Path(dest).is_dir(), "the tree was deleted by path after git refused to remove it"
    assert ["worktree", "prune"] not in fake.run_git_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("add_rc", [0, 128])
async def test_a_cancel_mid_checkout_settles_the_transaction_before_it_unwinds(
    repo, monkeypatch, add_rc
):
    """A gateway restart cancels the request while `git worktree add` runs in its worker
    thread. The cancel must not unwind past the transaction: the checkout finishes, a
    success is read back and a failure's claim is released, `_CREATE_LOCK` stays held
    until then, and only after that does the cancellation propagate."""
    entered = threading.Event()
    proceed = threading.Event()
    fake = _FakeGit(add_rc=add_rc)
    _install(monkeypatch, fake)
    answer = fake.run_git

    def _held_add(args, cwd, **kw):
        if "worktree" in args and "add" in args:
            entered.set()
            assert proceed.wait(timeout=10), "the test never released the checkout"
        return answer(args, cwd, **kw)

    monkeypatch.setattr(release_worktree, "_run_git", _held_add)
    task = asyncio.create_task(release_worktree.create_release_worktree())
    try:
        await async_wait_until(entered.is_set, describe=lambda: "the checkout never started")
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done(), "the cancel unwound the request mid-checkout"
        assert release_worktree._CREATE_LOCK.locked(), "the lock was released mid-checkout"
    finally:
        proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not release_worktree._CREATE_LOCK.locked()
    dest = release_worktree.worktree_path(repo)
    assert not any(a[:2] == ["worktree", "remove"] for a in fake.run_git_calls)
    if add_rc == 0:
        assert ["rev-parse", "HEAD"] in fake.run_git_calls, "the readback was skipped"
    else:
        assert not Path(dest).exists(), "the claim was not released"


@pytest.mark.asyncio
async def test_network_steps_run_at_standard_on_the_default_install(repo, monkeypatch):
    """``agent.sandbox = standard``: the two ls-remote reads and the fetch all run
    at ``standard``, unchanged from before -- the clamp is a no-op here."""
    fake = _FakeGit()
    _install(monkeypatch, fake)
    _set_agent_tier(monkeypatch, "standard")
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert fake.run_cmd_modes and all(m == "standard" for m in fake.run_cmd_modes)


@pytest.mark.asyncio
async def test_the_shipped_auto_default_reaches_the_network_steps_as_standard(repo, monkeypatch):
    """The shipped ``agent.sandbox`` default is the alias ``auto``. ``_run_cmd`` adds the
    credential-helper env only for the literal ``standard``, so the network steps must see
    the canonical name: every call runs at ``standard`` and a failed fetch carries no
    tier hint."""
    _set_agent_tier(monkeypatch, "auto")
    assert release_worktree._network_tier() == "standard"
    failing = _FakeGit(fetch_rc=128)
    _install(monkeypatch, failing)
    refused = await release_worktree.create_release_worktree()
    assert refused["ok"] is False and refused["code"] == "fetch_failed"
    assert "tier" not in refused["error"]
    fake = _FakeGit()
    _install(monkeypatch, fake)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert fake.run_cmd_modes and all(m == "standard" for m in fake.run_cmd_modes)


@pytest.mark.asyncio
async def test_network_steps_clamp_up_to_the_agent_tier_on_a_strict_install(repo, monkeypatch):
    """Option (a): on a ``strict`` install the network steps (ls-remote x2, fetch) clamp UP
    to ``strict`` instead of the hardcoded ``standard``, so no git child is less confined
    than an agent's own commands, within the scope ``release_worktree``'s module docstring
    states."""
    fake = _FakeGit()
    _install(monkeypatch, fake)
    _set_agent_tier(monkeypatch, "strict")
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert fake.run_cmd_modes and all(m == "strict" for m in fake.run_cmd_modes)


@pytest.mark.asyncio
async def test_a_private_remote_unfetchable_at_a_stricter_tier_is_refused_with_a_worded_reason(
    repo, monkeypatch
):
    """The cost of the clamp, surfaced: at ``cc``/``strict`` credentials are withheld, so a
    private remote's fetch fails and the refusal names the tier and the cause rather than
    reporting a bare git error."""
    fake = _FakeGit(fetch_rc=128)
    _install(monkeypatch, fake)
    _set_agent_tier(monkeypatch, "strict")
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "fetch_failed"
    assert "strict" in got["error"] and "private remote" in got["error"]
    # A standard install gets no tier hint (credentials are present there).
    fake2 = _FakeGit(fetch_rc=128)
    _install(monkeypatch, fake2)
    _set_agent_tier(monkeypatch, "standard")
    got2 = await release_worktree.create_release_worktree()
    assert got2["ok"] is False and got2["code"] == "fetch_failed"
    assert "tier" not in got2["error"]


def test_the_checkout_uses_the_strict_tier_gateway_primitives():
    """The property no unit test can run -- that the checkout masks ~/.aws and an
    include.path pointing at it -- is the STRICT tier's. Pin that the action uses
    exactly ``worktree.py``'s strict-tier ``_run_git`` and filter-refusing
    ``_checkout_filter`` rather than a re-implementation, so that guarantee holds."""
    assert dash_worktree._SANDBOX_MODE == "strict"
    assert release_worktree._run_git is dash_worktree._run_git
    assert release_worktree._checkout_filter is dash_worktree._checkout_filter


# --------------------------------------------------------------------------
# real git: filter refused and never run, hook suppressed, clean repo lands
# --------------------------------------------------------------------------
def _fixture_env(home: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.invalid",
    }
    # This env points GIT_CONFIG_GLOBAL away from the suite's floor, so it carries the
    # floor's auto-maintenance-off keys itself: no commit here leaves a detached
    # `git maintenance run --auto` behind.
    env.update(git_maintenance_off_entries(env))
    return env


def _git(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def _neutralized_net_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with the production NETWORK neutralizers applied, as ``runtime._run_cmd``
    applies them to every ls-remote/fetch (``_GIT_ENV_NEUTRALIZERS``: transports pinned,
    ``core.sshCommand`` pinned to ``ssh``, ``credential.helper`` reset, gpg/hooks off).

    ``GIT_ALLOW_PROTOCOL`` is widened to admit the ``file`` transport the local-origin
    fixtures use -- production pins it to ``https:ssh`` -- and ``GIT_PROTOCOL_FROM_USER``
    is dropped so the file transport is not re-blocked by policy. That is the only
    transport deviation: the vectors these tests probe (a ``url.*.insteadOf`` rewrite, a
    planted ``core.sshCommand``) are orthogonal to which transports are allow-listed, so
    running them with every GIT_CONFIG_* injection in force is the faithful case. The
    suite's auto-maintenance-off keys are appended after the neutralizers' own entries,
    which they leave as they are.
    """
    merged = dict(env)
    merged.update(runtime._GIT_ENV_NEUTRALIZERS)
    merged["GIT_ALLOW_PROTOCOL"] = "file:https:ssh"
    merged.pop("GIT_PROTOCOL_FROM_USER", None)
    # The neutralizers' own GIT_CONFIG_COUNT replaced the fixture's, and a fetch can
    # start auto maintenance too, so the auto-maintenance-off keys go back on after them.
    merged.update(git_maintenance_off_entries(merged))
    return merged


def _direct_runners(monkeypatch, env: dict[str, str], *, net_env: dict[str, str] | None = None):
    """Replace the action's git layer with DIRECT (non-sandboxed) runners that apply
    the same ``-c`` overrides and ``--includes`` filter probe the strict-tier path
    uses, so the action's behaviour is proven against real git without needing a
    sandbox backend under the test.

    ``net_env`` overrides the environment the NETWORK runner (``runtime._run_cmd``:
    ls-remote, fetch) uses, defaulting to ``env``. Pass :func:`_neutralized_net_env`
    to run those calls under the production git neutralizers for fidelity -- the local
    strict-tier calls (``_run_git``, ``_checkout_filter``) keep ``env``.
    """

    net = net_env if net_env is not None else env
    _no_code = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
    _filter_re = re.compile(r"^filter\.(?P<n>.*)\.(process|smudge|clean)$", re.IGNORECASE)

    async def _run_cmd(cmd, **_kw):
        # cmd is ["git", "-C", repo, ...]; run it directly under the network env.
        p = _git(net, Path(cmd[2]), *cmd[3:])
        return p.returncode, p.stdout, p.stderr

    def _run_git(args, cwd, **_kw):
        p = _git(env, Path(cwd), *_no_code, *args)
        return _Completed(p.returncode, p.stdout, p.stderr)

    def _checkout_filter(root):
        p = _git(env, Path(root), "config", "--local", "--includes", "--name-only", "--list")
        if p.returncode != 0:
            return "unreadable git config"
        for key in p.stdout.splitlines():
            if _filter_re.match(key.strip()):
                return key.strip()[:120]
        return ""

    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(release_worktree, "_run_git", _run_git)
    monkeypatch.setattr(release_worktree, "_checkout_filter", _checkout_filter)


def _origin_with_tag(
    tmp_path: Path, env: dict[str, str], tag: str, *, gitattributes: str | None = None
) -> Path:
    """A local origin carrying one annotated release tag; return a fresh clone.

    ``gitattributes`` seeds the file INTO the tagged commit, so a filter it names is
    bound when that tag is checked out (a binding added after the tag would not be in
    the tag's tree and would not run).
    """
    origin = tmp_path / "origin_repo"
    origin.mkdir()
    _git(env, origin, "init", "-q")
    (origin / "payload.txt").write_text("release payload\n", encoding="utf-8")
    _git(env, origin, "add", "payload.txt")
    if gitattributes is not None:
        (origin / ".gitattributes").write_text(gitattributes, encoding="utf-8")
        _git(env, origin, "add", ".gitattributes")
    _git(env, origin, "commit", "-qm", "one")
    assert _git(env, origin, "tag", "-a", tag, "-m", tag).returncode == 0
    clone = tmp_path / "checkout"
    assert _git(env, tmp_path, "clone", "-q", str(origin), "checkout").returncode == 0
    return clone


def _ref_map(env: dict[str, str], cwd: Path) -> dict[str, str]:
    """Every ref in ``cwd`` as ``{refname: objectname}`` -- names AND values, so a
    ref whose value moved (a recursed submodule's ``refs/remotes/origin/*``) is caught."""
    proc = _git(env, cwd, "for-each-ref", "--format=%(refname) %(objectname)")
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, _, oid = line.partition(" ")
        if name:
            out[name] = oid
    return out


#: The sweep (AGENTS.md): each repository or global config that could make the action's
#: one fetch write/update/delete a ref beyond the tag's objects, or fetch elsewhere.
#: ``--refmap=``/``--no-tags`` hold the refspec and tag-map members, ``-c fetch.bundleURI=``
#: the bundle member, ``--no-recurse-submodules`` the submodule members; the rest are
#: negatives pinned so a later argv change that drops a flag turns one of them red.
_SWEEP_MEMBERS = [
    ("baseline", "plain"),
    ("fetch.prune", "plain"),
    ("fetch.prune+pruneTags", "plain"),
    ("remote.prune+pruneTags", "plain"),
    ("tagmap+prune+pruneTags", "plain"),
    ("mirror", "plain"),
    ("tagOpt", "plain"),
    ("fetch.bundleURI", "bundle"),
    ("submodule:on-demand", "submodule"),
    ("submodule:fetch.recurseSubmodules", "submodule"),
    ("submodule:submodule.recurse", "submodule"),
]


def _build_sweep_fixture(
    tmp_path: Path, env: dict[str, str], member: str, kind: str
) -> tuple[Path, dict[str, str]]:
    """A clone whose config exercises one sweep *member*. Its release tag is made AFTER
    the clone, so the action must really fetch to get the tag's objects. A ``local-only``
    tag is planted that nothing the action does may prune. Returns ``(clone, ctx)``;
    ``ctx['evil']`` is the commit a bundle pull would smuggle in (bundle member only)."""
    allow = ("-c", "protocol.file.allow=always")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(env, origin, "init", "-q", "-b", "main")
    (origin / "payload.txt").write_text("a\n", encoding="utf-8")
    _git(env, origin, "add", "payload.txt")
    _git(env, origin, "commit", "-qm", "A")
    sub = tmp_path / "subrepo"
    if kind == "submodule":
        sub.mkdir()
        _git(env, sub, "init", "-q", "-b", "main")
        (sub / "s.txt").write_text("s1\n", encoding="utf-8")
        _git(env, sub, "add", "s.txt")
        _git(env, sub, "commit", "-qm", "S1")
        assert _git(env, origin, *allow, "submodule", "add", str(sub), "sub").returncode == 0
        _git(env, origin, "commit", "-qm", "add sub")
    clone = tmp_path / "clone"
    if kind == "submodule":
        assert (
            _git(env, tmp_path, *allow, "clone", "-q", "--recurse-submodules", str(origin), "clone")
        ).returncode == 0
    else:
        assert _git(env, tmp_path, "clone", "-q", str(origin), "clone").returncode == 0
    # The release the action fetches, created AFTER the clone so its objects are remote.
    if kind == "submodule":
        (sub / "s.txt").write_text("s2\n", encoding="utf-8")
        _git(env, sub, "commit", "-qam", "S2")
        _git(env, origin / "sub", *allow, "fetch", "-q", "origin")
        _git(env, origin / "sub", "checkout", "-q", "origin/main")
        _git(env, origin, "add", "sub")
        _git(env, origin, "commit", "-qm", "bump sub")
    else:
        (origin / "payload.txt").write_text("c\n", encoding="utf-8")
        _git(env, origin, "commit", "-qam", "C")
    _git(env, origin, "tag", "-a", "v0.11.0", "-m", "v0.11.0")
    _git(env, clone, "tag", "local-only")

    ctx: dict[str, str] = {}
    if member in ("fetch.prune", "fetch.prune+pruneTags"):
        _git(env, clone, "config", "fetch.prune", "true")
        if member.endswith("pruneTags"):
            _git(env, clone, "config", "fetch.pruneTags", "true")
    elif member == "remote.prune+pruneTags":
        _git(env, clone, "config", "remote.origin.prune", "true")
        _git(env, clone, "config", "remote.origin.pruneTags", "true")
    elif member == "tagmap+prune+pruneTags":
        _git(env, clone, "config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*")
        _git(env, clone, "config", "remote.origin.prune", "true")
        _git(env, clone, "config", "remote.origin.pruneTags", "true")
    elif member == "mirror":
        _git(env, clone, "config", "--replace-all", "remote.origin.fetch", "+refs/*:refs/*")
        _git(env, clone, "config", "remote.origin.mirror", "true")
    elif member == "tagOpt":
        _git(env, clone, "config", "remote.origin.tagOpt", "--tags")
    elif member == "fetch.bundleURI":
        evil = tmp_path / "evilrepo"
        evil.mkdir()
        _git(env, evil, "init", "-q", "-b", "evil")
        (evil / "e.txt").write_text("e\n", encoding="utf-8")
        _git(env, evil, "add", "e.txt")
        _git(env, evil, "commit", "-qm", "E")
        bundle = tmp_path / "evil.bundle"
        assert _git(env, evil, "bundle", "create", "-q", str(bundle), "--all").returncode == 0
        ctx["evil"] = _git(env, evil, "rev-parse", "HEAD").stdout.strip()
        _git(env, clone, "config", "fetch.bundleURI", str(bundle))
    elif member == "submodule:fetch.recurseSubmodules":
        _git(env, clone, "config", "fetch.recurseSubmodules", "yes")
    elif member == "submodule:submodule.recurse":
        _git(env, clone, "config", "submodule.recurse", "true")
    elif member in ("baseline", "submodule:on-demand"):
        pass
    else:  # pragma: no cover - a member name typo, not a product path
        raise AssertionError(f"unknown sweep member {member}")
    return clone, ctx


@pytest.mark.asyncio
@requires_git
@pytest.mark.parametrize("member,kind", _SWEEP_MEMBERS)
async def test_real_git_no_config_makes_the_one_fetch_write_a_ref_or_fetch_elsewhere(
    tmp_path, monkeypatch, member, kind
):
    """The class, swept once (AGENTS.md): no repository or global config may make the
    action's one fetch write, update or delete a ref beyond the tag's objects, or fetch in
    another repository. Every member runs the real production argv against real git; the
    fetch must leave the clone's ref map untouched, write no FETCH_HEAD, pull no bundle
    ref, and run no fetch inside a submodule."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    clone, ctx = _build_sweep_fixture(tmp_path, env, member, kind)
    before = _ref_map(env, clone)
    before_sub = _ref_map(env, clone / "sub") if kind == "submodule" else {}
    assert "refs/tags/local-only" in before

    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    # Run the fetch under the production network neutralizers, widened to the `file`
    # transport for these local fixtures -- the env in which a submodule recursion is
    # reachable, so `--no-recurse-submodules` is what stops it here, not a blocked transport.
    _direct_runners(monkeypatch, env, net_env=_neutralized_net_env(env))

    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got

    after = _ref_map(env, clone)
    assert (
        after == before
    ), f"{member}: the fetch changed a ref: {set(before.items()) ^ set(after.items())}"
    assert "refs/tags/local-only" in after
    assert not (clone / ".git" / "FETCH_HEAD").exists()
    if kind == "submodule":
        assert (
            _ref_map(env, clone / "sub") == before_sub
        ), f"{member}: a fetch ran inside the submodule"
        assert not (clone / ".git" / "modules" / "sub" / "FETCH_HEAD").exists()
    if member == "fetch.bundleURI":
        assert not any(
            name.startswith("refs/bundles/") for name in after
        ), "a bundle pull wrote a ref"
        assert (
            _git(env, clone, "cat-file", "-e", ctx["evil"]).returncode != 0
        ), "a bundle pull brought an unrelated commit into the store"


@pytest.mark.asyncio
@requires_git
async def test_real_git_clean_repo_lands_detached_on_the_peeled_commit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    clone = _origin_with_tag(tmp_path, env, "v0.10.0")
    peeled = _git(env, clone, "rev-list", "-n1", "v0.10.0").stdout.strip()
    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env)

    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert got["version"] == "0.10.0" and got["oid"] == peeled
    dest = Path(got["path"])
    assert dest.is_dir() and (dest / "payload.txt").read_text() == "release payload\n"
    # Detached exactly at the peeled commit.
    assert _git(env, dest, "rev-parse", "HEAD").stdout.strip() == peeled
    assert _git(env, dest, "symbolic-ref", "--quiet", "HEAD").returncode != 0


@pytest.mark.asyncio
@requires_git
async def test_real_git_the_one_tag_fetch_starts_no_auto_maintenance(tmp_path, monkeypatch):
    """A plain ``git fetch`` ends by starting ``git maintenance run --auto``, which by
    default detaches and keeps working on the shared ``.git`` after the request has
    answered. The action's fetch starts none. The suite turns auto maintenance off for
    every git, so this fetch runs without those keys, with the clone's maintenance held
    in the foreground so a regression leaves no process behind, and git's own trace shows
    whether maintenance ran."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    origin = _make_origin(tmp_path, env, "origin_repo", "v0.9.0", "one\n")
    assert _git(env, tmp_path, "clone", "-q", origin.as_posix(), "checkout").returncode == 0
    clone = tmp_path / "checkout"
    for key in ("maintenance.autoDetach", "gc.autoDetach"):
        assert _git(env, clone, "config", key, "false").returncode == 0
    # The release is cut after the clone, so the action's fetch brings new objects.
    (origin / "p.txt").write_text("two\n", encoding="utf-8")
    _git(env, origin, "commit", "-qam", "two")
    assert _git(env, origin, "tag", "-a", "v0.10.0", "-m", "v0.10.0").returncode == 0
    trace = tmp_path / "git-trace.txt"
    # The fixture's only GIT_CONFIG_COUNT entries are the suite's maintenance-off keys.
    net = {
        k: v
        for k, v in env.items()
        if not k.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    net["GIT_TRACE"] = str(trace)
    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env, net_env=net)

    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True and got["version"] == "0.10.0", got
    assert trace.is_file(), "git wrote no trace, so the check below would prove nothing"
    traced = trace.read_text(encoding="utf-8")
    assert "fetch" in traced
    assert "maintenance run" not in traced and "gc --auto" not in traced


@pytest.mark.asyncio
@requires_git
async def test_real_git_a_partial_clone_is_refused_and_its_remedy_clears_it(tmp_path, monkeypatch):
    """A ``--filter=blob:none`` clone fetches the release tag through its filter, so the
    release's files are not local and ``worktree add`` would fetch them itself. It is
    refused before the fetch, and the hydration the refusal names clears the refusal."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    origin = tmp_path / "origin_repo"
    origin.mkdir()
    _git(env, origin, "init", "-q")
    (origin / "payload.txt").write_text("one\n", encoding="utf-8")
    _git(env, origin, "add", "payload.txt")
    _git(env, origin, "commit", "-qm", "one")
    _git(env, origin, "config", "uploadpack.allowFilter", "true")
    # A file:// URL, not a path: git ignores --filter on a local-path clone.
    cloned = _git(env, tmp_path, "clone", "-q", "--filter=blob:none", origin.as_uri(), "checkout")
    assert cloned.returncode == 0, cloned.stderr
    clone = tmp_path / "checkout"
    assert _git(env, clone, "config", "--get", "remote.origin.promisor").stdout.strip() == "true"
    # The release is cut after the clone, so its payload is a blob the clone does not hold.
    (origin / "payload.txt").write_text("release payload\n", encoding="utf-8")
    _git(env, origin, "commit", "-qam", "two")
    assert _git(env, origin, "tag", "-a", "v0.10.0", "-m", "v0.10.0").returncode == 0
    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env)
    network_calls: list[list[str]] = []
    direct_run_cmd = runtime._run_cmd

    async def _recording_run_cmd(cmd, **kw):
        network_calls.append(list(cmd))
        return await direct_run_cmd(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", _recording_run_cmd)

    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "partial_clone", got
    assert not any("fetch" in c for c in network_calls), "refused before the fetch"
    assert not Path(release_worktree.worktree_path(str(clone))).exists()

    # Each command the refusal names, run as worded with `<name>` the clone's remote. The
    # `extensions.partialclone` step applies only where git set that key; it did not here.
    for command in re.findall(r"`(git [^`]+)`", got["error"]):
        if "extensions.partialclone" in command:
            continue
        argv = command.replace("<name>", "origin").split()[1:]
        assert _git(env, clone, *argv).returncode == 0, command
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert (Path(got["path"]) / "payload.txt").read_text(encoding="utf-8") == "release payload\n"


@pytest.mark.asyncio
@requires_git
async def test_real_git_a_replace_ref_does_not_swap_the_checked_out_tree(tmp_path, monkeypatch):
    """A ``refs/replace`` ref mapping the release commit to one with different files
    would make a plain checkout land that other tree while ``HEAD`` still reads the
    advertised oid. The action ignores replacement refs, so the tree is the release's."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    clone = _origin_with_tag(tmp_path, env, "v0.10.0")
    peeled = _git(env, clone, "rev-list", "-n1", "v0.10.0").stdout.strip()
    release_tree = _git(env, clone, "rev-parse", f"{peeled}^{{tree}}").stdout.strip()
    # A substitute commit with different files, then the graft replacing the release.
    (clone / "payload.txt").write_text("substituted payload\n", encoding="utf-8")
    assert _git(env, clone, "commit", "-qam", "substitute").returncode == 0
    substitute = _git(env, clone, "rev-parse", "HEAD").stdout.strip()
    assert _git(env, clone, "replace", peeled, substitute).returncode == 0
    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env)

    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    dest = Path(got["path"])
    assert (dest / "payload.txt").read_text(encoding="utf-8") == "release payload\n"
    no_replace = ("--no-replace-objects",)
    assert _git(env, dest, *no_replace, "rev-parse", "HEAD").stdout.strip() == peeled
    assert _git(env, dest, *no_replace, "write-tree").stdout.strip() == release_tree


@pytest.mark.asyncio
@requires_git
async def test_real_git_a_tag_mapping_fetch_refspec_writes_no_local_tag(tmp_path, monkeypatch):
    """A ``remote.<name>.fetch`` that maps tags makes a plain ``git fetch <remote> <tag>``
    write the tag through that mapping; the action's fetch writes no ref and no
    ``FETCH_HEAD`` there."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    clone = _origin_with_tag(tmp_path, env, "v0.10.0")
    assert _git(env, clone, "tag", "-d", "v0.10.0").returncode == 0
    tag_map = ("remote.origin.fetch", "+refs/tags/*:refs/tags/*")
    assert _git(env, clone, "config", "--add", *tag_map).returncode == 0
    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env)

    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert _git(env, clone, "rev-parse", "-q", "--verify", "refs/tags/v0.10.0").returncode != 0
    assert not (clone / ".git" / "FETCH_HEAD").exists()


@pytest.mark.asyncio
@requires_git
async def test_real_git_a_planted_filter_is_refused_and_never_runs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    # The binding lives in the TAG's own tree; the driver is added to the clone's
    # config. On checkout of the tag the smudge is bound and would run.
    clone = _origin_with_tag(tmp_path, env, "v0.10.0", gitattributes="payload.txt filter=evil\n")
    marker = tmp_path / "filter-ran"
    import shlex as _shlex

    _git(
        env,
        clone,
        "config",
        "--local",
        "filter.evil.smudge",
        f"sh -c 'printf ran > {_shlex.quote(marker.as_posix())}; cat'",
    )
    _git(env, clone, "config", "--local", "filter.evil.required", "true")

    # Control: a plain checkout of that tree RUNS the driver (the threat is real).
    peeled = _git(env, clone, "rev-list", "-n1", "v0.10.0").stdout.strip()
    ctrl = tmp_path / "control"
    assert _git(env, clone, "worktree", "add", "--detach", str(ctrl), peeled).returncode == 0
    assert marker.exists(), "fixture never armed the filter: the control checkout did not run it"
    marker.unlink()

    # The action: the filter is DECLARED, so it is refused before any checkout; the
    # driver never runs and no worktree is created.
    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "filter_declared"
    assert not marker.exists(), "the declared filter ran despite the refusal"
    assert not (Path(clone).parent / release_worktree.WORKTREE_NAME).exists()


@pytest.mark.asyncio
@requires_git
async def test_real_git_a_credential_shaped_filter_name_is_redacted_from_the_refusal(
    tmp_path, monkeypatch
):
    """A filter's NAME is the repository's choice, and the ``filter_declared`` refusal
    quotes it, so a credential-shaped name must not reach the refusal's ``error``."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    clone = _origin_with_tag(tmp_path, env, "v0.10.0")
    token = "ghp_" + "a" * 36
    assert _git(env, clone, "config", "--local", f"filter.{token}.smudge", "cat").returncode == 0
    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is False and got["code"] == "filter_declared", got
    assert token not in got["error"]
    assert "content filter" in got["error"]
    assert not (Path(clone).parent / release_worktree.WORKTREE_NAME).exists()


@pytest.mark.asyncio
@requires_git
async def test_real_git_a_post_checkout_hook_does_not_run(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    clone = _origin_with_tag(tmp_path, env, "v0.10.0")
    marker = tmp_path / "hook-ran"
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\nprintf ran > {marker.as_posix()}\n", encoding="utf-8")
    hook.chmod(0o755)

    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env)
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert not marker.exists(), "the repo's post-checkout hook ran despite core.hooksPath=/dev/null"


@pytest.mark.asyncio
@requires_git
async def test_real_git_own_tree_removal_clears_even_a_locked_record(tmp_path, monkeypatch):
    """The readback's removal of a tree the add made is `git worktree remove` forced
    twice, so a lock on the record -- here the `initializing` lock git writes while it
    checks out, which a single `--force` refuses -- cannot stop it. Against real git, and
    git alone removes the tree."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    clone = _origin_with_tag(tmp_path, env, "v0.10.0")
    peeled = _git(env, clone, "rev-list", "-n1", "v0.10.0").stdout.strip()
    dest = release_worktree.worktree_path(str(clone))
    assert _git(env, clone, "worktree", "add", "--detach", dest, peeled).returncode == 0
    record = Path(_git(env, Path(dest), "rev-parse", "--absolute-git-dir").stdout.strip())
    (record / "locked").write_text("initializing\n", encoding="utf-8")  # as a killed add leaves it
    # Control: one --force is refused on the locked record, so the plant is the real shape.
    assert _git(env, clone, "worktree", "remove", "--force", dest).returncode != 0
    _direct_runners(monkeypatch, env)

    assert await release_worktree._remove_own_worktree(str(clone), dest) == ""

    assert not Path(dest).exists()
    listed = _git(env, clone, "worktree", "list", "--porcelain").stdout
    assert release_worktree.WORKTREE_NAME not in listed, listed


# --------------------------------------------------------------------------
# real git: the redirect limit, stated exactly, and the code-exec neutralizers
# --------------------------------------------------------------------------
def _make_origin(tmp_path: Path, env: dict[str, str], name: str, tag: str, payload: str) -> Path:
    """A standalone local origin carrying one annotated tag. A distinct ``payload`` gives
    it a commit oid distinct from any sibling origin, so a redirect between them is
    observable in the oid the action lands on."""
    repo = tmp_path / name
    repo.mkdir()
    _git(env, repo, "init", "-q")
    (repo / "p.txt").write_text(payload, encoding="utf-8")
    _git(env, repo, "add", "p.txt")
    _git(env, repo, "commit", "-qm", "one")
    assert _git(env, repo, "tag", "-a", tag, "-m", tag).returncode == 0
    return repo


@pytest.mark.asyncio
@requires_git
async def test_real_git_an_insteadof_rewrite_redirects_resolution_documenting_the_limit(
    tmp_path, monkeypatch
):
    """DOCUMENTS THE TRUST BOUNDARY; it does not assert a defense, because there is none to
    assert. ``url.<evil>.insteadOf`` in the repo's own ``.git/config`` rewrites the upstream
    URL before ls-remote/fetch connect, so "newest stable" resolves against the remote the
    rewrite names -- the agent's, not the configured upstream's. The redirect survives the
    production network neutralizers (``_neutralized_net_env``): they pin the transports and the
    code-exec config keys this action relies on but carry no ``insteadOf`` suppression, because
    a fixed ``-c`` set cannot disable an arbitrarily-named one. The oid confirm still passes -- advertisement and
    served object are both the evil remote's and agree -- which is exactly why provenance is
    relative to the agent-writable ``.git/config``: the same trust Dev Fleet's sync, rebase
    and Make Live already place in that file."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    # Canonical upstream advertises v0.10.0; the evil remote advertises a HIGHER v0.99.0 at a
    # DIFFERENT commit. The clone's `origin` points at canonical.
    canonical = _make_origin(tmp_path, env, "canonical_repo", "v0.10.0", "canonical\n")
    evil = _make_origin(tmp_path, env, "evil_repo", "v0.99.0", "evil\n")
    # Clone via a forward-slashed path so git stores `remote.origin.url` with forward
    # slashes; the `insteadOf` value below is read back from it and must parse cleanly as
    # a git-config value (backslashes are escape-interpreted). No-op on POSIX.
    assert _git(env, tmp_path, "clone", "-q", canonical.as_posix(), "checkout").returncode == 0
    clone = tmp_path / "checkout"
    canonical_oid = _git(env, canonical, "rev-list", "-n1", "v0.10.0").stdout.strip()
    evil_oid = _git(env, evil, "rev-list", "-n1", "v0.99.0").stdout.strip()
    assert canonical_oid and evil_oid and canonical_oid != evil_oid

    monkeypatch.setattr(repository, "_repo", lambda: str(clone))

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    _direct_runners(monkeypatch, env, net_env=_neutralized_net_env(env))

    # BEFORE the rewrite: resolution follows the configured (canonical) upstream.
    before = await release_worktree._resolve_newest_stable(str(clone))
    assert before.get("ok") and before["version"] == "0.10.0" and before["oid"] == canonical_oid

    # Plant the rewrite in the repo's OWN config: the canonical origin URL -> the evil path.
    # Written as a config stanza rather than `git config url.<path>.insteadOf` so a path
    # containing dots cannot be misparsed as section.subsection.key. The evil path is
    # forward-slashed (`as_posix`) so the `[url "..."]` subsection parses on Windows too,
    # where a backslash is a config escape; no-op on POSIX.
    origin_url = _git(env, clone, "config", "--get", "remote.origin.url").stdout.strip()
    cfg = clone / ".git" / "config"
    cfg.write_text(
        cfg.read_text(encoding="utf-8")
        + f'\n[url "{evil.as_posix()}"]\n\tinsteadOf = {origin_url}\n',
        encoding="utf-8",
    )

    # AFTER: the SAME action now resolves AND checks out the EVIL remote's higher tag.
    got = await release_worktree.create_release_worktree()
    assert got["ok"] is True, got
    assert got["version"] == "0.99.0" and got["oid"] == evil_oid
    dest = Path(got["path"])
    assert dest.is_dir()
    assert _git(env, dest, "rev-parse", "HEAD").stdout.strip() == evil_oid


@pytest.mark.asyncio
@requires_git
async def test_real_git_a_planted_sshcommand_is_neutralized_to_ssh(tmp_path):
    """A hostile ``core.sshCommand`` in ``.git/config`` would otherwise run on every fetch
    over ssh. ``runtime._run_cmd`` pins it away via ``_GIT_ENV_NEUTRALIZERS`` (same precedence
    as ``git -c``, which beats a config file). Proven end to end against real git: the plant
    reads back as its hostile value under the bare fixture env, and as plain ``ssh`` under the
    neutralizers the network runner applies."""
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_env(home)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(env, repo, "init", "-q")
    evil = 'sh -c "touch owned"'
    _git(env, repo, "config", "--local", "core.sshCommand", evil)

    # Control: the plant is live under the bare env, so the vector is real.
    assert _git(env, repo, "config", "--get", "core.sshCommand").stdout.strip() == evil
    # Neutralized: the env `runtime._run_cmd` applies pins it back to plain `ssh`.
    got = _git(_neutralized_net_env(env), repo, "config", "--get", "core.sshCommand").stdout.strip()
    assert got == "ssh", got


def test_the_network_neutralizers_pin_the_keys_this_action_relies_on():
    """Pin the keys this action relies on in ``_GIT_ENV_NEUTRALIZERS`` so an edit that drops
    one fails here rather than in the field. This is defense in depth, NOT the boundary: the
    action's safety rests on the sandbox tier -- no git child runs less confined than an
    agent's own commands, within the scope ``release_worktree``'s module docstring states --
    not on this list being exhaustive of every config key git can exec.
    ``remote.<name>.url`` and ``url.*.insteadOf`` are deliberately ABSENT -- they are the
    documented limit (see the insteadOf test), not a neutralized vector."""
    n = runtime._GIT_ENV_NEUTRALIZERS
    pairs = {
        n[f"GIT_CONFIG_KEY_{i}"]: n[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(n["GIT_CONFIG_COUNT"]))
    }
    assert pairs.get("core.sshCommand") == "ssh"
    assert pairs.get("credential.helper") == ""
    assert pairs.get("core.hooksPath") == "/dev/null"
    assert pairs.get("core.fsmonitor") == "false"
    # Non-https/ssh transports are refused by git itself, which takes `core.gitProxy`,
    # `ext::` and a local-path `uploadpack` with them.
    assert n["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    # The redirect vectors are NOT pinned -- asserting their absence keeps a future edit
    # from quietly "fixing" the limit without updating the prose that states it.
    assert "remote.origin.url" not in pairs and not any(k.startswith("url.") for k in pairs)


if sys.platform == "win32":  # pragma: no cover - these real-git arms assume a POSIX shell
    # The filter and hook arms run a shell the plant supplies (`sh -c ...`, a `#!/bin/sh`
    # hook), which has no portable Windows form, so they stay skipped there. The insteadOf
    # arm is NOT skipped: its config paths are written forward-slashed (`as_posix`), which
    # git parses on both platforms, so the redirect it documents arms on Windows too.
    for _name in (
        "test_real_git_a_planted_filter_is_refused_and_never_runs",
        "test_real_git_a_post_checkout_hook_does_not_run",
    ):
        globals()[_name] = pytest.mark.skip(reason="POSIX shell assumptions")(globals()[_name])
