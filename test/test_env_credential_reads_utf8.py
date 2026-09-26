"""The .env credential store is read once as bytes, then decoded UTF-8 first
with a legacy fallback.

Every other reader and writer of ``~/.kiro/crew/.env`` names UTF-8 explicitly:
the setup wizard reads it as UTF-8, the dashboard credential writer reads it
as UTF-8 and stores ``str`` content through ``atomic_write`` (documented
UTF-8), the WeChat QR handler reads it as UTF-8 at every one of its sites, the
secrets migrator decodes ``read_bytes()`` as UTF-8, and the service warning
reader is pinned UTF-8. The two loader reads pinned here were the odd ones
out: a bare ``read_text()`` decodes with the active code page — cp936 on the
zh-CN Windows install this repository was audited on — where a human-written
UTF-8 comment in the operator's file raises ``UnicodeDecodeError`` out of a
path whose neighbours promise graceful degradation (the per-key reader catches
only ``OSError``; the boot ``load_credentials()`` loop guards nothing at all),
and a codepage that decodes most bytes silently mojibakes the file instead.

UTF-8 is the contract and the first decode. A store saved under a legacy code
page predates that contract, so a decode failure falls back to the host's ANSI
page, named explicitly through ``locale.getencoding()`` — the way its writer
actually encoded it — instead of failing a boot that reads it or answering
unset for a credential that is there. The explicit name matters because the
gateway launches with ``PYTHONUTF8=1``: under UTF-8 mode a bare read answers
``utf-8``, so a bare fallback would retry the decode that just failed. The
store is read ONCE, as bytes, and both decoders work on that snapshot: a
second read would decode a file a concurrent save may have replaced. Only a
store that neither decoder can read is unset (per-key reader) or keeps its
boot failure (``load_credentials()``), so the per-key reader's documented
unset contract holds on every host and launch configuration.

Pinned both ways: the behavioural tests name the ANSI page through
``locale.getencoding()`` (the decoder the fallback must use; decode does not
consult the text funnel at all) and the source assertions keep both sites
shaped — one byte snapshot per read, both decodes on that snapshot — even if
the functions are reshaped.
"""

from __future__ import annotations

import ast
from ast import AsyncFunctionDef, FunctionDef
from pathlib import Path

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import read_env_file_credential

_LOADER_PATH = Path(loader.__file__)
_UTF8 = 'raw.decode("utf-8")'
_LOCALE = "raw.decode(locale.getencoding())"


def _function_source(name: str) -> str:
    source = _LOADER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (FunctionDef, AsyncFunctionDef)) and node.name == name:
            lines = source.splitlines(keepends=True)
            return "".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"function {name!r} not found in config/loader.py")


def _legacy_store_reader(monkeypatch: pytest.MonkeyPatch, ansi_code_page: str) -> None:
    """Name the ANSI page the fallback must decode with. The store is read as
    bytes and decoded on that snapshot, so no text funnel is consulted here:
    ``decode()`` takes the codec by name. Patching ``locale.getencoding()``
    makes the legacy direction deterministic on any host."""
    monkeypatch.setattr("locale.getencoding", lambda: ansi_code_page)


def test_per_key_reader_decodes_a_non_ascii_comment_under_cp936(
    tmp_path: Path,
) -> None:
    """A UTF-8 .env with a non-ASCII comment must read the ASCII token value,
    not raise. Under cp936 the bare read raises UnicodeDecodeError, which is
    not an OSError and therefore escapes the reader's own recovery arm."""
    env_file = tmp_path / ".env"
    env_file.write_bytes("KIRO_API_KEY=abc123\n# 说明：生产密钥\n".encode("utf-8"))
    assert read_env_file_credential("KIRO_API_KEY", env_file) == "abc123"


def test_per_key_reader_keeps_the_value_intact_under_cp1252(tmp_path: Path) -> None:
    """cp1252 decodes most UTF-8 bytes: the failure there is silent mojibake,
    so the value (the part that matters) must still come back exact."""
    env_file = tmp_path / ".env"
    env_file.write_bytes("KIRO_API_KEY=abc123\n# naïve café note\n".encode("utf-8"))
    assert read_env_file_credential("KIRO_API_KEY", env_file) == "abc123"


def test_per_key_reader_still_reads_a_legacy_codepage_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store saved under the host code page predates the UTF-8 contract:
    its writer used the locale, so the locale is the correct decoder for it.
    The UTF-8 attempt fails here, and the value must survive the fallback —
    under the PYTHONUTF8 launch a bare-name fallback would retry utf-8 and
    raise the boot crash out of the unguarded caller."""
    env_file = tmp_path / ".env"
    env_file.write_bytes("KIRO_API_KEY=abc123\n# 说明：生产密钥\n".encode("cp936"))
    _legacy_store_reader(monkeypatch, "cp936")
    assert read_env_file_credential("KIRO_API_KEY", env_file) == "abc123"


def test_per_key_reader_reads_an_undecodable_store_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery arm promises unset for an unreadable store. Bytes that
    neither UTF-8 nor the host code page can decode must land there instead
    of raising."""
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"KIRO_API_KEY=ok\n\xff\xfe not text\n")
    _legacy_store_reader(monkeypatch, "cp936")
    assert read_env_file_credential("KIRO_API_KEY", env_file) == ""


def test_per_key_reader_source_names_utf8() -> None:
    body = _function_source("read_env_file_credential")
    assert "ep.read_bytes()" in body
    assert _UTF8 in body
    assert _LOCALE in body
    assert "ep.read_text(" not in body
    assert "except UnicodeDecodeError" in body


def test_boot_reader_loop_source_names_utf8() -> None:
    """load_credentials() runs at gateway boot with no exception guard around
    the read at all, so the read takes one byte snapshot and decodes it UTF-8
    first, then with the host's ANSI page — under the gateway's PYTHONUTF8
    launch a bare-name fallback would retry utf-8 and abort the boot."""
    body = _function_source("load_credentials")
    assert "ep.read_bytes()" in body
    assert _UTF8 in body
    assert _LOCALE in body
    assert "ep.read_text(" not in body
    assert "except UnicodeDecodeError" in body
