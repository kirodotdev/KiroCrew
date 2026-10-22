"""Backend-route tests: learned-pattern consolidation.

``consolidate_apply`` is the one write of worker-authored merge output into
learned-patterns.md: that content reaches the file redacted, and output with no pattern in it
is refused. A namespace cannot be deleted while a consolidation holds it, so a late apply
cannot bring a deleted namespace back.
"""

import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from sage_lib import learning

from kiro_crew.apps.builtins.code_review_sage.tests.backend_routes_helpers import (
    _load_routes_module,
)
from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import OwnerRequest


class TestConsolidateRedactsMergedContent:
    """``consolidate_apply`` is the persistence chokepoint for merge output.

    The merged markdown comes from the merge WORKER, which has shell and file
    tools and has read the reviewed diffs. learned-patterns.md is rendered in the
    dashboard and injected into every later review prompt, so a credential that
    survives this write is exposed twice over. The caller hardens the read PATH;
    only this guard covers the CONTENT.
    """

    def test_credential_in_merged_markdown_is_scrubbed_before_the_write(self, tmp_path):

        secret = "ghp_" + "A" * 36
        # Real pattern markdown (a "### " heading), so the pattern-shape
        # guard admits it and redaction is what this test exercises.
        merged = "### Never hardcode a credential\n" f"The reviewed diff hardcoded one: {secret}\n"
        out = learning.consolidate_apply(merged, tmp_path, None)
        assert out["ok"], out
        written = Path(out["path"]).read_text(encoding="utf-8")
        assert secret not in written, "worker-authored credential persisted verbatim"
        assert (
            "Never hardcode a credential" in written
        ), "redaction must not eat the legitimate content"


class TestConsolidateRejectsPatternlessOutput:
    """Non-empty is not the same as usable.

    The merge worker is an LLM, so it can reply in prose instead of the pattern
    format. The emptiness check passes that through, and the write would then
    replace EVERY pattern with commentary and clear the candidate file in the same
    call — losing the staged learnings with nothing to show for them.
    """

    def _seeded(self, tmp_path):
        first = learning.consolidate_apply(
            "### Keep the guard\nReset it on every exit path.\n", tmp_path, None
        )
        assert first["ok"], first
        return learning, Path(first["path"])

    def test_prose_is_refused_and_the_ruleset_survives(self, tmp_path):
        learning, path = self._seeded(tmp_path)
        before = path.read_text(encoding="utf-8")

        out = learning.consolidate_apply(
            "I reviewed the candidates and found nothing worth merging.\n", tmp_path, None
        )

        assert out["ok"] is False
        assert "no recognizable patterns" in out["error"]
        assert path.read_text(encoding="utf-8") == before, "the ruleset was overwritten"

    def test_a_real_pattern_is_still_accepted(self, tmp_path):
        # The guard must not refuse legitimate merges.
        learning, path = self._seeded(tmp_path)
        out = learning.consolidate_apply(
            "### Authorize by confirming the owner\nDo not reject known-bad only.\n", tmp_path, None
        )
        assert out["ok"], out
        assert "Authorize by confirming the owner" in path.read_text(encoding="utf-8")


