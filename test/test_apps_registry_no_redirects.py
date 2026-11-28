"""Registry/app git fetches isolate the network TRANSFER, not the local step.

Every path that starts a git network transfer for a registry/app repo runs a
validated absolute git with the user's git config out of scope for the TRANSFER
(empty temp HOME/XDG, system and global config off, ``-c http.followRedirects=false``),
so a user ``http.<url>.followRedirects``, ``url.<x>.insteadOf``, ``includeIf`` or
``credential.helper`` cannot redirect or re-authenticate the transfer. The LOCAL
step (checkout, ``merge --ff-only``, the LFS/filter smudge) runs under the normal
env, so an owner's LFS and filter drivers still materialise real content. A reused
checkout is updated through a fresh temp repo + a local fetch, so its own
``.git/config`` never reaches the network and its ``core.*`` is left untouched. An
OWNER-designated transfer re-injects a vetted subset of the user's config
(credential helpers + TLS CA), never followRedirects/insteadOf; an index-originated
transfer gets no subset.

Each test drives one fetch path against loopback servers and asserts the planted
redirect/insteadOf has no effect. These are red on origin/main, where the fetch
reads the user's global config and follows the redirect.
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import tempfile
import threading

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew.apps import git_install, registry
from kiro_crew.apps import routes_art as routes_art_mod
from kiro_crew.apps.registry_pipeline import checkout, indexes, manifests
from kiro_crew.apps.registry_pipeline.checkout import _HOOKS_NEUTRALIZER_ARGV, _trusted_git
from kiro_crew.apps.registry_pipeline.subprocess_env import anonymous_git_env, minimal_env
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_GIT = shutil.which("git") or ""


class _Recorder(http.server.BaseHTTPRequestHandler):
    status = 404
    location = ""
    paths: list[str]

    def do_GET(self):  # noqa: N802 - http.server API
        self.paths.append(self.path)
        self.send_response(self.status)
        if self.location:
            self.send_header("Location", self.location + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def redirecting(monkeypatch):
    """``(first_repo, first_paths, second_url, second_paths)``; first 302s to second."""
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    servers = []

    def serve(status, location=""):
        handler = type("_H", (_Recorder,), {"status": status, "location": location, "paths": []})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", handler.paths

    second, second_paths = serve(404)
    first, first_paths = serve(302, second)
    yield f"{first}/owner/demo.git", first_paths, second, second_paths
    for server in servers:
        server.shutdown()


class _Planted:
    def __init__(self, helper_ran):
        self.helper_ran = helper_ran

    @property
    def helper_called(self) -> bool:
        return self.helper_ran.exists()


@pytest.fixture
def planted(monkeypatch, tmp_path, redirecting):
    """Plant a user ``~/.gitconfig`` that enables redirects, rewrites the first host
    to the SECOND via ``insteadOf``, and names a recording ``credential.helper``.

    On this head none of it reaches the TRANSFER; on origin/main the owner fetch
    reads it and reaches the second host.
    """
    first_repo, _fp, second_url, _sp = redirecting
    first_base = first_repo.rsplit("/owner/demo.git", 1)[0]

    helper_ran = tmp_path / "helper-ran"
    helper = tmp_path / "cred-helper.sh"
    helper.write_text(f'#!/bin/sh\necho ran >> "{helper_ran}"\nexit 1\n', encoding="utf-8")
    helper.chmod(0o755)
    include = tmp_path / "evil-include.cfg"
    include.write_text(f'[http "{first_base}/"]\n\tfollowRedirects = true\n', encoding="utf-8")

    home = tmp_path / "planted-home"
    (home / ".config").mkdir(parents=True)
    (home / ".gitconfig").write_text(
        f'[http "{first_base}/"]\n\tfollowRedirects = true\n'
        f'[url "{second_url}/"]\n\tinsteadOf = "{first_base}/"\n'
        f"[credential]\n\thelper = {helper}\n"
        f'[includeIf "gitdir:/"]\n\tpath = {include}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return _Planted(helper_ran)


def _git(*args, cwd=None, env=None):
    subprocess.run([_GIT, *args], cwd=cwd, env=env, timeout=FALLBACK_TIMEOUT_SECS, check=True)


def _bare_env():
    return {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}


def _seed_upstream(tmp_path, name="up"):
    """A real git upstream served over file:// with one commit of app.json."""
    up = tmp_path / name
    up.mkdir()
    env = {
        **_bare_env(),
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    _git("init", "-q", "-b", "main", str(up), env=env)
    (up / "app.json").write_text('{"name":"demo","version":"1"}', encoding="utf-8")
    _git("-C", str(up), "add", ".", env=env)
    _git("-C", str(up), "commit", "-q", "-m", "c1", env=env)
    return up, env


def _passthrough_sandbox(monkeypatch, module):
    async def _passthrough(argv, **_kw):
        return argv, None

    monkeypatch.setattr(module, "wrap_argv_async", _passthrough)
    monkeypatch.setattr(module, "cgroup_scope_argv", lambda argv: argv)


# --- per fetch path: a planted redirect/insteadOf has no effect on the transfer ---


@pytest.mark.asyncio
async def test_a_pinned_commit_fetch_reads_no_user_config(redirecting, planted, tmp_path):
    first_repo, _fp, _s, second_paths = redirecting
    result = await checkout._git_fetch_commit(
        first_repo, "a" * 40, tmp_path / "dest", [], clone_env=minimal_env(), sandbox_mode="strict"
    )
    assert result is not None and result["ok"] is False
    assert second_paths == []


@pytest.mark.asyncio
async def test_an_isolate_transport_branch_fetch_reads_no_user_config(
    redirecting, planted, tmp_path
):
    first_repo, _fp, _s, second_paths = redirecting
    scheme, _, rest = first_repo.partition("://")
    cred_url = f"{scheme}://user:tok@{rest}"
    result = await checkout._git_fetch_branch(
        first_repo,
        "main",
        tmp_path / "dest",
        [],
        credential_target=cred_url,
        clone_env=minimal_env(),
        sandbox_mode="strict",
    )
    assert result is not None and result["ok"] is False
    assert second_paths == []


@pytest.mark.asyncio
async def test_the_store_art_prewarm_branch_fetch_reads_no_user_config(
    redirecting, planted, tmp_path
):
    first_repo, _fp, _s, second_paths = redirecting
    result = await checkout._git_fetch_branch(
        first_repo,
        "main",
        tmp_path / "dest",
        [],
        clone_env=minimal_env(),
        sandbox_mode="strict",
        mask_local_git_config=True,
        forward_ssh_auth_sock=True,
    )
    assert result is not None and result["ok"] is False
    assert second_paths == []


@pytest.mark.asyncio
async def test_the_install_clone_reads_no_user_config(redirecting, planted, monkeypatch, tmp_path):
    first_repo, _fp, _s, second_paths = redirecting
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True, raising=False)
    result = await registry._git_clone_or_pull(
        first_repo, "main", tmp_path / "demo", [], index_originated=True
    )
    assert result is not None and result["ok"] is False
    assert second_paths == []


