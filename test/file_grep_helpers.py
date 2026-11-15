"""The fake streaming ripgrep child the /api/file-grep tests drive ``_grep_rg`` with."""

from __future__ import annotations

import subprocess

from kiro_crew.dashboard.handlers import files as f


def _rg_spawn(
    monkeypatch, *, lines=(), returncode=0, wait_raises=False, patch_wrap=True, made=None
):
    """Point `_grep_rg` at a fake streaming child.

    `_grep_rg` reads `proc.stdout` line by line, so the fake's stdout is any
    iterable of record lines -- including an endless generator, which is how the
    cap is tested without materializing anything.
    """
    if patch_wrap:
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "wrap_argv", lambda cmd: (cmd, None))
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)

    class _Pipe:
        """Iterable like a text pipe, and closable like one."""

        def __init__(self, source):
            self._it = iter(source)
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.closed:
                raise StopIteration
            return next(self._it)

        def close(self):
            self.closed = True

    class _Stdin:
        """Records what the pattern channel was handed.

        Not a `StringIO`: the code under test CLOSES stdin so rg sees EOF and
        starts searching, and a closed `StringIO` refuses `getvalue()`. The
        recorded text has to outlive the close for a test to read it.
        """

        def __init__(self):
            self.written = ""
            self.closed = False

        def write(self, text):
            self.written += text
            return len(text)

        def close(self):
            self.closed = True

    class _Proc:
        pid = 4242

        def __init__(self):
            self.stdout = _Pipe(lines)
            # The pattern is handed over stdin, so the fake has to accept it --
            # and record it, which is how the "never in the argv" test proves the
            # query still reaches the child.
            self.stdin = _Stdin()
            self._reaped = False

        def poll(self):
            # None means "still running", which is what makes teardown kill it.
            return None if not self._reaped else returncode

        def wait(self, timeout=None):
            if wait_raises and not self._reaped:
                self._reaped = True
                raise subprocess.TimeoutExpired(cmd="rg", timeout=timeout or 1.0)
            self._reaped = True
            return returncode

    def _spawn(argv, **kw):
        proc = _Proc()
        if made is not None:
            made.append(proc)
        return proc

    monkeypatch.setattr(f, "popen_limited", _spawn)
    # Every path that stops the child early goes through this, so the helper owns
    # it rather than each test remembering: the fake pid is not a real process.
    killed: list[int] = []
    monkeypatch.setattr(f.platform_compat, "kill_process_tree", lambda pid: killed.append(pid))
    return killed