class TestConsolidationCannotResurrectADeletedNamespace(unittest.IsolatedAsyncioTestCase):
    """Deleting a namespace mid-consolidation must not bring it back.

    A consolidation captures its namespace BY NAME before dispatch, so the delete
    handler's active-list prune does nothing for it -- that prune only stops
    in-flight *reviews*, which write to whatever is currently active. Every
    learning writer mkdirs its parents, so a merge applying after the delete
    recreated the directory and ruleset after the caller was told the delete
    succeeded.

    The fix is the `_CONSOLIDATING` claim: it is added in the handler before its
    first await and released only in the worker's `finally`, so it brackets the
    worker's whole lifetime, and DELETE is refused for that entire span. These
    tests pin the hazard, the refusal, and the claim-lifetime property the refusal
    depends on -- because the guard is only sound while that property holds.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.mod = _load_routes_module()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    _MD = (
        "### A learned rule <!-- scope:common --> <!-- impact:high -->"
        " <!-- added:2026-01-01T00:00:00Z -->\nguidance here\n"
    )

    def test_a_late_apply_would_resurrect_a_deleted_namespace(self):
        """The hazard, so the claim is not mistaken for ceremony.

        If applying to a deleted namespace ever stops recreating it, this fails and
        the guard should be re-justified rather than kept out of habit.
        """
        L = self.mod.learning
        L.create_namespace("doomed")
        L.delete_namespace("doomed")
        self.assertNotIn("doomed", L.list_namespaces())
        L.consolidate_apply(self._MD, None, "doomed", [])
        self.assertIn("doomed", L.list_namespaces())

    async def test_refused_delete_does_not_deactivate_the_namespace(self):
        """A refused delete must not leave the namespace pruned from the active list:
        reviews would silently stop using one the caller was told still exists."""
        L, mod = self.mod.learning, self.mod
        L.create_namespace("keepme")
        mod._write_review_section({"active_namespaces": ["default", "keepme"]})

        # Refuse at the learning layer, the way a missing directory or an out-of-tree
        # path does -- none of those refusals depend on the active list, so pruning
        # first would deactivate a namespace that then fails to delete.
        with unittest.mock.patch.object(
            L, "delete_namespace", return_value={"ok": False, "error": "does not exist"}
        ):
            resp = await self._delete("keepme")

        self.assertEqual(resp.status, 400)
        self.assertIn(
            "keepme",
            mod._load_review_section().get("active_namespaces") or [],
            "a refused delete must not prune the active list",
        )

    async def test_delete_is_refused_while_a_consolidation_is_claimed(self):
        """Refuse with 409, and leave no partial side effect behind."""
        L, mod = self.mod.learning, self.mod
        L.create_namespace("busy")
        before = sorted(L.list_namespaces())
        active_before = list(mod._load_review_section().get("active_namespaces") or [])

        mod._CONSOLIDATING.add("busy")
        try:
            resp = await self._delete("busy")
        finally:
            mod._CONSOLIDATING.discard("busy")

        self.assertEqual(resp.status, 409)
        self.assertIn("consolidation_in_progress", resp.text)
        self.assertEqual(
            sorted(L.list_namespaces()), before, "a refused delete must not remove the namespace"
        )
        # And it must not have pruned the active list on its way out.
        self.assertEqual(
            list(mod._load_review_section().get("active_namespaces") or []), active_before
        )

    async def test_delete_still_works_when_nothing_is_consolidating(self):
        """The guard must not wedge ordinary deletes shut."""
        L = self.mod.learning
        L.create_namespace("spare")
        resp = await self._delete("spare")
        self.assertEqual(resp.status, 200)
        self.assertNotIn("spare", L.list_namespaces())

    def test_the_claim_brackets_the_whole_worker_lifetime(self):
        """Structural: the property the 409 relies on.

        The claim must be taken in the handler BEFORE the task is created, and
        given back in the worker's `finally`. If a later edit moves the add after
        `create_task`, or releases it in the handler instead, the refusal above
        stops covering the window it exists to cover.
        """
        with open(
            Path(self.mod.__file__).with_name("routes_parts") / "namespaces.py", encoding="utf-8"
        ) as fh:
            src = fh.read()

        handler = src.split("async def _handle_consolidate", 1)
        if len(handler) == 1:  # tolerate a rename of the endpoint
            handler = src.split("_CONSOLIDATING.add(", 1)
        tail = handler[1]
        add_at = tail.find("_CONSOLIDATING.add(")
        task_at = tail.find("create_task(_consolidate_bg(")
        self.assertNotEqual(add_at, -1)
        self.assertNotEqual(task_at, -1)
        self.assertLess(add_at, task_at, "the claim must be held before the worker is dispatched")

        worker = src.split("async def _consolidate_bg", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("finally:", worker)
        self.assertIn(
            "_CONSOLIDATING.discard(",
            worker.split("finally:", 1)[1],
            "the worker must release the claim on every terminal path",
        )

    def test_a_refused_delete_cannot_have_already_pruned(self):
        """The defect class: rejection must precede the side effect.

        An earlier shape of this guard checked the claim a SECOND time AFTER the
        active-list prune. That closed the race window but introduced a worse
        outcome -- a 409 returned with the namespace already pruned from
        `active_namespaces`, so the request refused to delete the namespace while
        silently deactivating it. The prune and the rmtree now happen in one
        offloaded helper under `_NS_OPS_LOCK`, so no 409 can be returned after a
        prune has been applied.
        """
        with open(
            Path(self.mod.__file__).with_name("routes_parts") / "namespaces.py", encoding="utf-8"
        ) as fh:
            src = fh.read()
        handler = src.split("async def _handle_namespaces", 1)[1]
        body = handler.split("\nasync def ", 1)[0]
        delete_arm = body.split('if request.method == "DELETE":', 1)[1]

        # Both sides must serialise on the same lock.
        self.assertIn("_NS_OPS_LOCK", delete_arm)
        consolidate = src.split("async def _handle_consolidate", 1)[1]
        self.assertIn("_NS_OPS_LOCK", consolidate.split("\nasync def ", 1)[0])

        # No 409 may appear after the active-list write in the DELETE arm.
        write_at = delete_arm.find("_write_review_section")
        self.assertNotEqual(write_at, -1)
        self.assertNotIn(
            "409",
            delete_arm[write_at:],
            "a delete must not be refused after pruning the active list",
        )

    async def _delete(self, ns):
        class _DeleteReq(OwnerRequest):
            method = "DELETE"
            query: dict = {}
            match_info: dict = {}

            async def json(self):
                return {"name": ns}

        return await self.mod._handle_namespaces(_DeleteReq())
