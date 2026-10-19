"""What a pod boots with: its seed config, pod env, config file, OS home and env file.

``sanitized_seed_config`` forces the tunnel and each channel in ``SEED_DISABLED_SECTIONS``
off, the pod env carries no live messaging credential, an ACP child's env drops the AWS
secret and session token, ``_seed_pod_os_home`` never copies a host token, and env-file
values are single-line and written under the per-name mutex. The fixtures come from
``pod_helpers.py``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from pod_helpers import (  # noqa: F401 -- the fixtures register by import
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _systemd_backend,
    cfg_fixture,
    requires_posix_pod_lifecycle,
)

from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


class TestSeedSanitization:
    """Deny-by-default: a seeded pod must NEVER boot with tunnel enabled."""

    def _write(self, d: Path, obj: object) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(obj))
        return d

    def test_forces_tunnel_off_when_enabled(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": {"enabled": True, "name": "keep"}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"]["enabled"] is False
        assert out["tunnel"]["name"] == "keep"  # other keys preserved

    def test_overwrites_non_dict_tunnel(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": True})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"] == {"enabled": False}

    def test_adds_tunnel_when_absent(self, tmp_path: Path) -> None:
        seed = self._write(tmp_path / "s", {"dashboard": {"port": 5476}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["tunnel"]["enabled"] is False

    def test_forces_agent_sandbox_floor_and_preserves_other_agent_keys(
        self, tmp_path: Path
    ) -> None:
        seed = self._write(
            tmp_path / "s",
            {
                "agent": {
                    "sandbox": "off",
                    "sandbox_allow_unsandboxed_exec": True,
                    "sandbox_allow_no_isolation": True,
                    "bot_name": "keep",
                }
            },
        )
        out = rt.sanitized_seed_config(seed)
        assert out is not None
        assert out["agent"]["sandbox"] == "auto"
        assert out["agent"]["sandbox_allow_unsandboxed_exec"] is False
        assert out["agent"]["sandbox_allow_no_isolation"] is False
        assert out["agent"]["bot_name"] == "keep"

    def test_bad_json_returns_none(self, tmp_path: Path) -> None:
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text("{not valid json")
        assert rt.sanitized_seed_config(seed) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert rt.sanitized_seed_config(tmp_path / "nope") is None

    def test_non_dict_root_returns_none(self, tmp_path: Path) -> None:
        assert rt.sanitized_seed_config(self._write(tmp_path / "s", [1, 2, 3])) is None

    def test_refuses_sensitive_seed_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = self._write(tmp_path / "s", {"tunnel": {"enabled": True}})
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda p, base_dir=None: True)
        assert rt.sanitized_seed_config(seed) is None

    def test_forces_teams_off_and_keeps_its_app_id(self, tmp_path: Path) -> None:
        """A pod that inherited ``teams.enabled=true`` boots the operator's REAL
        Azure Bot: the App ID rides along in config.json and the secret arrives
        from the inherited env, so it would answer real people in their org."""
        seed = self._write(tmp_path / "s", {"teams": {"enabled": True, "app_id": "azure-live"}})
        out = rt.sanitized_seed_config(seed)
        assert out is not None and out["teams"]["enabled"] is False
        assert out["teams"]["app_id"] == "azure-live"  # preserved, just disabled

    def test_forces_every_disabled_section_off(self, tmp_path: Path) -> None:
        """Both shapes at once, for every section: an enabled dict, and a non-dict
        value that would otherwise let the ``enabled=False`` write be skipped."""
        sections = rt.SEED_DISABLED_SECTIONS
        seeded = {s: ({"enabled": True} if i % 2 else True) for i, s in enumerate(sections)}
        out = rt.sanitized_seed_config(self._write(tmp_path / "s", seeded))
        assert out is not None
        still_on = [s for s in sections if out[s].get("enabled") is not False]
        assert not still_on, f"sections a seeded pod could boot live: {still_on}"

    def test_disabled_sections_cover_every_rostered_channel(self) -> None:
        """The ratchet: a channel added to the roster with a config-level enable
        must be named here, or a seeded pod boots it against real users. Teams
        reached the roster without reaching this list, which is the hole.

        A channel with no ``enabled`` field is credential-gated only (Slack), and
        those credentials are scrubbed by ``build_pod_env`` — but the exempt set is
        pinned exactly, so a new credential-gated channel is an explicit decision
        rather than a silent omission.
        """
        import dataclasses

        from kiro_crew.channels import builtin_channel_descriptors
        from kiro_crew.config.loader import KiroCrewConfig

        section_fields = {f.name: f for f in dataclasses.fields(KiroCrewConfig)}
        exempt: set[str] = set()
        ungated: list[str] = []
        for desc in builtin_channel_descriptors():
            section = desc.channel_type
            factory = section_fields[section].default_factory
            if "enabled" not in getattr(factory, "__dataclass_fields__", {}):
                exempt.add(section)
            elif section not in rt.SEED_DISABLED_SECTIONS:
                ungated.append(section)
        assert not ungated, f"channels a seeded pod could boot live: {ungated}"
        assert exempt == {"slack"}


class TestPodEnvCredentialScrub:
    """The pod env must carry no credential that can act as a live messaging
    identity — ``pod_context()`` hands this same env to arbitrary commands."""

    def test_scrubs_the_teams_app_credentials(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None of the three Teams vars ends in ``_TOKEN`` or starts with a scrubbed
        channel prefix, so the generic suffix rule that catches every other bot
        credential lets the Azure Bot secret through to the pod gateway."""
        monkeypatch.setenv("MICROSOFT_APP_ID", "app-live")
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "secret-live")
        monkeypatch.setenv("MICROSOFT_APP_TENANT_ID", "tenant-live")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "MICROSOFT_APP_ID" not in env
        assert "MICROSOFT_APP_PASSWORD" not in env
        assert "MICROSOFT_APP_TENANT_ID" not in env
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"  # AWS kept on purpose

    def test_scrubs_every_channel_credential_the_loader_knows(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ratchet for the scrub itself: it matches on prefix and suffix, so a
        credential named like neither reaches the pod silently — which is exactly
        how ``MICROSOFT_APP_*`` survived. Pin it against the loader's roster.

        Two keys are kept deliberately: ``KIRO_API_KEY`` is the agent's own model
        credential and a pod runs agent turns, and ``KIROCREW_OWNER_ID`` is the pod
        dashboard's owner identity rather than a bot credential.
        """
        from kiro_crew.config.loader import CRED_KIRO_API_KEY, CRED_OWNER_ID, CREDENTIAL_KEYS

        for key in CREDENTIAL_KEYS:
            monkeypatch.setenv(key, f"live-{key}")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        survivors = set(CREDENTIAL_KEYS) & set(env)
        assert survivors == {CRED_KIRO_API_KEY, CRED_OWNER_ID}

    @pytest.mark.parametrize("inherited", ["::1", "0.0.0.0", "192.168.1.5"])
    def test_bind_is_pinned_to_the_loopback_every_pod_client_dials(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited: str
    ) -> None:
        """An inherited KIROCREW_BIND must never reach the pod gateway.

        `health()`, `mint_token()` and `pod_api()` all dial `127.0.0.1:<port>`,
        and the ownership attestation vouches for the recorded PROCESS, not for
        the ADDRESS it bound. A gateway inheriting `::1` would serve the IPv6
        loopback while a foreign local process binds `127.0.0.1:<port>` — the
        address the mint's own HTTP request then lands on. `0.0.0.0` (the
        official image's export) would additionally expose the pod beyond the
        host. The pin is what keeps listener and attestation on one address.
        """
        monkeypatch.setenv("KIROCREW_BIND", inherited)
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert env["KIROCREW_BIND"] == "127.0.0.1"


class TestPodEnv:
    def test_redirects_provider_cli_credentials_into_pod_home(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host_glab = tmp_path / "host" / ".config" / "glab-cli"
        host_azure = tmp_path / "host" / ".azure"
        monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "host" / ".config" / "gh"))
        monkeypatch.setenv("GLAB_CONFIG_DIR", str(host_glab))
        monkeypatch.setenv("AZURE_CONFIG_DIR", str(host_azure))
        home = tmp_path / "pod-home"

        env = rt.build_pod_env(cfg, home, 7999, tmp_path / "co")

        assert env["GH_CONFIG_DIR"] == str(home / ".config" / "gh")
        assert env["GLAB_CONFIG_DIR"] == str(home / ".config" / "glab-cli")
        assert env["AZURE_CONFIG_DIR"] == str(home / ".azure")
        assert env["AZURE_EXTENSION_DIR"] == str(home / ".azure" / "cliextensions")

    def test_scrubs_slack_and_nonaws_tokens_keeps_aws(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live-bot")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-live")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
        monkeypatch.setenv("JIRA_TOKEN_ABC123", "jira-secret")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "SLACK_BOT_TOKEN" not in env and "SLACK_APP_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env  # non-AWS *_TOKEN scrubbed
        assert "JIRA_TOKEN_ABC123" not in env
        # AWS_* kept (agent turns need it); AWS_SESSION_TOKEN must survive.
        assert env.get("AWS_REGION") == "us-west-2"
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"
        assert env["KIROCREW_PORT"] == "7999"
        assert env["KIROCREW_HOME"].endswith("home")
        assert env["KIROCREW_PROJECT_DIR"].endswith("co")


class TestPodConfigWrite:
    def test_blank_pod_writes_tunnel_off_config(self, tmp_path: Path) -> None:
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed="")
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["tunnel"]["enabled"] is False

    def test_config_and_home_are_owner_only(self, tmp_path: Path) -> None:
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed="")
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600

    def test_seed_config_sanitized_and_locked_down(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps({"tunnel": {"enabled": True}, "provider_token": "secret"})
        )
        home = tmp_path / "pod-home"
        rt.write_pod_config(home, seed=str(seed))
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["tunnel"]["enabled"] is False  # sanitized
        if sys.platform == "win32":
            # No POSIX bits on Windows; owner-only enforcement is the DACL
            # from atomic_write(restrict_to_owner=True), pinned by the
            # restrict_to_owner suite. The sanitize above is this test's core.
            return
        assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600

    def test_a_failed_lockdown_publishes_no_config(self, tmp_path: Path, monkeypatch) -> None:
        """restrict_to_owner runs on the temp; a failure must not leave config.json."""
        monkeypatch.setattr(
            "kiro_crew.atomic_write.platform_compat.restrict_to_owner",
            lambda path: (_ for _ in ()).throw(OSError("icacls: transient failure")),
        )
        home = tmp_path / "pod-home"
        with pytest.raises(OSError, match="icacls"):
            rt.write_pod_config(home, seed="")
        assert not (home / "config.json").exists()
        assert not list(home.glob("*.tmp"))


@requires_posix_pod_lifecycle
class TestSeedPodOsHome:
    """``_seed_pod_os_home`` -- builds a pod's OS-home credential tree.

    The ``.aws/sso/cache`` corridor is CREATED (the pod's own kiro-cli writes its
    MCP OAuth grants there) and deliberately left EMPTY: nothing is copied from the
    host. An earlier revision staged the host's SSO tokens into it, which a security
    review blocked -- the carve-out that keeps the corridor writable also exposed
    those copied HOST bearer tokens to any agent shell in the pod, and live
    acceptance had already shown the copy was not load-bearing (sign-in comes from
    the runtime's own data store, ``_RUNTIME_AUTH_STORES``). These tests pin the
    absence of that copy.

    ``Path.home()`` inside these tests resolves to the per-test ``HOME`` pin
    of the autouse ``_isolate_pod_host_state`` (``pod_helpers.py``), not the
    real invoking user's home. Tests populate THAT pinned real home's
    ``.aws/sso/cache`` to stand in for "the real host".
    """

    def test_never_copies_the_hosts_sso_token(self, tmp_path: Path) -> None:
        """Inverted from the revision this replaces: the token must NOT be staged."""
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)

        corridor = os_home / ".aws" / "sso" / "cache"
        assert corridor.is_dir(), "the pod's own kiro-cli needs the corridor to exist"
        assert list(corridor.iterdir()) == []

    def test_never_copies_the_cli_flow_token_either(self, tmp_path: Path) -> None:
        """The CLI-flow token is host material too, so it stays on the host."""
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token-cli.json").write_text('{"accessToken": "cli-flow"}')

        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)

        assert list((os_home / ".aws" / "sso" / "cache").iterdir()) == []

    def test_never_copies_an_mcp_oauth_grant_pair(self, tmp_path: Path) -> None:
        """The core isolation requirement: the two-file, sha256-named MCP
        grant pairs must NEVER be seeded forward. Seeding one would make a
        pod boot already "Connected" to a provider nobody consented to from
        inside it."""
        from kiro_crew import mcp_grant

        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        key = mcp_grant.grant_key("https://mcp.example.com/mcp")
        (real_cache / f"{key}.token.json").write_text("{}")
        (real_cache / f"{key}.registration.json").write_text("{}")
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)

        staged_dir = os_home / ".aws" / "sso" / "cache"
        # Nothing is staged at all now -- neither the grant pair (never was) nor
        # the sign-in token (host material, deleted from the seeder).
        assert {p.name for p in staged_dir.iterdir()} == set()

    def test_is_create_only_and_never_clobbers_an_existing_pod_token(self, tmp_path: Path) -> None:
        """Re-running boot against an already-seeded pod home (e.g. a
        restart) must not overwrite a token the pod may have refreshed on
        its own."""
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        os_home = tmp_path / "os-home"
        staged_cache = os_home / ".aws" / "sso" / "cache"
        staged_cache.mkdir(parents=True)
        (staged_cache / "kiro-auth-token.json").write_text('{"accessToken": "pod-refreshed"}')

        rt._seed_pod_os_home(os_home)

        assert (staged_cache / "kiro-auth-token.json").read_text() == (
            '{"accessToken": "pod-refreshed"}'
        )

    def test_missing_real_cache_boots_signed_out_rather_than_aborting(self, tmp_path: Path) -> None:
        """A signed-out operator's host still boots a pod -- it can prompt for
        sign-in inside the pod, which is strictly better than refusing to
        boot at all."""
        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)  # no ~/.aws/sso/cache exists at all
        assert (os_home / ".aws" / "sso" / "cache").is_dir()
        assert not list((os_home / ".aws" / "sso" / "cache").iterdir())

    def test_unreadable_real_cache_boots_signed_out_rather_than_aborting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        def _boom(self, *_a, **_kw):
            raise OSError("EACCES")

        monkeypatch.setattr(Path, "glob", _boom)
        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)  # must not raise
        assert (os_home / ".aws" / "sso" / "cache").is_dir()

    def test_os_home_and_cache_tree_are_owner_only(self, tmp_path: Path) -> None:
        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)
        assert stat.S_IMODE(os_home.stat().st_mode) == 0o700
        assert stat.S_IMODE((os_home / ".aws" / "sso" / "cache").stat().st_mode) == 0o700

    def test_staged_token_is_owner_only(self, tmp_path: Path) -> None:
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)

        corridor = os_home / ".aws" / "sso" / "cache"
        # No file is staged, so the owner-only guarantee is asserted on the
        # DIRECTORY the pod's own grants land in.
        assert stat.S_IMODE(corridor.stat().st_mode) == 0o700

    @requires_posix_pod_lifecycle
    def test_never_copies_the_host_token_through_a_planted_symlink(self, tmp_path: Path) -> None:
        """The seeding copies a HOST credential and re-runs on every boot, so a
        link planted at any component of the pod's OS-home would deposit the
        operator's SSO token wherever it points -- an agent-readable workspace,
        for instance. Every component is created through a pinned no-follow
        descriptor, so the link is refused and nothing is written through it.

        The refusal RAISES: this directory becomes the pod child's HOME, so
        skipping the seed and booting anyway would hand kiro-cli the link's
        target and let it write its own grants there. See
        ``TestBootRefusesAnUnverifiableOsHome``."""
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        exfil = tmp_path / "agent-readable"
        exfil.mkdir()
        os_home = tmp_path / "os-home"
        os_home.symlink_to(exfil, target_is_directory=True)

        with pytest.raises(rt.PodError):
            rt._seed_pod_os_home(os_home)

        assert not list(exfil.rglob("kiro-auth-token.json")), (
            "host SSO token was copied through a planted symlink into an "
            "agent-readable directory"
        )

    @requires_posix_pod_lifecycle
    def test_refuses_a_link_planted_at_an_inner_component(self, tmp_path: Path) -> None:
        """An ancestor link is the harder case: O_NOFOLLOW on the final
        component alone would still follow a link at `.aws`."""
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        exfil = tmp_path / "agent-readable"
        exfil.mkdir()
        os_home = tmp_path / "os-home"
        os_home.mkdir()
        (os_home / ".aws").symlink_to(exfil, target_is_directory=True)

        with pytest.raises(rt.PodError):
            rt._seed_pod_os_home(os_home)

        assert not list(exfil.rglob("kiro-auth-token.json"))

    @requires_posix_pod_lifecycle
    def test_a_normal_os_home_does_not_raise_and_builds_the_tree(self, tmp_path: Path) -> None:
        """The other half of the fail-closed contract: an ordinary os-home must
        seed silently. Without this the refusal test above passes vacuously if
        seeding started raising for every input."""
        real_cache = Path.home() / ".aws" / "sso" / "cache"
        real_cache.mkdir(parents=True)
        (real_cache / "kiro-auth-token.json").write_text('{"accessToken": "real"}')

        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)

        assert (os_home / ".aws" / "sso" / "cache").is_dir()

    @requires_posix_pod_lifecycle
    def test_an_unreadable_host_cache_still_seeds_without_raising(self, tmp_path: Path) -> None:
        """A signed-out host is NOT a refusal: the tree is sound, there is just
        nothing to copy, so the pod boots signed-out. The two outcomes must stay
        on separate branches -- this is what the earlier revision conflated."""
        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)  # no host cache exists at all

        assert (os_home / ".aws" / "sso" / "cache").is_dir()

    @requires_posix_pod_lifecycle
    def test_every_component_is_owner_only(self, tmp_path: Path) -> None:
        """A group- or world-writable ancestor is a replacement window for the
        credential beneath it, so each level is tightened, not just the leaf."""
        os_home = tmp_path / "os-home"
        rt._seed_pod_os_home(os_home)

        for path in (
            os_home,
            os_home / ".aws",
            os_home / ".aws" / "sso",
            os_home / ".aws" / "sso" / "cache",
        ):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


class TestAnAcpTurnHasNoInheritedAwsCredentials:
    """An ACP turn inherits no AWS credentials. `build_pod_env` does keep `AWS_*`
    for the pod GATEWAY, but the ACP child is scrubbed AFTER it -- the gateway's
    environment and the child's are not the same."""

    def test_the_scrubber_removes_the_secret_and_session_token(self) -> None:
        from kiro_crew import sandbox

        scrubbed = sandbox.scrub_agent_subprocess_env(
            {
                "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "AWS_SESSION_TOKEN": "session",
                "PATH": "/usr/bin",
            }
        )

        assert "AWS_SECRET_ACCESS_KEY" not in scrubbed
        assert "AWS_SESSION_TOKEN" not in scrubbed
        # The key id survives (no AWS_ACCESS prefix in the sensitive list) but a
        # key id without its secret is not a credential -- which is the whole
        # reason the corrected posture says "no credentials on any path".
        assert scrubbed.get("AWS_ACCESS_KEY_ID") == "AKIAEXAMPLE"

    def test_the_prefixes_the_posture_depends_on_are_pinned(self) -> None:
        """If either prefix left the list, the corrected claim would silently
        become false again."""
        from kiro_crew import sandbox

        assert "AWS_SECRET" in sandbox._SENSITIVE_ENV_PREFIXES
        assert "AWS_SESSION" in sandbox._SENSITIVE_ENV_PREFIXES


class TestReviewRound1Fixes:
    """Round-1 review fixes: deny-by-default channel disable in the sanitized seed,
    WeCom env-cred scrub, ttl query quoting, matched-quote env parsing."""

    def test_seed_forces_all_channels_off(self, tmp_path: Path) -> None:
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps(
                {
                    "tunnel": {"enabled": True},
                    "telegram": {"enabled": True, "bot_token": "tg-live"},
                    "wecom": {"enabled": True},
                }
            )
        )
        out = rt.sanitized_seed_config(seed)
        assert out is not None
        assert out["tunnel"]["enabled"] is False
        assert out["telegram"]["enabled"] is False
        assert out["wecom"]["enabled"] is False
        assert out["telegram"]["bot_token"] == "tg-live"  # preserved, just disabled

    def test_build_pod_env_scrubs_wecom_and_telegram(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WECOM_BOT_ID", "bot-live")
        monkeypatch.setenv("WECOM_SECRET", "sec-live")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-live")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sts-temp")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "WECOM_BOT_ID" not in env
        assert "WECOM_SECRET" not in env
        assert "TELEGRAM_BOT_TOKEN" not in env  # non-AWS *_TOKEN
        assert env.get("AWS_SESSION_TOKEN") == "sts-temp"  # AWS kept

    def test_build_pod_env_scopes_oauth_grant_reads_to_the_pod(
        self, cfg: PodConfig, tmp_path: Path
    ) -> None:
        """KIROCREW_OS_HOME is what makes the pod's OWN mcp_grant reads (mint,
        status, disconnect, mcp_discovery) resolve the pod's tree instead of the
        real host's ``~/.aws/sso/cache`` -- see mcp_grant.kiro_oauth_cache_dir's
        docstring for the split this closes."""
        home = tmp_path / "home"
        env = rt.build_pod_env(cfg, home, 7999, tmp_path / "co")
        assert env["KIROCREW_OS_HOME"] == str(home / "os-home")
        # Reclaimed by cleanup_home alongside everything else under home_dir.
        assert Path(env["KIROCREW_OS_HOME"]).is_relative_to(home)

    def test_seed_forces_feishu_off(self, tmp_path: Path) -> None:
        """A seed cloned from a real home must not boot a live Feishu bot.

        ``feishu.enabled`` self-activates through ``maybe_start_feishu`` exactly
        like telegram/wecom, so it belongs in the sanitize roster. Other keys
        survive so the pod's config still round-trips.
        """
        seed = tmp_path / "s"
        seed.mkdir()
        (seed / "config.json").write_text(
            json.dumps({"feishu": {"enabled": True, "allowed_open_ids": ["ou_live"]}})
        )
        out = rt.sanitized_seed_config(seed)
        assert out is not None
        assert out["feishu"]["enabled"] is False
        assert out["feishu"]["allowed_open_ids"] == ["ou_live"]

    def test_build_pod_env_scrubs_feishu(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FEISHU_APP_ID / FEISHU_APP_SECRET match none of the other predicates.

        Neither ends in ``_TOKEN`` and neither carries the SLACK_/WECOM_ prefix,
        so without an explicit ``FEISHU_`` prefix a pod inherits the live plane's
        Feishu app identity from the systemd --user manager env.
        """
        monkeypatch.setenv("FEISHU_APP_ID", "cli-live")
        monkeypatch.setenv("FEISHU_APP_SECRET", "sec-live")
        env = rt.build_pod_env(cfg, tmp_path / "home", 7999, tmp_path / "co")
        assert "FEISHU_APP_ID" not in env
        assert "FEISHU_APP_SECRET" not in env

    def test_read_env_file_matched_quote_pair_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        # An inner apostrophe survives; only the one surrounding pair is stripped.
        c.env_file("x").write_text("CHECKOUT='/a/o'brien'\n")
        assert rt.read_env_file(c, "x")["CHECKOUT"] == "/a/o'brien"

    def test_mint_token_quotes_ttl(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s")
        captured: dict[str, str] = {}

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"t"}'

        def _urlopen(
            req: object,
            timeout: int = 5,
            *,
            socket_path: object = None,
            verify_peer: object = None,
        ) -> "_Resp":
            captured["url"] = req.full_url  # type: ignore[attr-defined]
            return _Resp()

        # Mint now requires positive ownership proof; this test is about the URL
        # it builds, so grant the proof rather than exercising the guard here.
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: 4242)
        monkeypatch.setattr(rt, "unix_socket_urlopen", _urlopen)
        monkeypatch.setattr(rt, "loopback_urlopen", _urlopen)
        rt.mint_token(c, "demo", "1 h")
        assert "ttl=1%20h" in captured["url"]


class TestReviewRound2Fix:
    """Round-2 review fix: per-pod env values must be single-line (fail-closed)."""

    def test_write_env_file_rejects_newline_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        with pytest.raises(rt.PodError):
            rt.write_env_file(c, "x", {"SEED": "/a/\nevil"})
        # a normal single-line value still round-trips
        rt.write_env_file(c, "y", {"CHECKOUT": "/a/b"})
        assert rt.read_env_file(c, "y")["CHECKOUT"] == "/a/b"


class TestEnvFileConcurrentWrite:
    """``write_env_file`` merges, so it must serialize per pod name.

    ``pod up`` writes pod settings AND starts the unit whose gateway writes the same
    file, so an unserialized merge drops one side's keys and boots the pod on stale
    config.

    Both tests assert a property only the real lock provides, and both run on every
    platform: ``pod_name_mutex`` locks through ``platform_compat.file_lock``, which
    is ``flock`` on POSIX and ``msvcrt.locking`` on Windows.
    """

    def test_a_concurrent_write_does_not_drop_the_other_writers_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two real threads, both past a barrier before either takes the lock.

        Threads rather than a nested call because the lock is per open-file-description:
        re-entering from one thread exercises the reentrant counter instead of the
        cross-writer exclusion this is about.
        """
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.write_env_file(c, "demo", {"CHECKOUT": "/first", "APPROVAL": "reads"})

        entered = threading.Barrier(2, timeout=30)
        errors: list[BaseException] = []

        def writer(updates: dict[str, str]) -> None:
            try:
                entered.wait()
                rt.write_env_file(c, "demo", updates)
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=({"CRONS": "1"},)),
            threading.Thread(target=writer, args=({"SEED": "/s"},)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "write_env_file deadlocked"
        assert not errors, f"writer raised: {errors!r}"

        final = rt.read_env_file(c, "demo")
        # Neither writer's key may be lost, and the pre-existing ones survive both.
        assert final["CRONS"] == "1"
        assert final["SEED"] == "/s"
        assert final["CHECKOUT"] == "/first"
        assert final["APPROVAL"] == "reads"

    def test_a_writer_inside_the_pod_up_transaction_is_serialized_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mutex must be the one ``pod up`` holds, not one private to the merge.

        ``pod up`` runs its whole transaction under :func:`pod_name_mutex`, so a lock
        only ``write_env_file`` took would leave the writes made inside it unexcluded.
        Holding the mutex here must therefore block a competing writer outright.
        """
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        rt.write_env_file(c, "demo", {"CHECKOUT": "/first"})

        blocked = threading.Event()

        def competing_writer() -> None:
            rt.write_env_file(c, "demo", {"SEED": "/s"})
            blocked.set()

        with rt.pod_name_mutex(c, "demo"):
            t = threading.Thread(target=competing_writer)
            t.start()
            # The transaction holds the mutex, so the other writer cannot proceed.
            # flake-ok: 1 s absence window while the mutex is held -- moved verbatim
            assert not blocked.wait(timeout=1.0), "a writer entered during the transaction"
            rt.write_env_file(c, "demo", {"APPROVAL": "yolo"})
        t.join(timeout=30)
        assert not t.is_alive()

        final = rt.read_env_file(c, "demo")
        assert final["APPROVAL"] == "yolo"
        assert final["SEED"] == "/s"
        assert final["CHECKOUT"] == "/first"

    def test_a_lock_free_reader_never_sees_a_torn_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write must be atomic, not merely serialized.

        ``boot`` reads without the mutex on purpose, so an in-place truncating
        rewrite lets a reader observe one generation spliced onto another. A
        missing ``APPROVAL`` is the least restrictive outcome (``boot`` leaves
        ``approval_mode`` unset, which falls through to auto-approve), so a torn
        read is a silent privilege upgrade rather than a crash.

        Modelled deterministically instead of by racing: a reader opens the file
        and consumes half of it, a write lands, then it consumes the rest. Under
        temp-file + rename its descriptor still refers to the intact old inode,
        so the two halves belong to ONE generation. Under an in-place rewrite the
        same descriptor reads across the truncation and the halves do not match
        any generation that was ever valid.
        """
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path))
        c = PodConfig.load()
        # Enough keys that a spliced read is unambiguous rather than a near-miss.
        first = {"APPROVAL": "interactive", "CHECKOUT": "/a"}
        first.update({f"PAD{i}": f"v{i}" * 8 for i in range(40)})
        rt.write_env_file(c, "demo", first)

        env_path = c.env_file("demo")
        before = env_path.read_bytes()

        # Raw fd, NOT open() -- a buffered text reader slurps a small file whole
        # on the first read, so the second read would come from memory and the
        # test could not observe the on-disk seam at all.
        fd = os.open(str(env_path), os.O_RDONLY)
        try:
            head = os.read(fd, len(before) // 2)
            # The writer runs while this reader is mid-file. On Windows the
            # reader's handle carries no FILE_SHARE_DELETE, so the final rename
            # is refused with a sharing violation once the bounded retry is spent:
            # the contract there is fail CLOSED, never torn, so the old generation
            # must survive byte for byte and the caller must hear about it.
            if sys.platform == "win32":
                with pytest.raises(PermissionError):
                    rt.write_env_file(c, "demo", {"APPROVAL": "yolo"})
            else:
                rt.write_env_file(c, "demo", {"APPROVAL": "yolo"})
            chunks = [head]
            while True:
                part = os.read(fd, 4096)
                if not part:
                    break
                chunks.append(part)
        finally:
            os.close(fd)
        seen = b"".join(chunks)

        after = env_path.read_bytes()
        assert head, "reader consumed nothing; the fixture is not exercising the seam"
        # The reader must have seen exactly one whole generation, old or new.
        assert seen in (before, after), "torn read: the reader spliced two generations together"
        final = rt.read_env_file(c, "demo")
        if sys.platform == "win32":
            # Refused, so the old generation is what remains, whole.
            assert after == before
            assert final["APPROVAL"] == "interactive"
        else:
            # And the new generation must be complete on disk.
            assert final["APPROVAL"] == "yolo"
        assert final["CHECKOUT"] == "/a"
