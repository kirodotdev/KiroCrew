"""An edition's own featured sections (``editorial`` on a pinned registry row).

What this file pins: only a registry the build pins at the ``owner`` tier AND
opts in has its ``editorial.json`` read; the document goes through the official
projector with artwork dropped; its blocks come back marked ``useAppArt``; the
copy is replaced with the index and removed when the clone stops carrying one;
and the listing puts these blocks ahead of the official ones.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os

import pytest

from kiro_crew.apps import default_installs as di
from kiro_crew.apps import edition_editorial as ee
from kiro_crew.apps import registry as reg_mod
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultAppsLoader

REGISTRY_REPO = "https://forge.example.com/org/app-registry.git"

DOC = {
    "schemaVersion": 1,
    "sections": [
        {
            "form": "full",
            "items": [
                {
                    "type": "app",
                    "appRef": "hover-cards",
                    "blurb": "See what a link points to.",
                    "artwork": {"ref": "assets/editorial/art.png", "alt": "art"},
                }
            ],
        }
    ],
}


class _Loader(DefaultAppsLoader):
    def __init__(self, registries) -> None:
        self._registries = registries

    def default_registries(self):
        return self._registries


def _pinned(trust: str = "owner", editorial: object = True) -> list[dict]:
    row: dict[str, object] = {
        "name": "internal",
        "repo": REGISTRY_REPO,
        "branch": "main",
        "trust": trust,
    }
    if editorial is not None:
        row["editorial"] = editorial
    return [row]


_REAL_LOAD = KiroCrewConfig.load


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

    def _load(cls, *a, **k):
        cfg = _REAL_LOAD()
        cfg.registries = []  # registries come from the edition only
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_load))
    _invalidate_config_cache()
    yield tmp_path
    _invalidate_config_cache()


def _edition(monkeypatch, registries) -> None:
    ctx = dataclasses.replace(
        build_default_context(KiroCrewConfig()), apps_loader=_Loader(registries)
    )
    monkeypatch.setattr(di, "current_context", lambda: ctx)
    monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)


def _the_registry():
    return reg_mod._effective_registries()[0]


class TestOptIn:
    def test_a_pinned_owner_row_with_editorial_true_is_opted_in(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        assert [r.name for r in ee.opted_in_registries()] == ["internal"]
        assert ee.registry_opted_in(_the_registry())

    @pytest.mark.parametrize("flag", [None, False, "true", 1])
    def test_anything_but_literal_true_is_not_an_opt_in(self, home, monkeypatch, flag):
        _edition(monkeypatch, _pinned(editorial=flag))
        assert ee.opted_in_registries() == []
        assert not ee.registry_opted_in(_the_registry())

    def test_a_row_below_the_owner_tier_is_ignored_and_named_by_its_flag(
        self, home, monkeypatch, caplog
    ):
        _edition(monkeypatch, _pinned(trust="index"))
        with caplog.at_level("ERROR"):
            assert ee.opted_in_registries() == []
        assert any("sets editorial but" in r.getMessage() for r in caplog.records)

    def test_the_default_install_flag_does_not_opt_into_editorial(self, home, monkeypatch):
        # Two keys, two decisions: allowing default installs says nothing about
        # featuring, and the reverse.
        row = _pinned(editorial=None)
        row[0]["allowDefaultInstall"] = True
        _edition(monkeypatch, row)
        assert ee.opted_in_registries() == []
        assert [r.name for r in di._allowed_registries()] == ["internal"]


class TestReadDocument:
    def test_reads_a_json_object(self, tmp_path):
        (tmp_path / "editorial.json").write_text(json.dumps(DOC))
        assert ee.read_document(tmp_path) == DOC

    def test_absent_is_none(self, tmp_path):
        assert ee.read_document(tmp_path) is None

    @pytest.mark.parametrize("text", ["[1, 2]", "not json", '"a string"'])
    def test_anything_but_an_object_is_none(self, tmp_path, text):
        (tmp_path / "editorial.json").write_text(text)
        assert ee.read_document(tmp_path) is None

    def test_an_oversized_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ee, "MAX_BYTES", 10)
        (tmp_path / "editorial.json").write_text(json.dumps(DOC))
        assert ee.read_document(tmp_path) is None

    def test_a_deeply_nested_document_is_invalid_not_a_crash(self, tmp_path):
        depth = 20000
        (tmp_path / "editorial.json").write_text("[" * depth + "]" * depth)
        assert ee.read_document(tmp_path) is None

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_symlink_is_refused(self, tmp_path):
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps(DOC))
        (tmp_path / "clone").mkdir()
        (tmp_path / "clone" / "editorial.json").symlink_to(target)
        assert ee.read_document(tmp_path / "clone") is None


class TestSections:
    def test_blocks_come_from_the_cached_copy_without_artwork(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        ee.write_cache(_the_registry(), DOC)
        assert ee.load_sections() == [
            {
                "form": "full",
                "items": [
                    {
                        "type": "app",
                        "appRefs": ["hover-cards"],
                        "blurb": "See what a link points to.",
                    }
                ],
                "useAppArt": True,
            }
        ]

    def test_an_unsupported_version_yields_nothing(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        ee.write_cache(_the_registry(), {**DOC, "schemaVersion": 2})
        assert ee.load_sections() == []

    def test_a_copy_left_by_a_registry_no_longer_opted_in_is_not_shown(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        ee.write_cache(_the_registry(), DOC)
        _edition(monkeypatch, _pinned(editorial=False))
        assert ee.load_sections() == []

    def test_writing_none_removes_the_copy(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        ee.write_cache(_the_registry(), DOC)
        ee.write_cache(_the_registry(), None)
        assert ee.load_sections() == []


class TestIndexFetch:
    """The index fetch asks for the document only for an opted-in registry."""

    def _fake_fetch(self, monkeypatch, doc):
        calls: list[dict] = []

        async def _fetch(repo, branch, **kwargs):
            calls.append(kwargs)
            if "editorial_out" in kwargs:
                kwargs["editorial_out"].append(doc)
            return [{"name": "hover-cards", "repo": "https://forge.example.com/org/app.git"}]

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _fetch)
        return calls

    def test_an_opted_in_fetch_keeps_the_document(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        calls = self._fake_fetch(monkeypatch, DOC)
        asyncio.run(reg_mod._fetch_and_cache_external_registry(_the_registry()))
        assert "editorial_out" in calls[0]
        assert [b["items"][0]["appRefs"] for b in ee.load_sections()] == [["hover-cards"]]

    def test_a_clone_without_the_file_removes_the_old_copy(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        ee.write_cache(_the_registry(), DOC)
        self._fake_fetch(monkeypatch, None)
        asyncio.run(reg_mod._fetch_and_cache_external_registry(_the_registry()))
        assert ee.load_sections() == []

    def test_the_editorial_copy_is_written_before_the_index(self, home, monkeypatch):
        _edition(monkeypatch, _pinned())
        self._fake_fetch(monkeypatch, DOC)
        order: list[str] = []
        real_editorial, real_index = ee.write_cache, reg_mod._write_external_registry_cache
        monkeypatch.setattr(
            ee, "write_cache", lambda *a: (order.append("editorial"), real_editorial(*a))[1]
        )
        from kiro_crew.apps.registry_pipeline import indexes

        monkeypatch.setattr(
            indexes,
            "_write_external_registry_cache",
            lambda *a: (order.append("index"), real_index(*a))[1],
        )
        asyncio.run(reg_mod._fetch_and_cache_external_registry(_the_registry()))
        assert order == ["editorial", "index"]

    def test_a_registry_not_opted_in_is_fetched_exactly_as_before(self, home, monkeypatch):
        _edition(monkeypatch, _pinned(editorial=None))
        calls = self._fake_fetch(monkeypatch, DOC)
        asyncio.run(reg_mod._fetch_and_cache_external_registry(_the_registry()))
        assert calls == [{}]
