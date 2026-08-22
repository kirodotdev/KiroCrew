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
        assert spec.display == "group/project"
        assert spec.dir_name == "group--project"
        assert spec.clone_url == "https://gitlab.com/group/project.git"

    def test_nested_groups_keep_the_full_path(self, no_dns, https_transport) -> None:
        spec, err = clone_setup.validate_target_url("https://gitlab.com/group/sub/project.git")
        assert err == "" and spec is not None
        assert spec.path == "group/sub/project"
        assert spec.dir_name == "group--sub--project"
        assert spec.clone_url == "https://gitlab.com/group/sub/project.git"

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
        assert spec is None and "gitlab.example.com" in err

    def test_the_gateway_allowlist_helper_is_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One list, not two: the gateway snapshot is the source of truth."""
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "_allowed_gitlab_hosts", lambda: frozenset({"gl.corp.example"}))
        assert clone_setup._gitlab_hosts() == frozenset({"gl.corp.example"})

    def test_a_cold_snapshot_falls_back_to_the_config_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "_allowed_gitlab_hosts", lambda: frozenset())
        monkeypatch.setattr(
            sp, "_load_provider_hosts", lambda: (frozenset({"gl.cold.example"}), frozenset())
        )
        assert clone_setup._gitlab_hosts() == frozenset({"gl.cold.example"})

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
            ("git@github.com:o/r.git", "github"),
            ("https://www.github.com/o/r", "github"),
            ("https://gitlab.com/g/s/p", "gitlab"),
            ("git@gitlab.com:g/p.git", "gitlab"),
            ("https://evil-gitlab.com/g/p", ""),
            ("https://gitlab.example.com/g/p", ""),  # unlisted self-managed
            ("", ""),
            ("not a url", ""),
        ],
    )
    def test_host_only_dispatch(self, allowlist, url: str, provider: str) -> None:
        assert clone_setup.provider_for_url(url) == provider

    def test_an_allowlisted_host_dispatches_to_gitlab(self, allowlist) -> None:
        allowlist("gl.corp.example")
        assert clone_setup.provider_for_url("https://gl.corp.example/g/p") == "gitlab"
        assert clone_setup.provider_for_url("git@gl.corp.example:g/p.git") == "gitlab"


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

    def test_the_persisted_provider_key_wins_over_derivation(self, which, auth) -> None:
        which["git"] = "/stub/git"
        auth["glab"] = False
        self._cfg(provider="gitlab")
        report = deps.check_deps()
        assert "glab" in report["blocking"]

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
