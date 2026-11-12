"""``kirocrew doctor``'s sign-in rows and its KAS section.

One sign-in row per selectable harness, the KAS section on the bundle route
and on kiro-cli, and the KAS block's withheld-permissions and tool-vocabulary
reports.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew import cli_doctor


class TestDoctorAgentAuth:
    """One sign-in row per selectable harness, from its declaration."""

    def _run(
        self,
        monkeypatch,
        capsys,
        backends,
        signed_in,
        *,
        vault_holds=False,
        vault_detail=None,
        vault_import_fails=False,
        kiro_cli="/opt/kiro/bin/kiro-cli",
    ):
        from kiro_crew import acp_backends

        probes: list[int] = []

        def probe():
            probes.append(1)
            return signed_in

        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: backends)
        monkeypatch.setattr(cli_doctor, "_kiro_cli_signed_in", probe)
        # Patched so no row depends on whether this host has kiro-cli installed.
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: kiro_cli)
        # The vault is always patched so no test reads the developer's real
        # sign-in vault -- the host-auth-callback row probes it via a deferred
        # import, and an unpatched probe would make these tests host-dependent.
        if vault_import_fails:
            monkeypatch.setitem(sys.modules, "kiro_crew.auth.bridge", None)
        else:
            monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: vault_holds)
            monkeypatch.setattr(
                "kiro_crew.auth.bridge.describe_vault_identity", lambda: vault_detail
            )
        cli_doctor._doctor_agent_auth()
        return capsys.readouterr().out, len(probes)

    def test_a_separate_sign_in_harness_is_not_probed(self, monkeypatch, capsys) -> None:
        """Reading another harness's token is what the credential floor forbids, so
        the row names the store and prints the declared ACTION, unprobed, and says
        so -- the marker tells the operator this is absence of evidence, not a
        verdict."""
        from kiro_crew.agent_sdk import host_auth

        out, probes = self._run(monkeypatch, capsys, ["codex"], signed_in=True)
        assert probes == 0
        assert host_auth.entitlement_label("codex") in out
        assert "not checked here" in out
        # Wrapped, not reworded: every word of the remedy reaches the row.
        for word in host_auth.declaration_for("codex").sign_in_remedy.split():
            assert word in out, word
        assert host_auth.ENTITLEMENT_OWN_CREDENTIAL_FILE not in out

    def test_host_store_harnesses_share_one_probe(self, monkeypatch, capsys) -> None:
        """kiro and KAS both resolve tokens from the host store, so the row probes it
        once, and a signed-out answer prints the signed-out STATEMENT -- the one row
        with evidence behind it."""
        from kiro_crew.agent_sdk import host_auth

        out, probes = self._run(monkeypatch, capsys, ["", "kas"], signed_in=False)
        assert probes == 1
        assert out.count("not signed in") == 2
        for word in host_auth.signed_out_message("").split():
            assert word in out, word

    def test_an_unknown_probe_is_not_reported_as_signed_out(self, monkeypatch, capsys) -> None:
        """A spawn that failed says nothing about the store; reporting it as signed
        out would send an operator to re-run a login they already completed."""
        out, probes = self._run(monkeypatch, capsys, [""], signed_in=None)
        assert probes == 1
        assert "could not check" in out
        assert "not signed in" not in out

    def test_no_kiro_cli_is_not_a_failed_check(self, monkeypatch, capsys) -> None:
        """With no kiro-cli there is nothing to ask, so the kiro row says so without a
        warning or a sign-in remedy: the kiro-cli row above already reports the install."""
        out, _probes = self._run(monkeypatch, capsys, [""], signed_in=None, kiro_cli=None)
        assert "kiro auth:   ⏭  kiro-cli's own sign-in; kiro-cli not installed" in out
        assert "could not check" not in out
        assert "⚠" not in out
        assert "kiro-cli login" not in out

    def test_a_vault_owned_kas_row_names_the_vault(self, monkeypatch, capsys) -> None:
        """When Crew's vault holds a usable identity the KAS spawn is vault-owned,
        so the row names the vault and carries the vault's own detail line -- and
        the verdict comes from the vault probe, not from the shared kiro-cli
        probe, which the kiro row still consumes exactly once for itself."""
        detail = "social/Google, expires in 42m, refresh token present -> usable"
        out, probes = self._run(
            monkeypatch,
            capsys,
            ["", "kas"],
            signed_in=True,
            vault_holds=True,
            vault_detail=detail,
        )
        assert probes == 1
        assert "✅ Kiro Crew vault (signed in through Kiro Crew)" in out
        assert detail in out
        # The kiro row is untouched: it still reports the host store's own state.
        assert "✅ kiro-cli's own sign-in" in out

    def test_a_vault_owned_row_alone_consumes_no_kiro_cli_probe(self, monkeypatch, capsys) -> None:
        """The vault verdict is the row's whole answer, so with no other host-store
        row on the board the kiro-cli probe never runs at all -- and a store that
        was never measured must not be claimed present, so the secondary-detail
        line stays absent too. With no detail line to affirm health the glyph is
        the row's "could not check" marker, never a green asserted from silence."""
        out, probes = self._run(monkeypatch, capsys, ["kas"], signed_in=True, vault_holds=True)
        assert probes == 0
        assert "⚠️  Kiro Crew vault (signed in through Kiro Crew)" in out
        assert "✅ Kiro Crew vault" not in out
        # The guard on the secondary line is load-bearing: nothing probed
        # kiro-cli's store here, so nothing may be asserted about it.
        assert "also present" not in out

    def test_a_rejected_refresh_vault_owner_is_not_a_green_row(self, monkeypatch, capsys) -> None:
        """The vault still OWNS the spawn when the issuer has rejected its refresh
        token (``is_usable`` cannot know that without a network call), but the
        glyph column is what an operator scans -- a ✅ above a detail line whose
        verdict says the sign-in is expired would bury the remedy. Ownership
        keeps the vault text; health downgrades the glyph."""
        detail = (
            "social/Google, expires in 42m, refresh token present, refresh REJECTED "
            "by issuer at 2026-09-18T01:00 -> sign-in expired -- sign in again from "
            "the dashboard or sign out"
        )
        out, probes = self._run(
            monkeypatch,
            capsys,
            ["kas"],
            signed_in=True,
            vault_holds=True,
            vault_detail=detail,
        )
        assert probes == 0
        assert "⚠️  Kiro Crew vault (signed in through Kiro Crew)" in out
        assert "✅ Kiro Crew vault" not in out
        # Wrapped, not reworded: the remedy reaches the row.
        for word in detail.split():
            assert word in out, word

    def test_both_stores_holding_reports_the_second_store_too(self, monkeypatch, capsys) -> None:
        """The two stores can hold DIFFERENT accounts. The vault owns the spawn,
        but a row that silently dropped kiro-cli's own sign-in would trade one
        wrong report for another -- so it is reported as secondary detail, and
        never adjudicated."""
        out, probes = self._run(
            monkeypatch,
            capsys,
            ["", "kas"],
            signed_in=True,
            vault_holds=True,
            vault_detail="social/Google, expires in 42m, refresh token present -> usable",
        )
        assert probes == 1
        assert "also present and may be a different account" in out
        assert "the relay uses the vault" in out

    def test_vault_not_holding_falls_back_to_the_kiro_cli_row(self, monkeypatch, capsys) -> None:
        """An empty vault leaves the row exactly as it was: kiro-cli's store is
        the runtime's fallback owner, probed once."""
        out, probes = self._run(monkeypatch, capsys, ["kas"], signed_in=True, vault_holds=False)
        assert probes == 1
        assert "✅ kiro-cli's own sign-in" in out
        assert "Kiro Crew vault" not in out

    def test_a_stored_but_unusable_vault_identity_is_still_reported(
        self, monkeypatch, capsys
    ) -> None:
        """A lapsed vault identity does not own the spawn, but it is exactly why a
        spawn is failing for an operator who signed in through Crew -- so the
        detail line (with its remedy) prints beneath the fallback row rather than
        vanishing with the ownership."""
        detail = (
            "social/Google, access token expired, no refresh token "
            "-> NOT usable -- sign in again or sign out"
        )
        out, probes = self._run(
            monkeypatch,
            capsys,
            ["kas"],
            signed_in=True,
            vault_holds=False,
            vault_detail=detail,
        )
        assert probes == 1
        assert "✅ kiro-cli's own sign-in" in out
        # Wrapped, not reworded: every word of the detail reaches the row.
        for word in detail.split():
            assert word in out, word

    def test_a_vault_import_failure_degrades_to_the_kiro_cli_path(
        self, monkeypatch, capsys
    ) -> None:
        """kiro_crew.auth brings the cryptography wheel with it; a broken install
        must degrade this advisory row to the kiro-cli path, never lose it."""
        out, probes = self._run(
            monkeypatch, capsys, ["kas"], signed_in=True, vault_import_fails=True
        )
        assert probes == 1
        assert "✅ kiro-cli's own sign-in" in out
        assert "Kiro Crew vault" not in out


