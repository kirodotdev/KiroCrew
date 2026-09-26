"""An ABSENT crew-home ceiling is still sealed read-only by the Linux launcher.

``mount(2)`` cannot target a path that does not exist, so the ``READONLY_DIRS`` loop's
``if os.path.exists(target)`` guard silently skips a ceiling that has never been written
— and on a default install that is most of them, which left the data home writable at
exactly the names the seal exists to protect. ``_materialize_sealable_ceilings()`` closes
that by creating the absent ceiling first, but only for the leaves that clear both tests
the production comment states: an empty document must mean what an absent file means, and
a STALE read of it must fail toward refusal.

The load-bearing test here is
:meth:`TestSealAppliesToAPreviouslyAbsentCeiling.test_bind_and_remount_pair_is_emitted`:
it executes the launcher's own seal loop (extracted from the generated script, so the
production source is what runs) against a real filesystem, with ``_mount_or_die``
replaced by a recorder. Delete the materialiser and that loop records nothing, because
the guard falls through — which is the whole defect.

:class:`TestCeilingsThatMustNotBeMaterialized` is the fence in the opposite direction,
and it is the one to read before adding a leaf: three ceilings read a present-but-empty
file as something other than absent, and a fourth would be pinned stale in the dangerous
direction by the bind mount itself.
"""

from __future__ import annotations

import errno
import inspect
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from kiro_crew import sandbox

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")

#: Flag values the launcher defines for itself; mirrored so the extracted loop can run.
_MS_RDONLY = 1
_MS_REMOUNT = 32
_MS_BIND = 4096

#: The masked leaves whose NAME must be the masked name: every hidden leaf that is not a
#: declared alias exception. DERIVED here rather than read from a production constant, so a
#: leaf added to ``_CREW_HIDDEN_LEAVES`` inherits this file's refusal tests automatically
#: while production carries no name that only tests consume.
_NO_ALIAS_MASKED_LEAVES: tuple[str, ...] = tuple(
    leaf for leaf in sandbox._CREW_HIDDEN_LEAVES if leaf not in sandbox._CREW_ALIAS_TOLERATED_LEAVES
)


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(monkeypatch):
    """``_build_launcher_script`` asks the HOST's ``ssh -V`` for accept-new support.

    Every test here extracts a loop from the generated launcher; none is about that
    probe, and a real ssh spawned from the test process is a host dependency the
    launcher text must not vary with. Pinned so no binary runs.
    """
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """Point ``config_dir()`` — the live data home — at a scratch tree.

    ``Path.home`` is pinned at the same scratch root, and that half is load-bearing rather
    than tidiness. The masked-leaf passes resolve EVERY crew-home spelling
    (:func:`sandbox._masked_crew_home_roots`), because the masks cover every spelling and a
    check scoped to the live home alone would judge one of them. Under a bare
    ``config_dir()`` patch those passes would read ``$HOME/.kiro/crew`` and
    ``$HOME/.kirocrew`` on the machine running the suite, so a developer whose real data
    home happens to hold a hard-linked credential leaf would see refusals from tests that
    never created one.
    """
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return home


def _seal_loop_source() -> str:
    """The launcher's ``READONLY_DIRS`` loop body, ready to run.

    Pulled out of the generated script rather than restated, so this test cannot pass
    against a loop the launcher does not contain.
    """
    script = sandbox._build_launcher_script("strict")
    loop = (
        "for d in READONLY_DIRS:"
        + script.split("for d in READONLY_DIRS:", 1)[1].split("\n\n", 1)[0]
    )
    return textwrap.dedent(loop)


def _run_seal_loop(targets: list[str]) -> list[tuple[str, int]]:
    """Execute the launcher's seal loop over *targets*, recording every mount call."""
    calls: list[tuple[str, int]] = []

    def _record(source, target, flags, what):
        assert source == target, "a ceiling is bound over ITSELF, not over an empty source"
        calls.append((os.fsdecode(target), flags))

    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(  # noqa: S102 - running the launcher's OWN generated source is the assertion
        _seal_loop_source(),
        {
            "os": os,
            "READONLY_DIRS": targets,
            "_mount_or_die": _record,
            "_MS_BIND": _MS_BIND,
            "_MS_REMOUNT": _MS_REMOUNT,
            "_MS_RDONLY": _MS_RDONLY,
            # Stubbed to 0 so this test keeps asserting the bind+remount PAIR
            # exactly; the flag re-assertion itself is covered by
            # test_sandbox_seal_locked_flags.py against the real helper.
            "_locked_mount_flags": lambda _target: 0,
        },
    )
    return calls


@_POSIX_ONLY
@pytest.mark.parametrize("leaf", ("subagents", "member-memory-bindings"))
def test_run_authority_root_is_sealed_before_its_first_record(crew_home, leaf):
    target = crew_home / leaf
    assert not target.exists()
    sandbox._materialize_sealable_ceilings()
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert _run_seal_loop([str(target)]) == [
        (str(target), _MS_BIND),
        (str(target), _MS_REMOUNT | _MS_BIND | _MS_RDONLY),
    ]


@_POSIX_ONLY
def test_member_memory_is_not_an_os_hidden_root(crew_home):
    target = crew_home / "memory_stores"
    assert not target.exists()
    sandbox.namespace_argv(["/bin/true"])
    script = sandbox._build_launcher_script("standard")
    match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
    assert match and str(target) not in json.loads(match.group(1))