@pytest.mark.asyncio
async def test_the_reused_checkout_pull_reads_no_config_it_did_not_write(
    redirecting, planted, monkeypatch, tmp_path
):
    first_repo, _fp, _s, second_paths = redirecting
    dest = tmp_path / "demo"
    dest.mkdir()
    _git("init", "-q", "-b", "main", str(dest), env=_bare_env())
    _git("-C", str(dest), "config", "user.email", "x@example.com", env=_bare_env())
    _git("-C", str(dest), "config", "user.name", "x", env=_bare_env())
    _git("-C", str(dest), "commit", "-q", "--allow-empty", "-m", "seed", env=_bare_env())
    _git("-C", str(dest), "remote", "add", "origin", first_repo, env=_bare_env())
    base = first_repo.rsplit("/owner/demo.git", 1)[0]
    with open(dest / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write(f'[http "{base}/"]\n\tfollowRedirects = true\n')
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True, raising=False)
    result = await registry._git_clone_or_pull(first_repo, "main", dest, [], index_originated=True)
    assert result is not None and result["ok"] is False
    assert second_paths == []


@pytest.mark.asyncio
async def test_a_reused_update_imports_objects_with_no_url(redirecting, monkeypatch, tmp_path):
    """The reused-checkout update brings the fetched objects into the checkout with
    NO URL, so a ``url.<x>.insteadOf`` in the checkout's own config that targets the
    throwaway transfer path cannot turn the import into a network fetch: the planted
    target gets zero requests, and the update still lands."""
    _first, _fp, second_url, second_paths = redirecting
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda u: True, raising=False)
    up, env = _seed_upstream(tmp_path)
    url = f"file://{up}"
    dest = tmp_path / "demo"
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    # Plant the attack in the CHECKOUT's local config: rewrite the transfer temp-dir
    # prefix to the recording loopback server.
    tmp_prefix = os.path.join(tempfile.gettempdir(), "kirocrew-xfer-")
    _git("-C", str(dest), "config", f"url.{second_url}/.insteadOf", tmp_prefix, env=_bare_env())
    (up / "app.json").write_text('{"name":"demo","version":"2"}', encoding="utf-8")
    _git("-C", str(up), "add", ".", env=env)
    _git("-C", str(up), "commit", "-qm", "c2", env=env)
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    assert second_paths == [], "the insteadOf target received a request"
    assert '"version":"2"' in (dest / "app.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_manifest_clone_reads_no_user_config(redirecting, planted, monkeypatch):
    first_repo, _fp, _s, second_paths = redirecting
    monkeypatch.setattr(manifests, "is_clone_host_trusted", lambda url: True)
    _passthrough_sandbox(monkeypatch, manifests)
    result = await manifests._fetch_app_manifest("owner/demo", "main", git_url=first_repo)
    assert result is None
    assert second_paths == []


@pytest.mark.asyncio
async def test_the_external_index_clone_reads_no_user_config(redirecting, planted, monkeypatch):
    first_repo, _fp, _s, second_paths = redirecting
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True, raising=False)
    monkeypatch.setattr(indexes, "_context_clone_sandbox_mode", lambda url: "strict")
    _passthrough_sandbox(monkeypatch, indexes)
    result = await indexes._fetch_external_registry_index(first_repo, "main")
    assert result is None
    assert second_paths == []


