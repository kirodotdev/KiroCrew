"""Regression: reverting an artifact restores the render kind of the target version.

``ArtifactStore`` records a per-version ``version_kinds`` map specifically so a
revert restores the right render-mode (see the comment on the snapshot branch of
``artifacts_writes.update``). A revert sends only content + ``from_version`` and no
``kind``, so before the fix the new version was stamped with the CURRENT kind: a
widget pulled to ``html`` upstream and then reverted stayed ``html``.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.artifacts import ArtifactStore

_WIDGET = "<div id='w'>hello widget</div>"
_PULLED_DOC = "<!doctype html><html><body><p>upstream page</p></body></html>"


def test_revert_restores_the_target_versions_render_kind(tmp_path: Path) -> None:
    store = ArtifactStore(root=tmp_path / "artifacts")
    art = store.create(name="Widget", content=_WIDGET, kind="widget")
    assert art.version == 1 and art.version_kinds == {"1": "widget"}

    # An upstream pull rewrites the widget to a foreign html document (v2).
    pulled = store.update(art.slug, content=_PULLED_DOC, kind="html", snapshot=True)
    assert pulled.version == 2 and pulled.kind == "html"

    # Revert to v1 exactly as the website and MCP revert callers do: the content
    # of the target version, event_type="reverted", from_version, and NO kind.
    target = store.get(art.slug, version=1)
    assert target.kind == "widget"
    store.update(
        art.slug,
        content=target.content,
        event_type="reverted",
        from_version=1,
        snapshot=True,
    )

    live = store.get(art.slug)
    assert live.version == 3
    assert live.content == _WIDGET
    assert live.kind == "widget", f"revert left kind={live.kind!r}, not the v1 widget kind"
    assert live.version_kinds.get("3") == "widget", live.version_kinds


_DASHBOARD_PACKAGE = {
    "kind": "dashboard",
    "bound_to": "crewmate:mate",
    "model": {
        "types": {
            "open_prs": {
                "type": "number",
                "label": "Open PRs",
                "source": {"fold": "work", "path": "summary.open_prs"},
            }
        }
    },
    "view": {"html": '<h2 data-dashboard-field="open_prs"></h2>'},
    "theme": {"tokens": {"--panel-bg": "oklch(21% 0 0)"}},
}


def test_revert_never_restores_a_dashboard_kind_past_its_write_gate(tmp_path: Path) -> None:
    store = ArtifactStore(root=tmp_path / "artifacts")
    art = store.create(name="Board", kind="dashboard", content=json.dumps(_DASHBOARD_PACKAGE))
    assert art.version_kinds == {"1": "dashboard"}

    # The owner converts the dashboard to prose (v2), naming the read it acts on.
    token = store.get(art.slug, assign_token=True).content_token
    store.update(art.slug, content="# notes", kind="markdown", snapshot=True, snapshot_token=token)
    assert store.get(art.slug).kind == "markdown"

    # Reverting to v1 sends v1's bytes with no kind. The dashboard gate ran against
    # ``markdown``, so the record must not come back as a dashboard.
    store.update(
        art.slug,
        content=store.get(art.slug, version=1).content,
        event_type="reverted",
        from_version=1,
        snapshot=True,
    )

    live = store.get(art.slug)
    assert live.kind == "markdown", f"revert restored kind={live.kind!r} past the dashboard gate"
    assert live.version_kinds.get(str(live.version)) == "markdown", live.version_kinds
