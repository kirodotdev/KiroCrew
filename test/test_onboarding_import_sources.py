"""The onboarding-import source registration seam.

The engine ships the foreign agents any user may plausibly have installed; an
edition registers its own (typically an agent it supersedes) through
``ImportSourceProvider``. These tests pin the two properties that make that seam
worth having: a registered source is indistinguishable from a builtin everywhere
the engine validates, scans, and reports — and a malformed one cannot degrade the
builtins.
"""

from __future__ import annotations

import dataclasses
import json
import logging

import pytest

from kiro_crew import mcp_cleanup, onboarding_import
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.interfaces import ImportSource


def _provider_of(*sources: ImportSource):
    """A provider object returning *sources*, for a test that installs its own."""

    class _Provider:
        def import_sources(self) -> list[ImportSource]:
            return list(sources)

    return _Provider()


def _install(*sources: ImportSource) -> None:
    """Compose a context whose edition contributes *sources*."""

    class _Provider:
        def import_sources(self) -> list[ImportSource]:
            return list(sources)

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, import_sources=_Provider()))


@pytest.fixture(autouse=True)
def _clean_context():
    yield
    reset_context()


def _lineage_source(**overrides) -> ImportSource:
    fields: dict = {
        "id": "predecessor",
        "display_name": "Predecessor",
        "env_vars": ("PREDECESSOR_HOME",),
        "home_dir": ".predecessor",
    }
    fields.update(overrides)
    return ImportSource(**fields)


class TestDefaultEdition:
    def test_public_edition_offers_only_the_builtins(self):
        assert tuple(onboarding_import._sources()) == (
            "codex",
            "claude_code",
            "gemini",
            "openclaw",
            "hermes",
        )

    def test_category_catalog_is_pinned(self):
        """A category silently vanishing would import less and report success."""
        assert onboarding_import.CATEGORY_IDS == (
            "instructions",
            "memories",
            "workspaces",
            "mcp_servers",
            "skills",
            "schedules",
            "settings",
        )

    def test_public_edition_declares_nothing_superseded(self):
        """Nothing is reclaimed from the user's global config by default."""
        assert onboarding_import.predecessor_mcp_names() == frozenset()
        assert onboarding_import.stale_mcp_binaries() == frozenset()

    @pytest.mark.parametrize(
        ("source_id", "env_var", "expected_dir"),
        [
            ("codex", "CODEX_HOME", ".codex"),
            ("claude_code", "CLAUDE_CONFIG_DIR", ".claude"),
            ("claude_code", "CLAUDE_HOME", ".claude"),
            ("gemini", "GEMINI_HOME", ".gemini"),
            ("gemini", "ANTIGRAVITY_HOME", ".gemini"),
            ("hermes", "HERMES_HOME", ".hermes"),
            ("hermes", "HERMES_AGENT_HOME", ".hermes"),
            ("hermes", "HERMES_CONFIG_DIR", ".hermes"),
        ],
    )
    def test_each_builtin_root_honors_its_env_override(
        self, tmp_path, source_id, env_var, expected_dir
    ):
        """Every builtin's declared env override still resolves after the move
        from a module-level root table to per-descriptor fields."""
        _base, defaults = onboarding_import._source_roots(tmp_path, {})
        assert defaults[source_id] == tmp_path / expected_dir

        elsewhere = tmp_path / f"relocated-{env_var}"
        _base, overridden = onboarding_import._source_roots(tmp_path, {env_var: str(elsewhere)})
        assert overridden[source_id] == elsewhere


class TestRegistration:
    def test_registered_source_joins_the_valid_id_set(self):
        _install(_lineage_source())
        assert "predecessor" in tuple(onboarding_import._sources())

    def test_registered_source_is_named_in_the_scan_report(self):
        """The display name a descriptor declares is what the preview reports."""
        _install(_lineage_source())
        assert onboarding_import._sources()["predecessor"].display_name == "Predecessor"

    def test_a_skipped_entry_for_an_unscanned_source_still_reports_a_name(self):
        """An unknown id has no projected name, and must not render as blank.

        The engine used to carry its own `_source_name` placeholder, but its only
        caller passed a name unconditionally — so the fallback was dead code kept
        alive by this test. The LIVE placeholder is the handler's, which projects
        a skipped entry whose source was never scanned, so the coverage moves here
        rather than being deleted with the dead path.
        """
        from kiro_crew.dashboard.handlers import onboarding_import as handler

        response = handler._scan_response(
            {
                "sources": [],
                "merge_only": True,
                "skipped": [{"source_id": "nope", "category_id": "", "reason": "unknown_source"}],
            }
        )

        assert response["skipped"][0]["source"] == "Unknown source"
        assert response["skipped"][0]["category"] == ""

    def test_root_resolves_from_the_declared_home_dir(self, tmp_path):
        _install(_lineage_source())
        _base, roots = onboarding_import._source_roots(tmp_path, {})
        assert roots["predecessor"] == tmp_path / ".predecessor"

    def test_root_resolves_from_the_declared_env_var(self, tmp_path):
        _install(_lineage_source())
        elsewhere = tmp_path / "relocated"
        _base, roots = onboarding_import._source_roots(
            tmp_path, {"PREDECESSOR_HOME": str(elsewhere)}
        )
        assert roots["predecessor"] == elsewhere

    def test_the_api_layer_accepts_a_registered_id(self):
        """The HTTP validator derives its id set from the registry.

        A second hardcoded copy in the handler is what previously let a source be
        known to the engine and rejected by the API.
        """
        from kiro_crew.dashboard.handlers import onboarding_import as handler

        _install(_lineage_source())
        assert "predecessor" in frozenset(handler._backend()._sources())


