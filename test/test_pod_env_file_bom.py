"""A pod env file saved with a byte-order mark by a Windows editor.

Notepad's "UTF-8" writes UTF-8 with a BOM, and PowerShell's default ``>`` writes
UTF-16LE with one. Every reader of the per-pod env file must read the first key
without the mark, and a wide-encoded file must refuse with a message naming the
fix rather than raising a bare ``UnicodeDecodeError``.
"""

from __future__ import annotations

import codecs
import locale
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import worktree_ops
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import runtime_ports
from kiro_crew.pod.config import PodConfig

_BODY = "MYVAR='hello'\r\nPORT='7999'\r\n"


def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
    cfg = PodConfig.load()
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_utf8_bom_first_key_is_read_without_the_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.env_file("x").write_bytes(codecs.BOM_UTF8 + _BODY.encode("utf-8"))

    assert rt.read_env_file(cfg, "x") == {"MYVAR": "hello", "PORT": "7999"}


def test_utf8_bom_non_ascii_value_decodes_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mark says UTF-8, so the value decodes as UTF-8 whatever the locale is.
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.env_file("x").write_bytes(codecs.BOM_UTF8 + "SEED='caf\u00e9'\n".encode("utf-8"))

    assert rt.read_env_file(cfg, "x") == {"SEED": "caf\u00e9"}


def test_bomless_file_reads_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.env_file("x").write_bytes(_BODY.encode("utf-8"))

    assert rt.read_env_file(cfg, "x") == {"MYVAR": "hello", "PORT": "7999"}


def test_missing_file_still_reads_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert rt.read_env_file(_cfg(tmp_path, monkeypatch), "absent") == {}


@pytest.mark.parametrize(
    ("bom", "codec", "family"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le", "UTF-16"),
        (codecs.BOM_UTF16_BE, "utf-16-be", "UTF-16"),
        (codecs.BOM_UTF32_LE, "utf-32-le", "UTF-32"),
    ],
)
def test_wide_encoded_file_refuses_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bom: bytes, codec: str, family: str
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.env_file("x").write_bytes(bom + _BODY.encode(codec))

    with pytest.raises(rt.PodError, match=rf"{family} byte-order mark; save it as UTF-8"):
        rt.read_env_file(cfg, "x")


def test_wide_encoded_file_is_not_overwritten_by_a_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    original = codecs.BOM_UTF16_LE + _BODY.encode("utf-16-le")
    cfg.env_file("x").write_bytes(original)

    with pytest.raises(rt.PodError):
        rt.write_env_file(cfg, "x", {"SEED": "/s"})
    assert cfg.env_file("x").read_bytes() == original


def test_merge_over_a_bom_file_keeps_the_first_key_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.env_file("x").write_bytes(codecs.BOM_UTF8 + _BODY.encode("utf-8"))

    rt.write_env_file(cfg, "x", {"MYVAR": "bye"})

    assert rt.read_env_file(cfg, "x") == {"MYVAR": "bye", "PORT": "7999"}


def test_merge_keeps_the_utf8_mark_so_a_non_ascii_value_survives_a_non_utf8_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the mark the next read falls to the locale decode, and under
    # cp1252 the UTF-8 bytes of a Cyrillic value do not decode.
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.env_file("x").write_bytes(codecs.BOM_UTF8 + "SEED='\u0401'\n".encode("utf-8"))
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp1252")

    rt.pin_checkout(cfg, "x", Path("/abs/co"))

    assert cfg.env_file("x").read_bytes().startswith(codecs.BOM_UTF8)
    assert rt.read_env_file(cfg, "x") == {"SEED": "\u0401", "CHECKOUT": str(Path("/abs/co"))}


def test_merge_into_a_bomless_file_adds_no_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    rt.write_env_file(cfg, "x", {"PORT": "7999"})

    assert cfg.env_file("x").read_bytes() == b"PORT='7999'\n"


def test_parse_env_text_drops_a_decoded_mark() -> None:
    assert rt._parse_env_text("\ufeffMYVAR='hello'\n") == {"MYVAR": "hello"}


def test_peer_env_reader_drops_the_mark(tmp_path: Path) -> None:
    path = tmp_path / "peer.env"
    path.write_bytes(codecs.BOM_UTF8 + _BODY.encode("utf-8"))

    assert runtime_ports._read_peer_env(path) == {"MYVAR": "hello", "PORT": "7999"}


