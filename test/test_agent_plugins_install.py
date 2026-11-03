"""Installing Agent Plugins from a folder or a git URL (spec §5)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_plugins_helpers import PLUGIN_SCHEMA, skill, symlink

from kiro_crew.agent_plugins import discover_plugins
from kiro_crew.agent_plugins import install as install_mod
from kiro_crew.agent_plugins.install import (
    InstallError,
    install_from_folder,
    install_from_git,
    uninstall,
)
from kiro_crew.testing.links import make_dir_link


def _write(root: Path, files: dict[str, str | dict]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")
    return root


def _standard(root: Path, name: str = "acme", version: str = "1.0.0") -> Path:
    return _write(
        root,
        {
            "plugin.json": {"$schema": PLUGIN_SCHEMA, "name": name, "version": version},
            "skills/greet/SKILL.md": skill("greet"),
        },
    )


def _index(home: Path) -> dict:
    return json.loads((home / "installed.json").read_text(encoding="utf-8"))


def _hidden(home: Path) -> list[str]:
    return sorted(p.name for p in (home / "installed").iterdir() if p.name.startswith("."))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "powers"


def test_standard_folder_installs_and_lands_off(tmp_path: Path, home: Path) -> None:
    src = _standard(tmp_path / "src")
    before = sorted(p.relative_to(src).as_posix() for p in src.rglob("*"))

    result = install_from_folder(src, home=home)

    assert result.name == "acme"
    assert result.conversion is None
    assert result.record.state == "off"
    assert [s.name for s in result.record.skills] == ["greet"]
    assert _index(home) == {"installedPowers": [{"name": "acme"}]}
    (rec,) = discover_plugins(home)
    assert (rec.name, rec.state) == ("acme", "off")
    assert (home / "installed" / "acme" / "skills" / "greet" / "SKILL.md").is_file()
    assert sorted(p.relative_to(src).as_posix() for p in src.rglob("*")) == before
    assert _hidden(home) == []
    assert not (home / "data").exists()


def test_claude_layout_is_converted_then_installed(tmp_path: Path, home: Path) -> None:
    src = _write(
        tmp_path / "src",
        {
            ".claude-plugin/plugin.json": {"name": "Acme Tools", "hooks": "./hooks.json"},
            "skills/greet/SKILL.md": skill("greet"),
        },
    )

    result = install_from_folder(src, home=home)

    assert result.name == "acme-tools"
    assert result.conversion is not None
    assert result.conversion.source_manifest == ".claude-plugin/plugin.json"
    assert result.conversion.not_converted == ["hooks"]
    assert result.record.state == "off"
    manifest = json.loads((home / "installed" / "acme-tools" / "plugin.json").read_text())
    assert manifest["$schema"] == PLUGIN_SCHEMA
    assert _index(home) == {"installedPowers": [{"name": "acme-tools"}]}
    assert not (src / "plugin.json").exists()


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"plugin.json": {"$schema": PLUGIN_SCHEMA, "name": "Bad Name"}}, "agent_plugin_invalid"),
        ({"plugin.json": {"$schema": PLUGIN_SCHEMA}}, "agent_plugin_invalid"),
        ({"POWER.md": "# legacy\n"}, "agent_plugin_not_a_plugin"),
        ({"README.md": "nothing here\n"}, "agent_plugin_not_a_plugin"),
    ],
)
def test_a_rejected_plugin_writes_nothing(
    tmp_path: Path, home: Path, files: dict, code: str
) -> None:
    src = _write(tmp_path / "src", files)

    with pytest.raises(InstallError) as err:
        install_from_folder(src, home=home)

    assert err.value.code == code
    assert not home.exists()


def test_a_rejected_plugin_leaves_an_existing_home_unchanged(tmp_path: Path, home: Path) -> None:
    install_from_folder(_standard(tmp_path / "ok"), home=home)
    index_before = (home / "installed.json").read_bytes()
    src = _write(tmp_path / "bad", {"plugin.json": {"$schema": PLUGIN_SCHEMA, "name": "-x"}})

    with pytest.raises(InstallError):
        install_from_folder(src, home=home)

    assert (home / "installed.json").read_bytes() == index_before
    assert sorted(p.name for p in (home / "installed").iterdir()) == ["acme"]


def test_a_duplicate_name_is_refused(tmp_path: Path, home: Path) -> None:
    install_from_folder(_standard(tmp_path / "one", version="1.0.0"), home=home)

    with pytest.raises(InstallError) as err:
        install_from_folder(_standard(tmp_path / "two", version="2.0.0"), home=home)

    assert err.value.code == "agent_plugin_already_installed"
    manifest = json.loads((home / "installed" / "acme" / "plugin.json").read_text())
    assert manifest["version"] == "1.0.0"
    assert _index(home) == {"installedPowers": [{"name": "acme"}]}
    assert _hidden(home) == []


def test_replace_swaps_the_folder_and_keeps_the_data_dir(tmp_path: Path, home: Path) -> None:
    install_from_folder(_standard(tmp_path / "one", version="1.0.0"), home=home)
    data = home / "data" / "acme"
    data.mkdir(parents=True)
    (data / "state.txt").write_text("kept", encoding="utf-8")

    result = install_from_folder(
        _standard(tmp_path / "two", version="2.0.0"), home=home, replace=True
    )

    assert result.replaced is True
    manifest = json.loads((home / "installed" / "acme" / "plugin.json").read_text())
    assert manifest["version"] == "2.0.0"
    assert (data / "state.txt").read_text(encoding="utf-8") == "kept"
    assert _index(home) == {"installedPowers": [{"name": "acme"}]}
    assert _hidden(home) == []


def test_replacing_an_on_plugin_keeps_it_on(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent_plugins import consent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    (tmp_path / "crew").mkdir()
    install_from_folder(_standard(tmp_path / "one", version="1.0.0"), home=home)
    consent.set_enabled("acme", True)

    install_from_folder(_standard(tmp_path / "two", version="2.0.0"), home=home, replace=True)

    (rec,) = discover_plugins(home)
    assert (rec.name, rec.state) == ("acme", "on")
    assert rec.manifest is not None and rec.manifest.version == "2.0.0"


def test_installed_json_keeps_other_entries_and_unknown_fields(tmp_path: Path, home: Path) -> None:
    home.mkdir()
    original = {
        "version": 3,
        "installedPowers": [
            {"name": "other", "source": {"type": "registry"}, "installedAt": "2026-11-01"},
            {"name": "legacy"},
        ],
        "lastUpdated": "2026-11-01T00:00:00Z",
    }
    (home / "installed.json").write_text(json.dumps(original), encoding="utf-8")

    install_from_folder(_standard(tmp_path / "src"), home=home)

    expected = dict(original)
    expected["installedPowers"] = original["installedPowers"] + [{"name": "acme"}]
    assert _index(home) == expected
    assert list(_index(home)) == ["version", "installedPowers", "lastUpdated"]


def test_an_unreadable_index_is_refused_not_overwritten(tmp_path: Path, home: Path) -> None:
    (home / "installed").mkdir(parents=True)
    (home / "installed.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(InstallError) as err:
        install_from_folder(_standard(tmp_path / "src"), home=home)

    assert err.value.code == "agent_plugin_index_unreadable"
    assert (home / "installed.json").read_text(encoding="utf-8") == "{not json"
    assert list((home / "installed").iterdir()) == []


def test_links_are_not_copied(tmp_path: Path, home: Path) -> None:
    outside = _write(tmp_path / "outside", {"secret.txt": "s", "dir/inner.txt": "i"})
    src = _standard(tmp_path / "src")
    make_dir_link(src / "linked-dir", outside / "dir")
    symlink(src / "linked.txt", outside / "secret.txt")

    result = install_from_folder(src, home=home)

    root = home / "installed" / "acme"
    assert not os.path.lexists(root / "linked-dir")
    assert not os.path.lexists(root / "linked.txt")
    assert any("linked-dir" in note for note in result.notes)
    assert any("linked.txt" in note for note in result.notes)


def test_a_source_inside_the_installed_folder_is_refused(tmp_path: Path, home: Path) -> None:
    install_from_folder(_standard(tmp_path / "src"), home=home)

    with pytest.raises(InstallError) as err:
        install_from_folder(home / "installed" / "acme", home=home, replace=True)

    assert err.value.code == "agent_plugin_bad_source"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///srv/plugin",
        "ssh://git@example.com/a/b.git",
        "git@example.com:a/b.git",
        "ext::sh -c touch% /tmp/pwned",
        "ext::sh",
        "/local/path/plugin",
        "./relative",
        "C:\\plugins\\x",
        "http://example.com/a/b.git",
        "https://user:secret@example.com/a/b.git",
        "https://token@example.com/a/b.git",
        "https:///no-host",
        "https::example.com/a",
        "https://example.com/a b",
        "https://example.com/a\nb",
        "https://example.com:99999/a",
        "https://example.com/a#frag",
    ],
)
def test_git_url_refusals(url: str, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_clone(*_a, **_k):
        raise AssertionError("git must not run for a refused URL")

    monkeypatch.setattr(install_mod, "_run_clone", no_clone)

    with pytest.raises(InstallError) as err:
        install_from_git(url, home=home)

    assert err.value.code == "agent_plugin_bad_git_url"
    assert not home.exists()


@pytest.mark.parametrize("ref", ["--upload-pack=x", "-b", "a..b", "a b", "main.lock", "x/"])
def test_bad_refs_are_refused(ref: str, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_mod, "_run_clone", MagicMock(side_effect=AssertionError))

    with pytest.raises(InstallError) as err:
        install_from_git("https://example.com/a/b.git", ref=ref, home=home)

    assert err.value.code == "agent_plugin_bad_git_ref"


@pytest.mark.parametrize("subdir", ["../x", "a/../../x", "/abs", "C:/abs", "~/x"])
def test_a_subdir_outside_the_clone_is_refused(
    subdir: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_mod, "_run_clone", MagicMock(side_effect=AssertionError))

    with pytest.raises(InstallError) as err:
        install_from_git("https://example.com/a/b.git", subdir=subdir, home=home)

    assert err.value.code == "agent_plugin_bad_subdir"


class _FakeClone:
    """Stands in for ``git clone``: records the call and writes a repository."""

    def __init__(self, files: dict[str, str | dict], returncode: int = 0) -> None:
        self.files = files
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, str], str]] = []

    def __call__(self, argv: list[str], *, env: dict[str, str], cwd: str):
        self.calls.append((argv, env, cwd))
        if self.returncode == 0:
            _write(Path(argv[-1]), self.files)
        proc = MagicMock()
        proc.returncode = self.returncode
        proc.stderr = "Cloning into 'repo'...\nfatal: repository not found\n"
        return proc


def test_git_install_clones_safely_and_installs_the_subdir(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClone(
        {
            "plugins/acme/plugin.json": {"$schema": PLUGIN_SCHEMA, "name": "acme"},
            "plugins/acme/skills/greet/SKILL.md": skill("greet"),
            "README.md": "repo",
        }
    )
    monkeypatch.setattr(install_mod, "_run_clone", fake)

    result = install_from_git(
        "https://example.com/org/repo.git", ref="v1.0", subdir="plugins/acme", home=home
    )

    assert result.record.state == "off"
    assert _index(home) == {"installedPowers": [{"name": "acme"}]}
    assert not (home / "installed" / "acme" / "README.md").exists()
    ((argv, env, cwd),) = fake.calls
    assert argv[:11] == [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "core.symlinks=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
    ]
    assert argv[11:] == [
        "clone",
        "--depth",
        "1",
        "--no-recurse-submodules",
        "--template=",
        "--branch",
        "v1.0",
        "--",
        "https://example.com/org/repo.git",
        argv[-1],
    ]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not Path(cwd).exists()


def test_a_subdir_link_in_the_clone_is_refused(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _standard(tmp_path / "outside")

    def clone(argv: list[str], *, env: dict[str, str], cwd: str):
        dest = Path(argv[-1])
        dest.mkdir()
        make_dir_link(dest / "plugin", outside)
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        return proc

    monkeypatch.setattr(install_mod, "_run_clone", clone)

    with pytest.raises(InstallError) as err:
        install_from_git("https://example.com/a/b.git", subdir="plugin", home=home)

    assert err.value.code == "agent_plugin_bad_subdir"
    assert not home.exists()


def test_a_failed_clone_writes_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClone({}, returncode=128)
    monkeypatch.setattr(install_mod, "_run_clone", fake)

    with pytest.raises(InstallError) as err:
        install_from_git("https://example.com/a/b.git", home=home)

    assert err.value.code == "agent_plugin_clone_failed"
    assert "repository not found" in str(err.value)
    assert not home.exists()
    assert not Path(fake.calls[0][2]).exists()


def test_uninstall_keeps_the_data_dir_and_other_entries(tmp_path: Path, home: Path) -> None:
    home.mkdir()
    (home / "installed.json").write_text(
        json.dumps({"installedPowers": [{"name": "other", "x": 1}], "extra": True}),
        encoding="utf-8",
    )
    install_from_folder(_standard(tmp_path / "src"), home=home)
    data = home / "data" / "acme"
    data.mkdir(parents=True)
    (data / "state.txt").write_text("kept", encoding="utf-8")

    uninstall("acme", home=home)

    assert not os.path.lexists(home / "installed" / "acme")
    assert _index(home) == {"installedPowers": [{"name": "other", "x": 1}], "extra": True}
    assert (data / "state.txt").read_text(encoding="utf-8") == "kept"
    assert _hidden(home) == []


@pytest.mark.parametrize("name", ["missing", "../acme", ""])
def test_uninstall_refuses_what_is_not_installed(name: str, home: Path) -> None:
    (home / "installed").mkdir(parents=True)

    with pytest.raises(InstallError) as err:
        uninstall(name, home=home)

    assert err.value.code == "agent_plugin_not_installed"


def _request(*, user: str, body: dict, app: str = "") -> MagicMock:
    req = MagicMock()
    req.path = "/api/agent-plugins/install"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body)
    return req


@pytest.mark.parametrize(("user", "app"), [("guest", ""), ("owner-1", "notes")])
def test_the_routes_refuse_anyone_but_the_owner(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, user: str, app: str
) -> None:
    from kiro_crew.dashboard.handlers import agent_plugins as handler

    monkeypatch.setenv("KIRO_POWERS_HOME", str(home))
    src = _standard(tmp_path / "src")

    install = asyncio.run(
        handler.api_agent_plugins_install(_request(user=user, app=app, body={"path": str(src)}))
    )
    remove = asyncio.run(
        handler.api_agent_plugins_uninstall(_request(user=user, app=app, body={"name": "acme"}))
    )

    assert (install.status, remove.status) == (403, 403)
    assert json.loads(install.text)["code"] == "dashboard_owner_required"
    assert not home.exists()


def test_the_owner_installs_and_uninstalls_through_the_routes(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import agent_plugins as handler

    monkeypatch.setenv("KIRO_POWERS_HOME", str(home))
    src = _standard(tmp_path / "src")

    resp = asyncio.run(
        handler.api_agent_plugins_install(_request(user="owner-1", body={"path": str(src)}))
    )
    again = asyncio.run(
        handler.api_agent_plugins_install(_request(user="owner-1", body={"path": str(src)}))
    )
    gone = asyncio.run(
        handler.api_agent_plugins_uninstall(_request(user="owner-1", body={"name": "acme"}))
    )

    assert resp.status == 200
    payload = json.loads(resp.text)["plugin"]
    assert (payload["name"], payload["state"], payload["skills"]) == ("acme", "off", ["greet"])
    assert again.status == 409
    assert json.loads(again.text)["code"] == "agent_plugin_already_installed"
    assert gone.status == 200
    assert _index(home) == {"installedPowers": []}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"path": "x", "url": "https://example.com/a"},
        {"path": 3},
        {"path": "x", "replace": "yes"},
    ],
)
def test_the_install_route_rejects_a_malformed_body(body: dict, home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import agent_plugins as handler

    monkeypatch.setenv("KIRO_POWERS_HOME", str(home))

    resp = asyncio.run(handler.api_agent_plugins_install(_request(user="owner-1", body=body)))

    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "agent_plugin_bad_request"
