"""An enterprise build's sandboxed processes take the ceiling from a read-only cache view.

A Kiro Crew MCP server that kiro-cli starts inside the agent sandbox enters the central
tier in an enterprise build, but the sandbox hides the Kiro sign-in from it, so it can
never fetch. Instead the sandbox shows it the policy cache the gateway wrote, read-only,
and the process resolves the ceiling in cache-only mode: no fetch, no sign-in, and a
refusal when the cache is missing, damaged or stale. The vault and the sign-in stay
hidden. An oss build is unchanged.

The https transport is a fake from the shared ``transport`` fixture, so nothing here
opens a socket.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import _body, _doc, _static_fetcher

from kiro_crew import sandbox, sandbox_launch_mark, sandbox_plan, sandbox_tiers
from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError

_REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
_MODES = ("standard", "cc", "strict")


@pytest.fixture(autouse=True)
def _outside_any_sandbox(_floor_monkeypatch):
    """Start every test as the gateway: unsandboxed.

    The suite itself may run inside a Kiro Crew sandbox, whose marker would otherwise
    make every process here a child.
    """
    _floor_monkeypatch.delenv(sandbox_launch_mark.SANDBOX_MARKER, raising=False)
    pd.reset_process_state()
    yield
    pd.reset_process_state()


@pytest.fixture(name="enterprise")
def enterprise_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def _as_child(monkeypatch) -> None:
    """Become a process the sandbox launcher started, after the entry point's pop."""
    monkeypatch.setattr(sandbox_launch_mark, "_launched_in_sandbox", True)
    pd.reset_process_state()


def _gateway_boot(transport, marker: str = "central") -> None:
    """Boot as the gateway against a fake control plane, which writes the cache."""
    transport(_static_fetcher(pd.FetchedPolicy(body=_body(marker))), scheme="https")
    ceiling = governance.load_security_policy()
    assert ceiling is not None and ceiling.identity_issuer == marker


def _child_transport(transport) -> list:
    """A control plane that records any request a child makes, and fails it."""
    seen: list = []

    def fetcher(request):
        seen.append(request)
        raise TimeoutError("a sandboxed child must not fetch")

    transport(fetcher, scheme="https")
    return seen


def _cache_target() -> str:
    return os.path.join(str(pd.cache_dir().parent), pd.CACHE_DIR_LEAF)


class TestTheCacheHoldsNoCredential:
    def test_the_marker_is_the_launchers(self):
        assert sandbox_launch_mark.SANDBOX_MARKER == sandbox_tiers._IN_SANDBOX_MARKER

    def test_the_cache_holds_only_the_document_and_its_validators(self, enterprise, transport):
        """Every file the cache writes: the document, its metadata, an empty lock."""
        _gateway_boot(transport)

        assert sorted(p.name for p in pd.cache_dir().iterdir()) == [
            "policy.json",
            "policy.lock",
            "policy.meta.json",
        ]
        assert (pd.cache_dir() / "policy.lock").read_bytes() == b""
        meta = json.loads((pd.cache_dir() / "policy.meta.json").read_bytes())
        assert sorted(meta) == ["digest", "etag", "fetched_at", "last_modified", "source_digest"]
        for leaf in ("policy.json", "policy.meta.json"):
            text = (pd.cache_dir() / leaf).read_text(encoding="utf-8")
            assert pd.ENTERPRISE_POLICY_URL not in text


class TestTheChildEnforcesTheCachedCeiling:
    def test_a_sandboxed_enterprise_process_is_cache_only(self, enterprise, monkeypatch):
        _as_child(monkeypatch)
        assert pd.cache_only() is True

    def test_the_child_boots_from_the_cache_without_fetching(
        self, enterprise, transport, monkeypatch
    ):
        _gateway_boot(transport)
        _as_child(monkeypatch)
        seen = _child_transport(transport)

        ceiling = governance.load_security_policy()

        assert ceiling is not None
        assert ceiling.identity_issuer == "central"
        assert ceiling.tier == governance.TIER_CENTRAL
        assert seen == []

    def test_the_child_does_not_write_the_cache(self, enterprise, transport, monkeypatch):
        _gateway_boot(transport)
        before = {p.name: p.stat().st_mtime_ns for p in pd.cache_dir().iterdir()}
        _as_child(monkeypatch)
        _child_transport(transport)

        governance.load_security_policy()
        pd.refresh_now(force=True)

        assert {p.name: p.stat().st_mtime_ns for p in pd.cache_dir().iterdir()} == before

    def test_a_child_refresh_never_fetches(self, enterprise, transport, monkeypatch):
        _gateway_boot(transport)
        _as_child(monkeypatch)
        seen = _child_transport(transport)

        assert pd.refresh_now(force=True).status == pd.REFRESH_REJECTED
        assert seen == []

    def test_the_marker_noted_before_the_pop_still_counts(self, monkeypatch):
        monkeypatch.setenv(sandbox_launch_mark.SANDBOX_MARKER, "1")
        sandbox_launch_mark.note()
        monkeypatch.delenv(sandbox_launch_mark.SANDBOX_MARKER)

        assert sandbox_launch_mark.launched_in_sandbox() is True

    @pytest.mark.parametrize("entry", ["mcp_entry.py", "cli.py"])
    def test_each_entry_point_notes_the_marker_before_dropping_it(self, entry):
        text = (_REPO_SRC / entry).read_text(encoding="utf-8")
        noted = text.index("sandbox_launch_mark.note()")
        dropped = text.index('os.environ.pop("KIROCREW_SANDBOX_ACTIVE", None)')
        assert noted < dropped


