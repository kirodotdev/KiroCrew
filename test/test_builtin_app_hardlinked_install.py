"""A hardlinking installer's built-in app agents keep their owning app.

An installer that hardlinks package files out of its cache leaves every file of
the installed package with a second link, and the agent-spec reader refuses a
hardlinked file. Without an exception the owner lookup reads no shipped
builtin's ``app.json`` or agent specs, and every built-in app agent reads as a
custom agent. The lookup admits such a file on CONTENT, as the built-in skill
reader does: it must be a ``.json`` file the distribution RECORD lists under a
built-in app's package directory, read from that shipped root, with bytes that
hash to the recorded digest. A hardlinked spec anywhere else stays refused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.apps import execution as execution_mod
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps.manager import InstalledApp, _write_installed
from kiro_crew.apps.manifest_containment import _declared_agent_names

APP = "demo-app"
LABEL = "Demo App"
MANIFEST = {"name": APP, "version": "1.0.0", "displayName": LABEL, "agents": ["agents/w.json"]}
SPEC = {"name": "worker", "prompt": "p", "tools": []}


def _record_digest(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _link_or_skip(source: Path, second_name: Path) -> None:
    try:
        os.link(source, second_name)
    except OSError as exc:  # pragma: no cover - a filesystem without hardlinks
        pytest.skip(f"hardlinks unavailable here: {exc}")


class _FakeInstall:
    """A site dir whose ``kiro_crew`` package ships one built-in app, hardlinked."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        site = tmp_path / "site"
        self.package = site / "kiro_crew"
        self.root = self.package / "apps" / "builtins" / "demo_app"
        (self.root / "agents").mkdir(parents=True)
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        self.manifest = self.root / manager_mod.APP_MANIFEST_FILENAME
        self.manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        self.spec = self.root / "agents" / "w.json"
        self.spec.write_text(json.dumps(SPEC), encoding="utf-8")
        self.cache = tmp_path / "installer-cache"
        self.cache.mkdir()
        _link_or_skip(self.manifest, self.cache / "app.json")
        _link_or_skip(self.spec, self.cache / "w.json")
        self.dist_info = site / "kirocrew-0.0.0.dist-info"
        self.dist_info.mkdir()
        self.write_record(spec_bytes=self.spec.read_bytes())
        monkeypatch.setattr(skills_mod, "_INSTALLED_PACKAGE_DIR", self.package)
        monkeypatch.setattr(skills_mod, "_package_record_cache", {})
        _write_installed(APP, InstalledApp(name=APP, version="1.0.0", enabled=True))

    def write_record(self, *, spec_bytes: bytes, extra: tuple[Path, ...] = ()) -> None:
        site = self.package.parent
        manifest_bytes = self.manifest.read_bytes()
        lines = [
            f"kiro_crew/__init__.py,{_record_digest(b'')},0",
            f"{self.manifest.relative_to(site).as_posix()},{_record_digest(manifest_bytes)},1",
            f"{self.spec.relative_to(site).as_posix()},{_record_digest(spec_bytes)},1",
            *(
                f"{p.relative_to(site).as_posix()},{_record_digest(p.read_bytes())},1"
                for p in extra
            ),
            f"{self.dist_info.name}/RECORD,,",
        ]
        (self.dist_info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def ship(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self.root
        monkeypatch.setattr(
            manager_mod, "shipped_builtin_app_root", lambda name: root if name == APP else None
        )


OWNER = manager_mod.AgentOwner(app=APP, label=LABEL, enabled=True)


def test_a_recorded_builtin_keeps_its_agent_owner(tmp_path, monkeypatch):
    install = _FakeInstall(tmp_path, monkeypatch)
    install.ship(monkeypatch)
    assert install.manifest.stat().st_nlink == 2 and install.spec.stat().st_nlink == 2

    assert manager_mod.owning_app_for_agent("worker") == OWNER
    assert manager_mod.owning_app_for_agent(f"{APP}--worker") == OWNER


def test_a_spec_whose_bytes_differ_from_the_record_is_refused(tmp_path, monkeypatch):
    install = _FakeInstall(tmp_path, monkeypatch)
    install.ship(monkeypatch)
    install.write_record(spec_bytes=b'{"name": "what the installer wrote"}')

    assert manager_mod.owning_app_for_agent("worker") is None


def test_a_hardlink_outside_the_shipped_root_is_refused(tmp_path, monkeypatch):
    """The data-home copy shares the recorded inode and bytes, but not the recorded path."""
    install = _FakeInstall(tmp_path, monkeypatch)
    tree = manager_mod.app_dir(APP)
    (tree / "agents").mkdir(parents=True)
    _link_or_skip(install.manifest, tree / manager_mod.APP_MANIFEST_FILENAME)
    _link_or_skip(install.spec, tree / "agents" / "w.json")
    monkeypatch.setattr(manager_mod, "shipped_builtin_app_root", lambda name: None)

    assert manager_mod.owning_app_for_agent("worker") is None


def test_a_recorded_file_read_as_an_unshipped_tree_is_refused(tmp_path, monkeypatch):
    """RECORD alone admits nothing: the read must come from the shipped root."""
    install = _FakeInstall(tmp_path, monkeypatch)
    monkeypatch.setattr(manager_mod, "shipped_builtin_app_root", lambda name: None)
    monkeypatch.setattr(manager_mod, "apps_dir", lambda: install.root.parent)
    (install.root / manager_mod.INSTALLED_META_FILENAME).write_text(
        json.dumps({"name": APP, "version": "1.0.0", "enabled": True}), encoding="utf-8"
    )
    assert manager_mod.owning_app_for_agent("worker") is None


def test_command_agent_containment_reads_a_recorded_builtin_spec(tmp_path, monkeypatch):
    install = _FakeInstall(tmp_path, monkeypatch)
    root = install.root.resolve()
    monkeypatch.setattr(
        execution_mod, "shipped_builtin_app_name_at", lambda p: APP if p == root else None
    )
    assert _declared_agent_names(["agents/w.json"], install.root) == {"worker"}

    install.write_record(spec_bytes=b"{}")
    monkeypatch.setattr(skills_mod, "_package_record_cache", {})
    assert _declared_agent_names(["agents/w.json"], install.root) == set()


def test_a_recorded_json_the_manifest_does_not_declare_is_refused(tmp_path, monkeypatch):
    """RECORD lists every shipped ``.json``; only app.json and the declared agents are admitted."""
    from kiro_crew import skills_discovery

    install = _FakeInstall(tmp_path, monkeypatch)
    extra = install.root / "agents" / "fixture.json"
    extra.write_text(json.dumps({"name": "fixture"}), encoding="utf-8")
    _link_or_skip(extra, install.cache / "fixture.json")
    install.write_record(spec_bytes=install.spec.read_bytes(), extra=(extra,))
    assert extra.stat().st_nlink == 2

    real = str(extra.resolve())
    # RECORD alone would admit it: the file is listed with a matching digest.
    assert skills_discovery._installed_builtin_app_file_match(real, extra.read_bytes())

    admit = skills_discovery._builtin_app_file_admission(
        install.root, [manager_mod.APP_MANIFEST_FILENAME, "agents/w.json"]
    )
    assert admit(real, extra.read_bytes()) is False
    assert admit(str(install.spec.resolve()), install.spec.read_bytes()) is True
    assert admit(str(install.manifest.resolve()), install.manifest.read_bytes()) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX os.replace over an open file")
def test_a_spec_replaced_while_it_is_read_still_reads(tmp_path, monkeypatch):
    """A writer's os.replace leaves the open inode at 0 links: that read is valid."""
    import kiro_crew.bounded_read as bounded_read_mod
    from kiro_crew.agent_discovery_parts.spec_reads import _read_spec_bytes

    spec = tmp_path / "kirocrew.json"
    spec.write_bytes(b'{"name": "old"}')
    fresh = tmp_path / "fresh.json"
    fresh.write_bytes(b'{"name": "new"}')
    real_read = bounded_read_mod.bounded_read

    def read_then_replace(fh, limit):
        data = real_read(fh, limit)
        os.replace(fresh, spec)
        return data

    monkeypatch.setattr(bounded_read_mod, "bounded_read", read_then_replace)

    assert _read_spec_bytes(spec.resolve()) == b'{"name": "old"}'
