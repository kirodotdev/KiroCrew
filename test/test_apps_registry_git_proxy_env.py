"""App Store git fetches see the operator's proxy; nothing else does.

The trusted ``git`` network spawn (clone, fetch, pull) gets the proxy and CA-bundle
variables, so an install behind ``HTTPS_PROXY`` reaches the remote. App install
scripts, builds and the local git steps never do, because a proxy URL can carry
``user:password@``. A fetch that cannot reach the remote says so, without echoing
the proxy URL. The module-wide autouse fixtures come from ``apps_registry_helpers.py``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew.apps import registry
from kiro_crew.apps.registry_pipeline.git_targets import (
    _GIT_NETWORK_FAILURE_HINT,
    _git_network_failure_hint,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import (
    _git_network_env,
    _proxy_env_has_credentials,
    anonymous_git_env,
    minimal_env,
)

_PROXY_SECRET = "ProxyPassw0rd"
_PROXY_VARS = {
    "HTTPS_PROXY": f"http://corp-user:{_PROXY_SECRET}@proxy.example.invalid:3128",
    "https_proxy": f"http://corp-user:{_PROXY_SECRET}@proxy.example.invalid:3128",
    "NO_PROXY": "localhost,127.0.0.1",
    "SSL_CERT_FILE": "/etc/corp/ca.pem",
    "GIT_SSL_CAINFO": "/etc/corp/ca.pem",
}


_PLAIN_PROXY = "http://proxy.example.invalid:3128"


@pytest.fixture
def proxy_env(monkeypatch):
    """A proxy URL that carries a password, plus a CA bundle."""
    for key, value in _PROXY_VARS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")


@pytest.fixture
def plain_proxy_env(monkeypatch):
    """A proxy URL with no credential."""
    for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _PLAIN_PROXY)
    monkeypatch.setenv("https_proxy", _PLAIN_PROXY)


def test_git_network_env_adds_proxy_and_ca_variables(proxy_env):
    env = _git_network_env({"BASE": "1", "NO_PROXY": "explicit"}, "https://example.com/o/r.git")

    assert env["BASE"] == "1"
    assert env["HTTPS_PROXY"] == _PROXY_VARS["HTTPS_PROXY"]
    assert env["SSL_CERT_FILE"] == _PROXY_VARS["SSL_CERT_FILE"]
    assert env["GIT_SSL_CAINFO"] == _PROXY_VARS["GIT_SSL_CAINFO"]
    # A caller's explicit value wins over the process env.
    assert env["NO_PROXY"] == "explicit"
    # Turning verification off is never forwarded.
    assert "GIT_SSL_NO_VERIFY" not in env


@pytest.mark.parametrize(
    "git_url", ["git@example.com:o/r.git", "ssh://git@example.com/o/r.git", "git://example.com/r"]
)
def test_non_http_remotes_get_no_proxy_and_no_split(proxy_env, git_url):
    """Only git's HTTP transport reads the proxy, so SSH keeps its combined path."""
    assert _git_network_env({"BASE": "1"}, git_url) == {"BASE": "1"}
    assert _proxy_env_has_credentials(git_url) is False


@pytest.mark.parametrize("builder", [minimal_env, anonymous_git_env])
def test_install_script_environments_carry_no_proxy_or_ca_variables(proxy_env, builder):
    env = builder()

    assert _PROXY_SECRET not in repr(env)
    for key in (*_PROXY_VARS, "GIT_SSL_NO_VERIFY"):
        assert key not in env