@pytest.mark.asyncio
async def test_the_blob_proxy_clone_reads_no_user_config(
    redirecting, planted, monkeypatch, tmp_path
):
    first_repo, _fp, _s, second_paths = redirecting
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True, raising=False)
    monkeypatch.setattr(routes_art_mod, "_context_clone_sandbox_mode", lambda url: "strict")
    monkeypatch.setattr(routes_art_mod, "_sel_credential_grant", lambda *a, **k: None)
    _passthrough_sandbox(monkeypatch, routes_art_mod)
    ok = await routes_art_mod._fetch_git_blob(
        "owner/demo",
        "main",
        "icon.png",
        tmp_path / "blob-cache",
        git_url=first_repo,
        owner_designated=True,
    )
    assert ok is False
    assert second_paths == []


# --- the local step runs under the normal env: filter/LFS smudge, SHA-256, core.* ---


@pytest.mark.asyncio
async def test_a_filter_driven_file_is_smudged_on_fresh_clone_and_update(monkeypatch, tmp_path):
    """The checkout (LOCAL step) runs the operator's smudge filter -- what Git LFS
    relies on -- because it keeps the normal env. If the checkout ran under the
    isolated transfer env the committed pointer would survive unsmudged."""
    up, env = _seed_upstream(tmp_path)
    (up / ".gitattributes").write_text("*.dat filter=demo\n", encoding="utf-8")
    (up / "f.dat").write_text("pointer\n", encoding="utf-8")
    _git("-C", str(up), "add", ".", env=env)
    _git("-C", str(up), "commit", "-q", "-m", "c2", env=env)
    # The smudge filter lives in the operator's global config (planted HOME).
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    # clean is the inverse of smudge so the round-trip leaves the working tree
    # clean (as Git LFS does), or the ff update would see a dirty file.
    (home / ".gitconfig").write_text(
        '[filter "demo"]\n\tsmudge = sed s/pointer/REALCONTENT/\n'
        "\tclean = sed s/REALCONTENT/pointer/\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True, raising=False)
    url = f"file://{up}"
    dest = tmp_path / "demo"
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    assert (dest / "f.dat").read_text(encoding="utf-8").strip() == "REALCONTENT"
    # update: new filtered content upstream, reused-checkout update smudges too.
    (up / "f.dat").write_text("pointer-two\n", encoding="utf-8")
    _git("-C", str(up), "commit", "-qam", "c3", env=env)
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    assert "REALCONTENT" in (dest / "f.dat").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_sha256_checkout_updates(monkeypatch, tmp_path):
    """A SHA-256 object-format checkout updates: the temp transfer repo is created
    with the checkout's own object format."""
    up = tmp_path / "up"
    up.mkdir()
    env = {
        **_bare_env(),
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    _git("init", "-q", "-b", "main", "--object-format=sha256", str(up), env=env)
    (up / "app.json").write_text("1", encoding="utf-8")
    _git("-C", str(up), "add", ".", env=env)
    _git("-C", str(up), "commit", "-q", "-m", "c1", env=env)
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True, raising=False)
    url = f"file://{up}"
    dest = tmp_path / "demo"
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    assert _run(dest, "rev-parse", "--show-object-format") == "sha256"
    (up / "app.json").write_text("2", encoding="utf-8")
    _git("-C", str(up), "commit", "-qam", "c2", env=env)
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    assert (dest / "app.json").read_text(encoding="utf-8") == "2"


@pytest.mark.asyncio
async def test_a_windows_style_reused_checkout_keeps_core_filemode(monkeypatch, tmp_path):
    """The reused-checkout update leaves the checkout's ``core.*`` untouched, so a
    Windows-style ``core.filemode=false`` survives (the old config-reset would have
    overwritten it)."""
    up, env = _seed_upstream(tmp_path)
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True, raising=False)
    url = f"file://{up}"
    dest = tmp_path / "demo"
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    _git("-C", str(dest), "config", "core.filemode", "false", env=_bare_env())
    _git("-C", str(dest), "config", "core.symlinks", "false", env=_bare_env())
    (up / "app.json").write_text('{"name":"demo","version":"2"}', encoding="utf-8")
    _git("-C", str(up), "commit", "-qam", "c2", env=env)
    assert await registry._git_clone_or_pull(url, "main", dest, [], index_originated=False) is None
    assert _run(dest, "config", "--local", "core.filemode") == "false"
    assert _run(dest, "config", "--local", "core.symlinks") == "false"


