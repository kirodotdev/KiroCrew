"""The interactive read-only tier's proof for kiro-cli's file searches.

kiro-cli asks for a ``grep`` / ``glob`` / ``code`` call only when it reaches outside
the session's folder, and its permission request carries no ``kind`` at all: the gate
sees the host-stamped tool name, the trusted-identity flag and the arguments. These
tests drive ``on_tool_call`` with exactly that shape. A search that only reads, rooted
at an absolute path that holds no sensitive location, is approved after every deny
tier; anything else keeps the prompt it had.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import hooks as hooks_mod
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig


def _judge(tool: str, raw: dict, *, trusted: bool = True, kind: str = "", **extra):
    """One kiro-cli permission request for built-in *tool*, as the gate receives it."""
    return HookManager(extra.pop("config", None) or HooksConfig()).on_tool_call(
        "Searching",
        tool_kind=kind,
        raw_params=raw,
        mcp_tool_name=tool,
        mcp_identity_trusted=trusted,
        **extra,
    )


@pytest.fixture
def root(tmp_path):
    """An absolute folder outside the session's, holding nothing sensitive."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    return str(folder)


class TestSearchesThatOnlyReadAreApproved:
    @pytest.mark.parametrize(
        ("tool", "raw"),
        [
            ("grep", {"pattern": "TODO"}),
            ("glob", {"pattern": "**/*.py"}),
            ("code", {"operation": "search_symbols", "symbol_name": "main"}),
            ("code", {"operation": "lookup_symbols", "symbols": ["main"]}),
            ("code", {"operation": "pattern_search", "pattern": "f($A)", "language": "python"}),
        ],
    )
    def test_a_trusted_search_outside_the_folder_runs_without_a_prompt(self, tool, raw, root):
        result = _judge(tool, {**raw, "path": root})
        assert (result.action, result.read_only) == (TOOL_AUTO_APPROVE, True)

    @pytest.mark.parametrize("kind", ["search", "read", "SEARCH"])
    def test_the_kind_the_tool_call_frame_stated_is_accepted(self, kind, root):
        assert _judge("grep", {"pattern": "x", "path": root}, kind=kind).action == (
            TOOL_AUTO_APPROVE
        )

    def test_a_document_lookup_on_one_file_is_approved(self, root):
        result = _judge(
            "code", {"operation": "get_document_symbols", "file_path": os.path.join(root, "a.py")}
        )
        assert result.action == TOOL_AUTO_APPROVE


