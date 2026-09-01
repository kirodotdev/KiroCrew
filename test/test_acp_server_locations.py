"""Unit tests for ``kiro_crew.acp_server.locations.extract_tool_locations``."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.acp_server.locations import extract_tool_locations


class TestKiroCliFileTools:
    """kiro-cli's builtin file tools use ``path`` (see acp/kas_permissions.py)."""

    @pytest.mark.parametrize(
        "tool_name",
        ["fs_read", "read", "read_file", "fs_write", "fs_append", "str_replace", "write"],
    )
    def test_kiro_cli_file_tools_expose_path(self, tool_name: str) -> None:
        result = extract_tool_locations(tool_name, {"path": "/abs/main.py"})
        assert result == [{"path": "/abs/main.py"}]

    def test_fs_read_start_line_becomes_line(self) -> None:
        result = extract_tool_locations("fs_read", {"path": "/abs/main.py", "start_line": 42})
        assert result == [{"path": "/abs/main.py", "line": 42}]

    def test_str_replace_carries_no_line(self) -> None:
        # A search-and-replace edit doesn't have a fixed line — the follow
        # should land on the file, and the editor's own cursor placement wins.
        result = extract_tool_locations(
            "str_replace",
            {"path": "/abs/a.py", "oldStr": "x", "newStr": "y"},
        )
        assert result == [{"path": "/abs/a.py"}]


class TestAnthropicStyleTools:
    """Anthropic/Claude tools use ``file_path``."""

    @pytest.mark.parametrize("key", ["file_path", "filename"])
    def test_alt_path_keys_are_honored(self, key: str) -> None:
        result = extract_tool_locations("Edit", {key: "/abs/x.py"})
        assert result == [{"path": "/abs/x.py"}]

    def test_line_number_becomes_line(self) -> None:
        result = extract_tool_locations("Edit", {"file_path": "/abs/x.py", "line_number": 7})
        assert result == [{"path": "/abs/x.py", "line": 7}]


class TestShellTools:
    """Shell tools never carry a follow-along location, even with a ``path``."""

    @pytest.mark.parametrize(
        "tool_name", ["execute_bash", "execute_pwsh", "control_bash_process", "Bash", "bash"]
    )
    def test_shell_tool_names_yield_no_locations(self, tool_name: str) -> None:
        # `cat /tmp/x` legitimately has ``/tmp/x`` in params but the agent is
        # not editing that file; following there would jump the editor away
        # from the file the agent IS working on.
        result = extract_tool_locations(tool_name, {"command": "cat /tmp/x", "path": "/tmp/x"})
        assert result == []


class TestSchemaEnforcement:
    """ACP requires ``path`` to be absolute; relative or empty must yield no location."""

    def test_relative_path_is_rejected(self) -> None:
        assert extract_tool_locations("write", {"path": "src/main.py"}) == []

    def test_empty_path_is_rejected(self) -> None:
        assert extract_tool_locations("write", {"path": ""}) == []

    def test_non_string_path_is_rejected(self) -> None:
        assert extract_tool_locations("write", {"path": 123}) == []

    def test_windows_drive_letter_is_accepted(self) -> None:
        result = extract_tool_locations("write", {"path": "C:\\src\\main.py"})
        assert result == [{"path": "C:\\src\\main.py"}]

    def test_windows_forward_slash_drive_is_accepted(self) -> None:
        result = extract_tool_locations("write", {"path": "C:/src/main.py"})
        assert result == [{"path": "C:/src/main.py"}]


class TestMalformedInput:
    """Bad params never raise — they resolve to no location."""

    @pytest.mark.parametrize("raw", [None, {}, "not a dict", 42, []])
    def test_non_dict_params_return_empty(self, raw: Any) -> None:
        assert extract_tool_locations("fs_read", raw) == []

    def test_unknown_tool_with_no_path_returns_empty(self) -> None:
        assert extract_tool_locations("weather_api", {"city": "Seattle"}) == []

    def test_zero_line_is_rejected(self) -> None:
        # 1-based lines; 0 is a schema violation waiting to happen.
        result = extract_tool_locations("fs_read", {"path": "/a.py", "line": 0})
        assert result == [{"path": "/a.py"}]

    def test_negative_line_is_rejected(self) -> None:
        result = extract_tool_locations("fs_read", {"path": "/a.py", "line": -1})
        assert result == [{"path": "/a.py"}]

    def test_bool_line_is_rejected(self) -> None:
        # bool subclasses int; guard so True doesn't become line=1.
        result = extract_tool_locations("fs_read", {"path": "/a.py", "line": True})
        assert result == [{"path": "/a.py"}]

    def test_numeric_string_line_is_accepted(self) -> None:
        result = extract_tool_locations("fs_read", {"path": "/a.py", "line": "42"})
        assert result == [{"path": "/a.py", "line": 42}]