class TestScannerIsolation:
    def test_every_builtin_id_satisfies_the_registration_pattern(self):
        """The pattern registered sources must match cannot be stricter than the
        builtins it is modelled on, or the seam would reject its own examples."""
        for source_id in tuple(onboarding_import._sources()):
            assert onboarding_import._SOURCE_ID_RE.match(source_id), source_id

    def test_a_failing_reader_does_not_deny_the_other_sources(self, tmp_path, monkeypatch):
        """Discovery reads installs this product does not own. One unreadable
        source must cost that source, not the whole import page."""
        codex = tmp_path / ".codex"
        codex.mkdir()
        (codex / "AGENTS.md").write_text("codex rules\n", encoding="utf-8")
        predecessor = tmp_path / ".predecessor"
        predecessor.mkdir()
        (predecessor / "config.json").write_text("{}", encoding="utf-8")

        def _explode(_scan) -> None:
            raise RuntimeError("reader blew up")

        monkeypatch.setattr(onboarding_import, "_scan_lineage_install", _explode)
        _install(_lineage_source())
        preview = onboarding_import._preview(None, tmp_path, {})
        assert "codex" in {source["id"] for source in preview["sources"]}
        assert any(entry.get("reason") == "source_unreadable" for entry in preview["skipped"])

    def test_a_failing_reader_contributes_no_items(self, tmp_path):
        """A reader that died mid-way must not have partial findings imported.

        Whatever it added before dying is a partial read of a source we now know
        we cannot read correctly; offering half of it presents that partial state
        to the user as their data.
        """
        root = tmp_path / ".predecessor"
        root.mkdir()

        def _half_then_explode(scan) -> None:
            scan.add("memories", "k", {"text": "half-written"})
            raise RuntimeError("died after adding")

        # `_Source` is the engine's own normalized record, so a test may build one
        # directly to drive a reader the public descriptor can no longer supply.
        source = onboarding_import._Source(
            id="predecessor",
            display_name="Predecessor",
            scan=_half_then_explode,
            env_vars=(),
            home_dir=".predecessor",
            managed_mcp_names=frozenset(),
            superseded=False,
            stale_mcp_binaries=frozenset(),
        )
        scan = onboarding_import._scan_source("predecessor", root, tmp_path, source=source)
        assert any(entry.get("reason") == "source_unreadable" for entry in scan.skipped)
        assert not any(
            scan.items[category] for category in scan.items
        ), "partial findings from a failed reader were retained"

    def test_a_whole_source_failure_is_not_attributed_to_a_category(self, tmp_path, monkeypatch):
        """The failure is the SOURCE, not one of its categories.

        Filing it under `settings` rendered "Codex: Settings — scanner_failed"
        while the source itself disappeared from the picker, sending the user to
        look at a category that was never the problem.
        """

        def _explode(scan):
            raise RuntimeError("reader is broken")

        monkeypatch.setattr(onboarding_import, "_scan_lineage_install", _explode)
        _install(_lineage_source())
        (tmp_path / ".predecessor").mkdir()
        preview = onboarding_import._preview(None, tmp_path, {})
        failures = [e for e in preview["skipped"] if e.get("reason") == "source_unreadable"]
        assert failures, "the whole-source failure was not reported"
        assert failures[0]["category_id"] == "", "a source-level failure must carry no category"


