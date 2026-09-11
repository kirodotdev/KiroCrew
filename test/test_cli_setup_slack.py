"""``kirocrew setup --slack`` — the tokens are checked BEFORE they are stored.

The wizard asks Slack about every value at the moment it is pasted, so a typo, a
revoked token, or a channel ID pasted where the member ID belongs is reported at
the prompt rather than hours later as a "Slack disabled" line in the gateway
log. The properties that matter are:

* a token Slack REJECTS is reported with Slack's own code and re-asked, and the
  value that finally lands in ``.env`` is the accepted one;
* a value Slack keeps refusing replaces nothing on disk;
* a member ID is format-checked with no network at all — ``C…`` is a channel and
  ``B…`` is a bot — while ``W…`` (Enterprise Grid) is a valid member ID and must
  never be refused;
* an UNREACHABLE Slack is not a rejection: being offline must not cost the
  operator the credentials they just typed;
* only ``user_not_found`` / ``users_not_found`` indict a member ID; a missing
  ``users:read`` scope indicts the CHECK, so it degrades to unverifiable rather
  than refusing the operator's own ID;
* the check runs only at a terminal — off one nobody can see the verdict, and
  re-asking would eat the next line of a piped answer file and misassign every
  remaining answer, so the step stays exactly as it was;
* the wizard offers to restart only a gateway that is ALREADY running, and
  survives a service manager that refuses the restart — the tokens are already
  on disk by then.

Every Slack call is stubbed at ``slack_sdk`` (the helpers import it lazily, so
patching the module attribute is enough): no test here reaches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError

import kiro_crew.cli_setup as cs
from kiro_crew.config.loader import CRED_OWNER_ID, CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN


class _FakeWebClient:
    """Accepts everything, and records what was asked of Slack.

    Subclasses override one method to make Slack refuse (``_api_error``) or go
    missing (``OSError``) for exactly that call.
    """

    calls: list[tuple[str, str]] = []

    def __init__(self, token: str = "", timeout: int = 0) -> None:
        self.token = token

    def apps_connections_open(self, app_token: str) -> dict[str, object]:
        type(self).calls.append(("apps_connections_open", app_token))
        return {"ok": True}

    def auth_test(self) -> dict[str, object]:
        type(self).calls.append(("auth_test", self.token))
        return {"ok": True, "team": "Zibble Corp", "team_id": "T1"}

    def users_info(self, user: str) -> dict[str, object]:
        type(self).calls.append(("users_info", user))
        return {"ok": True, "user": {"real_name": "Ada Lovelace", "name": "ada"}}


def _api_error(code: str) -> SlackApiError:
    """What ``slack_sdk`` raises when Slack answers ``ok: false``."""
    return SlackApiError("nope", {"ok": False, "error": code})


def _install(monkeypatch: pytest.MonkeyPatch, client: type[_FakeWebClient]) -> None:
    """Put *client* in place of the ``WebClient`` name ``cli_setup`` actually calls.

    Patched at the CONSUMER, not at ``slack_sdk.web.WebClient``: ``cli_setup``
    imports the class at module scope (per the top-level-imports convention), so it
    holds its own reference and patching the source module would silently no-op --
    the check would then run against real Slack and the assertion would read
    ``None``.
    """
    monkeypatch.setattr("kiro_crew.cli_setup.WebClient", client)


@pytest.fixture()
def fake_slack(monkeypatch: pytest.MonkeyPatch) -> type[_FakeWebClient]:
    """An all-accepting Slack whose calls the test can assert on."""

    class Client(_FakeWebClient):
        calls: list[tuple[str, str]] = []

    _install(monkeypatch, Client)
    return Client


@pytest.fixture()
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the credential writer at a tmp ``.env``."""
    path = tmp_path / ".env"
    monkeypatch.setattr(cs, "env_path", lambda: path)
    return path


def _answers(monkeypatch: pytest.MonkeyPatch, *lines: str) -> list[str]:
    """Feed ``input()`` from *lines*; returns the prompts that were shown."""
    prompts: list[str] = []
    pending = list(lines)

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        if not pending:
            raise AssertionError(f"the wizard asked one question too many: {prompt!r}")
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", _input)
    return prompts


