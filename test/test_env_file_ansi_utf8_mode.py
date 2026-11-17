"""A data-home ``.env`` saved in the ANSI code page still loads under UTF-8 mode.

The Desktop app runs the gateway with ``PYTHONUTF8=1``, where the locale's
preferred encoding is always UTF-8. A file Windows PowerShell 5.1 or Notepad saved
as cp1252 then failed the UTF-8 decode twice and the boot read raised. The locale
and UTF-8 mode are patched through the loader facade, so these run on any host.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.config import loader, loader_credentials
from kiro_crew.config.loader import KiroCrewConfig

_ANSI = b"OWNER_NAME=caf\xe9\r\n"


def _locale(preferred: str, actual: str) -> SimpleNamespace:
    return SimpleNamespace(
        getpreferredencoding=lambda _=False: preferred, getencoding=lambda: actual
    )


def _utf8_mode(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(
        loader,
        "_sys",
        SimpleNamespace(flags=SimpleNamespace(utf8_mode=int(on))),
    )


def _boot_read(monkeypatch: pytest.MonkeyPatch, ep: Path) -> dict[str, str]:
    monkeypatch.setattr(loader, "env_path", lambda: ep)
    for key in (*loader.CREDENTIAL_KEYS, "OWNER_NAME"):
        monkeypatch.delenv(key, raising=False)
    return KiroCrewConfig.__new__(KiroCrewConfig).load_credentials(propagate=False)


def test_an_ansi_env_file_loads_under_utf8_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "_locale", _locale("utf-8", "cp1252"))
    _utf8_mode(monkeypatch, True)
    ep = tmp_path / ".env"
    ep.write_bytes(_ANSI)

    assert _boot_read(monkeypatch, ep)["OWNER_NAME"] == "café"
    assert loader_credentials.read_env_text(ep) == "OWNER_NAME=café\r\n"


def test_a_utf8_file_still_reads_as_utf8_under_utf8_mode() -> None:
    # The code page is a fallback only: a file valid in UTF-8 is never re-read in it.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loader, "_locale", _locale("utf-8", "cp1252"))
        _utf8_mode(mp, True)
        text = loader_credentials.decode_env_bytes("OWNER_NAME=café\n".encode("utf-8"))
    assert text == "OWNER_NAME=café\n"


def test_outside_utf8_mode_an_undecodable_file_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loader, "_locale", _locale("utf-8", "cp1252"))
    _utf8_mode(monkeypatch, False)
    with pytest.raises(UnicodeDecodeError):
        loader_credentials.decode_env_bytes(_ANSI)


def test_an_explicit_encoding_is_never_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rewriters pass "utf-8" so they never overwrite a file they could not parse.
    monkeypatch.setattr(loader, "_locale", _locale("utf-8", "cp1252"))
    _utf8_mode(monkeypatch, True)
    with pytest.raises(UnicodeDecodeError):
        loader_credentials.decode_env_bytes(_ANSI, "utf-8")