@pytest.mark.asyncio
async def test_pinned_fetch_gives_proxy_only_to_the_network_step(proxy_env, monkeypatch, tmp_path):
    dest = tmp_path / "demo"
    commit = "a" * 40
    spawned: list[tuple[list[str], dict[str, str]]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs["env"]))
        if "init" in argv:
            (dest / ".git").mkdir(parents=True)
        return _Proc()

    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kw: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: commit)

    result = await registry._git_fetch_commit(
        "https://example.com/o/demo.git",
        commit,
        dest,
        [],
        clone_env=anonymous_git_env(),
        sandbox_mode="strict",
    )

    assert result is None
    fetches = [env for argv, env in spawned if "fetch" in argv]
    local_steps = [env for argv, env in spawned if "fetch" not in argv]
    assert len(fetches) == 1 and local_steps
    assert fetches[0]["HTTPS_PROXY"] == _PROXY_VARS["HTTPS_PROXY"]
    assert fetches[0]["SSL_CERT_FILE"] == _PROXY_VARS["SSL_CERT_FILE"]
    # The credential-free posture of the index-originated clone is unchanged.
    assert fetches[0]["GIT_CONFIG_GLOBAL"] == anonymous_git_env()["GIT_CONFIG_GLOBAL"]
    for env in local_steps:
        assert _PROXY_SECRET not in repr(env)


@pytest.mark.asyncio
async def test_network_fetch_output_is_english_whatever_the_locale(
    proxy_env, monkeypatch, tmp_path
):
    """The missing-ref retry and the hint read git's English wording."""
    from kiro_crew.apps.registry_pipeline.subprocess_env import _GIT_CLONE_LOCALE

    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    dest = tmp_path / "demo"
    spawned: list[tuple[list[str], dict[str, str]]] = []

    class _Proc:
        returncode = 0
        pid = 4244

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs["env"]))
        if "init" in argv:
            (dest / ".git").mkdir(parents=True)
        return _Proc()

    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kw: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)

    await registry._git_fetch_branch(
        "https://example.com/o/demo.git",
        "main",
        dest,
        [],
        clone_env=minimal_env(),
        sandbox_mode="strict",
    )

    assert [env["LC_ALL"] for argv, env in spawned if "fetch" in argv] == [_GIT_CLONE_LOCALE]
    assert {env["LC_ALL"] for argv, env in spawned if "fetch" not in argv} == {"de_DE.UTF-8"}


@pytest.mark.parametrize(
    "output",
    [
        "fatal: unable to access 'https://h/x.git/': Could not resolve proxy: proxy.corp",
        "fatal: unable to access 'https://h/x.git/': Received HTTP code 407 from proxy after CONNECT",
        "fatal: unable to access 'https://h/x.git/': Failed to connect to h port 443",
        "fatal: unable to access 'https://h/x.git/': Could not resolve host: h",
        "timed out",
        "git transport failed: the proxy refused the connection (details redacted)",
    ],
)
def test_unreachable_remote_output_gets_the_network_hint(output):
    assert _git_network_failure_hint(output) == _GIT_NETWORK_FAILURE_HINT


@pytest.mark.parametrize(
    "output",
    [
        "fatal: couldn't find remote ref deadbeef",
        "remote: Repository not found.",
        "fatal: unable to access 'https://h/proxy-tools.git/': The requested URL returned error: 404",
        "remote: build timed out\nfatal: couldn't find remote ref deadbeef",
    ],
)
def test_other_git_failures_get_no_network_hint(output):
    assert _git_network_failure_hint(output) == ""


