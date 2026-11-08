"""A protected or masked crew-home leaf reached through a symlink refuses the spawn.

A disposition attaches to a NAME, so a symlinked leaf the seal protects, a symlinked masked
leaf and a link planted at an intermediate component of a multi-component masked leaf
refuse instead of warning (the md-notebook leaves degrade). Every masked leaf falls in one
bucket of the alias decision, and each shape the passes deliberately tolerate has a case
asserting it is not refused. The scratch data home and the POSIX marker come from
``sandbox_absent_ceiling_seal_helpers.py``.
"""

from __future__ import annotations

import inspect
import os
import shutil

import pytest
from sandbox_absent_ceiling_seal_helpers import (  # noqa: F401 -- fixtures are requested by name
    _POSIX_ONLY,
    _no_host_ssh_probe,
    crew_home_fixture,
)

from kiro_crew import sandbox, sandbox_aliases

#: The masked leaves whose NAME must be the masked name: every hidden leaf that is not a
#: declared alias exception. DERIVED here rather than read from a production constant, so a
#: leaf added to ``_CREW_HIDDEN_LEAVES`` inherits this file's refusal tests automatically
#: while production carries no name that only tests consume.
_NO_ALIAS_MASKED_LEAVES: tuple[str, ...] = tuple(
    leaf for leaf in sandbox._CREW_HIDDEN_LEAVES if leaf not in sandbox._CREW_ALIAS_TOLERATED_LEAVES
)


@_POSIX_ONLY
class TestALinkedProtectedLeafRefusesTheSpawn:
    """A disposition attaches to a NAME; following a link voids it.

    The fourth probe of the same fence, and the same shape as the other three: the
    path the launcher covers and the path the bytes reach diverged. Here
    ``.resolve()`` followed the link, so the store wrote through to the target while
    the bind-mask -- which guards on ``isdir`` of the leaf -- attached to the link.
    The link name stays in the writable data home, so a sandboxed process can unlink
    it and drop a directory of its own.

    Refused rather than warned, unlike every other SEALED ceiling: see
    ``sandbox._CREW_NO_ALIAS_LEAVES`` for why the chezmoi/stow argument that earns
    the warning elsewhere does not apply to these two. The MASKED leaves refuse through
    their own pass; ``TestEveryMaskedLeafRefusesAnAliasedName`` below covers those.
    """

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_NO_ALIAS_LEAVES))
    def test_a_symlinked_leaf_refuses(self, crew_home, tmp_path, leaf):
        elsewhere = tmp_path / f"elsewhere-{leaf}"
        elsewhere.mkdir()
        link = crew_home / leaf
        if link.exists() or link.is_symlink():
            link.unlink() if link.is_symlink() else shutil.rmtree(link)
        link.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._materialize_sealable_ceilings()
        assert "SYMLINK" in str(caught.value)
        assert leaf in str(caught.value)

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_NO_ALIAS_LEAVES))
    def test_the_refusal_beats_the_warning_path(self, crew_home, tmp_path, leaf, caplog):
        """It must REFUSE, not log that the path was covered and continue.

        Warning and continuing is what made this silent: the log claimed a seal that
        the bytes never got.
        """
        elsewhere = tmp_path / f"elsewhere2-{leaf}"
        elsewhere.mkdir()
        link = crew_home / leaf
        if link.exists() or link.is_symlink():
            link.unlink() if link.is_symlink() else shutil.rmtree(link)
        link.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()
        # Nothing was written through the link either.
        assert not list(elsewhere.iterdir())

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_NO_ALIAS_LEAVES))
    def test_a_real_directory_is_accepted(self, crew_home, leaf):
        """The refusal must not reject the ordinary case."""
        real = crew_home / leaf
        real.mkdir(parents=True, exist_ok=True)
        sandbox._materialize_sealable_ceilings()  # does not raise
        assert real.is_dir() and not real.is_symlink()

    def test_the_no_alias_set_covers_both_panel_leaves(self):
        """Derived, so a third protected leaf has to be added here too."""
        assert "crew-panels" in sandbox._CREW_NO_ALIAS_LEAVES
        assert "panel-templates" in sandbox._CREW_NO_ALIAS_LEAVES
        # Every no-alias leaf is actually materialised, or the guard never runs.
        materialised = set(sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES) | set(
            sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        )
        assert (
            sandbox._CREW_NO_ALIAS_LEAVES <= materialised
        ), "a no-alias leaf that is never materialised is never checked"


