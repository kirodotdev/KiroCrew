"""A second hard link on a masked credential leaf, and the inode-alias walk that finds it.

A mask binds a PATH, so a second name on the inode is an unmasked way to the bytes. Under a
crew data home the walk names that second path and the spawn masks it too; a second name
outside every data home refuses the spawn in the live home. These cases pin that split, the
walk's bounds, the remedy's wording, the doctor read that reports the condition before a
spawn, a link count read through a linked component (skipped, not refused) and the plan the
launcher is handed. The scratch data home and the POSIX marker come from
``sandbox_absent_ceiling_seal_helpers.py``.
"""

from __future__ import annotations

import inspect
import logging
import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest
from sandbox_absent_ceiling_seal_helpers import (  # noqa: F401 -- fixtures are requested by name
    _POSIX_ONLY,
    _no_host_ssh_probe,
    _written_plan,
    crew_home_fixture,
    program,
)
from test_sandbox_launcher_program import launch, payload, refusal

from kiro_crew import sandbox, sandbox_aliases

#: Masked FILE leaves deliberately left on the warning side, listed rather than derived so
#: the boundary is asserted from both directions: a leaf moved into
#: ``_CREW_HARDLINK_REFUSED_LEAVES`` without a decision here fails the test below.
#:
#: Each is masked so an agent cannot WRITE it, and its reader re-validates the content:
#: the Notes vault registry and sync settings (the app's backend re-reads both), the Ops
#: Mission Control policy, the retired kiro-cli binary-trust file with no reader left, and
#: the two browser mode leaves that carry a setting rather than a secret.
_HARDLINK_TOLERATED_FILE_LEAVES: tuple[str, ...] = (
    f"workspace/{sandbox.MD_NOTEBOOK_APP_NAME}/vaults.json",
    f"workspace/{sandbox.MD_NOTEBOOK_APP_NAME}/settings.json",
    "ops_mission_control_policy.json",
    ".kiro_cli_binary_trust.json",
    "browser-mode-enabled",
    "browser-engine",
)


