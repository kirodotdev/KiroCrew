"""No built-in dashboard page may put a value into the DOM as markup.

Every crewmate's Dashboard tab and every root session's Dashboard view runs one of
these pages with scripts on. A page reads fold values and agent-written fields, and
those carry text a person did not write: task titles, summaries, issue text. Through
an HTML sink such text becomes elements and script. Through ``textContent`` it stays
text.

The crew panel templates carry the same rule
(``test_agent_panel_store.py::test_no_shipped_template_builds_markup_from_data``).
This is that rule for the dashboard pages, asserted on the source because it is a
rule about how a page is written, not about one payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew
import kiro_crew.dashboard_templates as dashboard_templates
from kiro_crew.dashboard_templates import catalog

BUILTIN = Path(dashboard_templates.__file__).resolve().parent / "builtin"

PAGES = sorted(BUILTIN.glob("*/template.html"))

GALLERY = Path(kiro_crew.__file__).resolve().parent / "builtin_skills" / "dashboard" / "gallery"
#: The shipped dashboard PACKAGES, the copies an agent starts its own page from.
PACKAGES = sorted(GALLERY.glob("*/package.json"))


def _package_html(path: Path) -> str:
    view = json.loads(path.read_text(encoding="utf-8")).get("view") or {}
    return str(view.get("html") or "")


def test_the_scan_finds_the_shipped_pages() -> None:
    """An empty glob would pass every case below, so the scan must see pages."""
    assert len(PAGES) >= 12, f"expected the built-in pages under {BUILTIN}, found {PAGES}"
    assert PACKAGES, f"expected the gallery packages under {GALLERY}"
    assert all(_package_html(p) for p in PACKAGES), "a gallery package has no view.html"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.parent.name)
def test_no_builtin_page_uses_an_html_sink(page: Path) -> None:
    sink = catalog.page_sink(page.read_text(encoding="utf-8"))
    assert sink is None, (
        f"{page.parent.name}/template.html reaches the DOM through {sink}; "
        "write values with textContent instead"
    )


@pytest.mark.parametrize("package", PACKAGES, ids=lambda p: p.parent.name)
def test_no_gallery_package_uses_an_html_sink(package: Path) -> None:
    sink = catalog.page_sink(_package_html(package))
    assert sink is None, (
        f"gallery/{package.parent.name} view.html reaches the DOM through {sink}; "
        "write values with textContent instead"
    )


@pytest.mark.parametrize(
    "line",
    [
        "el.innerHTML = v",
        "document.writeln(v)",
        "new Function(v)",
        "setTimeout('go()', 5)",
        "new DOMParser()",
    ],
)
def test_the_shared_list_catches_each_kind_of_sink(line: str) -> None:
    assert catalog.page_sink(f"<script>{line}</script>") is not None
    assert catalog.page_sink("<script>el.textContent = v; setTimeout(go, 5)</script>") is None


def test_an_operator_page_with_a_sink_is_a_problem_and_not_served(tmp_path, monkeypatch) -> None:
    """The catalog scan refuses a `user` page the same list refuses, like a bad manifest."""
    src = BUILTIN / "project-report"
    page = tmp_path / "panel-templates" / "my-page"
    page.mkdir(parents=True)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    manifest.update({"id": "my-page", "source": catalog.USER_SOURCE})
    for spec in manifest.get("fields", {}).values():
        spec.pop("upkeep", None)  # only a builtin page names an upkeep helper
    (page / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    html = (src / "template.html").read_text(encoding="utf-8")
    (page / "template.html").write_text(html + "<script>x.innerHTML = 1</script>", encoding="utf-8")

    entries, problems = catalog._scan(page.parent, catalog.USER_SOURCE)
    assert entries == []
    assert [name for name, _why in problems] == ["my-page"]
    assert "innerHTML" in problems[0][1]
