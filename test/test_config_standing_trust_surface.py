"""Which gate holds the STANDING approval posture, and which deliberately does not.

The posture (`agent.dangerously_skip_permissions`) is what a startup reads to install
a grant with no expiry, so a process that can write the document holding it can turn a
session-scoped elevation into the default for every later session. This module pins
where that authority lives, one assertion per control, so the coverage is a measured
fact rather than a reading of the tier lists.

The authority is the keystone `standing_approval.json`, which is MASKED in the agent
sandbox: the name cannot be opened, so an agent can neither read the grant nor obtain
a `link(2)` source for it. `config.json` is deliberately NOT sealed, because a
read-only bind on it breaks the in-sandbox `kirocrew config set` verb the agent is
documented to run, and because writing that document decides no authorization.
"""

from __future__ import annotations

import pytest

from kiro_crew import sandbox
from kiro_crew.security import (
    is_sensitive_bash_command,
    is_sensitive_path,
    is_sensitive_write_path,
)

#: Both crew data-home spellings carry the same runtime config leaves.
CONFIG_LEAVES = ("config.json", "config.local.json")

HOME_PREFIX = "~/.kiro/crew"


class TestFileEditToolGate:
    """The agent's file-edit tool is refused on the config documents, and the
    predicate discriminates."""

    def test_write_tier_covers_every_config_leaf(self) -> None:
        for leaf in CONFIG_LEAVES:
            assert is_sensitive_write_path(f"{HOME_PREFIX}/{leaf}") is True, leaf

    def test_write_tier_answers_false_for_an_ordinary_file(self) -> None:
        # Control: the tier is discriminating, so True above is a classification
        # rather than a predicate that accepts anything.
        assert is_sensitive_write_path(f"{HOME_PREFIX}/ordinary-note.txt") is False


class TestNoPathPredicateReachesTheShell:
    """Neither of the two controls that could refuse a SHELL write reaches these
    leaves, which is why the authority had to move rather than be fenced here."""

    def test_read_write_floor_does_not_cover_the_config_leaves(self) -> None:
        # Off the floor on purpose: reading config in-sandbox is routine.
        for leaf in CONFIG_LEAVES:
            assert is_sensitive_path(f"{HOME_PREFIX}/{leaf}") is False, leaf

    def test_shell_form_gate_does_not_refuse_a_redirect_onto_the_config(self) -> None:
        # The bash gate matches no paths at all, by design.
        cmd = f"printf '{{}}' > {HOME_PREFIX}/config.json"
        assert is_sensitive_bash_command(cmd) is None


class TestTheConfigDocumentsStayWritableInSandbox:
    """The config leaves are deliberately NOT sealed read-only, and that is a decision.

    A read-only bind would break `kirocrew config set`, `kirocrew agent create` and
    `kirocrew app enable` when an agent runs them from its own sandboxed shell, which
    the bundled command skill documents it doing: `atomic_write`'s temp-and-rename onto
    a mountpoint fails. Sealing also buys nothing now, because writing this document no
    longer installs a grant. These assertions exist so the seal cannot be reinstated
    without meeting that argument.
    """

    def test_neither_config_leaf_is_sealed_read_only(self) -> None:
        sealed = set(sandbox._CREW_READONLY_LEAVES)
        for leaf in CONFIG_LEAVES:
            assert leaf not in sealed, leaf

    def test_neither_config_leaf_is_pre_created(self) -> None:
        # Pre-creating an empty document also sets the loader's `loaded_base`, which
        # bypasses its never-setup return and drives a doomed write-back on every
        # in-sandbox load.
        precreated = set(sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES)
        for leaf in CONFIG_LEAVES:
            assert leaf not in precreated, leaf

    def test_neither_config_leaf_is_masked(self) -> None:
        # Masking them would break the in-sandbox readers that resolve the subagent
        # cap, the quarantine threshold and the browser preference per call.
        hidden = set(sandbox._CREW_HIDDEN_LEAVES)
        for leaf in CONFIG_LEAVES:
            assert leaf not in hidden, leaf

    def test_a_governance_ceiling_is_still_sealed(self) -> None:
        # Control: the sealed set is populated, so the assertions above are about
        # these leaves rather than about an empty or misspelled collection.
        assert "computer_use.json" in set(sandbox._CREW_READONLY_LEAVES)


