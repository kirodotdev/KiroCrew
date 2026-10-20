"""Tests that ``memory.*`` settings are validated on load and reach the stores they tune.

They pin that the CLI and dashboard paths pass ``embedding_dim``; that the production store
constructions pass the loaded config as ``config=`` while every other construction site
carries a recorded exemption (an inventory over src/kiro_crew, with a seeded self-test of
the guard); and that the config loader coerces a numeric ``episodic_dedup_threshold``
string, clamps it into the cosine range and falls back to the default on a typo there and in
its numeric siblings. The decay-rate sanitizer is pinned beside its scoring in
``test_vector_memory_recall_ranking.py``; the store core lives in ``test_vector_memory.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.vector_memory import VectorMemoryStore

# Same xdist group as ``test_vector_memory.py`` and the topic files its docstring names.
# ``TestMemoryTuningPlumbing`` walks every module under src/kiro_crew to inventory the store
# construction sites.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_vector_memory")


#: Every callable that constructs a store, so each is held to the config= plumbing
#: rule below. ``declared_store`` builds the store a named store's declaration asks for.
_STORE_CONSTRUCTORS = ("VectorMemoryStore", "open_member_database", "declared_store")


class TestEmbeddingDimPlumbing:
    """Tests that CLI and dashboard paths pass cfg.memory.embedding_dim to VectorMemoryStore."""

    def test_learn_cmd_passes_embedding_dim(self, tmp_path: Path, monkeypatch) -> None:
        """_learn() constructs VectorMemoryStore with embedding_dim from config."""
        from unittest.mock import MagicMock, patch

        mock_cfg = MagicMock()
        mock_cfg.memory.embedding_dim = 768

        captured_kwargs: dict = {}
        original_init = VectorMemoryStore.__init__

        def capturing_init(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            # Use tmp_path to avoid touching real db
            kwargs.setdefault("db_path", tmp_path / "test_learn.db")
            original_init(self, *args, **kwargs)

        with (
            patch("kiro_crew.cli_commands.KiroCrewConfig.load", return_value=mock_cfg),
            patch.object(VectorMemoryStore, "__init__", capturing_init),
            patch.object(VectorMemoryStore, "init", return_value=None),
            patch.object(VectorMemoryStore, "close", return_value=None),
        ):
            import argparse

            from kiro_crew.cli_commands import _learn

            args = argparse.Namespace(learn_action="list")
            try:
                _learn(args)
            except (SystemExit, Exception):
                pass  # We only care that VectorMemoryStore got the right dim

        assert captured_kwargs.get("embedding_dim") == 768

    def test_memory_cmd_passes_embedding_dim(self, tmp_path: Path, monkeypatch) -> None:
        """_memory_cmd() constructs VectorMemoryStore with embedding_dim from config."""
        from unittest.mock import MagicMock, patch

        mock_cfg = MagicMock()
        mock_cfg.memory.embedding_dim = 512

        captured_kwargs: dict = {}
        original_init = VectorMemoryStore.__init__

        def capturing_init(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            kwargs.setdefault("db_path", tmp_path / "test_mem.db")
            original_init(self, *args, **kwargs)

        with (
            patch("kiro_crew.cli_commands.KiroCrewConfig.load", return_value=mock_cfg),
            patch.object(VectorMemoryStore, "__init__", capturing_init),
            patch.object(VectorMemoryStore, "init", return_value=None),
            patch.object(VectorMemoryStore, "close", return_value=None),
        ):
            import argparse

            from kiro_crew.cli_commands import _memory_cmd

            args = argparse.Namespace(mem_action="list")
            try:
                _memory_cmd(args)
            except (SystemExit, Exception):
                pass

        assert captured_kwargs.get("embedding_dim") == 512

    def test_dashboard_fallback_passes_embedding_dim(self, tmp_path: Path, monkeypatch) -> None:
        """Dashboard _get_vector_store fallback constructs store with cfg embedding_dim."""
        from unittest.mock import MagicMock

        import kiro_crew.dashboard.handlers.memory as mem_mod

        mock_cfg = MagicMock()
        mock_cfg.memory.embedding_dim = 384

        captured_kwargs: dict = {}

        class TrackingStore:
            """Captures __init__ kwargs to verify embedding_dim is passed."""

            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            def reconfigure(self, cfg):
                pass

            def init(self):
                pass

        # _get_vector_store does lazy `from kiro_crew.config.loader import KiroCrewConfig`
        # and `from kiro_crew.vector_memory import VectorMemoryStore` inside the function.
        # Patch at the source module so the local import picks them up.
        import kiro_crew.config.loader as loader_mod
        import kiro_crew.vector_memory as vm_mod

        mock_config_cls = MagicMock()
        mock_config_cls.load.return_value = mock_cfg

        monkeypatch.setattr(loader_mod, "KiroCrewConfig", mock_config_cls)
        monkeypatch.setattr(vm_mod, "VectorMemoryStore", TrackingStore)

        # Mock _get_memory to return a Memory with no vector_store
        mock_memory = MagicMock()
        mock_memory.vector_store = None
        monkeypatch.setattr(mem_mod, "_get_memory", lambda state: mock_memory)

        # Use a plain object that lacks _standalone_vector
        fresh_state = type("FreshState", (), {})()

        mem_mod._get_vector_store(fresh_state)

        assert captured_kwargs.get("embedding_dim") == 384


class TestMemoryTuningPlumbing:
    """Configured ``memory.*`` tuning must reach every store that consumes it.

    ``VectorMemoryStore.reconfigure(cfg)`` applies every tunable the class copies
    out of config, and the live reload path already uses it. Each plumbed site
    passes its loaded config as ``config=``, which the constructor applies through
    ``reconfigure`` before subscribing to the watcher, so boot and reload apply the
    same values by construction, not by a hand-copied keyword list that can drop a
    key.
    """

    # Production stores on the live install whose episodic reads or writes reach
    # the similarity, cap-eviction or keyword-fallback paths. The cap check runs on
    # deferred writes too, so the ops-mission-control ledger indexer and lesson
    # recall are listed, as are the CLI's lesson, carve and import/migrate stores.
    _PLUMBED_SITES = (
        "slack/gateway_runtime/service_boot.py",
        "cli_server.py",
        "cli_commands_learn.py",
        "cli_commands_memory.py",
        "dashboard/handlers/memory_services.py",
        "apps/builtins/ops_mission_control/backend/dispatch.py",
        "apps/builtins/ops_mission_control/backend/routes.py",
        # Named stores, through declared_store.
        "context.py",
    )
    # Every other file that constructs a store, with the reason the AST guard does
    # not check it. The inventory test fails on a new construction site that is in
    # neither table, so a new store cannot silently fall back to the defaults.
    _EXEMPT_SITES = {
        # Forwards its caller's `**vector_options` unchanged.
        "vector_memory_member.py": "open_member_database and declared_store forward caller options",
        # Builds its store on an arbitrary data_home whose config
        # KiroCrewConfig.load() does not read.
        "onboarding_import.py": "foreign data_home",
        # The bench harness sweeps its own values.
        "eval/bench/ingest.py": "bench sweep values",
        # The V2 bench harness opens its own throwaway member database.
        "eval/bench/member_v2.py": "bench V2 member database",
        # The lesson recall bench writes its golden rules into a throwaway store.
        "eval/bench/lesson_recall.py": "bench lesson recall store",
        # Scenario workspaces are throwaway stores, not the install's memory.
        "eval/runner.py": "eval scenario workspace",
        # Read-only injection audit; it never writes an episode.
        "security/memory_audit.py": "read-only injection audit",
    }

    def test_dashboard_fallback_passes_configured_options(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The dashboard standalone fallback carries the configured tuning."""
        from unittest.mock import MagicMock

        import kiro_crew.dashboard.handlers.memory as mem_mod

        mock_cfg = MagicMock()
        mock_cfg.memory.embedding_dim = 1024
        mock_cfg.memory.decay_rates = {}
        # Deliberately differ from both defaults so omitted keywords cannot pass.
        mock_cfg.memory.episodic_dedup_threshold = 0.42
        mock_cfg.memory.episodic_max_count = 321
        mock_cfg.memory.semantic_confidence_threshold = 0.61
        mock_cfg.memory.episodic_max_results = 7
        mock_cfg.memory.semantic_keys = ["team."]

        built: list[VectorMemoryStore] = []

        class TmpPathStore(VectorMemoryStore):
            """A real store carrying captured configuration on a throwaway DB."""

            def __init__(self, **kwargs):
                kwargs.setdefault("db_path", tmp_path / "dashboard.db")
                super().__init__(**kwargs)
                built.append(self)

            def init(self) -> None:  # sqlite/migrations are not what this pins
                return None

        import kiro_crew.config.loader as loader_mod
        import kiro_crew.vector_memory as vm_mod

        mock_config_cls = MagicMock()
        mock_config_cls.load.return_value = mock_cfg
        monkeypatch.setattr(loader_mod, "KiroCrewConfig", mock_config_cls)
        monkeypatch.setattr(vm_mod, "VectorMemoryStore", TmpPathStore)

        mock_memory = MagicMock()
        mock_memory.vector_store = None
        monkeypatch.setattr(mem_mod, "_get_memory", lambda state: mock_memory)

        fresh_state = type("FreshState", (), {})()
        mem_mod._get_vector_store(fresh_state)

        assert len(built) == 1, "fallback did not construct the standalone store"
        assert built[0]._dedup_threshold == 0.42
        assert built[0]._episodic_max == 321
        assert built[0]._confidence_threshold == 0.61
        assert built[0]._episodic_limit == 7
        assert "team." in built[0]._prefixes

    @staticmethod
    def _find_unplumbed_sites(tree, where: str = "") -> list[str]:
        """Report every store construction that does not take ``config=``.

        Each ``VectorMemoryStore(...)``, and each ``open_member_database(...)`` or
        ``declared_store(...)`` (each constructs one), must pass the loaded config as ``config=``: the
        constructor applies it through ``reconfigure`` before it subscribes to
        the live watcher, so a reload landing during construction is never
        overwritten by the caller's older snapshot. Checked per call, so each
        branch that builds a store carries its own ``config=``. A literal value
        is flagged (``config=None`` is the defaults), and so is a tuning keyword:
        ``reconfigure`` is the one source, and a second copy is how boot and
        reload drift apart.
        """
        import ast

        tunables = {
            "confidence_threshold",
            "extra_prefixes",
            "dedup_threshold",
            "episodic_max",
            "episodic_limit",
            "decay_rates",
        }
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _STORE_CONSTRUCTORS:
                continue
            config = next((kw.value for kw in node.keywords if kw.arg == "config"), None)
            if config is None:
                violations.append(
                    f"{where}line {node.lineno}: {name}(...) does not pass config=cfg"
                )
            elif isinstance(config, ast.Constant):
                violations.append(
                    f"{where}line {node.lineno}: {name}(...) passes a literal config="
                )
            for kw in node.keywords:
                if kw.arg in tunables:
                    violations.append(
                        f"{where}line {node.lineno}: {kw.arg} is set on the "
                        "constructor — config=cfg is its only source"
                    )
        return violations

    def test_production_sites_pass_configured_options(self) -> None:
        import ast
        import inspect
        from pathlib import Path as _Path

        import kiro_crew

        pkg_root = _Path(inspect.getfile(kiro_crew)).parent
        violations: list[str] = []
        for rel in self._PLUMBED_SITES:
            src = pkg_root / rel
            assert src.is_file(), f"construction site moved: {rel}"
            tree = ast.parse(src.read_text(encoding="utf-8"))
            violations.extend(self._find_unplumbed_sites(tree, where=f"{rel}:"))
        assert not violations, "stores built without config=cfg:\n" + "\n".join(violations)

    def test_every_construction_site_is_plumbed_or_exempted(self) -> None:
        """A new production store construction must join one of the tables.

        ``open_member_database(...)`` and ``declared_store(...)`` count: each
        constructs a store, and the per-site guard checks it the same way.
        """
        import ast
        import inspect
        from pathlib import Path as _Path

        import kiro_crew

        pkg_root = _Path(inspect.getfile(kiro_crew)).parent
        found: set[str] = set()
        for src in pkg_root.rglob("*.py"):
            rel = src.relative_to(pkg_root).as_posix()
            text = src.read_text(encoding="utf-8")
            if "/tests/" in f"/{rel}" or (
                not any(f"{name}(" in text for name in _STORE_CONSTRUCTORS)
            ):
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in _STORE_CONSTRUCTORS:
                    found.add(rel)
                    break
        known = set(self._PLUMBED_SITES) | set(self._EXEMPT_SITES)
        assert found - known == set(), (
            "new store construction site(s) neither pass config=cfg nor carry "
            f"an exemption: {sorted(found - known)}"
        )
        assert known - found == set(), f"stale site table entries: {sorted(known - found)}"

    def test_guard_catches_seeded_violations(self) -> None:
        """The guard flags a missing or literal config= and a constructor tunable."""
        import ast

        seeded = ast.parse(
            "def a():\n"
            "    s = VectorMemoryStore(embedding_dim=1024)\n"
            "def b(cfg):\n"
            "    s = VectorMemoryStore(embedding_dim=1024, dedup_threshold=0.88, config=cfg)\n"
            "def c(cfg):\n"
            "    s = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim, config=cfg)\n"
            "class D:\n"
            "    def e(self):\n"
            "        self.vm = vm.VectorMemoryStore(embedding_dim=1024, config=self._cfg)\n"
            "def f():\n"
            "    return VectorMemoryStore(config=None)\n"
            "def g(path, cfg, v2):\n"
            "    if v2:\n"
            "        s = open_member_database(path, member_id='m', store_id='s', config=cfg)\n"
            "    else:\n"
            "        s = VectorMemoryStore(db_path=path)\n"
            "    s.reconfigure(cfg)\n"
        )
        violations = self._find_unplumbed_sites(seeded)
        # `c`, `e` and g's V2 arm are the production spellings and must stay quiet.
        # g's V1 arm is caught although the function reconfigures `s` afterwards:
        # the check is per call, and a late reconfigure is the race config= closes.
        # Pin each line exactly: a count alone would pass with one kind listed twice.
        assert sorted(violations) == sorted(
            [
                "line 2: VectorMemoryStore(...) does not pass config=cfg",
                "line 4: dedup_threshold is set on the constructor — "
                "config=cfg is its only source",
                "line 11: VectorMemoryStore(...) passes a literal config=",
                "line 16: VectorMemoryStore(...) does not pass config=cfg",
            ]
        )