@_POSIX_ONLY
class TestAMaskedCredentialLeafRefusesASecondHardLink:
    """A mask binds a PATH, so a second name on the inode is an unmasked way to the bytes.

    ``lstat`` on the masked name cannot see it, and the second name is readable AND writable,
    so for a leaf whose bytes are a usable secret the second name has to be dealt with.

    WHICH answer depends on whether the name can be found. Under a data home it can, so it is
    added to the spawn's hidden set and the bytes are unreachable in every namespace -- no
    spawn is refused, because there is nothing left to refuse over. Outside every data home it
    cannot be found without walking the filesystem, and only then does the live home refuse;
    that case is pinned separately below.
    """

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_HARDLINK_REFUSED_LEAVES))
    def test_a_credential_leaf_with_a_second_name_is_MASKED(self, crew_home, leaf):
        """Found means closed: the alias joins the hidden set instead of stopping the spawn."""
        target = crew_home / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret")
        alias = crew_home / f"alias-{leaf.replace('/', '-')}"
        os.link(target, alias)
        assert target.stat().st_nlink == 2

        to_hide = sandbox._refuse_aliased_masked_leaves()

        assert str(alias) in {m.path for m in to_hide}, (
            f"the second name for {leaf} was not returned for masking, so the bytes stay "
            "readable under it inside the sandbox"
        )

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_HARDLINK_REFUSED_LEAVES))
    def test_one_link_is_accepted(self, crew_home, leaf):
        """The ordinary case must not refuse, or the rule is a blanket outage."""
        target = crew_home / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret")
        assert target.stat().st_nlink == 1

        assert sandbox._refuse_aliased_masked_leaves() == ()  # does not raise, masks nothing

    def test_a_second_name_OUTSIDE_the_data_home_also_refuses(self, crew_home, tmp_path):
        """The accepted cost, pinned so it is a decision and not a surprise.

        ``st_nlink`` reports that a second name exists, not where it is. A name under a data
        home is located by the alias walk and masked; a snapshot tool's link OUTSIDE every
        data home is not, because finding it means walking the filesystem. The live home
        refuses there rather than launch with bytes it cannot cover, and the doctor read below
        is what stops that arriving as an unexplained outage.
        """
        target = crew_home / "token_signing.key"
        target.write_bytes(b"key")
        os.link(target, tmp_path / "rsnapshot-daily-0")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._refuse_aliased_masked_leaves()
        assert "find" in str(caught.value), "the remedy must name how to locate the other name"

    def test_the_channel_dotfile_keeps_its_symlink_tolerance(self, crew_home, tmp_path, caplog):
        """``.env`` refuses a hard link and still tolerates a SYMLINK.

        The two shapes get different answers on purpose: the tolerance exists for the layout
        a dotfile manager produces, and chezmoi and stow produce a symlink or a copy, never a
        hard link.
        """
        real = tmp_path / "dotfiles-env"
        real.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        link = crew_home / ".env"
        link.symlink_to(real)

        with caplog.at_level("WARNING"):
            sandbox._refuse_aliased_masked_leaves()  # does not raise
        assert any("SYMLINK" in r.getMessage() and ".env" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("leaf", _HARDLINK_TOLERATED_FILE_LEAVES)
    def test_an_integrity_file_leaf_stays_on_the_warning_side(self, crew_home, caplog, leaf):
        """The residual, per leaf: a write alias to these is real and is not refused."""
        assert leaf in sandbox._CREW_HIDDEN_LEAVES, f"{leaf} is not masked at all"
        assert leaf not in sandbox._CREW_HARDLINK_REFUSED_LEAVES
        target = crew_home / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / f"alias-{leaf.replace('/', '-')}")

        with caplog.at_level("WARNING"):
            sandbox._refuse_aliased_masked_leaves()  # does not raise
        assert any("hardlinks" in r.getMessage() for r in caplog.records)

    def test_a_DIRECTORY_leaf_is_outside_the_decision_by_shape(self, crew_home):
        """No directory entry is needed, because ``link(2)`` refuses a directory."""
        target = crew_home / "diag"
        target.mkdir()
        with pytest.raises(OSError):
            os.link(target, crew_home / "diag-alias")

    def test_a_credential_leaf_in_the_LEGACY_home_spelling_is_reported_not_refused(
        self, crew_home, tmp_path, caplog
    ):
        """Every masked spelling is INSPECTED, and a locatable alias there is MASKED.

        ``_crew_home_entries`` expands every hidden leaf across both ``_CREW_HOME_PREFIXES``,
        so ``~/.kirocrew/.env`` holds live channel tokens whichever spelling ``config_dir()``
        resolves to, and a second hard link on it reaches those bytes under a name no mask
        covers.

        It must NOT refuse. A mask target is bound only when it exists, so a home the install
        does not use is masked by nothing while it is absent -- which means a sandboxed agent
        can create it and plant exactly this shape, and a refusal there is a control the
        governed process can trip on demand, stopping every launch on the host.

        Masking the alias is what makes that safe rather than merely tolerated: the second
        name is under a home the walk covers, so it is found and hidden, and the bytes are
        unreachable without anything being refused.
        """
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()
        target = legacy / ".env"
        target.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        alias = legacy / "env-alias"
        os.link(target, alias)
        assert target.stat().st_nlink == 2

        with caplog.at_level(logging.WARNING):
            to_hide = sandbox._refuse_aliased_masked_leaves()

        assert str(alias) in {
            m.path for m in to_hide
        }, "the alias in a non-live home was not masked, so the bytes stay readable under it"
        assert any(
            "env-alias" in r.getMessage() for r in caplog.records
        ), "the masking went unreported, so an operator cannot find the extra link to remove it"

    def test_a_credential_leaf_left_in_the_DEFAULT_home_by_a_relocation_is_reported(
        self, tmp_path, monkeypatch, caplog
    ):
        """A relocated ``KIROCREW_HOME`` needs no migration history to reach this.

        ``_relocated_crew_targets`` masks the resolved home ON TOP of the ``$HOME``-relative
        spellings, so a signing key left in the default home is masked and would otherwise
        be unread by a live-home-only check. Reported, for the same reason the legacy
        spelling is: the default home is not the live one here, so an agent could have
        created it.
        """
        relocated = tmp_path / "srv" / "crew"
        relocated.mkdir(parents=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        default = tmp_path / ".kiro" / "crew"
        default.mkdir(parents=True)
        stranded = default / "token_signing.key"
        stranded.write_bytes(b"k" * 32)
        os.link(stranded, default / "key-alias")

        with caplog.at_level(logging.WARNING):
            sandbox._refuse_aliased_masked_leaves()

        assert any("token_signing.key" in r.getMessage() for r in caplog.records)

    def test_an_agent_CREATED_home_cannot_stop_every_spawn(self, crew_home, tmp_path, caplog):
        """The denial-of-service this split exists to close.

        An absent home is masked by nothing, so anything a sandboxed process writes there is
        reachable by it. If a second name in such a home refused, one ``mkdir`` plus one
        ``ln`` would stop every agent launch on the host until an operator found a dotfile in
        a directory they had never used. The spawn must proceed.
        """
        planted = tmp_path / ".kirocrew"
        planted.mkdir()
        leaf = planted / ".env"
        leaf.write_text("SLACK_BOT_TOKEN=planted\n", encoding="utf-8")
        os.link(leaf, planted / "second-name")

        with caplog.at_level(logging.WARNING):
            sandbox._refuse_aliased_masked_leaves()
            sandbox._sweep_legacy_auth_store_temps()
            sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert leaf.exists(), "the pass deleted a file it only had grounds to report"

    def test_an_integrity_leaf_in_a_NON_LIVE_home_still_does_not_refuse(
        self, crew_home, tmp_path, caplog
    ):
        """The widening carries the credential/integrity split with it, not past it.

        Without this the cross-home pass could quietly refuse on every masked leaf in a
        second home while the live home still warns for the same shape, which is a
        different rule in each home.
        """
        leaf = _HARDLINK_TOLERATED_FILE_LEAVES[0]
        legacy = tmp_path / ".kirocrew"
        target = legacy / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, legacy / f"alias-{leaf.replace('/', '-')}")

        with caplog.at_level("WARNING"):
            sandbox._refuse_aliased_masked_leaves()  # does not raise

        assert target.read_text(encoding="utf-8") == "{}\n"
        assert target.stat().st_nlink == 2

    def test_a_symlinked_legacy_home_does_not_refuse_the_spawn(self, crew_home, tmp_path):
        """The layout a part-migrated host actually has must keep starting agents.

        Pointing the legacy home AT the live one is the ordinary migration shape. Both names
        then reach ONE inode, whose link count the live home's own pass already judges, so
        refusing here would break a working host to close nothing.
        """
        (tmp_path / ".kirocrew").symlink_to(crew_home, target_is_directory=True)
        target = crew_home / ".env"
        target.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        assert target.stat().st_nlink == 1

        sandbox._refuse_aliased_masked_leaves()  # does not raise

    def test_the_roots_cover_every_spelling_the_masks_do(self, crew_home, tmp_path):
        """The refusal's root set and the mask's prefix set must not drift apart."""
        roots = sandbox._masked_crew_home_roots()
        for prefix in sandbox._CREW_HOME_PREFIXES:
            assert str(tmp_path / Path(prefix)) in roots, f"{prefix} is masked but unchecked"
        assert len(roots) == len(set(roots)), "a root repeated means one leaf is stat-ed twice"

    def test_the_refused_set_matches_an_independently_written_list(self):
        """Parameterizing from the set under test lets a deletion remove its own case.

        Every other test in this class derives its cases from
        ``_CREW_HARDLINK_REFUSED_LEAVES``, so dropping a leaf from that set silently drops
        the test that guarded it and the suite stays green while the protection is gone.
        This list is written out by hand for exactly that reason: changing the set must also
        change this literal, which is a decision someone makes rather than a side effect.
        """
        from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES

        expected = {
            # signs dashboard access and refresh tokens
            "token_signing.key",
            # refresh-token chain state; a read continues a session
            "refresh_chains.json",
            # live GitHub PAT, inside the masked state directory, and its old bare name
            "md-notebook-staging/pat",
            "workspace/md-notebook/pat",
            # named secrets store
            "ops_mission_control_secrets.json",
            # channel tokens
            ".env",
            # browser session material; a restored copy still carries live cookies
            "browser-cookies.txt",
            "playwright-storage-state.json",
            "playwright-extension-token",
            # the auth store: the module states the bytes are a live bearer token
            AUTH_SQLITE_DB,
            *(f"{AUTH_SQLITE_DB}{suffix}" for suffix in AUTH_SQLITE_SIDECAR_SUFFIXES),
        }
        assert set(sandbox._CREW_HARDLINK_REFUSED_LEAVES) == expected, (
            "the refused set changed; update this literal deliberately, and check the "
            "leaf's reason block and the security spec with it"
        )

    def test_the_refused_set_names_only_masked_leaves(self):
        """An entry outside ``_CREW_HIDDEN_LEAVES`` would refuse over a path nothing masks.

        A test rather than a module-level ``assert``, for the reason the tolerated set's own
        invariant gives: ``python -O`` strips an assert.
        """
        hidden = set(sandbox._CREW_HIDDEN_LEAVES)

        def masked(leaf: str) -> bool:
            # A leaf or any directory above it: a mask on a directory hides every name in it.
            parts = leaf.split("/")
            return any("/".join(parts[:n]) in hidden for n in range(1, len(parts) + 1))

        stray = sorted(leaf for leaf in sandbox._CREW_HARDLINK_REFUSED_LEAVES if not masked(leaf))
        assert not stray, f"refused on a hard link but not masked: {stray}"

    def test_the_sqlite_sidecars_come_from_the_shared_suffix_constant(self):
        """Re-listing the suffixes is how one gets forgotten when a fourth is added."""
        from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES

        for suffix in AUTH_SQLITE_SIDECAR_SUFFIXES:
            assert f"{AUTH_SQLITE_DB}{suffix}" in sandbox._CREW_HARDLINK_REFUSED_LEAVES
        assert AUTH_SQLITE_DB in sandbox._CREW_HARDLINK_REFUSED_LEAVES

    def test_every_refused_leaf_states_its_reason(self):
        """A bare entry is how an unexplained subset rots; each one is argued in the source.

        Scoped to the contiguous ``#:`` block directly above the set, not the module: a
        whole-module search passes for any leaf named anywhere earlier, which is every masked
        leaf, so it would assert nothing.
        """
        source = inspect.getsource(sandbox_aliases)
        before = source.split("_CREW_HARDLINK_REFUSED_LEAVES: frozenset")[0]
        block = []
        for line in reversed(before.splitlines()):
            if line.startswith("#:") or line == "#:":
                block.append(line)
            elif block:
                break
        doc = "\n".join(block)
        assert doc, "the refused set has no doc-comment block above it"
        for leaf in sandbox._CREW_HARDLINK_REFUSED_LEAVES:
            base = os.path.basename(leaf)
            named = base in doc or leaf in doc
            # The sqlite sidecars are argued as a group by their own entry, which names the
            # store rather than each suffix -- a per-suffix sentence would restate the
            # constant they are derived from.
            grouped = base.startswith(sandbox.AUTH_SQLITE_DB) or base == "pat"
            assert named or grouped, f"{leaf} refuses with no reason recorded beside it"


