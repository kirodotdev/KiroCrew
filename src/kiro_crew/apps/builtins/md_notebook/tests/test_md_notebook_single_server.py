"""Unit coverage for the md-notebook server's self-alias guard.

The gateway launches the backend with
``runpy.run_module("...md_notebook.server", run_name="__main__", alter_sys=True)``,
which registers the running module only under ``sys.modules["__main__"]`` — not
under its canonical dotted name. ``create_app()`` then imports ``syncer``, which
imports ``server`` by that dotted name; without the fix Python finds nothing
under that key and loads a SECOND canonical copy, so the routes'
``vault_write_lock`` and the auto-sync loop's ``vault_write_lock`` are different
objects and the lock serialises nothing.

``server.main()`` closes this by calling ``_register_canonical_self()`` before
``create_app()``: it aliases the running ``__main__`` copy under the canonical
dotted name so the dotted import resolves to it. The end-to-end launch (a real
subprocess) is pinned in ``test/test_server_md_notebook_facade.py``; here we hold
the guard's own contract — that it is a safe no-op on a normal import and never
clobbers an already-bound module.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("aiohttp")

CANONICAL = "kiro_crew.apps.builtins.md_notebook.server"


def test_register_canonical_self_is_a_noop_on_normal_import():
    """On a normal dotted import (``__name__`` != ``"__main__"``) the guard does
    nothing: the canonical ``sys.modules`` entry stays bound to the same module."""
    import importlib

    server = importlib.import_module(CANONICAL)
    before = sys.modules[CANONICAL]
    assert before is server

    server._register_canonical_self()

    assert sys.modules[CANONICAL] is before