class TestDedupThresholdLoaderValidation:
    """The loader must normalize `memory.episodic_dedup_threshold` like its siblings.

    A bare `memory_data.get(..., 0.88)` lets a string
    typo in config.json survive to the store, where
    `cosine_sim > self._dedup_threshold` raises TypeError and aborts the
    episodic write. Sibling numeric fields (`embedding_bulk_duty`) already
    route through `_safe_float`; this class pins the same guard here.
    """

    def _memory_section(self, tmp_path: Path, monkeypatch, raw_value: object):
        import json as _json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            _json.dumps({"memory": {"episodic_dedup_threshold": raw_value}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_path)
        return KiroCrewConfig.load().memory

    def test_a_string_typo_falls_back_to_the_default(self, tmp_path: Path, monkeypatch) -> None:
        section = self._memory_section(tmp_path, monkeypatch, "0.9x")
        assert section.episodic_dedup_threshold == 0.88
        assert isinstance(section.episodic_dedup_threshold, float)

    def test_a_numeric_string_is_coerced_not_crashed(self, tmp_path: Path, monkeypatch) -> None:
        section = self._memory_section(tmp_path, monkeypatch, "0.5")
        assert section.episodic_dedup_threshold == 0.5

    def test_out_of_range_values_are_clamped_into_cosine_range(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        assert self._memory_section(tmp_path, monkeypatch, 7).episodic_dedup_threshold == 1.0
        assert self._memory_section(tmp_path, monkeypatch, -3).episodic_dedup_threshold == 0.0

    def test_a_valid_float_passes_through(self, tmp_path: Path, monkeypatch) -> None:
        assert self._memory_section(tmp_path, monkeypatch, 0.42).episodic_dedup_threshold == 0.42

    def _section_with(self, tmp_path: Path, monkeypatch, memory: dict):
        import json as _json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(_json.dumps({"memory": memory}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_path)
        return KiroCrewConfig.load().memory

    def test_sibling_numeric_fields_absorb_typos_the_same_way(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The whole MemorySection numeric block shares one validation rule.

        Same defect class as the dedup threshold: `semantic_confidence_threshold`
        is plumbed to the same production stores, and the two int fields feed
        LIMIT/retention arithmetic. A typo in any of them must fall back, not
        surface as a TypeError at first use.
        """
        section = self._section_with(
            tmp_path,
            monkeypatch,
            {
                "semantic_confidence_threshold": "high",
                "episodic_max_results": "many",
                "episodic_max_count": None,
            },
        )
        assert section.semantic_confidence_threshold == 0.8
        assert section.episodic_max_results == 8
        assert section.episodic_max_count == 10_000

    def test_sibling_fields_still_accept_valid_values(self, tmp_path: Path, monkeypatch) -> None:
        section = self._section_with(
            tmp_path,
            monkeypatch,
            {
                "semantic_confidence_threshold": 0.6,
                "episodic_max_results": 12,
                "episodic_max_count": 500,
            },
        )
        assert section.semantic_confidence_threshold == 0.6
        assert section.episodic_max_results == 12
        assert section.episodic_max_count == 500
