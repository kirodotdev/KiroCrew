"""An installed app's pinned update during a catalog outage reports the outage.

Sibling of ``test_catalog_inventory.py``'s ``TestCatalogFailureBlocksExternalFallbackToo``,
kept in its own module because that file sits at the line cap.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.apps import registry as reg

SHA = "a" * 40
URL = "https://github.com/org/demo-app"


def catalog_git(**over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": "demo-app",
        "source": {"type": "git", "url": URL, "ref": SHA},
        "version": "1.2.3",
    }
    entry.update(over)
    return entry


class TestAPinnedUpdateReportsTheOutage:
    """An installed app that records its source updates through the pinned branch.

    That branch swallowed the catalog failure into an empty candidate set, so an
    outage read as "no registry entry matches its recorded source" and sent the
    user looking for a re-homed app. It must refuse exactly as before, reaching no
    external cache, while saying the catalog could not be consulted.
    """

    def _pinned(self, monkeypatch, fetch):
        monkeypatch.setattr(reg, "get_app", lambda n: {"name": n, "sourceUrl": URL})
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg.official_catalog, "fetch_document", fetch)
        monkeypatch.setattr(reg.official_catalog, "_read_cache", lambda: None)

        # A configured registry MUST exist, or the external loop iterates nothing
        # and the no-fallback assertion holds with the refusal removed.
        class _Reg:
            name = "ext"
            repo = "o/ext"

        class _Cfg:
            registries = [_Reg()]

            @staticmethod
            def load():
                return _Cfg()

        import kiro_crew.config.loader as loader

        monkeypatch.setattr(loader, "KiroCrewConfig", _Cfg)
        # Same URL as the recorded source and no `_registry` marker: the row a
        # provenance match would accept if resolution ever reached the caches.
        monkeypatch.setattr(
            reg,
            "_read_external_registry_cache",
            lambda *a, **k: [{"name": "demo-app", "gitUrl": URL, "repo": URL}],
        )
        monkeypatch.setattr(reg, "_apply_configured_branch", lambda rows, reg_cfg: None)

    def test_an_unreachable_catalog_is_reported_as_the_outage(self, monkeypatch):
        self._pinned(monkeypatch, lambda url: None)
        entry, error = reg._resolve_install_entry("demo-app")
        assert entry is None, "a same-source cache row must not answer for an outage"
        assert "could not be reached" in error
        assert "matches its recorded source" not in error

    def test_a_listed_app_without_an_install_source_says_so(self, monkeypatch):
        doc = {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [catalog_git(source={"type": "builtin"})],
        }
        self._pinned(monkeypatch, lambda url: doc)
        entry, error = reg._resolve_install_entry("demo-app")
        assert entry is None
        assert "no install source" in error
        assert "could not be reached" not in error

    def test_the_reason_omits_the_name_and_exception_text(self, monkeypatch):
        """The reason is returned to the caller and SEL-logged verbatim."""
        self._pinned(monkeypatch, lambda url: None)

        def _unavailable(name):
            raise reg.official_catalog.CatalogUnavailable(f"rejected {name} embedded-secret")

        monkeypatch.setattr(reg.official_catalog, "inventory_for_install", _unavailable)
        entry, error = reg._resolve_install_entry("demo-app")
        assert entry is None
        assert "embedded-secret" not in error
        assert "demo-app" not in error

    def test_the_update_makes_a_real_fetch_under_the_failure_memory(self, monkeypatch):
        """The update click is the install's own lookup, as on the fresh branch.

        A listing-page failure must not refuse it from memory once the catalog is
        back; the refusal above still holds when that real fetch fails.
        """
        doc = {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [catalog_git()],
        }
        self._pinned(monkeypatch, lambda url: doc)
        monkeypatch.setattr(reg.official_catalog, "_failed_recently", lambda: True)
        entry, error = reg._resolve_install_entry("demo-app")
        assert error == ""
        assert entry is not None and entry.get("commit") == SHA

    def test_an_unexpected_lookup_error_still_offers_no_candidate(self, monkeypatch):
        """Scope: only the catalog's own refusal propagates; any other error keeps
        the fail-closed empty set and still reaches no external cache."""
        self._pinned(monkeypatch, lambda url: None)

        def _boom(name):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(reg.official_catalog, "inventory_for_install", _boom)
        assert reg._registry_app_candidates("demo-app") == []
        entry, error = reg._resolve_install_entry("demo-app")
        assert entry is None and error
