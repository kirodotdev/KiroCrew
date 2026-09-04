"""Off-loop DB discipline for the knowledge store (#7078, remedy A of #3057).

``scripts/check_sync_io_in_async.py`` rejects a blocking DB call written
*lexically* inside an ``async def``. It cannot see the same call one frame down:
an ``async def`` that calls a plain synchronous store method which runs the
query. That interprocedural half is what this guard closes, by checking at the
one accessor every query funnels through -- ``KnowledgeStore.db``.

These tests pin the discipline from four directions:

1. the runtime chokepoint itself (strict raise / production warn / off-loop
   no-op / throttled warning),
2. the *interprocedural* catch, together with proof that the static gate is
   blind to the very same call -- the two are complementary, not redundant,
3. the deliberate departure from ``history.py``: ``KIROCREW_DEV_MODE`` alone
   must NOT arm this store's raise while #7019's 85 recorded on-loop callers
   are outstanding,
4. a mutation guard, so deleting the check from the accessor fails loudly
   instead of silently disarming everything above.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import textwrap
import time
from pathlib import Path

import pytest

from kiro_crew import on_loop_db
from kiro_crew.knowledge.store import _ON_LOOP_DB_GUARD, KnowledgeStore
from kiro_crew.on_loop_db import (
    STORE_STRICT_ENV,
    STRICT_ENV,
    OnLoopDBGuard,
    OnLoopStoreError,
    strict_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path: Path) -> KnowledgeStore:
    """A store built OFF the loop.

    Construction is the one sanctioned on-loop take (``__init__`` wraps its
    schema init, migrations and graph load in ``allow_on_loop()`` -- see
    ``TestConstructionOnLoopIsSanctioned``). Every test here still builds the
    store off-loop so that what it exercises afterwards is exactly one
    deliberate accessor take, not construction noise.
    """
    return KnowledgeStore(str(tmp_path / "knowledge.db"))


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The guard is module-level, so its throttle clock leaks between tests."""
    _ON_LOOP_DB_GUARD.reset_throttle()
    yield
    _ON_LOOP_DB_GUARD.reset_throttle()


