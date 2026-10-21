"""Tests for the hosts-file alias layer of the ssh-to-self floor.

A hosts alias to a late-published own address cannot stay allowed, and background
threads warm the hosts table so the gate itself only ever parses a small file.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import (
    _REAL_RESOLVED_HOST_VERDICT,
    _REAL_SCHEDULE_HOSTS_WARM,
    _denied_by,
)

from kiro_crew.security import argv_floor as _argv_floor

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestHostsAliasPublicationWindow:
    """A hosts alias to a late-published own address cannot stay allowed.

    The netlink layer publishes secondary own-IPs from the enrichment
    worker, after the synchronous seed.  A hosts-file alias for such an
    address, first looked up inside that window, must not be served as
    remote from the hosts cache once publication completes — and inside
    the window the not-local table entry is untrustworthy, so the async
    verdict layer (with its TTL revalidation) takes over instead.
    """

    _RULE = "sandbox-escape-ssh-self"

    @staticmethod
    def _recording_thread(started):
        class _RecordingThread:
            def __init__(self, *args, **kwargs):
                if kwargs.get("name") == "kirocrew-host-verdict":
                    started.append(kwargs.get("name", ""))

            def start(self):
                pass

            def is_alive(self):
                return False

        return _RecordingThread

    def _wire(self, monkeypatch, tmp_path, started):
        hosts = tmp_path / "hosts"
        hosts.write_text("10.99.0.7 sneakyalias\n10.4.4.4 farbox\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (str(hosts),))
        monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_CACHE", {})
        assert _REAL_RESOLVED_HOST_VERDICT is not None
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
        monkeypatch.setattr(_argv_floor.threading, "Thread", self._recording_thread(started))

    def test_late_published_own_address_flips_the_cached_alias_to_deny(self, monkeypatch, tmp_path):
        started: "list[str]" = []
        self._wire(monkeypatch, tmp_path, started)
        # Startup window: netlink has not published, and the secondary own
        # address 10.99.0.7 is not in the seed set yet.
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        monkeypatch.setattr(_argv_floor, "_own_host_names", lambda: frozenset({"127.0.0.1"}))
        assert _denied_by("ssh sneakyalias uptime") is None  # first contact, window open
        # Publication completes: the alias's address is now a known own-IP.
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(
            _argv_floor, "_own_host_names", lambda: frozenset({"127.0.0.1", "10.99.0.7"})
        )
        # The hosts cache must re-key on publication and re-parse, so the
        # alias is now a same-call deny — not a process-lifetime allow.
        assert _denied_by("ssh sneakyalias uptime") == self._RULE
        assert _denied_by("ssh farbox uptime") is None  # far entries stay allowed

    def test_window_negative_is_not_authoritative_and_schedules_revalidation(
        self, monkeypatch, tmp_path
    ):
        started: "list[str]" = []
        self._wire(monkeypatch, tmp_path, started)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        monkeypatch.setattr(_argv_floor, "_own_host_names", lambda: frozenset({"127.0.0.1"}))
        # Inside the window a not-local hosts entry answers open (the
        # adjudicated dotless first-contact shape) but must hand the name
        # to the async layer, whose verdict cache TTL-revalidates.
        assert _denied_by("ssh sneakyalias uptime") is None
        assert started, "the window negative must schedule the revalidation worker"

    def test_published_remote_alias_stays_a_same_call_allow(self, monkeypatch, tmp_path):
        started: "list[str]" = []
        self._wire(monkeypatch, tmp_path, started)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_own_host_names", lambda: frozenset({"127.0.0.1"}))
        # Post-publication the hosts table is authoritative again: a remote
        # alias answers allowed same-call with no worker (the round-21
        # ``ssh dev-dsk`` shape).
        assert _denied_by("ssh farbox uptime") is None
        assert started == []


class TestHostsFileWarmUp:
    """Background threads warm the hosts table; the gate parses only a small file.

    The enrichment worker (started at gateway boot) parses the table before
    its DNS lookups and again after the DNS merge.  A dotless ssh target
    that finds no table for the current key (file, publication and own set)
    is judged in the same call when the file fits in one read chunk; a
    larger file is refused as pending and one background warm is scheduled.
    Once a table is cached the allow/deny verdict per name is the same as
    before.
    """

    _RULE = "sandbox-escape-ssh-self"

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _isolate(self, monkeypatch, tmp_path):
        # No live DNS, no netlink read, no real /etc/hosts, and no thread is
        # ever started: every spawn is recorded instead.  Every global the
        # code under test writes is pinned here so monkeypatch restores it.
        # The gate runs on the gateway's event loop unless a test says
        # otherwise; the no-loop tests put the real check back.
        self._real_loop_check = getattr(_argv_floor, "_event_loop_running", None)
        monkeypatch.setattr(_argv_floor, "_event_loop_running", lambda: True, raising=False)
        self.started: "list[str]" = []
        started = self.started

        class _RecordingThread:
            def __init__(self, *args, **kwargs):
                started.append(kwargs.get("name", ""))

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(_argv_floor.threading, "Thread", _RecordingThread)
        monkeypatch.setattr(_argv_floor.socket, "getfqdn", lambda: "")
        monkeypatch.setattr(_argv_floor.socket, "getaddrinfo", lambda *a, **k: [])
        monkeypatch.setattr(_argv_floor, "_linux_netlink_addresses", lambda: set())
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"127.0.0.1"}))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_STAMP", time.monotonic())
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_IN_FLIGHT", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_CACHE", {})
        monkeypatch.setattr(_argv_floor, "_HOSTS_WARM_IN_FLIGHT", False, raising=False)
        # POSIX ctime semantics on every runner; a Windows runner would
        # otherwise refuse every dotless name on a file over the chunk cap.
        # The Windows tests pin ctime to creation time with _windows().
        monkeypatch.setattr(_argv_floor, "_ctime_is_creation_time", lambda: False)
        if _REAL_SCHEDULE_HOSTS_WARM is not None:
            monkeypatch.setattr(_argv_floor, "_schedule_hosts_file_warm", _REAL_SCHEDULE_HOSTS_WARM)
        assert _REAL_RESOLVED_HOST_VERDICT is not None
        monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {})
        monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
        self.tmp_path = tmp_path

    def _hosts(self, monkeypatch, text):
        hosts = self.tmp_path / "hosts"
        hosts.write_text(text)
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (str(hosts),))
        return str(hosts)

    @staticmethod
    def _cold_worker(monkeypatch, *, netlink):
        """A process whose enrichment pass has not run yet."""
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", None)
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", False)
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        monkeypatch.setattr(_argv_floor, "_linux_netlink_addresses", netlink)

    def _big_hosts(self, monkeypatch, text, name="hosts"):
        """A hosts file one byte over the in-call parse cap."""
        hosts = self.tmp_path / name
        pad = _argv_floor._HOSTS_FILE_READ_CHUNK + 1 - len(text.encode())
        hosts.write_bytes((text + "#" * (pad - 1) + "\n").encode())
        assert os.stat(hosts).st_size == _argv_floor._HOSTS_FILE_READ_CHUNK + 1
        return str(hosts)

    def _real_loop(self, monkeypatch):
        """Undo the fixture's on-loop pin: the gate sees this thread's real loop state."""
        if self._real_loop_check is not None:
            monkeypatch.setattr(_argv_floor, "_event_loop_running", self._real_loop_check)

    @staticmethod
    def _parser_must_not_run(monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("the gate path parsed the hosts file")

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _boom)

    # --- the gate path: small file in call, large file pending -----------

    def test_a_small_hosts_file_is_parsed_in_call_on_a_cold_gate(self, monkeypatch):
        path = self._hosts(monkeypatch, "10.4.4.4 farbox\n127.0.0.1 loopalias\n")
        assert _denied_by("ssh farbox uptime") is None
        assert _denied_by("ssh dev-dsk uptime") is None
        assert _denied_by("ssh dev-dsk 'cd /workplace && git status'") is None
        assert _denied_by("ssh loopalias uptime") == self._RULE
        assert _argv_floor._HOSTS_FILE_CACHE[path][1] == {"farbox": False, "loopalias": True}
        assert "kirocrew-hosts-warm" not in self.started

    def test_a_hosts_file_over_the_cap_is_never_parsed_on_the_gate(self, monkeypatch):
        path = self._big_hosts(monkeypatch, "10.4.4.4 farbox\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (path,))
        self._parser_must_not_run(monkeypatch)
        assert _denied_by("ssh farbox uptime") == self._RULE
        assert _denied_by("ssh dev-dsk uptime") == self._RULE
        assert self.started.count("kirocrew-hosts-warm") == 1, "warm is single-flight"

    def test_the_scheduled_warm_lets_the_same_command_through(self, monkeypatch):
        path = self._big_hosts(monkeypatch, "10.4.4.4 farbox\n127.0.0.1 loopalias\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (path,))
        assert _denied_by("ssh dev-dsk uptime") == self._RULE  # pending
        _argv_floor._hosts_file_warm_worker()  # the scheduled thread's body
        assert _argv_floor._HOSTS_WARM_IN_FLIGHT is False
        self._parser_must_not_run(monkeypatch)
        assert _denied_by("ssh dev-dsk uptime") is None
        assert _denied_by("ssh farbox uptime") is None
        assert _denied_by("ssh loopalias uptime") == self._RULE

    def test_a_thread_with_no_event_loop_parses_a_large_file_in_call(self, monkeypatch):
        # A process with no event loop (a cron-launched MCP server) gets no
        # retry, so a cold gate there parses the large file in the call.
        self._real_loop(monkeypatch)
        path = self._big_hosts(monkeypatch, "10.4.4.4 farbox\n127.0.0.1 loopalias\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (path,))
        assert _denied_by("ssh dev-dsk uptime") is None
        assert _denied_by("ssh farbox uptime") is None
        assert _denied_by("ssh loopalias uptime") == self._RULE
        assert path in _argv_floor._HOSTS_FILE_CACHE
        assert "kirocrew-hosts-warm" not in self.started

    def test_the_event_loop_thread_still_defers_a_large_file(self, monkeypatch):
        self._real_loop(monkeypatch)
        path = self._big_hosts(monkeypatch, "10.4.4.4 farbox\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (path,))
        self._parser_must_not_run(monkeypatch)

        async def _on_loop():
            return _denied_by("ssh dev-dsk uptime")

        assert asyncio.run(_on_loop()) == self._RULE
        assert self.started.count("kirocrew-hosts-warm") == 1

    def test_a_no_loop_read_failure_is_pending_and_not_cached(self, monkeypatch):
        self._real_loop(monkeypatch)
        path = self._big_hosts(monkeypatch, "10.4.4.4 farbox\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (path,))

        def _eio(*_a, **_k):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _eio)
        assert _denied_by("ssh farbox uptime") == self._RULE
        assert path not in _argv_floor._HOSTS_FILE_CACHE

    def test_a_no_loop_file_over_the_read_cap_is_read_truncated(self, monkeypatch):
        # Past the read cap the no-loop parse truncates, as the warm does, so
        # a name before the cap still gets a verdict instead of a refusal.
        self._real_loop(monkeypatch)
        chunk = _argv_floor._HOSTS_FILE_READ_CHUNK
        monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_READ_CAP", 2 * chunk)
        path = self._hosts(monkeypatch, "10.4.4.4 farbox\n" + "#" * (3 * chunk) + "\n")
        assert os.stat(path).st_size > 2 * chunk
        assert _denied_by("ssh farbox uptime") is None
        assert path in _argv_floor._HOSTS_FILE_CACHE

    def test_no_hosts_file_is_not_pending(self, monkeypatch):
        monkeypatch.setattr(
            _argv_floor, "_hosts_file_paths", lambda: (str(self.tmp_path / "missing"),)
        )
        assert _denied_by("ssh dev-dsk uptime") is None
        assert "kirocrew-hosts-warm" not in self.started

    # --- the key: publication, own set, and a mid-parse change ----------

    def test_unstable_own_set_is_a_deny_end_to_end(self, monkeypatch):
        # The own set changes on every read, and this host's 10.4.4.4 joins
        # it only after several reads: a table judged against any earlier
        # snapshot would call the alias remote.  Nothing may be cached, and
        # the gate must refuse the alias rather than fall through to allow.
        self._hosts(monkeypatch, "10.4.4.4 ownalias\n")
        reads = iter(range(10_000))

        def _own():
            n = next(reads)
            base = {f"10.200.0.{n % 250}", f"10.201.{n // 250}.0"}
            return frozenset(base | ({"10.4.4.4"} if n >= 4 else set()))

        monkeypatch.setattr(_argv_floor, "_own_host_names", _own)
        assert _argv_floor._host_is_self("ownalias") is True
        for _ in range(3):
            _argv_floor._warm_hosts_file_cache()
        assert _argv_floor._HOSTS_FILE_CACHE == {}
        assert _argv_floor._host_is_self("ownalias") is True

    def test_publication_during_a_parse_caches_nothing_and_still_denies(self, monkeypatch):
        # The worker is mid-parse when the address table publishes this
        # host's 203.0.113.66, and a later line aliases a name to it.  That
        # table was judged against the old own set, so it must be dropped.
        self._hosts(monkeypatch, "10.4.4.4 farbox\n203.0.113.66 ownalias\n")
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_READ_CHUNK", 16)  # line 1 only
        real_open = open

        class _Handle:
            def __init__(self, fh):
                self._fh, self._reads = fh, 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._fh.close()

            def read(self, n=-1):
                data = self._fh.read(n)
                self._reads += 1
                if self._reads == 1 and not _argv_floor._NETLINK_ADDRS_PUBLISHED:
                    _argv_floor._publish_netlink_addresses({"203.0.113.66"})
                return data

        monkeypatch.setattr(
            _argv_floor, "open", lambda f, *a, **k: _Handle(real_open(f, *a, **k)), raising=False
        )
        _argv_floor._warm_hosts_file_cache()
        assert _argv_floor._NETLINK_ADDRS_PUBLISHED is True
        assert _argv_floor._HOSTS_FILE_CACHE == {}
        assert _argv_floor._host_is_self("ownalias") is True
        _argv_floor._warm_hosts_file_cache()  # the next pass, key now stable
        assert _argv_floor._host_is_self("ownalias") is True
        assert _denied_by("ssh farbox uptime") is None

    def test_own_set_growing_during_the_gate_parse_keeps_an_absent_name_open(self, monkeypatch):
        # The first ssh vet in a cold process seeds the own set AND starts
        # the enrichment worker, whose netlink publish lands while this
        # same call parses the hosts file.  The file did not change, so a
        # name absent from it is absent under any own set and must fall
        # through to the DNS layer, not be refused as pending; a local
        # entry is still local; only a remote entry is held.
        path = self._hosts(monkeypatch, "127.0.0.1 loopalias\n10.4.4.4 farbox\n")
        real_parse = _argv_floor._parse_hosts_file
        grown = iter(range(1, 1000))

        def _parse_then_grow(*args, **kwargs):
            table = real_parse(*args, **kwargs)
            own = _argv_floor._OWN_HOST_NAMES_CACHE or frozenset()
            _argv_floor._OWN_HOST_NAMES_CACHE = own | {f"10.77.0.{next(grown)}"}
            return table

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _parse_then_grow)
        assert _argv_floor._hosts_file_verdict("backuphost") is None
        assert _argv_floor._hosts_file_verdict("loopalias") is True
        assert _argv_floor._hosts_file_verdict("farbox") is True  # held, not served
        assert "kirocrew-hosts-warm" in self.started
        assert _argv_floor._HOSTS_FILE_CACHE == {}  # a moved key caches nothing
        assert path not in _argv_floor._HOSTS_FILE_CACHE
        assert _denied_by("ssh -o ConnectTimeout=10 backup@host uptime") is None
        assert _denied_by("ssh loopalias uptime") == self._RULE

    def _publish_during_first_gate_parse(self, monkeypatch, addrs):
        """The enrichment worker publishes *addrs* while the gate parses the file."""
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        real_parse = _argv_floor._parse_hosts_file
        parses: "list[bool]" = []

        def _parse(*args, **kwargs):
            parses.append(_argv_floor._NETLINK_ADDRS_PUBLISHED)
            if len(parses) == 1:
                _argv_floor._publish_netlink_addresses(addrs)
            return real_parse(*args, **kwargs)

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _parse)
        return parses

    def test_a_cold_ssh_to_a_remote_alias_is_allowed_when_publication_lands_mid_parse(
        self, monkeypatch
    ):
        # The first ssh on a cold worker: the enrichment worker's netlink
        # publish (its real publish step) lands inside the gate's parse, as it
        # does whenever that parse is slow.  The remote alias is judged again
        # on the new key and allowed, not refused as ssh-to-self.
        path = self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        parses = self._publish_during_first_gate_parse(monkeypatch, {"203.0.113.66"})
        assert _denied_by("ssh farbox uptime") is None
        assert parses == [False, True]
        assert _argv_floor._HOSTS_FILE_CACHE[path][0][-2] is True
        assert "kirocrew-hosts-warm" not in self.started

    def test_the_second_judgement_uses_the_newly_published_own_set(self, monkeypatch):
        # The address published mid-parse is this host's, and a line aliases
        # a name to it: the retry judges that alias local, never remote.
        self._hosts(monkeypatch, "10.4.4.4 farbox\n203.0.113.66 ownalias\n")
        parses = self._publish_during_first_gate_parse(monkeypatch, {"203.0.113.66"})
        # The first table, judged before publication, maps ownalias remote:
        # only the own-state re-check stops that table from being served.
        assert _argv_floor._hosts_file_verdict("ownalias") is True
        assert parses == [False, True]
        assert _denied_by("ssh ownalias uptime") == self._RULE
        assert _denied_by("ssh farbox uptime") is None

    def test_a_file_edited_during_the_gate_parse_is_pending_at_once(self, monkeypatch):
        # Only a moved OWN state is judged again: a FILE that changes
        # mid-parse is refused as pending after one parse, with the warm
        # scheduled.
        path = self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        real_parse = _argv_floor._parse_hosts_file
        parses: "list[int]" = []

        def _parse(*args, **kwargs):
            parses.append(1)
            table = real_parse(*args, **kwargs)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("10.5.5.5 newbox\n")
            return table

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _parse)
        assert _argv_floor._hosts_file_verdict("farbox") is True
        assert len(parses) == 1
        assert _argv_floor._HOSTS_FILE_CACHE == {}
        assert self.started.count("kirocrew-hosts-warm") == 1

    def test_an_own_set_that_moves_before_a_remote_answer_is_judged_again(self, monkeypatch):
        # The own set grows between the table's key and the remote answer's
        # re-check: the gate judges again on the grown set and serves it.
        self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        reads: "list[int]" = []

        def _own():
            reads.append(1)
            grown = len(reads) > 2  # the key read and its re-check see the old set
            return frozenset({"127.0.0.1"} | ({"198.51.100.9"} if grown else set()))

        monkeypatch.setattr(_argv_floor, "_own_host_names", _own)
        assert _argv_floor._hosts_file_verdict("farbox") is False
        assert "kirocrew-hosts-warm" not in self.started

    def test_a_key_still_moving_after_every_attempt_is_pending(self, monkeypatch):
        # The bound: an own set that moves on every read is judged a fixed
        # number of times, then a remote entry is refused as pending.
        self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        reads = iter(range(10_000))
        monkeypatch.setattr(
            _argv_floor, "_own_host_names", lambda: frozenset({f"10.200.0.{next(reads) % 250}"})
        )
        real_parse = _argv_floor._parse_hosts_file
        parses: "list[int]" = []

        def _parse(*args, **kwargs):
            parses.append(1)
            return real_parse(*args, **kwargs)

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _parse)
        assert _argv_floor._hosts_file_verdict("farbox") is True
        assert len(parses) == _argv_floor._HOSTS_KEY_MOVED_ATTEMPTS == 3
        assert _argv_floor._HOSTS_FILE_CACHE == {}
        assert self.started.count("kirocrew-hosts-warm") == 1

    def test_a_failed_re_stat_after_the_parse_is_pending_at_once(self, monkeypatch):
        # The key cannot be re-read after a real parse: a read failure, not a
        # moved own state, so the gate refuses at once, unretried.
        self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        real_key = _argv_floor._hosts_file_key
        calls: "list[int]" = []

        def _key(path):
            calls.append(1)
            if len(calls) == 2:  # the re-stat after the parse
                raise PermissionError(path)
            return real_key(path)

        monkeypatch.setattr(_argv_floor, "_hosts_file_key", _key)
        assert _argv_floor._hosts_file_verdict("farbox") is True
        assert len(calls) == 2
        assert _argv_floor._HOSTS_FILE_CACHE == {}
        assert self.started.count("kirocrew-hosts-warm") == 1

    def test_an_own_address_learned_later_re_marks_a_cached_alias(self, monkeypatch):
        self._hosts(monkeypatch, "198.51.100.44 lateownalias\n")
        _argv_floor._warm_hosts_file_cache()
        assert _argv_floor._hosts_file_verdict("lateownalias") is False
        monkeypatch.setattr(
            _argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({"127.0.0.1", "198.51.100.44"})
        )
        assert _argv_floor._hosts_file_verdict("lateownalias") is True  # re-parsed, local
        assert _argv_floor._HOSTS_FILE_CACHE[str(self.tmp_path / "hosts")][1] == {
            "lateownalias": True
        }

    # --- the enrichment worker warms it ---------------------------------

    def test_worker_pass_leaves_a_published_table(self, monkeypatch):
        path = self._hosts(monkeypatch, "203.0.113.66 ownalias\n10.4.4.4 farbox\n")
        self._cold_worker(monkeypatch, netlink=lambda: {"203.0.113.66"})
        _argv_floor._resolve_own_host_names_into_cache()
        key, table = _argv_floor._HOSTS_FILE_CACHE[path]
        assert key[-2] is True
        assert table == {"ownalias": True, "farbox": False}
        self._parser_must_not_run(monkeypatch)
        assert _denied_by("ssh ownalias uptime") == self._RULE
        assert _denied_by("ssh farbox uptime") is None

    def test_hosts_table_is_warm_before_the_dns_lookups_run(self, monkeypatch):
        path = self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        self._cold_worker(monkeypatch, netlink=lambda: {"203.0.113.66"})
        seen: "list[bool]" = []

        def _getaddrinfo(*_a, **_k):
            seen.append(path in _argv_floor._HOSTS_FILE_CACHE)
            return []

        monkeypatch.setattr(_argv_floor.socket, "getaddrinfo", _getaddrinfo)
        _argv_floor._resolve_own_host_names()
        assert seen and all(seen)

    def test_worker_warms_even_without_a_netlink_dump(self, monkeypatch):
        # No dump (non-Linux, or the read failed): the table is still parsed
        # before DNS, keyed as unpublished, so a dotless target is not left
        # pending; its remote entries defer to the async layer as before.
        path = self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        self._cold_worker(monkeypatch, netlink=lambda: set())
        _argv_floor._resolve_own_host_names()
        key, table = _argv_floor._HOSTS_FILE_CACHE[path]
        assert key[-2] is False and table == {"farbox": False}
        assert _argv_floor._hosts_file_verdict("farbox") is None

    def test_warm_parse_sees_dns_derived_own_addresses(self, monkeypatch):
        path = self._hosts(monkeypatch, "198.51.100.44 dnsownalias\n")
        self._cold_worker(monkeypatch, netlink=lambda: {"203.0.113.66"})
        monkeypatch.setattr(
            _argv_floor.socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("198.51.100.44", 0))],
        )
        _argv_floor._resolve_own_host_names_into_cache()
        assert _argv_floor._HOSTS_FILE_CACHE[path][1] == {"dnsownalias": True}
        assert _denied_by("ssh dnsownalias uptime") == self._RULE

    def test_a_failed_warm_parse_does_not_fail_the_worker_pass(self, monkeypatch):
        self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        self._cold_worker(monkeypatch, netlink=lambda: {"203.0.113.66"})

        def _boom(*_a, **_k):
            raise RuntimeError("unreadable")

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _boom)
        _argv_floor._resolve_own_host_names_into_cache()
        assert "203.0.113.66" in _argv_floor._OWN_HOST_NAMES_CACHE
        assert _argv_floor._OWN_HOST_RESOLVE_DONE is True
        assert _argv_floor._HOSTS_FILE_CACHE == {}

    # --- the parse itself ----------------------------------------------

    def test_chunked_read_matches_one_whole_read(self, monkeypatch):
        # Chunk edges fall inside names, inside addresses and between the
        # \r and \n of a CRLF; the cap still cuts at the same character.
        text = (
            "# comment line\r\n127.0.0.1 localhost looplias\r\n"
            "10.4.4.4 farbox farbox.example\n::1 v6alias\n"
            "10.9.9.9 pastcap\n"
        )
        cap = text.index("10.9.9.9") + 4  # mid-address: the last line is unparseable
        hosts = self.tmp_path / "hosts"
        hosts.write_bytes(text.encode())
        monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_READ_CAP", cap)
        tables = []
        for chunk in (1, 3, 7, 16, 1 << 16):
            monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_READ_CHUNK", chunk)
            tables.append(_argv_floor._parse_hosts_file(str(hosts), frozenset()))
        assert tables[0] == {
            "localhost": True,
            "looplias": True,
            "farbox": False,
            "farbox.example": False,
            "v6alias": True,
        }
        assert all(t == tables[0] for t in tables)

    def test_own_name_reads_do_not_scale_with_lines(self, monkeypatch):
        self._hosts(monkeypatch, "".join(f"10.0.0.{i} host{i}\n" for i in range(50)))
        calls: "list[int]" = []

        def _own():
            calls.append(1)
            return frozenset({"10.0.0.7"})

        monkeypatch.setattr(_argv_floor, "_own_host_names", _own)
        _argv_floor._warm_hosts_file_cache()
        table = next(iter(_argv_floor._HOSTS_FILE_CACHE.values()))[1]
        assert table["host7"] is True and table["host8"] is False
        # One read for the key before the parse, one to confirm it after.
        assert len(calls) == 2

    # --- a remote verdict is re-checked at return -----------------------

    @staticmethod
    def _fire_after_lookup(monkeypatch, action):
        """Run *action* between the gate's cache lookup and its return."""

        class _Cache(dict):
            def get(self, *a, **k):
                found = dict.get(self, *a, **k)
                action()
                return found

        monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_CACHE", _Cache(_argv_floor._HOSTS_FILE_CACHE))

    def test_own_set_merge_after_the_key_check_is_a_deny(self, monkeypatch):
        # The worker merges this host's 10.4.4.4 after the gate matched the
        # cached key: the table's "remote" was judged without it.
        self._hosts(monkeypatch, "10.4.4.4 ownalias\n")
        _argv_floor._warm_hosts_file_cache()
        assert _argv_floor._hosts_file_verdict("ownalias") is False

        def _merge():
            _argv_floor._OWN_HOST_NAMES_CACHE = frozenset({"127.0.0.1", "10.4.4.4"})

        self._fire_after_lookup(monkeypatch, _merge)
        assert _argv_floor._host_is_self("ownalias") is True
        # The gate judged the file again on the merged set, in the same call:
        # the alias is cached local, so no pending warm was needed.
        assert dict(_argv_floor._HOSTS_FILE_CACHE)[str(self.tmp_path / "hosts")][1] == {
            "ownalias": True
        }

    def test_publication_after_the_key_check_is_a_deny(self, monkeypatch):
        # A table judged before publication must not be read as authoritative
        # because publication flipped between the key check and the return.
        self._hosts(monkeypatch, "10.4.4.4 ownalias\n")
        monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", False)
        _argv_floor._warm_hosts_file_cache()

        def _publish():
            _argv_floor._NETLINK_ADDRS_PUBLISHED = True
            _argv_floor._OWN_HOST_NAMES_CACHE = frozenset({"127.0.0.1", "10.4.4.4"})

        self._fire_after_lookup(monkeypatch, _publish)
        assert _argv_floor._host_is_self("ownalias") is True

    def test_a_stable_remote_is_still_a_same_call_allow(self, monkeypatch):
        self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        _argv_floor._warm_hosts_file_cache()
        self._fire_after_lookup(monkeypatch, lambda: None)
        assert _argv_floor._hosts_file_verdict("farbox") is False
        assert _denied_by("ssh farbox uptime") is None

    def test_a_pending_path_wins_over_another_paths_remote(self, monkeypatch):
        first = self._big_hosts(monkeypatch, "127.0.0.1 localalias\n", name="hosts-a")
        second = self.tmp_path / "hosts-b"
        second.write_text("10.4.4.4 localalias\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (str(second),))
        _argv_floor._warm_hosts_file_cache()  # only the second path is warm
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (first, str(second)))
        self._parser_must_not_run(monkeypatch)
        assert _argv_floor._host_is_self("localalias") is True

    # --- unreadable files and overlong lines ----------------------------

    def test_an_unreadable_hosts_file_refuses_until_it_reads(self, monkeypatch):
        # Fail closed: while the file cannot be read, a hosts-file alias for
        # this machine cannot be ruled out, so a dotless target is pending on
        # every call, nothing is cached, and each call reads again.  Once the
        # file reads, the same command passes.  (Main allowed here.)
        path = self._hosts(monkeypatch, "10.4.4.4 farbox\n")
        readable = [False]
        real_parse = _argv_floor._parse_hosts_file

        def _parse(p, own, **kw):
            if not readable[0]:
                raise PermissionError("no read access")
            return real_parse(p, own, **kw)

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _parse)
        assert _denied_by("ssh dev-dsk uptime") is not None
        assert _denied_by("ssh dev-dsk uptime") is not None
        assert path not in _argv_floor._HOSTS_FILE_CACHE
        readable[0] = True
        assert _denied_by("ssh farbox uptime") is None

    def test_a_long_loopback_line_is_parsed_at_any_offset(self, monkeypatch):
        # No hosts line is dropped, however long: a valid loopback line of
        # more than 4096 characters still makes its alias local wherever
        # the chunk edge falls inside it.
        line = "127.0.0.1 secretbox " + " ".join(f"a{i:04d}" for i in range(1200)) + "\n"
        assert len(line) > 4096
        for pad in (60000, 61500, 64000, 65535):
            self._hosts(monkeypatch, "# pad\n" * (pad // 6) + line + "10.4.4.4 farbox\n")
            monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_CACHE", {})
            _argv_floor._warm_hosts_file_cache()
            assert _argv_floor._host_is_self("secretbox") is True, pad
            assert _argv_floor._host_is_self("a1199") is True, pad
            assert _denied_by("ssh farbox uptime") is None, pad

    def test_a_line_with_no_break_reads_and_splits_each_chunk_once(self, monkeypatch):
        # A cap-sized file with no line break at all: each chunk read is split
        # once on its own (never re-split as part of an accumulated carry), so
        # the characters those chunk splits cover add up to the file size, not
        # to its square; the line is still parsed.  Only the per-chunk split
        # is counted, not the one-piece break checks on its output.
        chunk = 1024
        monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_READ_CHUNK", chunk)
        text = "127.0.0.1 " + "a" * (256 * 1024)
        hosts = self.tmp_path / "hosts"
        hosts.write_text(text)
        split: "list[int]" = []

        class _Tracked(str):
            def splitlines(self, *a, **k):
                split.append(len(self))
                return str.splitlines(self, *a, **k)

        real_open = open

        class _Handle:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._fh.close()

            def read(self, n=-1):
                return _Tracked(self._fh.read(n))

        monkeypatch.setattr(
            _argv_floor, "open", lambda f, *a, **k: _Handle(real_open(f, *a, **k)), raising=False
        )
        table = _argv_floor._parse_hosts_file(str(hosts), frozenset())
        assert table == {"a" * (256 * 1024): True}
        assert sum(split) == len(text)
        assert max(split) <= chunk

    def test_a_failed_read_is_never_cached_and_is_retried(self, monkeypatch):
        # A read that fails once (a transient error, nothing about the file
        # changes) is pending and caches nothing, so the key cannot pin that
        # failure: the very next check reads the file again, a remote alias
        # passes and the loopback alias is refused.  The failure is simulated
        # through the parser; a mode of 0 does not stop the owner reading on
        # Windows or root reading on POSIX.
        path = self._hosts(monkeypatch, "127.0.0.1 loopalias\n10.4.4.4 farbox\n")
        failures = [1]
        real_parse = _argv_floor._parse_hosts_file

        def _parse(p, own, **kw):
            if failures[0]:
                failures[0] -= 1
                raise OSError("transient read error")
            return real_parse(p, own, **kw)

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _parse)
        assert _argv_floor._host_is_self("farbox") is True  # the failing read: pending
        assert path not in _argv_floor._HOSTS_FILE_CACHE
        assert _argv_floor._host_is_self("farbox") is False  # read again: remote
        assert _argv_floor._host_is_self("loopalias") is True

    def test_an_unreadable_large_hosts_file_stays_pending_and_re_schedules(self, monkeypatch):
        # A file over the in-call cap whose background read fails (fd
        # exhaustion, say) is pending on every gate call, and every call
        # re-schedules the warm, so the retry does not hang on one thread.
        path = self._hosts(
            monkeypatch,
            "# pad\n" * (_argv_floor._HOSTS_FILE_READ_CHUNK // 3) + "127.0.0.1 loopalias\n",
        )

        def _emfile(*_a, **_k):
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(_argv_floor, "_parse_hosts_file", _emfile)
        _argv_floor._warm_hosts_file_cache()  # the failing background read
        scheduled: "list[int]" = []
        monkeypatch.setattr(_argv_floor, "_schedule_hosts_file_warm", lambda: scheduled.append(1))
        assert _argv_floor._host_is_self("loopalias") is True
        assert _argv_floor._host_is_self("loopalias") is True
        assert path not in _argv_floor._HOSTS_FILE_CACHE
        assert scheduled == [1, 1]

    def test_the_in_call_cap_counts_bytes_not_characters(self, monkeypatch):
        # Four-byte UTF-8 text puts more than 64 KiB of bytes in fewer than
        # 64 Ki characters.  The inline cap is enforced on the bytes read, so
        # a file swapped in after a small stat is still stopped at the cap.
        path = self._hosts(monkeypatch, "")
        text = "#" + "\U0001f600" * 20000 + "\n10.4.4.4 farbox\n"
        with open(path, "wb") as fh:
            fh.write(text.encode("utf-8"))
        assert len(text) < _argv_floor._HOSTS_FILE_READ_CHUNK
        assert os.stat(path).st_size > _argv_floor._HOSTS_FILE_READ_CHUNK
        real_key = _argv_floor._hosts_file_key

        def _stale_small_key(p):
            key = real_key(p)
            return key[:2] + (16,) + key[3:]

        monkeypatch.setattr(_argv_floor, "_hosts_file_key", _stale_small_key)
        monkeypatch.setattr(_argv_floor, "_schedule_hosts_file_warm", lambda: None)
        assert _argv_floor._host_is_self("farbox") is True
        assert path not in _argv_floor._HOSTS_FILE_CACHE

    def test_the_gate_never_reads_past_the_in_call_cap(self, monkeypatch):
        # The key's stat says the file is small, but it was replaced by a
        # larger one before the open.  The inline read is bounded by the cap
        # itself, so the gate stops at the cap, caches nothing, answers
        # pending and leaves the full parse to the background warm.
        path = self._hosts(
            monkeypatch, "# pad\n" * (_argv_floor._HOSTS_FILE_READ_CHUNK // 3) + "10.4.4.4 farbox\n"
        )
        real_key = _argv_floor._hosts_file_key

        def _stale_small_key(p):
            key = real_key(p)
            return key[:2] + (16,) + key[3:]

        monkeypatch.setattr(_argv_floor, "_hosts_file_key", _stale_small_key)
        scheduled: "list[int]" = []
        monkeypatch.setattr(_argv_floor, "_schedule_hosts_file_warm", lambda: scheduled.append(1))
        assert _argv_floor._host_is_self("farbox") is True
        assert path not in _argv_floor._HOSTS_FILE_CACHE
        assert scheduled == [1]

    # --- Windows: a content digest, since st_ctime is creation time ------

    def _windows(self, monkeypatch):
        """``st_ctime`` pinned to creation time, as on Windows.

        The stat seen by the code under test keeps each path's first
        ``st_ctime`` (its creation time), so a rewrite that restores mtime
        leaves mtime, ctime and size all unchanged, which is the Windows case.
        """
        monkeypatch.setattr(_argv_floor, "_ctime_is_creation_time", lambda: True)
        born: "dict[str, float]" = {}
        real_os = _argv_floor.os

        class _Stat:
            def __init__(self, st, ctime):
                self._st, self.st_ctime = st, ctime

            def __getattr__(self, name):
                return getattr(self._st, name)

        class _WindowsOs:
            def __getattr__(self, name):
                return getattr(real_os, name)

            @staticmethod
            def stat(path, *a, **k):
                st = real_os.stat(path, *a, **k)
                return _Stat(st, born.setdefault(str(path), st.st_ctime))

        monkeypatch.setattr(_argv_floor, "os", _WindowsOs())

    @staticmethod
    def _rewrite_keeping_mtime(path, old, new):
        """Replace *old* with *new* (same length) in place and restore mtime."""
        assert len(old) == len(new)
        st = os.stat(path)
        with open(path, "rb") as fh:
            data = fh.read()
        assert old.encode() in data
        with open(path, "r+b") as fh:
            fh.write(data.replace(old.encode(), new.encode()))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        after = os.stat(path)
        assert (after.st_size, after.st_mtime_ns) == (st.st_size, st.st_mtime_ns)

    def test_windows_same_size_rewrite_with_restored_mtime_re_parses(self, monkeypatch):
        self._windows(monkeypatch)
        path = self._hosts(monkeypatch, "10.44.4.4 swapbox\n")
        assert _argv_floor._host_is_self("swapbox") is False
        self._rewrite_keeping_mtime(path, "10.44.4.4 swapbox", "127.0.0.9 swapbox")
        assert _argv_floor._host_is_self("swapbox") is True
        assert _argv_floor._HOSTS_FILE_CACHE[path][1] == {"swapbox": True}

    def test_windows_large_file_never_serves_a_remote_or_absent_verdict(self, monkeypatch):
        # A Windows hosts file over 64 KiB cannot be verified without a gate
        # read past the chunk cap, so no cached remote or absent answer is
        # served for a dotless name, even straight after a background warm:
        # a same-size rewrite that restores mtime is invisible to its key.
        self._windows(monkeypatch)
        path = self._big_hosts(monkeypatch, "10.44.4.4 swapbox\n127.0.0.1 loopalias\n")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (path,))
        _argv_floor._hosts_file_warm_worker()
        assert _argv_floor._host_is_self("swapbox") is True
        assert _argv_floor._hosts_file_verdict("nobody") is True
        assert _denied_by("ssh dev-dsk uptime") == self._RULE
        self._rewrite_keeping_mtime(path, "10.44.4.4 swapbox", "127.0.0.9 swapbox")
        assert _argv_floor._host_is_self("swapbox") is True
        assert _denied_by("ssh loopalias uptime") == self._RULE

    @pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
    def test_small_file_read_failure_on_the_digest_is_pending(self, monkeypatch, windows):
        if windows:
            self._windows(monkeypatch)
        self._hosts(monkeypatch, "10.44.4.4 swapbox\n")
        _argv_floor._warm_hosts_file_cache()

        def _eio(*_a, **_k):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(_argv_floor, "_read_hosts_bytes", _eio)
        scheduled: "list[int]" = []
        monkeypatch.setattr(_argv_floor, "_schedule_hosts_file_warm", lambda: scheduled.append(1))
        assert _argv_floor._host_is_self("swapbox") is True
        assert scheduled == [1]

    def test_posix_same_size_rewrite_inside_one_ctime_tick_re_parses(self, monkeypatch):
        # Two writes inside one coarse ctime tick leave mtime, ctime and size
        # all unchanged; the stat seam returns the first stat for every call,
        # so no real clock is involved.  The digest is what moves the key.
        own = frozenset({"127.0.0.1", "10.4.4.4"})
        monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", own)
        path = self._hosts(monkeypatch, "10.9.9.9 farbox\n")
        first = os.stat(path)
        real_os = _argv_floor.os

        class _OneTickOs:
            def __getattr__(self, name):
                return getattr(real_os, name)

            @staticmethod
            def stat(_path, *_a, **_k):
                return first

        monkeypatch.setattr(_argv_floor, "os", _OneTickOs())
        assert _argv_floor._hosts_file_verdict("farbox") is False
        with open(path, "r+b") as fh:
            fh.write(b"10.4.4.4 farbox\n")
        assert os.stat(path).st_size == first.st_size
        assert _argv_floor._hosts_file_verdict("farbox") is True
        assert _argv_floor._HOSTS_FILE_CACHE[path][1] == {"farbox": True}

    def test_posix_large_file_keeps_a_stat_key_and_is_served(self, monkeypatch):
        path = self._hosts(monkeypatch, "10.44.4.4 swapbox\n")
        assert _argv_floor._hosts_file_key(path)[3] is not None
        big = self._big_hosts(monkeypatch, "10.44.4.4 swapbox\n", name="hosts-big")
        monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: (big,))
        assert _argv_floor._hosts_file_key(big)[3] is None
        _argv_floor._hosts_file_warm_worker()
        assert _argv_floor._host_is_self("swapbox") is False