class TestDoctorKasBundle:
    """The KAS section on the bundle route: Crew's own engine, no kiro-cli row.

    kiro-cli is never consulted here because the route never runs it. A missing
    kiro-cli on this route is not a problem, and reporting one would send the
    operator to install a binary nothing will use.
    """

    class _Cfg:
        def __init__(self) -> None:
            self.agent = type("A", (), {"acp_backend": "kas", "kas_bundle": True})()

    @pytest.fixture(autouse=True)
    def _cfg(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: self._Cfg())
        )
        # No kiro-cli on the host, the route's point. Only the auto-approve row
        # looks one up, through the writer's predicate (an installed kiro-cli
        # reads the same specs); the doctor's own resolver must not be asked.
        _floor_monkeypatch.setattr("kiro_crew.kiro_cli.resolve_kiro_cli", lambda: None)
        _floor_monkeypatch.setattr(
            cli_doctor,
            "resolve_kiro_cli",
            lambda: pytest.fail("kiro-cli resolved by the doctor on the bundle route"),
        )
        _floor_monkeypatch.setattr(cli_doctor, "_report_kas_spec_permissions", lambda issues: None)
        _floor_monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: False)
        _floor_monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: None)
        # The not-installed rows ask whether this host can run the engine at all;
        # an unreadable glibc answers yes, so the runner's own libc never decides.
        _floor_monkeypatch.setattr("kiro_crew.constants._host_glibc_version", lambda: None)

    def test_a_host_below_the_glibc_floor_is_told_so_not_to_install(self, monkeypatch, capsys):
        """Amazon Linux 2 (glibc 2.26) with kiro-cli signed out: the bundle route is
        automatic and the engine cannot run here, so the row names glibc instead of
        a first-chat download or an install that is refused every time."""
        from kiro_crew.acp import kas_route
        from kiro_crew.agent_sdk import kas_bundle

        monkeypatch.setattr(
            kas_bundle,
            "kas_route",
            lambda: kas_route.KasRoute(bundle=True, reason=kas_route.REASON_KIRO_CLI_SIGNED_OUT),
        )
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        monkeypatch.setattr("kiro_crew.constants._host_glibc_version", lambda: (2, 26))
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "bundle:      ❌" in out
        assert "glibc (2.26)" in out
        assert "first chat" not in out
        assert "kirocrew kas install" not in out
        assert len(issues) == 1
        assert "glibc (2.26)" in issues[0]

    def test_a_host_with_no_engine_build_is_told_so_not_to_install(self, monkeypatch, capsys):
        """A Linux host on an architecture the engine publishes no build for: every
        install refuses it, so the row names the architecture, as the install does."""
        from kiro_crew.acp import kas_route
        from kiro_crew.agent_sdk import kas_bundle

        monkeypatch.setattr(
            kas_bundle,
            "kas_route",
            lambda: kas_route.KasRoute(bundle=True, reason=kas_route.REASON_KIRO_CLI_SIGNED_OUT),
        )
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "riscv64")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "bundle:      ❌" in out
        assert "architecture (riscv64)" in out
        assert "first chat" not in out
        assert "kirocrew kas install" not in out
        assert len(issues) == 1
        assert "architecture (riscv64)" in issues[0]

    def test_nothing_installed_is_an_issue_naming_the_install(self, monkeypatch, capsys):
        """Not installed yet is a warning: the first chat downloads the engine, and
        the row names the command that does it now."""
        from kiro_crew.agent_sdk import kas_bundle

        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "route:       bundle" in out
        assert "bundle:      ⚠  not installed yet" in out
        assert "downloads it at the first chat" in out
        assert "kirocrew kas install" in out
        assert issues == ["agent.kas_bundle is on but no KAS engine bundle is installed"]
        assert "relay:" not in out

    def test_nothing_installed_reports_the_block_withheld_without_a_second_issue(
        self, monkeypatch, capsys
    ):
        """No engine is an unknown version to the writer's gate, so the block is
        withheld; the row says so, and the bundle row already names the fix."""
        from kiro_crew.agent_sdk import kas_bundle

        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert (
            "auto-approve: ⚠  spec `permissions` block withheld: no engine bundle installed" in out
        )
        assert "kiro-cli" not in out
        assert len(issues) == 1

    @staticmethod
    def _automatic_route_without_the_pin(monkeypatch, on_disk, *, installable):
        from kiro_crew.acp import kas_route
        from kiro_crew.agent_sdk import backend_install, kas_bundle

        automatic = kas_route.KasRoute(bundle=True, reason=kas_route.REASON_NO_KIRO_CLI)
        monkeypatch.setattr(kas_bundle, "kas_route", lambda: automatic)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda: on_disk)
        monkeypatch.setattr(kas_bundle, "pinned_version", lambda: "0.66.40")
        monkeypatch.setattr(backend_install, "_engine_installable_here", lambda: installable)

    @pytest.mark.parametrize("on_disk", [[], ["0.66.33"]])
    def test_a_pin_the_gateway_downloads_on_the_automatic_route_is_not_an_issue(
        self, monkeypatch, capsys, on_disk
    ):
        """A fresh install with no kiro-cli starts here, and the first chat downloads
        the engine: the row still warns, but the summary is not red."""
        self._automatic_route_without_the_pin(monkeypatch, on_disk, installable=True)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "bundle:      ⚠  " in out
        assert "downloads it at the first chat" in out
        assert issues == []

    @pytest.mark.parametrize("on_disk", [[], ["0.66.33"]])
    def test_a_pin_this_host_cannot_download_stays_an_issue(self, monkeypatch, capsys, on_disk):
        """A host the engine cannot run on, or a version override the gateway does
        not install unattended, fails at the first chat, so the summary says so."""
        self._automatic_route_without_the_pin(monkeypatch, on_disk, installable=False)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "The gateway will not install it on this host" in out
        assert "downloads it at the first chat" not in out
        assert len(issues) == 1
        assert "the gateway will not install it on this host" in issues[0]

    def test_another_version_on_disk_names_what_is_installed_and_what_is_needed(
        self, monkeypatch, capsys
    ):
        """A release whose pin moved finds the old engine, not nothing: say both versions.

        A warning like nothing-installed, because the first chat installs the pin."""
        from kiro_crew.agent_sdk import kas_bundle

        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda: ["0.66.33"])
        monkeypatch.setattr(kas_bundle, "pinned_version", lambda: "0.66.40")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "bundle:      ⚠  KAS 0.66.33 installed, but this release needs KAS 0.66.40" in out
        assert "downloads it at the first chat" in out
        assert "not installed yet under" not in out
        assert "kirocrew kas install" in out
        assert issues == [
            "agent.kas_bundle is on but the KAS engine this release needs (0.66.40) "
            "is not installed; on disk: 0.66.33"
        ]

    def test_an_incomplete_pinned_directory_names_the_force_install(self, monkeypatch, capsys):
        """The pin's directory is there but does not read back: not "X installed, needs X"."""
        from kiro_crew.agent_sdk import kas_bundle

        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda: ["0.66.40"])
        monkeypatch.setattr(kas_bundle, "pinned_version", lambda: "0.66.40")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "bundle:      ❌ KAS 0.66.40 is on disk but incomplete" in out
        assert "kirocrew kas install --force" in out
        assert "but this release needs" not in out
        assert "downloads it at the first chat" not in out
        assert issues == ["the KAS engine directory for 0.66.40 is incomplete"]

    def test_an_unreadable_engine_directory_is_an_issue_not_a_crash(self, monkeypatch, capsys):
        from kiro_crew.agent_sdk import kas_bundle

        def unreadable():
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: None)
        monkeypatch.setattr(kas_bundle, "installed_versions", unreadable)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "bundle:      ❌ cannot read" in out
        assert "Permission denied" in out
        assert len(issues) == 1
        assert issues[0].startswith("the KAS engine directory cannot be read")

    @staticmethod
    def _installed(tmp_path, version: str):
        from kiro_crew.agent_sdk import kas_bundle

        return kas_bundle.InstalledKasBundle(
            version=version,
            node_version="22.23.3",
            root=tmp_path,
            node_bin=tmp_path / "node" / "bin" / "node",
            acp_server=tmp_path / "bundle" / "acp-server.js",
        )

    def test_an_installed_engine_prints_the_exact_spawn_argv(self, monkeypatch, capsys, tmp_path):
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, "0.66.33")
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "bundle:      ✅ KAS 0.66.33, Node 22.23.3" in out
        argv = " ".join(kas_bundle.bundle_spawn_argv(installed))
        assert argv in out
        assert "auth owner:  Kiro Crew vault" in out
        assert "not signed in" in out
        assert issues == []

    def test_the_pinned_engine_reports_the_block_written_from_the_engine_gate(
        self, monkeypatch, capsys, tmp_path
    ):
        """The row is the writer's own rule (``engine_spec_permissions_supported`` on
        the installed engine's version), and with no kiro-cli installed its version
        is never asked."""
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, kas_bundle.KAS_PIN_VERSION)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        monkeypatch.setattr(
            cli_doctor,
            "installed_kiro_cli_version",
            lambda: pytest.fail("kiro-cli version asked on the bundle route"),
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert (
            "auto-approve: ✅ spec `permissions` block written"
            f" (the bundled engine KAS {kas_bundle.KAS_PIN_VERSION} reads it)"
        ) in out
        # The vault remedy names kiro-cli only to say the route never runs it.
        assert "kiro-cli" not in out.replace(kas_bundle.KAS_BUNDLE_SIGN_IN_REMEDY, "")
        assert issues == []

    def test_an_engine_below_the_floor_reports_the_block_withheld(
        self, monkeypatch, capsys, tmp_path
    ):
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, "0.66.32")
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        floor = ".".join(str(p) for p in kas_bundle.KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION)
        assert f"auto-approve: ❌ withheld: the bundled engine KAS 0.66.32 is below {floor}" in out
        assert kas_bundle.KAS_VERSION_OVERRIDE_ENV in out
        assert issues == ["the bundled KAS engine is too old to carry the KAS `permissions` block"]

    @pytest.mark.parametrize("cli_version", [(2, 10, 0), None], ids=["old", "unknown"])
    def test_an_installed_kiro_cli_that_refuses_withholds_the_block(
        self, monkeypatch, capsys, tmp_path, cli_version
    ):
        """kiro-cli reads the same specs, so the switch alone does not let the block
        through: the row names kiro-cli and its fix, not the engine."""
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, kas_bundle.KAS_PIN_VERSION)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        monkeypatch.setattr("kiro_crew.kiro_cli.resolve_kiro_cli", lambda: "/opt/kiro/bin/kiro-cli")
        monkeypatch.setattr(cli_doctor, "installed_kiro_cli_version", lambda: cli_version)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        shown = "unknown" if cli_version is None else "2.10.0"
        assert f"auto-approve: ❌ withheld: kiro-cli ({shown}) is installed" in out
        assert "block written" not in out
        assert issues == ["kiro-cli is installed but cannot carry the KAS `permissions` block"]

    @pytest.mark.parametrize("cli_version", [None, (2, 10, 0), (2, 23, 0)])
    @pytest.mark.parametrize("cli_present", [False, True])
    def test_the_row_agrees_with_the_writer_on_every_version(
        self, monkeypatch, tmp_path, cli_present, cli_version
    ):
        """Doctor and writer share the same predicates, so they cannot drift apart.
        The doctor reads the installed kiro-cli's version through its own import
        only to print it, so both names are pinned to the same answer."""
        from kiro_crew.agent_materialization import auto_approve
        from kiro_crew.agent_sdk import kas_bundle

        monkeypatch.setattr("kiro_crew.agent_sdk.backend_install._kas_bundle_route", lambda: True)
        binary = "/opt/kiro/bin/kiro-cli" if cli_present else None
        monkeypatch.setattr("kiro_crew.kiro_cli.resolve_kiro_cli", lambda: binary)
        for owner in ("kiro_crew.cli_doctor", "kiro_crew.kiro_cli"):
            monkeypatch.setattr(f"{owner}.installed_kiro_cli_version", lambda: cli_version)
        for version in (None, "0.66.32", kas_bundle.KAS_PIN_VERSION, "1.2.3"):
            installed = None if version is None else self._installed(tmp_path, version)
            monkeypatch.setattr(kas_bundle, "installed_bundle", lambda i=installed: i)
            issues: list[str] = []
            cli_doctor._report_kas_bundle_spec_permissions(version, issues)
            written = auto_approve._spec_permissions_accepted()
            assert (issues == []) is (written or version is None)

    def test_the_engine_rows_name_the_pin_and_the_install_record(
        self, monkeypatch, capsys, tmp_path
    ):
        """The rows a bundle host gets where the relay host gets kiro-cli's: the
        installed version against this release's pin, the ``install.json`` path and
        the two entry points it records, and the vault remedy that never names
        ``kiro-cli login``."""
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, kas_bundle.KAS_PIN_VERSION)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda: [installed.version])
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert f"pin:         ✅ KAS {kas_bundle.KAS_PIN_VERSION} is this release's pin" in out
        assert str(installed.root / kas_bundle.KAS_BUNDLE_INSTALL_RECORD_NAME) in out
        assert f"engine:      {installed.acp_server}" in out
        assert f"node:        {installed.node_bin}" in out
        assert kas_bundle.KAS_BUNDLE_SIGN_IN_REMEDY in out
        assert "kiro-cli login" not in out
        assert "also installed" not in out
        assert issues == []

    def test_an_overridden_engine_is_a_warning_naming_the_variable(
        self, monkeypatch, capsys, tmp_path
    ):
        """``installed_bundle()`` resolves the version ``pinned_version()`` names, so a
        version other than the pin can only come from ``KIROCREW_KAS_VERSION``: the
        row says so, and lists the other engines kept on disk without judging them."""
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, "0.99.0-other")
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        monkeypatch.setattr(
            kas_bundle,
            "installed_versions",
            lambda: ["0.99.0-other", kas_bundle.KAS_PIN_VERSION],
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert f"running KAS 0.99.0-other; this release pins {kas_bundle.KAS_PIN_VERSION}" in out
        assert kas_bundle.KAS_VERSION_OVERRIDE_ENV in out
        assert f"also installed: {kas_bundle.KAS_PIN_VERSION}" in out
        # Advisory: a developer's override is not a broken installation.
        assert issues == []

    def test_a_signed_in_vault_is_the_green_row(self, monkeypatch, capsys, tmp_path):
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, kas_bundle.KAS_PIN_VERSION)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda: [installed.version])
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.auth.bridge.describe_vault_identity", lambda: "builder-id -> usable"
        )
        cli_doctor._doctor_kas([])
        out = capsys.readouterr().out
        assert "crew vault:  builder-id -> usable" in out
        assert "crew vault:  ✅ signed in through Kiro Crew" in out
        assert "not signed in" not in out

    def test_a_rejected_refresh_vault_is_not_a_green_row(self, monkeypatch, capsys, tmp_path):
        from kiro_crew.agent_sdk import kas_bundle

        installed = self._installed(tmp_path, kas_bundle.KAS_PIN_VERSION)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: installed)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda: [installed.version])
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: True)
        detail = "social/Google, expired, refresh token rejected -> not usable"
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: detail)
        cli_doctor._doctor_kas([])
        out = capsys.readouterr().out
        assert "✅ signed in through Kiro Crew" not in out
        assert "may not be usable" in out
        assert f"crew vault:  {detail}" in out
        assert kas_bundle.KAS_BUNDLE_SIGN_IN_REMEDY in out
        assert "auth owner:  Kiro Crew vault" in out


