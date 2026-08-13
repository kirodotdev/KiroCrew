"""Update pins: where new code may come from, and the minimum version.

Covers the two enterprise pins (``governance.UpdatePins``) and the shared seam
the three update paths call (``platform.update_governance``).
"""

from __future__ import annotations

import pytest

from kiro_crew.platform import update_governance
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    UpdatePins,
    active_update_pins,
    parse_policy,
    parse_profile,
)


def _policy(**updates: str) -> dict:
    body: dict = {"version": 1, "boot": {}}
    if updates:
        body["updates"] = updates
    return body


class TestSourcePin:
    def test_unpinned_permits_anything(self):
        pins = UpdatePins()
        assert pins.permits_source("https://github.com/anyone/anything")
        assert pins.permits_source("")

    def test_glob_matches(self):
        pins = UpdatePins(source="https://github.com/acme/*")
        assert pins.permits_source("https://github.com/acme/kirocrew")
        assert not pins.permits_source("https://github.com/acme-evil/kirocrew")

    def test_unresolvable_source_is_denied_when_pinned(self):
        """An admin's pin must not be satisfied by "we could not tell"."""
        pins = UpdatePins(source="https://git.corp.example/*")
        assert not pins.permits_source("")
        assert not pins.permits_source("   ")

    def test_scp_and_path_remotes_are_matchable(self):
        """`updates.source` is a glob, so non-URL remote shapes work too."""
        assert UpdatePins(source="git@corp:*").permits_source("git@corp:team/repo")
        assert UpdatePins(source="/srv/repos/*").permits_source("/srv/repos/approved")

    @pytest.mark.parametrize(
        "url",
        [
            "/srv/repos/approved/../evil/repo.git",  # git resolves this outside
            "/srv/repos/approved/./ok.git",
            "/srv/repos/approved/..\\evil/repo.git",  # `\` separates on Windows
        ],
    )
    def test_traversal_cannot_escape_the_pin(self, url):
        """`*` spans separators, so a glob alone does not confine the path."""
        assert not UpdatePins(source="/srv/repos/approved/*").permits_source(url)

    def test_a_dot_inside_a_name_is_not_a_traversal(self):
        pins = UpdatePins(source="https://github.com/acme/*")
        assert pins.permits_source("https://github.com/acme/my.repo.git")
        assert pins.permits_source("https://github.com/acme/.hidden")

    def test_matching_is_case_sensitive_on_every_platform(self):
        """`fnmatch` normcases (lowercases on Windows); `fnmatchcase` does not.

        `…/APPROVED` must not satisfy an `…/approved` pin — git and every
        case-sensitive forge treat those as different repositories, and a ceiling
        must not change verdict with the OS. Fails on Windows if it regresses.
        """
        assert not UpdatePins(source="https://git.corp/approved").permits_source(
            "https://git.corp/APPROVED"
        )


class TestMinVersion:
    def test_unpinned_always_met(self):
        assert UpdatePins().meets_min_version("0.0.1")
        assert UpdatePins().meets_min_version("")

    @pytest.mark.parametrize(
        "current,floor,expected",
        [
            ("1.2.3", "1.2.3", True),
            ("1.2.4", "1.2.3", True),
            ("1.2.2", "1.2.3", False),
            ("2.0.0", "1.9.9", True),
            # Shorter tuples zero-extend: 1.2 == 1.2.0.
            ("1.2", "1.2.0", True),
            ("1.2", "1.2.1", False),
            ("1.10.0", "1.9.0", True),  # numeric, not lexical
        ],
    )
    def test_ordering(self, current, floor, expected):
        assert UpdatePins(min_version=floor).meets_min_version(current) is expected

    def test_prerelease_suffix_is_stripped_off_the_whole_string(self):
        """This project's CI stamps a dot INSIDE the pre-release.

        A per-component strip would leave `nightly.20260728t184500` as its own
        component and read every nightly build as version 0 — permanently
        non-compliant, which at boot means a forced-update loop.
        """
        pins = UpdatePins(min_version="0.2.0")
        assert pins.meets_min_version("0.2.0-nightly.20260728t184500")
        assert pins.meets_min_version("0.3.0-insider.2")
        assert pins.meets_min_version("0.2.0+build.7")
        assert not pins.meets_min_version("0.1.9-nightly.20260728t184500")

    def test_unparseable_floor_imposes_none(self):
        """A typo must not brick a fleet."""
        assert UpdatePins(min_version="not-a-version").meets_min_version("0.0.1")

    def test_unparseable_current_is_below_the_floor(self):
        """Take the update rather than sit on a build we cannot identify."""
        assert not UpdatePins(min_version="1.0.0").meets_min_version("dev")