@_POSIX_ONLY
class TestSealAppliesToAPreviouslyAbsentCeiling:
    def test_bind_and_remount_pair_is_emitted(self, crew_home):
        """The seal reaches a ceiling that did not exist when the spawn started.

        Both calls are asserted, not just the first: ``MS_RDONLY`` is ignored on the
        initial ``MS_BIND``, so a bind without the remount grants exactly the write
        access the loop exists to withhold.
        """
        target = str(crew_home / "computer_use.json")
        assert not os.path.exists(target)

        assert target in sandbox._materialize_sealable_ceilings()
        calls = _run_seal_loop([target])

        assert calls == [
            (target, _MS_BIND),
            (target, _MS_REMOUNT | _MS_BIND | _MS_RDONLY),
        ]

    def test_loop_skips_the_ceiling_when_it_was_never_materialized(self, crew_home):
        """The defect, pinned: no target on disk means no seal at all."""
        target = str(crew_home / "computer_use.json")

        assert _run_seal_loop([target]) == []

    def test_every_sealable_leaf_is_created(self, crew_home):
        created = sandbox._materialize_sealable_ceilings()

        for leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES:
            path = crew_home / leaf
            assert str(path) in created
            assert json.loads(path.read_text(encoding="utf-8")) == {}
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

        for leaf in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES:
            path = crew_home / leaf
            assert str(path) in created
            assert path.is_dir()
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

    def test_the_hidden_records_dir_is_materialised(self, crew_home):
        """The MASK's counterpart to the ceiling case above.

        A hidden leaf has the same existence requirement as a read-only ceiling and
        the opposite reason for it: on Linux the mask is a bind mount whose loop
        guards on ``isdir``, so an ABSENT directory is silently skipped -- and
        skipped precisely on the fresh install where the agent could create it
        first and write what the gateway later reads back as authoritative.

        Derived from the tuple, so a second hidden leaf is covered without editing
        this test.
        """
        created = set(sandbox._materialize_sealable_ceilings())

        assert sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES, "empty tuple would be vacuous"
        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            path = crew_home / leaf
            assert str(path) in created, f"{leaf} was not materialised, so its mask is skipped"
            assert path.is_dir()
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

    def test_created_ceilings_are_in_the_launcher_readonly_list(self, crew_home):
        """Creating a path is only useful if the seal loop is handed it.

        Reconciled PER DISPOSITION rather than against one list, because two kinds
        of path are materialised for opposite reasons and each has its own loop:

        * a read-only ceiling is created so the SEAL can apply -> ``READONLY_DIRS``;
        * a hidden leaf is created so the MASK can -> ``SENSITIVE_DIRS``.

        Asserting every created path against ``READONLY_DIRS`` alone would demand
        that a directory meant to be invisible in the sandbox be exposed read-only
        instead -- the exact inversion of its purpose. Derived from the
        precreate tuples, so a leaf added to either disposition must appear in the
        matching launcher list rather than in whichever list this test happened to name.
        """
        created = set(sandbox._materialize_sealable_ceilings())
        script = sandbox._build_launcher_script("strict")

        def _launcher_list(name: str) -> set[str]:
            match = re.search(rf"{name} = (\[.*?\])\n", script, re.S)
            assert match, f"{name} is not emitted by the launcher script"
            return set(json.loads(match.group(1)))

        readonly = _launcher_list("READONLY_DIRS")
        masked = _launcher_list("SENSITIVE_DIRS")

        # Every created path is handed to exactly the loop its disposition needs.
        readonly_leaves = (
            sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
            + sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        )
        for leaf in readonly_leaves:
            path = str(crew_home / leaf)
            if path not in created:
                continue  # covered by test_every_sealable_leaf_is_created
            assert (
                path in readonly
            ), f"{leaf} is created to be read-only but is not in READONLY_DIRS"
            assert path not in masked, (
                f"{leaf} is masked instead of exposed READ-ONLY; masking a governance "
                "ceiling removes it and restores the permissive default"
            )

        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            path = str(crew_home / leaf)
            if path not in created:
                continue  # covered by test_the_hidden_records_dir_is_materialised
            assert path in masked, f"{leaf} is created to be masked but is not in SENSITIVE_DIRS"
            assert path not in readonly, (
                f"{leaf} is exposed READ-ONLY as well as masked; a hidden leaf that "
                "is also readable is not hidden"
            )

        # And nothing created is left with no disposition at all -- the original
        # invariant, widened rather than weakened.
        unaccounted = created - readonly - masked
        assert not unaccounted, f"created but handed to no seal loop: {sorted(unaccounted)}"

    def test_namespace_argv_materializes_before_the_launcher_runs(self, crew_home):
        """The production wiring, not just the helper.

        The seal happens in the launcher CHILD after ``namespace_argv`` returns, so the
        creation has to be on this path — a helper nobody calls seals nothing.
        """
        sandbox.namespace_argv(["/bin/true"])

        assert (crew_home / "computer_use.json").is_file()
        assert (crew_home / "profiles").is_dir()

    def test_relocated_data_home_is_covered(self, tmp_path, monkeypatch):
        """A data home that escapes ``$HOME`` gets the same treatment.

        Without this the fleets that relocate the data home — the ones most likely to
        care about a governance ceiling — would be the only ones left unsealed.
        """
        relocated = tmp_path / "srv" / "crew"
        relocated.mkdir(parents=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)

        assert str(relocated / "computer_use.json") in sandbox._materialize_sealable_ceilings()

    def test_deprecated_home_spelling_gains_no_stubs(self, crew_home, tmp_path):
        """Only the LIVE data home is materialised.

        The deny lists cover both ``_CREW_HOME_PREFIXES`` because either tree may hold
        bytes, but creation is the opposite case: a stub under a migrated host's leftover
        ``~/.kirocrew`` is a file nothing will ever read.
        """
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()

        sandbox._materialize_sealable_ceilings()

        assert list(legacy.iterdir()) == []


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
        (crew_home / leaf).mkdir(parents=True, exist_ok=True)
        sandbox._materialize_sealable_ceilings()  # does not raise

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
        source = inspect.getsource(sandbox)
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

        sandbox._refuse_aliased_masked_leaves()  # does not raise


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
            # live GitHub PAT
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
        stray = sorted(sandbox._CREW_HARDLINK_REFUSED_LEAVES - set(sandbox._CREW_HIDDEN_LEAVES))
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
        source = inspect.getsource(sandbox)
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
class TestTheAuthStoreStagingReconciliation:
    """A staging name that is a second link to the published key is dropped, not refused.

    Without this the hard-link refusal is a one-way door on a state the gateway's own
    publisher creates deliberately: `token_secret` KEEPS the staging name when the directory
    sync after `os.link` fails, as a recoverable second name, and a kill between the link
    and the cleanup unlink leaves the same shape. The key then has two names, so every
    confined spawn refuses, and the operator's only exit is to find and remove the file by
    hand.
    """

    @pytest.fixture
    def staged_link(self, crew_home):
        """The published key plus a staging name that is a second link to it."""
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        stale = staging / ".token_signing.key.4242.deadbeefcafe0123.tmp"
        os.link(key, stale)
        assert key.stat().st_nlink == 2
        return key, stale

    def test_it_removes_the_stale_staging_link(self, staged_link):
        key, stale = staged_link

        removed = sandbox._reconcile_auth_store_staging_links()

        assert not stale.exists(), "the stale staging link survived"
        assert str(stale) in removed
        assert key.read_bytes() == b"k" * 32, "reconciling must not touch the key's bytes"
        assert key.stat().st_nlink == 1

    def test_the_spawn_path_is_not_deadlocked_by_it(self, staged_link):
        """The whole point: the refusal must be reachable past this state, not blocked by it.

        Asserted as the two calls in the order the spawn path uses them, because that order
        IS the fix -- with the refusal first this raises and no later spawn can ever clear
        it.
        """
        sandbox._reconcile_auth_store_staging_links()
        sandbox._refuse_aliased_masked_leaves()  # does not raise

    def test_it_syncs_the_keys_parent_BEFORE_dropping_the_second_name(
        self, staged_link, monkeypatch
    ):
        """Sharing an inode makes the staged name redundant for READING, not for durability.

        The publisher keeps that name precisely when the sync of the key's own parent
        failed, so at that moment the key's directory entry may not have reached the device
        and the staged name is the inode's one other reference. Dropping it first and
        crashing leaves the inode with no name at all.
        """
        key, stale = staged_link
        seen: list[tuple[str, bool]] = []

        def fake_sync(path, **kwargs):
            seen.append((str(path), stale.exists()))

        monkeypatch.setattr(sandbox, "fsync_dir", fake_sync)

        sandbox._reconcile_auth_store_staging_links()

        assert seen, "the key's own parent was never synced"
        synced, stale_still_there = seen[0]
        assert synced == str(key.parent), f"synced {synced}, not the key's parent"
        assert stale_still_there, (
            "the staged link was unlinked BEFORE the sync that makes the key's own name "
            "durable, so a crash in between would leave the inode with no name"
        )
        assert not stale.exists(), "the link survived a successful sync"

    def test_a_device_refused_sync_refuses_the_spawn_and_KEEPS_the_link(
        self, staged_link, monkeypatch
    ):
        """A failing sync means the key's entry is not durable, so its other name stays.

        Refusing is the honest outcome: the spawn cannot proceed while the leaf carries two
        names, and dropping the second one on a host whose device refuses the write trades
        a refused spawn for a possibly unrecoverable signing key.
        """
        key, stale = staged_link

        def refuse_sync(path, **kwargs):
            raise OSError(errno.EIO, "device refused the directory write")

        monkeypatch.setattr(sandbox, "fsync_dir", refuse_sync)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._reconcile_auth_store_staging_links()

        assert stale.exists(), "the key's only other name was dropped after a failed sync"
        assert key.stat().st_nlink == 2
        assert "durable" in str(caught.value)

    def test_an_unsupported_sync_still_drops_the_link(self, staged_link, monkeypatch):
        """``fsync_dir`` returns quietly where a directory sync cannot be EXPRESSED.

        Windows has no directory descriptor and some network mounts reject one, so a quiet
        return must not be read as a refusal -- that would make every such host refuse
        every spawn.
        """
        _key, stale = staged_link
        monkeypatch.setattr(sandbox, "fsync_dir", lambda path, **kwargs: None)

        removed = sandbox._reconcile_auth_store_staging_links()

        assert not stale.exists()
        assert str(stale) in removed

    def test_no_sync_when_there_is_no_matching_alias(self, crew_home, monkeypatch):
        """The cost bound: an ordinary spawn must not pay a directory sync.

        The key here has a second name, so the link-count test passes, but nothing in the
        staging directory shares its inode -- so there is nothing to drop and nothing to
        make durable first.
        """
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, crew_home / "alias-outside-staging")
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        (staging / ".token_signing.key.1.aa.tmp").write_bytes(b"n" * 32)
        calls: list[str] = []
        monkeypatch.setattr(sandbox, "fsync_dir", lambda path, **kw: calls.append(str(path)))

        sandbox._reconcile_auth_store_staging_links()

        assert calls == [], f"synced {calls} with no alias to drop"

    def test_a_staged_file_for_an_UNPUBLISHED_key_is_left_alone(self, crew_home):
        """The bound, and the inode identity is what draws it.

        A publish still in flight has staged its bytes but not linked them into place, so
        its temp is a DIFFERENT inode. Unlinking that would destroy a concurrent writer's
        work between its staging write and its link.
        """
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, crew_home / "some-other-alias")
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        inflight = staging / ".token_signing.key.9999.abcdef0123456789.tmp"
        inflight.write_bytes(b"n" * 32)

        removed = sandbox._reconcile_auth_store_staging_links()

        assert inflight.exists(), "an in-flight staging write for a different inode was removed"
        assert removed == []

    def test_a_non_regular_staging_entry_is_left_alone(self, crew_home):
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, crew_home / "alias")
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        subdir = staging / "not-a-file"
        subdir.mkdir()

        sandbox._reconcile_auth_store_staging_links()

        assert subdir.is_dir(), "a directory inside the staging dir was removed"

    def test_it_is_quiet_when_the_key_has_one_name(self, crew_home):
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)

        assert sandbox._reconcile_auth_store_staging_links() == []

    def test_it_is_quiet_with_no_staging_directory(self, crew_home):
        (crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF).write_bytes(b"k" * 32)

        assert sandbox._reconcile_auth_store_staging_links() == []

    def test_cleanup_runs_before_the_refusal_on_both_launch_paths(self):
        """The ordering contract, pinned in source rather than left to call-site drift.

        A future edit that moves a refusal above the cleanup restores the deadlock, and
        nothing else in the suite would notice: every test here calls the functions directly.

        Both launch paths must reach the CREDENTIAL refusal. A Seatbelt deny is path-shaped,
        so a second hard link is read straight through the profile -- the same exposure the
        namespace path refuses on -- which is why this is not namespace-only work.
        """
        for builder in (
            sandbox.namespace_argv,
            sandbox.sandbox_exec_argv,
        ):
            body = inspect.getsource(builder)
            cleanup = body.index("_reconcile_auth_store_staging_links()")
            # The namespace path reaches the credential refusal THROUGH
            # _refuse_aliased_masked_leaves, which owns the whole alias family; the Seatbelt
            # path calls only the credential half, because the symlink half is a bind-mask
            # concern. Either spelling counts, and neither platform may have none.
            # Matched WITHOUT the closing paren: the Seatbelt path passes
            # `masks_are_path_only=True` now, and pinning `()` made this guard pass only
            # while neither call took an argument -- a guard that a keyword can switch off
            # is not guarding the property it names.
            assert (
                "_refuse_multilinked_credential_leaves(" in body
                or "_refuse_aliased_masked_leaves(" in body
            ), (
                f"{builder.__name__} never judges a second hard link on a credential leaf, "
                "so an alias is readable on that platform"
            )
            for refusal_name in (
                "_refuse_multilinked_credential_leaves(",
                "_refuse_aliased_masked_leaves(",
            ):
                if refusal_name not in body:
                    continue
                assert cleanup < body.index(refusal_name), (
                    f"{builder.__name__} runs {refusal_name} before the reconciliation that "
                    "clears the link it refuses on, which makes the refusal permanent"
                )

    def test_an_unremovable_link_in_an_AGENT_created_home_does_not_refuse(
        self, crew_home, tmp_path, monkeypatch, caplog
    ):
        """The reconciler must carry the same live-home split as the passes around it.

        A sandboxed shell can create an unused home, plant a key plus a staging link to it,
        and make the directory unwritable. Escalating there would refuse every spawn on the
        host over a file the governed process wrote itself.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        planted = tmp_path / ".kirocrew"
        staging = planted / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(parents=True)
        key = planted / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, staging / "x")

        def _refuse_unlink(_name, **_kwargs):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(sandbox.os, "unlink", _refuse_unlink)
        with caplog.at_level(logging.WARNING):
            sandbox._reconcile_auth_store_staging_links()

        assert (staging / "x").exists(), "the link went despite the unlink failing"
        assert any(
            "stale auth-store staging link" in r.getMessage() for r in caplog.records
        ), "the condition was neither refused nor reported, so it is invisible"

    def test_the_legacy_prefix_is_derived_from_the_key_leaf(self):
        """Two spellings of one filename is how a rename half-lands."""
        assert (
            sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX == f".{sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF}."
        )
        assert sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF in sandbox._CREW_HARDLINK_REFUSED_LEAVES

    def test_an_agent_CHOSEN_name_cannot_rewrite_the_remedy(self, crew_home, monkeypatch):
        """The name in the message comes from ``os.listdir`` on an agent-writable root.

        So it is attacker-chosen text going into a message an operator is asked to act on.
        Terminal escapes in it could rewrite the remedy itself, which is why every
        interpolated value is sanitised rather than only the ones that looked risky.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: crew_home.parent))
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        hostile = crew_home / f"{sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX}1.\x1b[2Krm -rf x\r.tmp"
        hostile.write_bytes(b"staged")

        def _refuse_unlink(_name, **_kwargs):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(sandbox.os, "unlink", _refuse_unlink)
        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._sweep_legacy_auth_store_temps()

        detail = str(caught.value)
        assert "\x1b" not in detail and "\r" not in detail, (
            "an agent-chosen filename reached the refusal message with its control bytes "
            "intact, so it can rewrite the remedy the operator is told to follow"
        )


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
            source.index("_materialize_sealable_ceilings()"),
            source.index("_materialize_maskable_dirs()"),
            source.index("_materialize_md_notebook_mask_targets()"),
            source.index("_materialize_live_target_mask_target()"),
            source.index("_refuse_aliased_masked_leaves()"),
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
        victim = tmp_path / "agent-owned"
        parts = leaf.replace(os.sep, "/").split("/")
        (victim / "/".join(parts[1:-1])).mkdir(parents=True, exist_ok=True)
        target = victim / "/".join(parts[1:])
        target.write_bytes(b"secret")
        os.link(target, victim / "second-name")
        assert target.stat().st_nlink == 2
        planted = crew_home / parts[0]
        if planted.is_symlink():
            planted.unlink()
        elif planted.is_dir():
            shutil.rmtree(planted)
        planted.symlink_to(victim, target_is_directory=True)
        return target

    def test_the_refusal_skips_it(self, crew_home, tmp_path):
        for leaf in self._multi_component_leaves():
            self._plant(crew_home, tmp_path, leaf)

        sandbox._refuse_multilinked_credential_leaves()  # does not raise

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

    Returning a path the builder never receives would close nothing while reading, in every
    unit test of the pass itself, exactly like a fix. So these drive the real launch path and
    inspect what the builder is handed.
    """

    def _capture(self, monkeypatch) -> list:
        seen: list = []
        real = sandbox._build_launcher_script

        def _spy(*args, **kwargs):
            seen.append(tuple(kwargs.get("extra_hidden_dirs") or ()))
            return real(*args, **kwargs)

        monkeypatch.setattr(sandbox, "_build_launcher_script", _spy)
        return seen

    def test_an_agent_created_home_does_not_refuse_AND_masks_the_sibling(
        self, crew_home, tmp_path, monkeypatch, caplog
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

        seen = self._capture(monkeypatch)
        with caplog.at_level(logging.WARNING):
            sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert seen, "the launcher was never built, so this test proves nothing"
        assert str(sibling) in seen[-1], (
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

        seen = self._capture(monkeypatch)
        with caplog.at_level(logging.WARNING):
            sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert seen, "the launcher was never built, so this test proves nothing"
        assert str(elsewhere) in seen[-1], (
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

    def test_the_alias_is_handed_over_as_a_FAIL_CLOSED_mask(self, crew_home, monkeypatch, tmp_path):
        """Discovery and the mask bind are two acts, so the bind must not skip a vanished alias.

        The launcher's ordinary policy for a sensitive file that is not there is to skip it --
        an unused store is left absent rather than scaffolded. For a path the walk DISCOVERED
        moments ago that policy is wrong: absence means the name moved between the two acts and
        the credential answers to whatever it is called now, with no warning anywhere. So the
        alias has to arrive as a path whose absence is a fault, and the emitted launcher has to
        refuse on it rather than skip.
        """
        seen: dict = {}
        real = sandbox._build_launcher_script

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(sandbox, "_build_launcher_script", _spy)
        target = crew_home / ".env"
        target.write_text("SLACK_BOT_TOKEN=x\n", encoding="utf-8")
        alias = crew_home / "second-name"
        os.link(target, alias)

        sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert str(alias) in ({entry[0] for entry in (seen.get("fail_closed_file_masks") or ())}), (
            "the discovered alias was passed as an ordinary hidden path, so the launcher would "
            "SKIP it if it had been renamed since discovery and the bytes stay readable"
        )
        info = os.lstat(alias)
        carried = {entry[0]: tuple(entry[1:]) for entry in seen["fail_closed_file_masks"]}
        assert carried[str(alias)] == (info.st_dev, info.st_ino), (
            "the alias arrived without the inode it was discovered as, so the launcher can only "
            "judge the path's shape -- and a decoy left at that name is one link away from "
            "passing a mode and link-count test"
        )
        script = real("strict", fail_closed_file_masks=((str(alias), info.st_dev, info.st_ino),))
        assert "FAIL_CLOSED_FILE_MASKS" in script and str(alias) in script
        assert (
            "credential alias" in script
        ), "the launcher carries no refusal for a vanished alias, so absence is still a skip"
        assert "_want_ino" in script, (
            "the launcher compares no inode, so a file substituted at the same path is masked "
            "while the credential stays reachable under its new name"
        )

    def test_one_located_name_does_NOT_buy_a_pass_while_another_is_unlocated(
        self, crew_home, tmp_path, monkeypatch, caplog
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

        self._capture(monkeypatch)
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

    def test_the_same_leaf_does_NOT_refuse_when_the_second_name_is_reachable(
        self, crew_home, monkeypatch
    ):
        """Same leaf, same link count -- only the alias's LOCATION differs.

        This is what separates the two directions above. Without it, the refusal test could
        pass because the leaf refuses unconditionally.
        """
        target = crew_home / "token_signing.key"
        target.write_bytes(b"key")
        alias = crew_home / "reachable-alias"
        os.link(target, alias)

        seen = self._capture(monkeypatch)
        sandbox.namespace_argv(["echo", "ok"], sandbox_level="strict")

        assert seen and str(alias) in seen[-1]


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

        sandbox._refuse_aliased_masked_leaves()  # does not raise

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

        sandbox._refuse_aliased_masked_leaves()  # does not raise

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
        """Hand-listing it is how the two would drift apart."""
        assert sandbox._CREW_ALIAS_CHAIN_DEGRADE_LEAVES == frozenset(
            sandbox._MD_NOTEBOOK_PRECREATE_CONTENT
        )
        assert sandbox._CREW_ALIAS_CHAIN_DEGRADE_LEAVES <= set(sandbox._CREW_HIDDEN_LEAVES)


@_POSIX_ONLY
class TestPublishIsAllOrNothing:
    """The ceiling path never appears holding anything but the complete document."""

    def test_partial_write_publishes_nothing(self, crew_home, monkeypatch):
        """A zero-length ``*.json`` would read as CORRUPT, not as absent."""

        def _boom(fd, data):
            raise OSError("disk full")

        monkeypatch.setattr(os, "write", _boom)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        for leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES:
            assert not (crew_home / leaf).exists()

    def test_no_temp_file_is_left_behind(self, crew_home, monkeypatch):
        """The temp sibling is reclaimed on the failure path as well as the success one."""

        def _boom(src, dst):
            raise OSError("cross-device link")

        monkeypatch.setattr(os, "link", _boom)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        assert [p.name for p in crew_home.iterdir() if p.name.startswith(".kirocrew-ceiling")] == []

    def test_publish_never_clobbers_a_racing_writer(self, crew_home):
        """``os.link`` fails with EEXIST rather than overwriting.

        The upstream ``os.path.exists`` check is an optimisation, not the guard — a
        second spawn, or an operator writing the real document, can land between it and
        the publish.
        """
        target = crew_home / "computer_use.json"
        target.write_text('{"enabled": true}', encoding="utf-8")

        assert sandbox._publish_empty_ceiling(str(target), str(crew_home)) is False
        assert target.read_text(encoding="utf-8") == '{"enabled": true}'

    def test_existing_ceiling_is_left_byte_for_byte_alone(self, crew_home):
        """Never a truncate: an operator's document outranks the absent default."""
        target = crew_home / "aws_service_consent.json"
        target.write_text('{"s3": "confirmed"}', encoding="utf-8")

        created = sandbox._materialize_sealable_ceilings()

        assert str(target) not in created
        assert target.read_text(encoding="utf-8") == '{"s3": "confirmed"}'