class TestTheChildDeniesWithoutAUsableCache:
    def _expect_refusal(self, transport, match: str) -> None:
        seen = _child_transport(transport)
        with pytest.raises(PlatformCompositionError, match=match):
            governance.load_security_policy()
        assert seen == []

    def test_no_cache_refuses(self, enterprise, transport, monkeypatch):
        _as_child(monkeypatch)
        self._expect_refusal(transport, "found no cached ceiling")

    def test_an_empty_precreated_cache_refuses(self, enterprise, transport, monkeypatch):
        pd.cache_dir().mkdir(parents=True)
        _as_child(monkeypatch)
        self._expect_refusal(transport, "found no cached ceiling")

    def test_a_document_that_does_not_match_its_metadata_refuses(
        self, enterprise, transport, monkeypatch
    ):
        _gateway_boot(transport)
        (pd.cache_dir() / "policy.json").write_bytes(_body("looser"))
        _as_child(monkeypatch)
        self._expect_refusal(transport, "found no cached ceiling")

    def test_an_unusable_document_refuses(self, enterprise, transport, monkeypatch):
        pd.write_cache(b'{"version": 999}', source=pd.ENTERPRISE_POLICY_URL)
        _as_child(monkeypatch)
        self._expect_refusal(transport, "unusable in cache-only mode")

    def test_a_cache_for_another_source_refuses(self, enterprise, transport, monkeypatch):
        pd.write_cache(_body("elsewhere"), source="https://elsewhere.example.invalid/p")
        _as_child(monkeypatch)
        self._expect_refusal(transport, "found no cached ceiling")

    def test_a_cache_past_the_fleets_staleness_bound_refuses(
        self, enterprise, transport, monkeypatch
    ):
        doc = _body("old", distribution={"max_cache_age_secs": 3600})
        pd.write_cache(doc, source=pd.ENTERPRISE_POLICY_URL, now=time.time() - 3601)
        _as_child(monkeypatch)
        self._expect_refusal(transport, "past the 3600s")


class TestOssIsUnchanged:
    def test_a_sandboxed_oss_process_is_not_cache_only(self, monkeypatch):
        _as_child(monkeypatch)
        assert pd.cache_only() is False

    def test_a_sandboxed_oss_process_neither_reads_nor_requires_the_cache(
        self, transport, monkeypatch
    ):
        pd.write_cache(_body("planted"), source=pd.ENTERPRISE_POLICY_URL)
        _as_child(monkeypatch)
        seen = _child_transport(transport)

        assert governance.load_security_policy() is None
        assert seen == []

    def test_an_oss_sandbox_shows_no_cache(self):
        assert sandbox.enterprise_policy_cache_view() == ()


