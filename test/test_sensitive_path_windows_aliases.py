"""Windows namespace and default-stream spellings of a local path share one verdict.

On Windows ``\\\\?\\C:\\x``, ``\\\\.\\C:\\x``, ``//?/C:/x`` and ``\\??\\C:\\x`` open the
same file as ``C:\\x``, and so do ``C:\\x::$DATA`` and, for a directory,
``C:\\d::$INDEX_ALLOCATION``. The sensitive-path gates fold those spellings in
``_candidate_forms`` before resolving, so the inside gates (read, refusal, write) and
the reverse ``path_contains_sensitive`` gate reach the same answer as for the plain
spelling. Off Windows the same characters are ordinary file-name bytes and nothing
is folded.

The Windows half runs everywhere through ``ntpath``, the way
``test_security_path_resolve_bounded`` drives Windows spellings: ``os.path`` and
``os.sep`` are swapped, ``_ON_WINDOWS`` is set, and resolution is ``ntpath.normpath``,
so the verdict is the lexical fold's on every host, real Windows included.
"""

from __future__ import annotations

import ntpath
import os
import threading
from pathlib import Path

import pytest

from kiro_crew import security

HOME = r"C:\Users\alice"
CRED = HOME + r"\.aws\credentials"  # inside a protected directory
LEAF = HOME + r"\.docker\config.json"  # a protected single file
LEAF_W = HOME + r"\.kiro\crew\config.json"  # write-protected only
ARTIFACT = HOME + r"\.kiro\crew\tmpabc123.tmp"  # a keystone publish artifact

PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")


def _forward(path: str) -> str:
    return "//?/" + path.replace("\\", "/")


def _aliases(path: str) -> list[str]:
    return [prefix + path for prefix in PREFIXES] + [_forward(path)]


class _WindowsOs:
    """``security.paths``' private view of ``os``: ``ntpath``, ``\\`` and a fake env.

    Everything else is the real ``os``.
    """

    path = ntpath
    sep = ntpath.sep

    def __init__(self, environ: dict[str, str]) -> None:
        self.environ = environ

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)


def _windows_path_class(home: str) -> type:
    class _WindowsHomePath(type(Path())):  # type: ignore[misc]
        @classmethod
        def home(cls) -> Path:
            return cls(home)

    return _WindowsHomePath


