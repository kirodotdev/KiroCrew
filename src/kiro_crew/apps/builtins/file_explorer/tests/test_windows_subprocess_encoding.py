"""Windows portability: child-process output must be decoded as UTF-8.

``text=True`` on its own decodes a child's stdout with
``locale.getpreferredencoding(False)``. On a zh-CN Windows host that is cp936,
while both ``git`` and ``rg --json`` emit UTF-8 unconditionally. The mismatch
mangled branch names, git status keys (which ARE repo-relative paths, so the
badge landed on no file) and search previews — and on bytes cp936 cannot
represent it raised ``UnicodeDecodeError``, which neither call site's
``except (OSError, subprocess.TimeoutExpired)`` names, turning a recoverable
search into a 500 instead of the Python fallback.

These tests pin the decode on the argv-building side, so they run identically
on every platform rather than needing a cp936 host to reproduce.
"""

import subprocess

import pytest

from kiro_crew.apps.builtins.file_explorer import server


@pytest.fixture
def recorded_calls(monkeypatch):
    """Neutralise the sandbox wrappers and record every run_limited kwarg set."""
    calls: list[dict] = []

    def _fake_run_limited(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server, "wrap_argv", lambda argv, **kw: (list(argv), None))
    monkeypatch.setattr(server, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(server, "run_limited", _fake_run_limited)
    return calls


def _assert_utf8_decode(kwargs: dict) -> None:
    assert kwargs.get("encoding") == "utf-8", kwargs
    # ``replace`` is load-bearing, not cosmetic: it is what stops an
    # undecodable byte from raising past the narrow except clause.
    assert kwargs.get("errors") == "replace", kwargs


def test_git_status_decodes_both_child_processes_as_utf8(recorded_calls, tmp_path):
    """Both the rev-parse and the status child must be pinned to UTF-8."""
    server._git_status(tmp_path)
    assert len(recorded_calls) == 2, recorded_calls
    for kwargs in recorded_calls:
        _assert_utf8_decode(kwargs)


def test_search_rg_decodes_its_child_as_utf8(recorded_calls, monkeypatch, tmp_path):
    """``rg --json`` is UTF-8 by definition, so the reader must say so."""
    monkeypatch.setattr(server, "_contain_in_allowed_roots", lambda p, **kw: p)
    server._search_rg(tmp_path, "needle", "", "")
    assert len(recorded_calls) == 1, recorded_calls
    _assert_utf8_decode(recorded_calls[0])


def test_git_status_keys_non_ascii_paths_by_their_real_name(monkeypatch, tmp_path):
    """A CJK path must key the status map verbatim once decoded correctly.

    Guards the half of the bug the kwargs alone do not show: a mis-decoded key
    is still a str, so the endpoint returns 200 with a badge attached to a
    filename that does not exist on disk.
    """
    name = "笔记/项目说明.md"
    payload = f"?? {name}\x00 M src/app.py\x00"

    def _fake_run_limited(argv, **kwargs):
        out = payload if "status" in argv else "main\n"
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(server, "wrap_argv", lambda argv, **kw: (list(argv), None))
    monkeypatch.setattr(server, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(server, "run_limited", _fake_run_limited)

    out = server._git_status(tmp_path)
    assert out["branch"] == "main"
    assert name in out["statuses"], out["statuses"]
    assert out["statuses"][name] == "??"