class TestTheSandboxShowsTheCacheReadOnly:
    """The cache is lifted read-only; the vault and the sign-in stay hidden."""

    @pytest.fixture(autouse=True)
    def _pinned_ssh_probe(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)

    def test_an_enterprise_sandbox_shows_the_cache(self, enterprise):
        assert sandbox.enterprise_policy_cache_view() == (_cache_target(),)

    @pytest.mark.parametrize("hidden", ["cache", "crew home"])
    def test_a_callers_own_mask_over_the_cache_is_kept(self, enterprise, hidden):
        """Lifting would cancel a caller mask that contains the cache (a whole crew home)."""
        target = _cache_target() if hidden == "cache" else os.path.dirname(_cache_target())
        assert sandbox.enterprise_policy_cache_view("strict", (target,)) == ()

    def test_a_callers_mask_through_a_crew_home_alias_is_kept(
        self, enterprise, tmp_path, monkeypatch
    ):
        """The caller hides ``$HOME``'s spelling; the data home resolves to another one."""
        canonical = str(tmp_path / "real" / "crew")
        alias = str(tmp_path / "link" / "crew")
        monkeypatch.setattr(sandbox, "config_dir", lambda: Path(canonical))
        monkeypatch.setattr(sandbox, "_crew_home_alias_roots", lambda: ((alias, canonical, 1, 2),))
        monkeypatch.setattr(sandbox, "carveout_shadowed_by_foreign_mask", lambda *a, **k: False)

        assert sandbox.enterprise_policy_cache_view("strict", (alias,)) == ()
        assert sandbox.enterprise_policy_cache_view("strict", ("/elsewhere",)) == (
            os.path.join(canonical, "policy_cache"),
        )

    def test_an_unrelated_caller_mask_keeps_the_view(self, enterprise):
        assert sandbox.enterprise_policy_cache_view("strict", ("/elsewhere/secret",)) == (
            _cache_target(),
        )

    @pytest.mark.parametrize(
        "meta",
        [
            b'{"source": "https://policy.example/p?X-Amz-Signature=abc", "fetched_at": 1}',
            b"not json",
            b"[]",
        ],
    )
    def test_metadata_that_may_name_its_source_keeps_the_cache_hidden(self, enterprise, meta):
        """Older builds wrote the source URL in plain text, and a pre-signed URL is a credential."""
        pd.cache_dir().mkdir(parents=True)
        (pd.cache_dir() / "policy.meta.json").write_bytes(meta)
        assert sandbox.enterprise_policy_cache_view() == ()

    def test_current_metadata_shows_the_cache(self, enterprise):
        pd.write_cache(_body("current"), source=pd.ENTERPRISE_POLICY_URL)
        assert sandbox.enterprise_policy_cache_view() == (_cache_target(),)

    def test_a_304_rewrites_legacy_metadata_without_its_source(self, enterprise):
        pd.write_cache(_body("current"), source=pd.ENTERPRISE_POLICY_URL)
        legacy = json.loads((pd.cache_dir() / "policy.meta.json").read_bytes())
        legacy["source"] = pd.ENTERPRISE_POLICY_URL
        (pd.cache_dir() / "policy.meta.json").write_text(json.dumps(legacy), encoding="utf-8")
        assert sandbox.enterprise_policy_cache_view() == ()

        pd.touch_cache(pd.read_cache_meta())

        assert sandbox.enterprise_policy_cache_view() == (_cache_target(),)

    def test_a_cache_beneath_a_foreign_mask_is_not_shown(self, enterprise, monkeypatch):
        monkeypatch.setattr(
            sandbox_tiers, "carveout_shadowed_by_foreign_mask", lambda *a, **k: True
        )
        assert sandbox.enterprise_policy_cache_view() == ()

    @pytest.mark.parametrize("enterprise_build", [True, False])
    def test_wrap_argv_hands_the_view_to_the_launcher_only_in_enterprise(
        self, enterprise_build, install_ceiling, monkeypatch
    ):
        if enterprise_build:
            monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        # A booted context, as a real spawn has: the wrap reads the governance floor.
        install_ceiling(governance.parse_policy(_doc("booted")))
        seen: dict = {}

        def namespace_argv(argv, level, **kwargs):
            seen.update(kwargs)
            return [sys.executable, *sandbox._LAUNCHER_INTERPRETER_FLAGS, "launcher.py", *argv]

        monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "namespace")
        monkeypatch.setattr(sandbox, "namespace_argv", namespace_argv)
        monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "_spawns_kiro_cli", lambda argv: False)

        sandbox.wrap_argv(["/bin/true"], mode="standard")

        visible = seen.get("extra_visible_dirs", ())
        assert (_cache_target() in visible) is enterprise_build

    @pytest.mark.skipif(sys.platform == "win32", reason="the namespace plan is POSIX-only")
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_lifted_cache_is_read_only_and_the_secrets_stay_masked(self, mode):
        plan = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, mode, extra_visible_dirs=(_cache_target(),)
        )
        home = os.path.expanduser("~")

        assert _cache_target() in plan.readonly
        assert _cache_target() not in plan.sensitive_dirs
        for leaf in (".vault", "kas"):
            assert os.path.join(home, f".kiro/crew/{leaf}") in plan.sensitive_dirs, leaf

    @pytest.mark.parametrize("mode", _MODES)
    def test_macos_keeps_the_write_deny_and_the_secret_read_denies(self, mode):
        cache = _cache_target()
        profile = sandbox._build_seatbelt_profile(mode, extra_visible_dirs=(cache,))
        home = os.path.expanduser("~")

        assert f'(deny file-write* (subpath "{cache}"))' in profile
        assert f'(deny file-read* (subpath "{cache}"))' not in profile
        for leaf in (".vault", "kas"):
            target = os.path.join(home, f".kiro/crew/{leaf}")
            assert f'(deny file-read* (subpath "{target}"))' in profile, leaf

    def test_the_cache_is_precreated_so_its_seal_has_a_target(self):
        assert "policy_cache" in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES

    def test_an_empty_precreated_cache_is_no_cache_and_the_gateway_fills_it(
        self, enterprise, transport
    ):
        pd.cache_dir().mkdir(parents=True, mode=0o700)
        assert pd.read_cache_meta() is None and pd.read_cache() is None

        _gateway_boot(transport)

        assert pd.read_cache() is not None