def test_dev_fleet_strict_pin_reader_drops_the_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.env_file("x").write_bytes(codecs.BOM_UTF8 + b"CHECKOUT='/abs/co'\n")

    assert worktree_ops._read_pin_strict(cfg, "x") == (True, "/abs/co")


def _cp1252_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    # The locale a Western-European Windows host has outside UTF-8 mode: both the
    # preferred encoding and the code page name cp1252.
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp1252")
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")


def test_bomless_non_ascii_pin_round_trips_under_a_non_utf8_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _cp1252_locale(monkeypatch)
    checkout = Path("/abs/Jos\u00e9/src/kc-\u00fc")

    rt.pin_checkout(cfg, "x", checkout)

    assert not cfg.env_file("x").read_bytes().startswith(codecs.BOM_UTF8)
    assert rt.read_env_file(cfg, "x") == {"CHECKOUT": str(checkout)}


def test_merge_leaves_a_bomless_non_ascii_value_unchanged_under_a_non_utf8_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _cp1252_locale(monkeypatch)
    checkout = Path("/abs/Jos\u00e9/src/kc-\u00fc")
    rt.pin_checkout(cfg, "x", checkout)
    before = cfg.env_file("x").read_bytes()

    rt.write_env_file(cfg, "x", {"PORT": "15999"})
    rt.write_env_file(cfg, "x", {"PORT": "16000"})

    assert rt.read_env_file(cfg, "x") == {"CHECKOUT": str(checkout), "PORT": "16000"}
    assert cfg.env_file("x").read_bytes() == before + b"PORT='16000'\n"


def test_bomless_file_saved_in_the_locale_code_page_still_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hand-saved ANSI file is not valid UTF-8, so it decodes with the locale.
    cfg = _cfg(tmp_path, monkeypatch)
    _cp1252_locale(monkeypatch)
    cfg.env_file("x").write_bytes("SEED='caf\u00e9'\n".encode("cp1252"))

    assert rt.read_env_file(cfg, "x") == {"SEED": "caf\u00e9"}


def _utf8_mode_with_cp1252_codepage(monkeypatch: pytest.MonkeyPatch) -> None:
    # What a Windows host under the gateway's PYTHONUTF8=1 sees: UTF-8 mode makes
    # getpreferredencoding answer "utf-8", but getencoding still names the real
    # ANSI code page. Patching both lets the UTF-8-mode branch run off Windows.
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "utf-8")
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")


def test_bomless_locale_code_page_file_still_reads_under_utf8_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under UTF-8 mode getpreferredencoding is "utf-8", so the fallback must use
    # getencoding (the real code page) rather than re-running the UTF-8 decode.
    cfg = _cfg(tmp_path, monkeypatch)
    _utf8_mode_with_cp1252_codepage(monkeypatch)
    cfg.env_file("x").write_bytes("SEED='caf\u00e9'\n".encode("cp1252"))

    assert rt.read_env_file(cfg, "x") == {"SEED": "caf\u00e9"}


def test_file_no_decoder_can_read_raises_poderror_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A byte sequence that is neither valid UTF-8 nor valid in the ANSI code
    # page raises PodError naming the file, not a bare UnicodeDecodeError.
    cfg = _cfg(tmp_path, monkeypatch)
    _utf8_mode_with_cp1252_codepage(monkeypatch)
    # Patch the ANSI code page to a double-byte one that rejects a lone 0x81.
    monkeypatch.setattr(locale, "getencoding", lambda: "cp932")
    cfg.env_file("x").write_bytes(b"SEED='\x81'\n")

    with pytest.raises(rt.PodError, match=r"is not valid UTF-8 or cp932.*save it as UTF-8"):
        rt.read_env_file(cfg, "x")


def test_pod_reader_agrees_with_the_peer_and_strict_pin_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _cp1252_locale(monkeypatch)
    rt.pin_checkout(cfg, "x", Path("/abs/Jos\u00e9"))
    pinned = str(Path("/abs/Jos\u00e9"))

    assert rt.read_env_file(cfg, "x")["CHECKOUT"] == pinned
    assert runtime_ports._read_peer_env(cfg.env_file("x"))["CHECKOUT"] == pinned
    assert worktree_ops._read_pin_strict(cfg, "x") == (True, pinned)
