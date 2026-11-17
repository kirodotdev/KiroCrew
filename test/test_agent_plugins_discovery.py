"""Agent Plugins discovery, manifest and skills rules (spec 1.0.0 §4-§7.1)."""

from __future__ import annotations

import builtins
import errno
import json
import os
import threading
from pathlib import Path

import pytest
from agent_plugins_helpers import PLUGIN_SCHEMA, install, mcp, skill, symlink

from kiro_crew.agent_plugins import discover_plugins, discovery
from kiro_crew.agent_plugins import manifest as manifest_module
from kiro_crew.agent_plugins import powers_home
from kiro_crew.agent_plugins import skills as skills_module
from kiro_crew.agent_plugins.manifest import ManifestError, parse_manifest
from kiro_crew.agent_plugins.model import Discovery, Issue
from kiro_crew.agent_plugins.skills import frontmatter_name, is_valid_skill_name
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def _one(home):
    records = discover_plugins(home)
    assert len(records) == 1, records
    return records[0]


def _names(path, folder: str, leaf: str) -> bool:
    p = os.fspath(path)
    return os.path.basename(p) == leaf and os.path.basename(os.path.dirname(p)) == folder


def _denied(path) -> PermissionError:
    return PermissionError(errno.EACCES, "Permission denied", os.fspath(path))


def _refuse_open(monkeypatch, module, folder: str, leaf: str) -> None:
    """Make *module*'s ``open`` fail on ``<folder>/<leaf>`` as a mode-000 file does.

    Patched where the file is read rather than made with chmod, because root and
    Windows read a mode-000 file anyway and the test would pass there for nothing.
    """

    def refusing(file, *args, **kwargs):
        if _names(file, folder, leaf):
            raise _denied(file)
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(module, "open", refusing, raising=False)


def _refuse_os(monkeypatch, call: str, folder: str, leaf: str) -> None:
    """Make ``os.<call>`` fail on ``<folder>/<leaf>``, as it does in a mode-000 folder."""
    real = getattr(os, call)

    def refusing(path=".", *args, **kwargs):
        if _names(path, folder, leaf):
            raise _denied(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, call, refusing)


class TestDiscovery:
    def test_no_index_means_no_plugins(self, tmp_path):
        assert discover_plugins(tmp_path) == []

    def test_only_listed_folders_are_installed(self, tmp_path):
        install(tmp_path, "listed")
        (tmp_path / "installed" / "stray").mkdir()
        assert [r.name for r in discover_plugins(tmp_path)] == ["listed"]

    def test_index_names_that_leave_installed_are_dropped(self, tmp_path):
        install(tmp_path, "ok")
        data = json.loads((tmp_path / "installed.json").read_text())
        data["installedPowers"] += [{"name": "../x"}, {"name": ".."}, {"name": "a/b"}, {}, "s"]
        # A colon is a Windows drive (``D:x`` joins to a path outside installed/)
        # or an NTFS stream, so it is dropped on every platform.
        data["installedPowers"] += [{"name": "D:x"}, {"name": "C:"}, {"name": "x:stream"}]
        (tmp_path / "installed.json").write_text(json.dumps(data))
        assert [r.name for r in discover_plugins(tmp_path)] == ["ok"]

    def test_a_repeated_name_is_listed_once_in_first_seen_order(self, tmp_path):
        names = ["b", "a", "b", "c", "a"]
        (tmp_path / "installed.json").write_text(
            json.dumps({"installedPowers": [{"name": n} for n in names]})
        )
        assert discovery.installed_names(tmp_path) == ["b", "a", "c"]

    def test_listed_but_missing_folder_is_invalid(self, tmp_path):
        (tmp_path / "installed.json").write_text(
            json.dumps({"installedPowers": [{"name": "gone"}]})
        )
        rec = _one(tmp_path)
        assert rec.state == "invalid" and "missing" in rec.reason

    def test_legacy_power_is_not_a_plugin(self, tmp_path):
        install(tmp_path, "old", manifest="", **{"POWER.md": "---\nname: old\n---\n"})
        rec = _one(tmp_path)
        assert rec.state == "not-a-plugin" and "POWER.md" in rec.reason

    def test_valid_plugin_is_off(self, tmp_path):
        install(tmp_path, "p", **{"skills__s__SKILL.md": skill("s")})
        rec = _one(tmp_path)
        assert rec.state == "off" and rec.reason is None
        assert [s.name for s in rec.skills] == ["s"]

    def test_discovery_writes_nothing(self, tmp_path):
        install(
            tmp_path,
            "p",
            **{
                "mcp.json": mcp(
                    {"d": {"type": "stdio", "command": "node", "cwd": "${PLUGIN_DATA}"}}
                )
            },
        )
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        discover_plugins(tmp_path)
        assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before

    def test_powers_home_honours_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIRO_POWERS_HOME", str(tmp_path / "elsewhere"))
        assert powers_home() == tmp_path / "elsewhere"