class TestEverythingElseKeepsItsPrompt:
    def test_an_untrusted_identity_proves_nothing(self, root):
        assert _judge("grep", {"pattern": "x", "path": root}, trusted=False).action == TOOL_ALLOW

    def test_a_server_tool_named_like_the_builtin_proves_nothing(self, root):
        result = _judge("grep", {"pattern": "x", "path": root}, mcp_server_name="files")
        assert result.action == TOOL_ALLOW

    @pytest.mark.parametrize("kind", ["edit", "execute", "delete", "other", "move"])
    def test_a_kind_naming_anything_else_refuses_the_proof(self, kind, root):
        assert _judge("grep", {"pattern": "x", "path": root}, kind=kind).action == TOOL_ALLOW

    @pytest.mark.parametrize(
        "operation",
        [
            "pattern_rewrite",
            "rename_symbol",
            "format",
            "apply_code_action",
            "initialize_workspace",
            "find_references",
            "goto_definition",
            "get_diagnostics",
            "",
        ],
    )
    def test_a_code_operation_that_writes_or_starts_a_server_asks(self, operation, root):
        raw = {"operation": operation, "path": root, "file_path": os.path.join(root, "a.py")}
        # kiro-cli's permission request for it carries no kind, as for every call.
        assert _judge("code", raw).action == TOOL_ALLOW

    @pytest.mark.parametrize("path", ["rel/dir", "./x", "../x", "$HOME/x"])
    def test_a_path_the_gateway_would_resolve_differently_asks(self, path):
        assert _judge("grep", {"pattern": "x", "path": path}).action == TOOL_ALLOW

    def test_a_root_spelled_with_a_variable_asks(self, root, monkeypatch):
        # The sensitive-path matcher expands ``$VAR``; kiro-cli walks the literal.
        monkeypatch.setenv("KC_SEARCH_ROOT_PROBE", "elsewhere")
        literal = os.path.join(os.path.dirname(root), "$KC_SEARCH_ROOT_PROBE")
        assert _judge("grep", {"pattern": "x", "path": literal}).action == TOOL_ALLOW

    @pytest.mark.parametrize(
        ("path", "anchored"),
        [
            ("C:\\Users\\u\\proj", True),
            ("\\\\server\\share\\proj", True),
            ("\\Users\\u\\proj", False),
            ("proj\\src", False),
        ],
    )
    def test_a_windows_root_needs_a_drive(self, path, anchored):
        import ntpath

        assert hooks_mod._search_root_anchored(path, ntpath) is anchored

    def test_a_search_naming_no_path_asks(self):
        assert _judge("grep", {"pattern": "x"}).action == TOOL_ALLOW

    def test_a_search_over_a_folder_that_holds_a_credential_store_asks(self):
        # The home folder is not itself sensitive, so the keystone lets it pass,
        # but a search walks ``~/.ssh`` below it.
        for path in ("~", os.path.expanduser("~"), "/"):
            assert _judge("grep", {"pattern": "BEGIN", "path": path}).action == TOOL_ALLOW

    def test_a_search_whose_containment_check_stalls_asks(self, root, monkeypatch):
        monkeypatch.setattr(hooks_mod.security, "path_contains_sensitive", lambda _p: True)
        assert _judge("glob", {"pattern": "*", "path": root}).action == TOOL_ALLOW

    @pytest.mark.parametrize("pattern", ["../*", "a/../../b", "/etc/*", "~/.ssh/*", "..\\x"])
    def test_a_glob_pattern_that_leaves_its_root_asks(self, pattern, root):
        assert _judge("glob", {"pattern": pattern, "path": root}).action == TOOL_ALLOW

    def test_a_second_path_spelling_must_pass_too(self, root):
        raw = {"pattern": "x", "path": root, "filePath": "rel"}
        assert _judge("grep", raw).action == TOOL_ALLOW

    def test_an_operator_confirm_pattern_still_asks(self, root):
        config = HooksConfig(confirm_tools=["grep"])
        assert _judge("grep", {"pattern": "x", "path": root}, config=config).action == TOOL_ALLOW

    def test_the_sensitive_path_keystone_still_denies_first(self):
        result = _judge("grep", {"pattern": "x", "path": "~/.ssh"})
        assert result.action == TOOL_DENY


class TestTheSetsStayNarrow:
    def test_host_search_builtins_are_host_read_only(self):
        assert hooks_mod._HOST_SEARCH_BUILTIN_TOOLS == frozenset({"grep", "glob"})
        assert hooks_mod._HOST_SEARCH_BUILTIN_TOOLS <= hooks_mod._HOST_READ_ONLY_BUILTIN_TOOLS

    def test_code_stays_out_of_the_host_read_only_builtins(self):
        assert "code" not in hooks_mod._HOST_READ_ONLY_BUILTIN_TOOLS

    def test_the_code_operations_are_the_four_tree_sitter_reads(self):
        assert hooks_mod._CODE_READ_ONLY_OPERATIONS == frozenset(
            {"get_document_symbols", "lookup_symbols", "pattern_search", "search_symbols"}
        )

    @pytest.mark.parametrize("tool", ["fs_read", "web_fetch", "web_search", "read", "shell"])
    def test_no_other_builtin_gains_the_proof(self, tool, root):
        raw = {"path": root, "url": "https://example.com", "command": "ls"}
        assert not hooks_mod._is_host_search_builtin_call(tool, "", raw, mcp_identity_trusted=True)