@_POSIX_ONLY
class TestAShortWriteNeverPublishes:
    """``os.write`` may consume part of the buffer and report it as success."""

    def test_partial_progress_is_completed_not_published_short(self, crew_home):
        """The loop finishes the document; a one-byte-at-a-time write still lands whole."""
        real_write = os.write
        calls: list[int] = []

        def _one_byte(fd, data):
            calls.append(len(data))
            return real_write(fd, bytes(data[:1]))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "write", _one_byte)
            assert sandbox._publish_empty_ceiling(
                str(crew_home / "computer_use.json"), str(crew_home)
            )

        assert len(calls) > 1, "a short write must be retried, not accepted"
        assert (crew_home / "computer_use.json").read_bytes() == sandbox._EMPTY_CEILING_DOCUMENT

    def test_zero_progress_publishes_nothing(self, crew_home, monkeypatch):
        """A filesystem accepting nothing must fail, not spin forever."""
        monkeypatch.setattr(os, "write", lambda fd, data: 0)

        assert (
            sandbox._publish_empty_ceiling(str(crew_home / "computer_use.json"), str(crew_home))
            is False
        )
        assert not (crew_home / "computer_use.json").exists()


@_POSIX_ONLY
class TestAnUnsealedCeilingIsNeverSilent:
    """A seal that could not be established is logged, because nothing else reports it.

    Raising instead would reach far past this seal: ``namespace_argv`` runs under
    ``wrap_argv``, whose callers catch narrowly and degrade their own operation, so an
    additive control would take every sandboxed spawn on the host down with it.
    """

    def test_failed_dir_creation_warns(self, crew_home, monkeypatch, caplog):
        monkeypatch.setattr(
            os, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs"))
        )

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                sandbox._materialize_sealable_ceilings()

        assert "REFUSING to launch" in caplog.text
        assert sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES[0] in caplog.text

    def test_failed_publish_warns(self, crew_home, monkeypatch, caplog):
        monkeypatch.setattr(
            os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("no hardlinks"))
        )

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                sandbox._materialize_sealable_ceilings()

        # Only the FIRST unsealable ceiling is reached: the refusal is immediate, which is
        # the point -- the loop must not carry on creating the rest behind a known hole.
        assert "REFUSING to launch" in caplog.text
        assert sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES[0] in caplog.text

    def test_success_is_quiet(self, crew_home, caplog):
        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "REFUSING to launch" not in caplog.text