class TestDoctorAgentAuthOnTheBundleRoute:
    """The ``kas auth:`` row when KAS runs Crew's own engine.

    The vault is the route's only owner, so the row never falls through to the
    kiro-cli store: on a host with no kiro-cli that fallback printed ``could not
    check`` for a store the route never reads, and its remedy named ``kiro-cli
    login`` for a binary nothing would spawn.
    """

    class _Cfg:
        def __init__(self, backend: str, kas_bundle: bool) -> None:
            self.agent = type("A", (), {"acp_backend": backend, "kas_bundle": kas_bundle})()

    def _run(self, monkeypatch, capsys, *, backend="kas", bundle=True, vault_holds, detail=None):
        from kiro_crew import acp_backends

        cfg = self._Cfg(backend, bundle)
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: ["kas"])
        monkeypatch.setattr(
            cli_doctor,
            "_kiro_cli_signed_in",
            lambda: pytest.fail("kiro-cli store probed on the bundle route"),
        )
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: vault_holds)
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: detail)
        cli_doctor._doctor_agent_auth()
        return capsys.readouterr().out

    def test_an_empty_vault_is_the_signed_out_row_with_crews_remedy(self, monkeypatch, capsys):
        from kiro_crew.agent_sdk import kas_bundle

        out = self._run(monkeypatch, capsys, vault_holds=False)
        assert "kas auth:" in out
        assert "Kiro Crew vault; not signed in" in out
        for word in kas_bundle.KAS_BUNDLE_SIGN_IN_REMEDY.split():
            assert word in out, word
        assert "Sign in with `kirocrew kas login`" in out
        assert "could not check" not in out
        assert "kiro-cli login" not in out

    def test_a_signed_in_vault_mentions_no_second_store(self, monkeypatch, capsys):
        detail = "social/Google, expires in 42m, refresh token present -> usable"
        out = self._run(monkeypatch, capsys, vault_holds=True, detail=detail)
        assert "✅ Kiro Crew vault (signed in through Kiro Crew)" in out
        assert detail in out
        assert "also present" not in out

    def test_a_stored_but_unusable_identity_is_still_printed(self, monkeypatch, capsys):
        """The one line that explains a failing first prompt on a host the operator
        signed into through Crew: the vault holds an entry the probe rejected."""
        detail = "social/Google, expired, refresh token rejected -> not usable"
        out = self._run(monkeypatch, capsys, vault_holds=False, detail=detail)
        assert "Kiro Crew vault; not signed in" in out
        assert detail in out

    def test_the_relay_route_still_falls_through_to_the_kiro_cli_store(self, monkeypatch, capsys):
        """Parity: with the bundle off, the row is the kiro-cli store's, exactly as
        before -- the predicate is positive on BOTH halves."""
        from kiro_crew import acp_backends

        cfg = self._Cfg("kas", False)
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: ["kas"])
        monkeypatch.setattr(cli_doctor, "_kiro_cli_signed_in", lambda: None)
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: False)
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: None)
        cli_doctor._doctor_agent_auth()
        out = capsys.readouterr().out
        assert "kas auth:" in out
        assert "kiro-cli's own sign-in; could not check" in out
        assert "Kiro Crew vault; not signed in" not in out