@_POSIX_ONLY
class TestTheInodeAliasWalk:
    """Naming the OTHER path to a credential's bytes is what avoids the false choice.

    Refusing every spawn on a second hard link is reachable from inside a sandbox; leaving the
    bytes readable under an unmasked name is the hole. Knowing WHICH path is the second name
    turns both into one action: mask that path too.
    """

    def _leaf(self, root: Path) -> tuple[Path, os.stat_result]:
        target = root / ".env"
        target.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        return target, os.lstat(target)

    def test_it_names_an_unmasked_second_name(self, tmp_path):
        target, info = self._leaf(tmp_path)
        alias = tmp_path / "copy-of-env"
        os.link(target, alias)

        found = sandbox._inode_aliases_under(str(tmp_path), str(target), os.lstat(target))

        assert found.unmasked == [str(alias)], (
            "the walk did not name the second path to the credential's bytes, so a caller "
            "cannot close the hole without refusing the spawn"
        )
        assert found.masked == []
        assert info.st_ino == os.lstat(alias).st_ino

    def test_it_reports_an_ALREADY_masked_sibling_separately(self, tmp_path):
        """A masked second name is not a hole to close, but it IS a name that was located.

        Both halves matter and they pull in opposite directions. Masking it again buys
        nothing, so it must stay out of ``unmasked``. But the caller counts located names
        against ``st_nlink`` to decide whether every link is accounted for, so dropping it
        entirely leaves the count short and refuses a spawn whose every name is already
        unreachable -- which is what this module's own durability fallback produces, since it
        keeps the staging hard link inside a masked directory on purpose.
        """
        target, _ = self._leaf(tmp_path)
        masked_sibling = tmp_path / "token_signing.key"
        os.link(target, masked_sibling)

        found = sandbox._inode_aliases_under(str(tmp_path), str(target), os.lstat(target))

        assert found.unmasked == [], (
            "the walk offered a path the mask already covers as one to hide; acting on it "
            "would report a hole that does not exist"
        )
        assert found.masked == [str(masked_sibling)], (
            "the walk dropped an already-masked second name entirely, so the caller's "
            "accounting comes up short and refuses a spawn that is already safe"
        )

    def test_it_does_not_follow_a_directory_symlink_out_of_the_home(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        target, _ = self._leaf(home)
        escaped = outside / "escaped"
        os.link(target, escaped)
        (home / "door").symlink_to(outside, target_is_directory=True)

        found = sandbox._inode_aliases_under(str(home), str(target), os.lstat(target))

        assert found.unmasked == [] and found.masked == [], (
            "the walk followed a planted directory link and reported a path outside the home, "
            "which a caller would then try to mask"
        )

    def test_it_never_returns_the_leaf_itself(self, tmp_path):
        target, _ = self._leaf(tmp_path)
        os.link(target, tmp_path / "second")

        found = sandbox._inode_aliases_under(str(tmp_path), str(target), os.lstat(target))

        assert str(target) not in found.unmasked
        assert str(target) not in found.masked

    def test_it_stops_at_the_entry_cap(self, tmp_path, monkeypatch, caplog):
        """The home holds agent-writable subtrees, so the walk must not be unbounded."""
        monkeypatch.setattr(sandbox, "_ALIAS_WALK_MAX_ENTRIES", 3)
        target, _ = self._leaf(tmp_path)
        for i in range(12):
            (tmp_path / f"filler{i}").write_text("x", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            sandbox._inode_aliases_under(str(tmp_path), str(target), os.lstat(target))

        assert any("stopped the credential alias walk" in r.getMessage() for r in caplog.records), (
            "the cap was reached without saying so, so a partial answer would read as a "
            "complete one"
        )

    def test_it_stops_at_the_entry_cap_on_a_DIRECTORY_heavy_tree(
        self, tmp_path, monkeypatch, caplog
    ):
        """The cap must count directories too, or the tree that needs it is the one that escapes.

        A crew home's agent-writable subtrees are mostly directories. Counting only files let
        such a tree pass the cap untouched and enumerate in FULL -- once per multilinked
        credential leaf per home, on the spawn path -- which is exactly the unbounded walk the
        cap exists to prevent. The sibling test above cannot see this: its filler is files.
        """
        monkeypatch.setattr(sandbox, "_ALIAS_WALK_MAX_ENTRIES", 3)
        target, _ = self._leaf(tmp_path)
        # Many directories, no filler files at all.
        deep = tmp_path
        for i in range(12):
            deep = deep / f"sub{i}"
            deep.mkdir()
            for j in range(3):
                (deep / f"branch{j}").mkdir()

        real_walk = os.walk
        yielded: list[str] = []

        def counting_walk(*a, **kw):
            for entry in real_walk(*a, **kw):
                yielded.append(entry[0])
                yield entry

        monkeypatch.setattr(os, "walk", counting_walk)

        with caplog.at_level(logging.WARNING):
            sandbox._inode_aliases_under(str(tmp_path), str(target), os.lstat(target))

        assert any("stopped the credential alias walk" in r.getMessage() for r in caplog.records), (
            "a tree of directories never reached the cap, so the walk ran to completion on "
            f"the spawn path; it visited {len(yielded)} directories with the cap at 3"
        )
        assert len(yielded) <= 3, (
            "the walk kept descending after the cap was reached, so the bound is only on "
            f"what is REPORTED, not on the work done; it visited {len(yielded)} directories"
        )


@_POSIX_ONLY
class TestTheMultilinkRemedyNamesTheDataHome:
    """The remedy promises a data-home search, so it must name the data home.

    Every refused leaf but one is a root-level name, for which the leaf's parent and the home
    coincide; the md-notebook token is nested. Using the parent there searches one app's state
    directory while the sentence claims the home, so a second name anywhere else under the
    home reports as absent -- the operator reads "just this leaf" and stops.
    """

    def test_a_nested_leaf_still_searches_the_home(self):
        home = os.path.join(os.sep, "srv", "crew")
        nested = [leaf for leaf in sandbox._CREW_HARDLINK_REFUSED_LEAVES if "/" in leaf]
        assert nested, "no multi-component leaf left to test, so this guard is unreachable"
        for leaf in nested:
            target = os.path.join(home, leaf.replace("/", os.sep))
            detail = sandbox._masked_leaf_multilink_detail(target, 2)
            assert f"find {home} " in detail or f"find '{home}' " in detail, (
                f"the remedy for {leaf} searches something other than the data home, while "
                "its own sentence says it searches the home only"
            )

    def test_a_root_level_leaf_is_unchanged(self):
        home = os.path.join(os.sep, "srv", "crew")
        target = os.path.join(home, "token_signing.key")
        detail = sandbox._masked_leaf_multilink_detail(target, 2)
        assert f"find {home} " in detail or f"find '{home}' " in detail


@_POSIX_ONLY
class TestTheDoctorReadOfMaskedCredentialAliases:
    """The refusal must not be an operator's first notice, so doctor reads it pre-spawn.

    Same answer the live-target pointer already gives for the same shape: a hard link on a
    file in the home is ordinary snapshot-tool operation, so the condition appears without
    anybody doing anything wrong and the first symptom is that agents stop starting.
    """

    def test_it_reports_the_aliased_leaf_and_its_link_count(self, crew_home):
        target = crew_home / "token_signing.key"
        target.write_bytes(b"key")
        os.link(target, crew_home / "alias")

        found = sandbox.masked_credential_leaf_aliases()

        assert [(p, n, r, a) for p, n, r, a in found if p.endswith("token_signing.key")] == [
            (str(target), 2, str(crew_home), True)
        ], (
            "the probe must name the home it found the leaf under AND whether every other "
            "name was located, because only the live home refuses and only on an unlocated "
            "name -- a reader told otherwise chases a failure that is not coming"
        )

    def test_it_is_quiet_on_a_healthy_home(self, crew_home):
        (crew_home / "token_signing.key").write_bytes(b"key")

        assert sandbox.masked_credential_leaf_aliases() == []

    def test_it_reports_an_aliased_leaf_in_a_NON_LIVE_masked_home(self, crew_home, tmp_path):
        """A probe narrower than the refusal is worse than none at all.

        It would report a clean host and the next spawn would refuse anyway, which is the
        outage this read exists to pre-empt. So the probe walks the same homes the refusal
        does.
        """
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()
        stranded = legacy / "token_signing.key"
        stranded.write_bytes(b"k" * 32)
        os.link(stranded, legacy / "alias")

        found = sandbox.masked_credential_leaf_aliases()

        assert (str(stranded), 2, str(legacy), True) in found, (
            "the probe must report a non-live home's aliased leaf AND name that home, so "
            "the caller can say the spawn proceeds rather than claiming a refusal"
        )
        assert str(legacy) != str(sandbox.config_dir())

    def test_the_probe_and_the_refusal_read_the_same_homes(self):
        """Pinned as one source, because two lists that must agree are a list that drifts.

        Judged on the executable body with the docstring dropped: both functions DISCUSS
        ``config_dir()`` in prose, which is exactly the gap they explain, so scanning raw
        source would fail on the explanation instead of on the code.
        """
        import ast

        def code_only(func) -> str:
            tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
            body = tree.body[0].body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.unparse(node) for node in body)

        for func in (
            sandbox.masked_credential_leaf_aliases,
            sandbox._refuse_multilinked_credential_leaves,
        ):
            body = code_only(func)
            assert "_masked_crew_home_roots()" in body, f"{func.__name__} does not read every home"
        # The refusal resolves config_dir() on purpose, to decide refuse-versus-report. The
        # probe must not: it reports every home, so narrowing it would hide the very
        # condition the report exists to surface before a spawn meets it.
        assert "config_dir()" not in code_only(sandbox.masked_credential_leaf_aliases), (
            "the doctor probe resolves the live home alone, so it reports less than the "
            "spawn pass inspects"
        )
        assert "config_dir()" in code_only(sandbox._refuse_multilinked_credential_leaves), (
            "the refusal no longer identifies the live home, so it either refuses in a home "
            "an agent can create or refuses nowhere"
        )

    def test_it_creates_nothing(self, crew_home):
        before = sorted(p.name for p in crew_home.iterdir())

        assert sandbox.masked_credential_leaf_aliases() == []
        assert sorted(p.name for p in crew_home.iterdir()) == before

    def test_it_reports_the_same_sentence_the_spawn_refuses_with(self, crew_home, tmp_path):
        """One diagnosis, not two: the operator reads the launcher's own words.

        The alias goes OUTSIDE every data home deliberately. That is the shape the launcher
        still refuses on -- a name under a home is located and masked instead, and there is
        then no refusal sentence to agree with.
        """
        target = crew_home / "token_signing.key"
        target.write_bytes(b"key")
        os.link(target, tmp_path / "rsnapshot-alias")

        [(path, links, _root, accounted)] = sandbox.masked_credential_leaf_aliases()
        assert accounted is False, (
            "this alias is outside every home on purpose, so the probe must report it as "
            "unlocated -- that is the only shape that still refuses, and without it there "
            "is no refusal sentence to agree with"
        )
        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._refuse_aliased_masked_leaves()

        assert sandbox._masked_leaf_multilink_detail(path, links) == str(caught.value)

    def test_it_does_not_print_the_refusal_sentence_for_a_leaf_it_says_is_MASKED(
        self, crew_home, capsys, monkeypatch
    ):
        """Two sentences one line apart cannot both be true, and the operator cannot choose.

        A leaf whose every other name was located is masked for each spawn. Printing the
        launcher's refusal sentence under it says "cannot mask" about the same leaf, and the
        trailer then tells the operator these leaves sit outside the live data home -- for a
        leaf whose root IS the live home. The remedy command must survive, because finding the
        extra link is still the action.
        """
        from kiro_crew import cli_doctor

        target = crew_home / "token_signing.key"
        target.write_bytes(b"key")
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(exist_ok=True)
        os.link(target, staging / ".token_signing.key.7.f.tmp")
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: True)

        [(_p, _l, _r, accounted)] = sandbox.masked_credential_leaf_aliases()
        assert accounted is True, "the fixture did not build the all-names-located shape"

        issues: list[str] = []
        cli_doctor._doctor_masked_credential_aliases(issues)
        out = capsys.readouterr().out

        assert "masked for each spawn" in out, "the located-and-masked verdict was not printed"
        assert "cannot mask" not in out, (
            "doctor printed the launcher's refusal sentence for a leaf it had just called "
            "masked, so the operator reads two contradictory diagnoses of one leaf"
        )
        assert "-samefile" in out, (
            "the remedy that finds the extra link was dropped along with the refusal "
            "sentence, leaving the operator nothing to act on"
        )
        assert (
            "outside the live data home" not in out
        ), "the trailer claimed this leaf is outside the live data home while it sits in it"
        assert issues == [], "a leaf the launcher masks must not change doctor's exit code"

    def test_the_doctor_runs_on_every_platform_whose_launcher_refuses(self, monkeypatch):
        """The report and the refusal must cover the same platforms.

        Both confined launch paths issue this refusal: ``namespace_argv`` and
        ``sandbox_exec_argv``. A doctor that inspects fewer platforms than the launcher
        refuses on leaves an operator meeting a spawn outage their own report called clean.
        A platform with no confined launch path is skipped, so the section is silent rather
        than naming an outage that cannot arrive.
        """
        from kiro_crew import cli_doctor

        for platform, expected in (("linux", True), ("darwin", True), ("win32", False)):
            asked: list[bool] = []
            monkeypatch.setattr(sys, "platform", platform)
            monkeypatch.setattr(
                sandbox, "masked_credential_leaf_aliases", lambda: asked.append(True) or []
            )
            cli_doctor._doctor_masked_credential_aliases([])
            assert (
                bool(asked) is expected
            ), f"doctor on {platform} asked={bool(asked)}, launcher-refuses={expected}"

    def test_an_unresolvable_data_home_reports_nothing_rather_than_a_fault(self, monkeypatch):
        """Doctor must not turn its own probe failure into a verdict about the host."""
        monkeypatch.setattr(
            sandbox, "config_dir", lambda: (_ for _ in ()).throw(RuntimeError("no home"))
        )

        assert sandbox.masked_credential_leaf_aliases() == []

    def test_an_unresolvable_data_home_refuses(self, monkeypatch):
        """Fail CLOSED, like every other reason on this path."""

        def boom():
            raise OSError("data home unavailable")

        monkeypatch.setattr(sandbox, "config_dir", boom)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._refuse_aliased_masked_leaves()

    def test_an_unreadable_leaf_refuses_rather_than_reading_as_absent(self, crew_home, monkeypatch):
        """An errno other than ENOENT means "could not judge", not "safe".

        Treating them alike failed OPEN: the data home is agent-writable, so stripping
        search permission from an owned directory turned the check into a silent skip while
        the launcher's own ``isdir`` guard skipped the mask for the same reason.
        """
        real_lstat = os.lstat
        target = str(crew_home / "ledger")

        def fake_lstat(path, *a, **kw):
            if str(path) == target:
                raise PermissionError(13, "Permission denied")
            return real_lstat(path, *a, **kw)

        monkeypatch.setattr(sandbox.os, "lstat", fake_lstat)
        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._refuse_aliased_masked_leaves()
        assert "cannot stat" in str(caught.value)
        assert "ledger" in str(caught.value)

    def test_an_absent_leaf_still_reads_as_absent(self, crew_home, monkeypatch):
        """The fail-closed split must not turn the ordinary case into a refusal.

        Nearly every leaf is absent on an ordinary host, so ENOENT staying benign is what
        keeps the pass from being a blanket outage.
        """
        real_lstat = os.lstat
        seen = []

        def counting_lstat(path, *a, **kw):
            seen.append(str(path))
            return real_lstat(path, *a, **kw)

        monkeypatch.setattr(sandbox.os, "lstat", counting_lstat)
        sandbox._refuse_aliased_masked_leaves()  # does not raise
        assert seen, "the pass must actually have stat'd something"

    def test_an_unreadable_component_refuses(self, crew_home, monkeypatch):
        """The same split one level up: the ancestor walk must not answer "not a link".

        ``os.path.islink`` answers False when the stat fails, so an unreadable component
        would read as a real directory. Both the leaf and the component carry the identical
        decision, so both are pinned.
        """
        real_lstat = os.lstat
        blocked = str(crew_home / "apps")

        def fake_lstat(path, *a, **kw):
            if str(path) == blocked:
                raise PermissionError(13, "Permission denied")
            return real_lstat(path, *a, **kw)

        monkeypatch.setattr(sandbox.os, "lstat", fake_lstat)
        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._refuse_aliased_masked_leaves()
        assert "passes through a link" in str(caught.value)
        assert "apps" in str(caught.value)

    def test_the_pass_runs_after_every_materialiser(self):
        """Order is load-bearing: a leaf with its own sentence must answer first.

        ``live_target.json`` shares its wording with ``kirocrew doctor`` and the
        md-notebook leaves name their own documents, so a generic message arriving first
        would replace a sentence another surface is pinned to.
        """
        source = inspect.getsource(sandbox.namespace_argv)
        order = [
            # Anchored on the call NAME: each of these takes the established-target
            # collector, so pinning an empty argument list would read an argument change
            # as an ordering break.
            source.index("_materialize_sealable_ceilings("),
            source.index("_materialize_maskable_dirs("),
            source.index("_materialize_md_notebook_mask_targets("),
            source.index("_materialize_live_target_mask_target("),
            source.index("_refuse_aliased_masked_leaves("),
        ]
        assert order == sorted(order), "the alias pass must run last"


