"""Provider-neutral repo intake: GitLab SaaS, the self-managed allowlist, remote
pinning across multi-segment paths, and the provider-switched dependency preflight.

Pure-function tests: DNS (`_host_is_blocked`) and the forge-CLI transport probes are
monkeypatched so nothing here touches the network or the operator's CLIs — those
seams have their own tests. The self-managed allowlist is monkeypatched at
``clone_setup._gitlab_hosts``, the one helper both the intake and the remote
validator share with the gateway's ``dashboard.gitlab_hosts`` snapshot.
"""

from __future__ import annotations

import subprocess
from typing import Any, Callable

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup, deps, store


@pytest.fixture
def no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the SSRF resolve so acceptance is deterministic offline."""
    monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda host: False)


@pytest.fixture
def https_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin both forge CLIs to https so clone URLs are stable across dev hosts."""
    monkeypatch.setattr(clone_setup, "_gh_prefers_ssh", lambda: False)
    monkeypatch.setattr(clone_setup, "_glab_prefers_ssh", lambda host: False)


@pytest.fixture
def allowlist(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Set the self-managed GitLab allowlist for one test (empty by default)."""

    def _set(*hosts: str) -> None:
        monkeypatch.setattr(clone_setup, "_gitlab_hosts", lambda: frozenset(hosts))

    _set()
    return _set


class TestGitLabSaaSUrls:
    def test_a_two_segment_project_is_accepted(self, no_dns, https_transport) -> None:
        spec, err = clone_setup.validate_target_url("https://gitlab.com/group/project")
        assert err == "" and spec is not None
        assert spec.provider == clone_setup.PROVIDER_GITLAB
        assert spec.host == "gitlab.com"
        assert spec.path == "group/project"
        assert spec.display == "gitlab.com/group/project"
        assert spec.dir_name.startswith("gitlab+gitlab.com--group--project--")
        assert spec.clone_url == "https://gitlab.com/group/project.git"

    def test_nested_groups_keep_the_full_path(self, no_dns, https_transport) -> None:
        spec, err = clone_setup.validate_target_url("https://gitlab.com/group/sub/project.git")
        assert err == "" and spec is not None
        assert spec.path == "group/sub/project"
        assert spec.dir_name.startswith("gitlab+gitlab.com--group--sub--project--")
        assert spec.clone_url == "https://gitlab.com/group/sub/project.git"

    @pytest.mark.parametrize(
        "url",
        [
            "https://oauth2:glpat-abcdefghij@gitlab.com/group/project",
            "https://user@gitlab.com/group/project",
            "https://x-access-token:ghp_abc@github.com/o/r",
        ],
    )
    def test_a_url_carrying_credentials_is_refused(self, no_dns, https_transport, url: str) -> None:
        """The url is stored and shown as typed, so a token in it must never pass."""
        spec, err = clone_setup.validate_target_url(url)
        assert spec is None
        assert err.startswith("Remove the user name or token from the URL")

    def test_the_clone_dir_never_collides_with_another_forge(
        self, no_dns, https_transport, allowlist
    ) -> None:
        """Same path on GitHub, gitlab.com and a self-managed host: three clones."""
        allowlist("gl.corp.example")
        names = set()
        for url in (
            "https://github.com/acme/app",
            "https://gitlab.com/acme/app",
            "https://gl.corp.example/acme/app",
        ):
            spec, err = clone_setup.validate_target_url(url)
            assert err == "" and spec is not None
            names.add(spec.dir_name)
        assert len(names) == 3
        assert "acme--app" in names, "a GitHub clone keeps its existing directory"

    def test_the_workspace_never_collides_with_another_forge(self, no_dns, https_transport) -> None:
        """Ledgers, rulers and PR queues hang off the workspace key, which reads
        ``target_display``; the GitLab display carries its host for that reason."""
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        gh, _ = clone_setup.validate_target_url("https://github.com/acme/app")
        gl, _ = clone_setup.validate_target_url("https://gitlab.com/acme/app")
        assert gh is not None and gl is not None
        keys = {
            store.workspace_key({"target_display": s.display, "branch": "origin/main"})
            for s in (gh, gl)
        }
        assert len(keys) == 2

    def test_a_gitlab_path_longer_than_the_longest_github_name_is_refused(
        self, no_dns, https_transport
    ) -> None:
        """Every derived name (clone dir, workspace key, chat key) fits a GitHub-sized one."""
        ok = "https://gitlab.com/" + "a" * 60 + "/" + "b" * 68  # 140 with the host
        spec, err = clone_setup.validate_target_url(ok)
        assert err == "" and spec is not None and len(spec.display) == 140
        assert len(spec.dir_name) < 200
        spec, err = clone_setup.validate_target_url(ok + "c")
        assert spec is None
        assert err.startswith("GitLab project paths longer than 140 characters")

    def test_a_deep_link_still_names_its_project(self, no_dns, https_transport) -> None:
        """GitLab reserves ``/-/`` — everything after it is a view, not the project."""
        spec, err = clone_setup.validate_target_url("https://gitlab.com/g/p/-/tree/main")
        assert err == "" and spec is not None
        assert spec.path == "g/p"

    def test_www_is_canonicalized_to_gitlab_com(self, no_dns, https_transport) -> None:
        spec, err = clone_setup.validate_target_url("https://www.gitlab.com/g/p")
        assert err == "" and spec is not None
        assert spec.host == "gitlab.com"
        assert spec.clone_url == "https://gitlab.com/g/p.git"

    def test_ssh_transport_when_glab_prefers_it(
        self, no_dns, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}

        def _prefers(host: str) -> bool:
            seen["host"] = host
            return True

        monkeypatch.setattr(clone_setup, "_glab_prefers_ssh", _prefers)
        spec, err = clone_setup.validate_target_url("https://gitlab.com/g/sub/p")
        assert err == "" and spec is not None
        assert spec.clone_url == "git@gitlab.com:g/sub/p.git"
        assert seen["host"] == "gitlab.com", "the preference is asked per validated host"

    def test_github_intake_is_unchanged(self, no_dns, https_transport) -> None:
        spec, err = clone_setup.validate_target_url("https://github.com/owner/repo")
        assert err == "" and spec is not None
        assert spec.provider == clone_setup.PROVIDER_GITHUB
        assert spec.host == "github.com"
        assert spec.path == "owner/repo"
        assert spec.display == "owner/repo"
        assert spec.dir_name == "owner--repo"
        assert spec.clone_url == "https://github.com/owner/repo.git"


class TestSelfManagedAllowlist:
    def test_an_allowlisted_host_is_accepted(self, no_dns, https_transport, allowlist) -> None:
        allowlist("gitlab.example.com")
        spec, err = clone_setup.validate_target_url("https://gitlab.example.com/g/s/p")
        assert err == "" and spec is not None
        assert spec.provider == clone_setup.PROVIDER_GITLAB
        assert spec.host == "gitlab.example.com"
        assert spec.clone_url == "https://gitlab.example.com/g/s/p.git"

    def test_an_unlisted_host_is_rejected(self, no_dns, https_transport, allowlist) -> None:
        spec, err = clone_setup.validate_target_url("https://gitlab.example.com/g/p")
        assert spec is None
        assert err == (
            "Only github.com, gitlab.com, or an allowlisted self-managed GitLab host is "
            "supported. Got host: gitlab.example.com. To use a self-managed GitLab, add its "
            "host to dashboard.gitlab_hosts in ~/.kiro/crew/config.json."
        )

    def test_the_gateway_allowlist_snapshot_is_reused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One list, not two: a fresh gateway snapshot is the source of truth."""
        from kiro_crew.dashboard.source_providers import hosts

        monkeypatch.setattr(hosts, "_gitlab_hosts_fresh", lambda: True)
        monkeypatch.setattr(hosts, "_gitlab_hosts_snapshot", frozenset({"gl.corp.example"}))
        assert clone_setup._gitlab_hosts() == frozenset({"gl.corp.example"})

    def test_a_fresh_empty_snapshot_reads_no_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No self-managed hosts is an answer, not a cold cache: no config read."""
        from kiro_crew.dashboard.source_providers import hosts

        def _no_read():
            raise AssertionError("config read on a fresh snapshot")

        monkeypatch.setattr(hosts, "_gitlab_hosts_fresh", lambda: True)
        monkeypatch.setattr(hosts, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(hosts, "_load_source_link_settings", _no_read)
        assert clone_setup._gitlab_hosts() == frozenset()

    def test_a_cold_snapshot_falls_back_to_the_config_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback goes through the REAL loader, so its tuple shape is exercised."""
        from types import SimpleNamespace

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.source_providers import hosts

        dashboard = SimpleNamespace(
            gitlab_hosts=["gl.cold.example"],
            jira_hosts=["jira.example"],
            session_card_source_links=True,
            github_hosts=["ghe.example"],
            bitbucket_server_hosts=[],
            gerrit_hosts=[],
        )
        monkeypatch.setattr(hosts, "_gitlab_hosts_fresh", lambda: False)
        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            classmethod(lambda cls, *a, **k: SimpleNamespace(dashboard=dashboard)),
        )
        assert clone_setup._gitlab_hosts() == frozenset({"gl.cold.example"})

    def test_an_unreadable_allowlist_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.source_providers import hosts

        def _boom() -> frozenset[str]:
            raise RuntimeError("no config")

        monkeypatch.setattr(hosts, "gitlab_hosts_blocking", _boom)
        assert clone_setup._gitlab_hosts() == frozenset()

    def test_a_saas_remote_never_reads_the_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _no_read() -> frozenset[str]:
            raise AssertionError("allowlist read for a SaaS remote")

        monkeypatch.setattr(clone_setup, "_gitlab_hosts", _no_read)
        assert clone_setup._is_allowed_remote("https://github.com/o/r.git")
        assert clone_setup._is_allowed_remote("git@gitlab.com:g/p.git")

    def test_a_port_entry_authorizes_only_that_port(
        self, no_dns, https_transport, allowlist
    ) -> None:
        allowlist("gitlab.example.com:8443")
        spec, err = clone_setup.validate_target_url("https://gitlab.example.com:8443/g/p")
        assert err == "" and spec is not None
        assert spec.host == "gitlab.example.com:8443"
        assert spec.clone_url == "https://gitlab.example.com:8443/g/p.git"
        rejected, err2 = clone_setup.validate_target_url("https://gitlab.example.com/g/p")
        assert rejected is None and err2

    def test_a_port_host_never_uses_scp_syntax(
        self, no_dns, allowlist, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scp-like ssh cannot carry a port, so ssh preference is overridden."""
        allowlist("gitlab.example.com:8443")
        monkeypatch.setattr(clone_setup, "_glab_prefers_ssh", lambda host: True)
        spec, err = clone_setup.validate_target_url("https://gitlab.example.com:8443/g/p")
        assert err == "" and spec is not None
        assert spec.clone_url == "https://gitlab.example.com:8443/g/p.git"


class TestHostileTargetUrlsAreRejected:
    @pytest.mark.parametrize(
        "url",
        [
            "https://evil-gitlab.com/g/p",  # suffix confusion
            "https://gitlab.com.evil.com/g/p",  # subdomain confusion
            "https://mygitlab.com/g/p",
            "https://gitlab.com@evil.com/g/p",  # userinfo: real host is evil.com
            "http://gitlab.com/g/p",  # cleartext
            "https://gitlab.com/onlygroup",  # no project
            "https://gitlab.com//p",  # empty segment
            "https://gitlab.com/../p",  # traversal segment
            "https://gitlab.com/g/..",
            "https://gitlab.com/-/p",  # reserved marker in place of a namespace
            "git@gitlab.com:g/p.git",  # setup input is https-only
        ],
    )
    def test_rejected(self, no_dns, https_transport, allowlist, url: str) -> None:
        spec, err = clone_setup.validate_target_url(url)
        assert spec is None, f"{url!r} was accepted"
        assert err

    def test_a_blocked_address_is_refused_even_on_an_allowlisted_host(
        self, https_transport, allowlist, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DNS/SSRF check applies to every provider, not just github."""
        allowlist("gitlab.internal.example")
        monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda host: True)
        spec, err = clone_setup.validate_target_url("https://gitlab.internal.example/g/p")
        assert spec is None and "blocked" in err
        spec, err = clone_setup.validate_target_url("https://gitlab.com/g/p")
        assert spec is None and "blocked" in err