class _RefusingProxy:
    """A loopback HTTP proxy that records the CONNECT line and answers 407."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(30)
        self.port = self.sock.getsockname()[1]
        self.request_lines: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.request_lines.append(data.split(b"\r\n", 1)[0].decode("latin-1"))
                conn.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass

    def close(self) -> None:
        self.sock.close()
        self.thread.join(timeout=10)


@pytest.mark.asyncio
async def test_real_git_fetch_reaches_the_proxy_and_reports_it_without_credentials(
    monkeypatch, tmp_path
):
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=15)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is unavailable")
    proxy = _RefusingProxy()
    try:
        proxy_url = f"http://corp-user:{_PROXY_SECRET}@127.0.0.1:{proxy.port}"
        for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(key, proxy_url)
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kw: (cmd, None))
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
        log: list[str] = []

        result = await registry._git_fetch_commit(
            "https://example.invalid/o/demo.git",
            "b" * 40,
            tmp_path / "demo",
            log,
            # Global and system git config are off here, so the environment is the
            # only proxy setting git can see.
            clone_env=anonymous_git_env(),
            sandbox_mode="strict",
        )
    finally:
        proxy.close()

    assert proxy.request_lines and proxy.request_lines[0].startswith("CONNECT example.invalid:443")
    assert result is not None and result["ok"] is False
    assert result["error"].startswith("git fetch failed (exit ")
    assert result["error"].endswith(f": could not reach the remote; {_GIT_NETWORK_FAILURE_HINT}")
    assert _PROXY_SECRET not in result["error"]
    assert "corp-user" not in result["error"]
    # The proxy carries a password, so the fetch output is reduced to fixed labels.
    assert _PROXY_SECRET not in "\n".join(log)


class _FailedClone:
    returncode = 128
    pid = 4243

    async def communicate(self):
        return b"", b"fatal: Could not resolve proxy: proxy.example.invalid\n"

    def kill(self):
        pass


@pytest.mark.asyncio
async def test_registry_index_clone_gets_the_proxy(plain_proxy_env, monkeypatch):
    import kiro_crew.apps.registry_pipeline.indexes as indexes

    seen: list[dict[str, str]] = []

    async def _passthrough_wrap(argv, **_kw):
        return list(argv), None

    async def _fake_spawn(*argv, **kwargs):
        seen.append(kwargs["env"])
        return _FailedClone()

    monkeypatch.setattr(indexes, "wrap_argv_async", _passthrough_wrap)
    monkeypatch.setattr(indexes, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(indexes, "create_subprocess_limited", _fake_spawn)

    assert (
        await indexes._fetch_external_registry_index("https://example.com/o/registry.git", "main")
        is None
    )
    assert len(seen) == 1
    assert seen[0]["HTTPS_PROXY"] == _PLAIN_PROXY


@pytest.mark.asyncio
async def test_store_art_blob_clone_gets_the_proxy(plain_proxy_env, monkeypatch, tmp_path):
    import kiro_crew.apps.registry as reg_mod
    import kiro_crew.apps.routes as routes

    seen: list[dict[str, str]] = []

    async def _passthrough_wrap(argv, **_kw):
        return list(argv), None

    async def _fake_spawn(*argv, **kwargs):
        seen.append(kwargs["env"])
        return _FailedClone()

    monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(routes, "wrap_argv_async", _passthrough_wrap)
    monkeypatch.setattr(routes, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(routes, "create_subprocess_limited", _fake_spawn)

    assert not await routes._fetch_git_blob(
        "acme",
        "main",
        "assets/logo.png",
        tmp_path / "out.png",
        git_url="https://example.com/o/demo.git",
    )
    assert len(seen) == 1
    assert seen[0]["HTTPS_PROXY"] == _PLAIN_PROXY
    # The anonymous posture of the browse-time clone is unchanged.
    assert seen[0]["GIT_CONFIG_GLOBAL"] == anonymous_git_env()["GIT_CONFIG_GLOBAL"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://corp-user:pw@proxy:3128", True),
        ("corp-user:pw@proxy:3128", True),
        ("http://corp-user@proxy:3128/", True),
        ("http://proxy:3128", False),
        ("proxy:3128", False),
        ("http://corp-user:pa/ss@proxy:3128", True),
        ("http://proxy:3128/a@b", True),
    ],
)
def test_proxy_credential_detection(monkeypatch, value, expected):
    for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", value)

    assert _proxy_env_has_credentials("https://example.com/o/r.git") is expected


def _refuse_combined_git(argv):
    """Fail the test if a combined clone or pull (fetch plus checkout) is spawned."""
    words = [part for part in argv if not str(part).startswith("-")]
    assert "clone" not in words and "pull" not in words, words


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_checkout", [False, True])
async def test_proxy_password_forces_the_split_install_fetch(
    proxy_env, monkeypatch, tmp_path, existing_checkout
):
    """A checkout-time filter must never inherit a proxy password.

    So with one set, both a fresh branch install and an update of a same-origin
    checkout take the split path: fetch alone gets the proxy, and an existing
    checkout is moved aside rather than pulled in place.
    """
    public_url = "https://example.com/o/demo.git"
    dest = tmp_path / "demo"
    if existing_checkout:
        (dest / ".git").mkdir(parents=True)
    calls: list[bool] = []

    async def _origin(path):
        return public_url

    async def _split_fetch(*args, **kwargs):
        calls.append(dest.exists())
        return {"ok": False, "error": "fetch failed"}

    async def _spawn(*argv, **kwargs):
        _refuse_combined_git(argv)
        raise AssertionError(f"unexpected spawn {argv}")

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "_clone_origin_url", _origin)
    monkeypatch.setattr(registry, "_git_fetch_branch", _split_fetch)
    monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)

    result = await registry._git_clone_or_pull(public_url, "main", dest, [])

    assert result == {"ok": False, "error": "fetch failed"}
    # The existing checkout was isolated before the network fetch, not pulled.
    assert calls == [False]


@pytest.mark.asyncio
async def test_proxy_password_forces_the_split_index_and_blob_fetch(
    proxy_env, monkeypatch, tmp_path
):
    import kiro_crew.apps.registry as reg_mod
    import kiro_crew.apps.registry_pipeline.indexes as indexes
    import kiro_crew.apps.routes as routes

    split: list[str] = []

    async def _split_fetch(git_url, *args, **kwargs):
        split.append(git_url)
        return {"ok": False, "error": "fetch failed"}

    async def _spawn(*argv, **kwargs):
        _refuse_combined_git(argv)
        raise AssertionError(f"unexpected spawn {argv}")

    monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)
    for module in (indexes, routes):
        monkeypatch.setattr(module, "_git_fetch_branch", _split_fetch)
        monkeypatch.setattr(module, "create_subprocess_limited", _spawn)

    index_url = "https://example.com/o/registry.git"
    blob_url = "https://example.com/o/demo.git"
    assert await indexes._fetch_external_registry_index(index_url, "main") is None
    assert not await routes._fetch_git_blob(
        "acme", "main", "assets/logo.png", tmp_path / "out.png", git_url=blob_url
    )
    assert split == [index_url, blob_url]


@pytest.mark.asyncio
async def test_split_branch_fetch_also_takes_a_tag_like_git_clone_branch(monkeypatch, tmp_path):
    """A proxy password sends ``git clone --branch <tag>`` installs down the split path.

    That path must accept a tag the way the clone did: detached at the tag, no
    tracking branch.
    """
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    dest = tmp_path / "checkout"
    work.mkdir()

    def _git(*args, cwd):
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    _git("init", "--quiet", "--template=", cwd=work)
    _git("checkout", "--quiet", "-b", "main", cwd=work)
    (work / "payload.txt").write_text("tagged payload\n", encoding="utf-8")
    _git("add", "payload.txt", cwd=work)
    _git(
        "-c",
        "user.name=Registry Test",
        "-c",
        "user.email=registry@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "release",
        cwd=work,
    )
    _git("tag", "v1.0.0", cwd=work)
    tagged = _git("rev-parse", "HEAD", cwd=work)
    _git("clone", "--quiet", "--bare", str(work), str(remote), cwd=tmp_path)

    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kw: (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    log: list[str] = []
    result = await registry._git_fetch_branch(
        str(remote), "v1.0.0", dest, log, clone_env=minimal_env(), sandbox_mode="strict"
    )

    assert result is None, log
    assert _git("rev-parse", "HEAD", cwd=dest) == tagged
    assert _git("branch", "--show-current", cwd=dest) == ""
    assert (dest / "payload.txt").read_text(encoding="utf-8") == "tagged payload\n"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("fatal: couldn't find remote ref refs/heads/v1.0.0", True),
        ("git transport failed: the requested ref does not exist (details redacted)", True),
        ("fatal: unable to access 'https://h/x.git/': Could not resolve host: h", False),
    ],
)
def test_missing_ref_detection(output, expected):
    assert registry._names_a_missing_ref(output) is expected
