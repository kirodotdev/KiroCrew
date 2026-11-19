"""The shipped-builtin scan is kept between asks, and dropped when it can be wrong.

An agent-owner lookup asks ``shipped_builtin_app_root`` once per installed app, and
a scan resolves every builtin directory and reads every ``app.json``, so the scan
is kept rather than repeated per ask.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps import execution


@pytest.fixture
def edition_source(tmp_path, monkeypatch):
    """An edition source holding two builtins, the only source besides an empty core."""
    import kiro_crew.platform as platform_mod

    core = tmp_path / "core-builtins"
    core.mkdir()
    source = tmp_path / "edition-builtins"
    for name in ("alpha", "beta"):
        root = source / name
        root.mkdir(parents=True)
        (root / "app.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    context = SimpleNamespace(apps_loader=SimpleNamespace(manifest_sources=lambda: [source]))
    monkeypatch.setattr(execution, "_BUILTINS_DIR", core)
    monkeypatch.setattr(platform_mod, "current_context", lambda: context)
    # Restored after the test, so no scan of these temporary sources outlives it.
    monkeypatch.setattr(execution, "_shipped_cache", None, raising=False)
    return source


@pytest.fixture
def manifest_reads(monkeypatch):
    """How many ``app.json`` files were read since the fixture started."""
    seen = {"reads": 0}
    real = Path.read_text

    def counting(self, *args, **kwargs):
        if self.name == "app.json":
            seen["reads"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return seen


def test_repeated_asks_read_each_manifest_once(edition_source, manifest_reads):
    for _ in range(50):
        assert execution.shipped_builtin_app_root("beta") == (edition_source / "beta").resolve()
        assert execution.shipped_builtin_app_root("missing") is None

    assert manifest_reads["reads"] == 2, "every ask re-read the shipped manifests"


def test_a_builtin_added_to_a_source_is_seen(edition_source):
    assert execution.shipped_builtin_app_root("gamma") is None

    root = edition_source / "gamma"
    root.mkdir()
    (root / "app.json").write_text(json.dumps({"name": "gamma"}), encoding="utf-8")
    # A directory created in the same clock tick as the scan keeps the source's mtime;
    # two seconds clears even a filesystem with two-second timestamps.
    st = edition_source.stat()
    os.utime(edition_source, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    assert execution.shipped_builtin_app_root("gamma") == root.resolve()


def test_an_invalidation_rescans_a_manifest_changed_in_place(edition_source):
    assert execution.shipped_builtin_app_root("alpha") is not None

    (edition_source / "alpha" / "app.json").write_text(
        json.dumps({"name": "alpha-renamed"}), encoding="utf-8"
    )
    execution.invalidate_shipped_builtin_apps()

    assert execution.shipped_builtin_app_root("alpha") is None
    assert execution.shipped_builtin_app_root("alpha-renamed") is not None


def test_an_app_record_write_drops_the_scan(edition_source, tmp_path, monkeypatch):
    from kiro_crew.apps.manager import InstalledApp, _write_installed

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    assert execution.shipped_builtin_app_root("alpha") is not None
    (edition_source / "alpha" / "app.json").write_text(
        json.dumps({"name": "alpha-renamed"}), encoding="utf-8"
    )

    _write_installed("some-app", InstalledApp(name="some-app", version="1.0.0"))

    assert execution.shipped_builtin_app_root("alpha-renamed") is not None