class TestEveryMaskedLeafIsEnumerated:
    """No masked leaf may be silently outside the alias decision.

    The gap this closes was not a weak rule, it was an ABSENT one: a masked leaf that
    nothing materialises reached neither the sealing loop's warning nor the maskable-dir
    refusal, so its alias went unreported. The partition below is what makes a leaf added
    later inherit a decision instead of inheriting silence.
    """

    def test_the_buckets_sum_to_every_masked_leaf(self):
        refused = set(_NO_ALIAS_MASKED_LEAVES)
        tolerated = set(sandbox._CREW_ALIAS_TOLERATED_LEAVES)
        every = set(sandbox._CREW_HIDDEN_LEAVES)

        assert refused | tolerated == every, "a masked leaf in neither bucket is undecided"
        assert not (refused & tolerated), "a leaf cannot both refuse and be tolerated"
        assert len(refused) + len(tolerated) == len(every)

    def test_the_siblings_the_module_groups_together_all_refuse(self):
        """``agent_panel`` names this group; ``crew-panels`` already refused, these did not."""
        for leaf in ("ledger", "routing", "webhooks", "ledgers", "work-ledger"):
            assert leaf in _NO_ALIAS_MASKED_LEAVES, f"{leaf} may still be aliased"

    def test_the_tolerated_set_names_only_masked_leaves(self):
        """A tolerated entry must name a leaf that is actually masked.

        This invariant lives in a test rather than a module-level ``assert`` because
        ``python -O`` strips an assert: an exception naming something outside
        ``_CREW_HIDDEN_LEAVES`` is an exception to nothing, and it would read as a permission
        this pass never actually grants. The partition's SIZE is deliberately not asserted --
        the tuple is built by removing the tolerated names from the hidden ones, so counting
        them again only restates that line.
        """
        hidden = set(sandbox._CREW_HIDDEN_LEAVES)
        stray = sorted(sandbox._CREW_ALIAS_TOLERATED_LEAVES - hidden)
        assert not stray, f"tolerated but not masked: {stray}"

        overlap = sorted(set(_NO_ALIAS_MASKED_LEAVES) & sandbox._CREW_ALIAS_TOLERATED_LEAVES)
        assert not overlap, f"leaf is both refused and tolerated: {overlap}"

    def test_a_relocatable_looking_leaf_refuses_because_nothing_relocates_it(self):
        """``scratch`` and ``backup`` read like relocation candidates and are not.

        Each resolves to one managed path with no override, so a second name is not a
        layout the product offers and the refusal costs no supported setup.
        """
        from kiro_crew import agent_scratch

        assert "scratch" in _NO_ALIAS_MASKED_LEAVES
        assert "backup" in _NO_ALIAS_MASKED_LEAVES
        assert agent_scratch.scratch_root() == sandbox.config_dir() / "scratch"

    def test_every_tolerated_leaf_states_its_reason(self):
        """A bare exception is how a hole gets inherited; each one is argued in the source.

        Scoped to the contiguous ``#:`` block directly above the set, not to the module: a
        whole-module search passes for any leaf whose name appears anywhere earlier, which
        is every masked leaf, so it would assert nothing.
        """
        source = inspect.getsource(sandbox_aliases)
        before = source.split("_CREW_ALIAS_TOLERATED_LEAVES: frozenset")[0]
        block = []
        for line in reversed(before.splitlines()):
            if line.startswith("#:") or line == "#:":
                block.append(line)
            elif block:
                break
        doc = "\n".join(block)
        assert doc, "the tolerated set has no doc-comment block above it"
        for leaf in sandbox._CREW_ALIAS_TOLERATED_LEAVES:
            assert leaf in doc, f"{leaf} is tolerated with no reason recorded beside it"


