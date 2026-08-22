"""Tests for kirocrew secrets CLI subcommands."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.secrets import SecretVault


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    """Create a temporary vault with test secrets."""
    vault = SecretVault(tmp_path)
    vault._set_sync("API_KEY", "sk-test-12345")
    vault._set_sync("DB_PASS", "hunter2")
    return tmp_path


@pytest.fixture()
def empty_vault_dir(tmp_path: Path) -> Path:
    """Config dir with no vault."""
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_spawned_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure KIROCREW_SPAWNED is unset so CLI tests run as a human session."""
    monkeypatch.delenv("KIROCREW_SPAWNED", raising=False)


@pytest.fixture(autouse=True)
def _vault_floor_enforced(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Present an enforced OS vault floor to the secrets gate.

    Mutating verbs (``set``/``rm``) require the sandbox bind-mount hide of the
    vault to actually be in force, and the check FAILS CLOSED — under pytest the
    real posture depends on the host (CI runners often have no user namespaces),
    so every mutation test would be denied for the wrong reason.

    Tests that exercise the floor check ITSELF opt out with
    ``@pytest.mark.real_vault_floor`` so they assert against the implementation
    rather than this stub.
    """
    if request.node.get_closest_marker("real_vault_floor"):
        return
    monkeypatch.setattr("kiro_crew.cli._vault_floor_unavailable", lambda: None)


class TestSecretsAgentGate:
    """The agent env-var gate blocks secrets in spawned sessions."""

    def test_blocked_in_agent_session(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """secrets subcommand exits 1 when KIROCREW_SPAWNED is set."""
        from kiro_crew.cli import _secrets

        monkeypatch.setenv("KIROCREW_SPAWNED", "1")

        class Args:
            secrets_action = "list"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1

    def test_blocked_even_with_empty_value(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """Gate fires on key presence, not truthiness — empty string still blocks."""
        from kiro_crew.cli import _secrets

        monkeypatch.setenv("KIROCREW_SPAWNED", "")

        class Args:
            secrets_action = "list"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1

    def test_allowed_without_env(self, vault_dir: Path, capsys) -> None:
        """secrets subcommand works when not in an agent session."""
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "API_KEY" in out


class TestSecretsVaultFloorGate:
    """Mutations require the OS-level vault hide to actually be in force.

    The authorization boundary is not anything this process can attest about
    itself — it is ``sandbox.py``'s bind-mount hide of ``.kiro/crew/.vault``,
    which an agent subprocess cannot escape because a mount namespace is not
    process-controlled state. ``env -i``, ``setsid -f`` and a ``script``-allocated
    PTY all mutate process state and none of them reach it.

    So the only thing left to check is whether that floor EXISTS on this host.
    These tests cover both ways it can be absent, plus the fail-closed default.
    """

    @pytest.mark.real_vault_floor
    def test_set_refused_when_sandbox_is_off(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """`sandbox: off` builds no namespace, so the vault is not hidden."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "sandbox_off")

        class Args:
            secrets_action = "set"
            name = "NEW_KEY"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli._secrets(Args())

        assert exc_info.value.code == 1
        # Nothing was written: the refusal happens before the vault is touched.
        assert "NEW_KEY" not in SecretVault(vault_dir).list_names()

    @pytest.mark.real_vault_floor
    def test_rm_refused_when_sandbox_is_off(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """The same floor requirement guards deletion."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "sandbox_off")

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli._secrets(Args())

        assert exc_info.value.code == 1
        # The secret survives the refused deletion.
        assert "API_KEY" in SecretVault(vault_dir).list_names()

    @pytest.mark.real_vault_floor
    def test_list_still_works_when_sandbox_is_off(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """A names-only read needs no floor: there is no mutation to authorize."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "sandbox_off")

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        assert "API_KEY" in capsys.readouterr().out

    @pytest.mark.real_vault_floor
    def test_live_gateway_not_in_force_refuses_even_if_disk_would_allow(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """The LIVE gateway is authoritative: if it reports the floor is not in
        force, the mutation is refused even when the local disk check would
        allow it (the disk marker is agent-forgeable; the gateway's in-memory
        posture is not)."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_gateway_vault_floor", lambda: (False, "sandbox_off"))
        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda skip_boot_marker=False: None)

        class Args:
            secrets_action = "set"
            name = "NEW_KEY"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli._secrets(Args())

        assert exc_info.value.code == 1
        assert "NEW_KEY" not in SecretVault(vault_dir).list_names()

    @pytest.mark.real_vault_floor
    def test_live_gateway_in_force_allows(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """When the live gateway reports the floor in force, the mutation
        proceeds — the gateway answer is trusted over the local disk check."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_gateway_vault_floor", lambda: (True, ""))
        monkeypatch.setattr(
            cli, "_vault_floor_unavailable", lambda skip_boot_marker=False: "sandbox_off"
        )
        monkeypatch.setattr("sys.stdin", io.StringIO("piped-value\n"))

        class Args:
            secrets_action = "set"
            name = "ALLOWED_BY_GW"
            stdin = True

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        assert "ALLOWED_BY_GW" in SecretVault(vault_dir).list_names()

    @pytest.mark.real_vault_floor
    def test_set_reports_corrupt_vault_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """A restored/corrupt vault (store present, key missing) raises
        ValueError inside vault.set; the CLI must surface a clean error with a
        nonzero exit, never an uncaught traceback."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_gateway_vault_floor", lambda: (True, ""))
        monkeypatch.setattr("sys.stdin", io.StringIO("v\n"))

        async def _boom(name, value):
            raise ValueError("Vault store exists but key is missing")

        monkeypatch.setattr(SecretVault, "set", lambda self, n, v: _boom(n, v))

        class Args:
            secrets_action = "set"
            name = "K"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli._secrets(Args())

        assert exc_info.value.code == 1
        assert "error:" in capsys.readouterr().err.lower()

    @pytest.mark.real_vault_floor
    def test_set_allowed_when_the_floor_is_enforced(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """With the hide in force, the mutation proceeds normally."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: None)
        monkeypatch.setattr("sys.stdin", io.StringIO("piped-value\n"))

        class Args:
            secrets_action = "set"
            name = "ALLOWED"
            stdin = True

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        assert "ALLOWED" in SecretVault(vault_dir).list_names()

    @pytest.mark.real_vault_floor
    def test_floor_check_reports_sandbox_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit `sandbox: off` is reported as the reason."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "off")

        assert cli._vault_floor_unavailable() == "sandbox_off"

    @pytest.mark.real_vault_floor
    def test_floor_check_reports_a_missing_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host with no sandbox backend has no hide either, even on `auto`."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "none")

        assert cli._vault_floor_unavailable() == "no_sandbox_backend"

    @pytest.mark.real_vault_floor
    def test_floor_check_refuses_when_unsandboxed_exec_is_permitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backend can exist yet the spawn path fail OPEN: with
        agent.sandbox_allow_unsandboxed_exec enabled the gateway may launch an
        unconfined agent on a transient failure, so a live agent cannot be proven
        confined and the mutation must be refused fail-closed."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: True)

        assert cli._vault_floor_unavailable() == "unsandboxed_exec_permitted"

    @pytest.mark.real_vault_floor
    def test_floor_check_passes_when_a_backend_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A backend AND a covered vault together clear the check.

        A backend alone is deliberately not sufficient: the hide-lists are
        home-relative, so the vault has to be shown to fall inside one of them.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        covered = tmp_path / ".kiro" / "crew" / ".vault"
        covered.mkdir(parents=True)
        (covered / ".boot_sandbox_mode").write_text("auto")
        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(covered)])
        monkeypatch.setattr(cli, "config_dir", lambda: str(covered.parent))

        assert cli._vault_floor_unavailable() is None

    @pytest.mark.real_vault_floor
    def test_posture_changed_since_boot_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Disk `agent.sandbox` newer than the gateway's recorded boot posture
        (an off→auto flip without a restart) refuses fail-closed: live agents
        may still be running under the old, unconfined tier."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        covered = tmp_path / ".kiro" / "crew" / ".vault"
        covered.mkdir(parents=True)
        # Gateway booted under "off"; disk now says "auto" — mismatch.
        (covered / ".boot_sandbox_mode").write_text("off")
        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(covered)])
        monkeypatch.setattr(cli, "config_dir", lambda: str(covered.parent))

        assert cli._vault_floor_unavailable() == "sandbox_posture_changed_since_boot"

    @pytest.mark.real_vault_floor
    def test_missing_boot_posture_marker_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No recorded boot posture (older gateway, or write failed) refuses
        fail-closed: the CLI cannot confirm live agents are confined."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        covered = tmp_path / ".kiro" / "crew" / ".vault"
        covered.mkdir(parents=True)  # dir exists but NO .boot_sandbox_mode
        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(covered)])
        monkeypatch.setattr(cli, "config_dir", lambda: str(covered.parent))

        assert cli._vault_floor_unavailable() == "gateway_boot_posture_unknown"

    @pytest.mark.real_vault_floor
    def test_vault_dir_absent_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A sandbox exists but `.vault` does not yet exist -> refuse.

        A mount namespace can only mask a path that existed at spawn time; a
        newly first-created store would be readable by an agent started before
        it existed.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        home = tmp_path / ".kiro" / "crew"
        home.mkdir(parents=True)  # config dir exists, but .vault does NOT
        monkeypatch.setattr(
            sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(home / ".vault")]
        )
        monkeypatch.setattr(cli, "config_dir", lambda: str(home))

        assert cli._vault_floor_unavailable() == "vault_dir_absent"

    @pytest.mark.real_vault_floor
    def test_macos_delegated_backend_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The macOS `sandbox-exec` backend cannot be proven to mask `.vault`.

        The path-containment proof models the Linux mount-namespace hide only;
        on macOS the enforcing layer may be delegated, so refuse fail-closed
        even when the vault dir exists and would nominally be "covered".
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "sandbox-exec")
        covered = tmp_path / ".kiro" / "crew" / ".vault"
        covered.mkdir(parents=True)
        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(covered)])
        monkeypatch.setattr(cli, "config_dir", lambda: str(covered.parent))

        assert cli._vault_floor_unavailable() == "sandbox_backend_delegated"

    @pytest.mark.real_vault_floor
    def test_set_refused_when_vault_dir_absent(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """End to end: an absent-dir posture refuses set and names the path."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "vault_dir_absent")

        class Args:
            secrets_action = "set"
            name = "NOPE"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli._secrets(Args())

        assert exc_info.value.code == 1
        assert "NOPE" not in SecretVault(vault_dir).list_names()
        err = capsys.readouterr().err
        assert "does not exist yet" in err
        assert "{path}" not in err

    @pytest.mark.real_vault_floor
    def test_floor_check_fails_closed_when_indeterminate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An error resolving the posture denies rather than allowing."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        def _boom() -> str:
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", _boom)

        assert cli._vault_floor_unavailable() == "sandbox_posture_unknown"

    @pytest.mark.real_vault_floor
    def test_set_refused_when_posture_is_indeterminate(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """Fail-closed end to end: an unresolvable posture refuses the write."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        def _boom() -> str:
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", _boom)

        class Args:
            secrets_action = "set"
            name = "NEVER_STORED"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli._secrets(Args())

        assert exc_info.value.code == 1
        assert "NEVER_STORED" not in SecretVault(vault_dir).list_names()

    @pytest.mark.real_vault_floor
    def test_each_reason_produces_a_distinct_message(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """Each refusal reason must state its OWN cause and remedy.

        A single shared sentence would be factually wrong for two of the three:
        telling a Windows user to edit ``agent.sandbox`` does not help, because no
        config value builds a namespace on a host with no backend. This asserts on
        a substring unique to each reason so a future edit cannot silently
        collapse them back into one message.
        """
        from kiro_crew import cli

        expected = {
            "sandbox_off": "the OS sandbox is disabled",
            "no_sandbox_backend": "this host cannot build a sandbox",
            "sandbox_backend_delegated": "this host uses the macOS sandbox",
            "sandbox_posture_unknown": "posture could not be determined",
            "vault_dir_absent": "does not exist yet",
            "vault_outside_sandbox_hide": "outside the paths the sandbox hides",
        }
        seen: dict[str, str] = {}

        for reason, needle in expected.items():
            monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda r=reason: r)

            class Args:
                secrets_action = "set"
                name = "X"
                stdin = True

            with (
                patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
                pytest.raises(SystemExit),
            ):
                cli._secrets(Args())

            err = capsys.readouterr().err
            assert needle in err, f"{reason} message lost its distinguishing text: {err!r}"
            seen[reason] = err

        # All six must be genuinely different strings, not one sentence reused.
        assert len(set(seen.values())) == 6, f"messages collapsed: {seen}"

        # The no-backend case must NOT advise a config edit that cannot help.
        assert "sandbox" in seen["no_sandbox_backend"]
        assert '"auto"' not in seen["no_sandbox_backend"]

    @pytest.mark.real_vault_floor
    def test_refusal_names_the_attempted_verb(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """`rm` must be refused as `secrets rm`, not mislabelled `secrets set`."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "sandbox_off")

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit),
        ):
            cli._secrets(Args())

        assert "secrets rm refused" in capsys.readouterr().err

    @pytest.mark.real_vault_floor
    def test_vault_inside_a_hidden_path_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A vault under a hidden dir clears the floor check."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        hidden = tmp_path / "home" / ".kiro" / "crew" / ".vault"
        hidden.mkdir(parents=True)
        (hidden / ".boot_sandbox_mode").write_text("auto")
        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(hidden)])
        monkeypatch.setattr(cli, "config_dir", lambda: str(hidden.parent))

        assert cli._vault_floor_unavailable() is None

    @pytest.mark.real_vault_floor
    def test_vault_outside_every_hidden_path_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A relocated KIROCREW_HOME puts the vault where no bind mount covers it.

        This is the gap a bare ``detect_backend() != "none"`` check missed: a
        namespace exists, but the hide-lists are home-relative literals, so this
        vault is still readable by an agent subprocess.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        hidden = tmp_path / "home" / ".kiro" / "crew" / ".vault"
        hidden.mkdir(parents=True)
        elsewhere = tmp_path / "custom-home"
        (elsewhere / ".vault").mkdir(parents=True)
        (elsewhere / ".vault" / ".boot_sandbox_mode").write_text("auto")
        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(hidden)])
        monkeypatch.setattr(cli, "config_dir", lambda: str(elsewhere))

        assert cli._vault_floor_unavailable() == "vault_outside_sandbox_hide"

    @pytest.mark.real_vault_floor
    def test_prefix_sibling_is_not_treated_as_contained(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`.vault-backup` must not count as inside `.vault`.

        A string-prefix comparison would accept it and reopen the bypass.
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        hidden = tmp_path / "crew" / ".vault"
        hidden.mkdir(parents=True)
        sibling = tmp_path / "crew" / ".vault-backup"
        (sibling / ".vault").mkdir(parents=True)
        (sibling / ".vault" / ".boot_sandbox_mode").write_text("auto")
        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", lambda mode: [str(hidden)])
        monkeypatch.setattr(cli, "config_dir", lambda: str(sibling))

        assert cli._vault_floor_unavailable() == "vault_outside_sandbox_hide"

    @pytest.mark.real_vault_floor
    def test_hide_list_resolution_failure_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If the hidden paths cannot be resolved, refuse rather than allow."""
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew import cli

        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda mode: "namespace")
        # The vault dir must exist so the check reaches hide-list resolution
        # (the absent-dir guard runs first).
        home = tmp_path / ".kiro" / "crew"
        (home / ".vault").mkdir(parents=True)
        (home / ".vault" / ".boot_sandbox_mode").write_text("auto")
        monkeypatch.setattr(cli, "config_dir", lambda: str(home))

        def _boom(mode: str):
            raise OSError("cannot resolve home")

        monkeypatch.setattr(sandbox_mod, "hidden_dirs_for_mode", _boom)

        assert cli._vault_floor_unavailable() == "vault_outside_sandbox_hide"

    @pytest.mark.real_vault_floor
    def test_set_refused_when_vault_outside_hide(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """End to end: the mutation is refused and the vault is untouched."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "vault_outside_sandbox_hide")

        class Args:
            secrets_action = "set"
            name = "NOT_STORED"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli._secrets(Args())

        assert exc_info.value.code == 1
        assert "NOT_STORED" not in SecretVault(vault_dir).list_names()
        # The resolved path is named so the operator can diagnose it, and the
        # placeholder must be substituted rather than printed literally.
        err = capsys.readouterr().err
        assert "outside the paths the sandbox hides" in err
        assert "{path}" not in err
        assert ".vault" in err

    @pytest.mark.real_vault_floor
    def test_list_still_works_when_vault_outside_hide(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """Read-only name listing needs no floor: there is no mutation to gate."""
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "vault_outside_sandbox_hide")

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        assert "API_KEY" in capsys.readouterr().out

    def test_stdin_may_be_piped_for_set(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """`secrets set --stdin` remains a supported scripting path.

        Nothing in the gate inspects stdio any more, so piping stdin is simply
        not a signal — which is why this supported workflow cannot be broken by
        a bypass-hardening round.
        """
        from kiro_crew.cli import _secrets

        monkeypatch.setattr("sys.stdin", io.StringIO("piped-value\n"))

        class Args:
            secrets_action = "set"
            name = "PIPED"
            stdin = True

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        assert "PIPED" in SecretVault(vault_dir).list_names()


class TestSecretsAuditTrail:
    """Every secrets decision and mutation emits a SEL event."""

    def _capture_sel(self, monkeypatch: pytest.MonkeyPatch, cli) -> list[dict]:
        """Install a SEL stub that records every log_tool_invocation call.

        The lambda is wrapped in ``staticmethod`` because a plain lambda placed
        in a class body via ``type()`` becomes a BOUND method and would receive
        ``self`` as its first argument.
        """
        calls: list[dict] = []
        monkeypatch.setattr(
            cli,
            "sel",
            lambda: type(
                "S", (), {"log_tool_invocation": staticmethod(lambda **kw: calls.append(kw))}
            )(),
        )
        return calls

    @pytest.mark.real_vault_floor
    def test_denial_is_audited_with_a_machine_readable_reason(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        from kiro_crew import cli

        monkeypatch.setattr(cli, "_vault_floor_unavailable", lambda: "sandbox_off")
        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit),
        ):
            cli._secrets(Args())

        assert calls, "denial emitted no SEL event"
        assert calls[0]["outcome"] == "denied"
        assert calls[0]["metadata"]["reason"] == "sandbox_off"
        # The deny path never records a secret name: the caller was not
        # authorized to name one.
        assert "API_KEY" not in repr(calls)

    def test_env_marker_denial_is_audited(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        from kiro_crew import cli

        monkeypatch.setenv("KIROCREW_SPAWNED", "1")
        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "list"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit),
        ):
            cli._secrets(Args())

        assert calls[0]["outcome"] == "denied"
        assert calls[0]["metadata"]["reason"] == "agent_env_marker"

    def test_set_mutation_is_audited_without_the_value(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """The audit records the NAME only — never the secret value."""
        from kiro_crew import cli

        monkeypatch.setattr("sys.stdin", io.StringIO("super-secret-value\n"))
        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "set"
            name = "AUDITED"
            stdin = True

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        stored = [c for c in calls if c["outcome"] == "stored"]
        assert stored, "successful set emitted no SEL event"
        assert stored[0]["metadata"] == {"name": "AUDITED"}
        # The value must appear nowhere in the audit payload.
        assert "super-secret-value" not in repr(calls)

    def test_rm_mutation_is_audited(self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path) -> None:
        from kiro_crew import cli

        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        deleted = [c for c in calls if c["outcome"] == "deleted"]
        assert deleted, "successful rm emitted no SEL event"
        assert deleted[0]["metadata"] == {"name": "API_KEY"}


class TestSecretsListCommand:
    """Tests for `kirocrew secrets list`."""

    def test_lists_secret_names(self, vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "API_KEY" in out
        assert "DB_PASS" in out

    def test_escape_control_chars_neutralizes_terminal_sequences(self) -> None:
        """C0/C1/ESC/CSI/OSC bytes are rendered inert; printable text survives."""
        from kiro_crew.cli import _escape_control_chars

        # OSC window-title set + BEL, and a CSI clear-screen.
        raw = "evil\x1b]0;pwned\x07\x1b[2Jname"
        escaped = _escape_control_chars(raw)
        assert "\x1b" not in escaped
        assert "\x07" not in escaped
        assert "\\x1b" in escaped and "\\x07" in escaped
        # A lone C1 CSI introducer (single byte) is escaped too.
        assert _escape_control_chars("x\x9by") == "x\\x9by"
        # Ordinary names (incl. Unicode) pass through unchanged.
        assert _escape_control_chars("API_KEY-\u00e9") == "API_KEY-\u00e9"

    def test_list_escapes_control_chars_in_names(self, tmp_path: Path, capsys) -> None:
        """`secrets list` prints an escaped form of a control-char-laden name."""
        from kiro_crew.cli import _secrets

        vault = SecretVault(tmp_path)
        vault._set_sync("evil\x1b]0;pwned\x07name", "v")

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x07" not in out
        assert "\\x1b" in out

    def test_empty_vault(self, empty_vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(empty_vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "No secrets stored" in out


class TestSecretsSetCommand:
    """Tests for `kirocrew secrets set`."""

    def test_set_with_stdin_flag(self, tmp_path: Path, capsys) -> None:
        import io

        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "NEW_SECRET"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", io.StringIO("my-value-123\n")),
        ):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "stored" in out.lower()

        # Verify it was actually stored
        vault = SecretVault(tmp_path)
        sv = vault.get("NEW_SECRET")
        assert sv is not None
        assert sv.reveal() == "my-value-123"

    def test_set_rejects_non_utf8_value_with_clean_error(self, tmp_path: Path, capsys) -> None:
        """A value piped from a non-UTF-8 source (surrogate escapes survive the
        stdin decode) must fail with a clean CLI error and a non-zero exit, and
        must NOT write a partial/garbled secret."""
        import io

        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "BINARY"
            stdin = True

        # A lone surrogate is exactly what reaches us after Python decodes a
        # non-UTF-8 pipe with surrogateescape; it cannot be re-encoded as UTF-8.
        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", io.StringIO("bad-\udcff-value\n")),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "not valid utf-8" in err.lower()
        # Nothing was stored.
        assert SecretVault(tmp_path).get("BINARY") is None

    def test_set_rejects_non_utf8_name_with_clean_error(self, tmp_path: Path, capsys) -> None:
        """A secret NAME carrying an undecodable POSIX-argv byte (a surrogate)
        must fail with a clean CLI error and non-zero exit, not crash inside
        vault.set at AAD/key encoding, and must store nothing."""
        import io

        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "BAD-\udcff-NAME"  # lone surrogate: not UTF-8 encodable
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", io.StringIO("some-value\n")),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        assert "name is not valid utf-8" in capsys.readouterr().err.lower()

    def test_set_stdin_read_decode_error_is_clean(self, tmp_path: Path, capsys) -> None:
        """If the text-mode stdin raises UnicodeDecodeError at read() (a pipe of
        raw non-UTF-8 bytes under strict decoding), `set` must emit the clean
        invalid-UTF-8 error and exit non-zero, not surface a traceback."""
        from kiro_crew.cli import _secrets

        class _BadStdin:
            def read(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        class Args:
            secrets_action = "set"
            name = "BINARY"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", _BadStdin()),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        assert "not valid utf-8" in capsys.readouterr().err.lower()
        assert SecretVault(tmp_path).get("BINARY") is None
        """`rm` has the same non-UTF-8 name hazard as `set` (the name reaches
        vault.delete AAD/key encoding): reject cleanly before the vault call."""
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "rm"
            name = "BAD-\udcff-NAME"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        assert "name is not valid utf-8" in capsys.readouterr().err.lower()

    def test_set_prompts_for_value(self, tmp_path: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "PROMPTED"
            stdin = False

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("getpass.getpass", return_value="prompted-value"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            _secrets(Args())

        vault = SecretVault(tmp_path)
        sv = vault.get("PROMPTED")
        assert sv is not None
        assert sv.reveal() == "prompted-value"

    def test_set_escapes_control_chars_in_prompt_and_confirmation(
        self, tmp_path: Path, capsys
    ) -> None:
        """A control-char name is escaped in BOTH the getpass prompt and the
        stored-confirmation line — a dashboard-set name must not be able to
        rewrite the operator's terminal when echoed."""
        from kiro_crew.cli import _secrets

        raw = "EVIL\x1b]0;pwn\x07KEY"

        class Args:
            secrets_action = "set"
            name = raw
            stdin = False

        captured_prompt = {}

        def _fake_getpass(prompt: str = "") -> str:
            captured_prompt["p"] = prompt
            return "prompted-value"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("getpass.getpass", _fake_getpass),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            _secrets(Args())

        # Prompt: escaped form present, raw ESC/BEL bytes gone.
        assert "\\x1b" in captured_prompt["p"]
        assert "\x1b" not in captured_prompt["p"]
        assert "\x07" not in captured_prompt["p"]

        # Confirmation line: same guarantee.
        out = capsys.readouterr().out
        assert "\\x1b" in out
        assert "\x1b" not in out
        assert "\x07" not in out

        # The RAW name was still stored (escaping is display-only).
        assert SecretVault(tmp_path).get(raw) is not None


class TestSecretsRmCommand:
    """Tests for `kirocrew secrets rm`."""

    def test_rm_existing_secret(self, vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "deleted" in out.lower()

        vault = SecretVault(vault_dir)
        assert vault.get("API_KEY") is None

    def test_rm_escapes_control_chars_in_confirmation(self, tmp_path: Path, capsys) -> None:
        """The rm confirmation escapes a control-char name (display-only)."""
        import asyncio

        from kiro_crew.cli import _secrets

        raw = "EVIL\x1b[2JKEY"
        asyncio.run(SecretVault(tmp_path).set(raw, "v"))

        class Args:
            secrets_action = "rm"
            name = raw

        with patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "deleted" in out.lower()
        assert "\\x1b" in out
        assert "\x1b" not in out
        # The RAW name was still deleted.
        assert SecretVault(tmp_path).get(raw) is None

    def test_rm_nonexistent_is_noop(self, vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "rm"
            name = "MISSING"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        # Should not error
        out = capsys.readouterr().out
        assert "deleted" in out.lower()


class TestSecretsNoAction:
    """Tests for `kirocrew secrets` with no subcommand."""

    def test_shows_usage(self, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = None

        with patch("kiro_crew.cli.config_dir", return_value="/tmp"):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "Usage" in out or "usage" in out.lower()