def _run(repo, *args):
    return subprocess.run(
        [_GIT, "-C", str(repo), *args],
        capture_output=True,
        env=_bare_env(),
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    ).stdout.strip()


# --- owner-designated keeps the credential helper; index-originated gets no subset ---


@pytest.fixture
def auth_demand(monkeypatch):
    """A loopback server that answers 401 so git invokes a credential helper."""
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)

    class _H(http.server.BaseHTTPRequestHandler):
        paths: list[str] = []

        def do_GET(self):  # noqa: N802
            type(self).paths.append(self.path)
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/owner/demo.git"
    yield url
    server.shutdown()


def _plant_helper_config(monkeypatch, tmp_path, second_url=""):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    helper_ran = tmp_path / "helper-ran"
    helper = tmp_path / "helper.sh"
    helper.write_text(f'#!/bin/sh\necho ran >> "{helper_ran}"\nexit 1\n', encoding="utf-8")
    helper.chmod(0o755)
    cfg = f"[credential]\n\thelper = {helper}\n"
    if second_url:
        cfg += f'[http "{second_url}/"]\n\tfollowRedirects = true\n'
    (home / ".gitconfig").write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return helper_ran


@pytest.mark.asyncio
async def test_owner_designated_fetch_keeps_the_credential_helper(
    auth_demand, monkeypatch, tmp_path
):
    helper_ran = _plant_helper_config(monkeypatch, tmp_path, second_url=auth_demand)
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True, raising=False)
    await registry._git_clone_or_pull(
        auth_demand, "main", tmp_path / "demo", [], index_originated=False
    )
    # Owner-designated: the operator's credential.helper is re-injected, so the 401
    # makes git invoke it. The planted followRedirects had no effect on the transfer.
    assert helper_ran.exists(), "owner credential.helper was not invoked"