def _tty(monkeypatch: pytest.MonkeyPatch, interactive: bool) -> None:
    """Decide what ``_stdio_is_interactive()`` sees.

    Patched at ``sys.stdin``/``sys.stdout`` rather than at the predicate so the
    tests exercise the real one — and so a run with pytest's capture disabled
    (where stdin really IS a terminal) cannot flip the branch under them.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: interactive, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: interactive, raising=False)


@pytest.fixture(autouse=True)
def no_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may bounce the developer's own gateway.

    Patched at the primitives the offer consults — no active service, no
    listener — so the offer itself still runs for real (and takes its "nothing
    is running" branch), while an actual ``_restart`` would fail loudly.

    ``listening_pid_tool_available`` is pinned True as well, or every "nothing is
    running" assertion here would depend on whether the test host happens to have
    ``lsof`` under the trusted directories.
    """

    def _never() -> None:
        raise AssertionError("a test tried to restart the gateway")

    monkeypatch.setattr(cs, "_configured_port", lambda: 6473)
    monkeypatch.setattr("kiro_crew.service.controller.is_service_active", lambda: False)
    monkeypatch.setattr(cs.platform_compat, "find_listening_pids", lambda port: [])
    monkeypatch.setattr(cs.platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr("kiro_crew.cli_server._restart", _never)


class TestSetupSlackTokens:
    """End to end through the wizard step, with Slack stubbed."""

    def test_a_rejected_bot_token_is_re_asked_and_the_accepted_one_is_saved(
        self,
        env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The pre-check wizard wrote the typo and then read the NEXT answer as
        the member ID, shifting every remaining answer by one."""

        class Client(_FakeWebClient):
            def auth_test(self) -> dict[str, object]:
                if self.token == "xoxb-typo":
                    raise _api_error("invalid_auth")
                return {"ok": True, "team": "Zibble Corp"}

        _tty(monkeypatch, True)
        _install(monkeypatch, Client)
        _answers(monkeypatch, "y", "xapp-ok", "xoxb-typo", "xoxb-ok", "U03T18B4Y23")

        cs._setup_slack_tokens()

        saved = dict(
            line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines()
        )
        assert saved[CRED_SLACK_BOT_TOKEN] == "xoxb-ok"
        assert saved[CRED_SLACK_APP_TOKEN] == "xapp-ok"
        assert saved[CRED_OWNER_ID] == "U03T18B4Y23"
        out = capsys.readouterr().out
        assert "Slack rejected the bot token: invalid_auth" in out
        assert "workspace: Zibble Corp" in out
        assert "Owner verified: Ada Lovelace (U03T18B4Y23)" in out

    def test_a_channel_id_pasted_as_the_member_id_is_re_asked(
        self,
        env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The commonest paste error: a channel ID where the member ID goes.
        Stored as-is it made the bot answer nobody."""
        _tty(monkeypatch, True)
        _install(monkeypatch, _FakeWebClient)
        _answers(monkeypatch, "y", "xapp-ok", "xoxb-ok", "C03ABC2DEF3", "U03T18B4Y23")

        cs._setup_slack_tokens()

        content = env_file.read_text(encoding="utf-8")
        assert f"{CRED_OWNER_ID}=U03T18B4Y23" in content
        assert "C03ABC2DEF3" not in content
        assert "'C…' is a channel" in capsys.readouterr().out

    def test_three_rejections_save_nothing(
        self,
        env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A token Slack keeps refusing must not replace a working one on disk."""
        env_file.write_text(f"{CRED_SLACK_BOT_TOKEN}=xoxb-live\n", encoding="utf-8")

        class Client(_FakeWebClient):
            def apps_connections_open(self, app_token: str) -> dict[str, object]:
                raise _api_error("invalid_auth")

        _tty(monkeypatch, True)
        _install(monkeypatch, Client)
        _answers(monkeypatch, "y", "xapp-1", "xapp-2", "xapp-3")

        cs._setup_slack_tokens()

        assert env_file.read_text(encoding="utf-8") == f"{CRED_SLACK_BOT_TOKEN}=xoxb-live\n"
        assert "nothing was saved" in capsys.readouterr().out

    def test_an_unreachable_slack_saves_the_tokens_as_typed(
        self,
        env_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Being offline must not cost the operator what they just typed."""

        class Client(_FakeWebClient):
            def apps_connections_open(self, app_token: str) -> dict[str, object]:
                raise OSError("no route to host")

            def auth_test(self) -> dict[str, object]:
                raise OSError("no route to host")

            def users_info(self, user: str) -> dict[str, object]:
                raise OSError("no route to host")

        _tty(monkeypatch, True)
        _install(monkeypatch, Client)
        _answers(monkeypatch, "y", "xapp-ok", "xoxb-ok", "U03T18B4Y23")

        cs._setup_slack_tokens()

        content = env_file.read_text(encoding="utf-8")
        assert "xoxb-ok" in content and "U03T18B4Y23" in content
        assert "Could not reach Slack" in capsys.readouterr().out

    def test_off_a_terminal_slack_is_never_called_and_the_step_is_unchanged(
        self,
        env_file: Path,
        fake_slack: type[_FakeWebClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A verdict nobody can see cannot be acted on, and re-asking would eat
        the next line of a piped answer file and misassign every later answer.
        So an automated run (``kirocrew update`` re-runs setup with its output
        captured and stdin on /dev/null) stays byte-for-byte the pre-check
        behaviour: no Slack round-trip, no extra prompt, tokens written as
        typed."""
        _tty(monkeypatch, False)
        prompts = _answers(monkeypatch, "y", "xapp-ok", "xoxb-typo", "U03T18B4Y23")

        cs._setup_slack_tokens()

        assert fake_slack.calls == []
        assert len(prompts) == 4  # nothing was asked twice
        content = env_file.read_text(encoding="utf-8")
        assert "xoxb-typo" in content and "U03T18B4Y23" in content

    def test_declining_the_step_asks_slack_nothing(
        self, env_file: Path, fake_slack: type[_FakeWebClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answers(monkeypatch, "n")

        cs._setup_slack_tokens()

        assert not env_file.exists()
        assert fake_slack.calls == []


class TestVerifySlackSecret:
    """The token check mirrors the dashboard's: two calls, three verdicts."""

    def test_an_app_token_is_checked_with_the_call_the_gateway_makes(
        self, fake_slack: type[_FakeWebClient]
    ) -> None:
        assert cs._verify_slack_secret(CRED_SLACK_APP_TOKEN, "xapp-1-A-2-b") == (True, "")
        assert fake_slack.calls == [("apps_connections_open", "xapp-1-A-2-b")]

    def test_an_accepted_bot_token_names_the_workspace(
        self, fake_slack: type[_FakeWebClient]
    ) -> None:
        assert cs._verify_slack_secret(CRED_SLACK_BOT_TOKEN, "xoxb-good") == (True, "Zibble Corp")
        assert fake_slack.calls == [("auth_test", "xoxb-good")]

    def test_a_workspace_with_no_name_falls_back_to_its_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Client(_FakeWebClient):
            def auth_test(self) -> dict[str, object]:
                return {"ok": True, "team_id": "T0ZIBBLE"}

        _install(monkeypatch, Client)
        assert cs._verify_slack_secret(CRED_SLACK_BOT_TOKEN, "xoxb-good") == (True, "T0ZIBBLE")

    def test_slacks_own_error_code_is_the_rejection_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Client(_FakeWebClient):
            def auth_test(self) -> dict[str, object]:
                raise _api_error("invalid_auth")

        _install(monkeypatch, Client)
        assert cs._verify_slack_secret(CRED_SLACK_BOT_TOKEN, "xoxb-typo") == (
            False,
            "invalid_auth",
        )

    def test_an_unreachable_slack_is_unverifiable_not_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Client(_FakeWebClient):
            def auth_test(self) -> dict[str, object]:
                raise OSError("no route to host")

        _install(monkeypatch, Client)
        assert cs._verify_slack_secret(CRED_SLACK_BOT_TOKEN, "xoxb-good") == (None, "OSError")


class TestVerifySlackOwnerId:
    """The member-ID check: format first, then existence."""

    @pytest.mark.parametrize("pasted", ["C03ABC2DEF3", "B01234567", "u0123456", "", "U" * 40])
    def test_a_non_member_id_is_refused_without_calling_slack(
        self, pasted: str, fake_slack: type[_FakeWebClient]
    ) -> None:
        verdict, detail = cs._verify_slack_owner_id("xoxb-good", pasted)
        assert verdict is False
        assert "channel" in detail and "bot" in detail
        assert fake_slack.calls == []  # the paste error needs no network

    @pytest.mark.parametrize("pasted", ["U03T18B4Y23", "W03T18B4Y23"])
    def test_a_u_or_grid_w_member_id_is_accepted_and_named(
        self, pasted: str, fake_slack: type[_FakeWebClient]
    ) -> None:
        """``W…`` is what Enterprise Grid issues; refusing it would lock out
        every Grid operator."""
        assert cs._verify_slack_owner_id("xoxb-good", pasted) == (True, "Ada Lovelace")
        assert fake_slack.calls == [("users_info", pasted)]

    @pytest.mark.parametrize("code", sorted(cs._SLACK_OWNER_REJECTIONS))
    def test_a_member_slack_does_not_know_is_refused(
        self, code: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every documented rejection code refuses the ID — Slack uses the
        singular form for ``users.info``, the plural is carried defensively."""

        class Client(_FakeWebClient):
            def users_info(self, user: str) -> dict[str, object]:
                raise _api_error(code)

        _install(monkeypatch, Client)
        verdict, detail = cs._verify_slack_owner_id("xoxb-good", "U03T18B4Y23")
        assert verdict is False
        assert code in detail

    def test_a_missing_users_read_scope_indicts_the_check_not_the_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scope the operator never granted must not cost them their own ID."""

        class Client(_FakeWebClient):
            def users_info(self, user: str) -> dict[str, object]:
                raise _api_error("missing_scope")

        _install(monkeypatch, Client)
        assert cs._verify_slack_owner_id("xoxb-good", "U03T18B4Y23") == (None, "missing_scope")


class TestRejectedOwnerIdKeepsVerifiedTokens:
    """A rejected OPTIONAL field must not cost the operator a verified REQUIRED one.

    The member ID is optional at write time, so abandoning the save over it
    discarded two tokens Slack had just confirmed -- the one outcome the
    verify-as-you-type flow exists to prevent.

    Every test here takes ``env_file``. That fixture is not decoration: these tests
    drive the real credential WRITE, and the writer resolves its target through
    ``cs.env_path()``. Patching ``cs.KIROCREW_HOME`` does not redirect that, so
    without ``env_file`` the write lands in the developer's own
    ``~/.kiro/crew/.env`` and replaces live Slack tokens with these fakes.
    """

    def test_tokens_are_written_and_owner_is_left_unset(
        self, env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        class Client(_FakeWebClient):
            def users_info(self, user: str):  # noqa: D102 - fake
                raise _api_error("user_not_found")

        _install(monkeypatch, Client)
        _tty(monkeypatch, True)
        # The `env_file` fixture is what actually redirects the write: the writer
        # resolves its target through `cs.env_path()`, so patching `cs.KIROCREW_HOME`
        # alone leaves it pointed at the developer's REAL ~/.kiro/crew/.env — and
        # this test writes Slack tokens, so it would overwrite live credentials.
        monkeypatch.setattr(cs, "KIROCREW_HOME", tmp_path, raising=False)

        # A supplier rather than a fixed iterator: the member ID is re-asked
        # `_SLACK_VERIFY_ATTEMPTS` times, and a short list would raise StopIteration
        # mid-prompt and mask the branch under test.
        def answer(prompt: str = "") -> str:
            if "App-Level Token" in prompt or "App Token" in prompt or "xapp" in prompt:
                return "xapp-1-token"
            if "Bot" in prompt:
                return "xoxb-token"
            return "U01234567"

        monkeypatch.setattr("builtins.input", answer)

        cs._setup_slack_tokens()

        out = capsys.readouterr().out
        assert "saving the verified tokens without it" in out
        assert "nothing was saved" not in out

    def test_a_stale_owner_invalid_for_the_new_tokens_is_dropped(
        self, env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A rejected member ID must not leave the PREVIOUS one paired with NEW tokens.

        The merge re-reads .env under the lock, so an existing ``KIROCREW_OWNER_ID``
        survives unless something removes it. Pointed at a different workspace, that
        pairs fresh credentials with an owner who does not belong to them.
        """

        class Client(_FakeWebClient):
            def users_info(self, user: str):  # noqa: D102 - fake
                # Indicts BOTH the typed id and the stale one: the stale owner is
                # not a member of the workspace the new tokens belong to.
                raise _api_error("user_not_found")

        _install(monkeypatch, Client)
        _tty(monkeypatch, True)
        # The `env_file` fixture is what actually redirects the write: the writer
        # resolves its target through `cs.env_path()`, so patching `cs.KIROCREW_HOME`
        # alone leaves it pointed at the developer's REAL ~/.kiro/crew/.env — and
        # this test writes Slack tokens, so it would overwrite live credentials.
        monkeypatch.setattr(cs, "KIROCREW_HOME", tmp_path, raising=False)
        env = env_file
        env.write_text(
            "KIROCREW_SLACK_APP_TOKEN=xapp-old\n"
            "KIROCREW_SLACK_BOT_TOKEN=xoxb-old\n"
            "KIROCREW_OWNER_ID=U0STALEOWNER\n",
            encoding="utf-8",
        )

        def answer(prompt: str = "") -> str:
            if "App-Level Token" in prompt or "App Token" in prompt or "xapp" in prompt:
                return "xapp-1-token"
            if "Bot" in prompt:
                return "xoxb-token"
            return "U01234567"

        monkeypatch.setattr("builtins.input", answer)

        cs._setup_slack_tokens()

        written = env.read_text(encoding="utf-8")
        assert (
            "U0STALEOWNER" not in written
        ), "the previous owner ID survived beside brand-new workspace tokens"
        assert "xoxb-token" in written, "the verified tokens were lost"
        assert "Also dropped the saved member ID" in capsys.readouterr().out

    def test_a_stale_owner_still_valid_for_the_new_tokens_is_kept(
        self, env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """Guard the guard: a mistyped entry on the SAME workspace must not cost the
        operator an owner ID that is still correct, or the fix above would trade one
        data loss for another."""

        class Client(_FakeWebClient):
            def users_info(self, user: str):  # noqa: D102 - fake
                if user == "U0GOODOWNER":
                    return {"ok": True, "user": {"real_name": "Real Owner"}}
                raise _api_error("user_not_found")

        _install(monkeypatch, Client)
        _tty(monkeypatch, True)
        # The `env_file` fixture is what actually redirects the write: the writer
        # resolves its target through `cs.env_path()`, so patching `cs.KIROCREW_HOME`
        # alone leaves it pointed at the developer's REAL ~/.kiro/crew/.env — and
        # this test writes Slack tokens, so it would overwrite live credentials.
        monkeypatch.setattr(cs, "KIROCREW_HOME", tmp_path, raising=False)
        env = env_file
        env.write_text("KIROCREW_OWNER_ID=U0GOODOWNER\n", encoding="utf-8")

        def answer(prompt: str = "") -> str:
            if "App-Level Token" in prompt or "App Token" in prompt or "xapp" in prompt:
                return "xapp-1-token"
            if "Bot" in prompt:
                return "xoxb-token"
            return "U0TYPO9999"

        monkeypatch.setattr("builtins.input", answer)

        cs._setup_slack_tokens()

        assert "U0GOODOWNER" in env.read_text(
            encoding="utf-8"
        ), "a still-valid owner ID was deleted because the operator mistyped once"
        assert "Keeping the saved member ID" in capsys.readouterr().out