class TestDoctorKas:
    """`kirocrew doctor` KAS backend section — gated on acp_backend == kas.

    KAS is served by kiro-cli's ACP relay, so the section reports the relay
    invocation and whether this kiro-cli can select the KAS engine. It probes no
    credential: the relay resolves tokens from kiro-cli's own store, which the
    sign-in check already covers.
    """

    class _Cfg:
        def __init__(self, backend: str, kas_bundle: bool = False) -> None:
            self.agent = type("A", (), {"acp_backend": backend, "kas_bundle": kas_bundle})()

    def _patch_cfg(self, monkeypatch, backend: str) -> None:
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: self._Cfg(backend))
        )

    @pytest.fixture(autouse=True)
    def _accepting_cli(self, _floor_monkeypatch):
        """Pin the installed kiro-cli to one that accepts the spec ``permissions``
        field, so the cases here stay about the relay and the engine.

        Unpinned, the new auto-approve row would spawn the test host's own
        kiro-cli -- absent on CI, which reads as refusing and appends an issue the
        engine cases do not expect.
        """
        _floor_monkeypatch.setattr(
            cli_doctor,
            "installed_kiro_cli_version",
            lambda: cli_doctor.SPEC_PERMISSIONS_MIN_VERSION,
        )

    def _patch_vault(self, monkeypatch, holds: bool = False, detail: str | None = None) -> None:
        """Stub both vault probes so no test reads the developer's real vault.

        ``_report_kas_backend`` reaches ``vault_holds_identity`` and
        ``describe_vault_identity`` on every run, so every test that gets past
        the binary check needs these pinned to stay host-independent.
        """
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: holds)
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: detail)

    def test_silent_when_backend_not_kas(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        assert "KAS backend" not in capsys.readouterr().out
        assert issues == []

    def test_selected_but_no_kiro_cli_appends_issue(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "KAS backend" in out
        assert "KAS backend selected but kiro-cli is not installed" in issues
        # No engine probe is attempted when there is no binary to probe.
        assert "engine:" not in out

    def test_engine_supported_prints_the_relay_argv(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: False)
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: None)
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default), or v3",
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        # The exact invocation, so a reader can reproduce it by hand.
        assert "acp --agent-engine v3 --auth-method cli" in out
        assert "auth owner:  kiro-cli credential store" in out
        # The token line agrees with its own auth owner line: cli-owned spawn,
        # cli-named token source.
        assert "token:       ➖ kiro-cli's own sign-in (see the sign-in rows above)" in out
        assert "crew vault:" not in out
        assert "✅ v3 supported" in out
        assert issues == []

    def test_vault_import_failure_falls_back_to_cli_auth(self, monkeypatch, capsys) -> None:
        """A broken auth install cannot abort the KAS doctor section."""
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default), or v3",
        )
        monkeypatch.setitem(sys.modules, "kiro_crew.auth.bridge", None)
        issues: list[str] = []

        cli_doctor._doctor_kas(issues)

        out = capsys.readouterr().out
        assert "auth owner:  kiro-cli credential store" in out
        assert "crew vault:" not in out
        assert "token:       ➖ kiro-cli's own sign-in" in out
        assert issues == []

    @pytest.mark.parametrize(
        ("version", "auth_tail"),
        [
            ((3, 0, 0), " --auth-method client\n"),
            (None, " --auth-method client\n"),
            ((2, 35, 1), "\n"),
        ],
        ids=["at-or-above-floor", "unknown", "below-floor"],
    )
    def test_crew_sign_in_prints_the_crew_owned_argv(
        self, monkeypatch, capsys, version, auth_tail
    ) -> None:
        """With an identity in Crew's vault the reported argv carries the flag the
        runtime would pass for that release and names Crew as the auth owner --
        the same decision the runtime makes."""
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "installed_kiro_cli_version", lambda: version)
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.auth.bridge.describe_vault_identity",
            lambda: "social/Google, expires in 42m, refresh token present -> usable",
        )
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default), or v3",
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "acp --agent-engine v3" + auth_tail in out
        assert "--auth-method cli\n" not in out
        assert "auth owner:  Kiro Crew vault" in out
        assert "crew vault:  social/Google" in out
        # The token line agrees with its own auth owner line: vault-owned spawn,
        # vault-named token source -- naming kiro-cli's store here would
        # contradict the owner line two rows up.
        assert "token:       ➖ Kiro Crew vault sign-in (see the auth owner line above)" in out
        assert "kiro-cli's own sign-in" not in out
        # An unknown version also withholds the spec permissions block, which
        # the doctor reports as its own issue; nothing else is raised.
        assert [i for i in issues if "permissions` block" not in i] == []

    def test_engine_missing_appends_issue(self, monkeypatch, capsys) -> None:
        """A kiro-cli that offers engines but not ours cannot serve KAS."""
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        self._patch_vault(monkeypatch)
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default)",
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "does not offer engine v3" in out
        assert any("does not offer engine v3" in i for i in issues)

    def test_help_without_the_flag_is_a_failure_not_unknown(self, monkeypatch, capsys) -> None:
        """A kiro-cli predating engine selection must FAIL the check.

        Reporting it as "unknown" would let a configuration that cannot work
        pass readiness and fail later at session-create time instead.
        """
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        self._patch_vault(monkeypatch)
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "Usage: kiro-cli acp [OPTIONS]\n  -a, --trust-all-tools",
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "no --agent-engine flag" in out
        assert "engine support unknown" not in out
        assert any("no --agent-engine flag" in i for i in issues)

    def test_a_v3_kiro_cli_that_refuses_the_engine_is_a_failed_engine_row(
        self, monkeypatch, capsys
    ) -> None:
        """Help listing v3 is not the answer: kiro-cli refusing the start is the gap."""
        from kiro_crew.acp.kas_engine_refusal import KAS_ENGINE_REFUSED_REASON

        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        self._patch_vault(monkeypatch)
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default), or v3",
        )
        started: list[str | None] = []

        def _refused(binary=None):
            started.append(binary)
            return False, KAS_ENGINE_REFUSED_REASON

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_start_verdict", _refused)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "✅ v3 supported" not in out
        assert f"engine:      ❌ {KAS_ENGINE_REFUSED_REASON}" in out
        assert "switch agent.acp_backend to kiro" in out
        assert issues == [f"kiro-cli cannot serve the KAS engine: {KAS_ENGINE_REFUSED_REASON}"]
        # The start ran on the binary the help row read, never another one.
        assert started == ["/x/kiro-cli"]

    def test_engine_verdict_comes_from_the_shared_classifier(self, monkeypatch, capsys) -> None:
        """The doctor and the boot floor read ``acp --help`` through ONE function."""
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        self._patch_vault(monkeypatch)
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda _binary: "any help")
        monkeypatch.setattr(cli_doctor, "kas_engine_gap", lambda _text: "a stubbed gap")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "engine:      ❌ a stubbed gap" in out
        assert any("a stubbed gap" in i for i in issues)

    def test_unreadable_help_is_reported_unknown_not_failed(self, monkeypatch, capsys) -> None:
        """Only a FAILED probe is unknown; a diagnostic must not invent a verdict.

        ``None`` now means the subprocess did not run, which is the one case
        where nothing is established either way.
        """
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        self._patch_vault(monkeypatch)
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda _binary: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "engine support unknown" in out
        assert issues == []

    def test_probe_returns_help_text_even_without_the_flag(self, monkeypatch, tmp_path) -> None:
        """The probe must not swallow ran-but-lacks-the-flag into None.

        Pins the split directly: the previous implementation returned None for
        both a failed spawn and help text missing the selector, which is what
        let an unsupported kiro-cli pass.
        """

        from kiro_crew import kiro_prerequisite
        from kiro_crew.kiro_prerequisite_process import ProcessResult

        async def _ran(*_a, **_k):
            return ProcessResult(ok=True, output="Usage: kiro-cli acp [OPTIONS]", returncode=0)

        binary = tmp_path / "kiro-cli"
        binary.write_bytes(b"")
        binary.chmod(0o755)
        monkeypatch.setattr(kiro_prerequisite, "_run_process", _ran)
        got = cli_doctor._kas_relay_help(str(binary))
        assert got is not None
        assert "--agent-engine" not in got

    def test_probe_returns_none_when_the_spawn_fails(self, monkeypatch, tmp_path) -> None:
        from kiro_crew import kiro_prerequisite
        from kiro_crew.kiro_prerequisite_process import ProcessResult

        async def _failed(*_a, **_k):
            return ProcessResult(ok=False, error="no such binary")

        binary = tmp_path / "kiro-cli"
        binary.write_bytes(b"")
        binary.chmod(0o755)
        monkeypatch.setattr(kiro_prerequisite, "_run_process", _failed)
        assert cli_doctor._kas_relay_help(str(binary)) is None
        # An absent binary is not probed at all.
        assert cli_doctor._kas_relay_help(str(tmp_path / "absent")) is None

    def test_no_credential_probe_is_performed(self, monkeypatch, capsys) -> None:
        """The relay owns auth, so the doctor must not reach for a token.

        A probe would put Crew in the credential path. The row must show the declared
        entitlement source's LABEL and never its snake_case identifier.
        """
        from kiro_crew.agent_sdk import host_auth

        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda _binary: "v3")
        # Pinned False so the assertion reads the cli-owned token line rather
        # than the developer's real vault state.
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: False)
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert host_auth.entitlement_label("kas") in out
        assert host_auth.ENTITLEMENT_HOST_IDENTITY_STORE not in out
        assert not hasattr(cli_doctor, "_kas_version_label")