@pytest.mark.asyncio
async def test_index_originated_fetch_never_calls_the_helper(auth_demand, monkeypatch, tmp_path):
    helper_ran = _plant_helper_config(monkeypatch, tmp_path)
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda url: True, raising=False)
    await registry._git_clone_or_pull(
        auth_demand, "main", tmp_path / "demo", [], index_originated=True
    )
    assert not helper_ran.exists(), "index-originated fetch invoked a user credential helper"


@pytest.fixture
def auth_recorder(monkeypatch):
    """A loopback 401 server that records the ``Authorization`` header of each request."""
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)

    class _H(http.server.BaseHTTPRequestHandler):
        auths: list[str | None] = []

        def do_GET(self):  # noqa: N802
            type(self).auths.append(self.headers.get("Authorization"))
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/owner/demo.git"
    yield url, _H.auths
    server.shutdown()


def _plant_store_credentials(monkeypatch, tmp_path, url):
    """Plant ``credential.helper=store`` and a ``~/.git-credentials`` for *url*'s host."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n", encoding="utf-8")
    host_port = url.partition("://")[2].split("/", 1)[0]
    (home / ".git-credentials").write_text(f"http://user:pw@{host_port}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


@pytest.mark.parametrize("index_originated", [False, True], ids=["owner", "index"])
@pytest.mark.asyncio
async def test_credential_store_is_read_only_for_an_owner_designated_transfer(
    auth_recorder, monkeypatch, tmp_path, index_originated
):
    """``credential.helper=store`` reads ``~/.git-credentials`` from HOME. The owner
    transfer keeps the real HOME so the store authenticates; the index transfer gets
    an empty HOME, so the store has nothing and no credential is ever sent."""
    url, auths = auth_recorder
    _plant_store_credentials(monkeypatch, tmp_path, url)
    monkeypatch.setattr(checkout, "is_clone_host_trusted", lambda u: True, raising=False)
    await registry._git_clone_or_pull(
        url, "main", tmp_path / "demo", [], index_originated=index_originated
    )
    sent = [a for a in auths if a and a.lower().startswith("basic ")]
    if index_originated:
        assert not sent, "index-originated transfer read the owner's credential store"
    else:
        assert sent, "owner-designated transfer did not authenticate from credential.helper=store"


# --- gh config dir, trusted git, CLI parity ---


def test_an_explicit_gh_config_dir_survives(monkeypatch):
    from kiro_crew.apps.registry_pipeline import gh_credential

    monkeypatch.setenv("GH_CONFIG_DIR", "/explicit/gh/dir")
    env = gh_credential._gh_credential_env(
        {"GIT_CONFIG_COUNT": "0"}, shutil.which("sh") or "/bin/sh"
    )
    # _gh_credential_env forwards an explicit GH_CONFIG_DIR; run() only defaults it
    # when absent, so an operator's explicit dir is never overwritten.
    assert env.get("GH_CONFIG_DIR") == "/explicit/gh/dir"


def test_the_trusted_git_is_absolute(monkeypatch):
    checkout._TRUSTED_GIT.clear()
    resolved = _trusted_git()
    assert os.path.isabs(resolved) and os.path.basename(resolved) == "git"


def _ls_remote(config_argv, url, cwd):
    return subprocess.run(
        [_GIT, *config_argv, "ls-remote", url],
        capture_output=True,
        env=anonymous_git_env(),
        cwd=cwd,
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def test_the_registry_argv_does_not_follow_a_redirect(redirecting, tmp_path):
    first_repo, first_paths, _s, second_paths = redirecting
    result = _ls_remote(_HOOKS_NEUTRALIZER_ARGV, first_repo, tmp_path)
    assert result.returncode != 0
    assert first_paths
    assert second_paths == []


def test_the_cli_install_argv_still_follows_a_redirect(redirecting, tmp_path):
    first_repo, first_paths, _s, second_paths = redirecting
    config_argv = [*git_install._git_config_argv(), "-c", "protocol.http.allow=always"]
    _ls_remote(config_argv, first_repo, tmp_path)
    assert first_paths
    assert second_paths