@_POSIX_ONLY
class TestACreationFailureRefusesTheSpawn:
    """A ceiling that cannot be created is a ceiling that will not be sealed.

    Earlier revisions warned and continued here. That is the shape where the launcher's
    ``exists`` guard silently skips the path and the agent runs with a writable keystone,
    so the failure is fatal to the spawn instead — the ``_mount_or_die`` posture. No
    ``wrap_argv`` caller falls back to running the command unconfined, so refusing costs
    the operation, never the confinement.
    """

    def test_unwritable_data_home_refuses(self, crew_home, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(os, "mkdir", _boom)
        monkeypatch.setattr(tempfile, "mkstemp", _boom)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_a_failed_publish_refuses(self, crew_home, monkeypatch):
        monkeypatch.setattr(
            os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("no hardlinks"))
        )

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_a_race_that_another_writer_won_is_benign(self, crew_home, monkeypatch):
        """``EEXIST`` means the path now exists, so the launcher seals it: nothing to do."""
        target = crew_home / "computer_use.json"

        def _link_but_someone_won(src_path, dst):
            # Honour the dst actually being published: every ceiling in the loop goes
            # through this stub, and a stub that only ever creates ONE of them leaves the
            # others genuinely unsealable -- which is a different scenario.
            Path(os.fsdecode(dst)).write_text('{"enabled": false}', encoding="utf-8")
            raise FileExistsError("raced")

        monkeypatch.setattr(os, "link", _link_but_someone_won)

        created = sandbox._materialize_sealable_ceilings()

        assert str(target) not in created
        assert target.read_text(encoding="utf-8") == '{"enabled": false}'

    def test_an_existing_dir_ceiling_is_benign(self, crew_home):
        (crew_home / "profiles").mkdir()

        created = sandbox._materialize_sealable_ceilings()

        assert str(crew_home / "profiles") not in created

    def test_absent_data_home_is_not_created(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope" / "crew"
        monkeypatch.setattr(sandbox, "config_dir", lambda: missing)

        assert sandbox._materialize_sealable_ceilings() == []
        assert not missing.exists()

    def test_unresolvable_data_home_yields_nothing(self, monkeypatch):
        def _boom():
            raise RuntimeError("no data home")

        monkeypatch.setattr(sandbox, "config_dir", _boom)

        assert sandbox._sealable_absent_ceilings() == ([], [])
        assert sandbox._materialize_sealable_ceilings() == []


class TestAMissingParentRefusesTheSpawn:
    """A ceiling whose parent is not a directory refuses instead of scaffolding.

    Building the parents would create a writable ancestor the agent could
    rename through, so the materialiser names the path and raises rather than
    running the spawn unprotected.
    """

    def test_missing_dir_parent_refuses(self, crew_home, monkeypatch, tmp_path):
        ghost = str(tmp_path / "no-such-dir" / "profiles")
        monkeypatch.setattr(sandbox, "_sealable_absent_ceilings", lambda: ([ghost], []))

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()
        assert not (tmp_path / "no-such-dir").exists()

    def test_missing_file_parent_refuses(self, crew_home, monkeypatch, tmp_path):
        ghost = str(tmp_path / "no-such-dir" / "computer_use.json")
        monkeypatch.setattr(sandbox, "_sealable_absent_ceilings", lambda: ([], [ghost]))

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()
        assert not (tmp_path / "no-such-dir").exists()


@_POSIX_ONLY
class TestADanglingSymlinkRefusesTheSpawn:
    """The one state that defeats every ``os.path.exists`` guard on this path at once.

    ``exists`` FOLLOWS symlinks, so a dangling link reports as absent: the materialiser
    tries to create and ``os.link`` refuses with ``EEXIST``, the launcher's seal loop
    emits no mount, and the sandboxed process's write then follows the link to a file the
    host afterwards reads through the ceiling path. The link is plantable through the very
    hole this change closes, so an agent on an earlier build could pre-arm it.

    It is REFUSED, not removed. ``islink`` followed by ``unlink`` is not atomic and the
    dashboard publishes a real keystone over that same name with ``atomic_write``, so a
    removal racing a validated operator write would delete the operator's new settings —
    and POSIX has no unlink-only-if-still-a-symlink to close that window with.
    """

    def test_the_unguarded_chain_really_is_exploitable(self, crew_home):
        """Pin the mechanism itself, so the refusal below is not guarding a phantom."""
        target = crew_home / "computer_use.json"
        victim = crew_home / "elsewhere.json"
        target.symlink_to(victim)

        assert os.path.lexists(target) is True
        assert os.path.exists(target) is False, "exists() follows the link -> reads as absent"
        # The launcher's own guard therefore skips it: no bind, no remount.
        assert _run_seal_loop([str(target)]) == []
        # And a write through the link lands where the host will read it back.
        target.write_text('{"enabled": true}', encoding="utf-8")
        assert victim.exists()
        assert target.read_text(encoding="utf-8") == '{"enabled": true}'

    def test_a_file_ceiling_squatter_refuses(self, crew_home):
        target = crew_home / "computer_use.json"
        target.symlink_to(crew_home / "elsewhere.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_sealable_ceilings()

        assert "computer_use.json" in str(err.value)
        assert "elsewhere.json" in str(err.value), "the destination is the diagnostic value"

    def test_a_dir_ceiling_squatter_refuses(self, crew_home):
        target = crew_home / "profiles"
        target.symlink_to(crew_home / "no-such-dir")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_the_squatter_is_never_removed(self, crew_home):
        """Removing it is the data-loss path this refusal exists to avoid."""
        target = crew_home / "computer_use.json"
        target.symlink_to(crew_home / "elsewhere.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        assert target.is_symlink(), "the link is the operator's to resolve, not ours to delete"
        assert not (crew_home / "elsewhere.json").exists(), "nothing written through the link"

    def test_namespace_argv_refuses_rather_than_launching(self, crew_home):
        """The refusal has to reach the spawn path, or it protects nothing."""
        (crew_home / "computer_use.json").symlink_to(crew_home / "elsewhere.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox.namespace_argv(["/bin/true"])

    def test_a_resolving_symlink_is_left_alone(self, crew_home):
        """It reads as present, so the launcher seals the inode it resolves to.

        The remaining exposure — the link NAME stays replaceable in a writable parent — is
        pre-existing for every ceiling and cannot be closed without sealing the data-home
        root, so this change must neither refuse nor delete on account of it.
        """
        real = crew_home / "elsewhere.json"
        real.write_text('{"enabled": false}', encoding="utf-8")
        target = crew_home / "computer_use.json"
        target.symlink_to(real)

        created = sandbox._materialize_sealable_ceilings()

        assert str(target) not in created
        assert target.is_symlink()
        assert real.read_text(encoding="utf-8") == '{"enabled": false}'


@_POSIX_ONLY
class TestGatewayLauncherDirectoryNeedsARealLeaf:
    @pytest.mark.parametrize("leaf", ("playwright-cli", "subagents", "member-memory-bindings"))
    def test_a_resolving_symlink_refuses_the_spawn(self, crew_home, tmp_path, leaf):
        real = tmp_path / "attacker-controlled"
        real.mkdir()
        target = crew_home / leaf
        target.symlink_to(real, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        assert target.is_symlink(), "the operator must remove the refused link"

    def test_a_symlink_winning_the_create_race_refuses(self, crew_home, tmp_path, monkeypatch):
        target = crew_home / "playwright-cli"
        real = tmp_path / "race-winner"
        real.mkdir()
        real_mkdir = os.mkdir

        def _mkdir(path, mode=0o777, *, dir_fd=None):
            if os.fspath(path) == os.fspath(target):
                target.symlink_to(real, target_is_directory=True)
                raise FileExistsError("symlink won the race")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "mkdir", _mkdir)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        assert target.is_symlink()


@_POSIX_ONLY
class TestAnAliasBackedCeilingIsReported:
    """``MS_RDONLY`` binds a MOUNT, not an inode, so a second name survives the seal.

    Both shapes are PRE-EXISTING -- the ceilings this module publishes end at
    ``st_nlink == 1`` and are never symlinks (pinned below) -- so they are reported rather
    than refused: a dotfile manager or a snapshot tool giving a config file a second name
    is ordinary, and failing the spawn over it is a far wider blast radius than the hole.
    """

    def test_what_this_module_publishes_is_never_alias_backed(self, crew_home):
        """The premise of reporting rather than refusing: we never create the shape."""
        sandbox._materialize_sealable_ceilings()

        for leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES:
            path = crew_home / leaf
            assert path.stat().st_nlink == 1, "a published ceiling must have no alias"
            assert not path.is_symlink()
        assert [p.name for p in crew_home.iterdir() if p.name.startswith(".kirocrew-ceiling")] == []

    def test_a_symlinked_ceiling_is_reported(self, crew_home, caplog):
        real = crew_home / "elsewhere.json"
        real.write_text("{}", encoding="utf-8")
        (crew_home / "computer_use.json").symlink_to(real)

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "is a SYMLINK" in caplog.text
        assert "computer_use.json" in caplog.text

    def test_a_hardlinked_ceiling_is_reported(self, crew_home, caplog):
        target = crew_home / "computer_use.json"
        target.write_text("{}", encoding="utf-8")
        os.link(target, crew_home / "alias.json")
        assert target.stat().st_nlink == 2

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "hardlinks" in caplog.text
        assert "computer_use.json" in caplog.text

    def test_a_symlinked_dir_ceiling_is_reported(self, crew_home, caplog):
        real = crew_home / "real-profiles"
        real.mkdir()
        (crew_home / "profiles").symlink_to(real)

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "is a SYMLINK" in caplog.text
        assert "profiles" in caplog.text

    def test_reporting_never_refuses_and_never_removes(self, crew_home):
        """The whole point of warning: an ordinary dotfile-manager host still runs."""
        real = crew_home / "elsewhere.json"
        real.write_text('{"enabled": false}', encoding="utf-8")
        link = crew_home / "computer_use.json"
        link.symlink_to(real)

        sandbox._materialize_sealable_ceilings()  # must not raise

        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == '{"enabled": false}'

    def test_an_ordinary_single_link_ceiling_is_quiet(self, crew_home, caplog):
        (crew_home / "computer_use.json").write_text("{}", encoding="utf-8")

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "SYMLINK" not in caplog.text
        assert "hardlinks" not in caplog.text


@_POSIX_ONLY
class TestCeilingsThatMustNotBeMaterialized:
    """Four ceilings the seal deliberately does not reach, for two distinct reasons.

    ``denied_commands.json`` reads ``{}`` as its absent default, but a bind mount pins
    the INODE while every dashboard writer publishes a NEW one through ``atomic_write``,
    so a sealed stub would report "nothing is denied" to in-sandbox ``mcp_cron`` for the
    rest of the sandbox's life. The other three read a present-but-empty file as
    something other than absent: ``security_policy.json`` raises
    ``PlatformCompositionError`` out of ``governance.load_security_policy`` (which reruns
    at boot and per app callback), ``app_admission.json`` flips ``open_default()`` into
    deny-all, and ``admission_policy.json`` is already seeded at first run.
    """

    EXCLUDED = (
        "denied_commands.json",
        "security_policy.json",
        "app_admission.json",
        "admission_policy.json",
    )

    @pytest.mark.parametrize("leaf", EXCLUDED)
    def test_leaf_is_not_in_the_precreate_set(self, leaf):
        assert leaf in sandbox._CREW_READONLY_LEAVES
        assert leaf not in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        assert leaf not in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES

    @pytest.mark.parametrize("leaf", EXCLUDED)
    def test_leaf_is_not_written_to_disk(self, leaf, crew_home):
        sandbox._materialize_sealable_ceilings()

        assert not (crew_home / leaf).exists()


class TestMaskableDirsAreMaterializedBeforeTheSpawn:
    """An on-demand HIDDEN directory gets the mirror treatment of the ceilings above.

    The ``SENSITIVE_DIRS`` loop is guarded on ``isdir``, so a leaf the gateway creates
    lazily is unmasked in every sandbox spawned before its first use -- and once the
    gateway does create it, that running sandbox sees it. Creating it empty before the
    spawn is what gives the mask a name to bind over.
    """

    def test_every_maskable_leaf_is_created_owner_only(self, crew_home):
        created = sandbox._materialize_maskable_dirs()

        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            path = crew_home / leaf
            assert str(path) in created
            assert path.is_dir()
            # The mode is a POSIX property; this materialisation feeds the Linux
            # namespace launcher, and Windows ignores the mode argument.
            if os.name == "posix":
                assert stat.S_IMODE(path.stat().st_mode) == 0o700

    def test_the_leaf_is_created_directly_under_the_data_home(self, crew_home):
        # A top-level leaf on purpose: every intermediate directory between the
        # data home and the mask would be an agent-writable ancestor that a rename
        # could swap out from under a transfer (see storage.STAGING_DIR_LEAF).
        sandbox._materialize_maskable_dirs()
        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            assert "/" not in leaf
            assert (crew_home / leaf).is_dir()

    def test_an_existing_directory_is_left_alone_and_not_reported(self, crew_home):
        """Every leaf, not one of them: with a leaf hardcoded here, adding a second one to
        ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` makes materialisation report the new leaf and
        this assertion fail for a reason that has nothing to do with what it checks."""
        markers = []
        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            target = crew_home / leaf
            target.mkdir(parents=True)
            marker = target / "pre-existing-content"
            marker.mkdir()
            markers.append(marker)

        assert sandbox._materialize_maskable_dirs() == []
        assert markers, "no maskable leaves are declared, so this proves nothing"
        for marker in markers:
            assert marker.is_dir()

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", ["standard", "cc", "strict"])
    def test_an_absent_ledgers_root_is_materialized_before_the_mask_binds(self, crew_home, mode):
        """The sharpest case of this whole class, named on its own.

        A crew log is the authority a reader trusts instead of re-deriving, and the
        store creates this root on its first write. Absent, the ``isdir`` guard
        skips it and the mask is vacuous for the life of every sandbox spawned
        first -- one of which can then create the directory itself and fill it with
        entries attributed to the gateway.
        """
        root = crew_home / "crew-log"
        assert not root.exists(), "the point of the test is that it starts absent"

        created = sandbox._materialize_maskable_dirs()

        assert str(root) in created
        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        script = sandbox._build_launcher_script(mode)
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        assert str(root) in set(json.loads(match.group(1)))

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", ["standard", "cc", "strict"])
    def test_created_dirs_are_in_the_launcher_hidden_list(self, crew_home, mode):
        """Creating a path is only useful if the mask loop is handed it."""
        created = sandbox._materialize_maskable_dirs()
        script = sandbox._build_launcher_script(mode)
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        hidden = set(json.loads(match.group(1)))

        assert created
        assert set(created) <= hidden

    @_POSIX_ONLY
    def test_namespace_argv_materializes_the_masked_dirs(self, crew_home):
        sandbox.namespace_argv(["/bin/true"])

        assert (crew_home / "aws-control-staging").is_dir()

    def test_a_file_squatting_the_name_refuses_the_spawn(self, crew_home):
        # A plain file where the directory should be cannot be masked by the dir
        # loop (``isdir`` is false) and would be skipped silently; refuse instead.
        squat = crew_home / "aws-control-staging"
        squat.parent.mkdir(parents=True, exist_ok=True)
        squat.write_text("not a directory", encoding="utf-8")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_maskable_dirs()

    def test_an_unresolvable_data_home_refuses_the_spawn(self, monkeypatch):
        # Skipping the mask because the data home could not be resolved would
        # run the agent with the staging directory visible -- the exposure this
        # function exists to prevent. Fail closed, like every other reason.
        def boom():
            raise OSError("data home unavailable")

        monkeypatch.setattr(sandbox, "config_dir", boom)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_maskable_dirs()


@_POSIX_ONLY
class TestASymlinkMaskableLeafRefusesTheSpawn:
    """A HIDDEN-dir leaf that is a symlink is GPT's staging-mask bypass, and is refused.

    ``os.path.isdir`` follows a link, so a leaf that RESOLVES to a real directory reads as
    a directory and the mask loop would bind over the link's TARGET, not the leaf name.
    The name lives in the writable data home, so a sandboxed process can unlink it and put
    an agent-owned directory in its place -- and the pre-created staging directory the
    preview CLI writes into is then one the agent controls. ``_refuse_if_dangling_symlink``
    only rejects a link that resolves to nothing, so ``_refuse_if_symlink_leaf`` closes the
    RESOLVING case; the create-race re-check closes the swap-during-the-window case.
    """

    def test_the_unguarded_chain_really_is_exploitable(self, crew_home):
        """Pin the mechanism: isdir() follows the link, so it reads as a maskable dir."""
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"
        target.symlink_to(victim)

        assert os.path.islink(target) is True
        assert os.path.isdir(target) is True, "isdir() follows the link -> reads as a dir"
        # Without the guard the loop would hit the isdir() branch and mask the link's
        # target, leaving the replaceable leaf name pointing at an agent-owned tree.

    def test_a_resolving_symlink_leaf_refuses(self, crew_home):
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"
        target.symlink_to(victim)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_maskable_dirs()

        assert "aws-control-staging" in str(err.value)
        assert "agent-writable" in str(err.value), "the destination is the diagnostic value"

    def test_the_symlink_leaf_is_never_removed(self, crew_home):
        """Removing it is a data-loss path; the link is the operator's to resolve."""
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"
        target.symlink_to(victim)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_maskable_dirs()

        assert target.is_symlink(), "the link is not ours to delete"

    def test_a_symlink_swapped_in_after_a_lost_create_race_refuses(self, crew_home, monkeypatch):
        # The window between our checks and ``mkdir``: a sandboxed process wins the
        # create with a symlink to a tree it owns, so mkdir raises FileExistsError and a
        # plain isdir() re-check would follow the link and mask its target. The
        # no-follow re-validation must refuse instead.
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"

        real_mkdir = os.mkdir

        def racing_mkdir(path, mode=0o777, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(str(target)):
                # Simulate the racer landing a symlink at the leaf, then report the
                # collision the kernel would have raised.
                os.symlink(str(victim), str(target))
                raise FileExistsError(errno.EEXIST, "File exists", str(path))
            return real_mkdir(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", racing_mkdir)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_maskable_dirs()

        assert "aws-control-staging" in str(err.value)
        assert target.is_symlink(), "the raced-in link is not ours to delete"

    def test_a_real_dir_winning_the_create_race_is_accepted(self, crew_home, monkeypatch):
        # The benign race: another spawn created the real directory first. mkdir raises
        # FileExistsError, the no-follow re-check finds a real dir, and the spawn proceeds.
        target = crew_home / "aws-control-staging"

        real_mkdir = os.mkdir

        def racing_mkdir(path, mode=0o777, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(str(target)):
                real_mkdir(str(target), 0o700)
                raise FileExistsError(errno.EEXIST, "File exists", str(path))
            return real_mkdir(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", racing_mkdir)

        # Must not raise: a real directory won the race.
        sandbox._materialize_maskable_dirs()
        assert target.is_dir() and not target.is_symlink()


class TestTheAuthStoreStagingLeafIsSpelledOnceInEffect:
    """The staging directory's name is written in three modules and must not drift.

    ``sandbox`` masks and precreates it, ``security.paths`` fences it, and
    ``dashboard.token_secret`` stages in it. Each spells the literal rather than
    importing, deliberately -- ``token_secret`` and ``security.paths`` stay off the
    ``sandbox`` import chain, the same trade ``service.live_target`` makes for
    ``live-target-staging``. The cost of that choice is paid here: a rename in one module
    that misses another silently leaves the temp unmasked or unfenced, which is the whole
    exposure, so the equality is asserted instead of assumed.
    """

    def test_the_three_spellings_agree(self):
        from kiro_crew.dashboard import token_secret as ts

        assert sandbox._AUTH_STORE_STAGING_LEAF == ts._AUTH_STORE_STAGING_LEAF, (
            "the sandbox masks one directory name and the publisher stages in another, "
            "so the in-flight signing key is visible in every agent namespace"
        )

    def test_the_fence_names_the_same_directory(self):
        from kiro_crew import security

        assert any(
            leaf == sandbox._AUTH_STORE_STAGING_LEAF for leaf in security.sensitive_home_dirs()
        ) or any(
            leaf.endswith("/" + sandbox._AUTH_STORE_STAGING_LEAF)
            for leaf in security.sensitive_home_dirs()
        ), (
            "the staging directory is masked but not fenced, so the agent file tools can "
            "still read a staged copy of the signing key"
        )

    def test_it_is_both_masked_and_precreated(self):
        assert sandbox._AUTH_STORE_STAGING_LEAF in sandbox._CREW_HIDDEN_LEAVES
        # Precreation is not cosmetic: a sandbox spawned before the first key write would
        # otherwise find the directory absent, the mask loop would skip it, and the
        # directory the gateway creates later would appear INSIDE that running namespace.
        assert sandbox._AUTH_STORE_STAGING_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES


@_POSIX_ONLY
class TestTheLegacyAuthStoreTempSweep:
    """Pre-upgrade signing-key temps in the data-home root are removed on spawn.

    The bound is the interesting half. The md-notebook sweep may take every ``*.tmp`` in
    its own directory because nothing else writes there; the data home root is shared, and
    ``atomic_write`` stages ``tmp<random>.tmp`` in it for unrelated stores. So the control
    test below -- an unrelated temp SURVIVES -- is what proves this sweep cannot unlink
    another component's in-flight write between its ``mkstemp`` and its rename.

    POSIX-only for a reason that is a property of the code under test, not of the test.
    The sweep lists and unlinks through a PINNED DIRECTORY DESCRIPTOR -- ``os.listdir(fd)``
    and ``os.unlink(..., dir_fd=fd)`` from :func:`_open_dir_anchored` -- which is how it
    refuses to delete through a component swapped under it. Windows offers none of that
    family, so the descriptor open fails, the sweep skips every root and removes nothing.
    That is correct rather than broken: its only callers are the Linux namespace launcher
    and the macOS Seatbelt builder, and Windows has no sandbox launcher to call it. Running
    these assertions there would measure the absence of a code path instead of its
    behaviour, and the bound control would pass while discriminating nothing.
    """

    @pytest.fixture
    def isolated_home(self, crew_home, monkeypatch, tmp_path):
        """Keep the sweep's ``Path.home()`` arm inside the scratch tree.

        The sweep deliberately visits both crew-home spellings under ``$HOME`` as well as
        the live data home, so without this the test would reach the developer's real home.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        return crew_home

    def _orphan(self, home: Path) -> Path:
        return home / f"{sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX}4242.deadbeefcafe0123.tmp"

    def test_it_removes_a_signing_key_staging_orphan(self, isolated_home):
        key = isolated_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"live" * 8)
        orphan = self._orphan(isolated_home)
        orphan.write_bytes(b"k" * 32)

        removed = sandbox._sweep_legacy_auth_store_temps()

        assert not orphan.exists(), (
            "a pre-upgrade staged copy of the signing key survived the sweep, so it stays "
            "readable in every agent namespace"
        )
        assert str(orphan) in removed
        assert key.exists(), "the live key was removed alongside the orphan"

    def test_an_orphan_is_KEPT_when_the_published_key_is_absent(self, isolated_home, caplog):
        """With no key at its own name, the orphan may be the inode's last name.

        It is also indistinguishable from a file created inside a sandbox, because an unused
        home is masked by nothing while it is absent. So the pass neither removes it -- that
        could destroy a key an operator can still recover by renaming -- nor refuses, which
        would let one ``touch`` stop every launch. It reports, and the message names both
        recoveries so the operator picks.
        """
        orphan = self._orphan(isolated_home)
        orphan.write_bytes(b"k" * 32)

        with caplog.at_level(logging.WARNING):
            removed = sandbox._sweep_legacy_auth_store_temps()

        assert orphan.exists(), (
            "the sweep removed the orphan while the key's own name was absent; that can "
            "destroy the only remaining name for the signing key's inode"
        )
        assert str(orphan) not in removed
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert orphan.name in messages, "the kept orphan was not reported at all"
        assert sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF in messages, (
            "the report must name the signing-key path, or the operator cannot tell which "
            "of the two recoveries it is offering"
        )

    def test_an_orphan_sharing_the_keys_inode_is_synced_before_removal(
        self, isolated_home, monkeypatch
    ):
        """A second name for the key inode gets the reconciler's fsync-first protocol.

        The pre-upgrade publisher kept exactly this name when its own directory sync failed,
        so the key's own entry may not have reached the device and this is the inode's one
        other reference.
        """
        key = isolated_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        orphan = self._orphan(isolated_home)
        os.link(key, orphan)

        synced: list[str] = []
        unlinked: list[str] = []
        real_unlink = os.unlink

        def _record_sync(path):
            synced.append(str(path))

        def _record_unlink(name, *args, **kwargs):
            unlinked.append(str(name))
            return real_unlink(name, *args, **kwargs)

        monkeypatch.setattr(sandbox, "fsync_dir", _record_sync)
        monkeypatch.setattr(os, "unlink", _record_unlink)

        sandbox._sweep_legacy_auth_store_temps()

        assert synced, "the key's own parent was never synced before its second name went"
        assert unlinked, "nothing was unlinked, so the ordering claim is untested"
        assert key.exists(), "the sweep removed the key's own name"

    def test_a_device_refused_sync_KEEPS_the_keys_second_name(self, isolated_home, monkeypatch):
        """Where the device refuses the sync, removing the second name can lose the inode."""
        key = isolated_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        orphan = self._orphan(isolated_home)
        os.link(key, orphan)

        def _refusing_sync(_path):
            raise OSError(errno.EIO, "device refused the directory sync")

        monkeypatch.setattr(sandbox, "fsync_dir", _refusing_sync)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._sweep_legacy_auth_store_temps()

        assert orphan.exists(), (
            "the second name was dropped even though the key's own entry could not be made "
            "durable; a crash then leaves the inode with no name at all"
        )
        assert "sync" in str(caught.value)

    def test_it_leaves_an_unrelated_atomic_write_temp_alone(self, isolated_home):
        # atomic_write's own shape for ANY other store in this shared directory.
        bystander = isolated_home / "tmpa1b2c3d4.tmp"
        bystander.write_bytes(b"another store's in-flight write")

        removed = sandbox._sweep_legacy_auth_store_temps()

        assert bystander.exists(), (
            "the sweep unlinked a temp belonging to another store; in the shared data "
            "home that aborts an unrelated in-flight write for no reason"
        )
        assert removed == []

    def test_it_leaves_the_published_key_alone(self, isolated_home):
        key = isolated_home / "token_signing.key"
        key.write_bytes(b"k" * 32)

        sandbox._sweep_legacy_auth_store_temps()

        assert key.exists(), "the sweep removed the live signing key"

    @_POSIX_ONLY
    def test_a_symlink_matching_the_shape_is_not_followed(self, isolated_home, tmp_path):
        target = tmp_path / "outside-the-home"
        target.write_bytes(b"not ours")
        link = self._orphan(isolated_home)
        link.symlink_to(target)

        removed = sandbox._sweep_legacy_auth_store_temps()

        assert target.exists(), "the sweep followed a link and deleted outside the home"
        assert removed == []
        assert link.is_symlink(), "the link itself was removed, which lstat should prevent"

    def test_the_prefix_matches_the_name_the_publisher_really_stages(self, tmp_path, monkeypatch):
        """Pin the sweep's pattern against the publisher's actual output, not a literal.

        A rename on either side otherwise leaves the sweep matching a shape nothing
        produces, which reads exactly like a clean home.
        """
        from kiro_crew.dashboard import token_secret as ts

        captured: list[str] = []
        real_link = os.link

        def _spy_link(src_path, dst_path, **kwargs):
            captured.append(os.path.basename(str(src_path)))
            return real_link(src_path, dst_path, **kwargs)

        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        monkeypatch.setattr(os, "link", _spy_link)
        ts._load_or_create_secret()

        assert captured, "the publish never linked, so no staged name was observed"
        staged_name = captured[0]
        assert staged_name.startswith(sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX), (
            f"the publisher stages {staged_name!r}, which the sweep's prefix "
            f"{sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX!r} does not match, so a pre-upgrade "
            "orphan of that shape would never be found"
        )
        assert staged_name.endswith(".tmp")

    def test_both_launch_paths_sweep(self):
        """Both launchers must call it: a Seatbelt profile names paths, never a temp shape.

        Asserted against the real sources, so a launcher added or reordered later cannot
        quietly drop the sweep on one platform.
        """
        for fn in (sandbox.namespace_argv, sandbox.sandbox_exec_argv):
            src = inspect.getsource(fn)
            assert "_sweep_legacy_auth_store_temps()" in src, (
                f"{fn.__name__} does not sweep legacy auth-store temps, so an orphan "
                "holding the signing key stays readable on that platform"
            )
