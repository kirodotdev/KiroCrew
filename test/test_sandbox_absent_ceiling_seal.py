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
    """Point ``config_dir()`` — the live data home — at a scratch tree."""
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
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

    def test_an_extra_hardlink_is_tolerated(self, crew_home, tmp_path, caplog):
        """``rsync --link-dest`` and snapshot tools leave one on a healthy host.

        A hardlink does not make the masked NAME replaceable, which is why the shape is
        tolerated for every leaf rather than per leaf. It is WARNED, though: nothing else
        warns over these leaves, so staying silent would leave the alias outside the mask
        with nothing said about it.
        """
        target = crew_home / "token_signing.key"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"key")
        alias = tmp_path / "backup-hardlink"
        os.link(target, alias)
        assert target.stat().st_nlink == 2

        with caplog.at_level("WARNING"):
            sandbox._refuse_aliased_masked_leaves()  # does not raise
        assert any(
            "hardlinks" in r.getMessage() and "token_signing.key" in r.getMessage()
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