class TestTheStoredGitLabPushDestinationIsValidated:
    """`resolve_origin_url` applies the same host allowlist + identity pin to GitLab.

    Mirrors ``TestTheStoredPushDestinationIsValidated`` for the widened host set and
    multi-segment (nested-group) slugs over both the https and scp-like shapes.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil-gitlab.com/g/p.git",
            "https://gitlab.com.evil.com/g/p.git",
            "git@gitlab.com.evil.com:g/p.git",
            "git@evil-gitlab.com:g/p.git",
            "https://gitlab.example.com/g/p.git",  # self-managed but NOT allowlisted
            "git@gitlab.example.com:g/p.git",
            "https://gitlab.com/onlygroup",  # one segment: nothing pinnable
            "git@gitlab.com:onlygroup",
            "http://gitlab.com/g/p.git",
        ],
    )
    def test_a_foreign_or_malformed_remote_yields_no_push_target(self, allowlist, url: str) -> None:
        assert (
            clone_setup.resolve_origin_url({"origin_url": url}) == ""
        ), f"{url!r} would have become the push destination"

    @pytest.mark.parametrize(
        "url",
        [
            "git@gitlab.com:group/sub/proj.git",
            "https://gitlab.com/group/sub/proj.git",
            "ssh://git@gitlab.com/group/sub/proj.git",
        ],
    )
    def test_a_pinned_gitlab_remote_resolves(
        self, no_dns, https_transport, allowlist, url: str
    ) -> None:
        cfg = {"origin_url": url, "target_url": "https://gitlab.com/group/sub/proj"}
        assert clone_setup.resolve_origin_url(cfg) == url

    def test_a_sibling_group_swap_is_refused(self, no_dns, https_transport, allowlist) -> None:
        """A two-segment slug would call these equal — the full path is the identity."""
        cfg = {
            "origin_url": "https://gitlab.com/group/sub/other.git",
            "target_url": "https://gitlab.com/group/sub/proj",
        }
        assert clone_setup.resolve_origin_url(cfg) == ""

    def test_an_allowlisted_self_managed_remote_resolves(
        self, no_dns, https_transport, allowlist
    ) -> None:
        allowlist("gl.corp.example")
        cfg = {
            "origin_url": "git@gl.corp.example:g/s/p.git",
            "target_url": "https://gl.corp.example/g/s/p",
        }
        assert clone_setup.resolve_origin_url(cfg) == "git@gl.corp.example:g/s/p.git"

    def test_the_legacy_fallback_revalidates_a_gitlab_target(
        self, no_dns, https_transport, allowlist
    ) -> None:
        got = clone_setup.resolve_origin_url({"target_url": "https://gitlab.com/g/s/p"})
        assert got == "https://gitlab.com/g/s/p.git"


class TestProviderForUrl:
    @pytest.mark.parametrize(
        ("url", "provider"),
        [
            ("https://github.com/o/r", "github"),
            ("https://www.github.com/o/r", "github"),
            ("https://gitlab.com/g/s/p", "gitlab"),
            # A stored self-managed target stays GitLab even after its host leaves the
            # allowlist; its push is then refused by the remote allowlist, not sent to gh.
            ("https://gitlab.example.com/g/p", "gitlab"),
            ("", "github"),
            ("not a url", "github"),
        ],
    )
    def test_the_stored_url_host_decides_the_provider(
        self, allowlist, url: str, provider: str
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.profiles import target_provider

        assert target_provider({"target_url": url}) == provider


class TestDepsProviderSwitch:
    """`check_deps` requires the forge CLI matching the configured target only."""

    @pytest.fixture
    def which(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
        table: dict[str, str] = {}
        monkeypatch.setattr(deps, "_which", lambda binary: table.get(binary, ""))
        return table

    @pytest.fixture
    def auth(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
        """Per-CLI `auth status` outcome, delivered through a patched subprocess.run."""
        state = {"gh": True, "glab": True}

        def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            code = 0 if state.get(cmd[0], False) else 1
            return subprocess.CompletedProcess(args=cmd, returncode=code, stdout="", stderr="")

        monkeypatch.setattr(deps.subprocess, "run", _run)
        return state

    @staticmethod
    def _cfg(**payload: Any) -> None:
        store.write_json_atomic(store.config_path(), payload)

    @staticmethod
    def _by_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {d["id"]: d for d in report["deps"]}

    def test_a_github_target_requires_gh_not_glab(self, which, auth) -> None:
        which.update({"git": "/stub/git", "gh": "/stub/gh"})
        auth["glab"] = False
        self._cfg(target_url="https://github.com/o/r")
        report = deps.check_deps()
        by_id = self._by_id(report)
        assert by_id["gh"]["required"] is True
        assert by_id["glab"]["required"] is False
        assert report["ok"] is True and report["blocking"] == []

    def test_a_gitlab_target_requires_glab_not_gh(self, which, auth) -> None:
        which.update({"git": "/stub/git", "glab": "/stub/glab"})
        auth["gh"] = False
        self._cfg(target_url="https://gitlab.com/g/s/p")
        report = deps.check_deps()
        by_id = self._by_id(report)
        assert by_id["glab"]["required"] is True
        assert by_id["gh"]["required"] is False
        assert report["ok"] is True and report["blocking"] == []

    def test_an_unauthenticated_glab_blocks_a_gitlab_target(self, which, auth) -> None:
        which.update({"git": "/stub/git", "glab": "/stub/glab"})
        auth["glab"] = False
        self._cfg(target_url="https://gitlab.com/g/p")
        report = deps.check_deps()
        assert report["ok"] is False and report["blocking"] == ["glab"]
        assert "glab auth login" in self._by_id(report)["glab"]["detail"]

    def test_a_stray_provider_key_does_not_override_the_url(self, which, auth) -> None:
        """The target URL is the one source: a ``provider`` key decides nothing."""
        which["git"] = "/stub/git"
        which["gh"] = "/stub/gh"
        auth.update({"gh": True, "glab": False})
        self._cfg(provider="gitlab", target_url="https://github.com/o/r")
        report = deps.check_deps()
        assert report["blocking"] == []

    def test_no_target_reports_both_forge_clis_as_non_blocking(self, which, auth) -> None:
        which["git"] = "/stub/git"
        auth.update({"gh": False, "glab": False})
        report = deps.check_deps()
        by_id = self._by_id(report)
        assert report["ok"] is True and report["blocking"] == []
        assert by_id["gh"]["required"] is False and by_id["gh"]["ok"] is False
        assert by_id["glab"]["required"] is False and by_id["glab"]["ok"] is False
        assert by_id["glab"]["fix"] == "install the GitLab CLI, then run `glab auth login`"

    def test_a_self_managed_target_dispatches_when_allowlisted(
        self, which, auth, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(clone_setup, "_gitlab_hosts", lambda: frozenset({"gl.corp.example"}))
        which["git"] = "/stub/git"
        auth["glab"] = False
        self._cfg(target_url="https://gl.corp.example/g/p")
        report = deps.check_deps()
        assert report["blocking"] == ["glab"]


class TestTheCloneUsesTheTargetForgesCredentialHelper:
    """A private https clone authenticates only through the one helper the clone names.

    Global/system git config is disabled for the clone, so a GitLab target cloned with
    the ``gh`` helper has no way to authenticate. The spawn is captured, not run.
    """

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            if "clone" in argv:
                seen["argv"] = argv
                seen["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(argv, 1, "", "fatal: stub")

        monkeypatch.setattr(clone_setup.subprocess, "run", _fake_run)
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-ambient-saas-token")
        monkeypatch.setenv("GITLAB_HOST", "steer.example")
        return seen

    @staticmethod
    def _helper(argv: list[str]) -> str:
        return next(a for a in argv if a.startswith("credential.helper="))

    def test_a_gitlab_com_target_uses_glab(
        self, no_dns, https_transport, captured, tmp_path
    ) -> None:
        _result, err = clone_setup.setup_safe_clone(
            "https://gitlab.com/group/project", tmp_path / "scratch"
        )
        assert "git clone failed" in err
        assert self._helper(captured["argv"]) == "credential.helper=!glab auth git-credential"
        assert captured["env"]["GITLAB_HOST"] == "gitlab.com"
        assert captured["env"]["GITLAB_TOKEN"] == "glpat-ambient-saas-token"

    def test_a_self_managed_target_never_sees_the_saas_token(
        self, no_dns, https_transport, allowlist, captured, tmp_path
    ) -> None:
        allowlist("gl.corp.example:8443")
        _result, err = clone_setup.setup_safe_clone(
            "https://gl.corp.example:8443/team/svc", tmp_path / "scratch"
        )
        assert "git clone failed" in err
        assert self._helper(captured["argv"]) == "credential.helper=!glab auth git-credential"
        assert captured["env"]["GITLAB_HOST"] == "gl.corp.example:8443"
        assert "GITLAB_TOKEN" not in captured["env"]

    def test_a_github_target_keeps_the_gh_helper(
        self, no_dns, https_transport, captured, tmp_path
    ) -> None:
        _result, err = clone_setup.setup_safe_clone(
            "https://github.com/owner/repo", tmp_path / "scratch"
        )
        assert "git clone failed" in err
        assert self._helper(captured["argv"]) == "credential.helper=!gh auth git-credential"
        # The GitHub path is unchanged: no GitLab host pin is applied to its env.
        assert captured["env"]["GITLAB_HOST"] == "steer.example"