@_POSIX_ONLY
class TestEveryMaskedLeafRefusesAnAliasedName:
    """A SYMLINKED masked leaf refuses the spawn, for every leaf but the argued exceptions.

    ``mount(2)`` binds what the leaf RESOLVES to, so a symlinked leaf reads as masked and
    is not: the name stays in the writable data home, and a sandboxed process unlinks it
    and puts its own directory or file there. Warning about that is what made it silent.
    """

    @pytest.mark.parametrize("leaf", sorted(_NO_ALIAS_MASKED_LEAVES))
    def test_a_symlinked_masked_leaf_refuses(self, crew_home, tmp_path, leaf):
        elsewhere = tmp_path / f"target-{leaf.replace('/', '-')}"
        elsewhere.mkdir(parents=True, exist_ok=True)
        link = crew_home / leaf
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            shutil.rmtree(link)
        elif link.exists():
            link.unlink()
        link.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._refuse_aliased_masked_leaves()
        assert "SYMLINK" in str(caught.value)
        assert os.path.basename(leaf) in str(caught.value)

    @pytest.mark.parametrize("leaf", sorted(_NO_ALIAS_MASKED_LEAVES))
    def test_nothing_was_written_through_the_link(self, crew_home, tmp_path, leaf):
        """It must REFUSE, not report the path masked and carry on."""
        elsewhere = tmp_path / f"probe-{leaf.replace('/', '-')}"
        elsewhere.mkdir(parents=True, exist_ok=True)
        link = crew_home / leaf
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            shutil.rmtree(link)
        elif link.exists():
            link.unlink()
        link.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._refuse_aliased_masked_leaves()
        assert not list(elsewhere.iterdir())

    def test_a_real_directory_or_file_is_accepted(self, crew_home):
        """The ordinary case must not be refused, or the pass is a blanket outage."""
        for leaf in _NO_ALIAS_MASKED_LEAVES:
            target = crew_home / leaf
            target.parent.mkdir(parents=True, exist_ok=True)
            if "." in os.path.basename(leaf):
                target.write_text("{}\n", encoding="utf-8")
            else:
                target.mkdir(parents=True, exist_ok=True)

        assert sandbox._refuse_aliased_masked_leaves() == ()


@_POSIX_ONLY
class TestTheDeliberateAliasExceptions:
    """Each tolerated shape gets a test asserting it is NOT refused.

    An exception nobody exercises is indistinguishable from a leaf the pass forgot, so the
    permissive direction is pinned as hard as the refusal.
    """

    def test_a_symlinked_env_file_is_tolerated(self, crew_home, tmp_path, caplog):
        """``.env`` is the operator's own file and the dotfile-manager case.

        Tolerated means not REFUSED, not unexamined: the warning must fire, or the exception
        reproduces on the credential leaf the exact silence this pass exists to end.
        """
        real = tmp_path / "dotfiles-env"
        real.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        link = crew_home / ".env"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(real)

        with caplog.at_level("WARNING"):
            sandbox._refuse_aliased_masked_leaves()  # does not raise
        assert link.is_symlink(), "the operator's link is not ours to remove"
        assert any(
            "SYMLINK" in r.getMessage() and ".env" in r.getMessage() for r in caplog.records
        ), "a tolerated symlink must still be reported"

    def test_an_extra_hardlink_on_an_integrity_LEAF_is_tolerated(self, crew_home, tmp_path, caplog):
        """``rsync --link-dest`` and snapshot tools leave one on a healthy host.

        Tolerated for a leaf masked so an agent cannot WRITE it: the write alias is real,
        but its reader re-validates the content and a spawn-wide outage is not proportionate
        to it. A leaf whose BYTES are a credential refuses instead --
        ``TestAMaskedCredentialLeafRefusesASecondHardLink`` covers that side. It is WARNED
        either way: nothing else warns over these leaves, so staying silent would leave the
        alias outside the mask with nothing said about it.
        """
        target = crew_home / "ops_mission_control_policy.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        alias = tmp_path / "backup-hardlink"
        os.link(target, alias)
        assert target.stat().st_nlink == 2

        with caplog.at_level("WARNING"):
            sandbox._refuse_aliased_masked_leaves()  # does not raise
        assert any(
            "hardlinks" in r.getMessage() and "ops_mission_control_policy.json" in r.getMessage()
            for r in caplog.records
        ), "the tolerated shape must still be reported"

    def test_an_absent_leaf_is_skipped_and_nothing_is_created(self, crew_home):
        """The pass must create NOTHING, which is what lets it cover unmaterialised leaves.

        ``ledgers`` is the case that forces this: its own entry says precreating it would
        re-materialise a retired name on every machine.
        """
        before = sorted(p.name for p in crew_home.iterdir())

        sandbox._refuse_aliased_masked_leaves()  # does not raise

        assert sorted(p.name for p in crew_home.iterdir()) == before
        assert not (crew_home / "ledgers").exists(), "the retired root must stay absent"


