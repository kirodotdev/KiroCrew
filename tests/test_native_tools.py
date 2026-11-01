from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from kiro_byo.native_tools import NativeToolRuntime, ToolValidationError
from kiro_byo.sandbox import SandboxError


class NativeToolRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "alpha\nneedle = 1\nomega\n", encoding="utf-8"
        )
        (self.root / "README.md").write_text("hello\nworld\n", encoding="utf-8")
        self.runtime = NativeToolRuntime(self.root)

    def tearDown(self) -> None:
        self.runtime.close()
        self.tmp.cleanup()

    def call(self, name: str, args: dict):
        return self.runtime.execute(self.runtime.validate(name, args))

    def test_read_supports_line_windows(self) -> None:
        result = self.call("read", {"path": "README.md", "offset": 2, "limit": 1})
        self.assertIn("2: world", result.text)
        self.assertEqual(result.touched_paths, ("README.md",))

    def test_parent_escape_is_denied_even_after_schema_validation(self) -> None:
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        invocation = self.runtime.validate("read", {"path": "../secret.txt"})
        with self.assertRaises(SandboxError):
            self.runtime.execute(invocation)

    def test_sensitive_files_are_hard_denied_but_samples_are_readable(self) -> None:
        (self.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (self.root / ".env.example").write_text("TOKEN=example\n", encoding="utf-8")
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_ed25519").write_text("secret-key", encoding="utf-8")
        with self.assertRaises(SandboxError):
            self.call("read", {"path": ".env"})
        with self.assertRaises(SandboxError):
            self.call("read", {"path": ".ssh"})
        result = self.call("read", {"path": ".env.example"})
        self.assertIn("TOKEN=example", result.text)

    def test_provider_config_and_secret_named_json_are_hard_denied(self) -> None:
        config = self.root / "config"
        config.mkdir()
        (config / "providers.json").write_text('{"api_key":"super-secret"}', encoding="utf-8")
        (config / "providers.json.bak").write_text('{"api_key":"old-secret"}', encoding="utf-8")
        (config / "providers.example.json").write_text('{"api_key":"YOUR_KEY"}', encoding="utf-8")
        (self.root / "client-secret.json").write_text('{"token":"secret"}', encoding="utf-8")

        with self.assertRaises(SandboxError):
            self.call("read", {"path": "config/providers.json"})
        with self.assertRaises(SandboxError):
            self.call("read", {"path": "config/providers.json.bak"})
        with self.assertRaises(SandboxError):
            self.call("read", {"path": "client-secret.json"})
        example = self.call("read", {"path": "config/providers.example.json"})
        self.assertIn("YOUR_KEY", example.text)

    def test_symlink_escape_is_not_followed(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        with self.assertRaises(SandboxError):
            self.call("read", {"path": "escape/secret.txt"})
        globbed = self.call("glob", {"pattern": "**/*"})
        self.assertNotIn("escape", globbed.text)

    def test_glob_returns_files_and_directories_and_prunes_gitignore(self) -> None:
        ignored = self.root / "build"
        ignored.mkdir()
        (ignored / "artifact.py").write_text("needle\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("build/\n", encoding="utf-8")
        # Re-create runtime so it observes the new .gitignore.
        self.runtime = NativeToolRuntime(self.root)
        result = self.call("glob", {"pattern": "**/*", "limit": 100})
        self.assertIn("src/", result.text)
        self.assertIn("src/app.py", result.text)
        self.assertNotIn("build", result.text)

    def test_grep_is_regex_based_and_respects_file_glob(self) -> None:
        result = self.call(
            "grep",
            {"pattern": "NEEDLE", "glob": "**/*.py", "case_sensitive": False},
        )
        self.assertIn("src/app.py:2: needle = 1", result.text)
        self.assertNotIn("README.md", result.text)



    def test_read_files_batches_known_files(self) -> None:
        result = self.call(
            "read_files",
            {"paths": ["README.md", "src/app.py"], "limit": 2},
        )
        self.assertIn("===== README.md =====", result.text)
        self.assertIn("1: hello", result.text)
        self.assertIn("===== src/app.py =====", result.text)
        self.assertEqual(result.touched_paths, ("README.md", "src/app.py"))

    def test_list_directory_is_distinct_from_file_read(self) -> None:
        result = self.call("list_directory", {"path": ".", "limit": 20})
        self.assertIn("src/", result.text)
        self.assertIn("README.md", result.text)
        self.assertNotIn("hello", result.text)

    def test_delete_file_removes_only_regular_files(self) -> None:
        target = self.root / "trash.txt"
        target.write_text("remove me", encoding="utf-8")
        result = self.call("delete_file", {"path": "trash.txt"})
        self.assertFalse(target.exists())
        self.assertIn("Deleted trash.txt", result.text)
        with self.assertRaises(Exception):
            self.call("delete_file", {"path": "src"})

    def test_write_and_unified_diff_edit_are_atomic_and_available(self) -> None:
        written = self.call("write", {"path": "notes.txt", "content": "alpha\nbeta\n"})
        self.assertIn("notes.txt", written.touched_paths)
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"), "alpha\nbeta\n")
        patched = self.call(
            "edit",
            {
                "path": "notes.txt",
                "patch": "@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma\n",
            },
        )
        self.assertIn("notes.txt", patched.touched_paths)
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"), "alpha\ngamma\n")

    def test_allowed_root_permits_canonical_parent_access(self) -> None:
        outside = Path(self.tmp.name) / "shared"
        outside.mkdir()
        target = outside / "visible.txt"
        target.write_text("allowed\n", encoding="utf-8")
        old = os.environ.get("KIRO_BYO_ALLOWED_ROOTS")
        os.environ["KIRO_BYO_ALLOWED_ROOTS"] = str(outside)
        try:
            runtime = NativeToolRuntime(self.root)
            result = runtime.execute(runtime.validate("read", {"path": str(target)}))
            self.assertIn("allowed", result.text)
        finally:
            if old is None:
                os.environ.pop("KIRO_BYO_ALLOWED_ROOTS", None)
            else:
                os.environ["KIRO_BYO_ALLOWED_ROOTS"] = old

    def test_user_approved_scope_root_can_be_added_and_revoked(self) -> None:
        outside = Path(self.tmp.name) / "approved-project"
        outside.mkdir()
        target = outside / "README.md"
        target.write_text("approved\n", encoding="utf-8")
        invocation = self.runtime.validate("read", {"path": str(target)})
        roots = self.runtime.required_scope_roots(invocation)
        self.assertEqual(roots, (outside.resolve(),))
        added = self.runtime.grant_scope_roots(roots)
        self.assertEqual(added, (outside.resolve(),))
        self.runtime.preflight(invocation)
        self.assertIn("approved", self.runtime.execute(invocation).text)
        self.runtime.revoke_scope_roots(added)
        with self.assertRaises(SandboxError):
            self.runtime.preflight(invocation)

    def test_sensitive_external_path_never_becomes_permission_scope(self) -> None:
        outside = Path(self.tmp.name) / ".ssh"
        outside.mkdir()
        target = outside / "id_ed25519"
        target.write_text("secret", encoding="utf-8")
        invocation = self.runtime.validate("read", {"path": str(target)})
        with self.assertRaises(SandboxError):
            self.runtime.required_scope_roots(invocation)

    def test_read_only_policy_disables_mutating_tools_without_changing_code(self) -> None:
        policy_home = Path(self.tmp.name) / "policy-home"
        (policy_home / "config").mkdir(parents=True)
        (policy_home / "config" / "native_tools.json").write_text(
            json.dumps({"read_only": True}), encoding="utf-8"
        )
        old = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = str(policy_home)
        try:
            runtime = NativeToolRuntime(self.root)
            names = [spec.name for spec in runtime.specs()]
            expected = ["read", "glob", "grep", "read_files", "list_directory", "code_symbols"]
            if "code_query" in names:
                expected.append("code_query")
            expected += ["web_fetch", "web_search", "update_plan"]
            self.assertEqual(names, expected)
            with self.assertRaises(ToolValidationError):
                runtime.validate("write", {"path": "x.txt", "content": "x"})
        finally:
            if old is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old

    def test_web_tools_can_be_disabled_by_hard_policy(self) -> None:
        policy_home = Path(self.tmp.name) / "web-policy-home"
        (policy_home / "config").mkdir(parents=True)
        (policy_home / "config" / "native_tools.json").write_text(
            json.dumps({"web_enabled": False}), encoding="utf-8"
        )
        old = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = str(policy_home)
        try:
            runtime = NativeToolRuntime(self.root)
            names = [spec.name for spec in runtime.specs()]
            self.assertNotIn("web_fetch", names)
            self.assertNotIn("web_search", names)
            with self.assertRaises(ToolValidationError):
                runtime.validate("web_fetch", {"url": "https://example.com"})
        finally:
            if old is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old

    def test_shell_runs_with_cwd_and_returns_exit_code(self) -> None:
        command = "Write-Output kiro-shell" if os.name == "nt" else "printf kiro-shell"
        result = self.call("shell", {"command": command, "cwd": ".", "timeout_seconds": 5})
        self.assertEqual(result.exit_code, 0)
        self.assertIn("kiro-shell", result.text)

    def test_shell_yields_quickly_to_user_steer(self) -> None:
        command = "Start-Sleep -Seconds 5" if os.name == "nt" else "sleep 5"
        invocation = self.runtime.validate(
            "shell", {"command": command, "cwd": ".", "timeout_seconds": 10}
        )
        steer = threading.Event()

        def trigger() -> None:
            time.sleep(0.15)
            steer.set()

        thread = threading.Thread(target=trigger)
        thread.start()
        started = time.monotonic()
        result = self.runtime.execute(
            invocation,
            interrupt_probe=lambda: "steered" if steer.is_set() else "",
        )
        elapsed = time.monotonic() - started
        thread.join(1)

        self.assertLess(elapsed, 2.0)
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(result.interruption, "steered")
        self.assertIn("interrupted by user steering", result.text)

    def test_glob_pattern_cannot_be_absolute_or_parent_relative(self) -> None:
        with self.assertRaises(ToolValidationError):
            self.runtime.validate("glob", {"pattern": "../*.py"})
        with self.assertRaises(ToolValidationError):
            self.runtime.validate("grep", {"pattern": "x", "glob": "C:\\**\\*.txt"})


if __name__ == "__main__":
    unittest.main()


def test_native_tool_contracts_live_outside_executor_module() -> None:
    from kiro_byo import native_specs
    from kiro_byo.native_tools import NATIVE_TOOL_SPECS as runtime_specs

    assert runtime_specs is native_specs.NATIVE_TOOL_SPECS
    assert [spec.name for spec in runtime_specs] == [
        "read", "glob", "grep", "read_files", "list_directory",
        "write", "edit", "delete_file", "shell", "execute_shell_process", "control_shell_process",
        "code_symbols", "code_query", "web_fetch", "web_search", "update_plan"
    ]


def test_web_fetch_preflight_normalizes_web_policy_error() -> None:
    from unittest.mock import patch
    from kiro_byo.native_tools import NativeToolError
    from kiro_byo.web_tools import WebToolError
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        runtime = NativeToolRuntime(Path(td))
        invocation = runtime.validate("web_fetch", {"url": "https://blocked.example/"})
        try:
            with patch.object(runtime._web, "_assert_public_url", side_effect=WebToolError("blocked test host")):
                with unittest.TestCase().assertRaisesRegex(NativeToolError, "blocked test host"):
                    runtime.preflight(invocation)
        finally:
            runtime.close()