class TestTheKasBlockReportsAWithheldPermissionsField:
    """The one place a withheld KAS auto-approve is visible.

    ``agent.py`` writes the spec ``permissions`` block only when kiro-cli accepts it:
    an older release uses ``deny_unknown_fields`` and would reject the whole spec. The
    withheld field is reported only here, since it matters only when KAS is selected.
    """

    def _run(
        self, monkeypatch, capsys, version, *, help_probe_fails: bool = False
    ) -> tuple[str, list[str]]:
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig,
            "load",
            classmethod(
                lambda cls: type(
                    "C", (), {"agent": type("A", (), {"acp_backend": "kas", "kas_bundle": False})()}
                )()
            ),
        )
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        # A help text the engine probe is satisfied by, so the only issue any case
        # here can append is the one the auto-approve row is responsible for.
        # ``help_probe_fails`` swaps in the FAILED probe (``None``) instead.
        help_text = (
            None if help_probe_fails else f"--agent-engine <ENGINE>  {cli_doctor.KAS_RELAY_ENGINE}"
        )
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda _binary: help_text)
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: False)
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: None)
        monkeypatch.setattr(cli_doctor, "installed_kiro_cli_version", lambda: version)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        return capsys.readouterr().out, issues

    def test_an_accepting_cli_reports_the_block_as_written(self, monkeypatch, capsys) -> None:
        out, issues = self._run(monkeypatch, capsys, cli_doctor.SPEC_PERMISSIONS_MIN_VERSION)
        assert "auto-approve: ✅" in out
        assert issues == []

    def test_a_refusing_cli_names_the_version_and_the_floor(self, monkeypatch, capsys) -> None:
        """Both numbers, because the fix is "update past this floor"."""
        floor = cli_doctor.SPEC_PERMISSIONS_MIN_VERSION
        out, issues = self._run(monkeypatch, capsys, (floor[0], floor[1] - 1, 0))
        assert "auto-approve: ❌" in out
        assert f"{floor[0]}.{floor[1] - 1}.0" in out
        assert ".".join(str(part) for part in floor) in out
        assert "kiro-cli is too old to carry the KAS `permissions` block" in issues

    def test_an_unknown_version_is_its_own_row_and_names_the_pin_remedy(
        self, monkeypatch, capsys
    ) -> None:
        """Unknown is not "too old": the writer withholds a NEW block but keeps one
        already on disk, and the remedy is a probeable binary, not an update."""
        out, issues = self._run(monkeypatch, capsys, None)
        assert "auto-approve: ⚠️" in out
        assert "version unknown" in out
        assert cli_doctor.PATH_ONLY_INSTALL_NOTE in out
        assert "already on disk is kept" in out
        assert "update kiro-cli" not in out
        assert issues == ["kiro-cli version unknown, so the KAS `permissions` block is not seeded"]

    def test_a_failed_help_probe_does_not_swallow_the_row(self, monkeypatch, capsys) -> None:
        """``acp --help`` failing says nothing about ``--version``.

        The engine rows return early when their probe fails; this row must not
        ride on that return, or a withheld auto-approve is hidden on exactly the
        host where kiro-cli is misbehaving.
        """
        floor = cli_doctor.SPEC_PERMISSIONS_MIN_VERSION
        out, issues = self._run(
            monkeypatch, capsys, (floor[0], floor[1] - 1, 0), help_probe_fails=True
        )
        assert "engine support unknown" in out
        assert "auto-approve: ❌" in out
        assert issues == ["kiro-cli is too old to carry the KAS `permissions` block"]

    def test_the_row_is_silent_when_kas_is_not_the_backend(self, monkeypatch, capsys) -> None:
        """A kiro-cli or Claude Code install loses nothing, so it hears nothing."""
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig,
            "load",
            classmethod(
                lambda cls: type("C", (), {"agent": type("A", (), {"acp_backend": ""})()})()
            ),
        )
        monkeypatch.setattr(cli_doctor, "installed_kiro_cli_version", lambda: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        assert "auto-approve:" not in capsys.readouterr().out
        assert issues == []


class TestTheKasBlockReportsToolVocabularyDrift:
    """The KAS tool-name tables (``platform.tool_names``) were measured on one
    kiro-cli release. A newer engine may rename or add a built-in; the policy
    direction fails permissive on an unknown id, so the doctor's KAS block says
    when the installed engine is newer than the measurement. Advisory only."""

    def _run(self, monkeypatch, capsys, version) -> tuple[str, list[str]]:
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig,
            "load",
            classmethod(
                lambda cls: type(
                    "C", (), {"agent": type("A", (), {"acp_backend": "kas", "kas_bundle": False})()}
                )()
            ),
        )
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: f"--agent-engine <ENGINE>  {cli_doctor.KAS_RELAY_ENGINE}",
        )
        monkeypatch.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: False)
        monkeypatch.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: None)
        monkeypatch.setattr(cli_doctor, "installed_kiro_cli_version", lambda: version)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        return capsys.readouterr().out, issues

    def test_the_measured_version_is_reported_as_matching(self, monkeypatch, capsys) -> None:
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        out, _ = self._run(monkeypatch, capsys, KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[0])
        assert "tool names:  ✅" in out

    def test_every_verified_release_is_quiet_not_a_drift_warning(self, monkeypatch, capsys) -> None:
        """The pin is a LIST, not one exact release: a routine kiro-cli release the
        tables were re-measured on (2.24.1 here, the release the write-plane path
        shapes were measured on) is ✅, so the row does not cry drift on every
        release until someone notices and erode the one guard on fail-permissive
        drift."""
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        assert len(KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI) >= 2
        for verified in KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI:
            out, issues = self._run(monkeypatch, capsys, verified)
            assert "tool names:  ✅" in out, verified
            assert "⚠️" not in out.split("tool names:")[1].splitlines()[0]
            assert ".".join(str(p) for p in verified) in out

    def test_an_older_engine_is_unverified_not_compatible(self, monkeypatch, capsys) -> None:
        """The tables were read from ONE release; an older KAS may lack an id or
        spell it differently, so ``<=`` marking it ✅ attested what nothing measured."""
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        major, minor, patch = KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[0]
        out, issues = self._run(monkeypatch, capsys, (major, minor - 1, patch))
        assert "tool names:  ⚠️" in out
        assert "tool names:  ✅" not in out
        assert "older than" in out
        assert f"{major}.{minor - 1}.{patch}" in out
        assert not any("tool" in i and "vocabul" in i for i in issues)

    def test_a_newer_engine_warns_and_names_both_versions_and_the_fixture(
        self, monkeypatch, capsys
    ) -> None:
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        major, minor, patch = KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[-1]
        out, issues = self._run(monkeypatch, capsys, (major, minor + 1, patch))
        assert "tool names:  ⚠️" in out
        assert f"{major}.{minor + 1}.{patch}" in out
        assert f"{major}.{minor}.{patch}" in out
        assert "kas_builtin_tool_ids.json" in out
        assert "verified_kiro_cli_versions" in out
        # Advisory: not known to have drifted, only not known not to have.
        assert not any("tool" in i and "vocabul" in i for i in issues)

    def test_an_unknown_version_is_its_own_row(self, monkeypatch, capsys) -> None:
        out, _ = self._run(monkeypatch, capsys, None)
        assert "tool names:  ➖" in out
        assert "installed version unknown" in out