class TestPolicyParsing:
    def test_absent_updates_is_unpinned(self):
        ceiling = parse_policy(_policy())
        assert ceiling.updates == UpdatePins()

    def test_pins_are_parsed(self):
        ceiling = parse_policy(_policy(source="https://git.corp/*", min_version="1.2.3"))
        assert ceiling.updates.source == "https://git.corp/*"
        assert ceiling.updates.min_version == "1.2.3"

    def test_unknown_key_fails_closed(self):
        with pytest.raises(PlatformCompositionError, match="unknown key"):
            parse_policy(_policy(sources="typo"))

    def test_non_object_fails_closed(self):
        with pytest.raises(PlatformCompositionError, match="must be an object"):
            parse_policy({"version": 1, "boot": {}, "updates": "https://git.corp"})

    def test_profile_may_not_set_updates(self):
        """Policy-only: a profile redirecting the source would be escalation."""
        with pytest.raises(PlatformCompositionError, match="policy-only"):
            parse_profile({"name": "app-x", "updates": {"source": "https://evil/*"}})

    def test_profile_without_updates_still_parses(self):
        assert parse_profile({"name": "app-x"}).name == "app-x"

    @pytest.mark.parametrize("bad", [False, 0, [], {}])
    def test_falsy_non_string_pin_is_rejected_not_coerced(self, bad):
        """`"source": false` must not silently mean "unpinned"."""
        with pytest.raises(PlatformCompositionError, match="must be a string"):
            parse_policy(_policy(source=bad))

    def test_null_is_a_valid_no_pin(self):
        ceiling = parse_policy({"version": 1, "boot": {}, "updates": {"source": None}})
        assert ceiling.updates.source == ""


class TestSeam:
    """The shared gate the API, CLI and boot paths call."""

    def test_ungoverned_host_is_unpinned(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins", lambda: UpdatePins()
        )
        assert update_governance.update_blocked_reason("https://anywhere") == ""
        assert update_governance.update_required("0.0.1") is False
        assert update_governance.min_version() == ""

    def test_source_mismatch_is_blocked_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins",
            lambda: UpdatePins(source="https://git.corp/*"),
        )
        reason = update_governance.update_blocked_reason("https://github.com/evil/x")
        # Names neither the remote nor the pin: both can embed a token.
        assert "does not match" in reason
        assert "github.com" not in reason and "git.corp" not in reason

    def test_unresolvable_source_reports_so(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins",
            lambda: UpdatePins(source="https://git.corp/*"),
        )
        assert "does not match" in update_governance.update_blocked_reason("")

    def test_below_floor_requires_an_update(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance.active_update_pins",
            lambda: UpdatePins(min_version="2.0.0"),
        )
        assert update_governance.update_required("1.9.9") is True
        assert update_governance.update_required("2.0.0") is False

    def test_governance_error_does_not_block(self, monkeypatch):
        """A glitch must not strand a host on a build that may need a patch."""

        def _boom():
            raise RuntimeError("context unavailable")

        monkeypatch.setattr("kiro_crew.platform.context.current_context", _boom)
        assert active_update_pins() == UpdatePins()
        assert update_governance.update_blocked_reason("https://anywhere") == ""
        assert update_governance.update_required("0.0.1") is False


class TestRemoteResolution:
    def test_reads_the_tracked_remote_not_origin(self, monkeypatch):
        """`git pull` follows branch.<name>.remote, so that is what we check."""
        calls: list[list[str]] = []

        class _R:
            returncode = 0

            def __init__(self, out: str) -> None:
                self.stdout = out

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "config" in argv:
                return _R("upstream\n")
            if "ls-remote" in argv:
                return _R("https://git.corp/team/repo\n")
            return _R("")

        monkeypatch.setattr("subprocess.run", fake_run)
        url = update_governance.resolve_remote_url("/proj", branch="main")
        assert url == "https://git.corp/team/repo"
        assert ["git", "ls-remote", "--get-url", "--", "upstream"] in calls

    def test_unknown_remote_echo_is_not_a_url(self, monkeypatch):
        """`--get-url` echoes its argument back for an unknown remote."""

        class _R:
            returncode = 0
            stdout = "origin\n"

        monkeypatch.setattr("subprocess.run", lambda argv, **kw: _R())
        assert update_governance.resolve_remote_url("/proj", branch="main") == ""

    def test_detached_head_is_unresolvable(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("must not run git"))
        assert update_governance.resolve_remote_url("/proj", branch="HEAD") == ""

    def test_fixed_remote_ignores_the_tracked_remote(self, monkeypatch):
        """CLI/boot fetch a hardcoded `origin`, so they must validate `origin`.

        Otherwise a branch tracking an approved upstream green-lights an `origin`
        fetch from elsewhere — approving one source and installing another.
        """
        calls: list[list[str]] = []

        class _R:
            returncode = 0
            stdout = "https://git.corp/origin-repo\n"

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return _R()

        monkeypatch.setattr("subprocess.run", fake_run)
        assert (
            update_governance.resolve_remote_url("/proj", remote="origin")
            == "https://git.corp/origin-repo"
        )
        # Neither the branch nor its tracked remote is consulted.
        assert calls == [["git", "ls-remote", "--get-url", "--", "origin"]]

    def test_resolves_the_branch_itself_when_not_given(self, monkeypatch):
        """The API path passes no branch; the seam resolves it (one impl)."""
        calls: list[list[str]] = []

        class _R:
            returncode = 0

            def __init__(self, out: str) -> None:
                self.stdout = out

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "rev-parse" in argv:
                return _R("main\n")
            if "config" in argv:
                return _R("origin\n")
            return _R("https://git.corp/team/repo\n")

        monkeypatch.setattr("subprocess.run", fake_run)
        assert update_governance.resolve_remote_url("/proj") == "https://git.corp/team/repo"
        assert ["git", "rev-parse", "--abbrev-ref", "HEAD"] in calls

    def test_missing_git_is_unresolvable_not_an_error(self, monkeypatch):
        def _no_git(*a, **k):
            raise FileNotFoundError("git")

        monkeypatch.setattr("subprocess.run", _no_git)
        assert update_governance.resolve_remote_url("/proj", branch="main") == ""
