# SPDX-License-Identifier: Apache-2.0
"""A row's Remove reads the same with the direct answer as with the per-row rebuild.

``worker_summary`` offers a glob-free row's Remove without rebuilding the file
when no other effective entry reaches it. A Remove offered where the rebuild
withholds it tells the owner a grant is gone while another entry still grants
it, so every row is checked against the rebuild here.
"""

from __future__ import annotations

import pytest

from kiro_crew import mate_capabilities, mate_worker
from kiro_crew.agent_sdk import agent_specs, mcp_refs

BULK = 300
BASE = {
    "mcpServers": {"plat": {"command": "plat"}},
    "tools": ["@plat"],
    # Reached by the file's ``@notes/write_*``: the reach pass adds it.
    "allowedTools": ["@notes/write_derived", "fs_read"],
}
SERVERS = {n: {"command": n} for n in ("notes", "srv1", "srv2", "bulk")}
FILE = {
    "mcpServers": {"plat": {"command": "plat"}, **SERVERS},
    "tools": ["@plat", "@notes/write_note", "@notes/Read", "@notes/read", "@srv1", "@bulk"],
    "allowedTools": [
        "@notes/write_*",
        "@notes/write_note",
        "@NOTES/Read",
        "@notes/read",
        "@notes/other",
        "@srv?/x",
        "@srv1/x",
        "@srv2/y",
        "@plat/use",
        "@srv1",
        "@srv1/z",
        "read",
        "x/y",
        *(f"@bulk/t{i}" for i in range(BULK)),
    ],
}


def _removes(monkeypatch, shortcut: bool) -> tuple[dict[tuple[str, str], object], int]:
    rebuilds = [0]
    real = mate_worker.worker_additions

    def counted(base, spec):
        rebuilds[0] += 1
        return real(base, spec)

    with monkeypatch.context() as m:
        m.setattr(mate_worker, "worker_additions", counted)
        if not shortcut:
            # Every key reads as globbed, so every Remove takes the rebuild.
            m.setattr(mcp_refs, "glob_free", lambda entry: False)
        rows = mate_capabilities.worker_summary("ben", {})["rows"]
    removes = {
        (r["section"], r["id"]): r["remove"]
        for r in rows
        if r["section"] in ("tools", "allowedTools") and r["state"] == "local"
    }
    return removes, rebuilds[0]


def test_every_added_row_offers_the_remove_the_rebuild_offers(monkeypatch):
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, FILE))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, FILE))
    monkeypatch.setattr(agent_specs, "load_agent_spec", lambda *a, **k: dict(BASE))

    (fast, fast_rebuilds), (slow, slow_rebuilds) = (
        _removes(monkeypatch, True),
        _removes(monkeypatch, False),
    )
    assert len(slow) > BULK
    # The direct answer really answered most rows, so the comparison is not vacuous.
    assert slow_rebuilds - fast_rebuilds > BULK
    assert fast == slow
    # The shapes the shortcut must withhold on are really withheld.
    assert slow[("allowedTools", "@notes/write_note")] is None
    assert slow[("allowedTools", "@srv1/x")] is None


ENTRIES = [
    "y",
    "x/y",
    "Y",
    "@s/y",
    "@S/Y",
    "@s/x/y",
    "@s",
    "@s/*",
    "@t?/q",
    "@t1/q",
    "@u/q",
    "q",
    "r*",
    "rx",
    "@v/rx",
    "*",
]


@pytest.mark.parametrize("entries", [ENTRIES, ENTRIES[:-1], [e for e in ENTRIES if "*" not in e]])
def test_other_reach_matches_trying_every_other_entry(entries):
    index = mcp_refs.OtherReach(entries)
    for key in entries:
        if not mcp_refs.glob_free(key):
            continue
        want = any(mcp_refs.entry_reaches(e, key) for e in entries if e != key)
        assert index.reached_by_another(key) is want, key