class TestNormalization:
    """Everything questionable about a descriptor is settled at one boundary.

    Three review rounds each found a different consumer re-deriving a field the
    registry should have canonicalised, so the invariant under test is that
    `_normalize_source` is the only place that decides.
    """

    def test_an_env_only_source_with_no_variable_set_is_not_scanned(self, tmp_path):
        """`base_home / ""` is the user's ENTIRE home — scanning it would walk
        every file they own, so an unresolvable source stays unresolved."""
        _install(_lineage_source(home_dir=""))
        _base, roots = onboarding_import._source_roots(tmp_path, {})
        assert "predecessor" not in roots

    def test_an_env_only_source_resolves_when_its_variable_is_set(self, tmp_path):
        _install(_lineage_source(home_dir=""))
        target = tmp_path / "elsewhere"
        _base, roots = onboarding_import._source_roots(tmp_path, {"PREDECESSOR_HOME": str(target)})
        assert roots["predecessor"] == target

    def test_the_preview_skips_a_source_with_no_resolvable_root(self, tmp_path):
        _install(_lineage_source(home_dir=""))
        preview = onboarding_import._preview(None, tmp_path, {})
        assert "predecessor" not in {source["id"] for source in preview["sources"]}

    def test_an_overlong_home_dir_is_refused_at_the_boundary(self, tmp_path, caplog):
        """A component past the filesystem limit makes every stat of that root
        raise ENAMETOOLONG instead of answering "absent", so it is refused here
        rather than left to crash discovery for every OTHER source."""
        _install(_lineage_source(home_dir="." + "x" * 300))
        with caplog.at_level(logging.WARNING):
            assert "predecessor" not in tuple(onboarding_import._sources())
        assert "home_dir exceeds" in caplog.text

    def test_an_overlong_env_root_does_not_crash_discovery(self, tmp_path):
        """The env var is the USER's, not a descriptor's, so it cannot be refused
        at registration. Discovery must survive it and simply not offer the
        source — a 500 here would deny the user every other agent as well.
        """
        _install(_lineage_source(home_dir=""))
        (tmp_path / ".codex").mkdir()
        overlong = str(tmp_path / ("y" * 300))
        preview = onboarding_import._preview(None, tmp_path, {"PREDECESSOR_HOME": overlong})
        offered = {source["id"] for source in preview["sources"]}
        assert "predecessor" not in offered
        assert "codex" in offered, "an unreadable root must not suppress the other sources"

    def test_the_existence_probe_answers_false_for_an_unstattable_root(self, tmp_path):
        """`_source_exists` is the gate both preview and apply call, so it is the
        one place that must never propagate an OSError."""
        assert onboarding_import._source_exists("codex", tmp_path / ("z" * 300)) is False

    def test_a_home_dir_with_an_embedded_nul_is_refused(self, tmp_path, caplog):
        """A NUL makes the syscall unreachable, so `lstat` raises ValueError —
        NOT OSError — and the link guard runs before every other probe."""
        _install(_lineage_source(home_dir="bad\x00name"))
        with caplog.at_level(logging.WARNING):
            assert "predecessor" not in tuple(onboarding_import._sources())

    def test_a_nul_bearing_root_does_not_crash_the_probe(self, tmp_path):
        """Belt to the boundary's braces: the probe itself must survive a NUL
        whatever produced it, since a raise here 500s the whole scan."""
        assert onboarding_import._source_exists("codex", tmp_path / "bad\x00name") is False

    @pytest.mark.parametrize(
        ("declared", "looked_up"),
        [("Predecessor-Core", "predecessor-core"), ("PRED-CRON", "pred-cron")],
    )
    def test_contributed_managed_names_are_casefolded(self, declared, looked_up):
        """One consumer casefolding its lookup and another not is how a
        contributed name silently stopped being excluded from import."""
        _install(_lineage_source(managed_mcp_names=(declared,)))
        assert looked_up in onboarding_import._managed_mcp_names()

    @pytest.mark.parametrize("name", ["python3.12", "node20", "NODE-22.1", "python3"])
    def test_a_versioned_shared_runtime_is_still_refused(self, name):
        """Comparing the raw string let a versioned spelling walk past the guard."""
        assert onboarding_import._runtime_stem(name) in onboarding_import._SHARED_RUNTIME_BINARIES

    @pytest.mark.parametrize("name", ["nodejs", "env", "busybox", "dash", "bunx", "pipx"])
    def test_an_aliased_shared_runtime_is_refused(self, name):
        """Version stripping cannot collapse a LETTER suffix.

        `nodejs` is Debian's and Ubuntu's name for `node`, `env` is how a shebang
        reaches an interpreter, and `busybox` IS the shell on minimal images — so
        each names someone else's runtime while looking nothing like `node`. A
        descriptor claiming one would have had first-run cleanup delete a
        user-owned MCP server whose command merely resolves to that binary.
        """
        assert onboarding_import._runtime_stem(name) in onboarding_import._SHARED_RUNTIME_BINARIES

    def test_a_descriptor_claiming_an_aliased_runtime_is_dropped(self, caplog):
        """End to end: the guard must actually refuse the descriptor, not just
        agree the name is a runtime."""
        _install(_lineage_source(stale_mcp_binaries=("nodejs",)))
        with caplog.at_level(logging.WARNING):
            assert "predecessor" not in tuple(onboarding_import._sources())
        assert "claims shared runtime" in caplog.text

    def test_a_genuine_launcher_is_not_mistaken_for_a_runtime(self):
        """The version strip must not turn an agent's own name into a runtime."""
        for name in ("predecessor", "meshy2", "claw3"):
            assert (
                onboarding_import._runtime_stem(name)
                not in onboarding_import._SHARED_RUNTIME_BINARIES
            )

    def test_a_reserved_id_is_never_registered(self, caplog):
        """`quick` is the first-run setup mode, not a source.

        Accepting it would register a source the dashboard is required to hide —
        one that imports nothing and gives no reason why.
        """
        _install(_lineage_source(id="quick"))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.onboarding_import"):
            ids = tuple(onboarding_import._sources())
        assert "reserved" in caplog.text
        assert "quick" not in ids

    def test_builtins_pass_through_the_same_boundary(self):
        """The builtins must not travel a laxer path than the rule they model.

        No per-id exemption: a builtin that cannot satisfy the normalizer is a
        builtin the normalizer would silently drop.
        """
        sources = onboarding_import._sources()
        assert set(sources) == {"codex", "claude_code", "gemini", "openclaw", "hermes"}
        for source in sources.values():
            assert isinstance(source, onboarding_import._Source)
            assert source.env_vars or source.home_dir, source.id


