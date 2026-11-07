"""A repository's own ``.mcp.json`` servers do not start in a claude session unapproved.

Claude Code approves a pending project server by itself in a non-interactive session,
so each session's inline settings list each such name in ``disabledMcpjsonServers``,
the key Claude Code checks before any approval; the seed file does not. A name the
user approved in the user settings of the claude child's config directory stays off
the list. When a name cannot be learned, no read finished, or the adapter may drop the
inline settings, the session mounts only Crew's own servers (``strictMcpConfig``).
"""

from __future__ import annotations

import errno
import importlib
import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _write_spec

from conftest import requires_symlinks
from kiro_crew.acp import claude_settings_sources
from kiro_crew.acp.claude_settings_sources import CLAUDE_SETTINGS_MAX_BYTES
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_framing import encode_frame
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

#: The settings key Claude Code checks before every project-server approval.
DISABLED_KEY = "disabledMcpjsonServers"

_HOME_VAR = "USERPROFILE" if os.name == "nt" else "HOME"

requires_proc = pytest.mark.skipif(
    not Path("/proc/self/cwd").exists(), reason="needs a /proc/self/cwd link"
)


def _gate_module():
    """The module under test, imported when a test runs rather than at collection."""
    return importlib.import_module("kiro_crew.acp.claude_project_mcp")


def project_mcp_gate(work_dir, config_dir=None, home=None):
    """The gate, reading approvals from this test's config dir unless one is named."""
    config = Path(os.environ["CLAUDE_CONFIG_DIR"]) if config_dir is None else config_dir
    return _gate_module().project_mcp_gate(work_dir, config_dir=config, home=home)


def _seen(work_dir, **kwargs) -> tuple[list[str], bool]:
    gate = project_mcp_gate(work_dir, **kwargs)
    return gate.disabled, gate.unlisted


@pytest.fixture(autouse=True)
def _user_config_dir(tmp_path, _floor_monkeypatch):
    """The ambient Claude config directory is this test's, never the host's."""
    config = tmp_path / "claude-config"
    config.mkdir()
    _floor_monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


@pytest.fixture(autouse=True, name="real_search_dirs")
def _walk_stays_in_tmp(tmp_path, _floor_monkeypatch):
    """Only this test's directories are read, never a ``.mcp.json`` above the temp root.

    Returns the real walk, which still goes to the filesystem root
    (``test_the_walk_reaches_the_root``). Without the module there is no walk to narrow.
    """
    try:
        module = _gate_module()
    except ImportError:
        return None
    real = module._search_dirs
    roots = {tmp_path, Path(os.path.realpath(tmp_path))}

    def _inside(work_dir):
        return [d for d in real(work_dir) if any(d == r or r in d.parents for r in roots)]

    _floor_monkeypatch.setattr(module, "_search_dirs", _inside)
    return real