class TestBatchShapes:
    """Batch file operations produce one location per absolute-path entry."""

    def test_files_list_of_dicts(self) -> None:
        result = extract_tool_locations(
            "batch_read",
            {"files": [{"path": "/a.py"}, {"path": "/b.py", "line": 3}]},
        )
        assert result == [{"path": "/a.py"}, {"path": "/b.py", "line": 3}]

    def test_files_list_of_strings(self) -> None:
        result = extract_tool_locations("batch_read", {"files": ["/a.py", "/b.py"]})
        assert result == [{"path": "/a.py"}, {"path": "/b.py"}]

    def test_relative_batch_entries_are_dropped(self) -> None:
        result = extract_tool_locations(
            "batch_read",
            {"files": [{"path": "/a.py"}, {"path": "b.py"}]},
        )
        assert result == [{"path": "/a.py"}]


class TestPathCanonicalization:
    """Paths are canonicalized so ``$HOME`` symlink aliases don't defeat follow-along.

    On the Linux automount split ($HOME = /home/<u>, realpath = /local/home/<u>),
    an editor rooted at one side won't recognize a file addressed via the other.
    Regression guard for that: the extractor must return the realpath so both
    sides agree regardless of which alias the tool happened to receive.
    """

    def test_symlinked_directory_resolves_to_target(self, tmp_path: Any) -> None:
        target = tmp_path / "real"
        target.mkdir()
        real_file = target / "a.py"
        real_file.write_text("x = 1\n")
        alias = tmp_path / "alias"
        alias.symlink_to(target)

        result = extract_tool_locations("fs_read", {"path": str(alias / "a.py")})

        assert result == [{"path": str(real_file)}]

    def test_dotdot_and_extra_slashes_collapse(self, tmp_path: Any) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.py").write_text("")

        raw = f"{tmp_path}//sub/./../sub/a.py"
        result = extract_tool_locations("fs_read", {"path": raw})

        assert result == [{"path": str(tmp_path / "sub" / "a.py")}]

    def test_line_survives_canonicalization(self, tmp_path: Any) -> None:
        target = tmp_path / "real"
        target.mkdir()
        (target / "a.py").write_text("")
        alias = tmp_path / "alias"
        alias.symlink_to(target)

        result = extract_tool_locations("fs_read", {"path": str(alias / "a.py"), "start_line": 12})

        assert result == [{"path": str(target / "a.py"), "line": 12}]

    def test_nonexistent_path_is_still_returned(self, tmp_path: Any) -> None:
        # realpath resolves as far as it can and leaves the rest unchanged;
        # a not-yet-created file (fs_write to a new path) must still yield a
        # location so the editor can create+open it.
        missing = tmp_path / "not_yet.py"
        result = extract_tool_locations("fs_write", {"path": str(missing)})
        assert result == [{"path": str(missing)}]

    def test_windows_drive_paths_are_left_alone(self) -> None:
        # POSIX-only alias problem; on Windows, realpath's drive-letter
        # handling would introduce a different failure mode. Leave the raw
        # path so a Windows client's workspace comparison still matches.
        result = extract_tool_locations("write", {"path": "C:\\src\\main.py"})
        assert result == [{"path": "C:\\src\\main.py"}]

    def test_batch_string_entries_are_canonicalized(self, tmp_path: Any) -> None:
        target = tmp_path / "real"
        target.mkdir()
        (target / "a.py").write_text("")
        alias = tmp_path / "alias"
        alias.symlink_to(target)

        result = extract_tool_locations("batch_read", {"files": [str(alias / "a.py")]})

        assert result == [{"path": str(target / "a.py")}]