class TestMalformedContributions:
    """A bad descriptor is dropped; it never degrades the builtins."""

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"id": ""}, "no id"),
            ({"id": "codex"}, "id is already registered"),
            ({"id": "../../etc"}, "becomes a path segment"),
            ({"id": "a/b"}, "becomes a path segment"),
            ({"id": "Predecessor"}, "becomes a path segment"),
            ({"id": "_leading"}, "becomes a path segment"),
            ({"env_vars": (), "home_dir": ""}, "no root to scan"),
            ({"home_dir": "/etc"}, "must be a single directory name"),
            ({"home_dir": "../../etc"}, "must be a single directory name"),
            (
                {"stale_mcp_binaries": ("node",), "superseded": True},
                "claims shared runtime",
            ),
            (
                {"stale_mcp_binaries": ("Python3",), "superseded": True},
                "claims shared runtime",
            ),
            (
                {"stale_mcp_binaries": ("python3.12",), "superseded": True},
                "claims shared runtime",
            ),
            (
                {"stale_mcp_binaries": ("node20",), "superseded": True},
                "claims shared runtime",
            ),
            (
                {"stale_mcp_binaries": ("NODE-22.1",), "superseded": True},
                "claims shared runtime",
            ),
        ],
    )
    def test_dropped_with_a_warning(self, overrides, reason, caplog):
        _install(_lineage_source(**overrides))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.onboarding_import"):
            ids = tuple(onboarding_import._sources())
        assert reason in caplog.text
        # The builtins survive intact, and a shadowing id keeps its builtin.
        assert ("codex", "claude_code", "gemini", "openclaw", "hermes") == ids

    def test_a_shared_runtime_claim_cannot_reclaim_unrelated_servers(self, tmp_path, monkeypatch):
        """The guard is what stands between a careless descriptor and the user's
        own MCP servers: a launcher name is an agent's own, never its interpreter.
        """
        path = tmp_path / "mcp.json"
        path.write_text(
            json.dumps({"mcpServers": {"user-owned": {"command": "/usr/bin/node"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", path)
        _install(_lineage_source(stale_mcp_binaries=("node",), superseded=True))
        assert mcp_cleanup.clean_stale_managed_mcp() == []

    def test_a_builtin_id_is_never_shadowed(self):
        """A contribution reusing a builtin id must not change what it imports."""
        before = onboarding_import._sources()["codex"].scan
        _install(_lineage_source(id="codex"))
        assert onboarding_import._sources()["codex"].scan is before

    def test_a_registered_source_cannot_supply_reader_code(self):
        """The seam is what the engine reads WITH, not code it runs.

        A descriptor that carried a callable would hand out-of-tree code the
        engine's scan accumulator, and with it the ability to add content that
        never passed the credential, injection, sensitive-path and size gates
        living inside the engine's readers. A registered source therefore neither
        supplies a reader nor selects one — the engine picks.
        """
        assert not hasattr(ImportSource("x", "X"), "scan")
        assert not hasattr(ImportSource("x", "X"), "layout")
        _install(_lineage_source())
        assert onboarding_import._sources()["predecessor"].scan is (
            onboarding_import._scan_lineage_install
        )

    def test_a_failing_provider_degrades_to_the_builtins(self, caplog):
        """Fail-closed: a broken adapter costs the edition's sources, not the page."""

        class _Broken:
            def import_sources(self):
                raise RuntimeError("adapter exploded")

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Broken()))
        with caplog.at_level(logging.WARNING):
            assert tuple(onboarding_import._sources()) == (
                "codex",
                "claude_code",
                "gemini",
                "openclaw",
                "hermes",
            )


class TestManagedServerNames:
    def test_contributed_managed_names_are_never_imported(self):
        _install(_lineage_source(managed_mcp_names=("predecessor-core",)))
        assert "predecessor-core" in onboarding_import._managed_mcp_names()

    def test_managed_names_alone_do_not_make_an_agent_purgeable(self):
        """A live foreign agent's servers are skipped on import, never reclaimed.

        OpenClaw is the shipped instance of this: the user may still be running
        it, so purging its entries from their global provider config would delete
        servers that are in use.
        """
        assert "openclaw-core" in onboarding_import._managed_mcp_names()
        assert "openclaw-core" not in onboarding_import.predecessor_mcp_names()

    def test_superseded_agent_names_become_purgeable(self):
        _install(
            _lineage_source(
                managed_mcp_names=("predecessor-core",),
                stale_mcp_binaries=("predecessor",),
                superseded=True,
            )
        )
        assert "predecessor-core" in onboarding_import.predecessor_mcp_names()
        assert "predecessor" in onboarding_import.stale_mcp_binaries()

    def test_binaries_are_ignored_without_the_superseded_flag(self):
        _install(_lineage_source(stale_mcp_binaries=("predecessor",)))
        assert onboarding_import.stale_mcp_binaries() == frozenset()


class TestGlobalConfigCleanup:
    """``mcp_cleanup`` reclaims a superseded agent's leftovers, and only those."""

    def _write_mcp_json(self, tmp_path, servers, monkeypatch):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", path)
        return path

    def test_purges_a_superseded_agent_by_server_name(self, tmp_path, monkeypatch):
        self._write_mcp_json(
            tmp_path,
            {"predecessor-core": {"command": "anything"}},
            monkeypatch,
        )
        _install(_lineage_source(managed_mcp_names=("predecessor-core",), superseded=True))
        assert mcp_cleanup.clean_stale_managed_mcp() == ["predecessor-core"]

    def test_purges_a_superseded_agent_by_command_basename(self, tmp_path, monkeypatch):
        """Catches an entry whose NAME is the user's but whose command is dead."""
        self._write_mcp_json(
            tmp_path,
            {"playwright-mcp": {"command": "/opt/Predecessor/bin/predecessor"}},
            monkeypatch,
        )
        _install(_lineage_source(stale_mcp_binaries=("predecessor",), superseded=True))
        assert mcp_cleanup.clean_stale_managed_mcp() == ["playwright-mcp"]

    def test_windows_console_script_is_matched(self, tmp_path, monkeypatch):
        """mcp.json is cross-platform data, so both separators must be honored."""
        self._write_mcp_json(
            tmp_path,
            {"leftover": {"command": r"C:\\Predecessor\\Scripts\\predecessor.exe"}},
            monkeypatch,
        )
        _install(_lineage_source(stale_mcp_binaries=("predecessor",), superseded=True))
        assert mcp_cleanup.clean_stale_managed_mcp() == ["leftover"]

    @pytest.mark.parametrize(
        "command",
        ["predecessor.helper", "predecessor.cli.js", "predecessor.example.com"],
    )
    def test_an_unrelated_dotted_command_is_not_collapsed(self, tmp_path, monkeypatch, command):
        """Only a real executable suffix is stripped.

        Matching on the first dot-separated segment would delete any server whose
        command merely STARTS with a registered name, which is a different binary.
        """
        self._write_mcp_json(tmp_path, {"user-owned": {"command": command}}, monkeypatch)
        _install(_lineage_source(stale_mcp_binaries=("predecessor",), superseded=True))
        assert mcp_cleanup.clean_stale_managed_mcp() == []

    def test_leaves_a_live_foreign_agents_servers_alone(self, tmp_path, monkeypatch):
        self._write_mcp_json(tmp_path, {"openclaw-core": {"command": "openclaw"}}, monkeypatch)
        assert mcp_cleanup.clean_stale_managed_mcp() == []

    def test_leaves_a_user_owned_server_alone(self, tmp_path, monkeypatch):
        self._write_mcp_json(
            tmp_path,
            {"playwright-mcp": {"command": "npx", "args": ["@playwright/mcp"]}},
            monkeypatch,
        )
        _install(_lineage_source(stale_mcp_binaries=("predecessor",), superseded=True))
        assert mcp_cleanup.clean_stale_managed_mcp() == []

    def test_public_edition_purges_nothing_extra(self, tmp_path, monkeypatch):
        self._write_mcp_json(tmp_path, {"some-server": {"command": "whatever"}}, monkeypatch)
        assert mcp_cleanup.clean_stale_managed_mcp() == []


class TestLineageScanner:
    """``_scan_lineage_install`` reads this product's OWN layout.

    That is what makes it reusable: a predecessor, a rename, or a fork writes the
    same files in the same places, so a registered source supplies a root and
    needs no format knowledge.
    """

    def _lineage_home(self, tmp_path):
        root = tmp_path / ".predecessor"
        (root / "workspace" / "memory").mkdir(parents=True)
        (root / "workspace").joinpath("AGENTS.md").write_text("be careful\n", encoding="utf-8")
        (root / "config.json").write_text(json.dumps({"timezone": "UTC"}), encoding="utf-8")
        (root / "crons.json").write_text(
            json.dumps({"jobs": [{"name": "nightly", "schedule": "0 3 * * *", "message": "go"}]}),
            encoding="utf-8",
        )
        return root

    def test_reads_instructions_schedules_and_settings(self, tmp_path):
        root = self._lineage_home(tmp_path)
        scan = onboarding_import._Scan(source_id="predecessor", root=root, user_home=tmp_path)
        onboarding_import._scan_lineage_install(scan)
        assert scan.items["instructions"], "workspace AGENTS.md not read"
        assert scan.items["schedules"], "crons.json not read"
        assert scan.items["settings"], "config.json not read"

    def test_settings_are_attributed_to_the_scanning_source(self, tmp_path):
        """The scanner takes its id from the scan, never from a baked-in name."""
        root = self._lineage_home(tmp_path)
        scan = onboarding_import._Scan(source_id="predecessor", root=root, user_home=tmp_path)
        onboarding_import._scan_lineage_install(scan)
        assert all(item.source_id == "predecessor" for item in scan.items["settings"])

    def test_a_registered_lineage_source_is_detected_end_to_end(self, tmp_path):
        self._lineage_home(tmp_path)
        _install(_lineage_source())
        preview = onboarding_import._preview(["predecessor"], tmp_path, {})
        # The engine payload lists only sources it actually found; the per-source
        # ``detected`` flag is added later by the HTTP projection.
        assert preview["detected_count"] == 1
        assert [source["id"] for source in preview["sources"]] == ["predecessor"]
        found = {
            category["id"] for source in preview["sources"] for category in source["categories"]
        }
        assert {"instructions", "settings"} <= found


class TestRegistrySnapshotIsStable:
    """A preview validates ids, resolves roots and dispatches scanners — those
    must agree. Each read is fail-closed, so a degrading adapter between two of
    them previously left an accepted id with no resolved root and crashed."""

    def test_a_provider_that_degrades_mid_preview_does_not_crash(self, tmp_path):
        predecessor = tmp_path / ".predecessor"
        (predecessor / "workspace").mkdir(parents=True)
        (predecessor / "workspace" / "AGENTS.md").write_text("rules\n", encoding="utf-8")

        calls: list[int] = []

        class _Flaky:
            """Answers once, then fails — the shape of a transient adapter."""

            def import_sources(self) -> list[ImportSource]:
                calls.append(1)
                if len(calls) > 1:
                    raise RuntimeError("adapter went away")
                return [_lineage_source()]

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Flaky()))

        preview = onboarding_import._preview(None, tmp_path, {})
        assert preview["detected_count"] >= 0
        assert len(calls) == 1, "the preview must resolve the registry exactly once"


class TestRegistryCache:
    """A complete resolve is cached per context; a degraded one never is.

    This is what makes preview and apply agree. Caching the degraded read instead
    would be worse than not caching at all: a transient provider failure would
    become a permanent loss of that edition's sources for the whole process.
    """

    def test_a_provider_is_consulted_once_per_context(self):
        calls: list[int] = []

        class _Counting:
            def import_sources(self) -> list[ImportSource]:
                calls.append(1)
                return [_lineage_source()]

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Counting()))
        for _ in range(4):
            assert "predecessor" in tuple(onboarding_import._sources())
        assert len(calls) == 1

    def test_a_provider_that_fails_later_cannot_change_a_resolved_registry(self):
        """The apply-path defect: a source discovered at preview must still have a
        reader at apply, even if the provider has since gone away."""
        state = {"fail": False}

        class _Flaky:
            def import_sources(self) -> list[ImportSource]:
                if state["fail"]:
                    raise RuntimeError("provider went away")
                return [_lineage_source()]

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Flaky()))
        assert "predecessor" in tuple(onboarding_import._sources())
        state["fail"] = True
        assert "predecessor" in onboarding_import._sources()

    def test_a_degraded_first_read_is_retried_not_cached(self):
        """Otherwise one transient failure at boot would cost the edition its
        sources for the entire process."""
        state = {"fail": True}

        class _Flaky:
            def import_sources(self) -> list[ImportSource]:
                if state["fail"]:
                    raise RuntimeError("transient")
                return [_lineage_source()]

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Flaky()))
        assert "predecessor" not in tuple(onboarding_import._sources())
        state["fail"] = False
        assert "predecessor" in tuple(onboarding_import._sources())

    def test_a_new_context_is_resolved_afresh(self):
        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_provider_of(_lineage_source())))
        assert "predecessor" in tuple(onboarding_import._sources())

        class _Other:
            def import_sources(self) -> list[ImportSource]:
                return [_lineage_source(id="second")]

        set_context(dataclasses.replace(base, import_sources=_Other()))
        ids = tuple(onboarding_import._sources())
        assert "second" in ids and "predecessor" not in ids