class TestManifest:
    def test_missing_schema_rejects_the_plugin(self, tmp_path):
        install(tmp_path, "p", manifest={"name": "p"}, **{"skills__s__SKILL.md": skill("s")})
        rec = _one(tmp_path)
        assert rec.state == "invalid" and rec.skills == () and rec.servers == ()

    def test_unknown_version_is_unsupported(self):
        with pytest.raises(ManifestError, match="unsupported"):
            parse_manifest(
                {
                    "$schema": "https://agent-plugins.org/schemas/9.0.0/plugin.schema.json",
                    "name": "p",
                }
            )

    @pytest.mark.parametrize(
        "name",
        [
            "My-Plugin",
            "-start",
            ".start",
            "end-",
            "end.",
            "has--double",
            "too.many..dots",
            "",
            "under_score",
            "caf\u00e9",
            "trailing-newline\n",
            "x" * 65,
        ],
    )
    def test_bad_names_reject(self, name):
        with pytest.raises(ManifestError):
            parse_manifest({"$schema": PLUGIN_SCHEMA, "name": name})

    @pytest.mark.parametrize("name", ["my-plugin", "acme.tools", "lint3r", "a", "9", "x" * 64])
    def test_good_names_pass(self, name):
        assert parse_manifest({"$schema": PLUGIN_SCHEMA, "name": name})[0].name == name

    def test_unknown_field_and_bad_extensions_are_reported_not_fatal(self):
        manifest, issues = parse_manifest(
            {"$schema": PLUGIN_SCHEMA, "name": "p", "hooks": "x", "extensions": False}
        )
        assert manifest.extensions == {}
        assert [i.message for i in issues] == [
            "unknown field ignored: hooks",
            "extensions is not an object, ignored",
        ]

    def test_extension_values_are_not_validated(self):
        manifest, issues = parse_manifest(
            {"$schema": PLUGIN_SCHEMA, "name": "p", "extensions": {"com.x": 5}}
        )
        assert manifest.extensions == {"com.x": 5} and issues == []

    def test_non_semver_version_is_accepted(self):
        assert parse_manifest({"$schema": PLUGIN_SCHEMA, "name": "p", "version": "next"})[0]

    @pytest.mark.parametrize(
        "extra",
        [
            {"author": {"name": "a", "phone": "1"}},
            {"author": "a"},
            {"keywords": "a"},
            {"version": 1},
        ],
    )
    def test_typed_field_violations_are_fatal(self, extra):
        with pytest.raises(ManifestError):
            parse_manifest({"$schema": PLUGIN_SCHEMA, "name": "p", **extra})

    def test_manifest_symlinked_outside_the_root_rejects(self, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"$schema": PLUGIN_SCHEMA, "name": "p"}))
        root = install(tmp_path, "p", manifest="")
        symlink(root / "plugin.json", outside)
        assert _one(tmp_path).state == "invalid"


