"""Tests for the PATH and env snapshot a script hook runs with."""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    run_script_hook,
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH layout")
def test_hook_env_path_gets_the_user_tool_dirs_kiro_cli_gets(monkeypatch, tmp_path):
    """A gateway started with a bare system PATH still lets a hook find ~/.local/bin."""
    from kiro_crew.hooks import _hook_path

    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    dirs = _hook_path(os.environ.get("PATH")).split(os.pathsep)

    assert str(local_bin) in dirs
    # System dirs keep precedence over the user-writable dirs added after them.
    assert dirs[:4] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    monkeypatch.delenv("PATH")
    unset = _hook_path(os.environ.get("PATH")).split(os.pathsep)
    assert unset[: len(os.defpath.split(os.pathsep))] == os.defpath.split(os.pathsep)
    assert str(local_bin) in unset


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH layout")
def test_hook_env_keeps_an_inherited_empty_path_entry(monkeypatch, tmp_path):
    """An empty PATH entry (the cwd) is not dropped when user dirs are added."""
    from kiro_crew.hooks import _hook_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin::/bin")
    assert _hook_path(os.environ.get("PATH")).startswith("/usr/bin::/bin")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH layout")
def test_hook_env_keeps_an_explicitly_empty_path(monkeypatch, tmp_path):
    """A set but empty PATH (the shell's cwd search) is kept, not replaced by defpath."""
    from kiro_crew.hooks import _hook_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    dirs = _hook_path(os.environ.get("PATH")).split(os.pathsep)

    # The defpath fallback would start with an absolute dir; "" is the cwd.
    assert dirs[0] == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH layout")
@pytest.mark.asyncio
async def test_a_home_change_during_the_build_cannot_split_home_from_path(monkeypatch, tmp_path):
    """A .env HOME change on the loop cannot give a hook one HOME and another's PATH.

    The change is queued on the loop as the env snapshot starts, and the PATH
    build waits for it when it runs off the loop. The hook's HOME and its home-rooted PATH entries must
    name the same home, whichever one that is.
    """
    import asyncio
    import threading

    from kiro_crew import env as kc_env
    from kiro_crew import hooks as script_hooks

    a, b = tmp_path / "a", tmp_path / "b"
    for home in (a, b):
        (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(a))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    loop_thread = threading.get_ident()
    changed = threading.Event()
    real_aug = kc_env.augmented_path
    real_env = script_hooks._hook_subprocess_env
    built: list[dict] = []

    def change_home():
        monkeypatch.setenv("HOME", str(b))
        changed.set()

    def aug(*args, **kwargs):
        if threading.get_ident() != loop_thread:
            changed.wait(5)
        return real_aug(*args, **kwargs)

    loop = asyncio.get_running_loop()

    def env_spy(*args, **kwargs):
        # Queue the change just as the snapshot starts: a design that reads
        # the allowlist and the PATH inputs at two moments lets it land between.
        loop.call_soon_threadsafe(change_home)
        env = real_env(*args, **kwargs)
        built.append(env)
        return env

    monkeypatch.setattr(kc_env, "augmented_path", aug)
    monkeypatch.setattr(script_hooks, "_hook_subprocess_env", env_spy)
    hook = ScriptHook(
        id="home-split",
        name="home-split",
        event=HOOK_EVENT_USER_PROMPT_SUBMIT,
        command="echo ok",
    )
    await run_script_hook(hook, "ctx")

    home = built[0]["HOME"]
    other = str(b) if home == str(a) else str(a)
    dirs = built[0]["PATH"].split(os.pathsep)
    assert os.path.join(home, ".local", "bin") in dirs
    assert not [d for d in dirs if d.startswith(other + os.sep)]


@pytest.mark.asyncio
async def test_hook_env_and_path_are_built_in_one_pass_off_the_loop(monkeypatch):
    """The allowlist read and the PATH build run in the same worker thread."""
    import threading

    from kiro_crew import hooks as script_hooks

    loop_thread = threading.get_ident()
    env_threads: list[int] = []
    path_threads: list[int] = []
    real_env = script_hooks._hook_subprocess_env
    real_path = script_hooks._hook_path

    def env_spy(*args, **kwargs):
        env_threads.append(threading.get_ident())
        return real_env(*args, **kwargs)

    def path_spy(*args, **kwargs):
        path_threads.append(threading.get_ident())
        return real_path(*args, **kwargs)

    monkeypatch.setattr(script_hooks, "_hook_subprocess_env", env_spy)
    monkeypatch.setattr(script_hooks, "_hook_path", path_spy)
    hook = ScriptHook(
        id="one-pass",
        name="one-pass",
        event=HOOK_EVENT_USER_PROMPT_SUBMIT,
        command="echo ok",
    )
    await run_script_hook(hook, "ctx")

    assert env_threads and env_threads == path_threads
    assert loop_thread not in env_threads


