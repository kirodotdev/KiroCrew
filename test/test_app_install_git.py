"""``kirocrew app install <git-url>``: clone with the operator's git, then a folder install.

``apps.git_install`` validates the URL, clones it in the CLI process and finds the
folder holding ``app.json``; ``cli_commands_app._resolve_install_source`` hands that
folder to the unchanged local-directory install. The clone is stubbed at
``_run_clone`` except in ``TestRealGit``, which clones a local repository with the
exact command line (only the ``file`` protocol is added for it).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew import app_lifecycle_client, platform_compat
from kiro_crew.apps import git_install
from kiro_crew.apps.git_install import (
    GitInstallError,
    clone_argv,
    fetch_app_from_git,
    is_git_install_source,
    locate_app_dir,
    looks_like_git_url,
    validate_git_url,
    validate_ref,
    validate_subdirectory,
)
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, _read_installed
from kiro_crew.cli_commands import _handle_app, _print_git_update_hint, _shell_quoted
from kiro_crew.cli_parser_agent import register_app_parser
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import default_timeout

APP = "git-test-app"
URL = "git@github.com:example/git-test-app.git"


def _write_app(folder: Path, name: str = APP) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Git Test App",
        "description": "installed from a git URL",
        "author": "tester",
    }
    (folder / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return folder


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    data_home = tmp_path / "kirocrew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod

    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(git_install, "_git_on_path", lambda: True)
    return data_home


def _stub_clone(monkeypatch, layout, calls: list | None = None, branch: str = "main") -> None:
    """Make ``_run_clone`` write *layout(dest)* instead of running git."""

    def fake(url: str, dest: Path, ref: str | None) -> None:
        if calls is not None:
            calls.append((url, dest, ref))
        layout(dest)

    monkeypatch.setattr(git_install, "_run_clone", fake)
    monkeypatch.setattr(git_install, "_cloned_head", lambda _checkout: ("a" * 40, branch))


def _root_entries(home: Path) -> set[str]:
    root = home / git_install.GIT_CHECKOUTS_DIRNAME
    return {p.name for p in root.iterdir()} if root.exists() else set()


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/example/app",
            "https://gitlab.com/group/sub/app.git",
            "ssh://git@gitlab.com/group/app.git",
            "ssh://gitlab.com:2222/group/app.git",
            "git+ssh://git@github.com/example/app.git",
            "git@github.com:example/app.git",
        ],
    )
    def test_accepts_https_ssh_and_scp_remotes(self, url: str) -> None:
        assert validate_git_url(url) == url

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("http://github.com/example/app", "plaintext"),
            ("git://github.com/example/app", "plaintext"),
            ("file:///srv/app", "unsupported"),
            ("ext::sh", "unsupported"),
            ("https://token@github.com/example/app", "password or token"),
            ("https://user:pass@github.com/example/app", "password or token"),
            ("ssh://user:pass@gitlab.com/group/app", "colon in its user name"),
            ("user:pass@github.com:example/app", "colon in its user name"),
            ("https://github.com/example/app?ref=x", "query"),
            ("https://github.com/example/app#main", "fragment"),
            ("git@github.com:example/app\n", "whitespace"),
            ("https://github.com", "unsupported"),
            ("https://github.com/", "unsupported"),
            ("", "empty"),
            ("https://github.com/" + "a" * git_install.MAX_URL_LEN, "too long"),
        ],
    )
    def test_refuses_before_git_runs(self, url: str, reason: str) -> None:
        with pytest.raises(GitInstallError, match=reason):
            validate_git_url(url)

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("git@github.com:example/app.git", True),
            ("https://github.com/example/app", True),
            ("http://github.com/example/app", True),
            ("ext::sh", True),
            ("./my-app", False),
            ("/srv/apps/my-app", False),
            ("C:\\apps\\my-app", False),
            ("my-app", False),
        ],
    )
    def test_recognises_remote_spellings(self, source: str, expected: bool) -> None:
        assert looks_like_git_url(source) is expected

    @pytest.mark.skipif(os.sep != "/", reason="a colon is not a name character on Windows")
    def test_an_existing_folder_named_like_a_remote_installs_as_a_folder(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "me@host:app").mkdir()
        assert looks_like_git_url("me@host:app")
        assert not is_git_install_source("me@host:app")
        assert is_git_install_source("me@host:other-app")


class TestRefAndSubdirectory:
    @pytest.mark.parametrize("ref", ["main", "v1.2.0", "release/2026-11", "feature_x"])
    def test_accepts_branch_and_tag_names(self, ref: str) -> None:
        assert validate_ref(ref) == ref

    @pytest.mark.parametrize("ref", ["-c", "a..b", "main\n", "x.lock", "x/", "", "--upload-pack=x"])
    def test_refuses_what_is_not_a_ref(self, ref: str) -> None:
        with pytest.raises(GitInstallError, match="not a branch or tag"):
            validate_ref(ref)

    def test_no_ref_means_the_default_branch(self) -> None:
        assert validate_ref(None) is None

    @pytest.mark.parametrize(
        ("subdirectory", "parts"),
        [
            ("app", ("app",)),
            ("apps/my-app", ("apps", "my-app")),
            ("apps\\my-app/", ("apps", "my-app")),
        ],
    )
    def test_accepts_relative_folders(self, subdirectory: str, parts: tuple[str, ...]) -> None:
        assert validate_subdirectory(subdirectory) == parts

    @pytest.mark.parametrize(
        "subdirectory", ["../app", "a/../b", "/etc", "\\app", "C:/app", ".", ""]
    )
    def test_refuses_a_folder_outside_the_clone(self, subdirectory: str) -> None:
        with pytest.raises(GitInstallError, match="relative folder"):
            validate_subdirectory(subdirectory)


class TestCloneCommand:
    def test_runs_nothing_the_repository_supplies(self, tmp_path) -> None:
        argv = clone_argv("https://github.com/example/app", tmp_path / "dest", None)
        configs = {argv[i + 1] for i, token in enumerate(argv) if token == "-c"}
        assert f"core.hooksPath={os.devnull}" in configs
        assert "core.fsmonitor=false" in configs
        assert {
            "protocol.allow=never",
            "protocol.https.allow=always",
            "protocol.ssh.allow=always",
        } <= configs
        assert "submodule.recurse=false" in configs
        clone_at = argv.index("clone")
        options = argv[clone_at:]
        assert {"--no-recurse-submodules", "--template=", "--single-branch"} <= set(options)
        assert options[options.index("--depth") + 1] == "1"
        assert "--branch" not in options

    def test_the_url_follows_the_end_of_options(self, tmp_path) -> None:
        dest = tmp_path / "dest"
        argv = clone_argv("git@github.com:example/app.git", dest, "v1")
        assert argv[-3:] == ["--", "git@github.com:example/app.git", str(dest)]
        assert argv[argv.index("--branch") + 1] == "v1"

    def test_keeps_the_operators_credentials_and_drops_repository_pointers(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
            monkeypatch.setenv(key, "/elsewhere")
        env = git_install._clone_env()
        assert env["SSH_AUTH_SOCK"] == "/run/agent.sock"
        assert env["GIT_SSH_COMMAND"] == "ssh -i key"
        assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"} & set(env)

    def test_a_failed_clone_says_to_check_the_operators_own_git(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            git_install.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 128),
        )
        with pytest.raises(GitInstallError, match=r"exit 128.*`git clone`"):
            git_install._run_clone(URL, tmp_path / "dest", None)

    def test_a_hung_clone_is_stopped(self, tmp_path, monkeypatch) -> None:
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(git_install.subprocess, "run", hang)
        with pytest.raises(GitInstallError, match="did not finish"):
            git_install._run_clone(URL, tmp_path / "dest", None)


class TestLocateAppDir:
    def test_the_repository_root(self, tmp_path) -> None:
        _write_app(tmp_path)
        assert locate_app_dir(tmp_path, ()) == tmp_path

    def test_the_one_top_level_folder_holding_an_app(self, tmp_path) -> None:
        _write_app(tmp_path / "app")
        (tmp_path / "infra").mkdir()
        assert locate_app_dir(tmp_path, ()) == tmp_path / "app"

    def test_several_candidates_ask_for_a_subdirectory(self, tmp_path) -> None:
        _write_app(tmp_path / "zeta")
        _write_app(tmp_path / "alpha")
        with pytest.raises(GitInstallError, match=r"\(alpha, zeta\).*--subdirectory"):
            locate_app_dir(tmp_path, ())

    def test_no_candidate_asks_for_a_subdirectory(self, tmp_path) -> None:
        _write_app(tmp_path / "apps" / "deep")
        with pytest.raises(GitInstallError, match="no app.json.*--subdirectory"):
            locate_app_dir(tmp_path, ())

    def test_a_named_nested_folder(self, tmp_path) -> None:
        _write_app(tmp_path / "apps" / "deep")
        assert locate_app_dir(tmp_path, ("apps", "deep")) == tmp_path / "apps" / "deep"

    def test_a_named_folder_without_an_app(self, tmp_path) -> None:
        (tmp_path / "apps").mkdir()
        with pytest.raises(GitInstallError, match="apps/app.json not found"):
            locate_app_dir(tmp_path, ("apps",))
        with pytest.raises(GitInstallError, match="no folder 'missing'"):
            locate_app_dir(tmp_path, ("missing",))

    def test_a_directory_link_is_never_followed_out_of_the_clone(self, tmp_path) -> None:
        # A junction on Windows, a symlink elsewhere: both must stay refused.
        outside = _write_app(tmp_path / "outside")
        clone = tmp_path / "clone"
        clone.mkdir()
        make_dir_link(clone / "app", outside)
        with pytest.raises(GitInstallError, match="link is not followed"):
            locate_app_dir(clone, ("app",))
        with pytest.raises(GitInstallError, match="no app.json"):
            locate_app_dir(clone, ())

    def test_a_linked_manifest_is_not_a_candidate(self, tmp_path) -> None:
        # A file link needs a real symlink: listed in test/requires-real-symlinks.txt,
        # so the root conftest skips it only on a host that cannot create one.
        outside = _write_app(tmp_path / "outside")
        clone = tmp_path / "clone"
        clone.mkdir()
        (clone / APP_MANIFEST_FILENAME).symlink_to(outside / APP_MANIFEST_FILENAME)
        with pytest.raises(GitInstallError, match="no app.json"):
            locate_app_dir(clone, ())


class TestFetch:
    def test_clones_into_a_new_folder_of_its_own(self, home, monkeypatch) -> None:
        calls: list = []
        _stub_clone(monkeypatch, lambda dest: _write_app(dest / "app"), calls)
        fetched = fetch_app_from_git(URL, ref="v1")
        checkout = fetched.checkout
        assert checkout.parent == home / git_install.GIT_CHECKOUTS_DIRNAME
        assert checkout.name.startswith("git-test-app-")
        assert fetched.name == APP
        assert fetched.app_dir == checkout / "app"
        assert (fetched.app_dir / APP_MANIFEST_FILENAME).is_file()
        assert (fetched.commit, fetched.branch) == ("a" * 40, "main")
        assert _root_entries(home) == {checkout.name}
        assert calls == [(URL, checkout, "v1")]

    @pytest.mark.parametrize(
        ("url", "prefix"),
        [
            (URL, "git-test-app-"),
            ("https://gitlab.com/group/sub/my.app/", "my-app-"),
            ("ssh://git@gitlab.com:2222/group/repo.git/", "repo-"),
            ("git@github.com:o/.git", "app-"),
        ],
    )
    def test_a_checkout_is_named_after_the_repository(self, url: str, prefix: str) -> None:
        assert git_install._checkout_prefix(url) == prefix

    def test_a_byte_order_mark_reads_as_it_does_for_a_folder(self, home, monkeypatch) -> None:
        def with_bom(dest: Path) -> None:
            manifest = _write_app(dest) / APP_MANIFEST_FILENAME
            manifest.write_bytes(b"\xef\xbb\xbf" + manifest.read_bytes())

        _stub_clone(monkeypatch, with_bom)
        assert fetch_app_from_git(URL).name == APP

    def test_never_touches_another_attempts_checkout(self, home, monkeypatch) -> None:
        earlier = home / git_install.GIT_CHECKOUTS_DIRNAME / "git-test-app-earlier"
        earlier.mkdir(parents=True)
        (earlier / "local-work.txt").write_text("kept", encoding="utf-8")
        _stub_clone(monkeypatch, _write_app)
        first = fetch_app_from_git(URL)
        second = fetch_app_from_git(URL)
        assert len({earlier, first.checkout, second.checkout}) == 3
        assert (earlier / "local-work.txt").read_text(encoding="utf-8") == "kept"
        assert (first.app_dir / APP_MANIFEST_FILENAME).is_file()
        assert _root_entries(home) == {earlier.name, first.checkout.name, second.checkout.name}

    def test_refuses_an_installed_app_and_removes_only_its_own_clone(
        self, home, monkeypatch
    ) -> None:
        existing = home / git_install.GIT_CHECKOUTS_DIRNAME / "git-test-app-installed"
        existing.mkdir(parents=True)
        (existing / "kept.txt").write_text("kept", encoding="utf-8")
        monkeypatch.setattr(git_install, "get_app", lambda name: {"name": name})
        _stub_clone(monkeypatch, _write_app)
        with pytest.raises(GitInstallError, match=f"kirocrew app update {APP}"):
            fetch_app_from_git(URL)
        assert (existing / "kept.txt").is_file()
        assert _root_entries(home) == {existing.name}

    def test_a_failed_clone_leaves_nothing(self, home, monkeypatch) -> None:
        def fail(dest: Path) -> None:
            (dest / "partial").write_text("x", encoding="utf-8")
            raise GitInstallError("git clone failed (exit 128)")

        _stub_clone(monkeypatch, fail)
        with pytest.raises(GitInstallError, match="exit 128"):
            fetch_app_from_git(URL)
        assert _root_entries(home) == set()

    def test_an_invalid_app_name_leaves_nothing(self, home, monkeypatch) -> None:
        _stub_clone(monkeypatch, lambda dest: _write_app(dest, name="../escape"))
        with pytest.raises(GitInstallError, match="invalid name"):
            fetch_app_from_git(URL)
        assert _root_entries(home) == set()

    def test_no_git_refuses_before_any_folder_is_made(self, home, monkeypatch) -> None:
        monkeypatch.setattr(git_install, "_git_on_path", lambda: False)
        _stub_clone(monkeypatch, _write_app)
        with pytest.raises(GitInstallError, match="needs git"):
            fetch_app_from_git(URL)
        assert not (home / git_install.GIT_CHECKOUTS_DIRNAME).exists()

    def test_a_bad_url_refuses_before_any_folder_is_made(self, home, monkeypatch) -> None:
        _stub_clone(monkeypatch, _write_app)
        with pytest.raises(GitInstallError, match="plaintext"):
            fetch_app_from_git("http://github.com/example/app")
        assert not (home / git_install.GIT_CHECKOUTS_DIRNAME).exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
class TestRealGit:
    """The exact clone command line, run by real git against a local repository."""

    def _git(self, *args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            timeout=default_timeout(),
        )

    @pytest.fixture()
    def repo(self, tmp_path, home, monkeypatch) -> Path:
        # Hermetic: the developer's own git config must not change the result.
        global_config = tmp_path / "gitconfig"
        global_config.write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setattr(git_install, "_git_on_path", lambda: True)
        # The stand-in remote is local, so the file protocol joins the allowlist.
        monkeypatch.setattr(
            git_install,
            "_CLONE_CONFIG_PAIRS",
            (*git_install._CLONE_CONFIG_PAIRS, ("protocol.file.allow", "always")),
        )
        monkeypatch.setattr(git_install, "validate_git_url", lambda url: url)
        repo = tmp_path / "remote"
        _write_app(repo / "app")
        self._git("init", "-q", "-b", "main", cwd=repo)
        self._git("add", ".", cwd=repo)
        self._git("commit", "-q", "-m", "init", cwd=repo)
        self._git("tag", "v1", cwd=repo)
        return repo

    @pytest.mark.parametrize(("ref", "branch"), [(None, "main"), ("main", "main"), ("v1", "")])
    def test_clones_a_branch_or_tag(self, repo, ref, branch) -> None:
        fetched = fetch_app_from_git(repo.as_uri(), ref=ref)
        assert fetched.app_dir == fetched.checkout / "app"
        assert (fetched.app_dir / APP_MANIFEST_FILENAME).is_file()
        assert len(fetched.commit) == 40
        assert fetched.branch == branch

    def test_a_missing_ref_fails_and_leaves_nothing(self, repo, home) -> None:
        with pytest.raises(GitInstallError, match="git clone failed"):
            fetch_app_from_git(repo.as_uri(), ref="no-such-branch")
        assert _root_entries(home) == set()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="the hook is a POSIX shell script")
    def test_a_configured_hook_does_not_run(self, repo, tmp_path, monkeypatch) -> None:
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        marker = tmp_path / "hook-ran"
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        (tmp_path / "gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
        fetch_app_from_git(repo.as_uri())
        assert not marker.exists()


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    register_app_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


class TestCli:
    @pytest.fixture()
    def cli(self, home, monkeypatch):
        monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
        _stub_clone(monkeypatch, lambda dest: _write_app(dest / "app"))
        return home

    def test_parses_the_git_options(self) -> None:
        ns = _parse(["app", "install", URL, "--ref", "v1", "--subdirectory", "app"])
        assert (ns.source, ns.ref, ns.subdirectory) == (URL, "v1", "app")

    def test_installs_the_cloned_app_folder_from_files(self, cli, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            "kiro_crew.cli_commands._install_app_through_gateway", lambda _source: False
        )
        _handle_app(_parse(["app", "install", URL]))
        [checkout] = (cli / git_install.GIT_CHECKOUTS_DIRNAME).iterdir()
        meta = _read_installed(APP)
        assert meta is not None and not meta.enabled
        assert meta.source == str((checkout / "app").resolve())
        out = capsys.readouterr().out
        assert f"Cloning {URL}" in out
        assert f"kirocrew app enable {APP}" in out
        assert f"git -C {checkout} pull, then kirocrew app update {APP}" in out

    def test_hands_the_cloned_folder_to_a_running_gateway(self, cli, monkeypatch, capsys) -> None:
        sent: list[str] = []

        def gateway(source: str) -> bool:
            sent.append(source)
            return True

        monkeypatch.setattr("kiro_crew.cli_commands._install_app_through_gateway", gateway)
        _handle_app(_parse(["app", "install", URL, "--subdirectory", "app"]))
        [checkout] = (cli / git_install.GIT_CHECKOUTS_DIRNAME).iterdir()
        assert sent == [str(checkout / "app")]
        assert _read_installed(APP) is None
        assert f"kirocrew app update {APP}" in capsys.readouterr().out

    def test_a_tag_install_says_how_to_move_the_tag(self, cli, monkeypatch, capsys) -> None:
        _stub_clone(monkeypatch, lambda dest: _write_app(dest / "app"), branch="")
        monkeypatch.setattr(
            "kiro_crew.cli_commands._install_app_through_gateway", lambda _source: True
        )
        _handle_app(_parse(["app", "install", URL, "--ref", "v1"]))
        [checkout] = (cli / git_install.GIT_CHECKOUTS_DIRNAME).iterdir()
        out = capsys.readouterr().out
        assert f"git -C {checkout} fetch --depth 1 origin tag <tag>" in out
        assert f"git -C {checkout} checkout <tag>, then kirocrew app update {APP}" in out
        assert " pull," not in out

    @pytest.mark.skipif(os.sep != "/", reason="a control character is not a Windows name")
    def test_repository_folder_names_reach_the_terminal_inert(
        self, cli, monkeypatch, capsys
    ) -> None:
        hostile = "b\x1b]0;PWNED\x07\x1b[2K"

        def two_apps(dest: Path) -> None:
            _write_app(dest / "a")
            _write_app(dest / hostile)

        _stub_clone(monkeypatch, two_apps)
        with pytest.raises(SystemExit) as exc:
            _handle_app(_parse(["app", "install", URL]))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "several folders hold an app.json" in err
        assert "\x1b" not in err and "\x07" not in err

    @pytest.mark.parametrize("branch", ["main", ""])
    def test_the_update_hint_quotes_a_checkout_path_with_a_space(
        self, tmp_path, capsys, branch: str
    ) -> None:
        checkout = tmp_path / "data home" / "my-app-x1"
        fetched = git_install.FetchedApp(
            name=APP, checkout=checkout, app_dir=checkout, commit="", branch=branch
        )
        _print_git_update_hint(fetched)
        out = capsys.readouterr().out
        quoted = _shell_quoted(str(checkout))
        assert quoted != str(checkout)
        assert out.count(f"git -C {quoted} ") == (1 if branch else 2)

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX shell quoting")
    def test_a_quoted_checkout_splits_back_to_one_argument(self, tmp_path) -> None:
        checkout = str(tmp_path / "it's a data home")
        assert shlex.split(f"git -C {_shell_quoted(checkout)} pull") == [
            "git",
            "-C",
            checkout,
            "pull",
        ]

    def test_a_manifest_refusal_reaches_the_terminal_inert(self, cli, monkeypatch, capsys) -> None:
        def hostile_manifest(dest: Path) -> None:
            manifest = _write_app(dest) / APP_MANIFEST_FILENAME
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["contributes"] = {"sessionControls": [{"id": "\x1b]0;PWNED\x07"}]}
            manifest.write_text(json.dumps(data), encoding="utf-8")

        _stub_clone(monkeypatch, hostile_manifest)
        monkeypatch.setattr(
            "kiro_crew.cli_commands._install_app_through_gateway", lambda _source: False
        )
        with pytest.raises(SystemExit) as exc:
            _handle_app(_parse(["app", "install", URL]))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "session control contribution" in err
        assert "\x1b" not in err and "\x07" not in err

    def test_a_gateway_refusal_reaches_the_terminal_inert(self, cli, monkeypatch, capsys) -> None:
        def refuse(_source: str):
            raise app_lifecycle_client.AppGatewayError("bad id \x1b]0;PWNED\x07", code="x")

        monkeypatch.setattr(app_lifecycle_client, "install_app", refuse)
        with pytest.raises(SystemExit) as exc:
            _handle_app(_parse(["app", "install", URL]))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "gateway refused: bad id" in err
        assert "\x1b" not in err and "\x07" not in err

    def test_a_fetch_refusal_exits_1(self, cli, monkeypatch, capsys) -> None:
        installed: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.cli_commands._install_app_through_gateway",
            lambda source: installed.append(source) or True,
        )
        with pytest.raises(SystemExit) as exc:
            _handle_app(_parse(["app", "install", "http://github.com/example/app"]))
        assert exc.value.code == 1
        assert "plaintext" in capsys.readouterr().err
        assert installed == []

    def test_git_options_need_a_git_url(self, cli, tmp_path, capsys) -> None:
        src = _write_app(tmp_path / "local-app")
        with pytest.raises(SystemExit) as exc:
            _handle_app(_parse(["app", "install", str(src), "--ref", "main"]))
        assert exc.value.code == 1
        assert "apply only when the source is a git URL" in capsys.readouterr().err
        assert _read_installed(APP) is None
