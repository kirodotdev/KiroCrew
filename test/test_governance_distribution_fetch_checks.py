"""Policy distribution: what a source and its answer must be before they are believed.

The ``file://`` transport's bounds, its content-digest validator and its refusal of a
source this account could rewrite (on POSIX, and treated as writable where it cannot
tell), the proxy bypass for loopback, refusing malformed or non-finite declarations
rather than crashing on them and a published source that carries a credential, the
two-channel split where the environment owns the address,
what a ``304`` does and does not establish, what ``kirocrew policy fetch`` may claim
it did, and a boot-only source that is not fetched again.

One topic of the ``test_governance_distribution*.py`` family; the constants, document
builders, fake fetchers and fixtures the family shares come from
``governance_distribution_helpers.py``. Nothing here touches the network either: see
``test_governance_distribution.py``'s docstring for how the transport is faked.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import urllib.request

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import readonly_chain_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import (
    _DECLARED,
    _TEST_SOURCE,
    _body,
    _doc,
    _failing_fetcher,
    _file_source,
    _static_fetcher,
    _write_policy,
)

from kiro_crew import platform_compat
from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError

#: The uid/gid mode-bit tests below monkeypatch ``os.getuid`` and friends, and none of
#: them exist on Windows -- ``monkeypatch.setattr`` would raise before the assertion ran.
#: The off-POSIX contract is what the two ``fails_closed_off_posix`` tests cover, and they
#: stay unguarded, so neither platform's answer is left untested.
_POSIX_IDS_ONLY = pytest.mark.skipif(
    not hasattr(os, "getgroups"),
    reason="POSIX uid/gid mode-bit semantics; os.getgroups does not exist on Windows",
)


class TestFileTransportIsBounded:
    @pytest.mark.skipif(
        not hasattr(os, "mkfifo"),
        reason=(
            "no os.mkfifo on Windows, and there is no substitute that reaches the same "
            "check: the S_ISREG guard runs on an already-open handle, and a Windows named "
            "pipe cannot be handed to os.open by path this way. The guard itself is "
            "platform-independent code, so POSIX coverage is the whole of it."
        ),
    )
    def test_a_non_regular_file_is_refused(self, tmp_path):
        """A FIFO would make the read block forever — a boot that hangs, not fails."""
        fifo = tmp_path / "policy.fifo"
        os.mkfifo(fifo)
        with pytest.raises(PlatformCompositionError, match="not a regular file"):
            pd.fetch_once(governance.PolicyDistribution(source=fifo.as_uri()))

    def test_the_size_is_judged_on_the_open_handle(self, readonly_chain, tmp_path, monkeypatch):
        """Stat-then-read is two trips to a path that can change in between.

        The bytes read must be the bytes measured, or a file swapped in after the stat
        defeats the ceiling.
        """
        path = tmp_path / "policy.json"
        path.write_bytes(b"x" * (governance.MAX_POLICY_BYTES + 64))
        path.chmod(0o444)
        with pytest.raises(PlatformCompositionError, match="exceeds"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "every assertion here is about POSIX mode bits, and off POSIX the predicates "
        "answer True unconditionally -- so a Windows run would pass the refusals for the "
        "platform reason rather than the one under test, and could not run the acceptance "
        "half at all. The off-POSIX contract is pinned by "
        "TestAnUnverifiableSourceIsTreatedAsWritable instead. Guarded rather than listed "
        "in windows-expected-failures.txt: that list is a burn-down backlog, and this is "
        "a platform boundary until the DACL can be read."
    ),
)
class TestAFileSourceMustNotBeAgentWritable:
    """A distribution source this account can rewrite is one an agent can rewrite.

    It runs as the same uid, so a writable path would let it publish itself a ceiling that
    the refresher installs without even a restart. The field manual already tells operators
    to distribute to a read-only, root-owned path; this makes that a precondition.
    """

    @pytest.mark.parametrize("mode", [0o644, 0o664, 0o666, 0o600])
    def test_a_writable_source_is_refused(self, tmp_path, mode):
        path = _write_policy(tmp_path / "policy.json", "published")
        path.chmod(mode)
        with pytest.raises(PlatformCompositionError, match="writable by the account"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))

    def test_a_read_only_source_is_accepted(self, readonly_chain, tmp_path):
        """The other half, so the refusal above cannot pass for the wrong reason."""
        source = _file_source(tmp_path / "policy.json", "published")
        assert pd.fetch_once(governance.PolicyDistribution(source=source)).body == _body(
            "published"
        )

    def test_a_read_only_file_in_a_writable_directory_is_refused(self, tmp_path):
        """The ancestor half is what makes the check mean anything.

        A ``0444`` file inside a directory this account can write is replaceable by
        unlink-and-recreate, so an agent could publish a forged read-only document and a
        leaf-only check would accept it. NOT stubbed here — this is the control's own test.
        """
        path = _write_policy(tmp_path / "policy.json", "forged")
        path.chmod(0o444)
        with pytest.raises(PlatformCompositionError, match="writable by the account"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))

    def test_the_walk_answers_false_for_a_genuinely_root_owned_chain(self):
        """The other direction, against a real path, so the refusal is not vacuous."""
        from kiro_crew import platform_compat

        # /etc is root-owned on every supported POSIX host; skip where it is not present.
        if not os.path.isfile("/etc/hostname"):
            pytest.skip("no /etc/hostname on this host")
        assert platform_compat.path_writable_by_current_user("/etc/hostname") is False

    def test_the_walk_finds_a_writable_ancestor_above_a_read_only_leaf(self, tmp_path):
        from kiro_crew import platform_compat

        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        leaf = nested / "policy.json"
        leaf.write_text("{}", encoding="utf-8")
        leaf.chmod(0o444)
        nested.chmod(0o555)
        try:
            assert platform_compat.path_writable_by_current_user(leaf) is True
        finally:
            nested.chmod(0o755)

    def test_the_message_names_the_channel_designed_for_a_local_file(self, tmp_path):
        """An operator refused here needs to be told where to go instead."""
        path = _write_policy(tmp_path / "policy.json", "published")
        path.chmod(0o644)
        with pytest.raises(PlatformCompositionError, match="KIROCREW_SECURITY_POLICY"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))


class TestTheFileValidatorIsAContentDigest:
    def test_a_same_size_swap_with_a_preserved_mtime_is_detected(self, readonly_chain, tmp_path):
        """An ``mtime:size`` validator misses this, and misses it FOREVER.

        ``cp -p``, a restored backup or a deliberate ``touch`` all reproduce it, and every
        poll would then report "unchanged" while the previous — potentially looser —
        ceiling stayed enforced.
        """
        path = tmp_path / "policy.json"
        source = _file_source(path, "fleet")
        first = pd._fetch_file(pd.FetchRequest(url=source))
        before = os.stat(path)

        swapped = first.body.replace(b'"fleet"', b'"EVIL!"')
        assert len(swapped) == len(first.body), "the swap must be size-preserving to be a test"
        path.chmod(0o644)
        path.write_bytes(swapped)
        path.chmod(0o444)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert os.stat(path).st_mtime_ns == before.st_mtime_ns
        assert os.stat(path).st_size == before.st_size

        again = pd._fetch_file(pd.FetchRequest(url=source, etag=first.etag))

        assert again.not_modified is False
        assert json.loads(again.body)["identity"]["issuer"] == "EVIL!"

    def test_identical_bytes_still_cost_no_body(self, readonly_chain, tmp_path):
        source = _file_source(tmp_path / "policy.json", "published")
        first = pd._fetch_file(pd.FetchRequest(url=source))
        again = pd._fetch_file(pd.FetchRequest(url=source, etag=first.etag))
        assert again.not_modified is True and again.body == b""


class TestLoopbackBypassesTheProxy:
    """urllib has NO implicit loopback exemption.

    With ``HTTP_PROXY`` set, a request to 127.0.0.1 is sent to the proxy in absolute
    form *with the request headers* — handing the fleet's credential to the proxy and
    letting it answer with a substituted ceiling. Asserted on the pure handler-chain
    function, so no socket is opened and no DNS lookup happens.
    """

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8080/p.json", "http://localhost/p.json"])
    def test_a_loopback_url_gets_a_proxy_bypass(self, url):
        handlers = pd._opener_handlers(url)
        assert any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)

    @pytest.mark.parametrize(
        "url", ["https://config.corp.example/p.json", "http://config.corp.example/p.json"]
    )
    def test_a_remote_url_keeps_the_proxy(self, url):
        """There a corporate proxy is the intended path, so it must NOT be bypassed."""
        handlers = pd._opener_handlers(url)
        assert not any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)

    def test_every_chain_refuses_redirects(self):
        """The redirect guard is not conditional — a 3xx must never change origin."""
        for url in ("https://config.corp.example/p.json", "http://127.0.0.1/p.json"):
            assert any(isinstance(h, pd._NoRedirects) for h in pd._opener_handlers(url))


class TestNonFiniteNumbersAreRejected:
    """NaN slips past every range check — each comparison against it is False.

    It would then reach ``int()`` and raise an uncaught ValueError at boot instead of a
    validation error here.
    """

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_the_policy_block_refuses_them(self, literal):
        doc = json.loads('{"source": "https://x.invalid/p", "timeout_secs": ' + literal + "}")
        with pytest.raises(PlatformCompositionError, match="finite"):
            governance.PolicyDistribution.from_dict(doc)

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    def test_the_env_channel_refuses_them(self, monkeypatch, raw):
        monkeypatch.setenv(pd.POLICY_TIMEOUT_ENV, raw)
        with pytest.raises(PlatformCompositionError, match="finite"):
            pd.resolve_distribution(_DECLARED)


class TestOwningTheSourceIsEnoughToBeWritable:
    """An owner may ``chmod`` its own file, so a ``0444`` file this account owns is one it
    can make writable and then rewrite — which is the exact move the threat model
    describes, because the agent subprocess runs as the same uid. Requiring ``S_IWUSR``
    accepted precisely the source an agent could take over with one ``chmod``.
    """

    @_POSIX_IDS_ONLY
    def test_a_read_only_file_we_own_is_writable(self, tmp_path):
        target = tmp_path / "policy.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o444)
        assert platform_compat.stat_writable_by_current_user(os.stat(target)) is True

    @_POSIX_IDS_ONLY
    def test_a_read_only_file_owned_by_someone_else_is_not(self, monkeypatch):
        """The control has to stay a control: the field manual's root-owned path passes."""
        monkeypatch.setattr(platform_compat.os, "getuid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [1000])
        st = os.stat_result((stat.S_IFREG | 0o444, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is False

    @_POSIX_IDS_ONLY
    def test_running_as_root_makes_everything_writable(self, monkeypatch):
        """Root writes anything the mode says, so no source can be shown safe from it."""
        monkeypatch.setattr(platform_compat.os, "getuid", lambda: 0)
        monkeypatch.setattr(platform_compat.os, "geteuid", lambda: 0)
        st = os.stat_result((stat.S_IFREG | 0o444, 0, 0, 1, 12345, 12345, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True

    @_POSIX_IDS_ONLY
    def test_a_directory_we_own_makes_a_leaf_inside_it_replaceable(self, tmp_path):
        """Owning a directory means being able to chmod it, hence to unlink and recreate
        what is inside — so the ancestor walk must answer True for it."""
        nested = tmp_path / "a"
        nested.mkdir()
        nested.chmod(0o555)
        leaf = nested / "policy.json"
        try:
            leaf.write_text("{}", encoding="utf-8")
        except PermissionError:
            nested.chmod(0o755)
            leaf.write_text("{}", encoding="utf-8")
            nested.chmod(0o555)
        leaf.chmod(0o444)
        try:
            assert platform_compat.path_writable_by_current_user(leaf) is True
        finally:
            nested.chmod(0o755)

    @_POSIX_IDS_ONLY
    def test_an_acl_grant_the_mode_bits_hide_is_still_writable(self, tmp_path, monkeypatch):
        """A POSIX **ACL** entry for a named user does not appear in ``st_mode`` at all — the
        group bits show the ACL *mask*, not that entry — so a mode-only check reports "not
        writable" for a source this account can in fact rewrite. ``os.access`` with
        ``effective_ids`` goes through ``faccessat(AT_EACCESS)``, which the kernel evaluates
        against the full ACL.

        The ACL itself is the kernel's to honour, not this code's to reproduce, and creating
        the distinguishing case needs a file owned by a second uid. What is pinned here is
        that the kernel IS asked: a component the mode bits call read-only but ``os.access``
        calls writable comes back writable.
        """
        target = tmp_path / "a" / "policy.json"
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")

        # Owned by someone else and mode-clean, so every mode-bit arm answers "no".
        real_stat = os.stat

        def foreign(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            fields = list(st)
            fields[4] = os.getuid() + 1  # st_uid
            fields[5] = 999999  # st_gid, a group we are not in
            # Decided from the REAL mode: os.path.isfile would re-enter this stub.
            is_dir = stat.S_ISDIR(st.st_mode)
            fields[0] = (stat.S_IFDIR | 0o555) if is_dir else (stat.S_IFREG | 0o444)
            return os.stat_result(tuple(fields))

        monkeypatch.setattr(platform_compat.os, "stat", foreign)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        # And the kernel reports no ACL grant either, so the baseline really is "no".
        monkeypatch.setattr(platform_compat.os, "access", lambda p, mode, **kw: False)
        assert platform_compat.path_writable_by_current_user(target) is False

        # Now the kernel reports an ACL grant on the leaf. Nothing about the mode changed.
        monkeypatch.setattr(
            platform_compat.os,
            "access",
            lambda p, mode, **kw: str(p) == str(target.resolve()),
        )
        assert platform_compat.path_writable_by_current_user(target) is True

    @_POSIX_IDS_ONLY
    def test_an_acl_grant_on_an_ANCESTOR_is_also_caught(self, tmp_path, monkeypatch):
        """Write on a directory means unlink-and-recreate, so the walk must ask about every
        component, not just the leaf."""
        target = tmp_path / "a" / "policy.json"
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")
        parent = str(target.parent.resolve())

        real_stat = os.stat

        def foreign(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            fields = list(st)
            fields[4] = os.getuid() + 1
            fields[5] = 999999
            # Decided from the REAL mode: os.path.isfile would re-enter this stub.
            is_dir = stat.S_ISDIR(st.st_mode)
            fields[0] = (stat.S_IFDIR | 0o555) if is_dir else (stat.S_IFREG | 0o444)
            return os.stat_result(tuple(fields))

        monkeypatch.setattr(platform_compat.os, "stat", foreign)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        monkeypatch.setattr(platform_compat.os, "access", lambda p, mode, **kw: str(p) == parent)
        assert platform_compat.path_writable_by_current_user(target) is True

    @_POSIX_IDS_ONLY
    @pytest.mark.skipif(
        not hasattr(os, "symlink") or os.name == "nt",
        reason="planting the link needs SeCreateSymbolicLinkPrivilege on Windows",
    )
    def test_a_writable_link_to_a_read_only_target_is_refused(self, tmp_path, monkeypatch):
        """Resolving first and walking only the target was the gap. Re-pointing a symlink needs
        no permission on the link and none on the target — it needs write on the LINK's parent,
        which the resolved chain never visits. So a root-owned, read-only document reached
        through a link in a directory this account can write is a source this account controls.
        """
        target_dir = tmp_path / "readonly"
        target_dir.mkdir()
        target = _write_policy(target_dir / "policy.json", "published")
        link_dir = tmp_path / "writable"
        link_dir.mkdir()
        link = link_dir / "policy.json"
        link.symlink_to(target)

        # EXACTLY ONE component reads as ours: the link's parent. Everything else — the
        # target, its directory, and every shared ancestor including the world-writable
        # /tmp — reads as foreign and read-only. That is what makes the test discriminate:
        # the two chains share everything above `tmp_path`, so leaving those real would let
        # a resolved-only walk pass on /tmp's own mode and prove nothing.
        real_stat = os.stat
        ours = str(link_dir)

        def foreign_except_the_links_parent(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if str(path) == ours:
                return st
            fields = list(st)
            fields[4] = os.getuid() + 1
            fields[5] = 999999
            fields[0] = (
                (stat.S_IFDIR | 0o555) if stat.S_ISDIR(st.st_mode) else (stat.S_IFREG | 0o444)
            )
            return os.stat_result(tuple(fields))

        monkeypatch.setattr(platform_compat.os, "stat", foreign_except_the_links_parent)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        # os.access would answer from the real filesystem, where everything here is ours.
        monkeypatch.setattr(platform_compat.os, "access", lambda p, mode, **kw: False)

        assert (
            platform_compat.path_writable_by_current_user(link) is True
        ), "the link's own parent is writable, so the source is replaceable"
        # The control: the target itself, reached directly, is genuinely not replaceable.
        assert platform_compat.path_writable_by_current_user(target) is False

    @_POSIX_IDS_ONLY
    def test_a_file_source_we_own_is_therefore_refused(self, tmp_path):
        """End to end: the transport declines it, whatever its mode bits say."""
        path = _write_policy(tmp_path / "policy.json", "published")
        path.chmod(0o444)
        with pytest.raises(PlatformCompositionError, match="writable by the account"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))


class TestTheFetchCommandDoesNotOverclaim:
    """``refresh_now`` installs the ceiling in the CALLING process, and
    ``kirocrew policy fetch`` exits immediately — so a bare "applied" would claim
    something the command cannot do. A running gateway is a different process and keeps
    its own ceiling until its refresher polls; with a boot-only source there is no poll.
    """

    @staticmethod
    def _run(capsys, *, interval, monkeypatch):
        from kiro_crew import cli_commands
        from kiro_crew.platform import policy_distribution as dist_mod

        monkeypatch.setattr(
            dist_mod,
            "refresh_now",
            lambda *, force=False: pd.RefreshOutcome(pd.REFRESH_APPLIED, signature_state=""),
        )
        monkeypatch.setattr(dist_mod, "effective_refresh_interval", lambda: interval)
        cli_commands._policy_fetch(force=False)
        return capsys.readouterr().out

    def test_it_reports_the_cache_write_not_an_install(self, capsys, monkeypatch):
        out = self._run(capsys, interval=900, monkeypatch=monkeypatch)
        assert "cached it as this host's own" in out
        assert "and applied" not in out, "the CLI process's own install is not the claim"

    def test_a_polling_source_names_when_a_running_gateway_takes_it(self, capsys, monkeypatch):
        out = self._run(capsys, interval=900, monkeypatch=monkeypatch)
        assert "900s" in out
        assert "next refresh" in out

    def test_a_boot_only_source_says_a_running_gateway_will_NOT_take_it(self, capsys, monkeypatch):
        """The case with no next cycle at all, which is the one that misleads."""
        out = self._run(capsys, interval=0, monkeypatch=monkeypatch)
        assert "boot-only" in out
        assert "restarted" in out
        assert "next refresh" not in out

    def test_the_interval_accessor_reports_zero_for_a_boot_only_source(self, transport):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central"))))
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 0}
        )
        assert declared.effective_refresh_interval() == 0


class TestTheEnvironmentChannelMayOwnTheAddress:
    """The ordinary two-channel split: whatever provisions the host owns the address (and
    any credential in it), while the fleet publishes the cadence and the staleness bound in
    the document. Both guards had to learn about it — one aborted boot on such a document,
    the other refused every published document on a canary pinned elsewhere by env.
    """

    def test_settings_without_a_source_are_fine_when_the_env_supplies_one(self, monkeypatch):
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://config.corp.example/p.json")
        parsed = governance.PolicyDistribution.from_dict({"refresh_interval_secs": 900})
        assert parsed.refresh_interval_secs == 900

    def test_settings_without_a_source_still_fail_closed_with_no_env(self, monkeypatch):
        """The control: a block that tunes a fetch it never configures is how a fleet ends
        up ungoverned while its policy file reads as managed."""
        monkeypatch.delenv(pd.POLICY_URL_ENV, raising=False)
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"refresh_interval_secs": 900})
        assert pd.POLICY_URL_ENV in str(caught.value), "the message must name the way out"

    def test_a_source_less_declaration_reaches_the_engine_not_just_the_parser(self, monkeypatch):
        """``from_dict`` accepts such a block; discarding it for having no source would
        parse the settings and throw them away. ``on_unavailable`` is the one
        that bites: a fleet that chose ``degrade`` silently got the ``fail_closed`` default
        and aborted startup on the first outage."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://config.corp.example/p.json")
        doc = {
            "version": 1,
            "distribution": {"on_unavailable": governance.UNAVAILABLE_DEGRADE},
        }
        declared = governance._declared_distribution(doc)
        assert declared is not None, "the declaration must survive to the engine"
        assert declared.on_unavailable == governance.UNAVAILABLE_DEGRADE

        # And it must survive the env overlay, which is what actually reaches the tier.
        assert pd.resolve_distribution(declared).on_unavailable == (governance.UNAVAILABLE_DEGRADE)

    def test_a_source_less_declaration_stays_inert_with_no_env_pin(self, monkeypatch):
        """The control: with no address from either channel the tier must not even be
        imported, and an empty block is inert by any reading."""
        monkeypatch.delenv(pd.POLICY_URL_ENV, raising=False)
        assert governance._declared_distribution({"version": 1, "distribution": {}}) is None

    def test_an_outage_degrades_rather_than_aborting_under_that_split(self, transport, monkeypatch):
        """End to end: the disposition the fleet published is the one that applies."""
        source = transport(_failing_fetcher(TimeoutError("the endpoint is down")))
        monkeypatch.setenv(pd.POLICY_URL_ENV, source)
        declared = governance._declared_distribution(
            {"version": 1, "distribution": {"on_unavailable": governance.UNAVAILABLE_DEGRADE}}
        )

        # None, not a raise: degrade falls through to the next tier.
        assert pd.load_distributed_policy(declared) is None

    def test_a_canary_pinned_by_env_is_not_treated_as_a_migration(
        self, transport, install_ceiling, monkeypatch
    ):
        """``resolve_distribution`` already lets the env win per setting, so the document's
        own source is ignored — refusing the document over it would break every canary."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        monkeypatch.setenv(pd.POLICY_URL_ENV, source)
        # The fleet publishes its CANONICAL address; this host is pinned to the test source.
        published = _body("v2", distribution={"source": "https://canonical.corp.example/p.json"})
        transport(_static_fetcher(pd.FetchedPolicy(body=published, etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED

    def test_the_migration_guard_still_fires_without_an_env_pin(
        self, transport, install_ceiling, monkeypatch
    ):
        """The control: with the document's declaration authoritative, a move is a move."""
        monkeypatch.delenv(pd.POLICY_URL_ENV, raising=False)
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        moved = _body("v2", distribution={"source": "https://elsewhere.corp.example/p.json"})
        transport(_static_fetcher(pd.FetchedPolicy(body=moved, etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_REJECTED


class TestMalformedInputIsRefusedNotCrashedOn:
    """Two shapes a typo produces that made a library call raise out of the very check meant
    to reject it: a bracketed host ``urlsplit`` cannot parse, and a JSON integer too large for
    ``math.isfinite`` to convert to a float.
    """

    @pytest.mark.parametrize(
        "source", ["https://[::1", "https://[bad]:x/p.json"], ids=["unclosed", "not-an-address"]
    )
    def test_a_malformed_declared_source_is_a_composition_error(self, source):
        """An operator who mistyped the address must get an error naming the key, not an
        uncaught ValueError traceback out of boot."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": source})
        assert "distribution.source" in str(caught.value)

    def test_a_declared_source_must_be_valid_utf8_text(self):
        """A lone JSON surrogate must fail at composition, before cache provenance hashes it."""
        policy = json.loads(r'{"source": "https://h/p\udc80"}')

        with pytest.raises(
            PlatformCompositionError,
            match="distribution.source is not valid UTF-8 text",
        ):
            governance.PolicyDistribution.from_dict(policy)

    def test_the_sanitiser_survives_a_malformed_source(self):
        """This one matters most: a sanitiser that crashes on a malformed source takes the
        error REPORT down with it, so the operator sees a traceback about URL parsing instead
        of the problem they actually have. The env channel is not validated by ``from_dict``,
        so a malformed address does reach here."""
        assert pd._sanitize_detail("could not fetch it", "https://[::1") == "could not fetch it"
        assert pd._split_url("https://[::1").scheme == ""

    def test_a_malformed_env_source_is_refused_as_an_unknown_scheme(self, monkeypatch):
        """An unparseable URL yields an all-empty split, which the fetcher table already
        handles: no scheme means no registered transport."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://[::1")
        with pytest.raises(PlatformCompositionError) as caught:
            pd.fetch_once(pd.resolve_distribution(None))
        assert "scheme" in str(caught.value)

    def test_the_posture_survives_a_malformed_env_source(self, monkeypatch):
        """It is polled by every open Security tab, so a crash here is a broken panel."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://[::1")
        posture = pd.distribution_posture()
        assert posture["configured"] is True and posture["source_scheme"] == ""

    @pytest.mark.parametrize(
        "source",
        ["https://host:notaport/p.json", "https://host:99999999/p.json"],
        ids=["non-numeric", "out-of-range"],
    )
    def test_a_malformed_port_is_a_composition_error(self, source):
        """``.port`` is a LAZILY parsed property, so guarding ``urlsplit`` does not cover it."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": source})
        assert "distribution.source" in str(caught.value)

    @pytest.mark.parametrize(
        "source",
        ["https://host:notaport/p.json", "https://host:99999999/p.json"],
        ids=["non-numeric", "out-of-range"],
    )
    def test_the_source_identity_survives_a_malformed_port(self, source):
        """The env channel is not validated by ``from_dict``, so a malformed port does reach
        the engine — and ``_source_digest`` is on the boot path."""
        digest = pd._source_digest(source)
        assert digest and digest == pd._source_digest(source), "stable, and no raise"
        assert digest != pd._source_digest(
            "https://host/p.json"
        ), "an unusable port is still part of the location, so it is not the same source"

    @pytest.mark.parametrize(
        "key",
        ["refresh_interval_secs", "timeout_secs", "max_cache_age_secs"],
    )
    def test_a_huge_json_integer_is_REFUSED_not_crashed_on(self, key):
        """``math.isfinite`` converts to a float first, so a 310-digit integer raised
        OverflowError inside the check meant to screen it — and ``float()`` on the way out would
        have raised three lines later. The verdict is now a clean refusal (it is far past what the
        platform can wait for); what this pins is that it is a COMPOSITION error rather than an
        OverflowError escaping from a library."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": _TEST_SOURCE, key: int("1" * 310)})
        assert f"distribution.{key}" in str(caught.value)

    @pytest.mark.parametrize("key", ["refresh_interval_secs", "timeout_secs", "max_cache_age_secs"])
    def test_a_duration_beyond_what_the_platform_can_WAIT_for_is_refused(self, key):
        """The refresher passes the interval to ``Event.wait`` and the fetch passes the timeout
        to a socket, and both raise OverflowError above ``threading.TIMEOUT_MAX`` — silently
        killing the poller thread, so the host simply stops receiving policy updates. The
        refusal is explicit rather than an accident of a ``float()`` round-trip."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict(
                {"source": _TEST_SOURCE, key: int(governance.MAX_DURATION_SECS) + 1}
            )
        assert "wait for" in str(caught.value)

    def test_the_ceiling_itself_is_accepted(self):
        """It bounds typos rather than intentions — ~292 years — so the boundary is inclusive."""
        parsed = governance.PolicyDistribution.from_dict(
            {"source": _TEST_SOURCE, "refresh_interval_secs": int(governance.MAX_DURATION_SECS)}
        )
        assert parsed.refresh_interval_secs == int(governance.MAX_DURATION_SECS)

    def test_the_ENVIRONMENT_channel_is_bounded_by_the_same_constant(self, monkeypatch):
        """A merely LARGE finite value passes the isfinite screen — ``1e12`` is a perfectly good
        float — and then raises inside ``Event.wait``. Both channels read one constant so they
        cannot drift apart."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://config.corp.example/p.json")
        monkeypatch.setenv(pd.POLICY_REFRESH_ENV, str(int(pd.MAX_DURATION_SECS) + 1))
        with pytest.raises(PlatformCompositionError) as caught:
            pd.resolve_distribution(None)
        assert "wait for" in str(caught.value)

    def test_an_oversized_wait_really_does_raise(self):
        """The premise, asserted rather than assumed: this is why the bound exists."""
        import threading as _threading

        with pytest.raises(OverflowError):
            _threading.Event().wait(governance.MAX_DURATION_SECS + 1)

    def test_a_non_finite_FLOAT_is_still_refused(self):
        """The control: the check must keep doing its job for the type that has such values.
        NaN slips past every range comparison, which is why it is screened here."""
        for bad in (float("nan"), float("inf")):
            with pytest.raises(PlatformCompositionError) as caught:
                governance.PolicyDistribution.from_dict(
                    {"source": _TEST_SOURCE, "timeout_secs": bad}
                )
            assert "finite" in str(caught.value)

    def test_a_huge_integer_in_the_cached_document_yields_no_bound(self):
        """``_cached_max_cache_age``'s contract is that anything unusable yields 0, and its
        ``try`` does not cover the arithmetic below it."""
        body = json.dumps(
            {"version": 1, "distribution": {"max_cache_age_secs": int("1" * 310)}}
        ).encode("utf-8")
        assert pd._cached_max_cache_age(body) == int("1" * 310)

        # And a non-finite float there still reads as no bound.
        assert pd._cached_max_cache_age(b'{"distribution": {"max_cache_age_secs": 1e999}}') == 0


class TestAPublishedSourceMayNotCarryACredential:
    """The ``distribution`` block lands in the cache VERBATIM — the document has to be
    byte-identical for its signature to verify — and that file is readable by an app
    backend, which is arbitrary third-party code. So a credential placed in a *declared*
    source would be published to every host and then handed to every app. The module's rule
    was already that the per-machine credential travels in ``KIROCREW_POLICY_HEADERS``,
    which "a published document must not" carry; this makes it enforceable.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "https://user:pass@config.corp.example/p.json",
            "https://user@config.corp.example/p.json",
            "https://config.corp.example/p.json?X-Amz-Signature=deadbeefcafe",
            "https://config.corp.example/p.json?token=abc",
        ],
        ids=["userinfo-both", "userinfo-user", "presigned", "query-token"],
    )
    def test_a_declared_source_with_a_credential_shape_is_refused(self, source):
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": source})
        message = str(caught.value)
        assert (
            pd.POLICY_URL_ENV in message and pd.POLICY_HEADERS_ENV in message
        ), "the refusal must name the channels that DO carry a credential"

    @pytest.mark.parametrize(
        "source",
        [
            "https://config.corp.example/p.json",
            "https://config.corp.example:8443/deep/path/p.json",
            "file:///etc/kirocrew/security_policy.json",
            "kctest://policy.example/security_policy.json",
        ],
        ids=["plain", "port-and-path", "file", "custom-scheme"],
    )
    def test_an_ordinary_declared_source_is_accepted(self, source):
        assert governance.PolicyDistribution.from_dict({"source": source}).source == source

    def test_the_environment_channel_is_deliberately_unrestricted(self, monkeypatch):
        """That is where a pre-signed URL belongs: it is set by whatever provisions the
        host and it never lands in the document."""
        presigned = "https://config.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        monkeypatch.setenv(pd.POLICY_URL_ENV, presigned)
        assert pd.resolve_distribution(None).source == presigned


class TestAnUnconditional304IsNotSuccess:
    """``--force`` skips the validators deliberately, so a fetcher that answers "unchanged"
    anyway has answered nothing. Reporting that as UNCHANGED made ``kirocrew policy fetch
    --force`` exit 0 having established nothing — and exiting 0 is exactly what a
    config-management run reads as "this host took the change".
    """

    def test_a_forced_fetch_answered_304_is_a_rejection(self, transport, install_ceiling):
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("running"), source=source, etag="v1")

        outcome = pd.refresh_now(force=True)

        assert outcome.status == pd.REFRESH_REJECTED
        assert "no validators were sent" in outcome.detail

    def test_the_cli_exits_non_zero_on_it(self, transport, install_ceiling, capsys):
        """Which is the whole point: a verification step must fail on the host that did not
        take the change."""
        from kiro_crew import cli_commands

        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("running"), source=source, etag="v1")

        with pytest.raises(SystemExit) as caught:
            cli_commands._policy_fetch(force=True)
        assert caught.value.code == 1

    def test_an_ordinary_304_against_a_matching_cache_is_still_unchanged(
        self, transport, install_ceiling
    ):
        """The control: a real 304 answered against validators we DID send is success."""
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)

        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED


class TestAnUnverifiableSourceIsTreatedAsWritable:
    """Off POSIX the mode bits carry no answer, and "cannot tell" has to round to unsafe.

    The caller REFUSES a writable source, so a ``False`` here would not abstain — it
    would assert the source is safe and admit every Windows ``file://`` source
    unchecked, including one an agent just planted.
    """

    def test_the_stat_predicate_fails_closed_off_posix(self, monkeypatch, tmp_path):
        """A read-only file, so only the off-POSIX guard can produce the True."""
        target = tmp_path / "policy.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o444)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        assert platform_compat.stat_writable_by_current_user(os.stat(target)) is True

    def test_the_ancestor_walk_fails_closed_off_posix(self, monkeypatch, tmp_path):
        target = tmp_path / "policy.json"
        target.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        assert platform_compat.path_writable_by_current_user(target) is True

    @_POSIX_IDS_ONLY
    def test_our_primary_group_counts_even_when_getgroups_omits_it(self, monkeypatch):
        """POSIX leaves it unspecified whether the effective gid is in the SUPPLEMENTARY
        list. A process that reached its gid through ``setegid``, or one in a container
        built without ``initgroups``, has a primary group ``getgroups()`` never mentions —
        so a membership test alone calls a group-writable file we CAN replace safe."""
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 4242)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 4242)
        st = os.stat_result((stat.S_IFREG | 0o664, 0, 0, 1, os.getuid() + 1, 4242, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True

    @_POSIX_IDS_ONLY
    def test_the_real_gid_counts_too(self, monkeypatch):
        """A process holding a real gid can regain it, so the union is the honest answer."""
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 1)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 4243)
        st = os.stat_result((stat.S_IFREG | 0o664, 0, 0, 1, os.getuid() + 1, 4243, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True

    @_POSIX_IDS_ONLY
    def test_a_group_we_are_not_in_is_still_not_writable(self, monkeypatch):
        """The control has to stay a control: a real read-only source must pass."""
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [7, 8])
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 9)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 9)
        st = os.stat_result((stat.S_IFREG | 0o664, 0, 0, 1, os.getuid() + 1, 4244, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is False

    @_POSIX_IDS_ONLY
    def test_the_effective_uid_counts_too(self, monkeypatch):
        monkeypatch.setattr(platform_compat.os, "getuid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "geteuid", lambda: 2000)
        st = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 2000, 0, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True


class TestABootOnlySourceIsNotRefetched:
    """``refresh_interval_secs: 0`` means boot-only, and the loader path must honour it.

    ``load_security_policy`` is re-run per app callback by ``mcp_gateway/app_call.py``, and
    ``_fetch_window`` floors a zero interval at ``MIN_REFRESH_INTERVAL_SECS`` so it never
    reads as "fetch every call". But a floor is still a cadence: an operator who froze the
    ceiling for the process lifetime got a 60-second poller anyway, one that could hand an
    app callback a LOOSENED document while the gateway kept the ceiling it booted on.
    """

    def test_boot_fetches_once_and_later_callers_do_not(self, transport, monkeypatch):
        calls: list[str] = []

        def counting(request):
            calls.append(request.url)
            return pd.FetchedPolicy(body=_body("central"))

        source = transport(counting)
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 0}
        )
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 1, "boot itself must still establish the ceiling"

        # Age the cache well past the 60s floor `_fetch_window` clamps a zero interval
        # to, and clear the cooldown as it would have expired by then. Without this the
        # ordinary freshness shortcut would carry the test and the freeze would be
        # unobserved. No `max_cache_age_secs` is declared, so no staleness bound applies.
        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, now=time.time() - 100_000)
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)
        for _ in range(3):
            assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 1, "a boot-only source must not be re-fetched after boot"

    def test_a_configured_interval_still_refetches(self, transport, monkeypatch):
        """The freeze is specific to a zero interval; an ordinary cadence is unchanged."""
        calls: list[str] = []

        def counting(request):
            calls.append(request.url)
            return pd.FetchedPolicy(body=_body("central"))

        source = transport(counting)
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 900}
        )
        assert pd.load_distributed_policy(declared) is not None
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)
        # Age the cache past the window so the shortcut cannot apply.
        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, now=time.time() - 100_000)
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 2

    def test_the_staleness_bound_still_applies_in_boot_only_mode(self, transport, monkeypatch):
        """Not re-fetching is not the same as trusting bytes of any age."""
        calls: list[str] = []

        def counting(request):
            calls.append(request.url)
            return pd.FetchedPolicy(body=_body("central"))

        source = transport(counting)
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 0, "max_cache_age_secs": 300}
        )
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 1

        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, now=time.time() - 10_000)
        # Past max_cache_age_secs, so the cache is not an acceptable answer even in
        # boot-only mode: the tier goes back out rather than serving it. (The fetch
        # cooldown is cleared, since by that age it would have long expired.)
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 2, "a cache past the staleness bound must not be served"

    def test_a_file_source_is_therefore_refused_off_posix(self, monkeypatch, tmp_path):
        """The end-to-end consequence: the transport declines rather than trusting it."""
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(pd, "IS_POSIX", False, raising=False)
        doc = tmp_path / "policy.json"
        doc.write_text(json.dumps(_doc("central")))
        os.chmod(doc, 0o444)

        with pytest.raises(Exception) as excinfo:
            pd._fetch_file(pd.FetchRequest(url=doc.as_uri()))
        assert "writ" in str(excinfo.value).lower()


class TestA304RevalidatesAgainstTheTrustRoot:
    def test_a_newly_mandated_signature_is_caught_without_a_new_document(
        self, transport, install_ceiling, monkeypatch
    ):
        """ "Unchanged" is a statement about the DOCUMENT.

        The trust root is a separate input that moves on its own schedule: a fleet turning
        on ``require_policy_signature`` makes the running ceiling untrusted without the
        endpoint publishing anything, and a 304 would otherwise let it stand indefinitely.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED

        # The fleet now mandates provenance. Nothing was published.
        monkeypatch.setattr(governance, "_policy_signature_required", lambda: True)
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "trust root" in outcome.detail
