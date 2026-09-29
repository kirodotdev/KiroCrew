"""Where contributed projection rows LIVE, as opposed to how they behave.

These two pins are deliberately in a file of their own. Every behavioural test of
the contribution protocol runs under an autouse fixture that monkeypatches
``contrib.contrib_root`` to a temporary directory, which is right for exercising
the store and wrong for asserting where the real root is: under that fixture a
fence test would only ever describe the fixture's own path, and would keep passing
if the product moved its rows outside both fences.

So nothing here is patched except the data home, by ENV VAR, which is what
``crew_log.store`` and the file-tool gate both read.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the data home at a temporary directory, the way both fences see it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def test_contributed_projection_rows_live_under_the_fenced_crew_log_tree():
    """`store.values()` trusts the file and the drawer renders it.

    Outside the fences an agent's own file tools could plant a row, so the answer
    is the same one the member log got: live under the leaf both fences name.
    """
    from kiro_crew.crew_log.store import crew_log_tree_root
    from kiro_crew.eventlog.contrib import contrib_root

    root = contrib_root()
    tree = crew_log_tree_root()
    assert tree == root.parent or tree in root.parents


def test_the_file_tool_gate_refuses_a_contributed_projection_path():
    """The gate's own answer, not a re-derivation of its list.

    Asserting the path is refused rather than that ``crew-log`` appears in some
    table: the second would keep passing if the gate stopped consulting the table.
    """
    from kiro_crew.eventlog.contrib import contrib_root
    from kiro_crew.security.paths import is_sensitive_path

    assert is_sensitive_path(str(contrib_root() / "member" / "alice.json"))