class TestOnLoopGuard:
    """The runtime chokepoint: ``KnowledgeStore.db`` flags on-loop entry."""

    @pytest.mark.asyncio
    async def test_on_loop_db_raises_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        with pytest.raises(OnLoopStoreError):
            st.db  # noqa: B018 - taking the connection is the operation under test

    def test_off_loop_db_allowed_under_strict(self, store, monkeypatch):
        """No running loop (worker thread / executor lane / CLI / cron) is the
        sanctioned path -- strict mode must not flag it."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        assert store.db.execute("SELECT 1").fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_offloaded_call_is_allowed_under_strict(self, tmp_path, monkeypatch):
        """The remedy the message names must actually satisfy the guard."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        got = await asyncio.to_thread(lambda: st.db.execute("SELECT 1").fetchone()[0])
        assert got == 1

    @pytest.mark.asyncio
    async def test_on_loop_db_warns_in_production_mode(self, tmp_path, monkeypatch, caplog):
        """Strict off (production): the take proceeds but logs loudly, so a
        mis-wired call-site is never silent. Production must not start raising --
        that would turn a slow query into a failed request."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "0")
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        with caplog.at_level("WARNING", logger="kiro_crew.on_loop_db"):
            assert st.db.execute("SELECT 1").fetchone()[0] == 1
        assert any("event loop" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_on_loop_warning_is_throttled(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "0")
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        with caplog.at_level("WARNING", logger="kiro_crew.on_loop_db"):
            for _ in range(3):
                st.db.execute("SELECT 1").fetchone()
        assert sum("event loop" in r.message for r in caplog.records) == 1

    def test_first_warning_is_never_swallowed_by_the_throttle(self, caplog, monkeypatch):
        """A 0.0 "last warned" sentinel compares against ``time.monotonic()``,
        which is small on a freshly-booted host -- so the FIRST warning, the one
        that matters most, could fall inside the throttle window and vanish. The
        guard uses a None sentinel; pin it with a huge window.
        """
        monkeypatch.setattr(on_loop_db, "DEFAULT_WARN_INTERVAL_S", 1e9)
        guard = OnLoopDBGuard(label="probe store", remedy="offload it")

        async def _take() -> None:
            guard.check()

        with caplog.at_level("WARNING", logger="kiro_crew.on_loop_db"):
            asyncio.run(_take())
        assert any("probe store" in r.message for r in caplog.records)


class TestInterproceduralCatch:
    """The half the static gate cannot see -- the reason this guard exists."""

    # An async frame reaching the store through a plain SYNC store method. No
    # DB-named call appears lexically inside the ``async def``.
    _INTERPROCEDURAL_SRC = (
        "async def handler(store, item_id):\n" "    return store.get_item(item_id)\n"
    )

    @pytest.mark.asyncio
    async def test_sync_helper_one_frame_down_is_caught(self, tmp_path, monkeypatch):
        """``get_item`` is a plain ``def`` that runs ``self.db.execute(...)``.
        Called from an ``async def`` it runs the busy wait on the loop, and the
        runtime guard is what notices."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))

        async def handler():
            return st.get_item("does-not-exist")

        with pytest.raises(OnLoopStoreError):
            await handler()

    def test_static_gate_is_blind_to_that_same_call(self):
        """Complementarity, asserted against the real gate rather than claimed:
        the gate reports NOTHING for the interprocedural form, so the runtime
        guard is not redundant with it. If a future gate learns to see this,
        this test fails and the overlap can be re-judged deliberately."""
        spec = importlib.util.spec_from_file_location(
            "_sync_io_gate", REPO_ROOT / "scripts" / "check_sync_io_in_async.py"
        )
        assert spec and spec.loader
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)

        # Coherence check: the detector does fire on the LEXICAL form, so a zero
        # below is a real blind spot and not a mis-wired probe.
        lexical = gate._violations_in_source(
            "async def handler(store):\n    return store.db.execute('SELECT 1').fetchone()\n"
        )
        assert lexical, "probe is mis-wired: the gate did not flag the lexical form"

        interprocedural = gate._violations_in_source(self._INTERPROCEDURAL_SRC)
        assert interprocedural == [], (
            "the static gate now sees the interprocedural form; re-judge whether "
            f"the runtime guard still adds coverage. found: {interprocedural}"
        )