@_POSIX_ONLY
class TestACredentialLeafBehindALinkedComponentIsNotRefused:
    """A link count read THROUGH a link belongs to a file somewhere else entirely.

    ``lstat`` leaves only the final component un-followed, so for a multi-component
    credential leaf a link planted above it makes this read report the link count of a file
    outside the data home. Refusing on that number would fail every spawn over a file the
    sandbox does not protect, and it would invert the decision
    ``_CREW_ALIAS_CHAIN_DEGRADE_LEAVES`` records for exactly that chain: degrade, do not
    refuse. The chain hazard has its own control, and this pass is not it.
    """

    @staticmethod
    def _multi_component_leaves() -> list[str]:
        return sorted(
            leaf for leaf in sandbox._CREW_HARDLINK_REFUSED_LEAVES if "/" in leaf or os.sep in leaf
        )

    def test_such_a_leaf_exists_to_test(self):
        """Guard against a vacuous pass if the refused set ever loses its nested leaf."""
        assert self._multi_component_leaves(), (
            "no multi-component leaf in _CREW_HARDLINK_REFUSED_LEAVES, so the cases below "
            "prove nothing"
        )

    def _plant(self, crew_home, tmp_path, leaf):
        """Put an aliased leaf behind a symlinked FIRST component, outside the data home."""
        parts = leaf.replace(os.sep, "/").split("/")
        victim = tmp_path / f"agent-owned-{parts[0]}"
        (victim / "/".join(parts[1:-1])).mkdir(parents=True, exist_ok=True)
        target = victim / "/".join(parts[1:])
        target.write_bytes(b"secret")
        os.link(target, tmp_path / f"second-name-{parts[0]}")
        assert target.stat().st_nlink == 2
        planted = crew_home / parts[0]
        if planted.is_symlink():
            planted.unlink()
        elif planted.is_dir():
            shutil.rmtree(planted)
        planted.symlink_to(victim, target_is_directory=True)
        return target

    def test_the_refusal_skips_it(self, crew_home, tmp_path):
        targets = [
            self._plant(crew_home, tmp_path, leaf) for leaf in self._multi_component_leaves()
        ]

        sandbox._refuse_multilinked_credential_leaves()  # does not raise

        for target in targets:
            assert target.read_bytes() == b"secret"
            assert target.stat().st_nlink == 2

    def test_the_doctor_read_skips_it_too(self, crew_home, tmp_path):
        """A probe wider than the refusal fails doctor's exit code over a foreign path."""
        planted = [
            self._plant(crew_home, tmp_path, leaf) for leaf in self._multi_component_leaves()
        ]

        found = sandbox.masked_credential_leaf_aliases()

        assert found == [], f"doctor reported an alias it cannot refuse on: {found}"
        for target in planted:
            assert target.stat().st_nlink == 2, "the probe must not change anything"

    def test_a_REAL_chain_with_an_aliased_leaf_is_still_acted_on(self, crew_home):
        """The skip is about the linked component, not about the nested leaf.

        With every component a real directory, the leaf is inside the data home and its second
        name is exactly what this pass exists for. Acted on now means MASKED: the alias is
        under a home the walk covers, so the bytes are closed without refusing a spawn. The
        discriminating part is that a nested leaf is not skipped merely for being nested.
        """
        leaf = self._multi_component_leaves()[0]
        target = crew_home / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret")
        alias = crew_home / "alias"
        os.link(target, alias)

        to_hide = sandbox._refuse_multilinked_credential_leaves()

        assert str(alias) in {m.path for m in to_hide}, (
            f"the alias on nested leaf {leaf} was neither masked nor refused, so a "
            "multi-component leaf is being skipped just for being nested"
        )

    def test_a_located_alias_REFUSES_when_the_platform_can_only_hide_it_by_path(self, crew_home):
        """Masking closes the hole only where the mask is bound to the inode.

        On Seatbelt the mask is a path rule, and nothing denies a write to the alias's parent
        or its ancestors while the data home root is writable in-sandbox -- so the governed
        process renames a parent and reads the bytes under a name no rule covers, with no race
        to win. This module already carries the lesson in the voice-runtime seal, which exists
        because a same-UID agent can rename a parent around a path-based subtree deny.

        So the same input that is MASKED for an inode-bound caller must be REFUSED for a
        path-only one. Both halves are asserted here, because the refusal alone would also
        pass if the pass simply refused everything.
        """
        leaf = self._multi_component_leaves()[0]
        target = crew_home / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret")
        alias = crew_home / "alias"
        os.link(target, alias)

        # The inode-bound caller masks, as the sibling test above establishes.
        to_hide = sandbox._refuse_multilinked_credential_leaves()
        assert str(alias) in {m.path for m in to_hide}

        # The path-only caller must refuse the same layout instead of trusting the rule.
        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._refuse_multilinked_credential_leaves(masks_are_path_only=True)

        message = str(caught.value)
        assert "alias" in message, (
            "the refusal does not name the located alias, so the operator is told a leaf "
            f"cannot be masked without being told which other name to remove: {message}"
        )
        assert "renam" in message, (
            "the refusal does not say WHY a path rule is not enough here, which is the one "
            f"thing that distinguishes it from the unlocatable-name refusal: {message}"
        )


