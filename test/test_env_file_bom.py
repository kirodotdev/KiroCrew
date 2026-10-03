"""A ``.env`` saved with a byte-order mark by default Windows tooling.

PowerShell 5.1's ``Out-File -Encoding utf8`` writes UTF-8 with a BOM, and its
default ``>`` / ``Out-File`` writes UTF-16LE with a BOM. Every reader and
in-place rewriter of the data home's ``.env`` must agree on the first key of
such a file: the readers load it un-prefixed, the rewriters can clear or
replace it, and a UTF-16 or UTF-32 file is treated as unset instead of crashing
the load.
"""

from __future__ import annotations

import ast
import codecs
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig, read_env_file_credential

_BODY = "JIRA_API_TOKEN=abc123\r\nSLACK_BOT_TOKEN=xyz\r\n"


def _utf8_bom(tmp_path: Path) -> Path:
    ep = tmp_path / ".env"
    ep.write_bytes(codecs.BOM_UTF8 + _BODY.encode("utf-8"))
    return ep


_UTF16_BOM_FOR = {"utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE}


def _utf16_bom(tmp_path: Path, codec: str) -> Path:
    # Explicit byte order, so the big-endian branch is exercised on a
    # little-endian runner too (native "utf-16" would only ever write LE).
    ep = tmp_path / ".env"
    ep.write_bytes(_UTF16_BOM_FOR[codec] + _BODY.encode(codec))
    return ep


@pytest.fixture(params=sorted(_UTF16_BOM_FOR))
def utf16_codec(request: pytest.FixtureRequest) -> str:
    return request.param


_UTF32_BOM_FOR = {"utf-32-le": codecs.BOM_UTF32_LE, "utf-32-be": codecs.BOM_UTF32_BE}
_WIDE_BOM_FOR = {**_UTF16_BOM_FOR, **_UTF32_BOM_FOR}


def _wide_bom(tmp_path: Path, codec: str) -> Path:
    ep = tmp_path / ".env"
    ep.write_bytes(_WIDE_BOM_FOR[codec] + _BODY.encode(codec))
    return ep


@pytest.fixture(params=sorted(_UTF32_BOM_FOR))
def utf32_codec(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=sorted(_WIDE_BOM_FOR))
def wide_codec(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(autouse=True)
def _fresh_warning_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "_warned_undecodable_env", set(), raising=False)


def _load(monkeypatch: pytest.MonkeyPatch, ep: Path) -> dict[str, str]:
    monkeypatch.setattr(loader, "env_path", lambda: ep)
    # load_credentials overlays every recognised key from the environment, so
    # scrub them all or a shell exporting one would change the result.
    for key in (*loader.CREDENTIAL_KEYS, "JIRA_API_TOKEN", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return KiroCrewConfig.__new__(KiroCrewConfig).load_credentials(propagate=False)


class TestReaders:
    def test_utf8_bom_loads_the_first_key_unprefixed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        creds = _load(monkeypatch, _utf8_bom(tmp_path))
        assert creds["JIRA_API_TOKEN"] == "abc123"
        assert creds["SLACK_BOT_TOKEN"] == "xyz"
        assert not any(k.startswith("\ufeff") for k in creds)

    def test_utf8_bom_single_key_read(self, tmp_path: Path) -> None:
        assert read_env_file_credential("JIRA_API_TOKEN", _utf8_bom(tmp_path)) == "abc123"

    def test_utf16_is_unset_with_one_warning_naming_the_file(
        self,
        utf16_codec: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ep = _utf16_bom(tmp_path, utf16_codec)
        with caplog.at_level(logging.WARNING, logger=loader.logger.name):
            creds = _load(monkeypatch, ep)
            assert read_env_file_credential("JIRA_API_TOKEN", ep) == ""
        assert "JIRA_API_TOKEN" not in creds
        warnings = [r for r in caplog.records if "Cannot decode" in r.getMessage()]
        assert len(warnings) == 1
        assert str(ep) in warnings[0].getMessage()

    def test_a_file_fixed_then_broken_again_warns_again(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ep = tmp_path / ".env"
        broken = _BODY.encode("utf-16")
        with caplog.at_level(logging.WARNING, logger=loader.logger.name):
            ep.write_bytes(broken)
            assert read_env_file_credential("JIRA_API_TOKEN", ep) == ""
            ep.write_bytes(_BODY.encode("utf-8"))
            assert read_env_file_credential("JIRA_API_TOKEN", ep) == "abc123"
            ep.write_bytes(broken)
            assert read_env_file_credential("JIRA_API_TOKEN", ep) == ""
        warnings = [r for r in caplog.records if "Cannot decode" in r.getMessage()]
        assert len(warnings) == 2

    def test_plain_utf8_and_crlf_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ep = tmp_path / ".env"
        ep.write_bytes(_BODY.encode("utf-8"))
        assert _load(monkeypatch, ep) == {"JIRA_API_TOKEN": "abc123", "SLACK_BOT_TOKEN": "xyz"}
        assert read_env_file_credential("SLACK_BOT_TOKEN", ep) == "xyz"

    def test_a_bomless_file_invalid_in_the_locale_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only a UTF-16 BOM is treated as unset; any other decode error
        # propagates exactly as it did before BOM handling existed.
        ep = tmp_path / ".env"
        ep.write_bytes("JIRA_API_TOKEN=caf\u00e9\n".encode("utf-8"))
        monkeypatch.setattr(
            loader, "_locale", SimpleNamespace(getpreferredencoding=lambda _=False: "ascii")
        )
        with pytest.raises(UnicodeDecodeError) as excinfo:
            _load(monkeypatch, ep)
        assert not isinstance(excinfo.value, loader.EnvFileWideEncodingError)
        with pytest.raises(UnicodeDecodeError):
            read_env_file_credential("JIRA_API_TOKEN", ep)

    def test_both_utf16_byte_orders_raise_the_typed_error(self, utf16_codec: str) -> None:
        raw = _UTF16_BOM_FOR[utf16_codec] + _BODY.encode(utf16_codec)
        with pytest.raises(loader.EnvFileWideEncodingError):
            loader.decode_env_bytes(raw, "utf-8")

    def test_the_utf16_error_carries_no_credential_bytes(self, utf16_codec: str) -> None:
        # repr() and a traceback that captures locals print ``.object``.
        secret = "SLACK_BOT_TOKEN=xoxb-do-not-leak\n"
        raw = _UTF16_BOM_FOR[utf16_codec] + secret.encode(utf16_codec)
        with pytest.raises(loader.EnvFileWideEncodingError) as excinfo:
            loader.decode_env_bytes(raw, "utf-8")
        exc = excinfo.value
        assert exc.object == _UTF16_BOM_FOR[utf16_codec]
        assert (exc.start, exc.end) == (0, 2)
        for text in (repr(exc), str(exc)):
            assert "leak" not in text
            assert "xoxb" not in text
            assert "\\x00" not in text

    def test_a_utf32le_bom_is_classified_as_utf32_not_utf16(self) -> None:
        # FF FE 00 00 starts with the UTF-16LE BOM; it must get the UTF-32
        # verdict, not the UTF-16 one.
        raw = codecs.BOM_UTF32_LE + _BODY.encode("utf-32-le")
        with pytest.raises(loader.EnvFileWideEncodingError) as excinfo:
            loader.decode_env_bytes(raw, "utf-8")
        assert excinfo.value.encoding == "utf-32-le"
        assert excinfo.value.wide_encoding == "UTF-32"

    @pytest.mark.parametrize("locale_codec", ["utf-8", "cp1252"])
    def test_utf32_is_unset_with_one_warning_in_any_locale(
        self,
        utf32_codec: str,
        locale_codec: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A locale decode of a UTF-32 file either raises out of the load (a
        # strict locale) or yields NUL-riddled keys (cp1252); neither may happen.
        ep = _wide_bom(tmp_path, utf32_codec)
        monkeypatch.setattr(
            loader, "_locale", SimpleNamespace(getpreferredencoding=lambda _=False: locale_codec)
        )
        with caplog.at_level(logging.WARNING, logger=loader.logger.name):
            creds = _load(monkeypatch, ep)
            assert read_env_file_credential("JIRA_API_TOKEN", ep) == ""
        assert not any("\x00" in k for k in creds)
        assert "JIRA_API_TOKEN" not in creds
        warnings = [r for r in caplog.records if "Cannot decode" in r.getMessage()]
        assert len(warnings) == 1
        assert str(ep) in warnings[0].getMessage()
        assert "UTF-32" in warnings[0].getMessage()

    def test_the_wide_error_names_the_encoding_not_a_codec(self, wide_codec: str) -> None:
        raw = _WIDE_BOM_FOR[wide_codec] + _BODY.encode(wide_codec)
        with pytest.raises(loader.EnvFileWideEncodingError) as excinfo:
            loader.decode_env_bytes(raw, "utf-8")
        exc = excinfo.value
        family = "UTF-32" if wide_codec.startswith("utf-32") else "UTF-16"
        assert str(exc) == f"the .env is saved as {family}; re-save it as UTF-8"
        assert "codec" not in str(exc)
        assert exc.object == _WIDE_BOM_FOR[wide_codec]
        assert (exc.start, exc.end) == (0, len(_WIDE_BOM_FOR[wide_codec]))

    def test_service_warning_sees_a_bom_first_key(self, utf16_codec: str, tmp_path: Path) -> None:
        from kiro_crew.service.common import _names_defined_in_env_file

        assert "JIRA_API_TOKEN" in _names_defined_in_env_file(_utf8_bom(tmp_path))
        assert _names_defined_in_env_file(_utf16_bom(tmp_path, utf16_codec)) == set()

    def test_service_warning_treats_a_non_utf8_file_as_unreadable(self, tmp_path: Path) -> None:
        # A BOM-less file in a legacy code page must make the caller warn,
        # not raise into a handler that swallows the warning with it.
        from kiro_crew.service.common import _names_defined_in_env_file

        ep = tmp_path / ".env"
        ep.write_bytes("KIRO_API_KEY=caf\u00e9\n".encode("cp1252"))
        assert _names_defined_in_env_file(ep) == set()

    def test_service_warning_still_fires_for_a_wide_file(
        self, wide_codec: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wide file is unset to the gateway, so a key it holds is still
        # dropped and the install must say so instead of raising.
        from kiro_crew.service import common

        ep = tmp_path / ".env"
        ep.write_bytes(_WIDE_BOM_FOR[wide_codec] + "KIRO_API_KEY=x\r\n".encode(wide_codec))
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        warning = common.headless_auth_warning({"KIRO_API_KEY": "value-not-shown"})
        assert "KIRO_API_KEY is set in this shell" in warning
        assert "value-not-shown" not in warning


class TestRewriters:
    """A clear through a rewriter must remove the key the gateway loads."""

    def test_dashboard_channel_clear_removes_a_bom_first_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import messaging

        ep = _utf8_bom(tmp_path)
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        messaging._write_env_updates({"JIRA_API_TOKEN": None})
        assert read_env_file_credential("JIRA_API_TOKEN", ep) == ""
        assert read_env_file_credential("SLACK_BOT_TOKEN", ep) == "xyz"
        # Line endings follow the platform's text mode, so compare lines.
        after = ep.read_bytes()
        assert after.startswith(codecs.BOM_UTF8)
        assert after[3:].decode("utf-8").splitlines() == ["SLACK_BOT_TOKEN=xyz"]

    @pytest.mark.parametrize("rewriter", ["dashboard", "weixin-upsert", "weixin-delete", "setup"])
    def test_a_rewrite_keeps_the_bom_so_non_ascii_still_loads_in_any_locale(
        self, rewriter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The BOM is what makes the loader decode the file as UTF-8. A rewrite
        # that dropped it would leave a non-ASCII value to the locale codec,
        # which on a legacy Windows code page raises at the next gateway boot.
        import sys

        from kiro_crew import cli_setup
        from kiro_crew.dashboard.handlers import messaging
        from kiro_crew.dashboard.handlers import weixin_qr as qr

        ep = tmp_path / ".env"
        ep.write_bytes(codecs.BOM_UTF8 + "WEIXIN_TOKEN=old\nNOTE=caf\u00e9\n".encode("utf-8"))
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        monkeypatch.setattr(qr, "env_path", lambda: ep)
        monkeypatch.setattr(cli_setup, "env_path", lambda: ep)
        if rewriter == "dashboard":
            messaging._write_env_updates({"WEIXIN_TOKEN": "new"})
        elif rewriter == "weixin-upsert":
            qr._write_env_secret("WEIXIN_TOKEN", "new")
        elif rewriter == "weixin-delete":
            qr._delete_env_key("WEIXIN_TOKEN")
        else:
            answers = ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23"]
            monkeypatch.setattr("builtins.input", lambda _p="": answers.pop(0))
            monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
            monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
            cli_setup._setup_slack_tokens()
        assert ep.read_bytes().startswith(codecs.BOM_UTF8)
        assert not ep.read_bytes()[3:].startswith(codecs.BOM_UTF8)
        monkeypatch.setattr(
            loader, "_locale", SimpleNamespace(getpreferredencoding=lambda _=False: "ascii")
        )
        assert _load(monkeypatch, ep)["NOTE"] == "caf\u00e9"

    def test_a_rewrite_of_a_bomless_file_adds_no_bom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import messaging

        ep = tmp_path / ".env"
        ep.write_bytes(_BODY.encode("utf-8"))
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        messaging._write_env_updates({"JIRA_API_TOKEN": None})
        after = ep.read_bytes()
        assert not after.startswith(codecs.BOM_UTF8)
        assert after.decode("utf-8").splitlines() == ["SLACK_BOT_TOKEN=xyz"]

    def test_weixin_writers_match_a_bom_first_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import weixin_qr as qr

        ep = tmp_path / ".env"
        ep.write_bytes(codecs.BOM_UTF8 + b"WEIXIN_TOKEN=old\nOTHER=1\n")
        monkeypatch.setattr(qr, "env_path", lambda: ep)
        assert qr._read_env_value("WEIXIN_TOKEN") == "old"
        qr._write_env_secret("WEIXIN_TOKEN", "new")
        assert read_env_file_credential("WEIXIN_TOKEN", ep) == "new"
        assert ep.read_text(encoding="utf-8").count("WEIXIN_TOKEN=") == 1

        ep.write_bytes(codecs.BOM_UTF8 + b"WEIXIN_TOKEN=old\nOTHER=1\n")
        qr._delete_env_key("WEIXIN_TOKEN")
        assert read_env_file_credential("WEIXIN_TOKEN", ep) == ""

    def test_rewriter_refuses_a_wide_file_without_overwriting_it(
        self, wide_codec: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import messaging

        ep = _wide_bom(tmp_path, wide_codec)
        before = ep.read_bytes()
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        with pytest.raises(UnicodeDecodeError):
            messaging._write_env_updates({"JIRA_API_TOKEN": None})
        assert ep.read_bytes() == before


class TestDashboardChannelSave:
    """A channel save against a wide-encoded ``.env`` says how to fix it."""

    @pytest.mark.parametrize("channel", ["slack", "webex"])
    def test_a_wide_env_answers_409_with_the_remedy_and_changes_nothing(
        self, channel: str, wide_codec: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import json

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from test_channel_env_write_off_loop import CHANNELS

        from kiro_crew.dashboard.handlers import messaging

        name, handler_attr, validator_attr, body = next(c for c in CHANNELS if c[0] == channel)
        ep = _wide_bom(tmp_path, wide_codec)
        before = ep.read_bytes()
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(messaging, "is_direct_local_request", lambda req: True)

        async def _accept(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(messaging, validator_attr, _accept, raising=False)

        async def _run() -> tuple[int, str]:
            app = web.Application()
            app.router.add_put(f"/api/{name}/config", getattr(messaging, handler_attr))
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/{name}/config", json=body)
                return resp.status, await resp.text()

        status, text = asyncio.run(_run())
        assert status == 409, text
        payload = json.loads(text)
        family = "UTF-32" if wide_codec.startswith("utf-32") else "UTF-16"
        assert f"saved as {family}" in payload["error"]
        assert "Re-save it as UTF-8" in payload["error"]
        assert "was not changed" in payload["error"]
        assert ep.read_bytes() == before
        assert payload["error"].endswith("The .env was not changed.")

    @pytest.mark.parametrize("channel", ["discord", "telegram"])
    def test_a_save_that_keeps_its_config_says_the_other_settings_were_saved(
        self, channel: str, wide_codec: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # These two saves commit config before the .env write and keep it when
        # that write is refused, so the 409 must not read as "nothing saved".
        import asyncio
        import json

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from test_channel_env_write_off_loop import CHANNELS

        from kiro_crew.dashboard.handlers import messaging

        name, handler_attr, validator_attr, body = next(c for c in CHANNELS if c[0] == channel)
        ep = _wide_bom(tmp_path, wide_codec)
        before = ep.read_bytes()
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        monkeypatch.setattr(loader, "config_path", lambda: cfg)
        monkeypatch.setattr(messaging, "is_direct_local_request", lambda req: True)

        async def _accept(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(messaging, validator_attr, _accept, raising=False)

        async def _run() -> tuple[int, str]:
            app = web.Application()
            app.router.add_put(f"/api/{name}/config", getattr(messaging, handler_attr))
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/{name}/config", json={**body, "enabled": True})
                return resp.status, await resp.text()

        status, text = asyncio.run(_run())
        assert status == 409, text
        error = json.loads(text)["error"]
        assert "Re-save it as UTF-8" in error
        assert error.endswith("The .env was not changed; your other settings were saved.")
        assert ep.read_bytes() == before
        saved = json.loads(cfg.read_text(encoding="utf-8"))
        assert saved[channel]["enabled"] is True


class TestMigrate:
    @pytest.fixture(autouse=True)
    def _no_exported_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The migrator skips a key the environment already exports, so a shell
        # exporting JIRA_API_TOKEN would empty the migration and mask the result.
        for key in (*loader.CREDENTIAL_KEYS, "JIRA_API_TOKEN", "SLACK_BOT_TOKEN"):
            monkeypatch.delenv(key, raising=False)

    def _run(self, tmp_path: Path, ep: Path, **kwargs):
        from kiro_crew.secrets.migrate import migrate_env_secrets

        with (
            patch("kiro_crew.secrets.migrate.env_path", return_value=ep),
            patch("kiro_crew.secrets.migrate.config_dir", return_value=tmp_path / "cfg"),
        ):
            return migrate_env_secrets(**kwargs)

    def test_utf8_bom_first_key_migrates_and_keeps_the_bom(self, tmp_path: Path) -> None:
        ep = _utf8_bom(tmp_path)
        report = self._run(tmp_path, ep, dry_run=False)
        assert report.migrated == ["JIRA_API_TOKEN"]
        after = ep.read_bytes()
        assert after.startswith(codecs.BOM_UTF8)
        assert after[3:] == b"JIRA_API_TOKEN=secret://JIRA_API_TOKEN\r\nSLACK_BOT_TOKEN=xyz\r\n"

    def test_a_wide_file_migrates_nothing(self, wide_codec: str, tmp_path: Path) -> None:
        ep = _wide_bom(tmp_path, wide_codec)
        before = ep.read_bytes()
        report = self._run(tmp_path, ep, dry_run=False)
        assert report.migrated == []
        assert ep.read_bytes() == before

    def test_a_successful_import_re_arms_the_undecodable_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Broken, then fixed and imported, then broken again: the second break
        # must warn again, because the import did decode the file.
        ep = tmp_path / ".env"
        broken = codecs.BOM_UTF16_LE + _BODY.encode("utf-16-le")
        with caplog.at_level(logging.WARNING, logger=loader.logger.name):
            ep.write_bytes(broken)
            self._run(tmp_path, ep, dry_run=True)
            ep.write_bytes(_BODY.encode("utf-8"))
            self._run(tmp_path, ep, dry_run=True)
            ep.write_bytes(broken)
            self._run(tmp_path, ep, dry_run=True)
        warnings = [r for r in caplog.records if "Cannot decode" in r.getMessage()]
        assert len(warnings) == 2


class TestSetupSlack:
    def test_a_wide_env_prints_the_remedy_and_changes_nothing(
        self,
        wide_codec: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from kiro_crew import cli_setup

        ep = _wide_bom(tmp_path, wide_codec)
        before = ep.read_bytes()
        monkeypatch.setattr(cli_setup, "env_path", lambda: ep)

        def _no_prompt(*_a: object) -> str:
            raise AssertionError("must not prompt for tokens it cannot save")

        monkeypatch.setattr("builtins.input", _no_prompt)
        cli_setup._setup_slack_tokens()  # must not raise
        err = capsys.readouterr().err
        family = "UTF-32" if wide_codec.startswith("utf-32") else "UTF-16"
        assert f"saved as {family}" in err and "UTF-8" in err
        assert ep.read_bytes() == before


_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


_DECODERS = frozenset({"decode_env_bytes", "read_env_file", "read_env_text"})


def _opens_for_reading(call: ast.Call) -> bool:
    """A builtin ``open(p)`` or a ``p.open()`` that is not for writing.

    ``os.open`` is excluded (the writers use it for their lock file), and so
    is any call whose literal mode contains ``w``, ``a`` or ``x``.
    """
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open":
        mode_args = call.args[1:2]
    elif (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and not (isinstance(func.value, ast.Name) and func.value.id == "os")
    ):
        mode_args = call.args[0:1]
    else:
        return False
    mode = mode_args[0].value if mode_args and isinstance(mode_args[0], ast.Constant) else None
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return not (isinstance(mode, str) and any(c in mode for c in "wax"))


def _joins_dot_env(fn: ast.AST) -> bool:
    """Whether *fn* builds a path as ``<dir> / ".env"``."""
    return any(
        isinstance(n, ast.BinOp)
        and isinstance(n.op, ast.Div)
        and isinstance(n.right, ast.Constant)
        and n.right.value == ".env"
        for n in ast.walk(fn)
    )


def _bare_env_readers(tree: ast.AST, where: str) -> list[str]:
    """Functions that locate the ``.env`` and read it without the decoder.

    A function locates it by calling ``env_path()`` or by joining
    ``<dir> / ".env"`` (the data-home form the prerequisite checks use). A
    ``read_text`` in such a function bypasses the decoder; a ``read_bytes`` or
    a read-mode ``open`` is fine only beside a loader decoder call. This sees
    one function body at a time, so a path handed to a helper is judged in the
    helper only if the helper locates the file itself.
    """
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
        ]
        called = {c.func.id if isinstance(c.func, ast.Name) else c.func.attr for c in calls}
        if "env_path" not in called and not _joins_dot_env(fn):
            continue
        raw = "read_bytes" in called or any(_opens_for_reading(c) for c in calls)
        if "read_text" in called or (raw and not called & _DECODERS):
            found.append(f"{where}:{fn.lineno} {fn.name}")
    return found


class TestEveryReaderUsesTheLoaderDecoder:
    def test_no_function_reads_env_path_without_the_decoder(self) -> None:
        loader_file = _SRC / "config" / "loader.py"
        found: list[str] = []
        for path in sorted(_SRC.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if path == loader_file or ("env_path(" not in source and '".env"' not in source):
                continue
            found += _bare_env_readers(ast.parse(source), str(path.relative_to(_SRC)))
        assert found == [], "read the .env through loader.read_env_text or decode_env_bytes"

    def test_the_scan_catches_a_bare_reader(self) -> None:
        bare = "def f():\n    return env_path().read_text()\n"
        raw = "def g():\n    return env_path().read_bytes().decode()\n"
        ok = "def h():\n    return decode_env_bytes(env_path().read_bytes())\n"
        opened = "def i():\n    with open(env_path()) as fh:\n        return fh.read()\n"
        joined = "def j(home):\n    return (home / '.env').read_text()\n"
        joined_open = (
            "def k(home):\n    with (home / '.env').open('rb') as fh:\n        return fh.read()\n"
        )
        written = "def m():\n    with open(env_path(), 'w') as fh:\n        fh.write('')\n"
        lock = "def n():\n    return os.open(str(env_path()) + '.lock', 0)\n"
        assert _bare_env_readers(ast.parse(bare), "x") == ["x:1 f"]
        assert _bare_env_readers(ast.parse(raw), "x") == ["x:1 g"]
        assert _bare_env_readers(ast.parse(ok), "x") == []
        assert _bare_env_readers(ast.parse(opened), "x") == ["x:1 i"]
        assert _bare_env_readers(ast.parse(joined), "x") == ["x:1 j"]
        assert _bare_env_readers(ast.parse(joined_open), "x") == ["x:1 k"]
        assert _bare_env_readers(ast.parse(written), "x") == []
        assert _bare_env_readers(ast.parse(lock), "x") == []