class TestSkills:
    def test_only_immediate_children_count(self, tmp_path):
        install(
            tmp_path,
            "p",
            **{"skills__a__SKILL.md": skill("a"), "skills__group__b__SKILL.md": skill("b")},
        )
        assert [s.name for s in _one(tmp_path).skills] == ["a"]

    def test_bad_skill_is_skipped_and_others_load(self, tmp_path):
        install(
            tmp_path,
            "p",
            **{
                "skills__good__SKILL.md": skill("good"),
                "skills__bad__SKILL.md": "---\nname: bad\n- description: x\n---\n",
                "skills__wrong__SKILL.md": skill("other"),
                "skills__nodesc__SKILL.md": "---\nname: nodesc\n---\n",
            },
        )
        rec = _one(tmp_path)
        assert [s.name for s in rec.skills] == ["good"]
        assert sorted(i.entry for i in rec.issues if i.component == "skills") == [
            "bad",
            "nodesc",
            "wrong",
        ]

    def test_skills_file_instead_of_folder_disables_skills_only(self, tmp_path):
        install(
            tmp_path,
            "p",
            **{"skills": "not a dir", "mcp.json": mcp({"s": {"type": "stdio", "command": "node"}})},
        )
        rec = _one(tmp_path)
        assert rec.state == "off" and rec.skills == () and [s.name for s in rec.servers] == ["s"]

    def test_skill_linked_outside_the_root_is_skipped(self, tmp_path):
        outside = tmp_path / "elsewhere" / "s"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(skill("s"))
        root = install(tmp_path, "p")
        (root / "skills").mkdir()
        symlink(root / "skills" / "s", outside, directory=True)
        rec = _one(tmp_path)
        assert rec.skills == () and rec.issues[0].entry == "s"

    @pytest.mark.parametrize("name", ["a", "9", "a-b", "lint3r", "x" * 64])
    def test_good_skill_names_pass(self, name):
        assert is_valid_skill_name(name)

    @pytest.mark.parametrize(
        "name",
        ["", "A", "-a", "a-", "a--b", "a.b", "a_b", "caf\u00e9", "a\n", "x" * 65, None, 1],
    )
    def test_bad_skill_names_reject(self, name):
        assert not is_valid_skill_name(name)

    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("---\nname: a\n---\nBody.\n", "a"),
            ("---\r\nname: a\r\n---\r\nBody.\r\n", "a"),
            ("\ufeff---\nname: a\n---\n", "a"),
            ("---\nname: a\n---", "a"),
            ("--- \t\nname: a\n---\t \r\nBody.\n", "a"),
            ("---\nname: a\n---\nname: b\n---\n", "a"),
            ("---\nname: a\n", None),
            ("---\nname: a\n---\r", None),
            ("---\n---\nname: a\n", None),
            ("Body.\n---\nname: a\n---\n", None),
            ("----\nname: a\n---\n", None),
            ("---\nname: a\n---x\n", None),
            ("---\r\r\nname: a\n---\n", None),
        ],
        ids=[
            "lf",
            "crlf",
            "bom",
            "closing-fence-ends-the-text",
            "fences-with-trailing-blanks",
            "first-closing-fence-wins",
            "no-closing-fence",
            "closing-fence-ends-in-a-bare-cr",
            "no-line-between-the-fences",
            "fence-not-on-the-first-line",
            "four-dashes",
            "closing-fence-with-text",
            "opening-fence-with-two-crs",
        ],
    )
    def test_frontmatter_fences(self, text, name):
        assert frontmatter_name(text) == name

    def test_skill_linked_inside_the_root_loads(self, tmp_path):
        root = install(tmp_path, "p", **{"shared__s__SKILL.md": skill("s")})
        (root / "skills").mkdir()
        symlink(root / "skills" / "s", root / "shared" / "s", directory=True)
        rec = _one(tmp_path)
        assert [s.name for s in rec.skills] == ["s"]
        assert rec.skills[0].path == os.path.realpath(root / "shared" / "s")


# Nesting this deep exhausts the JSON and YAML parsers' stacks while staying far
# under every size cap.
_DEEP_JSON = "[" * 100_000
_DEEP_YAML = "a: " + "[" * 50_000


