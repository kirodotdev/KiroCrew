"""Regression: reverting an artifact restores the render kind of the target version.

``ArtifactStore`` records a per-version ``version_kinds`` map specifically so a
revert restores the right render-mode (see the comment on the snapshot branch of
``artifacts_writes.update``). A revert sends only content + ``from_version`` and no
``kind``, so before the fix the new version was stamped with the CURRENT kind: a
widget pulled to ``html`` upstream and then reverted stayed ``html``.
"""

from __future__ import annotations

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
