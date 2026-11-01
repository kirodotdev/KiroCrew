from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from kiro_byo.workspace_manager import WorkspaceManager


class WorkspaceManagerTests(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)

    def test_git_worktree_writer_is_isolated_and_diffable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            self._git(repo, "init")
            (repo / "a.txt").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", "a.txt")
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base"],
                check=True, capture_output=True, text=True,
            )
            manager = WorkspaceManager(root / "runtime")
            lease = manager.provision(cwd=str(repo), task_id="writer-1")
            worktree = Path(lease.path)
            self.assertNotEqual(worktree.resolve(), repo.resolve())
            (worktree / "a.txt").write_text("candidate\n", encoding="utf-8")
            self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "base\n")
            diff = manager.diff(lease.workspace_id)
            self.assertIn("candidate", diff)
            compared = manager.compare([lease.workspace_id])
            self.assertEqual(compared[0]["changed_files"], 1)
            check = manager.run_check(lease.workspace_id, ["git", "status", "--short"])
            self.assertEqual(check["returncode"], 0)
            applied = manager.apply_candidate(lease.workspace_id)
            self.assertTrue(applied["applied"])
            self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "candidate\n")
            self.assertTrue(manager.remove(lease.workspace_id, force=True))
            self.assertFalse(worktree.exists())


if __name__ == "__main__":
    unittest.main()