class TestTheStandingGrantLivesOnAnUnopenableLeaf:
    """Where the authority is now, and why that placement is the one that closes it.

    A read-only bind covers a PATH, not the inode behind it, and the crew data-home root
    is writable in every sandbox, so a sealed document stays writable through a second
    name a sandboxed process can create itself. A mask covers the name for every present
    and future spelling, so the document cannot be opened at all.

    Both directions are asserted, because either alone would pass a broken change: a leaf
    nobody can write is useless if the operator cannot grant, and a leaf the operator can
    grant on is useless if the agent can write it too.
    """

    LEAF = "standing_approval.json"
    STAGING = "standing-approval-staging"

    def test_the_leaf_is_masked_not_merely_sealed(self) -> None:
        assert self.LEAF in set(sandbox._CREW_HIDDEN_LEAVES)
        assert self.LEAF not in set(sandbox._CREW_READONLY_LEAVES)

    def test_the_leaf_is_on_the_read_and_write_floor(self) -> None:
        # The tool path, which is the tier a mask does not cover.
        assert is_sensitive_path(f"{HOME_PREFIX}/{self.LEAF}") is True
        assert is_sensitive_write_path(f"{HOME_PREFIX}/{self.LEAF}") is True

    def test_the_staging_directory_is_masked_and_pre_created(self) -> None:
        # The temp staged there BECOMES the grant document, so a visible temp name would
        # be a writable second path to it.
        assert self.STAGING in set(sandbox._CREW_HIDDEN_LEAVES)
        assert self.STAGING in set(sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES)
        assert is_sensitive_path(f"{HOME_PREFIX}/{self.STAGING}") is True

    def test_one_publisher_serves_every_masked_file_leaf(self) -> None:
        # The publish sequence exists once: two copies would diverge.
        assert callable(sandbox._materialize_masked_file_leaf)
        assert callable(sandbox._materialize_standing_approval_mask_target)
        assert callable(sandbox._materialize_live_target_mask_target)

    def test_the_publisher_refuses_a_pre_existing_hardlink(self, tmp_path, monkeypatch) -> None:
        # The WIRING, not just the helper: a second name is a writable path to the very
        # document a startup reads a grant from, so tolerating it would leave the
        # authorization writable through an alias the mask does not cover.
        import os

        crew = tmp_path / "crew"
        crew.mkdir()
        target = crew / self.LEAF
        target.write_text('{"enabled": false}', encoding="utf-8")
        os.link(target, crew / "backup-copy.json")
        monkeypatch.setattr(sandbox, "config_dir", lambda: crew)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_standing_approval_mask_target()

    def test_the_doctor_probe_reports_that_shape_first(self, tmp_path, monkeypatch) -> None:
        # The refusal costs every spawn on the host, so it is paid with a notice rather
        # than a surprise: the probe answers the same sentence before a spawn meets it.
        import os

        crew = tmp_path / "crew"
        crew.mkdir()
        target = crew / self.LEAF
        target.write_text('{"enabled": false}', encoding="utf-8")
        os.link(target, crew / "backup-copy.json")
        monkeypatch.setattr(sandbox, "config_dir", lambda: crew)

        unfit = sandbox.standing_approval_unfitness()

        assert unfit is not None
        assert unfit.path == str(target)
        assert "standing auto-approve grant" in unfit.detail

    def test_the_doctor_probe_answers_fit_for_a_lone_file(self, tmp_path, monkeypatch) -> None:
        # Control, both directions: absent and lone-regular are FIT, so a report is a
        # classification rather than a probe that always complains.
        crew = tmp_path / "crew"
        crew.mkdir()
        monkeypatch.setattr(sandbox, "config_dir", lambda: crew)
        assert sandbox.standing_approval_unfitness() is None

        (crew / self.LEAF).write_text('{"enabled": false}', encoding="utf-8")
        assert sandbox.standing_approval_unfitness() is None

    def test_the_publisher_refuses_a_symlinked_leaf(self, tmp_path, monkeypatch) -> None:
        # A symlink makes the mask bind the wrong file entirely.
        crew = tmp_path / "crew"
        crew.mkdir()
        elsewhere = tmp_path / "attacker.json"
        elsewhere.write_text("{}", encoding="utf-8")
        (crew / self.LEAF).symlink_to(elsewhere)
        monkeypatch.setattr(sandbox, "config_dir", lambda: crew)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_standing_approval_mask_target()

    def test_the_operator_read_path_answers_a_real_grant(self, tmp_path, monkeypatch) -> None:
        import json

        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_text(
            json.dumps({"enabled": True, "mac": so_mod.standing_grant_mac(True)}),
            encoding="utf-8",
        )
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is True

    def test_a_document_with_no_provenance_answers_no_grant(self, tmp_path, monkeypatch) -> None:
        # The pre-upgrade window this closes: on a release that does not know this leaf the
        # name is ordinary and writable, so a sandboxed agent could plant `enabled: true`
        # and wait for the upgrade to adopt it. It cannot mint the MAC, because the key is
        # the host secret that predates this change and is already unreadable in-sandbox.
        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_text('{"enabled": true}', encoding="utf-8")
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is False

    def test_a_document_with_a_forged_provenance_answers_no_grant(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_text('{"enabled": true, "mac": "deadbeef"}', encoding="utf-8")
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is False

    def test_a_non_ascii_provenance_answers_no_grant_instead_of_raising(
        self, tmp_path, monkeypatch
    ) -> None:
        # ``hmac.compare_digest`` refuses two ``str`` arguments when either holds a
        # non-ASCII character and raises TypeError rather than answering False. This
        # reader's contract is to fail soft to NO GRANT in every direction, and both
        # callers reach it through an unguarded ``to_thread``, so a raise here would abort
        # gateway startup on a document the MAC check exists to refuse.
        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_text('{"enabled": true, "mac": "caf\u00e9"}', encoding="utf-8")
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is False

    def test_a_lone_surrogate_provenance_answers_no_grant_instead_of_raising(
        self, tmp_path, monkeypatch
    ) -> None:
        # ``json.loads`` accepts a lone surrogate, which plain utf-8 encoding then refuses;
        # ``surrogatepass`` is why this decodes to a comparison rather than an exception.
        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_text('{"enabled": true, "mac": "\\ud800"}', encoding="utf-8")
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is False

    def test_a_mac_minted_for_the_off_posture_does_not_grant(self, tmp_path, monkeypatch) -> None:
        # The MAC covers the DECISION, so one minted for `false` cannot be carried onto a
        # document claiming `true`.
        import json

        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_text(
            json.dumps({"enabled": True, "mac": so_mod.standing_grant_mac(False)}),
            encoding="utf-8",
        )
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is False

    def test_the_writer_audits_before_it_grants(self, tmp_path, monkeypatch) -> None:
        # A record that appears only after the authorization exists cannot be the thing
        # that authorized it, so the audit runs FIRST and a failing sink writes no grant.
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew import safety_override as so_mod
        from kiro_crew.config import loader as loader_mod

        target = tmp_path / self.LEAF
        monkeypatch.setattr(loader_mod, "standing_approval_path", lambda: target)
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)
        monkeypatch.setattr(cli_commands, "_standing_grant_key_is_persisted", lambda: True)

        seen: list[dict] = []

        class _Sink:
            def log_api_access(self, **kw) -> None:
                seen.append(kw)
                raise RuntimeError("audit sink down")

        monkeypatch.setattr(cli_commands, "sel", lambda: _Sink())
        cli_commands._standing_approval(argparse.Namespace(enable=True, disable=False))

        assert not target.exists()
        assert so_mod.standing_grant_declared() is False
        # `critical=True` is what makes the refusal real: the ordinary path ENQUEUES and
        # swallows an append failure in the writer thread, so a non-critical call would let
        # the grant publish with no record of the decision.
        assert seen and seen[-1].get("critical") is True

    def test_the_writer_refuses_an_ephemeral_signing_key(self, tmp_path, monkeypatch) -> None:
        # A MAC minted under an ephemeral secret does not verify in the NEXT process, so
        # the grant would be accepted at write time and silently refused at every startup.
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew import safety_override as so_mod
        from kiro_crew.config import loader as loader_mod

        target = tmp_path / self.LEAF
        monkeypatch.setattr(loader_mod, "standing_approval_path", lambda: target)
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)
        monkeypatch.setattr(cli_commands, "_standing_grant_key_is_persisted", lambda: False)
        monkeypatch.setattr(cli_commands, "standing_approval_path", lambda: target)

        cli_commands._standing_approval(argparse.Namespace(enable=True, disable=False))

        assert not target.exists()
        assert so_mod.standing_grant_declared() is False

    def test_withdrawal_refuses_a_malformed_grant_path(self, tmp_path, monkeypatch) -> None:
        # A directory at this name is not a grant, and a traceback is not a refusal: the
        # verb says what is in the way and leaves it alone.
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew.config import loader as loader_mod

        target = tmp_path / self.LEAF
        target.mkdir()
        monkeypatch.setattr(loader_mod, "standing_approval_path", lambda: target)
        monkeypatch.setattr(cli_commands, "standing_approval_path", lambda: target)

        cli_commands._standing_approval(argparse.Namespace(enable=False, disable=True))

        assert target.is_dir()

    def test_the_slack_startup_reads_the_grant_off_the_loop(self) -> None:
        # Reading the keystone touches the filesystem and resolves the host signing secret,
        # which on a cold or contended key creates the file under a lock. On the loop that
        # stalls every gateway task, so both calls go through a worker thread -- the same
        # treatment the grant call beside them already has.
        import inspect

        from kiro_crew.slack import events as ev

        src = inspect.getsource(ev.init_socket_mode)
        assert "to_thread(standing_grant_declared)" in src
        assert "to_thread(warn_if_config_declares_standing_grant" in src

    def test_the_writer_verb_round_trips_through_the_reader(self, tmp_path, monkeypatch) -> None:
        # The only writer, and the pin that it produces what the reader accepts: an operator
        # cannot hand-compute the MAC, so a broken verb would leave the grant unreachable.
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew import safety_override as so_mod
        from kiro_crew.config import loader as loader_mod

        target = tmp_path / self.LEAF
        monkeypatch.setattr(loader_mod, "standing_approval_path", lambda: target)
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)
        # The key check has its own pin; the host secret is memoized process-wide, so
        # leaving it live here would make this test depend on which home loaded it first.
        monkeypatch.setattr(cli_commands, "_standing_grant_key_is_persisted", lambda: True)
        # The verb holds its own module-scope reference, so the reader's copy is not the one
        # it resolves. Production is unaffected: the bound function still reads
        # ``KIROCREW_HOME`` on every call, and nothing reassigns it outside a test.
        monkeypatch.setattr(cli_commands, "standing_approval_path", lambda: target)

        cli_commands._standing_approval(argparse.Namespace(enable=True, disable=False))
        assert so_mod.standing_grant_declared() is True

        cli_commands._standing_approval(argparse.Namespace(enable=False, disable=True))
        assert so_mod.standing_grant_declared() is False

    @pytest.mark.parametrize(
        "body",
        (
            '{"enabled": false}',
            "{}",
            '{"enabled": "true"}',
            '{"enabled": 1}',
            "[]",
            "not json at all",
            "",
        ),
    )
    def test_every_other_document_answers_no_grant(self, tmp_path, monkeypatch, body) -> None:
        # Fails soft in every direction, and `true` is required exactly so a truthy string
        # or a non-zero number cannot become an authorization by accident.
        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_text(body, encoding="utf-8")
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is False

    def test_an_absent_document_answers_no_grant(self, tmp_path, monkeypatch) -> None:
        from kiro_crew import safety_override as so_mod

        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: tmp_path / self.LEAF)

        assert so_mod.standing_grant_declared() is False

    def test_the_precreated_stub_is_absent_equivalent(self, tmp_path, monkeypatch) -> None:
        # The argument the pre-create list asks each masked file leaf to supply: what the
        # sandbox is pinned at must read the same as no file at all.
        from kiro_crew import safety_override as so_mod

        target = tmp_path / self.LEAF
        target.write_bytes(sandbox._STANDING_APPROVAL_PRECREATE_CONTENT)
        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: target)

        assert so_mod.standing_grant_declared() is False

    def test_the_config_key_alone_no_longer_grants(self, tmp_path, monkeypatch) -> None:
        # The migration's teeth: an operator who has only the old key gets no grant, and
        # the warning names the file to write.
        from kiro_crew import safety_override as so_mod

        monkeypatch.setattr(so_mod, "standing_approval_path", lambda: tmp_path / self.LEAF)

        assert so_mod.standing_grant_declared() is False
        so_mod.warn_if_config_declares_standing_grant(True)


