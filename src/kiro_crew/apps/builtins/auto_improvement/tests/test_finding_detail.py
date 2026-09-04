"""The finding-detail endpoint: joins a ledger fingerprint to its evidence.

The list endpoint returns bare ledger rows, which cannot answer "why was this
kept or rejected?". These tests pin the join and the degradation behavior — a
finding with no candidate artifact must still return its ledger facts rather
than 404 or crash.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.auto_improvement.backend import routes, store

PREFIX = "/api/apps/auto-improvement"


@pytest.fixture()
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's data root at a tmp dir and enable the app.

    ``workspace_dir`` is pinned to the same dir so a test's flat
    ``data_home/ledger.jsonl`` and the real ``store.ledger_path()`` (now under a
    per-repo ``repos/<key>/`` subtree) resolve to one place — the test is
    exercising the artifact contract, not the repo-scoping layout.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(store, "workspace_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(routes, "is_app_enabled", lambda _n: True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store.ensure_layout()
    return tmp_path / "data"


def _write_ledger(root: Path, rows: list[dict]) -> None:
    root.joinpath("ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


async def _client() -> TestClient:
    app = web.Application()
    routes.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestFindingDetail:
    @pytest.mark.asyncio
    async def test_unknown_fingerprint_is_404(self, data_home: Path) -> None:
        _write_ledger(data_home, [{"fp": "aaaa", "kind": "bug", "target": "x", "status": "seen"}])
        client = await _client()
        try:
            res = await client.get(f"{PREFIX}/findings/zzzz")
            assert res.status == 404
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_ledger_only_finding_still_returns_its_facts(self, data_home: Path) -> None:
        """A finding with no candidate artifact must degrade, not 404: the ledger
        row alone is still the answer to "what did the run see?"."""
        _write_ledger(
            data_home,
            [{"fp": "abc123", "kind": "bug", "target": "src/x.py::f", "status": "seen"}],
        )
        client = await _client()
        try:
            res = await client.get(f"{PREFIX}/findings/abc123")
            assert res.status == 200
            f = (await res.json())["finding"]
            assert f["target"] == "src/x.py::f"
            assert f["status"] == "seen"
            assert f.get("candidate") is None  # no artifact, and that is fine
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_history_is_ordered_oldest_first(self, data_home: Path) -> None:
        """A finding's story (seen -> failed_gate -> duplicate) is the useful part;
        the latest status alone hides it."""
        _write_ledger(
            data_home,
            [
                {"fp": "f1", "kind": "bug", "target": "src/a.py::g", "status": "seen"},
                {"fp": "other", "kind": "bug", "target": "src/b.py::h", "status": "seen"},
                {"fp": "f1", "kind": "bug", "target": "src/a.py::g", "status": "failed_gate"},
                {"fp": "f1", "kind": "bug", "target": "src/a.py::g", "status": "duplicate"},
            ],
        )
        client = await _client()
        try:
            f = (await (await client.get(f"{PREFIX}/findings/f1")).json())["finding"]
            assert [h["status"] for h in f["history"]] == ["seen", "failed_gate", "duplicate"]
            assert f["status"] == "duplicate"  # latest wins for the headline
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_cr_field_is_exposed_as_pr(self, data_home: Path) -> None:
        """The spine's ledger field is historically ``cr``. The API must surface it
        as ``pr`` or the UI never renders a pull-request link."""
        _write_ledger(
            data_home,
            [
                {
                    "fp": "f2",
                    "kind": "bug",
                    "target": "src/c.py::i",
                    "status": "filed",
                    "cr": "https://github.com/o/r/pull/7",
                }
            ],
        )
        client = await _client()
        try:
            f = (await (await client.get(f"{PREFIX}/findings/f2")).json())["finding"]
            assert f["pr"] == "https://github.com/o/r/pull/7"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_candidate_gate_and_diff_are_joined(self, data_home: Path) -> None:
        """The evidence join: a candidate artifact whose cand_id embeds the target
        supplies the signature, the gate verdict, and the diff."""
        _write_ledger(
            data_home,
            [{"fp": "f3", "kind": "bug", "target": "src/search.py::negamax", "status": "filed"}],
        )
        cand_dir = data_home / "results" / "candidates"
        cand_dir.mkdir(parents=True, exist_ok=True)
        stem = "c1_wide_search_py_negamax_abc"
        (cand_dir / f"{stem}.json").write_text(
            json.dumps(
                {
                    "status": "kept",
                    "proposal": {
                        "cand_id": stem,
                        "candidate": {
                            "signature": "off-by-one at depth 64",
                            "hypothesis": "raises IndexError",
                            "reproducing_test": {"test_path": "test/test_bug.py"},
                        },
                    },
                    "bug_gate": {"passed": True, "red": True, "green": True, "staygreen": True},
                }
            ),
            encoding="utf-8",
        )
        (cand_dir / f"{stem}.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        client = await _client()
        try:
            f = (await (await client.get(f"{PREFIX}/findings/f3")).json())["finding"]
            assert f["candidate"]["signature"] == "off-by-one at depth 64"
            assert f["candidate"]["reproducing_test"]["test_path"] == "test/test_bug.py"
            assert f["gate"]["red"] is True and f["gate"]["staygreen"] is True
            assert "+++ b" in f["diff"]
            assert f["diffTruncated"] is False
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_oversized_diff_is_truncated_and_reported(self, data_home: Path) -> None:
        """An agent can write an arbitrarily large file, and this response renders
        in a browser — so the cap must apply and must be reported, never silent."""
        _write_ledger(
            data_home, [{"fp": "f4", "kind": "bug", "target": "src/big.py::b", "status": "filed"}]
        )
        q = data_home / "pr_queue"
        q.mkdir(parents=True, exist_ok=True)
        (q / "f4.diff").write_text("x" * (routes._MAX_DIFF_CHARS + 500), encoding="utf-8")
        client = await _client()
        try:
            f = (await (await client.get(f"{PREFIX}/findings/f4")).json())["finding"]
            assert len(f["diff"]) == routes._MAX_DIFF_CHARS
            assert f["diffTruncated"] is True
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_missing_fingerprint_is_400(self, data_home: Path) -> None:
        """An empty path segment must not be read as "any finding"."""
        client = await _client()
        try:
            res = await client.get(f"{PREFIX}/findings/")
            assert res.status in (400, 404)
        finally:
            await client.close()


class TestFindingsListDedup:
    """Regression: the list showed one row per LEDGER ENTRY, so a finding with a
    status history (seen -> failed_gate -> duplicate) appeared 3x with the same
    fingerprint. Duplicate React keys then made expanding one row toggle another's
    detail panel. The list must be one row per finding, its latest status."""

    async def _findings(self, data_home) -> list[dict]:
        client = await _client()
        try:
            res = await client.get(f"{PREFIX}/findings")
            return (await res.json())["findings"]
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_one_row_per_fingerprint_latest_status(self, data_home) -> None:
        _write_ledger(
            data_home,
            [
                {"fp": "aa", "kind": "bug", "target": "x", "status": "seen"},
                {"fp": "bb", "kind": "bug", "target": "y", "status": "seen"},
                {"fp": "aa", "kind": "bug", "target": "x", "status": "failed_gate"},
                {"fp": "aa", "kind": "bug", "target": "x", "status": "duplicate"},
            ],
        )
        rows = await self._findings(data_home)
        fps = [r["fp"] for r in rows]
        assert len(fps) == len(set(fps)), f"duplicate fingerprints in the list: {fps}"
        by_fp = {r["fp"]: r["status"] for r in rows}
        assert by_fp["aa"] == "duplicate"  # latest, not the original "seen"
        assert by_fp["bb"] == "seen"

    @pytest.mark.asyncio
    async def test_a_row_without_a_fingerprint_is_kept(self, data_home) -> None:
        """An fp-less row cannot be deduped; dropping it would hide a finding."""
        _write_ledger(
            data_home,
            [
                {"kind": "bug", "target": "x", "status": "seen"},
                {"fp": "aa", "kind": "bug", "target": "y", "status": "filed"},
            ],
        )
        rows = await self._findings(data_home)
        assert len(rows) == 2


class TestCommitButton:
    """The one-click commit action: apply a queued change to the branch, gated by
    the same protected-branch denylist the loop's direct-commit mode uses."""

    @pytest.mark.asyncio
    async def test_no_queued_change_is_refused(self, data_home) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod

        store.write_json_atomic(store.config_path(), {"clone": "/tmp/x", "branch": "feature/x"})
        out = await asyncio.to_thread(commit_mod.commit_finding, "nofp")
        assert out["ok"] is False and "no queued change" in str(out["error"])

    @pytest.mark.asyncio
    async def test_a_commit_while_a_run_is_live_is_refused_with_409(
        self, data_home, monkeypatch
    ) -> None:
        """One-click commit checks out the branch, applies the diff and pushes — and the
        loop's direct-commit mode does the same on the same branch from its worker thread.
        Running both at once interleaves two checkout/apply sequences in one clone. The
        handler must refuse while the supervisor is RUNNING / CALIBRATING / STOPPING.
        Raised by the GPT review of this branch."""
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import (
            runner,
        )

        class _Supervisor:
            def status(self) -> dict:
                return {"status": runner.STATUS_RUNNING}

        monkeypatch.setattr(runner, "get_supervisor", lambda: _Supervisor())

        called: list[str] = []

        def _record(fp: str) -> dict:
            called.append(fp)
            return {"ok": True}

        monkeypatch.setattr(commit_mod, "commit_finding", _record)

        client = await _client()
        try:
            res = await client.post(f"{PREFIX}/findings/anyfp/commit")
            assert res.status == 409, "a commit during a live run was not refused"
            body = await res.json()
            assert body["code"] == "run_in_progress"
        finally:
            await client.close()
        assert called == [], "commit_finding ran despite a live run — it touched the clone"

    @pytest.mark.asyncio
    async def test_a_draft_while_a_run_is_live_is_refused_with_409(
        self, data_home, monkeypatch
    ) -> None:
        """The draft route became clone-MUTATING and needs the same gate as commit.

        Materializing the queued diff runs `checkout -B` / `apply --index` (and
        `reset --hard` on a failed apply) in the SAME clone the driver's worker thread is
        mid-cycle on, so interleaving discards the loop's staged winner and then pushes
        whatever HEAD the interleaving left. The gap was introduced by the materialize fix
        itself. Raised by the Opus 5 review of this branch.
        """
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import runner

        class _Supervisor:
            def status(self) -> dict:
                return {"status": runner.STATUS_RUNNING}

        monkeypatch.setattr(runner, "get_supervisor", lambda: _Supervisor())

        touched: list[str] = []

        def _staged(**_kw) -> dict:
            touched.append("staged")
            return {"ok": True, "base": "b"}

        monkeypatch.setattr(commit_mod, "materialize_queued_diff", _staged)

        client = await _client()
        try:
            res = await client.post(f"{PREFIX}/draft-pr/anyfp")
            assert res.status == 409, "a draft during a live run was not refused"
            body = await res.json()
            assert body["code"] == "run_in_progress"
        finally:
            await client.close()
        assert touched == [], "the draft route mutated the clone despite a live run"

    def test_a_failed_draft_rolls_the_commit_back(self) -> None:
        """A draft that publishes nothing must not leave its commit on the branch.

        Committing the staged diff put the change on the configured branch, and
        `clone_setup.checkout_branch` prefers an existing local branch — so a failed draft
        (no `gh`, no network, a refused push) left the NEXT run starting from an unfiled
        commit and treating the queued change as already-landed baseline. Measured on a real
        bare repo: local `work` sat 1 commit ahead of a remote it was never pushed to.
        `commit_finding` already resets at each of its own failure points; this path had
        none. Raised by the GPT review of this branch.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        src = inspect.getsource(routes._handle_draft_pr)
        assert "_rollback" in src, "a failed draft strands its commit on the branch"
        # All three post-commit exits must roll back: a failed commit, a `draft()` that
        # degraded to "queued", and an unexpected raise.
        assert src.count("_rollback()") >= 3, "not every post-commit failure path rolls back"
        assert 'reset", "--hard"' in src or '"reset", "--hard"' in src

    @pytest.mark.asyncio
    async def test_retargeting_during_a_run_is_refused_with_409(
        self, data_home, monkeypatch
    ) -> None:
        """`PUT /config` mid-run would move the workspace the loop is writing to.

        `branch` is in `_CONFIG_WRITABLE` and `store.workspace_key()` reads config FRESH,
        keying on `target_url` + `branch` — so the ruler, results, PR queue and profiles all
        move to a different key while the run is still producing them. Measured: flipping
        `branch` from `origin/main` to `origin/feature` moved the key from `..._repoa__main`
        to `..._repoa__feature`. The calibrate path was already pinned to its LAUNCHED config
        for this reason; this closes the write side. Raised by the GPT review of this branch.
        """
        from kiro_crew.apps.builtins.auto_improvement.backend import runner, store

        class _Supervisor:
            def status(self) -> dict:
                return {"status": runner.STATUS_RUNNING}

        monkeypatch.setattr(runner, "get_supervisor", lambda: _Supervisor())
        before = store.read_json(store.config_path(), {}) or {}

        client = await _client()
        try:
            res = await client.put(f"{PREFIX}/config", json={"branch": "origin/other"})
            assert res.status == 409, "a config change during a live run was not refused"
            assert (await res.json())["code"] == "run_in_progress"

            res = await client.post(f"{PREFIX}/setup-clone", json={"url": "https://github.com/o/r"})
            assert res.status == 409, "a retarget during a live run was not refused"
            assert (await res.json())["code"] == "run_in_progress"
        finally:
            await client.close()

        assert (
            store.read_json(store.config_path(), {}) or {}
        ) == before, "config was mutated despite the refusal"

    @pytest.mark.asyncio
    async def test_a_successful_setup_persists_the_config(
        self, data_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SUCCESS path of the app's front door, end to end.

        Every existing test of this route drove a refusal (409 mid-run) or read the source
        with `inspect.getsource`, so nothing actually executed the persist. That let a
        closure-scoping bug through: when clone+persist were wrapped in an inner
        `_clone_and_persist` to hold the clone lock, that inner function bound its OWN local
        `result`, while `_persist` still read `result` as a free variable of the enclosing
        handler — a cell nothing ever filled. Every successful setup raised
        `NameError: free variable 'result' referenced before assignment`, surfacing as a 500
        with the clone on disk but `config.json` never written: the app could not be set up
        at all. Reproduced standalone before fixing. Raised by the Opus 5 review.

        `setup_safe_clone` is stubbed because the real one clones over the network; the bug
        is in the handler's own scoping, which this still exercises fully.
        """
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup, store

        clone_dir = data_home / "fake-clone"
        clone_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            clone_setup,
            "setup_safe_clone",
            lambda _url, _scratch: (
                {
                    "clone": str(clone_dir),
                    "display": "o/r",
                    "origin_url": "https://github.com/o/r",
                    "push_disabled": True,
                },
                "",
            ),
        )

        client = await _client()
        try:
            res = await client.post(f"{PREFIX}/setup-clone", json={"url": "https://github.com/o/r"})
            body = await res.json()
            assert res.status == 200, f"setup failed: {body}"
            assert body["ok"] is True
        finally:
            await client.close()

        # The persisted config is the whole point — a 200 with nothing written would leave
        # the app unusable in exactly the way the bug did.
        cfg = store.read_json(store.config_path(), {}) or {}
        assert cfg.get("clone") == str(clone_dir), f"clone not persisted: {cfg}"
        assert cfg.get("target_url") == "https://github.com/o/r"
        assert cfg.get("target_display") == "o/r"

    def test_every_clone_mutating_route_has_the_run_gate(self) -> None:
        """Structural: adding another clone-mutating handler must not silently skip it.

        This is the guard for the mistake that produced the finding above — the draft route
        gained clone mutation without gaining the gate, while every sibling already had it.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        # Also covers the two CONFIG handlers: `branch` is writable and `workspace_key()` reads
        # config fresh, so a mid-run edit moves the artifact set the loop is writing to.
        for name in (
            "_handle_commit",
            "_handle_draft_pr",
            "_handle_put_config",
            "_handle_setup_clone",
        ):
            src = inspect.getsource(getattr(routes, name))
            # The gate is now ONE shared helper (`_refuse_while_running`) rather than four
            # inline copies — a hand-rolled fourth copy is how the guarded status set drifts.
            # This test previously grepped for the inline `run_in_progress` literal, and the
            # refactor tripped it, which is the guard working.
            assert "_refuse_while_running(" in src, f"{name} can act mid-run"

        # And the helper itself must still consult the supervisor and refuse with 409.
        helper = inspect.getsource(routes._refuse_while_running)
        assert "get_supervisor" in helper and "run_in_progress" in helper
        assert "status=409" in helper

    @pytest.mark.asyncio
    async def test_a_commit_while_idle_is_allowed_through(self, data_home, monkeypatch) -> None:
        """The guard must not block the normal case: with no run active, the commit
        proceeds to `commit_finding`."""
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import (
            runner,
        )

        class _Idle:
            def status(self) -> dict:
                return {"status": runner.STATUS_DONE}

        monkeypatch.setattr(runner, "get_supervisor", lambda: _Idle())
        monkeypatch.setattr(commit_mod, "commit_finding", lambda fp: {"ok": True, "fp": fp})

        client = await _client()
        try:
            res = await client.post(f"{PREFIX}/findings/xyz/commit")
            assert res.status == 200, "an idle-state commit was wrongly blocked"
            assert (await res.json())["fp"] == "xyz"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_protected_branch_is_refused_before_touching_the_repo(self, data_home) -> None:
        """main must never be a commit target, even with a queued diff present."""
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod

        q = store.pr_queue_dir()
        (q / "ff.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (q / "ff.pr.md").write_text("# fix: thing\n", encoding="utf-8")
        store.write_json_atomic(store.config_path(), {"clone": "/tmp/x", "branch": "origin/main"})
        out = await asyncio.to_thread(commit_mod.commit_finding, "ff")
        assert out["ok"] is False
        err = str(out["error"]).lower()
        assert "push policy" in err or "protected" in err


class TestFingerprintIsValidatedAtTheBoundary:
    """Every ``{fp}`` handler interpolates the value into a filesystem path
    (``pr_queue/<fp>.diff``, per-repo ledger subtrees, watcher clone dirs), so an
    unvalidated ``fp`` is a path-traversal vector. Input-validation guidance:
    allowlist at the boundary, block traversal, fail closed. `validate_fingerprint`
    is the allowlist authority (no ``.``/``/``/``..``); the handlers now all call it
    through `_validated_fp`. Raised by the GPT review of this branch.
    """

    # The fp-bearing routes, with the HTTP method each uses.
    ROUTES = (
        ("GET", "findings/{fp}"),
        ("POST", "findings/{fp}/draft-pr"),
        ("POST", "findings/{fp}/commit"),
        ("POST", "findings/{fp}/forget"),
        ("POST", "findings/{fp}/purge"),
        ("POST", "findings/{fp}/watch"),
        ("GET", "watchers/{fp}/log"),
    )

    # Values that must never reach a path build. urllib keeps these in the path
    # segment rather than collapsing them, so they exercise the handler.
    EVIL = ("..", "..%2f..%2fetc", "a/../../b", "with space", "semi;colon", "dot.dot")

    @pytest.mark.asyncio
    async def test_a_traversal_fingerprint_is_rejected_before_any_path_use(
        self, data_home, monkeypatch
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import (
            ledger_admin,
            pr_watchers,
        )

        # Trip if any downstream sink is reached with an invalid fp — the point is that
        # the boundary rejects FIRST, so none of these should ever run.
        for mod, name in (
            (commit_mod, "commit_finding"),
            (ledger_admin, "forget"),
            (ledger_admin, "purge"),
        ):
            monkeypatch.setattr(
                mod, name, lambda *a, **k: pytest.fail(f"{name} reached with an invalid fp")
            )
        monkeypatch.setattr(
            pr_watchers.PRWatcherRegistry,
            "start",
            lambda *a, **k: pytest.fail("watcher start reached with an invalid fp"),
        )

        client = await _client()
        try:
            for method, tmpl in self.ROUTES:
                for evil in self.EVIL:
                    path = f"{PREFIX}/{tmpl.replace('{fp}', evil)}"
                    res = await client.request(method, path)
                    # Either the router does not match (404) or the handler rejects
                    # (400). What must NOT happen is a 200 or a 500 from a path build.
                    assert res.status in (400, 404), (
                        f"{method} {path} returned {res.status} — an invalid fp reached "
                        "the handler body"
                    )
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_valid_fingerprint_still_reaches_the_handler(self, data_home) -> None:
        """The allowlist must not reject legitimate fingerprints."""
        _write_ledger(
            data_home,
            [{"fp": "abc123", "kind": "bug", "target": "src/x.py::f", "status": "seen"}],
        )
        client = await _client()
        try:
            res = await client.get(f"{PREFIX}/findings/abc123")
            assert res.status == 200
            assert (await res.json())["finding"]["fp"] == "abc123"
        finally:
            await client.close()


class TestServedEvidenceIsRedacted:
    """A candidate diff is model-authored text rendered in the operator's browser, so
    reading it back is an egress boundary. Raised by CodeQL on this branch
    (`py/clear-text-storage-sensitive-data` on the write side); the read side is where
    a redaction pass can run without corrupting the diff the gate must still apply.
    """

    def test_a_credential_in_a_diff_does_not_reach_the_browser(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import (
            _redact_for_display,
        )

        leaked = "+AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n"
        out = _redact_for_display(leaked)
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in out

    def test_ordinary_diff_text_survives_intact(self) -> None:
        """The gate still has to be able to apply what we show."""
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import (
            _redact_for_display,
        )

        diff = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-return None\n+return 0\n"
        assert _redact_for_display(diff) == diff

    def test_redactor_failure_withholds_rather_than_leaks(self, monkeypatch) -> None:
        """Fail-CLOSED: if the scanner cannot run, serve nothing."""
        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        def _boom(_text):
            raise RuntimeError("scanner unavailable")

        # Patch the name ROUTES holds, not `kiro_crew.security.redact`: the import is at
        # module scope (per `top-level-imports`), so routes binds the function object at
        # import time and patching the source module would not affect it. This is exactly
        # the mock-target hazard the rule's own rationale mentions.
        monkeypatch.setattr(routes, "redact", _boom)
        out = routes._redact_for_display("+secret = 'hunter2'\n")
        assert "hunter2" not in out
        assert "withheld" in out


class TestAllAgentAuthoredFieldsAreRedacted:
    """Not just the diff. The candidate's signature/hypothesis/evidence are the MODEL's
    own prose about the defect, rendered in the operator's browser by FindingDetail — the
    same egress boundary the diff crosses, and the same class of text.

    Raised by review of this branch: the first pass redacted the diff and the PR body and
    missed these six fields, so a credential the discovery agent quoted while explaining
    the bug would still reach the dashboard.
    """

    def test_the_gate_tree_is_redacted_recursively(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _redact_tree

        gate = {
            "passed": False,
            "detail": "assert aws_access_key_id=AKIAIOSFODNN7EXAMPLE",
            "failing_tests": ["test_x", "aws_access_key_id=AKIAIOSFODNN7EXAMPLE"],
        }
        out = _redact_tree(gate)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(out)

    def test_non_strings_survive_as_themselves(self) -> None:
        """The UI renders gate flags as tri-state icons — a stringified True breaks that."""
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _redact_tree

        gate = {"passed": True, "red": False, "staygreen": None, "cycle": 3, "band": 1.5}
        assert _redact_tree(gate) == gate

    def test_a_credential_shaped_frame_name_is_scrubbed_from_a_profile_tree(self) -> None:
        """A profiler frame's function/module/file names come from the target repo's code, so a
        credential-shaped identifier there must not reach the browser. `_handle_profile`/
        `_handle_profiles` now wrap their payload in `_redact_tree`; this pins that the wrap
        actually scrubs a frame tree of the shape those routes return. Raised by the GPT review."""
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _redact_tree

        # A frame tree of the shape profile_normalize produces, with a credential-shaped symbol
        # name and file path (as could occur if the target repo named something that way).
        tree = {
            "profile": {
                "name": "aws_access_key_id=AKIAIOSFODNN7EXAMPLE",
                "file": "src/aws_access_key_id=AKIAIOSFODNN7EXAMPLE.py",
                "children": [
                    {"name": "helper", "file": "app.py", "self_ms": 1.2},
                    {"name": "AKIAIOSFODNN7EXAMPLE_worker", "file": "w.py", "self_ms": 0.3},
                ],
            }
        }
        out = _redact_tree(tree)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(out), (
            "a credential-shaped profiler frame name reached the browser unredacted"
        )
        # Numeric timing fields must survive so the flame/sunburst view still renders.
        assert out["profile"]["children"][0]["self_ms"] == 1.2

    def test_every_candidate_text_field_goes_through_the_redactor(self) -> None:
        """Structural: each agent-authored field must be wrapped, not just some."""
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "backend" / "routes.py").read_text(
            encoding="utf-8"
        )
        block = src.split('detail["candidate"] = {', 1)[1].split("}", 1)[0]
        for field in ("signature", "hypothesis", "evidence", "severity_note", "blast_radius"):
            line = next(ln for ln in block.splitlines() if f'"{field}"' in ln)
            assert "_redact_for_display" in line, f"{field} reaches the browser unredacted"


class TestEveryReaderOfRunEvidenceRedacts:
    """A SWEEP, not another point fix.

    Agent-authored run evidence (a finding's note/signature/hypothesis, the diff, the
    activity feed, MCP results) reaches five different readers, and this PR fixed them one
    at a time — each round treating that path as the last. Review caught the MCP surface;
    sweeping afterwards found a sixth (the findings LIST endpoint, which served raw ledger
    rows including `note`), and the GPT review then found a SEVENTH — the progress
    endpoint, whose per-point `description` is the candidate's prose, served with no
    redaction at all. The finding-detail handler was also serving a `run` provenance
    block and a gate tree outside its per-field redaction, so it now redacts the whole
    payload like the list endpoint. This test enumerates the readers so an EIGHTH cannot
    be added without a deliberate decision.
    """

    #: (module, symbol that must apply redaction). Kept as data so adding a reader means
    #: adding a line here — which is the point.
    READERS = (
        ("backend/routes.py", "_handle_findings", "_redact_tree"),
        ("backend/routes.py", "_handle_finding_detail", "_redact_tree"),
        ("backend/routes.py", "_handle_progress", "_redact_tree"),
        # The profiler-frame endpoints: a frame's function/module/file names come from the
        # target repo's code, so a credential-shaped identifier there reached the browser raw.
        # Found by the GPT review; both wrapped in `_redact_tree` now.
        ("backend/routes.py", "_handle_profile", "_redact_tree"),
        ("backend/routes.py", "_handle_profiles", "_redact_tree"),
        ("backend/runner.py", "_on_agent_activity", "_redact_activity"),
        ("backend/mcp_server.py", "handle", "_redact_result"),
        ("backend/pr_watchers.py", "_log", "_redact"),
    )

    def test_each_known_reader_applies_a_redactor(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for rel, func, redactor in self.READERS:
            src = (root / rel).read_text(encoding="utf-8")
            # The function must exist AND the redactor must appear inside it.
            marker = f"def {func}("
            assert marker in src, f"{rel}::{func} no longer exists — update this sweep"
            body = src.split(marker, 1)[1]
            # Bound the search to this function: stop at the next top-level/def boundary.
            nxt = body.find("\ndef ")
            nxt2 = body.find("\nasync def ")
            cut = min(x for x in (nxt, nxt2, len(body)) if x > 0)
            # Require the CALL form (`redactor(`), not a bare mention — a docstring or comment
            # that merely names the redactor must not satisfy this guard. (A reverted wrap that
            # left an explanatory comment behind slipped past the bare-substring check once.)
            assert f"{redactor}(" in body[:cut], (
                f"{rel}::{func} serves evidence without calling {redactor}"
            )

    def test_the_progress_series_redacts_a_credential_in_a_description(self) -> None:
        """The seventh reader, found by the GPT review. A progress point's `description`
        is the candidate's own prose, served verbatim into the dashboard chart tooltip."""
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _redact_tree

        series = {
            "points": [
                {
                    "candId": "c1",
                    "description": "sped up via aws_access_key_id=AKIAIOSFODNN7EXAMPLE",
                    "bestSoFar": 90.0,
                    "kept": True,
                }
            ],
            "primary": {"direction": "minimize"},
        }
        out = _redact_tree(series)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(out)
        # Numbers and booleans the chart plots must survive unchanged.
        assert out["points"][0]["bestSoFar"] == 90.0
        assert out["points"][0]["kept"] is True

    def test_finding_detail_redacts_a_block_outside_its_per_field_list(self) -> None:
        """The detail handler redacted enumerated candidate fields but assembled other
        blocks (the `run` provenance meta, the gate tree) separately. Wrapping the whole
        payload means a field added later cannot leak by being forgotten."""
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _redact_tree

        detail = {
            "fp": "abc",
            "signature": "clean",
            # A block NOT in the per-field redaction list — run provenance.
            "run": {"note": "token was aws_access_key_id=AKIAIOSFODNN7EXAMPLE"},
        }
        out = _redact_tree(detail)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(out)
        assert out["fp"] == "abc"

    def test_the_findings_list_redacts_a_credential_in_a_note(self) -> None:
        """The sixth reader, found by the sweep rather than by review."""
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _redact_tree

        rows = [{"fp": "abc", "note": "aws_access_key_id=AKIAIOSFODNN7EXAMPLE", "passed": True}]
        out = _redact_tree(rows)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(out)
        # Gate flags must survive as booleans — the UI renders them as tri-state icons.
        assert out[0]["passed"] is True
