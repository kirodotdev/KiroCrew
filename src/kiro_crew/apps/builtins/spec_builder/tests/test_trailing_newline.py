"""A trailing newline is not part of a spec name, slot key or document hash.

Python's ``$`` also matches just before a final ``"\\n"``, so a ``$``-anchored
``.match`` would accept ``"live\\n"`` as a second spelling of ``"live"``. The values
reach a directory name, a session file name and the approval record, so each
check ends at ``\\Z``. No test here creates a file or directory whose name holds a
newline: Windows refuses such names, and CI runs there.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _make_client,
    _never_touch_the_real_state,
    _seed_spec,
    _spec_identity,
    _state_for,
)

_DIGEST = "0123456789abcdef" * 4
_SUFFIX = "0123abcd"


def test_a_spec_name_ending_in_a_newline_is_not_usable():
    assert routes._usable_name("live")
    assert not routes._usable_name("live\n")


def test_a_slot_key_ending_in_a_newline_is_not_owned_by_the_spec():
    assert routes._owns_slot_key("live", "spec-builder-live")
    assert routes._owns_slot_key("live", f"spec-builder-live-{_SUFFIX}")
    assert not routes._owns_slot_key("live", "spec-builder-live\n")
    assert not routes._owns_slot_key("live", f"spec-builder-live-{_SUFFIX}\n")


def test_an_approved_hash_ending_in_a_newline_is_not_an_approval():
    docs = {"requirements.md": {"hash": _DIGEST}}
    ok = routes._normalize_approvals({"requirements": {"hash": _DIGEST}}, docs)
    assert ok["requirements"]["stale"] is False
    assert routes._normalize_approvals({"requirements": {"hash": _DIGEST + "\n"}}, docs) == {}


def test_the_index_drops_an_entry_whose_name_ends_in_a_newline(tmp_path):
    """index.json is writable by the agent, so a key with a newline can be planted."""
    _seed_spec(tmp_path)
    entry = routes._load_index()["live"]
    routes._INDEX_PATH.write_text(
        json.dumps({"live": entry, "live\n": {**entry, "slot_key": "spec-builder-live\n"}}),
        encoding="utf-8",
    )

    assert set(routes._load_index()) == {"live"}


def test_the_index_ignores_a_slot_key_that_ends_in_a_newline(tmp_path):
    """The key becomes a session file name, so the one in the index must not carry it."""
    routes._INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"spec_dir": str(tmp_path / "spec"), "slot_key": f"spec-builder-live-{_SUFFIX}\n"}
    routes._INDEX_PATH.write_text(json.dumps({"live": entry}), encoding="utf-8")

    assert "live" in routes._load_index()
    assert routes._slot_key("live") == "spec-builder-live"


@pytest.mark.asyncio
async def test_a_claimed_hash_ending_in_a_newline_is_refused_as_malformed(tmp_path, monkeypatch):
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reviewed"})
    digest = routes._sha256_text("# reviewed")

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={**_spec_identity(), "phase": "requirements", "hash": digest + "\n"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 400 and body["code"] == "invalid_hash"
    assert "approvals" not in routes._load_index()["live"]
