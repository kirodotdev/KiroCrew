"""``_git_status`` must decode git's output as UTF-8, not as the host locale.

``git status --porcelain=1 -z`` is the one porcelain form that does **not**
C-quote a non-ASCII path. Verified against real git:

    $ git status --porcelain=1     ->  ?? "caf\\303\\251-\\346\\227\\245....txt"
    $ git status --porcelain=1 -z  ->  ?? caf\\303\\251-\\346\\227\\245....txt\\0

The first is pure ASCII whatever the filename is; the second is raw UTF-8. So the
moment ``-z`` was added, ``text=True`` stopped being safe on its own: it decodes
with ``locale.getpreferredencoding(False)``, which on a Windows console codepage
is cp950 / cp932 / cp1252 rather than UTF-8. One file with a non-ASCII name then
takes out the whole repository's status — ``UnicodeDecodeError`` is neither an
``OSError`` nor a ``TimeoutExpired``, so it escapes both handlers in
``_git_status`` and 500s the endpoint.

``platform/update_capability.py`` already pins ``encoding="utf-8"`` on its own git
call for exactly this reason; these tests hold the same line here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.apps.builtins.file_explorer import server

# What real git writes for an untracked `café-日本.txt` under `--porcelain=1 -z`,
# captured from git itself rather than hand-written: two status chars, a space,
# the path verbatim in UTF-8, then NUL.
NON_ASCII_NAME = "café-日本.txt"
RAW_STATUS = f"?? {NON_ASCII_NAME}\x00".encode()
RAW_BRANCH = "main\n".encode()


def _decoding_run_limited(host_encoding: str):
    """A ``run_limited`` stand-in that decodes the way ``subprocess`` documents.

    The bytes are fixed and real; the only variable is which codec the child's
    output is decoded with. ``subprocess`` uses the ``encoding=`` keyword when one
    is given and ``locale.getpreferredencoding(False)`` when it is not, so this
    reproduces the contract under test on every platform — no console codepage to
    switch, and nothing timing-dependent.
    """
    calls: list[dict] = []

    def _fake(argv, **kwargs):
        calls.append(kwargs)
        raw = RAW_STATUS if "status" in argv else RAW_BRANCH
        codec = kwargs.get("encoding") or host_encoding
        # A decode failure surfaces from `run_limited` exactly as it would from
        # `subprocess.run`, which is the whole point: `_git_status` does not
        # catch it.
        return subprocess.CompletedProcess(argv, 0, raw.decode(codec), "")

    return _fake, calls


@pytest.fixture(autouse=True)
def _no_sandbox_wrappers():
    """Neutralise the spawn wrappers, which are not what these tests are about.

    wrap_argv refuses outright on a host with no OS sandbox backend (this box
    and every Windows runner), and cgroup_scope_argv is a Linux-only ceiling.
    Both sit between _git_status and run_limited and neither touches
    decoding, so stubbing them to identity keeps the test measuring one thing and
    keeps it portable. The same idiom is already used elsewhere in
    test_file_explorer_app.py.
    """
    with (
        patch.object(server, "wrap_argv", side_effect=lambda cmd: (cmd, None)),
        patch.object(server, "cgroup_scope_argv", side_effect=lambda cmd: cmd),
    ):
        yield


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.mark.parametrize("host_encoding", ["cp950", "ascii"])
def test_a_non_ascii_path_survives_a_non_utf8_host_codepage(repo: Path, host_encoding: str) -> None:
    """The load-bearing one.

    On a host whose preferred encoding cannot represent the path, inheriting the
    locale codec raises inside the subprocess call. `cp950` is a real, shipped
    Windows codepage (Traditional Chinese) and `ascii` is the degenerate POSIX
    `LC_ALL=C` case; both fail on the same bytes, from opposite directions.
    """
    fake, calls = _decoding_run_limited(host_encoding)
    with patch.object(server, "run_limited", side_effect=fake):
        out = server._git_status(repo)

    assert out["statuses"] == {NON_ASCII_NAME: "??"}, (
        "the non-ASCII path did not survive decoding; git's own bytes were read "
        f"with the host codec instead of UTF-8 (out={out!r})"
    )
    # `_git_status` swallows OSError into `status_error`, so a regression could
    # otherwise look like an empty-but-successful result.
    assert "status_error" not in out and "branch_error" not in out
    assert out["branch"] == "main"
    assert len(calls) == 2, "both git calls must run"


def test_a_locale_that_silently_mojibakes_is_also_refused(repo: Path) -> None:
    """The quieter half, and the reason `errors=` would not be a fix.

    cp1252 maps every byte to *something*, so it does not raise — it returns a
    corrupted path. The endpoint stays 200 and the file simply never matches its
    tree entry, which is harder to notice than the crash. Asserting on the path
    itself catches both failures with one assertion.
    """
    fake, _ = _decoding_run_limited("cp1252")
    with patch.object(server, "run_limited", side_effect=fake):
        out = server._git_status(repo)

    assert out["statuses"] == {NON_ASCII_NAME: "??"}


def test_an_ascii_path_is_unaffected(repo: Path) -> None:
    """Over-fix guard: the ordinary case must be byte-identical to before.

    Without this, pinning UTF-8 could not be told apart from a change that
    happened to fix the non-ASCII case by mangling every path.
    """

    def _fake(argv, **kwargs):
        raw = b" M plain.txt\x00" if "status" in argv else b"main\n"
        codec = kwargs.get("encoding") or "cp950"
        return subprocess.CompletedProcess(argv, 0, raw.decode(codec), "")

    with patch.object(server, "run_limited", side_effect=_fake):
        out = server._git_status(repo)

    assert out["statuses"] == {"plain.txt": "M"}
    assert out["branch"] == "main"


def test_a_ripgrep_match_survives_a_non_utf8_host_codepage(tmp_path: Path, monkeypatch) -> None:
    """The same rule on the other spawn in this file.

    ripgrep's ``--json`` stream is UTF-8 by definition and carries both the
    matched path and the matched line, so non-ASCII is the norm. The fallback
    below `_search_rg`'s `try` catches only `OSError` / `TimeoutExpired`, so a
    decode failure does not degrade to `_search_python` — it 500s the search.
    """
    monkeypatch.setattr(server, "_has_rg", lambda: True)

    hit = {
        "type": "match",
        "data": {
            "path": {"text": str(tmp_path / NON_ASCII_NAME)},
            "line_number": 1,
            "lines": {"text": "naïve café — 日本\n"},
            "submatches": [{"start": 0, "end": 5}],
        },
    }
    raw = (json.dumps(hit, ensure_ascii=False) + "\n").encode("utf-8")

    def _fake(argv, **kwargs):
        codec = kwargs.get("encoding") or "cp950"
        return subprocess.CompletedProcess(argv, 0, raw.decode(codec), "")

    with patch.object(server, "run_limited", side_effect=_fake):
        results = server._search_rg(tmp_path, "café", "", "")

    assert results, "the match was dropped"
    assert results[0]["file"].endswith(NON_ASCII_NAME)
    assert results[0]["preview"] == "naïve café — 日本"