class TestHostileDescriptorsCannotCrashDiscovery:
    """A descriptor is out-of-tree DATA, so any attribute can be any object.

    Normalization must be total: one malformed contribution must cost that source
    alone, never discovery for every source including the builtins.
    """

    class _Raising:
        id = "predecessor"
        display_name = "Predecessor"
        home_dir = ".predecessor"
        env_vars: tuple[str, ...] = ()

        @property
        def managed_mcp_names(self):
            raise RuntimeError("attribute access exploded")

    @pytest.mark.parametrize(
        "attrs",
        [
            {"env_vars": 5},
            {"env_vars": None},
            {"managed_mcp_names": 7},
            {"stale_mcp_binaries": object(), "superseded": True},
        ],
    )
    def test_an_unreadable_attribute_drops_the_source_fail_closed(self, attrs):
        """Empty is the PERMISSIVE answer for the MCP-name fields, so a descriptor
        whose attributes cannot be read is dropped rather than read as empty."""
        _install(_lineage_source(**attrs))
        ids = tuple(onboarding_import._sources())
        assert "predecessor" not in ids
        assert ("codex", "claude_code", "gemini", "openclaw", "hermes") == ids

    @pytest.mark.parametrize(
        "attrs",
        [
            {"env_vars": "PREDECESSOR_HOME"},
            {"managed_mcp_names": "predecessor-core"},
            {"stale_mcp_binaries": "node", "superseded": True},
            {"managed_mcp_names": {"predecessor-core": 1}},
        ],
    )
    def test_a_scalar_or_mapping_is_refused_not_iterated(self, attrs):
        """A string is a sequence of CHARACTERS.

        Iterating `env_vars="PREDECESSOR_HOME"` yields sixteen one-character
        names, and `stale_mcp_binaries="node"` yields four that each pass the
        shared-runtime refusal individually — so the natural authoring slip of
        passing a bare string must be refused, not silently shredded.
        """
        _install(_lineage_source(**attrs))
        assert "predecessor" not in tuple(onboarding_import._sources())

    def test_a_bare_string_launcher_cannot_reclaim_single_letter_commands(
        self, tmp_path, monkeypatch
    ):
        """The end-to-end consequence the shredding would have had."""
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"n-server": {"command": "n"}}}), "utf-8")
        monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", path)
        _install(_lineage_source(stale_mcp_binaries="node", superseded=True))
        assert mcp_cleanup.clean_stale_managed_mcp() == []

    def test_junk_inside_a_readable_sequence_is_filtered_not_fatal(self):
        """A value the engine can interpret is interpreted; only an unreadable
        attribute is fail-closed."""
        _install(_lineage_source(env_vars=[None, 5, "PREDECESSOR_HOME"]))
        assert "predecessor" in tuple(onboarding_import._sources())

    def test_an_attribute_that_raises_costs_only_that_source(self, caplog):
        class _Provider:
            def import_sources(self):
                return [TestHostileDescriptorsCannotCrashDiscovery._Raising()]

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Provider()))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.onboarding_import"):
            ids = tuple(onboarding_import._sources())
        assert ("codex", "claude_code", "gemini", "openclaw", "hermes") == ids

    def test_a_usable_contribution_alongside_a_broken_one_still_registers(self):
        class _Provider:
            def import_sources(self):
                return [
                    _lineage_source(id="broken", env_vars=5, home_dir=""),
                    _lineage_source(id="usable"),
                ]

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Provider()))
        ids = tuple(onboarding_import._sources())
        assert "usable" in ids and "broken" not in ids