class TestParserFailuresStayContained:
    def test_bad_timestamp_frontmatter_skips_only_that_skill(self, tmp_path):
        install(
            tmp_path,
            "p",
            **{
                "skills__good__SKILL.md": skill("good"),
                "skills__dated__SKILL.md": (
                    "---\nname: dated\ndescription: x\ncreated: 2026-13-01\n---\n"
                ),
            },
        )
        rec = _one(tmp_path)
        assert rec.state == "off" and [s.name for s in rec.skills] == ["good"]
        assert [i.entry for i in rec.issues if i.component == "skills"] == ["dated"]

    def test_deeply_nested_frontmatter_skips_only_that_skill(self, tmp_path):
        install(
            tmp_path,
            "p",
            **{
                "skills__good__SKILL.md": skill("good"),
                "skills__deep__SKILL.md": f"---\n{_DEEP_YAML}\n---\n",
            },
        )
        rec = _one(tmp_path)
        assert rec.state == "off" and [s.name for s in rec.skills] == ["good"]
        assert [i.entry for i in rec.issues if i.component == "skills"] == ["deep"]

    def test_deeply_nested_manifest_rejects_only_that_plugin(self, tmp_path):
        install(tmp_path, "deep", manifest=_DEEP_JSON)
        install(tmp_path, "ok", **{"skills__s__SKILL.md": skill("s")})
        deep, ok = discover_plugins(tmp_path)
        assert deep.name == "deep" and deep.state == "invalid"
        assert deep.reason == "plugin.json is not valid JSON"
        assert ok.state == "off" and [s.name for s in ok.skills] == ["s"]

    def test_deeply_nested_mcp_json_turns_only_mcp_off(self, tmp_path):
        install(tmp_path, "p", **{"mcp.json": _DEEP_JSON, "skills__s__SKILL.md": skill("s")})
        rec = _one(tmp_path)
        assert rec.state == "off" and rec.servers == ()
        assert [s.name for s in rec.skills] == ["s"]
        assert [i.message for i in rec.issues if i.component == "mcp"] == [
            "mcp.json is not valid JSON; MCP is off"
        ]

    def test_deeply_nested_index_lists_nothing(self, tmp_path):
        install(tmp_path, "p")
        (tmp_path / "installed.json").write_text(_DEEP_JSON, encoding="utf-8")
        assert discover_plugins(tmp_path) == []

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFO on this platform")
    def test_an_index_that_is_a_named_pipe_lists_nothing(self, tmp_path):
        os.mkfifo(tmp_path / "installed.json")
        # Returning AT ALL is the assertion: opening a pipe with no writer never
        # comes back, so a regression is a hung read. Bounded so it reports as a
        # failure instead of stalling the shard.
        done: list[list] = []
        worker = threading.Thread(
            target=lambda: done.append(discover_plugins(tmp_path)), daemon=True
        )
        worker.start()
        worker.join(timeout=FALLBACK_TIMEOUT_SECS)
        assert done == [[]]

    def test_unexpected_error_marks_only_that_plugin_invalid(self, tmp_path, monkeypatch):
        install(tmp_path, "a", **{"skills__s__SKILL.md": skill("s")})
        install(tmp_path, "boom", **{"skills__s__SKILL.md": skill("s")})
        install(tmp_path, "c", **{"skills__s__SKILL.md": skill("s")})
        real = discovery.discover_skills

        def flaky(root_real):
            if os.path.basename(root_real) == "boom":
                raise LookupError("secret file contents")
            return real(root_real)

        monkeypatch.setattr(discovery, "discover_skills", flaky)
        a, boom, c = discover_plugins(tmp_path)
        assert [a.state, c.state] == ["off", "off"]
        assert [s.name for s in a.skills] == [s.name for s in c.skills] == ["s"]
        assert boom.state == "invalid" and boom.skills == () and boom.servers == ()
        assert boom.reason == "it could not be inspected: LookupError"
        assert boom.failed and boom.issues == (Issue("plugin", boom.reason, failed=True),)
        assert not a.failed and not c.failed
        assert "secret" not in repr(boom)

    def test_unreadable_plugin_folder_is_invalid(self, tmp_path, monkeypatch):
        install(tmp_path, "locked")
        install(tmp_path, "ok")
        real = Path.is_dir

        def is_dir(self, *args, **kwargs):
            if self.name == "locked":
                raise PermissionError(13, "denied")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_dir", is_dir)
        locked, ok = discover_plugins(tmp_path)
        assert locked.state == "invalid"
        assert locked.reason == "its folder could not be read: PermissionError"
        assert locked.failed
        assert ok.state == "off" and not ok.failed