def test_augmented_path_reads_only_the_mapping_it_is_given(monkeypatch, tmp_path):
    """Given a mapping, the PATH build reads no os.environ, and builds the same PATH."""
    from types import MappingProxyType

    from kiro_crew import env as kc_env
    from kiro_crew.hooks import _HOOK_PATH_INPUT_KEYS

    (tmp_path / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = kc_env.augmented_path("")
    frozen = MappingProxyType(
        {k: v for k in _HOOK_PATH_INPUT_KEYS if (v := os.environ.get(k)) is not None}
    )
    reads: list[str] = []

    class _Raising(dict):
        def _read(self, name):
            reads.append(str(name))
            raise AssertionError(f"os.environ read: {name}")

        def __getitem__(self, key):
            self._read(key)

        def get(self, key, default=None):
            self._read(key)

        def __contains__(self, key):
            self._read(key)

        def __iter__(self):
            self._read("<iter>")

    real = os.environ
    os.environ = _Raising()  # restored below, before pytest reads it again
    try:
        got = kc_env.augmented_path("", env=frozen)
    finally:
        os.environ = real

    assert reads == []
    assert got == expected


def test_a_tilde_data_home_override_expands_from_the_mapping(tmp_path):
    """Guard: a ``~/...`` KIROCREW_HOME in the mapping expands from the mapping's HOME.

    The live HOME differs and os.environ raises on any read, so the result can
    only come from the mapping.
    """
    from kiro_crew.config import paths

    # HOME and USERPROFILE both name the synthetic home: POSIX reads the first
    # and Windows the second, so the test reads no live variable on either.
    snap = {
        "HOME": str(tmp_path / "snap"),
        "USERPROFILE": str(tmp_path / "snap"),
        "KIROCREW_HOME": "~/x",
    }
    reads: list[str] = []

    class _Raising(dict):
        def _read(self, name):
            reads.append(str(name))
            raise AssertionError(f"os.environ read: {name}")

        def __getitem__(self, key):
            self._read(key)

        def get(self, key, default=None):
            self._read(key)

        def __contains__(self, key):
            self._read(key)

        def __iter__(self):
            self._read("<iter>")

    real = os.environ
    os.environ = _Raising()  # restored below, before pytest reads it again
    try:
        got = paths._valid_override_home(snap)
    finally:
        os.environ = real

    assert reads == []
    assert got == (tmp_path / "snap" / "x").resolve()
    assert str(got) != os.path.join(os.path.expanduser("~"), "x")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX password database")
def test_user_home_matches_expanduser_without_a_passwd_entry(monkeypatch):
    """A mapping without HOME and a uid with no passwd entry gives "~", not KeyError."""
    import pwd

    from kiro_crew.config.paths import user_home

    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", missing)
    monkeypatch.delenv("HOME", raising=False)

    assert user_home({}) == os.path.expanduser("~") == "~"


def test_hook_path_input_keys_cover_every_key_the_chain_reads():
    """Every variable the PATH chain reads from its mapping is in the hook snapshot."""
    import inspect
    import re

    from kiro_crew import env as kc_env
    from kiro_crew import env_node_windows
    from kiro_crew.config import paths
    from kiro_crew.hooks import _HOOK_PATH_INPUT_KEYS

    chain = (
        kc_env.augmented_path,
        kc_env.mise_data_dir,
        kc_env._xdg_config_dir,
        kc_env._windows_toolbox_root,
        env_node_windows.roots,
        paths.user_home,
        paths._valid_override_home,
    )
    keys = {kc_env._NODE_BIN_DIR_ENV}
    for fn in chain:
        src = inspect.getsource(fn)
        keys |= set(re.findall(r'\b(?:env|source)(?:\.get\(|\[)"([A-Za-z_]+)"', src))
        keys |= set(re.findall(r'"([A-Za-z_]+)" in env\b', src))
        keys |= set(re.findall(r'_base\("([A-Za-z_]+)"', src))

    assert {"HOME", "LOCALAPPDATA", "KIROCREW_HOME"} <= keys
    assert keys <= _HOOK_PATH_INPUT_KEYS, sorted(keys - _HOOK_PATH_INPUT_KEYS)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH layout")
def test_hook_env_unset_path_fallback_drops_relative_defpath_entries(monkeypatch, tmp_path):
    """With PATH unset, a relative os.defpath entry (the cwd) never reaches a hook."""
    from kiro_crew.hooks import _hook_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setattr(os, "defpath", ".:/bin:/usr/bin")
    dirs = _hook_path(os.environ.get("PATH")).split(os.pathsep)

    assert "." not in dirs
    assert dirs[:2] == ["/bin", "/usr/bin"]