class TestPlanIsTheAuthorityAtApply:
    """Everything downstream of a plan trusts the plan, not a fresh registry read.

    A preview validated every id against one snapshot to build the plan. Apply
    re-filtering against a fail-closed read meant a transient adapter failure
    between the two silently dropped a source the user had selected — and apply
    then reported success having imported nothing for it.
    """

    def _plan(self) -> dict:
        return {
            "sources": [
                {
                    "id": "predecessor",
                    "name": "Predecessor",
                    "root": "/tmp/x/.predecessor",
                    "user_home": "/tmp/x",
                    "categories": [{"id": "memories", "label": "Memories", "count": 1}],
                }
            ],
            "selection": [{"source_id": "predecessor", "category_id": "memories"}],
        }

    def test_selection_survives_a_registry_that_no_longer_lists_the_source(self):
        # No source registered: this is the degraded-registry state at apply time.
        assert "predecessor" not in tuple(onboarding_import._sources())
        assert onboarding_import._selected_pairs(self._plan()) == {("predecessor", "memories")}

    def test_roots_and_homes_survive_the_same_state(self):
        plan = self._plan()
        assert "predecessor" in onboarding_import._plan_roots(plan)
        assert "predecessor" in onboarding_import._plan_user_homes(plan)

    def test_a_selection_naming_a_source_absent_from_the_plan_is_still_rejected(self):
        """Trusting the plan is not trusting the request: a pair the plan does not
        contain has no root, so importing it would be undefined."""
        plan = self._plan()
        plan["selection"].append({"source_id": "ghost", "category_id": "memories"})
        assert onboarding_import._selected_pairs(plan) == {("predecessor", "memories")}

    def test_an_unknown_category_is_still_rejected(self):
        plan = self._plan()
        plan["selection"].append({"source_id": "predecessor", "category_id": "not_a_category"})
        assert onboarding_import._selected_pairs(plan) == {("predecessor", "memories")}