@_POSIX_ONLY
class TestTheAliasReachesTheSpawnsHiddenSet:
    """The walk is only worth anything if what it finds actually reaches the launcher.

    Returning a path the launcher never receives would close nothing while reading, in
    every unit test of the pass itself, exactly like a fix. So these drive the real launch
    path and read the plan the launcher it wrote carries.
    """

    def test_an_agent_created_home_does_not_refuse_AND_masks_the_sibling(
        self, crew_home, tmp_path, caplog
    ):
        """The ruling's first direction, and the DoS that made the refusal unusable.

        An unused home is masked by nothing while it is absent, so a sandboxed process can
        create it and plant both names. Refusing there hands it a way to stop every launch on
        the host; masking the sibling closes the bytes and costs no spawn.
        """
        planted = tmp_path / ".kirocrew"
        planted.mkdir()
        leaf = planted / ".env"
        leaf.write_text("SLACK_BOT_TOKEN=planted\n", encoding="utf-8")
        sibling = planted / "second-name"
        os.link(leaf, sibling)

        with caplog.at_level(logging.WARNING):
            argv = sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert str(sibling) in _written_plan(argv)["sensitive_dirs"], (
            "the planted sibling never reached the spawn's hidden set, so the credential "
            "bytes stay readable under it"
        )
        assert leaf.exists(), "the pass deleted a file it only had grounds to mask"

    def test_a_second_name_in_ANOTHER_crew_home_counts_as_located(
        self, crew_home, tmp_path, monkeypatch, caplog
    ):
        """The count is over every masked home, not the one holding the leaf.

        A search scoped to the leaf's own root would miss a name under a sibling home, come
        up short against ``st_nlink`` and refuse -- a spawn-wide outage over a name it could
        have masked, which is the failure mode this whole split exists to avoid.
        """
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()
        monkeypatch.setattr(
            sandbox, "_masked_crew_home_roots", lambda: [str(crew_home), str(legacy)]
        )
        target = crew_home / "token_signing.key"
        target.write_bytes(b"k" * 32)
        elsewhere = legacy / "second-name"
        os.link(target, elsewhere)

        with caplog.at_level(logging.WARNING):
            argv = sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert str(elsewhere) in _written_plan(argv)["sensitive_dirs"], (
            "the sibling home's name never reached the hidden set, so either it was missed "
            "and the spawn should have refused, or it was found and not masked"
        )

    def test_a_second_name_a_mask_ALREADY_covers_does_not_refuse(self, crew_home, monkeypatch):
        """An already-masked second name is LOCATED, so the live home must not refuse on it.

        Two masked credential names for one inode: every path to the bytes is behind a mask,
        so there is nothing to hide and nothing to refuse. Counting only the names this pass
        would NEWLY hide leaves the count short of ``st_nlink`` and refuses every spawn over
        bytes already unreachable from inside one -- the same false-refusal class as a name in
        a sibling home, from a third direction.

        The second half is a control, and it is what makes the first half mean anything: add a
        name NO mask covers and the refusal returns. Without it the quiet outcome could come
        from the pass never having examined this leaf at all.
        """
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: True)
        target = crew_home / ".env"
        target.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        masked_second = crew_home / "browser-cookies.txt"
        os.link(target, masked_second)
        assert os.lstat(target).st_nlink == 2, "the fixture did not build the two-link shape"

        assert sandbox._refuse_multilinked_credential_leaves() == (), (
            "a leaf whose only other name is itself a masked leaf was either refused or "
            "offered for masking again; both paths to the bytes are already unreachable"
        )

        unmasked_third = crew_home / "an-agent-chosen-name"
        os.link(target, unmasked_third)

        hide = sandbox._refuse_multilinked_credential_leaves()

        assert str(unmasked_third) in {m.path for m in hide}, (
            "the control did not reach the pass, so the quiet outcome above proves nothing "
            "about the accounting"
        )

    def test_a_SYMLINKED_home_spelling_does_not_double_count_one_link(
        self, crew_home, tmp_path, monkeypatch
    ):
        """One physical link under two names for one directory must count once.

        A host part-way through a migration points the legacy spelling AT the live home, and
        this module's own docstring names that layout as one whose spawns must keep working.
        De-duplicating the search roots by STRING leaves both spellings in, ``os.walk``
        descends each, and the same directory entry arrives twice -- so the located count
        overshoots ``st_nlink``, the equality test fails, and the live home refuses a spawn
        over a single link.
        """
        legacy = tmp_path / ".kirocrew"
        legacy.symlink_to(crew_home, target_is_directory=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: True)
        roots = sandbox._masked_crew_home_roots()
        assert str(legacy) not in roots, (
            "the symlinked spelling survived root de-duplication, so every walk over the roots "
            f"reports each entry twice (roots={roots})"
        )
        target = crew_home / ".env"
        target.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        alias = crew_home / "one-extra-name"
        os.link(target, alias)
        assert os.lstat(target).st_nlink == 2, "the fixture did not build the single-link shape"

        hide = sandbox._refuse_multilinked_credential_leaves()

        assert str(alias) in {
            m.path for m in hide
        }, "the alias was not offered for masking, so this test is not reaching the pass"

    def test_the_search_roots_are_deduplicated_by_directory_inode(self, tmp_path, monkeypatch):
        """The de-duplication itself, so the fix cannot be undone by a caller that bypasses it.

        Two names for one directory collapse to one root; a name that cannot be stat'ed is
        KEPT, because absence is a state every caller already handles and dropping it would
        silently narrow the set a refusal is measured against.
        """
        live = tmp_path / "live"
        live.mkdir()
        twin = tmp_path / "twin"
        twin.symlink_to(live, target_is_directory=True)
        absent = tmp_path / "never-created"
        monkeypatch.setattr(sandbox, "config_dir", lambda: live)
        monkeypatch.setattr(sandbox, "_CREW_HOME_PREFIXES", ("twin", "never-created"))
        monkeypatch.setattr(sandbox.Path, "home", classmethod(lambda _cls: tmp_path))

        roots = sandbox._masked_crew_home_roots()

        assert str(live) in roots
        assert str(twin) not in roots, (
            "two names for one directory both survived, so every walk over them reports each "
            "entry twice and the alias count overshoots the link count"
        )
        assert (
            str(absent) in roots
        ), "an absent home was dropped, which narrows the set a refusal is measured against"

    def test_the_alias_is_handed_over_as_a_FAIL_CLOSED_mask(self, crew_home, tmp_path):
        """Discovery and the mask bind are two acts, so the bind must not skip a vanished alias.

        The launcher's ordinary policy for a sensitive file that is not there is to skip it --
        an unused store is left absent rather than scaffolded. For a path the walk DISCOVERED
        moments ago that policy is wrong: absence means the name moved between the two acts and
        the credential answers to whatever it is called now, with no warning anywhere. So the
        alias has to arrive as a path whose absence is a fault, and the emitted launcher has to
        refuse on it rather than skip.
        """
        target = crew_home / ".env"
        target.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        alias = crew_home / "second-name"
        os.link(target, alias)

        argv = sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        handed = _written_plan(argv)["fail_closed_file_masks"]
        assert str(alias) in {entry[0] for entry in handed}, (
            "the discovered alias was passed as an ordinary hidden path, so the launcher would "
            "SKIP it if it had been renamed since discovery and the bytes stay readable"
        )
        info = os.lstat(alias)
        carried = {entry[0]: tuple(entry[1:]) for entry in handed}
        assert carried[str(alias)] == (info.st_dev, info.st_ino), (
            "the alias arrived without the inode it was discovered as, so the launcher can only "
            "judge the path's shape -- and a decoy left at that name is one link away from "
            "passing a mode and link-count test"
        )
        # What the launcher does with what it was handed: the discovered file passes, a
        # file substituted at the name refuses, and a vanished alias refuses.
        run = launch(tmp_path, payload(fail_closed_file_masks=handed))
        assert refusal(program.verify_fail_closed_aliases, run) is None
        alias.unlink()
        alias.write_text("SLACK_BOT_TOKEN=decoy\n", encoding="utf-8")
        substituted = refusal(program.verify_fail_closed_aliases, run)
        assert substituted is not None and "names a different inode" in substituted, (
            "the launcher compares no inode, so a file substituted at the same path is masked "
            "while the credential stays reachable under its new name"
        )
        alias.unlink()
        vanished = refusal(program.verify_fail_closed_aliases, run)
        assert (
            vanished is not None and "credential alias" in vanished
        ), "the launcher carries no refusal for a vanished alias, so absence is still a skip"

    def test_one_located_name_does_NOT_buy_a_pass_while_another_is_unlocated(
        self, crew_home, tmp_path, caplog
    ):
        """Finding ONE alias must not suppress the refusal for the ones still unaccounted.

        Three links: the leaf, a sibling inside the live home, and a backup link outside every
        home. Masking the sibling and proceeding would report the hole closed while the
        external name still reads the bytes, and the only warning would name what WAS masked
        -- so the residual would be silent. The count is what makes it loud: the leaf's own
        name plus the located ones must equal ``st_nlink``.
        """
        target = crew_home / "token_signing.key"
        target.write_bytes(b"k" * 32)
        sibling = crew_home / "second-name"
        os.link(target, sibling)
        external = tmp_path / "cp-al-backup"
        os.link(target, external)
        assert os.lstat(target).st_nlink == 3, "the fixture did not build the three-link shape"

        with caplog.at_level(logging.WARNING):
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert any("could be located" in r.getMessage() for r in caplog.records), (
            "the operator was not told that a name remains unlocated, so masking the sibling "
            "would have looked like a closed hole"
        )

    def test_a_leaf_whose_second_name_is_OUTSIDE_every_home_refuses_in_the_live_home(
        self, crew_home, tmp_path
    ):
        """The ruling's second direction: closed where it can be, refused where it cannot.

        A backup tool's link outside every data home cannot be masked without walking the
        filesystem, so the live home refuses rather than launch with bytes it cannot cover.
        """
        target = crew_home / "token_signing.key"
        target.write_bytes(b"key")
        os.link(target, tmp_path / "cp-al-backup")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert "hard links" in str(caught.value)

    def test_the_same_leaf_does_NOT_refuse_when_the_second_name_is_reachable(self, crew_home):
        """Same leaf, same link count -- only the alias's LOCATION differs.

        This is what separates the two directions above. Without it, the refusal test could
        pass because the leaf refuses unconditionally.
        """
        target = crew_home / "token_signing.key"
        target.write_bytes(b"key")
        alias = crew_home / "reachable-alias"
        os.link(target, alias)

        argv = sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert str(alias) in _written_plan(argv)["sensitive_dirs"]
