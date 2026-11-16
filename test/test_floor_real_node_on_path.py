"""The floor puts the real ``node`` ahead of a version-manager shim on ``PATH``.

A shim picks its node from ``HOME``, ``$XDG_CONFIG_HOME`` and the CWD, and the floor
redirects ``$XDG_CONFIG_HOME`` for every test, so a shim that works in the operator's
shell fails inside a test. ``_put_the_real_node_first_on_path`` asks node for its own
binary once and puts that directory first. These tests drive it with a stand-in shim
that answers the probe, so no real node or version manager is needed.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest
from conftest_floor_configure import _REAL_NODE_RESOLVED_ENV, _put_the_real_node_first_on_path

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in shim is a POSIX shell script")


def _node(directory: Path, body: str) -> Path:
    """Write an executable ``node`` into *directory* that runs *body* as Python.

    A ``/bin/sh`` launcher with the interpreter quoted, not a ``#!<python>`` line: the
    kernel splits a shebang at whitespace, so an interpreter path with a space in it
    would never start.
    """
    directory.mkdir(parents=True, exist_ok=True)
    payload = directory / "node_payload.py"
    payload.write_text(f"import sys\n{body}\n", encoding="utf-8")
    node = directory / "node"
    node.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(payload))} "$@"\n',
        encoding="utf-8",
    )
    node.chmod(0o755)
    return node


@pytest.fixture
def unresolved(monkeypatch):
    """Clear the controller's once-only mark so the function runs again here."""
    monkeypatch.delenv(_REAL_NODE_RESOLVED_ENV, raising=False)
    return monkeypatch


def test_a_shim_is_replaced_by_the_directory_node_names(tmp_path, unresolved):
    real = _node(tmp_path / "installs" / "node" / "bin", "")
    shims = tmp_path / "shims"
    _node(shims, f"sys.stdout.write({str(real)!r})")
    unresolved.setenv("PATH", os.pathsep.join([str(shims), "/usr/bin", "/bin"]))

    _put_the_real_node_first_on_path()

    assert os.environ["PATH"].split(os.pathsep) == [
        str(real.parent),
        str(shims),
        "/usr/bin",
        "/bin",
    ]
    assert os.environ[_REAL_NODE_RESOLVED_ENV] == "1"


def test_a_real_node_leaves_path_alone(tmp_path, unresolved):
    bin_dir = tmp_path / "bin"
    _node(bin_dir, f"sys.stdout.write({str(bin_dir / 'node')!r})")
    path = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
    unresolved.setenv("PATH", path)

    _put_the_real_node_first_on_path()

    assert os.environ["PATH"] == path


@pytest.mark.parametrize(
    "body",
    [
        "sys.stderr.write('mise ERROR No version is set for shim: node'); sys.exit(1)",
        "sys.stdout.write('not-a-path')",
    ],
    ids=["probe-fails", "answer-is-not-a-path"],
)
def test_a_shim_that_cannot_answer_leaves_path_alone(tmp_path, unresolved, body):
    shims = tmp_path / "shims"
    _node(shims, body)
    path = os.pathsep.join([str(shims), "/usr/bin", "/bin"])
    unresolved.setenv("PATH", path)

    _put_the_real_node_first_on_path()

    assert os.environ["PATH"] == path


def test_a_resolved_process_does_not_probe_again(tmp_path, monkeypatch):
    shims = tmp_path / "shims"
    marker = tmp_path / "probed"
    _node(shims, f"open({str(marker)!r}, 'w').close()")
    monkeypatch.setenv(_REAL_NODE_RESOLVED_ENV, "1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(shims), "/usr/bin", "/bin"]))

    _put_the_real_node_first_on_path()

    assert not marker.exists()