class TestReadFailuresAreMarked:
    """A read that failed on this host is marked ``failed``; a rule the files break is not.

    Each failure sits next to its validation twin: the two differ in ``failed``,
    so the dashboard tells them apart without reading the English.
    """

    def test_an_unreadable_manifest_is_a_failed_read(self, tmp_path, monkeypatch):
        install(tmp_path, "locked")
        install(tmp_path, "broken", manifest="{")
        _refuse_open(monkeypatch, manifest_module, "locked", "plugin.json")
        broken, locked = discover_plugins(tmp_path)
        assert (locked.state, locked.failed) == ("invalid", True)
        assert locked.reason == "plugin.json could not be read: PermissionError"
        assert locked.issues == (Issue("plugin", locked.reason, failed=True),)
        assert (broken.state, broken.failed) == ("invalid", False)
        assert broken.issues == (Issue("plugin", "plugin.json is not valid JSON"),)

    def test_a_manifest_that_is_not_utf8_breaks_a_rule(self, tmp_path):
        root = install(tmp_path, "p", manifest="")
        (root / "plugin.json").write_bytes(b'{"name": "\xff"}')
        rec = _one(tmp_path)
        assert (rec.state, rec.failed) == ("invalid", False)
        assert rec.reason == "plugin.json is not UTF-8 text"

    def test_an_unreadable_plugin_folder_is_not_called_a_folder_without_plugin_json(
        self, tmp_path, monkeypatch
    ):
        install(tmp_path, "locked", **{"skills__s__SKILL.md": skill("s")})
        install(tmp_path, "bare", manifest="")
        _refuse_os(monkeypatch, "lstat", "locked", "plugin.json")
        bare, locked = discover_plugins(tmp_path)
        assert (locked.state, locked.failed) == ("invalid", True)
        assert locked.reason == "its folder could not be read: PermissionError"
        assert (bare.state, bare.reason, bare.failed) == ("not-a-plugin", "no plugin.json", False)

    def test_an_unreadable_skill_md_is_a_failed_read(self, tmp_path, monkeypatch):
        install(
            tmp_path,
            "p",
            **{
                "skills__alpha__SKILL.md": skill("alpha"),
                "skills__beta__SKILL.md": "no frontmatter",
                "skills__gamma__SKILL.md": skill("gamma"),
            },
        )
        _refuse_open(monkeypatch, skills_module, "alpha", "SKILL.md")
        rec = _one(tmp_path)
        assert [s.name for s in rec.skills] == ["gamma"]
        assert rec.issues == (
            Issue("skills", "skill skipped: SKILL.md could not be read", "alpha", failed=True),
            Issue("skills", "skill skipped: SKILL.md has no frontmatter", "beta"),
        )

    def test_a_skill_md_that_is_not_utf8_breaks_a_rule(self, tmp_path):
        root = install(tmp_path, "p", **{"skills__s__SKILL.md": ""})
        (root / "skills" / "s" / "SKILL.md").write_bytes(b"---\nname: s\xff\n---\n")
        assert _one(tmp_path).issues == (
            Issue("skills", "skill skipped: SKILL.md is not UTF-8 text", "s"),
        )

    def test_an_unreadable_skill_folder_is_reported_not_dropped(self, tmp_path, monkeypatch):
        install(
            tmp_path,
            "p",
            **{"skills__a__SKILL.md": skill("a"), "skills__b__SKILL.md": skill("b")},
        )
        (tmp_path / "installed" / "p" / "skills" / "notes.txt").write_text("not a skill")
        (tmp_path / "installed" / "p" / "skills" / "empty").mkdir()
        _refuse_os(monkeypatch, "lstat", "b", "SKILL.md")
        rec = _one(tmp_path)
        assert [s.name for s in rec.skills] == ["a"]
        assert rec.issues == (
            Issue("skills", "skill skipped: its folder could not be read", "b", failed=True),
        )

    def test_an_unlistable_skills_folder_is_a_failed_read(self, tmp_path, monkeypatch):
        install(tmp_path, "locked", **{"skills__s__SKILL.md": skill("s")})
        install(tmp_path, "file", **{"skills": "not a folder"})
        _refuse_os(monkeypatch, "listdir", "locked", "skills")
        file, locked = discover_plugins(tmp_path)
        assert locked.issues == (
            Issue("skills", "skills/ could not be listed; skills are off", failed=True),
        )
        assert file.issues == (Issue("skills", "skills/ is not a folder; skills are off"),)
        assert not locked.failed and not file.failed

    def test_an_unreadable_index_is_reported_and_lists_nothing(self, tmp_path, monkeypatch):
        install(tmp_path, "p")
        real = Path.read_text

        def read_text(self, *args, **kwargs):
            if self.name == "installed.json":
                raise _denied(self)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        found = discovery.discover(tmp_path)
        assert found.plugins == ()
        assert found.index_error == "installed.json could not be read: PermissionError"
        assert discovery.installed_names(tmp_path) == []

    @pytest.mark.parametrize(
        "text",
        ["{", _DEEP_JSON, '{"installedPowers": 1}'],
        ids=["bad-json", "deep-json", "not-a-list"],
    )
    def test_a_malformed_index_lists_nothing_and_is_no_read_failure(self, tmp_path, text):
        (tmp_path / "installed.json").write_text(text, encoding="utf-8")
        assert discovery.discover(tmp_path) == Discovery()

    def test_an_index_that_is_not_utf8_is_no_read_failure(self, tmp_path):
        (tmp_path / "installed.json").write_bytes(b'{"installedPowers": [{"name": "\xff"}]}')
        assert discovery.discover(tmp_path) == Discovery()

    def test_a_missing_index_is_no_read_failure(self, tmp_path):
        assert discovery.discover(tmp_path / "absent") == Discovery()