@_POSIX_ONLY
class TestALinkedComponentBelowTheDataHomeRefuses:
    """``lstat`` un-follows only the FINAL component, so the chain needs its own check.

    A multi-component masked leaf sits under intermediates the agent can write, so a link
    planted at one of them lands the mask on an attacker-chosen tree while the lexical name
    stays replaceable. That is the same hole as a linked leaf, one level up, and a leaf-only
    ``lstat`` cannot see it.
    """

    @pytest.mark.parametrize("leaf", ["apps/aws-control/data", "apps/meetings/data/edits"])
    def test_a_linked_intermediate_refuses(self, crew_home, tmp_path, leaf):
        victim = tmp_path / "agent-owned"
        victim.mkdir(exist_ok=True)
        # Link the FIRST component below the root, leaving the leaf name itself innocent.
        first = leaf.split("/")[0]
        planted = crew_home / first
        if planted.is_symlink():
            planted.unlink()
        elif planted.is_dir():
            shutil.rmtree(planted)
        planted.symlink_to(victim, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._refuse_aliased_masked_leaves()
        assert "LINK" in str(caught.value)
        assert first in str(caught.value)

    def test_a_real_chain_is_accepted(self, crew_home):
        for leaf in ("apps/aws-control/data", "apps/meetings/data/edits"):
            (crew_home / leaf).mkdir(parents=True, exist_ok=True)

        assert sandbox._refuse_aliased_masked_leaves() == ()

    def test_a_symlinked_data_home_itself_is_not_refused(self, tmp_path, monkeypatch):
        """``config_dir()`` documents that a symlinked data HOME is supported.

        Only components BELOW the root are walked, so relocating the whole home by link --
        a layout the product allows -- must not refuse every spawn on the host.
        """
        real = tmp_path / "real-home"
        real.mkdir()
        (real / "ledger").mkdir()
        link = tmp_path / "linked-home"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: link)

        assert sandbox._refuse_aliased_masked_leaves() == ()
        assert link.is_symlink() and (real / "ledger").is_dir()

    def test_the_md_notebook_leaves_degrade_instead_of_refusing(self, crew_home, tmp_path, caplog):
        """Matching the sibling control rather than overriding it.

        ``carveout_chain_has_planted_link`` withholds the carve-out for exactly these
        leaves, and while it does the backend cannot write that state, so an unmasked leaf
        has nothing to expose. Refusing here instead would let one optional app's layout
        take every sandboxed process on the host down with it. Degrading still REPORTS.
        """
        victim = tmp_path / "workspace-elsewhere"
        victim.mkdir()
        planted = crew_home / "workspace"
        if planted.is_symlink():
            planted.unlink()
        elif planted.is_dir():
            shutil.rmtree(planted)
        planted.symlink_to(victim, target_is_directory=True)

        with caplog.at_level("WARNING"):
            sandbox._refuse_aliased_masked_leaves()  # does not raise
        assert any(
            "passes through a component that is a link" in r.getMessage() for r in caplog.records
        ), "degrading must not be silent"

    def test_the_degrade_set_is_derived_from_the_carveout_leaves(self):
        """Hand-listing it is how the two would drift apart.

        Only the retired bare Notes names degrade; the live state directory is a top-level
        name, so a link at it refuses like any other leaf.
        """
        assert sandbox._CREW_ALIAS_CHAIN_DEGRADE_LEAVES == frozenset(
            sandbox._MD_NOTEBOOK_RETIRED_STATE_LEAVES
        )
        assert sandbox._MD_NOTEBOOK_STAGING_LEAF not in sandbox._CREW_ALIAS_CHAIN_DEGRADE_LEAVES
        assert sandbox._CREW_ALIAS_CHAIN_DEGRADE_LEAVES <= set(sandbox._CREW_HIDDEN_LEAVES)
