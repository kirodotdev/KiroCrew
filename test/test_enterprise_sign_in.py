"""The enterprise build's one sign-in (RFC D4): Crew's own vault sign-in.

The vault is a real ``TokenStore`` in a temporary directory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew import enterprise_sign_in as sign_in
from kiro_crew.auth.store import KasToken, TokenStore

_ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEFGHIJKL"
_OTHER_ARN = "arn:aws:codewhisperer:us-east-1:210987654321:profile/ZYXWVUTSRQPO"


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokenStore:
    store = TokenStore(tmp_path / "vault")
    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: store)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    return store


@pytest.fixture(name="vault")
def vault_fixture(store: TokenStore):
    def save(
        profile_arn: str | None = _ARN,
        *,
        auth_method: str = "identity_center",
        expires_in: float = 3600,
        refresh_token: str | None = "refresh",
        start_url: str = "",
        region: str | None = None,
    ) -> None:
        store.save(
            KasToken(
                access_token="vault-access",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                provider="Enterprise",
                identity=auth_method,
                refresh_token=refresh_token,
                profile_arn=profile_arn,
                auth_method=auth_method,
                token_endpoint="https://idp.example.invalid/token",
                region=region,
                extra={"start_url": start_url} if start_url else {},
            )
        )

    return save


@pytest.fixture(name="unpinned")
def unpinned_fixture(monkeypatch: pytest.MonkeyPatch):
    from kiro_crew.platform import enterprise_policy_fetch as epf

    monkeypatch.setattr(epf, "_pinned", "")
    monkeypatch.delenv(epf.PROFILE_PIN_ENV, raising=False)
    monkeypatch.setattr(epf, "cache_only_process", lambda: False)
    return epf


def _cause(**kwargs) -> str:
    with pytest.raises(sign_in.SignInError) as exc:
        sign_in.resolve(**kwargs)
    return exc.value.cause


class TestSelection:
    def test_a_vault_sign_in_is_the_sign_in(self, vault) -> None:
        vault()
        found = sign_in.resolve()
        assert (found.source, found.profile_arn, found.token) == (sign_in.Source.VAULT, _ARN, "")

    def test_the_token_read_returns_the_vault_token(self, vault) -> None:
        vault()
        found = sign_in.resolve(with_token=True)
        assert (found.token, found.profile_arn, found.token_type) == ("vault-access", _ARN, "")

    def test_an_external_idp_sign_in_sends_its_token_type(self, vault) -> None:
        vault(auth_method="external_idp")
        assert sign_in.resolve(with_token=True).token_type == sign_in.TOKEN_TYPE_EXTERNAL_IDP

    def test_an_empty_vault_is_not_signed_in(self, store) -> None:
        with pytest.raises(sign_in.SignInError) as exc:
            sign_in.resolve()
        assert exc.value.cause == sign_in.CAUSE_NO_SIGN_IN
        assert "kas login --provider idc" in str(exc.value)

    def test_no_profile_is_not_an_organisation_account(self, vault) -> None:
        vault(None, auth_method="external_idp")
        assert _cause() == sign_in.CAUSE_NOT_ELIGIBLE

    def test_an_unreadable_vault_fails_closed(self, monkeypatch) -> None:
        class Broken:
            def holds_no_identity(self) -> bool:
                raise OSError("unreadable")

        monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: Broken())
        assert _cause() == sign_in.CAUSE_SIGN_IN_UNUSABLE

    def test_the_result_is_frozen_and_never_prints_the_token(self, vault) -> None:
        vault()
        found = sign_in.resolve(with_token=True)
        assert "vault-access" not in repr(found)
        with pytest.raises(AttributeError):
            found.profile_arn = _OTHER_ARN  # type: ignore[misc]

    def test_no_kiro_cli_store_is_read(self, store) -> None:
        # Vault only in this change; the kiro-cli fallback lands in a follow-up.
        assert list(sign_in.Source) == [sign_in.Source.VAULT]
        assert not any("kiro_cli" in name.lower() for name in vars(sign_in))


class TestLapsed:
    def test_a_lapsed_vault_is_not_signed_in_but_names_its_account(self, vault) -> None:
        vault(expires_in=-3600, refresh_token=None, auth_method="external_idp")
        chosen = sign_in.select()
        assert chosen.profile_arn == _ARN and chosen.problem is not None
        with pytest.raises(sign_in.SignInError) as exc:
            sign_in.resolve(with_token=True)
        assert exc.value.cause == sign_in.CAUSE_SIGN_IN_UNUSABLE
        assert "kas login --provider idc" in str(exc.value)

    def test_a_refused_refresh_is_unusable(self, vault, store) -> None:
        vault(expires_in=-3600)
        store.mark_refresh_rejected("identity_center")
        assert _cause() == sign_in.CAUSE_SIGN_IN_UNUSABLE

    def test_an_expired_token_not_yet_refused_still_counts(self, vault) -> None:
        vault(expires_in=-3600)
        assert sign_in.resolve().source == sign_in.Source.VAULT


class TestAccountLock:
    def test_a_lapsed_sign_in_is_not_an_account_change(self, vault, unpinned) -> None:
        epf = unpinned
        vault()
        pin = epf.pinned_scope()
        vault(expires_in=-3600, refresh_token=None, auth_method="external_idp")
        assert epf.signed_in_scope() == pin
        epf.assert_signed_in_unchanged()  # does not raise, does not stop the gateway
        with pytest.raises(epf.EnterprisePolicyError) as exc:
            epf.signed_in_identity()
        assert exc.value.cause == sign_in.CAUSE_SIGN_IN_UNUSABLE

    def test_an_unreadable_vault_is_not_an_account_change(
        self, vault, unpinned, monkeypatch
    ) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        epf = unpinned
        stops: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.platform.policy_distribution.central_ceiling_installed", lambda: True
        )
        monkeypatch.setattr(epf.signal, "raise_signal", lambda sig: stops.append(sig))
        vault()
        epf.pinned_scope()

        class Broken:
            def holds_no_identity(self) -> bool:
                raise OSError("unreadable")

        monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: Broken())
        with pytest.raises(PlatformCompositionError) as exc:
            epf.assert_signed_in_unchanged()
        assert epf.ACCOUNT_CHANGED not in str(exc.value)
        assert stops == []

    def test_a_never_pinned_start_is_never_called_an_account_change(
        self, vault, unpinned, monkeypatch
    ) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        epf = unpinned
        monkeypatch.setattr(epf, "signed_in_scope", lambda: "")  # pin read raced a sign-in
        vault()
        with pytest.raises(PlatformCompositionError) as exc:
            epf.assert_signed_in_unchanged()
        assert epf.ACCOUNT_CHANGED not in str(exc.value)

    def test_the_lock_and_the_fetch_name_the_same_account(self, vault) -> None:
        from kiro_crew.platform import enterprise_policy_fetch as epf

        vault()
        assert epf.signed_in_scope() == epf.profile_scope(epf.signed_in_identity().profile_arn)


class TestLoginSource:
    @pytest.mark.asyncio
    async def test_it_follows_a_sign_out(self, vault, store) -> None:
        vault()
        source = sign_in.SignInLoginSource()
        assert source.authenticated
        store.delete("identity_center")
        assert await source.login_verified(max_age_secs=0) is False
        assert not source.authenticated

    @pytest.mark.asyncio
    async def test_a_refusal_whose_marker_write_fails_still_closes_the_gate(
        self, vault, store, monkeypatch
    ) -> None:
        # A full disk: the marker file cannot be written, the refusal still counts.
        vault(expires_in=-3600)
        source = sign_in.SignInLoginSource()
        assert source.authenticated

        def full_disk(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_text", full_disk)
        store.mark_refresh_rejected("identity_center", "refresh")
        assert store.refresh_rejected("identity_center") is None  # no marker landed
        assert await source.login_verified(max_age_secs=0) is False

    @pytest.mark.asyncio
    async def test_a_new_sign_in_after_a_refusal_in_memory_is_not_refused(
        self, vault, store, monkeypatch
    ) -> None:
        vault(expires_in=-3600)

        def full_disk(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        # Only the disk stub is scoped; the fixtures' own patches stay in place.
        with monkeypatch.context() as disk:
            disk.setattr(Path, "write_text", full_disk)
            store.mark_refresh_rejected("identity_center", "refresh")
        vault(expires_in=-3600, refresh_token="a-new-refresh")
        assert sign_in.SignInLoginSource().authenticated

    @pytest.mark.asyncio
    async def test_an_unreadable_refusal_marker_closes_the_gate(self, vault, store) -> None:
        vault(expires_in=-3600)
        source = sign_in.SignInLoginSource()
        assert source.authenticated
        # A marker that exists but cannot be read is not "not refused".
        store._refresh_rejected_path("identity_center").mkdir(parents=True)
        assert await source.login_verified(max_age_secs=0) is False

    def test_kas_login_and_the_gate_agree_on_an_empty_refusal_marker(self, vault, store) -> None:
        # A refusal write cut short leaves an empty marker: the gate closes, and
        # `kas login` must not answer "already signed in" over it.
        from kiro_crew.auth.whoami import resolve_signed_in

        vault(expires_in=-3600)
        path = store._refresh_rejected_path("identity_center")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        assert not sign_in.SignInLoginSource().authenticated
        assert resolve_signed_in(store) is None

    @pytest.mark.asyncio
    async def test_a_refused_refresh_closes_the_gate(self, vault, store) -> None:
        vault(expires_in=-3600)
        source = sign_in.SignInLoginSource()
        assert source.authenticated
        store.mark_refresh_rejected("identity_center")
        assert await source.login_verified(max_age_secs=0) is False

    @pytest.mark.asyncio
    async def test_an_account_switch_closes_the_gate(self, vault, unpinned) -> None:
        vault(_ARN)
        source = sign_in.SignInLoginSource()
        assert source.authenticated  # pins _ARN
        vault(_OTHER_ARN)
        assert await source.login_verified(max_age_secs=0) is False

    @pytest.mark.asyncio
    async def test_a_new_sign_in_reopens_on_the_next_check(self, vault, store) -> None:
        source = sign_in.SignInLoginSource()
        assert not source.authenticated
        # The closed watch's own budget is 60 s; a sign-in must not wait it out.
        assert await source.login_verified(max_age_secs=60) is False
        vault()
        assert await source.login_verified(max_age_secs=60) is True

    @pytest.mark.asyncio
    async def test_a_signed_in_source_keeps_its_read_within_the_age(self, vault, store) -> None:
        vault()
        source = sign_in.SignInLoginSource()
        store.delete("identity_center")
        assert await source.login_verified(max_age_secs=60) is True

    def test_the_sign_in_command_never_carries_a_placeholder(self, store) -> None:
        # The shell reads `<` and `>` as redirects, so a copied placeholder fails.
        for text in (
            sign_in.vault_sign_in_command(),
            sign_in.sign_in_hint(),
            sign_in.sign_in_command()[0],
        ):
            assert "<" not in text and ">" not in text
        assert sign_in.sign_in_command() == (sign_in.vault_sign_in_command(), False)

    def test_the_sign_in_command_names_this_installs_launcher(self, store, monkeypatch) -> None:
        # A desktop AppImage puts no `kirocrew` on PATH: the bare name would fail.
        launcher = "/tmp/.mount_Kiro Crew/usr/bin/kirocrew"
        monkeypatch.setattr(sign_in.sys, "platform", "linux")
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: launcher)
        command, _ = sign_in.sign_in_command()
        assert command == f"'{launcher}' kas login --provider idc --start-url"
        assert sign_in.sign_in_hint().startswith(f"`'{launcher}' kas login")
        assert "<" not in command and ">" not in command

    def test_posix_renders_with_shlex_join(self) -> None:
        argv = ["/opt/Kiro Crew/bin/kirocrew", "kas", "login", "--start-url", "https://a/start"]
        assert sign_in.render_command(argv, platform="linux") == (
            "'/opt/Kiro Crew/bin/kirocrew' kas login --start-url https://a/start"
        )

    def test_windows_renders_a_powershell_call_for_a_path_with_spaces(self) -> None:
        # A quoted path alone is a string to PowerShell; `&` runs it.
        argv = [r"C:\Program Files\Kiro Crew\kirocrew.exe", "kas", "login", "--provider", "idc"]
        assert sign_in.render_command(argv, platform="win32") == (
            r"& 'C:\Program Files\Kiro Crew\kirocrew.exe' kas login --provider idc"
        )

    def test_windows_doubles_a_quote_inside_the_path(self) -> None:
        argv = [r"C:\Users\O'Brien\kirocrew.exe", "kas", "login"]
        assert sign_in.render_command(argv, platform="win32") == (
            r"& 'C:\Users\O''Brien\kirocrew.exe' kas login"
        )

    def test_windows_command_for_this_install_is_the_powershell_form(
        self, store, monkeypatch
    ) -> None:
        monkeypatch.setattr(sign_in.sys, "platform", "win32")
        monkeypatch.setattr(
            "kiro_crew.agent._resolve_kirocrew_bin", lambda: r"C:\Program Files\Kiro\kirocrew.exe"
        )
        assert sign_in.vault_sign_in_command() == (
            r"& 'C:\Program Files\Kiro\kirocrew.exe' kas login --provider idc --start-url"
        )

    def test_the_sign_in_command_falls_back_to_the_bare_name(self, store, monkeypatch) -> None:
        def broken() -> str:
            raise OSError("no install")

        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", broken)
        # Rendered for a POSIX shell here; the Windows form has its own tests.
        monkeypatch.setattr(sign_in.sys, "platform", "linux")
        assert sign_in.sign_in_command() == (sign_in.BARE_SIGN_IN_COMMAND, False)

    def test_the_sign_in_command_names_the_recorded_start_url(self, vault) -> None:
        # A lapsed sign-in still records its start URL, so the command runs as copied.
        url = "https://acme.awsapps.com/start"
        vault(expires_in=-3600, refresh_token=None, start_url=url)
        assert sign_in.sign_in_command() == (f"{sign_in.vault_sign_in_command()} {url}", True)

    def test_the_sign_in_command_keeps_the_stored_region(self, vault) -> None:
        # Without --region, `kas login` starts the sign-in in us-east-1.
        url = "https://acme.awsapps.com/start"
        vault(expires_in=-3600, refresh_token=None, start_url=url, region="eu-west-1")
        command, complete = sign_in.sign_in_command()
        assert complete
        assert command == f"{sign_in.vault_sign_in_command()} {url} --region eu-west-1"
        assert "<" not in command and ">" not in command

    def test_an_unsafe_stored_region_is_left_out(self, vault) -> None:
        url = "https://acme.awsapps.com/start"
        vault(start_url=url, region="eu-west-1; echo hi")
        assert sign_in.sign_in_command() == (f"{sign_in.vault_sign_in_command()} {url}", True)

    def test_an_unsafe_recorded_start_url_is_never_put_in_the_command(self, vault) -> None:
        vault(start_url="https://x.example/start; echo hi > out")
        assert sign_in.sign_in_command() == (sign_in.vault_sign_in_command(), False)

    def test_describe_reports_the_sign_in(self, vault) -> None:
        vault()
        line = sign_in.describe()
        assert line.startswith("✅") and "Kiro Crew's own sign-in" in line and _ARN in line

    def test_describe_reports_no_sign_in(self, store) -> None:
        assert sign_in.describe().startswith("❌ no Kiro sign-in was found")


class TestDoctor:
    def test_an_enterprise_doctor_reports_the_sign_in(self, vault, monkeypatch, capsys) -> None:
        from kiro_crew import cli_doctor

        monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        vault()
        issues: list[str] = []
        cli_doctor._doctor_enterprise_sign_in(issues)
        out = capsys.readouterr().out
        assert "sign-in:" in out and "Kiro Crew's own sign-in" in out and _ARN in out
        assert issues == []

    def test_an_enterprise_doctor_with_no_sign_in_is_an_issue(
        self, store, monkeypatch, capsys
    ) -> None:
        from kiro_crew import cli_doctor

        monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        issues: list[str] = []
        cli_doctor._doctor_enterprise_sign_in(issues)
        assert "no Kiro sign-in was found" in capsys.readouterr().out
        assert issues and issues[0].startswith("enterprise sign-in: no Kiro sign-in")

    def test_an_oss_doctor_prints_no_sign_in_row(self, store, capsys) -> None:
        from kiro_crew import cli_doctor

        issues: list[str] = []
        cli_doctor._doctor_enterprise_sign_in(issues)
        assert capsys.readouterr().out == "" and issues == []


class TestEditionFree:
    def test_the_resolver_does_not_read_the_edition(self) -> None:
        """Callers decide when it applies, so an oss caller can reuse it as-is."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(sign_in))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert "edition" not in names and "kiro_crew.edition" not in names

    def test_the_resolver_answers_in_an_oss_build(self, vault) -> None:
        vault()
        assert sign_in.select().source == sign_in.Source.VAULT  # EDITION is oss here

    @pytest.mark.asyncio
    async def test_the_gate_wiring_builds_no_source_in_oss(self) -> None:
        from kiro_crew.dashboard.enterprise_gate_middleware import enterprise_login_source

        assert await enterprise_login_source() is None