class TestDevModeDeparture:
    """This store deliberately does NOT let ``KIROCREW_DEV_MODE`` alone raise.

    ``history.py``'s guard can arm that branch because every session mutator was
    already offloaded when it landed, so a raise there means genuinely new
    drift. This store still has 85 recorded on-loop callers (the whole of
    ``.github/sync-io-in-async-baseline.txt``), owned by #7019. Raising on a
    tracked backlog reports it as a regression, and the developer's rational
    response -- unset ``KIROCREW_DEV_MODE`` -- would silence history.py's guard
    too. Pinned so flipping it later is a conscious edit with a failing test.
    """

    @pytest.mark.asyncio
    async def test_dev_mode_alone_warns_but_does_not_raise(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("KIROCREW_STRICT_ON_LOOP_STORE", raising=False)
        monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        with caplog.at_level("WARNING", logger="kiro_crew.on_loop_db"):
            assert st.db.execute("SELECT 1").fetchone()[0] == 1  # must NOT raise
        assert any("event loop" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_explicit_opt_in_still_raises_under_dev_mode(self, tmp_path, monkeypatch):
        """The escape from the departure: a CI job or a discipline test that
        wants the hard failure exports the explicit flag."""
        monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        with pytest.raises(OnLoopStoreError):
            st.db  # noqa: B018

    def test_store_guard_opts_out_of_the_dev_mode_arm(self):
        """Pin the wiring, not just the behaviour: the store's guard instance
        must be the one that excludes dev mode."""
        assert _ON_LOOP_DB_GUARD._include_dev_mode is False


class TestSharedSwitchCannotArmThisStore:
    """Regression: the shared switch is ALREADY exported in CI.

    ``setup.py``'s ``test_e2e`` and ``.github/workflows/ci.yml`` both export
    ``KIROCREW_STRICT_ON_LOOP_PERSIST=1`` into the e2e gateway, scoped when they
    were written to history's fully-offloaded surface. If this store read that
    same switch, the on-loop ``/api/knowledge/stats`` and
    ``/api/knowledge/namespaces`` handlers would raise and 500 the e2e run --
    a green-looking flag arming a raise on the backlog #7019 owns. The store
    therefore reads its OWN switch. Pinned here because the failure only shows
    up in an expensive end-to-end job.
    """

    def test_store_reads_its_own_switch(self):
        assert _ON_LOOP_DB_GUARD._strict_env == STORE_STRICT_ENV
        assert STORE_STRICT_ENV != STRICT_ENV

    @pytest.mark.asyncio
    async def test_ci_shared_flag_alone_does_not_raise(self, tmp_path, monkeypatch):
        """The exact e2e environment: shared flag on, store flag absent."""
        monkeypatch.setenv(STRICT_ENV, "1")
        monkeypatch.delenv(STORE_STRICT_ENV, raising=False)
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        assert st.db.execute("SELECT 1").fetchone()[0] == 1  # must NOT raise

    @pytest.mark.asyncio
    async def test_the_e2e_handler_shapes_survive_the_shared_flag(self, tmp_path, monkeypatch):
        """Both shapes the e2e Playwright run exercises against /knowledge: the
        interprocedural one (``get_stats`` -> ``self.db``) and the direct one
        (``store.db.execute``) that the namespaces handler uses."""
        monkeypatch.setenv(STRICT_ENV, "1")
        monkeypatch.delenv(STORE_STRICT_ENV, raising=False)
        st = await asyncio.to_thread(KnowledgeStore, str(tmp_path / "k.db"))
        assert isinstance(st.get_stats(), dict)
        st.db.execute(
            "SELECT namespace, COUNT(*) FROM items WHERE status = 'active' GROUP BY namespace"
        ).fetchall()

    def test_ci_exports_the_shared_flag_not_the_store_flag(self):
        """The premise above, asserted against the real CI config rather than
        assumed. If CI later exports the store switch too, this fails and the
        narrowing must be re-judged (that is the #7019 completion signal)."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        setup_py = (REPO_ROOT / "setup.py").read_text(encoding="utf-8")
        assert STRICT_ENV in ci and STRICT_ENV in setup_py, (
            "the shared switch is no longer exported by CI; this store's "
            "narrowing was justified by that export"
        )
        assert STORE_STRICT_ENV not in ci and STORE_STRICT_ENV not in setup_py, (
            "CI now exports the store switch -- the knowledge store's on-loop "
            "callers must be offloaded (#7019) before that can hold"
        )


class TestConstructionOnLoopIsSanctioned:
    """Construction is the ONE vetted on-loop take (#8231).

    ``setup_knowledge_routes()`` reads the gateway's lazy ``knowledge_store``
    property at route registration, before the socket binds, so ``__init__``
    -- schema init, migrations, graph load -- runs on the event-loop thread on
    every launch, by the constructor's documented design (moving that work off
    the boot path is #8329). Before #8231 the guard warned on every boot for
    that deliberate take. The constructor now wraps exactly those calls in
    ``allow_on_loop()``; these tests pin both directions -- construction is
    silent, AND the guard stays fully armed on every path after the block ends.
    """

    @staticmethod
    def _guard_records(caplog):
        """Only THIS guard's records: construction runs dozens of migration
        statements that may legitimately log through other loggers, and
        ``caplog.records`` captures every logger at the handler."""
        return [r for r in caplog.records if r.name == "kiro_crew.on_loop_db"]

    @pytest.mark.asyncio
    async def test_construction_on_loop_emits_no_warning(self, tmp_path, monkeypatch, caplog):
        """The boot-time symptom itself: building the store on the loop must
        not log the on-loop diagnostic. (The presence control proving this
        probe is wired lives in ``test_reader_after_construction_still_warns``,
        which sees the same logger fire for a real reader take.)"""
        monkeypatch.setenv(STORE_STRICT_ENV, "0")
        with caplog.at_level("WARNING", logger="kiro_crew.on_loop_db"):
            KnowledgeStore(str(tmp_path / "k.db"))
        assert not self._guard_records(caplog)

    @pytest.mark.asyncio
    async def test_construction_on_loop_does_not_raise_under_strict(self, tmp_path, monkeypatch):
        """Strict mode must not turn a sanctioned constructor into a boot
        failure -- and the guard must re-arm the instant the block ends."""
        monkeypatch.setenv(STORE_STRICT_ENV, "1")
        st = KnowledgeStore(str(tmp_path / "k.db"))  # must NOT raise
        with pytest.raises(OnLoopStoreError):
            st.db  # noqa: B018 - the accessor take is the operation under test

    @pytest.mark.asyncio
    async def test_reader_after_construction_still_warns(self, tmp_path, monkeypatch, caplog):
        """The other mutation direction: the opt-out must not disarm the real
        reader paths. A genuine on-loop take right after construction warns --
        which also proves the suppression left the throttle clock untouched."""
        monkeypatch.setenv(STORE_STRICT_ENV, "0")
        with caplog.at_level("WARNING", logger="kiro_crew.on_loop_db"):
            st = KnowledgeStore(str(tmp_path / "k.db"))
            assert not self._guard_records(caplog), "construction itself must stay silent"
            st.get_item("does-not-exist")
        assert any("event loop" in r.message for r in self._guard_records(caplog))

    def test_allow_on_loop_resets_on_exception(self, caplog):
        """The opt-out is a ContextVar token reset in a ``finally``, so an
        exception inside the block must not leave the guard suppressed."""
        guard = OnLoopDBGuard(label="probe store", remedy="offload it")

        async def _probe() -> None:
            with pytest.raises(RuntimeError):
                with guard.allow_on_loop():
                    guard.check()  # suppressed: must not warn
                    raise RuntimeError("boom")
            guard.check()  # must warn: the opt-out ended with the block

        with caplog.at_level("WARNING", logger="kiro_crew.on_loop_db"):
            asyncio.run(_probe())
        assert sum("probe store" in r.message for r in caplog.records) == 1

    def test_constructor_wraps_init_in_the_scoped_opt_out(self):
        """Mutation guard: the ``allow_on_loop()`` ``with`` block's body must be
        EXACTLY the three init calls. Textual-order checks are not enough: they
        stay green when the calls are dedented out of the block (the likely
        drift in a later constructor edit), and they cannot see a FOURTH call
        joining the block -- which the scoped exemption would silently sanction
        against a ``blocking: true`` rule. AST nesting catches both.
        """
        src = textwrap.dedent(inspect.getsource(KnowledgeStore.__init__))
        tree = ast.parse(src)
        blocks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "allow_on_loop"
                for item in node.items
            )
        ]
        assert len(blocks) == 1, "constructor must use the scoped opt-out exactly once"
        calls = []
        for stmt in blocks[0].body:
            assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call), (
                "only plain self-method calls belong in the sanctioned block, "
                f"found {ast.dump(stmt)[:80]}"
            )
            func = stmt.value.func
            assert (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ), "the sanctioned block must call methods on self only"
            calls.append(func.attr)
        assert calls == ["_init_schema", "_migrate", "_load_graph"], (
            "the sanctioned block's body changed -- a call moved out (re-arming "
            "the guard for it) or joined in (sanctioning NEW on-loop work, which "
            f"needs its own justification): {calls}"
        )


class TestGuardIsWired:
    def test_db_property_enters_the_guard(self):
        """Mutation guard: removing the check from the accessor silently disarms
        every other protection here -- pin the call."""
        src = textwrap.dedent(inspect.getsource(KnowledgeStore.db.fget))
        tree = ast.parse(src)
        attrs = [
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        assert "check" in attrs, "KnowledgeStore.db no longer calls the on-loop guard"

    def test_guard_check_precedes_the_connection(self):
        """The check must run BEFORE the cached-connection fast path, or a
        second on-loop query on an already-open connection escapes it."""
        src = inspect.getsource(KnowledgeStore.db.fget)
        assert src.index("_ON_LOOP_DB_GUARD.check()") < src.index("_thread_local")

    def test_throttle_uses_monotonic_clock(self):
        """Wall-clock jumps must not re-open or permanently close the window."""
        src = inspect.getsource(OnLoopDBGuard.check)
        assert "time.monotonic()" in src
        assert time.monotonic  # imported and real


class TestStrictSwitchDoesNotDrift:
    """``on_loop_db.strict_enabled`` re-derives the switch instead of calling
    ``history.on_loop_persist_strict()``, because history's version hardcodes the
    dev-mode arm and this store needs that arm excludable. Two readers of one
    env var can drift apart, so pin that they agree wherever they overlap.
    """

    CASES = [
        {},
        {"KIROCREW_STRICT_ON_LOOP_PERSIST": "1"},
        {"KIROCREW_STRICT_ON_LOOP_PERSIST": "true"},
        {"KIROCREW_STRICT_ON_LOOP_PERSIST": "0"},
        {"KIROCREW_STRICT_ON_LOOP_PERSIST": "off"},
        {"KIROCREW_DEV_MODE": "1"},
        {"KIROCREW_DEV_MODE": "yes"},
        {"KIROCREW_DEV_MODE": "0"},
        # explicit falsy must force-disable even under dev mode, in both readers
        {"KIROCREW_DEV_MODE": "1", "KIROCREW_STRICT_ON_LOOP_PERSIST": "0"},
        {"KIROCREW_DEV_MODE": "1", "KIROCREW_STRICT_ON_LOOP_PERSIST": "1"},
        # an unrecognised value is neither truthy nor falsy in either reader
        {"KIROCREW_STRICT_ON_LOOP_PERSIST": "maybe"},
        {"KIROCREW_DEV_MODE": "1", "KIROCREW_STRICT_ON_LOOP_PERSIST": "maybe"},
    ]

    def test_agrees_with_history_when_dev_mode_is_armed(self, monkeypatch):
        from kiro_crew.history import on_loop_persist_strict

        for env in self.CASES:
            for var in ("KIROCREW_STRICT_ON_LOOP_PERSIST", "KIROCREW_DEV_MODE"):
                monkeypatch.delenv(var, raising=False)
            for key, value in env.items():
                monkeypatch.setenv(key, value)
            assert (
                strict_enabled(include_dev_mode=True) == on_loop_persist_strict()
            ), f"strict switch drifted from history.py for {env}"

    def test_excluding_dev_mode_differs_only_on_the_dev_mode_arm(self, monkeypatch):
        """The narrowing must be exactly the dev-mode arm and nothing else: for
        every case where the explicit flag decides, both forms must agree."""
        for env in self.CASES:
            for var in ("KIROCREW_STRICT_ON_LOOP_PERSIST", "KIROCREW_DEV_MODE"):
                monkeypatch.delenv(var, raising=False)
            for key, value in env.items():
                monkeypatch.setenv(key, value)
            explicit = env.get("KIROCREW_STRICT_ON_LOOP_PERSIST", "").strip().lower()
            if explicit in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                assert strict_enabled(include_dev_mode=False) == strict_enabled(
                    include_dev_mode=True
                ), f"narrowing changed an explicitly-decided case: {env}"
            else:
                assert (
                    strict_enabled(include_dev_mode=False) is False
                ), f"narrowed form must never raise without the explicit flag: {env}"