class TestDoctorKiroCliPickOnAnOldKiroCli:
    """A stored Kiro CLI pick reads as KAS; a kiro-cli without v3 keeps it on v2.

    The engine row then offers the kiro-cli upgrade, never a switch to the harness
    the user already picked (the a-kiro-cli-choice-runs-on-kas entry in docs/decisions).
    """

    _OLD_HELP = "Usage: kiro-cli acp [OPTIONS]\n  --trust-all-tools\n"

    @pytest.fixture(autouse=True)
    def _relay(self, _floor_monkeypatch):
        mp = _floor_monkeypatch
        cfg = type("C", (), {"agent": type("A", (), {"acp_backend": "kas", "kas_bundle": False})()})
        mp.setattr(cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: cfg()))
        mp.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/opt/kiro-cli")
        mp.setattr(cli_doctor, "_report_kas_spec_permissions", lambda issues: None)
        mp.setattr(cli_doctor, "_report_kas_tool_vocabulary", lambda issues: None)
        mp.setattr(cli_doctor, "_kas_relay_help", lambda binary: self._OLD_HELP)
        # The gateway's own verdict, stubbed so no row spawns the host's kiro-cli.
        mp.setattr(
            "kiro_crew.agent_sdk.kas_engine_verdict",
            lambda: (False, "this kiro-cli has no --agent-engine flag"),
        )
        mp.setattr("kiro_crew.auth.bridge.vault_holds_identity", lambda: False)
        mp.setattr("kiro_crew.auth.bridge.describe_vault_identity", lambda: None)
        # The v3 start that follows a help verdict naming v3 answers without a refusal.
        mp.setattr("kiro_crew.agent_sdk.kas_engine_start_verdict", lambda binary=None: (True, None))

    def test_a_kiro_cli_pick_is_told_to_update_kiro_cli(self, monkeypatch, capsys):
        """The doctor never probes-and-confirms, so the pick loads as Kiro CLI here."""
        cfg = type("C", (), {"agent": type("A", (), {"acp_backend": "", "kas_bundle": False})()})
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: cfg()))
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: True)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "your Kiro CLI selection stays on kiro-cli's v2 engine" in out
        assert "Fix: run `kiro-cli update`, then restart Kiro Crew." in out
        assert "switch agent.acp_backend to kiro" not in out
        assert len(issues) == 1 and issues[0].startswith("kiro-cli is too old for its v3")

    _V3_HELP = (
        "Usage: kiro-cli acp [OPTIONS]\n  --agent-engine <ENGINE>  [possible values: v2, v3]\n"
    )

    @pytest.mark.parametrize(
        ("verdict", "kas_selectable", "row"),
        [
            (
                (True, None),
                True,
                "engine:      ✅ v3 supported; your Kiro CLI selection runs on KAS",
            ),
            (
                (True, None),
                False,
                "v3 supported, but KAS is not available here; "
                "your Kiro CLI selection stays on kiro-cli's v2 engine",
            ),
            (
                (None, None),
                True,
                "engine support unknown; your Kiro CLI selection stays on kiro-cli's v2 engine",
            ),
        ],
        ids=["on-kas", "kas-not-selectable", "unknown"],
    )
    def test_the_engine_row_says_which_engine_a_kiro_cli_pick_runs_on(
        self, monkeypatch, capsys, verdict, kas_selectable, row
    ):
        """The engine row reads the floor's own verdict, never the doctor's own probe.

        The doctor's unsandboxed probe answers v3 in every case here, so an
        unknown floor verdict (a sandbox-refused probe) must still read unknown.
        """
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: True)
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda binary: self._V3_HELP)
        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", lambda: verdict)
        monkeypatch.setattr(cli_doctor, "_kas_selectable", lambda: kas_selectable)
        cli_doctor._doctor_kas([])
        assert row in capsys.readouterr().out

    def test_a_kas_pick_keeps_the_plain_engine_row(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: False)
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda binary: self._V3_HELP)
        cli_doctor._doctor_kas([])
        out = capsys.readouterr().out
        assert "engine:      ✅ v3 supported\n" in out
        assert "Kiro CLI selection" not in out

    def test_a_kiro_cli_pick_on_a_refusing_kiro_cli_names_the_refusal(self, monkeypatch, capsys):
        """Help lists v3 and the start is refused: the issue says cannot run, not too old."""
        from kiro_crew.acp.kas_engine_refusal import KAS_ENGINE_REFUSED_REASON

        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: True)
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default), or v3",
        )
        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", lambda: (True, None))
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.kas_engine_start_verdict",
            lambda binary=None: (False, KAS_ENGINE_REFUSED_REASON),
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert (
            f"engine:      ❌ {KAS_ENGINE_REFUSED_REASON}; your Kiro CLI selection stays on"
            " kiro-cli's v2 engine"
        ) in out
        assert "switch agent.acp_backend to kiro" not in out
        assert issues == [f"kiro-cli cannot run its v3 (KAS) engine: {KAS_ENGINE_REFUSED_REASON}"]

    def test_a_kas_pick_keeps_its_own_remedy(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: False)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "switch agent.acp_backend to kiro" in out
        assert issues[0].startswith("kiro-cli cannot serve the KAS engine")

    @pytest.mark.parametrize(
        ("selected", "verdict", "expected"),
        [
            (True, (None, None), False),
            (True, (False, "no v3"), False),
            (True, (True, None), True),
            (False, (None, None), True),
        ],
        ids=["kiro-pick-unknown", "kiro-pick-gap", "kiro-pick-on-kas", "kas-pick"],
    )
    def test_kas_only_rows_run_only_where_the_pick_runs_on_kas(
        self, monkeypatch, capsys, selected, verdict, expected
    ):
        """A Kiro CLI pick kept on v2 gets no KAS permissions or tool-name rows."""
        ran: list[str] = []
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: selected)
        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", lambda: verdict)
        monkeypatch.setattr(
            cli_doctor, "_report_kas_spec_permissions", lambda issues: ran.append("perm")
        )
        monkeypatch.setattr(
            cli_doctor, "_report_kas_tool_vocabulary", lambda issues: ran.append("vocab")
        )
        cli_doctor._doctor_kas([])
        capsys.readouterr()
        assert ran == (["perm", "vocab"] if expected else [])

    def test_a_pick_whose_v3_start_is_refused_gets_no_kas_rows(self, monkeypatch, capsys):
        """The gateway keeps that pick on v2, so the KAS-only rows stay out too."""
        ran: list[str] = []
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: True)
        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", lambda: (True, None))
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.kas_engine_start_verdict", lambda binary=None: (False, "refused")
        )
        monkeypatch.setattr(
            cli_doctor, "_report_kas_spec_permissions", lambda issues: ran.append("perm")
        )
        monkeypatch.setattr(
            cli_doctor, "_report_kas_tool_vocabulary", lambda issues: ran.append("vocab")
        )
        cli_doctor._doctor_kas([])
        capsys.readouterr()
        assert ran == []

    def test_a_kiro_cli_pick_on_the_bundle_route_gets_no_kas_block(self, monkeypatch, capsys):
        """The bundle route keeps a Kiro CLI pick on kiro-cli, so no KAS rows print."""
        cfg = type("C", (), {"agent": type("A", (), {"acp_backend": "", "kas_bundle": True})()})
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: cfg()))
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: True)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        assert "KAS backend" not in capsys.readouterr().out
        assert issues == []