def _servers(path: Path, *names: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    servers = {name: {"command": "true"} for name in names}
    (path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _user_settings(config: Path, settings: dict) -> None:
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    """A work dir one level inside a parent that carries its own ``.mcp.json``."""
    work = tmp_path / "parent" / "repo"
    _servers(work, "proj_a", "proj_b")
    _servers(tmp_path / "parent", "parent_c")
    return work


def _unknown(tmp_path: Path) -> Path:
    """A work dir whose only ``.mcp.json`` is a directory, so its names are unknown."""
    work = tmp_path / "w"
    (work / ".mcp.json").mkdir(parents=True)
    return work


def _client(work: Path, **kwargs) -> AcpClient:
    return AcpClient(work_dir=work, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, **kwargs)


def _options(client: AcpClient) -> dict:
    return client._claude_session_meta()["claudeCode"]["options"]


def _seed(work: Path) -> dict:
    return json.loads((work / ".claude" / "settings.local.json").read_bytes().decode("utf-8"))


class TestNames:
    def test_the_work_dir_and_every_parent_are_listed(self, tmp_path):
        assert _seen(_repo(tmp_path)) == (["parent_c", "proj_a", "proj_b"], False)

    def test_no_mcp_json_lists_nothing(self, tmp_path):
        work = tmp_path / "empty"
        work.mkdir()
        assert _seen(work) == ([], False)

    def test_a_user_approved_name_stays_off_the_list(self, tmp_path, _user_config_dir):
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": ["proj_a", "unrelated"]})
        assert project_mcp_gate(_repo(tmp_path)).disabled == ["parent_c", "proj_b"]

    def test_an_approval_matches_only_the_exact_name(self, tmp_path, _user_config_dir):
        work = tmp_path / "w"
        _servers(work, "my server", "plugin:x:y", "kept")
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": ["my_server", "kept"]})
        # Claude Code would also take "my_server" as approving "my server"; Crew keeps it
        # off, which only keeps a server off.
        assert project_mcp_gate(work).disabled == ["my server", "plugin:x:y"]

    def test_a_single_name_string_counts_as_a_list(self, tmp_path, _user_config_dir):
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": "proj_a"})
        assert project_mcp_gate(_repo(tmp_path)).disabled == ["parent_c", "proj_b"]

    def test_a_single_name_string_must_be_one_whole_name(self, tmp_path, _user_config_dir):
        work = tmp_path / "w"
        _servers(work, "a b")
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": "a b"})
        assert project_mcp_gate(work).disabled == ["a b"]

    def test_enable_all_approves_every_name(self, tmp_path, _user_config_dir):
        _user_settings(_user_config_dir, {"enableAllProjectMcpServers": True})
        assert project_mcp_gate(_repo(tmp_path)).disabled == []

    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_only_a_real_true_approves_every_name(self, tmp_path, _user_config_dir, value):
        _user_settings(_user_config_dir, {"enableAllProjectMcpServers": value})
        assert project_mcp_gate(_repo(tmp_path)).disabled == ["parent_c", "proj_a", "proj_b"]

    def test_non_string_approvals_approve_nothing(self, tmp_path, _user_config_dir):
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": [1, None, ["proj_a"]]})
        assert project_mcp_gate(_repo(tmp_path)).disabled == ["parent_c", "proj_a", "proj_b"]

    def test_a_project_name_the_user_disabled_stays_listed(self, tmp_path, _user_config_dir):
        # Approved and disabled is disabled, as in Claude Code. A disabled name no
        # .mcp.json names changes nothing here and is left to the user's own file.
        _user_settings(
            _user_config_dir,
            {"enabledMcpjsonServers": ["proj_a"], "disabledMcpjsonServers": ["proj_a", "x"]},
        )
        assert project_mcp_gate(_repo(tmp_path)).disabled == ["parent_c", "proj_a", "proj_b"]

    @requires_symlinks
    def test_a_symlinked_user_settings_file_is_followed(self, tmp_path, _user_config_dir):
        _user_settings(tmp_path / "dotfiles", {"enabledMcpjsonServers": ["proj_a"]})
        os.symlink(tmp_path / "dotfiles" / "settings.json", _user_config_dir / "settings.json")
        assert project_mcp_gate(_repo(tmp_path)).disabled == ["parent_c", "proj_b"]

    def test_no_config_dir_approves_nothing(self, tmp_path, _user_config_dir):
        _user_settings(_user_config_dir, {"enableAllProjectMcpServers": True})
        gate = _gate_module().project_mcp_gate(_repo(tmp_path), config_dir=None)
        assert gate.disabled == ["parent_c", "proj_a", "proj_b"]

    def test_the_walk_reaches_the_root(self, tmp_path, real_search_dirs):
        work = tmp_path / "a" / "b"
        work.mkdir(parents=True)
        dirs = real_search_dirs(work)
        assert dirs[0] == work and tmp_path in dirs and Path(work.anchor) in dirs

    def test_a_file_whose_names_are_all_approved_is_no_source(self, tmp_path, _user_config_dir):
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": ["proj_a", "proj_b"]})
        assert project_mcp_gate(_repo(tmp_path)).sources == (tmp_path / "parent" / ".mcp.json",)

    @requires_symlinks
    def test_a_file_reached_two_ways_is_one_source(self, tmp_path):
        real = _repo(tmp_path)
        link = tmp_path / "elsewhere" / "link"
        link.parent.mkdir()
        os.symlink(real, link, target_is_directory=True)
        sources = project_mcp_gate(link).sources
        files = {os.path.realpath(real / ".mcp.json"), os.path.realpath(real.parent / ".mcp.json")}
        assert len(sources) == 2 and {os.path.realpath(path) for path in sources} == files

    @requires_symlinks
    def test_a_parent_seen_only_through_a_symlinked_work_dir_is_listed(self, tmp_path):
        real = _repo(tmp_path)
        link = tmp_path / "elsewhere" / "link"
        link.parent.mkdir()
        os.symlink(real, link, target_is_directory=True)
        assert project_mcp_gate(link).disabled == ["parent_c", "proj_a", "proj_b"]


class TestConfigDir:
    def test_the_childs_environment_names_the_config_dir(self, tmp_path):
        claude_config_dir = claude_settings_sources.claude_config_dir
        assert claude_config_dir({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path
        home = {"CLAUDE_CONFIG_DIR": "", _HOME_VAR: str(tmp_path)}
        assert claude_config_dir(home) == tmp_path / ".claude"
        # Claude Code reads a relative one from its own cwd, the session's work dir.
        assert claude_config_dir({"CLAUDE_CONFIG_DIR": "relative"}) is None
        # A relative home too, so it names no config dir and no home.
        assert claude_config_dir({"CLAUDE_CONFIG_DIR": "", _HOME_VAR: "h"}) is None
        assert claude_settings_sources.claude_home({_HOME_VAR: "h"}) is None
        assert claude_settings_sources.claude_home({_HOME_VAR: str(tmp_path)}) == tmp_path

    def test_a_relative_home_skips_no_mcp_json(self, tmp_path, agents_dir, monkeypatch):
        # The child resolves "h" against its own cwd, the work dir, not against Crew's.
        _write_spec(agents_dir, servers={}, tools=[])
        _servers(tmp_path / "h", "beside_the_home")
        work = tmp_path / "h" / "repo"
        work.mkdir()
        monkeypatch.chdir(tmp_path)
        client = _client(work, extra_env={_HOME_VAR: "h"})
        client._write_claude_local_settings()
        assert _options(client)["settings"][DISABLED_KEY] == ["beside_the_home"]

    def test_the_gateways_config_dir_approves_when_the_session_names_none(
        self, tmp_path, agents_dir, _user_config_dir, _floor_monkeypatch
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        _floor_monkeypatch.setenv(_HOME_VAR, str(tmp_path / "home"))
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": ["proj_a"]})
        client = _client(_repo(tmp_path))
        client._write_claude_local_settings()
        assert _options(client)["settings"][DISABLED_KEY] == ["parent_c", "proj_b"]

    def test_the_sessions_extra_env_config_dir_decides(
        self, tmp_path, agents_dir, _user_config_dir
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": ["proj_a"]})
        child = tmp_path / "child-config"
        child.mkdir()
        client = _client(_repo(tmp_path), extra_env={"CLAUDE_CONFIG_DIR": str(child)})
        client._write_claude_local_settings()
        assert _options(client)["settings"][DISABLED_KEY] == ["parent_c", "proj_a", "proj_b"]

    def test_agent_env_decides_under_extra_env(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={}, tools=[])
        approving = tmp_path / "approving"
        _user_settings(approving, {"enabledMcpjsonServers": ["proj_a"]})
        client = _client(_repo(tmp_path))
        client._agent_env_snapshot = {"CLAUDE_CONFIG_DIR": str(approving)}
        client._write_claude_local_settings()
        assert _options(client)["settings"][DISABLED_KEY] == ["parent_c", "proj_b"]
        client._extra_env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "none")}
        client._write_claude_local_settings()
        assert _options(client)["settings"][DISABLED_KEY] == ["parent_c", "proj_a", "proj_b"]

    @pytest.mark.parametrize("case_insensitive", [True, False])
    def test_a_name_spelled_two_ways_on_windows_approves_nothing(
        self, tmp_path, _user_config_dir, case_insensitive
    ):
        # The ambient CLAUDE_CONFIG_DIR approves proj_a. On Windows the child also sees
        # claude_config_dir as that name, and which of the two it reads is not defined.
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": ["proj_a"]})
        gate = _gate_module().child_project_mcp_gate(
            _repo(tmp_path),
            agent_env={},
            extra_env={"claude_config_dir": str(tmp_path / "other")},
            case_insensitive=case_insensitive,
        )
        approved = set() if case_insensitive else {"proj_a"}
        assert gate.disabled == sorted({"parent_c", "proj_a", "proj_b"} - approved)
        assert gate.config_dir == (None if case_insensitive else _user_config_dir)

    def test_a_name_spelled_two_ways_on_windows_skips_no_home_file(
        self, tmp_path, _floor_monkeypatch
    ):
        # Which home the child reads is not defined either, so the home's file counts.
        home = tmp_path / "home"
        _servers(home, "beside_the_home")
        (home / "repo").mkdir()
        _floor_monkeypatch.setenv(_HOME_VAR, str(home))
        gate = _gate_module().child_project_mcp_gate(
            home / "repo",
            agent_env={},
            extra_env={"claude_config_dir": str(tmp_path / "other")},
            case_insensitive=True,
        )
        assert gate.disabled == ["beside_the_home"]

    def test_one_spelling_on_windows_names_the_config_dir(
        self, tmp_path, _user_config_dir, _floor_monkeypatch
    ):
        _floor_monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        approving = tmp_path / "approving"
        _user_settings(approving, {"enabledMcpjsonServers": ["proj_a"]})
        gate = _gate_module().child_project_mcp_gate(
            _repo(tmp_path),
            agent_env={},
            extra_env={"claude_config_dir": str(approving)},
            case_insensitive=True,
        )
        assert gate.disabled == ["parent_c", "proj_b"]

    def test_names_match_in_any_case_only_on_windows(self, tmp_path, _user_config_dir):
        _user_settings(_user_config_dir, {"enabledMcpjsonServers": ["proj_a"]})
        gate = _gate_module().child_project_mcp_gate(
            _repo(tmp_path), agent_env={}, extra_env={"claude_config_dir": str(tmp_path / "x")}
        )
        approved = set() if os.name == "nt" else {"proj_a"}
        assert gate.disabled == sorted({"parent_c", "proj_a", "proj_b"} - approved)


_BIG = b"1" * 5000


class TestParse:
    def _names(self, tmp_path: Path, raw: bytes) -> tuple[list[str], bool]:
        work = tmp_path / "w"
        work.mkdir()
        (work / ".mcp.json").write_bytes(raw)
        return _seen(work)

    def test_invalid_json_lists_nothing_and_is_not_unlisted(self, tmp_path):
        # Claude Code starts nothing from a file it cannot parse.
        assert self._names(tmp_path, b'{"mcpServers": {"a": {}},') == ([], False)

    @pytest.mark.parametrize(
        "raw",
        [b"[1, 2]", b'"mcpServers"', b'{"mcpServers": ["a"]}', b'{"x": {"mcpServers": {"a": 1}}}'],
    )
    def test_no_top_level_server_object_lists_nothing(self, tmp_path, raw):
        assert self._names(tmp_path, raw) == ([], False)

    def test_nesting_the_parser_refuses_is_unlisted(self, tmp_path):
        deep = b"[" * 200000 + b"]" * 200000
        raw = b'{"pad": ' + deep + b', "mcpServers": {"evil": {}}}'
        assert self._names(tmp_path, raw) == ([], True)

    @pytest.mark.parametrize(
        "raw",
        [
            b'{"mcpServers": {"evil": {"command": "true"}}, "pad": ' + _BIG + b"}",
            b'{"pad": ' + _BIG + b', "mcpServers": {"evil": {"command": "true"}}}',
            b'{"mcpServers": {"evil": {"command": "true"}}, "z": [' + _BIG + b"]}",
        ],
        ids=["after", "before", "in_a_list"],
    )
    def test_an_integer_python_refuses_is_unlisted(self, tmp_path, raw):
        # Python refuses more than 4,300 digits; Claude Code's JSON.parse reads the file.
        assert self._names(tmp_path, raw) == ([], True)

    def test_bom_escapes_duplicates_and_bad_bytes_read_as_claude_reads_them(self, tmp_path):
        # The escaped key is a second "mcpServers"; the last one wins, as in Claude Code.
        raw = (
            b'\xef\xbb\xbf{"mcp\\u0053ervers": {"one": {}}, "mcpServers": '
            b'{"two": {}, "bad\xffbyte": {"k": "v"}}}'
        )
        assert self._names(tmp_path, raw) == (["bad\ufffdbyte", "two"], False)

    def test_a_lone_surrogate_name_still_encodes_into_the_frame(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={}, tools=[])
        work = tmp_path / "w"
        work.mkdir()
        (work / ".mcp.json").write_bytes(b'{"mcpServers": {"lone\\ud800": {}}}')
        client = _client(work)
        client._write_claude_local_settings()
        meta = client._claude_session_meta()
        assert meta["claudeCode"]["options"]["settings"][DISABLED_KEY] == ["lone\ud800"]
        frame = json.loads(encode_frame({"params": {"_meta": meta}}))
        [name] = frame["params"]["_meta"]["claudeCode"]["options"]["settings"][DISABLED_KEY]
        # U+FFFD on the wire. Claude Code compares names with every character outside
        # [A-Za-z0-9_-] mapped to "_", so it still matches the .mcp.json key.
        assert name == "lone\ufffd"

    @requires_symlinks
    def test_a_symlinked_file_is_followed(self, tmp_path):
        work = tmp_path / "w"
        _servers(tmp_path / "shared", "linked")
        work.mkdir()
        os.symlink(Path("..") / "shared" / ".mcp.json", work / ".mcp.json")
        assert _seen(work) == (["linked"], False)

    @requires_symlinks
    def test_a_chain_of_links_is_followed(self, tmp_path):
        work = tmp_path / "w"
        _servers(tmp_path / "shared", "linked")
        work.mkdir()
        os.symlink(tmp_path / "shared" / ".mcp.json", tmp_path / "hop.json")
        os.symlink(tmp_path / "hop.json", work / ".mcp.json")
        assert _seen(work) == (["linked"], False)

    @requires_symlinks
    def test_a_link_to_a_sensitive_path_is_not_read(self, tmp_path, monkeypatch):
        work = tmp_path / "w"
        _servers(tmp_path / "secret", "hidden")
        work.mkdir()
        os.symlink(tmp_path / "secret" / ".mcp.json", work / ".mcp.json")
        monkeypatch.setattr(_gate_module(), "is_sensitive_path", lambda _p: True)
        assert _seen(work) == ([], True)

    @requires_symlinks
    def test_a_link_loop_is_unlisted(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        os.symlink(work / ".mcp.json", work / ".mcp.json")
        assert _seen(work) == ([], True)

    @requires_symlinks
    def test_a_dangling_link_is_unlisted(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        os.symlink(tmp_path / "missing.json", work / ".mcp.json")
        assert _seen(work) == ([], True)

    @requires_symlinks
    @requires_proc
    @pytest.mark.parametrize(
        "how", ["direct", "through_a_dir", "up_through_the_root", "double_slash"]
    )
    def test_a_link_through_proc_self_cwd_is_unlisted(self, tmp_path, monkeypatch, how):
        # /proc/self/cwd is Claude Code's work dir in its process and Crew's own cwd here,
        # where a harmless x.json waits to be read in its place.
        work = tmp_path / "w"
        work.mkdir()
        (work / "x.json").write_text('{"mcpServers": {"evil": {}}}', encoding="utf-8")
        gateway_cwd = tmp_path / "gateway"
        gateway_cwd.mkdir()
        (gateway_cwd / "x.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        if how == "through_a_dir":
            os.symlink("/proc/self/cwd", work / "d")
            os.symlink(Path("d") / "x.json", work / ".mcp.json")
        elif how == "double_slash":
            # The kernel reads "//proc" as "/proc"; pathlib keeps both slashes.
            os.symlink("//proc/self/cwd/x.json", work / ".mcp.json")
        elif how == "up_through_the_root":
            # A relative link that climbs to the root before it reaches /proc.
            up = [".."] * (len(Path(os.path.realpath(work)).parts) - 1)
            os.symlink(Path(*up, "proc", "self", "cwd", "x.json"), work / ".mcp.json")
        else:
            os.symlink("/proc/self/cwd/x.json", work / ".mcp.json")
        monkeypatch.chdir(gateway_cwd)
        # Only the process-relative rule may refuse it, wherever the temp root sits.
        monkeypatch.setattr(_gate_module(), "is_sensitive_path", lambda _p: False)
        assert _seen(work) == ([], True)

    @pytest.mark.parametrize("text", ["//proc/self/cwd", "///proc/1/root", "//dev/fd/3"])
    def test_extra_leading_slashes_still_name_a_process_relative_dir(self, text):
        assert _gate_module()._process_relative(Path(text)) is True

    @requires_symlinks
    @pytest.mark.skipif(not Path("/dev/stdin").is_symlink(), reason="needs a /dev/stdin link")
    def test_a_link_to_dev_stdin_is_unlisted(self, tmp_path, monkeypatch):
        # /dev/stdin is each process's own input. The walk stops at the hop under /proc/
        # (Linux) or /dev/fd/ (macOS), not at whatever this process's stdin is.
        assert _gate_module()._link_target(Path("/dev/stdin")) is None
        work = tmp_path / "w"
        work.mkdir()
        os.symlink("/dev/stdin", work / ".mcp.json")
        monkeypatch.setattr(_gate_module(), "is_sensitive_path", lambda _p: False)
        assert _seen(work) == ([], True)

    @requires_symlinks
    @pytest.mark.skipif(
        not Path("/sys/kernel/uevent_seqnum").is_file(), reason="needs a sysfs file"
    )
    def test_a_link_into_sys_is_read_as_claude_code_reads_it(self, tmp_path, monkeypatch):
        # /sys means the same in both processes; this file is not JSON, so neither
        # side starts anything from it.
        work = tmp_path / "w"
        work.mkdir()
        os.symlink("/sys/kernel/uevent_seqnum", work / ".mcp.json")
        monkeypatch.setattr(_gate_module(), "is_sensitive_path", lambda _p: False)
        assert _seen(work) == ([], False)

    def test_an_oversized_file_is_unlisted(self, tmp_path):
        pad = b" " * (CLAUDE_SETTINGS_MAX_BYTES + 1)
        assert self._names(tmp_path, b'{"mcpServers": {"big": {}}}' + pad) == ([], True)

    def test_a_directory_in_its_place_is_unlisted(self, tmp_path):
        assert _seen(_unknown(tmp_path)) == ([], True)

    def test_under_enable_all_a_name_crew_cannot_learn_is_approved(
        self, tmp_path, _user_config_dir
    ):
        _user_settings(_user_config_dir, {"enableAllProjectMcpServers": True})
        assert _seen(_unknown(tmp_path)) == ([], False)

    def test_more_names_than_the_settings_argument_holds_are_unlisted(self, tmp_path):
        work = tmp_path / "w"
        _servers(work, *(f"server_{i:05d}_{'x' * 64}" for i in range(2000)))
        assert _seen(work) == ([], True)

    @pytest.mark.parametrize(
        "char,count",
        # 45,000 three-byte characters are under 64K characters but past one Linux
        # argument as --settings; 35,000 ASCII ones are past a whole Windows command line.
        # Windows quotes the \" the JSON writes for each quote as \\\", so 5,000 quotes,
        # about 10,000 bytes of JSON, quote to about 20,000 characters.
        [("\u6f22", 90), ("x", 70), ('"', 10)],
        ids=["past_one_linux_argument", "past_a_windows_command_line", "quoted_on_windows"],
    )
    def test_the_cap_counts_the_encoded_size_not_the_characters(
        self, tmp_path, agents_dir, char, count
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        work = tmp_path / "w"
        _servers(work, *(f"n{i:02d}_" + char * 500 for i in range(count)))
        client = _client(work)
        client._write_claude_local_settings()
        assert client._claude_project_mcp_gate.unlisted is True
        options = _options(client)
        assert options.get("strictMcpConfig") is True
        assert DISABLED_KEY not in options.get("settings", {})

    def test_a_list_under_the_cap_still_rides_inline(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={}, tools=[])
        work = tmp_path / "w"
        names = [f"n{i:02d}_" + "\u6f22" * 500 for i in range(4)]
        _servers(work, *names)
        client = _client(work)
        client._write_claude_local_settings()
        options = _options(client)
        assert options["settings"][DISABLED_KEY] == names
        assert "strictMcpConfig" not in options


def _symlinked_local_settings(work: Path, _patch: pytest.MonkeyPatch | None = None) -> None:
    (work / ".claude").mkdir()
    (work / "x.json").write_text("{}", encoding="utf-8")
    os.symlink(Path("..") / "x.json", work / ".claude" / "settings.local.json")


def _dangling_local_settings(work: Path, _patch: pytest.MonkeyPatch | None = None) -> None:
    (work / ".claude").mkdir()
    os.symlink(work / "missing" / "x.json", work / ".claude" / "settings.local.json")


def _symlinked_claude_dir(work: Path, _patch: pytest.MonkeyPatch | None = None) -> None:
    (work / "dotclaude").mkdir()
    os.symlink("dotclaude", work / ".claude", target_is_directory=True)


def _regular_file_claude(work: Path, _patch: pytest.MonkeyPatch | None = None) -> None:
    (work / ".claude").write_text("not a directory", encoding="utf-8")


def _refused_exclusion(work: Path, _patch: pytest.MonkeyPatch | None = None) -> None:
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    (work / ".claude" / "settings.json").write_text(
        '{"sandbox": {"enabled": false}}', encoding="utf-8"
    )


def _failed_write(work: Path, patch: pytest.MonkeyPatch | None = None) -> None:
    """The seed's own write fails once its file is created."""
    assert patch is not None
    target = work / ".claude" / "settings.local.json"
    real_fdopen = os.fdopen

    def _fdopen(fd, *args, **kwargs):
        if target.exists() and os.path.samestat(os.fstat(fd), target.stat()):
            os.close(fd)
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_fdopen(fd, *args, **kwargs)

    patch.setattr(os, "fdopen", _fdopen)


def _foreign_local_settings(work: Path, _patch: pytest.MonkeyPatch | None = None) -> None:
    own = work / ".claude" / "settings.local.json"
    own.parent.mkdir()
    own.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}), encoding="utf-8")


_NO_SEED = {
    "symlinked_local_settings": pytest.param(_symlinked_local_settings, marks=requires_symlinks),
    "dangling_local_settings": pytest.param(_dangling_local_settings, marks=requires_symlinks),
    "symlinked_claude_dir": pytest.param(_symlinked_claude_dir, marks=requires_symlinks),
    "regular_file_claude": pytest.param(_regular_file_claude),
    "refused_exclusion": pytest.param(_refused_exclusion),
    "failed_write": pytest.param(_failed_write),
}

_GATE_LOGGER = "kiro_crew.acp.claude_project_mcp"


async def _seeded(work: Path, arrange, patch: pytest.MonkeyPatch) -> AcpClient:
    """A client whose spawn-time seed ran on *work* after *arrange*, as at launch."""
    arrange(work, patch)
    client = _client(work)
    await client._seed_session_settings()
    return client


def _project_mcp_notices(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _GATE_LOGGER]


def _home_link(tmp_path: Path, home: Path) -> Path:
    link = tmp_path / "home-link"
    os.symlink(home, link, target_is_directory=True)
    return link


class TestSession:
    def test_the_inline_settings_carry_the_list_and_the_seed_does_not(self, tmp_path, agents_dir):
        # The user's own claude in this work dir reads the seed, and still asks there.
        _write_spec(agents_dir, servers={}, tools=[])
        work = _repo(tmp_path)
        client = _client(work)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        assert DISABLED_KEY not in _seed(work)
        options = _options(client)
        assert options["settings"][DISABLED_KEY] == ["parent_c", "proj_a", "proj_b"]
        assert "strictMcpConfig" not in options

    def test_a_client_whose_files_were_never_read_is_strict(self, tmp_path, agents_dir):
        # Every launch reads them before session/new; a path that skips it fails closed.
        _write_spec(agents_dir, servers={}, tools=[])
        client = _client(_repo(tmp_path))
        assert client._claude_project_mcp_gate is None
        assert _options(client).get("strictMcpConfig") is True

    def test_one_seed_write_reads_the_files_once(self, tmp_path, agents_dir, monkeypatch):
        _write_spec(agents_dir, servers={}, tools=[])
        seed_module = importlib.import_module("kiro_crew.acp.client_claude_seed")
        real = seed_module.child_project_mcp_gate
        calls = []

        def _counted(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(seed_module, "child_project_mcp_gate", _counted)
        client = _client(_repo(tmp_path))
        client._write_claude_local_settings()
        assert _options(client)["settings"][DISABLED_KEY] == ["parent_c", "proj_a", "proj_b"]
        assert len(calls) == 1

    def test_an_empty_list_writes_no_key(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={}, tools=[])
        work = tmp_path / "w"
        work.mkdir()
        client = _client(work)
        client._write_claude_local_settings()
        assert DISABLED_KEY not in _seed(work)
        assert DISABLED_KEY not in _options(client).get("settings", {})

    def test_the_exclusion_path_adds_no_list(self, tmp_path, agents_dir, caplog):
        # The project source is off there, so the servers stay pending, and the inline
        # settings (the project's own file) do not grow on the command line.
        _write_spec(agents_dir, servers={}, tools=[])
        work = _repo(tmp_path)
        _foreign_local_settings(work)
        client = _client(work)
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is True
        assert client._claude_project_mcp_gate.disabled == ["parent_c", "proj_a", "proj_b"]
        options = _options(client)
        assert options["settingSources"] == ["user"]
        assert DISABLED_KEY not in options.get("settings", {})
        assert "strictMcpConfig" not in options
        # No project server starts there, approved or not, so no remedy applies.
        assert _project_mcp_notices(caplog) == []

    def test_an_excluded_sessions_own_list_passes_through(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={}, tools=[])
        client = _client(_repo(tmp_path))
        client._claude_local_settings_excluded = True
        client._claude_inline_settings = {DISABLED_KEY: ["x_inline"]}
        client._claude_project_mcp_gate = _gate_module().ProjectMcpGate(["a_gate"], False)
        assert _options(client)["settings"][DISABLED_KEY] == ["x_inline"]
        assert client._claude_inline_settings == {DISABLED_KEY: ["x_inline"]}

    def test_an_excluded_session_with_an_unknown_name_is_not_strict(self, tmp_path, agents_dir):
        # The project setting source is off there, and Claude Code approves no project
        # server without it.
        _write_spec(agents_dir, servers={}, tools=[])
        work = _unknown(tmp_path)
        _foreign_local_settings(work)
        client = _client(work)
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is True
        options = _options(client)
        assert options["settingSources"] == ["user"]
        assert "strictMcpConfig" not in options

    def test_names_it_cannot_list_mount_only_crews_servers(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={}, tools=[])
        client = _client(_unknown(tmp_path))
        client._write_claude_local_settings()
        assert _options(client).get("strictMcpConfig") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arrange", list(_NO_SEED.values()), ids=list(_NO_SEED))
    async def test_a_session_no_seed_reaches_carries_the_list_inline(
        self, tmp_path, agents_dir, monkeypatch, arrange
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        client = await _seeded(_repo(tmp_path), arrange, monkeypatch)
        assert not client._claude_settings_authored
        assert not client._claude_local_settings_excluded
        options = _options(client)
        assert options["settings"][DISABLED_KEY] == ["parent_c", "proj_a", "proj_b"]
        assert "strictMcpConfig" not in options

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arrange", list(_NO_SEED.values()), ids=list(_NO_SEED))
    async def test_unknown_names_on_a_branch_no_seed_reaches_are_strict(
        self, tmp_path, agents_dir, monkeypatch, arrange
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        client = await _seeded(_unknown(tmp_path), arrange, monkeypatch)
        assert not client._claude_settings_authored
        assert _options(client).get("strictMcpConfig") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arrange", list(_NO_SEED.values()), ids=list(_NO_SEED))
    async def test_without_project_servers_such_a_session_keeps_the_users_own(
        self, tmp_path, agents_dir, monkeypatch, arrange
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        work = tmp_path / "w"
        work.mkdir()
        options = _options(await _seeded(work, arrange, monkeypatch))
        assert "strictMcpConfig" not in options
        assert DISABLED_KEY not in options.get("settings", {})

    @pytest.mark.parametrize(
        "arrange",
        [None, pytest.param(_symlinked_claude_dir, marks=requires_symlinks)],
        ids=["seeded", "symlinked_claude_dir"],
    )
    def test_below_the_settings_floor_a_listed_session_is_strict(
        self, tmp_path, agents_dir, caplog, arrange
    ):
        # An adapter below the floor may drop options.settings, the inline list with it.
        _write_spec(agents_dir, servers={}, tools=[])
        work = _repo(tmp_path)
        if arrange is not None:
            arrange(work)
        client = _client(work)
        client._write_claude_local_settings()
        client._agent_version_read = True
        client._agent_version = "0.83.0"
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        assert _options(client).get("strictMcpConfig") is True
        [message] = _project_mcp_notices(caplog)
        assert "too old" in message and "enabledMcpjsonServers" in message
        client._agent_version = "0.84.0"
        assert "strictMcpConfig" not in _options(client)

    def test_below_the_floor_a_session_without_project_servers_is_not_strict(
        self, tmp_path, agents_dir
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        work = tmp_path / "w"
        work.mkdir()
        client = _client(work)
        client._write_claude_local_settings()
        client._agent_version_read = True
        client._agent_version = ""
        assert "strictMcpConfig" not in _options(client)

    @pytest.mark.parametrize(
        "spelling",
        [
            "as_is",
            pytest.param("home_through_a_link", marks=requires_symlinks),
            pytest.param("both_through_a_link", marks=requires_symlinks),
        ],
    )
    def test_the_homes_own_mcp_json_is_skipped(self, tmp_path, agents_dir, spelling):
        # Crew writes its discovered servers there, and no repository can.
        _write_spec(agents_dir, servers={}, tools=[])
        home = tmp_path / "home"
        _servers(home, "crew_discovered")
        (home / "repo").mkdir()
        named = home if spelling == "as_is" else _home_link(tmp_path, home)
        work = (named if spelling == "both_through_a_link" else home) / "repo"
        _symlinked_claude_dir(work)
        client = _client(work, extra_env={_HOME_VAR: str(named)})
        client._write_claude_local_settings()
        assert client._claude_project_mcp_gate.disabled == []
        options = _options(client)
        assert "strictMcpConfig" not in options
        assert DISABLED_KEY not in options.get("settings", {})
        _servers(work, "proj_a")
        client._write_claude_local_settings()
        assert _options(client)["settings"][DISABLED_KEY] == ["proj_a"]

    @requires_symlinks
    @pytest.mark.parametrize("spelling", ["as_is", "home_through_a_link", "work_through_a_link"])
    def test_in_the_home_dir_itself_its_mcp_json_counts(self, tmp_path, agents_dir, spelling):
        _write_spec(agents_dir, servers={}, tools=[])
        home = tmp_path / "home"
        _servers(home, "proj_a")
        link = _home_link(tmp_path, home)
        named, work = {
            "as_is": (home, home),
            "home_through_a_link": (link, Path(os.path.realpath(home))),
            "work_through_a_link": (Path(os.path.realpath(home)), link),
        }[spelling]
        client = _client(work, extra_env={_HOME_VAR: str(named)})
        client._write_claude_local_settings()
        assert client._claude_project_mcp_gate.disabled == ["proj_a"]

    @requires_symlinks
    @pytest.mark.parametrize("spelling", ["home_resolved", "home_through_its_own_link"])
    def test_the_homes_own_mcp_json_is_skipped_under_a_third_spelling(self, tmp_path, spelling):
        # The work dir reaches the home through "alias", a link HOME does not use. pytest
        # resolves tmp_path, so that unresolved spelling is built here.
        base = tmp_path / "base"
        _servers(base / "home", "crew_discovered")
        (base / "home" / "repo").mkdir()
        alias = tmp_path / "alias"
        os.symlink(base, alias, target_is_directory=True)
        home = base / "home"
        if spelling == "home_through_its_own_link":
            home = _home_link(tmp_path, home)
        assert project_mcp_gate(alias / "home" / "repo", home=home).disabled == []

    def test_a_strict_session_names_the_files_and_the_remedy(
        self, tmp_path, agents_dir, _user_config_dir, caplog
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        work = _unknown(tmp_path)
        client = _client(work)
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        client._write_claude_local_settings()
        # The read itself says nothing about what the session mounts.
        assert _project_mcp_notices(caplog) == []
        assert _options(client).get("strictMcpConfig") is True
        [message] = _project_mcp_notices(caplog)
        assert "strictMcpConfig" in message
        assert str(work / ".mcp.json") in message
        assert str(_user_config_dir / "settings.json") in message
        assert "enableAllProjectMcpServers: true" in message

    def test_a_listed_session_names_the_files_and_both_remedies(
        self, tmp_path, agents_dir, _user_config_dir, caplog
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        work = _repo(tmp_path)
        client = _client(work)
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        client._write_claude_local_settings()
        assert "strictMcpConfig" not in _options(client)
        [message] = _project_mcp_notices(caplog)
        assert str(work / ".mcp.json") in message
        assert str(work.parent / ".mcp.json") in message
        assert str(_user_config_dir / "settings.json") in message
        assert "enabledMcpjsonServers" in message
        assert "enableAllProjectMcpServers: true" in message
        assert not any(name in message for name in ("proj_a", "proj_b", "parent_c"))

    @pytest.mark.parametrize(
        "arrange,word", [(_repo, "not approved"), (_unknown, "strictMcpConfig")]
    )
    def test_a_notice_is_logged_once_per_session(self, tmp_path, agents_dir, caplog, arrange, word):
        _write_spec(agents_dir, servers={}, tools=[])
        client = _client(arrange(tmp_path))
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        client._write_claude_local_settings()
        _options(client)
        # A rewind sends a fork, then a load; a re-seed reads the files again.
        _options(client)
        client._write_claude_local_settings()
        _options(client)
        [message] = _project_mcp_notices(caplog)
        assert word in message

    @pytest.mark.parametrize(
        "arrange,word", [(_repo, "not approved"), (_unknown, "strictMcpConfig")]
    )
    def test_a_different_read_logs_again(self, tmp_path, agents_dir, caplog, arrange, word):
        _write_spec(agents_dir, servers={}, tools=[])
        client = _client(arrange(tmp_path))
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        client._write_claude_local_settings()
        _options(client)
        _servers(tmp_path, "added_later")
        client._write_claude_local_settings()
        _options(client)
        messages = _project_mcp_notices(caplog)
        assert len(messages) == 2 and all(word in message for message in messages)

    def test_below_the_floor_an_unreadable_file_is_named_as_the_cause(
        self, tmp_path, agents_dir, caplog
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        work = _unknown(tmp_path)
        _servers(tmp_path, "listed")
        client = _client(work)
        client._write_claude_local_settings()
        client._agent_version_read = True
        client._agent_version = "0.83.0"
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        assert _options(client).get("strictMcpConfig") is True
        [message] = _project_mcp_notices(caplog)
        assert "cannot list" in message and "too old" not in message

    @pytest.mark.parametrize("kind", ["zero_tool", "side_chat"])
    @pytest.mark.parametrize("arrange", [_repo, _unknown])
    def test_a_session_already_strict_logs_no_project_notice(
        self, tmp_path, agents_dir, caplog, arrange, kind
    ):
        # A zero-tool or Side Chat session mounts no MCP server of any kind already.
        _write_spec(agents_dir, servers={}, tools=[])
        client = _client(arrange(tmp_path), side_read_only=kind == "side_chat")
        client._write_claude_local_settings()
        if kind == "zero_tool":
            client._spec_zero_tools = True
        else:
            client._claude_side_read_denies = ("Read(//secret/**)",)
        caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
        assert _options(client).get("strictMcpConfig") is True
        assert _project_mcp_notices(caplog) == []

    @pytest.mark.parametrize("arrange,strict", [(_repo, None), (_unknown, True)])
    def test_a_sharing_sibling_is_strict_exactly_when_a_name_is_unknown(
        self, tmp_path, agents_dir, arrange, strict
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        work = arrange(tmp_path)
        owner = _client(work)
        owner._write_claude_local_settings()
        assert owner._claude_settings_authored is True
        sibling = _client(work)
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True
        assert _options(owner).get("strictMcpConfig") is strict
        assert _options(sibling).get("strictMcpConfig") is strict

    @pytest.mark.parametrize("arrange,strict", [(_repo, None), (_unknown, True)])
    def test_a_create_race_loser_that_shares_is_strict_exactly_when_a_name_is_unknown(
        self, tmp_path, agents_dir, monkeypatch, arrange, strict
    ):
        from kiro_crew.acp import seed_provenance

        _write_spec(agents_dir, servers={}, tools=[])
        work = arrange(tmp_path)
        loser = _client(work)
        loser._claude_project_mcp_gate = _gate_module().child_project_mcp_gate(
            work, agent_env={}, extra_env={}
        )
        payload = loser._render_claude_settings_payload()
        path = work / ".claude" / "settings.local.json"
        real_open = os.open
        fired = []

        def _winner_creates_and_records(p, flags, *args, **kwargs):
            # The winner's seed lands, recorded, right under the loser's O_EXCL open.
            if not fired and os.fspath(p) == os.fspath(path) and flags & os.O_EXCL:
                fired.append(1)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
                assert seed_provenance.record(path, payload, "winner-session") is True
            return real_open(p, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _winner_creates_and_records)
        loser._write_claude_local_settings()
        assert fired
        assert loser._claude_settings_shared is True
        assert _options(loser).get("strictMcpConfig") is strict

    def test_a_raise_before_the_gate_answers_leaves_the_session_strict(
        self, tmp_path, agents_dir, monkeypatch
    ):
        _write_spec(agents_dir, servers={}, tools=[])
        client = _client(_repo(tmp_path))
        client._write_claude_local_settings()
        assert DISABLED_KEY in _options(client)["settings"]

        def _raise(*_args, **_kwargs):
            raise OSError("gate read failed")

        monkeypatch.setattr("kiro_crew.acp.client_claude_seed.child_project_mcp_gate", _raise)
        with pytest.raises(OSError):
            client._write_claude_local_settings()
        # Nothing of the earlier read is kept.
        assert client._claude_project_mcp_gate is None
        options = _options(client)
        assert options.get("strictMcpConfig") is True
        assert DISABLED_KEY not in options.get("settings", {})


class TestFork:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make,arrange,key",
        [
            (_repo, _foreign_local_settings, "settingSources"),
            pytest.param(_repo, _symlinked_claude_dir, "settings", marks=requires_symlinks),
            (_unknown, None, "strictMcpConfig"),
        ],
        ids=["excluded", "listed_inline", "strict"],
    )
    async def test_a_fork_carries_the_options_of_the_load(
        self, tmp_path, agents_dir, make, arrange, key
    ):
        # The adapter starts the fork's process, and its servers, before the load.
        _write_spec(agents_dir, servers={}, tools=[])
        work = make(tmp_path)
        if arrange is not None:
            arrange(work)
        client = _client(work)
        client._write_claude_local_settings()
        load = _options(client)
        assert key in load
        client._agent_version = "0.100.0"
        sent = []

        async def _send(method, params):
            sent.append(params)
            return 1

        client._send_request = _send
        client._wait_for_response = AsyncMock(return_value={"sessionId": "forked"})
        client._session_work_dir = AsyncMock(return_value=str(work))
        fork = await client._fork_session("src", "msg_1", {"sessionCapabilities": {"fork": {}}})
        assert fork == "forked"
        meta = sent[0]["_meta"]
        assert meta["claudeCode"]["options"] == load
        assert meta["jetbrains"] == {"air": {"fork": {"version": 1, "messageId": "msg_1"}}}
