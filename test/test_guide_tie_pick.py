"""How ``find_ui`` settles a tie between results it would otherwise ask about.

Every way a tie is settled -- one control, the one guided result, the one on
screen, the feature's own page -- ends at one safety check: a pick that
removes something (its ``find_ref.params.caution``) or is the agent's own
ceiling is asked about instead, never chosen by elimination.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import mcp_guide


def _row(
    rid: str,
    label: str,
    *,
    page: str = "page.apps-library",
    route: str = "/apps/library",
    caution: bool = False,
    live: str | None = None,
    guided: bool = True,
) -> dict[str, Any]:
    params: dict[str, Any] = {"label": label, "route": route}
    if caution:
        params["caution"] = True
    row: dict[str, Any] = {
        "id": rid,
        "placements": [{"route": route, "path": [{"id": page}]}],
    }
    if guided:
        row["find_ref"] = {"action_id": "ui.find", "params": params}
    if live:
        row["live"] = {"status": live}
    return row


def _pick(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return mcp_guide._tie_pick(rows, rows, len(rows))


def _same(caution: bool) -> list[dict[str, Any]]:
    # Two results that are one control: the same guide target.
    a = _row("apps.one", "Remove", caution=caution)
    b = _row("apps.two", "Remove", caution=caution)
    return [a, b]


def _only_guided(caution: bool) -> list[dict[str, Any]]:
    return [
        _row("apps.thing", "Remove", caution=caution),
        _row("page.webhooks", "Webhooks", page="page.webhooks", guided=False),
    ]


def _on_screen(caution: bool) -> list[dict[str, Any]]:
    return [
        _row("apps.here", "Remove", caution=caution, live="pointable"),
        _row("agents.there", "Remove x", page="page.customize", route="/customize"),
    ]


def _home(caution: bool) -> list[dict[str, Any]]:
    return [
        _row("members.remove", "Remove", page="page.members", route="/members", caution=caution),
        _row("agents.remove", "Remove y", page="page.customize", route="/customize"),
    ]


@pytest.mark.parametrize("build", [_same, _only_guided, _on_screen, _home])
def test_each_settling_branch_picks_a_plain_result(build: Any) -> None:
    rows = build(False)
    assert _pick(rows) is rows[0]


@pytest.mark.parametrize("build", [_same, _only_guided, _on_screen, _home])
def test_each_settling_branch_never_picks_a_caution_find_ref(build: Any) -> None:
    # The flag lives in find_ref.params, the shape find_ui hands out.
    rows = build(True)
    assert rows[0]["find_ref"]["params"]["caution"] is True
    assert "caution" not in rows[0] and "caution" not in rows[0]["find_ref"]
    assert _pick(rows) is None


@pytest.mark.parametrize("build", [_same, _only_guided, _on_screen, _home])
def test_each_settling_branch_never_picks_a_trust_root(
    build: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import guide_catalog

    rows = build(False)
    root = rows[0]["id"]
    monkeypatch.setattr(guide_catalog, "is_trust_root", lambda rid, _routes: rid == root)
    assert _pick(rows) is None


def test_a_top_level_caution_flag_still_refuses() -> None:
    rows = _same(False)
    rows[0]["caution"] = True
    assert _pick(rows) is None
