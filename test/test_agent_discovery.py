"""Tests for agent discovery in ``agent_discovery.py``.

Focus on the robustness/security guards around scanning ``~/.kiro/agents/*.json``:
- macOS AppleDouble (``._*.json``) and non-UTF-8 files must not crash the scan.
- A ``*.json`` symlink pointing at a sensitive credential file must NOT be read.

Tests use a tmp_path fake $HOME so the real filesystem is never touched.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from kiro_crew.agent_discovery import AgentInfo, clear_list_agents_cache, list_agents


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _agents_dir(home: Path) -> Path:
    d = home / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


class TestListAgentsRobustness:
    def test_survives_non_utf8_and_appledouble(self, fake_home):
        """A non-UTF-8 file (AppleDouble ``._*.json`` sidecar or arbitrary
        binary ``*.json``) must be skipped, not raise UnicodeDecodeError."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        # AppleDouble sidecar: starts with "._" and is non-UTF-8 binary.
        (d / "._good.json").write_bytes(b"\x02\x00\x00\x00\xa3\x80\x81 not utf-8")
        # Arbitrary non-UTF-8 *.json that is not an AppleDouble name either.
        (d / "binary.json").write_bytes(b"\xff\xfe\x00\x01\xa3")

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    def test_skips_non_dict_json(self, fake_home):
        """Valid JSON that is not an object (e.g. a top-level array) must be
        skipped, not raise AttributeError on data.get()."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        (d / "array.json").write_text(json.dumps([1, 2, 3]))
        (d / "scalar.json").write_text(json.dumps("just a string"))

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    def test_skips_symlink_to_sensitive_file(self, fake_home):
        """A ``*.json`` symlink under ~/.kiro/agents/ that resolves to a
        sensitive credential path must NOT be read or returned."""
        d = _agents_dir(fake_home)
        (d / "real.json").write_text(json.dumps({"name": "real"}))

        # Plant a credential file under the sensitive ~/.aws dir and symlink
        # it in as a fake agent config. Even though it is valid JSON that
        # would parse, the sensitive-path guard must skip it.
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"name": "evil"}))
        (d / "evil.json").symlink_to(creds)

        names = [a.name for a in list_agents(agents_dir=d)]
        assert "evil" not in names
        assert names == ["real"]

    def test_skips_non_dict_mcp_servers(self, tmp_path: Path) -> None:
        """list_agents must not crash when mcpServers is a list instead of a dict.

        AttributeError: 'list' object has no attribute 'keys' previously escaped
        the except clause, aborting the entire loop and dropping all sibling agents.
        """
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.json").write_text(
            json.dumps({"name": "bad", "model": "auto", "mcpServers": ["a", "b"]}),
            encoding="utf-8",
        )
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "good", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        names = {a.name for a in agents}
        assert "good" in names, "well-formed sibling agent must survive a bad mcpServers value"


class TestSpecModelCoercion:
    """``AgentInfo.model`` is declared ``str`` and must always BE one.

    ``~/.kiro/agents`` is shared with other tools whose specs spell ``model``
    differently. A non-string reached the dashboard via ``to_dict()`` ->
    ``/api/agents/installed`` and, rendered as a React child, threw error #31 —
    taking the whole Agent Templates tab (and every other agent's row) down.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            # ACP-style structured reference, observed in the wild. This exact
            # shape produced "object with keys {id}" in the React #31 message.
            {"id": "anthropic:claude-opus-4-8"},
            None,  # key present but null
            ["claude-opus-4-8"],
            42,
        ],
        ids=["dict-id", "null", "list", "int"],
    )
    def test_non_string_model_degrades_to_auto(self, tmp_path: Path, raw: object) -> None:
        d = tmp_path / "agents"
        d.mkdir()
        (d / "foreign.json").write_text(
            json.dumps({"name": "foreign", "model": raw}), encoding="utf-8"
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.model == "auto"
        # to_dict() is what the API serialises — the guarantee has to hold there,
        # since that is the value the dashboard renders.
        assert isinstance(agent.to_dict()["model"], str)

    def test_string_model_is_preserved(self, tmp_path: Path) -> None:
        """The coercion must not flatten a legitimately pinned model."""
        d = tmp_path / "agents"
        d.mkdir()
        (d / "pinned.json").write_text(
            json.dumps({"name": "pinned", "model": "claude-opus-4-6"}), encoding="utf-8"
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.model == "claude-opus-4-6"

    def test_non_string_description_degrades_to_empty(self, tmp_path: Path) -> None:
        """``model`` is not the only rendered field, so it is not the only one guarded.

        The detail panel renders ``description`` as a JSX child too, and an object
        is truthy — so a foreign spec with a structured ``description`` blanks the
        whole tab exactly like a structured ``model`` does. Coercing per FIELD is
        what closes the class rather than the one observed instance.
        """
        d = tmp_path / "agents"
        d.mkdir()
        (d / "foreign.json").write_text(
            json.dumps({"name": "foreign", "description": {"text": "hi"}, "model": "auto"}),
            encoding="utf-8",
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.description == ""
        assert isinstance(agent.to_dict()["description"], str)

    def test_string_description_is_preserved(self, tmp_path: Path) -> None:
        d = tmp_path / "agents"
        d.mkdir()
        (d / "ok.json").write_text(
            json.dumps({"name": "ok", "description": "a real one", "model": "auto"}),
            encoding="utf-8",
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.description == "a real one"

    def test_bad_model_does_not_drop_sibling_agents(self, tmp_path: Path) -> None:
        """A foreign spec must cost only its own row, never the whole listing."""
        d = tmp_path / "agents"
        d.mkdir()
        (d / "foreign.json").write_text(
            json.dumps({"name": "foreign", "model": {"id": "anthropic:claude-opus-4-8"}}),
            encoding="utf-8",
        )
        (d / "good.json").write_text(
            json.dumps({"name": "good", "model": "auto"}), encoding="utf-8"
        )
        clear_list_agents_cache()
        names = {a.name for a in list_agents(agents_dir=d)}
        assert names == {"foreign", "good"}

    def test_edition_supplied_row_is_coerced(self, tmp_path: Path, monkeypatch) -> None:
        """The edition seam is a SECOND ``AgentInfo`` construction site.

        Rows arrive from out-of-tree code, so ``AgentInfo.model: str`` has to be
        enforced there too — coercing only the on-disk path would leave the same
        crash reachable through an edition build.
        """
        d = tmp_path / "agents"
        d.mkdir()
        # Stub the seam at ``safe_context_call``: it is what ``_with_edition_agents``
        # funnels the platform lookup through, so this needs no platform context.
        monkeypatch.setattr(
            "kiro_crew.platform.context.safe_context_call",
            lambda *_a, **_kw: [
                {"name": "edition-foreign", "model": {"id": "anthropic:claude-opus-4-8"}}
            ],
        )
        clear_list_agents_cache()
        by_name = {a.name: a for a in list_agents(agents_dir=d)}
        assert by_name["edition-foreign"].model == "auto"

    def test_every_str_field_is_coerced_at_construction(self) -> None:
        """The invariant is on the CONSTRUCTOR, not on any one caller.

        `model` and `description` were the two fields observed failing, but
        `name`, `package`, `source` and `filename` are rendered bare too
        (`{a.name}`, `{a.package}`, `<SourceBadge source={a.source}>`,
        `a.filename.startsWith(...)`), so a per-field fix at one call site only
        looks complete. Constructing directly — as the out-of-tree edition seam
        does — must still yield the declared types.
        """
        info = AgentInfo(
            name={"id": "x"},  # type: ignore[arg-type]
            filename=None,  # type: ignore[arg-type]
            description=["a"],  # type: ignore[arg-type]
            model={"id": "anthropic:claude-opus-4-8"},  # type: ignore[arg-type]
            source=7,  # type: ignore[arg-type]
            package={"n": 1},  # type: ignore[arg-type]
        )
        assert info.name == ""
        assert info.filename == ""
        assert info.description == ""
        assert info.model == "auto"
        assert info.source == "builtin"
        assert info.package == ""
        # to_dict() is the wire shape the dashboard renders.
        assert all(isinstance(v, str) for k, v in info.to_dict().items() if k not in
                   ("skills", "mcp_servers"))

    def test_list_fields_drop_only_the_unusable_elements(self) -> None:
        """`skills` / `mcp_servers` are rendered as chips, one element each.

        A bad entry costs itself, not the whole list — dropping the list would
        hide real skills the agent does have.
        """
        info = AgentInfo(
            name="a",
            filename="a.json",
            description="",
            model="auto",
            skills=["good", {"bad": 1}, "also-good"],  # type: ignore[list-item]
            mcp_servers=[None, "srv"],  # type: ignore[list-item]
        )
        assert info.skills == ["good", "also-good"]
        assert info.mcp_servers == ["srv"]

    def test_non_string_name_falls_back_to_filename_stem(self, tmp_path: Path) -> None:
        """A structured `name` must degrade the row, not silently DROP it.

        The package-detection branch does `stem.endswith(agent_name)`, which
        raised TypeError on a non-string name; the loop's broad `except` then
        swallowed it and the agent vanished from the listing entirely.
        """
        d = tmp_path / "agents"
        d.mkdir()
        (d / "weird.json").write_text(
            json.dumps({"name": {"id": "nope"}, "model": "auto"}), encoding="utf-8"
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.name == "weird"

    def test_edition_row_with_unusable_name_is_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """Unlike cosmetic fields, an unusable NAME is not degraded.

        The name is the dedup key, the React list key, and the argument every
        mutation is addressed by, so a blank-named row would be unselectable and
        would collide with any other nameless row.
        """
        d = tmp_path / "agents"
        d.mkdir()
        monkeypatch.setattr(
            "kiro_crew.platform.context.safe_context_call",
            lambda *_a, **_kw: [
                {"name": {"id": "nope"}, "model": "auto"},
                {"name": "usable", "model": "auto"},
            ],
        )
        clear_list_agents_cache()
        names = {a.name for a in list_agents(agents_dir=d)}
        assert names == {"usable"}


class TestListAgentsGlobalGuards:
    """Global agent loader edge cases."""

    def test_global_broken_symlink_skipped(self, tmp_path: Path) -> None:
        """list_agents skips broken symlinks in the global dir."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        broken = agents_dir / "broken.json"
        broken.symlink_to(tmp_path / "nonexistent.json")
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)
        assert not any(a.name == "broken" for a in agents)

    def test_global_bad_json_skipped(self, tmp_path: Path) -> None:
        """list_agents skips malformed JSON in the global dir."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.json").write_text("not json {{{", encoding="utf-8")
        (agents_dir / "ok.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)


class TestListAgentsDedup:
    """Deduplication and AIM package-name extraction edge cases."""

    def test_aim_package_name_extracted(self, tmp_path: Path) -> None:
        """AIM filename pattern extracts package name."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # AIM filename pattern: {package}-{agent_name}.json
        (agents_dir / "MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myagent"), None)
        assert a is not None
        assert a.package == "MyPkg"
        assert a.source == "package"

    def test_aim_kirocrew_package_source(self, tmp_path: Path) -> None:
        """A package-installed agent (e.g. KiroCrewAICapabilities) gets source='package'."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "KiroCrewAICapabilities-myskill.json").write_text(
            json.dumps({"name": "myskill", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myskill"), None)
        assert a is not None
        assert a.source == "package"

    def test_aim_package_preferred_over_builtin(self, tmp_path: Path) -> None:
        """AIM-packaged agent replaces same-name builtin in dedup."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # "dev.json" is builtin (stem == name). "zzz-MyPkg-dev.json" is AIM-packaged.
        # sorted() puts "dev.json" first, so builtin is seen first, then AIM replaces it.
        (agents_dir / "dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        (agents_dir / "zzz-MyPkg-dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        dev_agents = [a for a in agents if a.name == "dev"]
        assert len(dev_agents) == 1
        assert dev_agents[0].package == "zzz-MyPkg"

    def test_local_prefix_stripped_from_aim_package(self, tmp_path: Path) -> None:
        """AIM filename with 'local-' prefix has it stripped from package name."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "local-MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myagent"), None)
        assert a is not None
        assert a.package == "MyPkg"

    def test_local_twin_of_same_package_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 'local-' twin of the same package dedupes silently (no WARNING).

        Package managers publish a locally-built package as BOTH
        ``{package}-{name}.json`` and ``local-{package}-{name}.json``. Since the
        ``local-`` prefix is stripped from the package name, the twins collide on
        the same (name, package) — an expected layout, not an anomaly. This
        previously logged a self-contradictory "from packages 'X' and 'X'"
        WARNING per agent per scan.
        """
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        (agents_dir / "local-MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.agent_discovery"):
            agents = list_agents(agents_dir=agents_dir)
        dupes = [a for a in agents if a.name == "myagent"]
        assert len(dupes) == 1
        # First-seen wins, and which twin enumerates first is platform-
        # dependent (WindowsPath sorts case-insensitively, so "local-..."
        # can precede "MyPkg-..."). The fix deliberately leaves selection
        # untouched — assert only that exactly one twin survives.
        assert dupes[0].filename in ("MyPkg-myagent.json", "local-MyPkg-myagent.json")
        assert not [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ], "same-package local twin must not produce a WARNING"
        # The twin is still visible at debug for diagnosis.
        assert any("same-package twin" in r.getMessage() for r in caplog.records)

    def test_cross_package_duplicate_still_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A genuine name collision between two DIFFERENT packages still warns."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "AaaPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        (agents_dir / "BbbPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_discovery"):
            agents = list_agents(agents_dir=agents_dir)
        dupes = [a for a in agents if a.name == "myagent"]
        assert len(dupes) == 1
        assert dupes[0].package == "AaaPkg"  # first-seen wins
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Duplicate agent name" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "AaaPkg" in warnings[0].getMessage()
        assert "BbbPkg" in warnings[0].getMessage()


class TestListAgentsCache:
    """list_agents caches parsed results per directory and reuses them while the
    stat-only directory signature is unchanged."""

    def test_cache_hit_skips_reparse(self, tmp_path: Path) -> None:
        """An unchanged signature returns the cached result without re-parsing."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()

        first = [a.name for a in list_agents(agents_dir=d)]
        assert first == ["v1"]

        # Rewrite the content but restore the original mtime so the signature is
        # unchanged: a re-parse would yield "v2"; a cache hit yields "v1".
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))

        second = [a.name for a in list_agents(agents_dir=d)]
        assert second == ["v1"], "unchanged signature must return the cached result"

    def test_cache_invalidates_on_add(self, tmp_path: Path) -> None:
        """Adding a file changes the signature and is reflected immediately."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        (d / "a.json").write_text(
            json.dumps({"name": "a", "model": "auto"}), encoding="utf-8"
        )
        assert {a.name for a in list_agents(agents_dir=d)} == {"a"}

        (d / "b.json").write_text(
            json.dumps({"name": "b", "model": "auto"}), encoding="utf-8"
        )
        assert {a.name for a in list_agents(agents_dir=d)} == {"a", "b"}

    def test_cache_invalidates_on_remove(self, tmp_path: Path) -> None:
        """Removing a file changes the signature and is reflected immediately."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        (d / "a.json").write_text(
            json.dumps({"name": "a", "model": "auto"}), encoding="utf-8"
        )
        (d / "b.json").write_text(
            json.dumps({"name": "b", "model": "auto"}), encoding="utf-8"
        )
        assert {a.name for a in list_agents(agents_dir=d)} == {"a", "b"}

        (d / "b.json").unlink()
        assert {a.name for a in list_agents(agents_dir=d)} == {"a"}

    def test_cache_invalidates_on_inplace_edit(self, tmp_path: Path) -> None:
        """An in-place content edit (newer mtime) invalidates the cache."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        assert [a.name for a in list_agents(agents_dir=d)] == ["v1"]

        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        # Bump mtime forward deterministically so the signature is guaranteed newer.
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert [a.name for a in list_agents(agents_dir=d)] == ["v2"], (
            "an in-place edit must invalidate the cache"
        )

    def test_clear_cache_forces_rescan(self, tmp_path: Path) -> None:
        """clear_list_agents_cache() forces a fresh scan even when the signature
        is unchanged."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()
        assert [a.name for a in list_agents(agents_dir=d)] == ["v1"]

        # Change content but freeze the mtime so the signature would still hit ...
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
        # ... then force a clear: the next call must re-scan and see "v2".
        clear_list_agents_cache()
        assert [a.name for a in list_agents(agents_dir=d)] == ["v2"]