def _enter_windows(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Off Windows ``C:\Users\alice`` is a RELATIVE name. So the fake home and the
    # ``ntpath`` swap are handed to ``security.paths`` alone, never to the process:
    # ``os.environ``, ``os.path`` and ``Path.home()`` stay real for every other thread.
    # A thread left over from an earlier test that read a process-wide fake ``HOME``
    # could ``mkdir`` it after teardown restored the CWD, i.e. in the checkout.
    # The CWD pin still catches a write the code under test makes itself.
    monkeypatch.chdir(tmp_path)
    environ = {
        k: v
        for k, v in os.environ.items()
        if k not in ("KIROCREW_HOME", "KIRO_HOME", "KIROCREW_OS_HOME", "XDG_CONFIG_HOME")
    }
    environ["HOME"] = HOME
    environ["USERPROFILE"] = HOME
    monkeypatch.setattr(security.paths, "os", _WindowsOs(environ))
    monkeypatch.setattr(security.paths, "Path", _windows_path_class(HOME))
    monkeypatch.setattr(security.paths, "_ON_WINDOWS", True)
    monkeypatch.setattr(security.paths, "_resolved_spellings", lambda e: {ntpath.normpath(e)})
    monkeypatch.setattr(
        security.paths, "_realpaths_or_none", lambda ps: [ntpath.normpath(p) for p in ps]
    )
    monkeypatch.setattr(security.paths, "_home_targets_cache", {})


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _enter_windows(monkeypatch, tmp_path)


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize("target", [CRED, LEAF])
def test_plain_spelling_is_refused(target: str) -> None:
    assert security.is_sensitive_path(target)
    assert security.sensitive_path_refusal(target) is not None


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize("path", _aliases(CRED) + _aliases(LEAF))
def test_namespace_prefix_spelling_is_refused(path: str) -> None:
    assert security.is_sensitive_path(path)
    assert security.sensitive_path_refusal(path) is not None


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize("path", _aliases(LEAF_W) + [LEAF_W + "::$DATA"])
def test_write_gate_refuses_alias_spellings(path: str) -> None:
    assert security.is_sensitive_write_path(path)


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize(
    "path",
    _aliases(HOME) + ["\\\\?\\C:", HOME + "::$INDEX_ALLOCATION", HOME + ":$I30:$INDEX_ALLOCATION"],
)
def test_contains_gate_refuses_alias_spellings(path: str) -> None:
    assert security.path_contains_sensitive(path)


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize("suffix", ["::$DATA", "::$data", "::$Data"])
def test_default_stream_suffix_on_a_protected_file_is_refused(suffix: str) -> None:
    assert security.is_sensitive_path(LEAF + suffix)
    assert security.is_sensitive_path("\\\\?\\" + LEAF + suffix)


@pytest.mark.usefixtures("windows")
def test_keystone_artifact_precheck_sees_the_folded_spelling() -> None:
    assert security.is_sensitive_resolved_path(ARTIFACT)
    assert security.is_sensitive_resolved_path(ARTIFACT + "::$DATA")
    assert security.is_sensitive_path(ARTIFACT + "::$DATA")


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize(
    "path",
    [
        HOME + r"\proj\a.txt",
        "\\\\?\\" + HOME + r"\proj\a.txt",
        "\\\\.\\" + HOME + r"\proj\a.txt",
        r"\\?\D:\data\x.bin",
        r"\\?\UNC\fileserver\share\x.txt",
        r"\\fileserver\share\x.txt",
        HOME + r"\proj\a.txt::$DATA",
        HOME + r"\proj\a.txt:zone",
        LEAF + ":named",
    ],
)
def test_ordinary_paths_stay_allowed(path: str) -> None:
    assert not security.is_sensitive_path(path)


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize(
    "path",
    [
        HOME + r"\proj",
        "\\\\?\\" + HOME + r"\proj",
        r"\\?\D:\data",
        HOME + r"\proj::$INDEX_ALLOCATION",
    ],
)
def test_ordinary_roots_stay_allowed_for_the_contains_gate(path: str) -> None:
    assert not security.path_contains_sensitive(path)


@pytest.mark.usefixtures("windows")
def test_an_unprotected_file_stays_writable_through_a_prefix() -> None:
    assert not security.is_sensitive_write_path("\\\\?\\" + HOME + r"\proj\config.json")


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize("volume", ["\\\\?\\C:", "\\\\.\\C:", "//?/C:"])
def test_a_bare_volume_folds_to_the_drive_root(volume: str) -> None:
    assert security.paths._fold_windows_alias(volume) == "C:\\"
    assert security.path_contains_sensitive(volume, base_dir=r"C:\work")


@pytest.mark.usefixtures("windows")
def test_the_raw_spelling_stays_a_candidate() -> None:
    raw = "\\\\?\\" + LEAF
    forms = security.paths._candidate_forms(raw)
    assert raw in forms
    assert LEAF in forms


@pytest.mark.usefixtures("windows")
@pytest.mark.parametrize("path", _aliases(CRED) + [LEAF + "::$DATA"])
def test_without_the_fold_the_alias_spellings_are_allowed(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Mutation check: the fold is what refuses these, not some other candidate."""
    monkeypatch.setattr(security.paths, "_fold_windows_alias", lambda p: p)
    assert not security.is_sensitive_path(path)


def test_the_fake_windows_home_never_reaches_the_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Another thread sees the real home and ``os.path`` while the fixture holds."""
    before = (os.environ.get("HOME"), os.environ.get("USERPROFILE"), os.path, str(Path.home()))
    _enter_windows(monkeypatch, tmp_path)
    assert security.paths.Path.home() == security.paths.Path(HOME)
    assert security.paths.os.path is ntpath
    seen: list[object] = []
    reader = threading.Thread(
        target=lambda: seen.extend(
            (os.environ.get("HOME"), os.environ.get("USERPROFILE"), os.path, str(Path.home()))
        )
    )
    reader.start()
    reader.join()
    assert tuple(seen) == before


@pytest.mark.parametrize(
    "name", ["\\\\?\\x", "\\\\?\\C:\\Users\\a\\.aws\\credentials", "notes::$DATA", "a.json::$DATA"]
)
def test_posix_spellings_are_not_folded(
    monkeypatch: pytest.MonkeyPatch, tmp_path, name: str
) -> None:
    monkeypatch.setattr(security.paths, "_ON_WINDOWS", False)
    assert security.paths._fold_windows_alias(name) == name
    if os.name == "nt":
        return  # these names are not creatable file names on Windows
    path = os.path.join(str(tmp_path), name)
    assert not security.is_sensitive_path(path)
    assert not security.path_contains_sensitive(path)