class TestTheAliasShapesAreAnsweredPerLeaf:
    """Both shapes refuse, and each with prose naming its own document.

    A symlink makes the mask bind the wrong file entirely. A second hardlink leaves a
    writable path to the right one, which for an authorization document is the same harm,
    so it refuses too -- and the cost (every sandboxed spawn on a host carrying a snapshot
    link) is paid with a `kirocrew doctor` notice rather than a surprise.
    """

    def test_the_symlink_refusal_names_this_leaf(self, tmp_path) -> None:
        elsewhere = tmp_path / "attacker.json"
        elsewhere.write_text("{}", encoding="utf-8")
        target = tmp_path / "standing_approval.json"
        target.symlink_to(elsewhere)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._refuse_if_standing_approval_symlink(str(target))

        assert "standing auto-approve grant" in str(exc.value)
        assert "live-target" not in str(exc.value)

    def test_the_multilink_refusal_names_this_leaf(self, tmp_path) -> None:
        import os

        target = tmp_path / "standing_approval.json"
        target.write_text("{}", encoding="utf-8")
        os.link(target, tmp_path / "second-name.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._refuse_unless_sole_regular_link(
                str(target),
                irregular_detail=sandbox._standing_approval_irregular_detail,
                multilink_detail=sandbox._standing_approval_multilink_detail,
            )

        assert "standing auto-approve grant" in str(exc.value)
        assert "live-target" not in str(exc.value)

    def test_the_helper_still_carries_the_pointer_prose_by_default(self, tmp_path) -> None:
        # Control: the per-leaf prose is the caller's choice, not the helper changing
        # what it says for everyone.
        import os

        target = tmp_path / "live_target.json"
        target.write_text("{}", encoding="utf-8")
        os.link(target, tmp_path / "second-name.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._refuse_unless_sole_regular_link(str(target))

        assert "live-target pointer" in str(exc.value)