class TestSourceFilter:
    """``_preview(source_ids=[...])`` narrows BOTH the reported sources and the
    default selection — an unfiltered selection would import from a source the
    caller deliberately excluded."""

    def _two_installs(self, tmp_path):
        codex = tmp_path / ".codex"
        codex.mkdir()
        (codex / "AGENTS.md").write_text("codex rules\n", encoding="utf-8")
        predecessor = tmp_path / ".predecessor"
        (predecessor / "workspace").mkdir(parents=True)
        (predecessor / "workspace" / "AGENTS.md").write_text("other rules\n", encoding="utf-8")

    def test_filter_limits_sources_and_selection(self, tmp_path):
        self._two_installs(tmp_path)
        _install(_lineage_source())
        preview = onboarding_import._preview(["codex"], tmp_path, {})
        assert [source["id"] for source in preview["sources"]] == ["codex"]
        assert {pair["source_id"] for pair in preview["selection"]} == {"codex"}

    def test_no_filter_reports_every_detected_source(self, tmp_path):
        self._two_installs(tmp_path)
        _install(_lineage_source())
        preview = onboarding_import._preview(None, tmp_path, {})
        assert {source["id"] for source in preview["sources"]} == {"codex", "predecessor"}

    def test_unknown_id_in_the_filter_is_reported_not_scanned(self, tmp_path):
        self._two_installs(tmp_path)
        preview = onboarding_import._preview(["codex", "ghost"], tmp_path, {})
        assert [source["id"] for source in preview["sources"]] == ["codex"]
        # Silently dropping it would leave the caller believing it was imported.
        assert {
            (entry["source_id"], entry["reason"])
            for entry in preview["skipped"]
            if entry["reason"] == "unknown_source"
        } == {("ghost", "unknown_source")}


class TestUnknownSourceIsInert:
    def test_scanning_an_unregistered_id_reports_rather_than_raises(self, tmp_path):
        """A stale id in a persisted plan must not crash the scan."""
        root = tmp_path / "whatever"
        root.mkdir()
        scan = onboarding_import._scan_source("ghost", root, tmp_path)
        assert any(entry.get("reason") == "unknown_source" for entry in scan.skipped)
