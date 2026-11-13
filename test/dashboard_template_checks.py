"""Checks every built-in dashboard template's test file runs the same way, written once.

Each ``test_dashboard_template_<id>.py`` pins one template with the same few checks:
the shared loader accepts it, its fold paths resolve to the declared types, and its
page reads the host's field bag without reaching the network. A check lives here as a
mixin; a file's test class takes the mixin as a base, so the node id stays
``test_dashboard_template_<id>.py::<Class>::<test>`` and pytest resolves the ``loaded``
and ``html`` fixtures from that file. A check reads the file's own constants and
helpers (``TEMPLATE_ID``, ``POD``, ``_folds`` ...) through :func:`_file_module`.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS


def _file_module(test: object) -> ModuleType:
    """The test file the class running this check is defined in."""
    return sys.modules[type(test).__module__]


class ItReadsTheHostsOwnFieldBag:
    def test_it_reads_the_hosts_own_field_bag(self, html: str) -> None:
        assert "window.kirocrew" in html


class TheSharedLoaderAcceptsIt:
    def test_the_shared_loader_accepts_it(self, loaded: tuple[Any, str]) -> None:
        here = _file_module(self)
        manifest, page = loaded
        assert manifest.id == here.TEMPLATE_ID
        assert manifest.source == "builtin"
        assert page.strip()
        assert len(manifest.fields) <= MAX_FIELDS


class ItRendersOnLoadAndOnEveryMessage:
    def test_it_renders_on_load_and_on_every_message(self, html: str) -> None:
        assert "window.addEventListener('load', render)" in html
        assert "addEventListener('kirocrew-dashboard'" in html


class EachResolvedValueHasTheDeclaredType:
    def test_each_resolved_value_has_the_declared_type(self, loaded: tuple[Any, str]) -> None:
        here = _file_module(self)
        manifest, _ = loaded
        folds = here._folds(here.POD)
        wrong: list[str] = []
        for spec in manifest.fields.values():
            if not spec.fold:
                continue
            found, value = here._walk(folds.get(spec.fold), spec.path)
            if not found or value is None:
                continue
            got = here._type_of(value)
            if got != spec.type:
                wrong.append(f"{spec.name} declares {spec.type!r} but served {got!r}")
        assert not wrong, "; ".join(wrong)


class ItReachesNoNetwork:
    def test_it_reaches_no_network(self, html: str) -> None:
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"


class ItReadsOnlyTheWorkFold:
    def test_it_reads_only_the_work_fold(self, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        assert set(manifest.folds) == {"work"}, f"reads {sorted(manifest.folds)}"
