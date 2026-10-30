"""Which names and addresses are this host, for the ssh self-target floor.

``_host_is_self`` answers from four sources: the loopback names every machine answers
to; this machine's own hostname and interface addresses, seeded synchronously from
``socket.gethostname()`` and enriched by a background worker; the hosts-file aliases
that map to a local address; and a per-host DNS verdict cache for a dotted name. Every
blocking read -- DNS, the hosts file, the interface sweep -- runs off the event loop, and
the comments on each cache say which answer it gives while a read is still pending. A new
source of "this is the same machine" goes here.

The hosts-file line parser and its digest live in ``hosts_file.py`` and the per-platform
interface readers in ``host_addresses.py``; this module owns every read, the caches and
the verdicts built on them. ``kiro_crew.security.argv_floor`` serves every name defined
here, and a patch through it reaches this module (:mod:`kiro_crew._facade_mirror`). This
module imports no other part of the argv floor.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Iterator

from kiro_crew.atomic_write import on_event_loop as _event_loop_running

from .host_addresses import (
    _darwin_interface_addresses,
    _linux_netlink_addresses,
    _windows_interface_addresses,
)
from .hosts_file import (
    _ctime_is_creation_time,
    _decoded_chunks,
    _hosts_content_digest,
    _HostsFileTooLarge,
    _HostsFileUnreadable,
    _parse_hosts_chunks,
)

# fcntl/struct drive the Linux per-interface address sweep in
# _resolve_own_host_names_into_cache.  They are imported here at module scope to
# satisfy the top-level-imports rule; both are stdlib on Linux, and fcntl is
# absent on Windows, so the import is guarded and the sweep is gated on
# sys.platform == "linux" regardless.
try:
    import fcntl as _fcntl
    import struct as _struct
except ImportError:  # pragma: no cover - non-Linux platforms lack fcntl
    _fcntl = None  # type: ignore[assignment]
    _struct = None  # type: ignore[assignment]

logger = logging.getLogger("kiro_crew.security.argv_floor")


# Names every machine answers to on loopback, plus the two spellings that
# reach the wildcard/unspecified address (both connect to loopback in
# practice).
_LOOPBACK_HOST_NAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "localhost4",
        "localhost4.localdomain4",
        "localhost6",
        "localhost6.localdomain6",
        "ip6-localhost",
        "ip6-loopback",
        "0",
        "0.0.0.0",
        "::",
        "::1",
    }
)


# bash pathname-expands an unquoted glob against the filesystem before exec,
# so a word carrying one of these can BECOME an ssh-family program or a
# self-host by matching a file that exists (``/usr/bin/s?h`` matches the
# installed client; ``localho?t`` matches a file the agent creates).  The
# deny floor cannot consult the filesystem, so a pattern that CAN match is
# treated as matching -- an over-approximation toward deny.
_GLOB_CHARS: frozenset[str] = frozenset("*?[")


# A hostname the textual layers cannot classify may still resolve to a
# loopback or local address (a DNS alias pointed at 127.0.0.1).  Resolution
# is a blocking network call, so it runs in a worker off the event loop:
# the decision FAILS CLOSED (denied) until the worker publishes a verdict,
# and the verdict is cached per host.  Only dotted, lettered hostname
# shapes reach this layer: IP-literal spellings are classified above it,
# and a dotless word on an ssh command line is ordinarily the remote
# command, not a destination.
# Hostname shape eligible for the DNS-alias verdict layer.  The dotted-part
# is OPTIONAL: a dotless name in a confirmed HOST position may be
# an /etc/hosts loopback alias, and the layer only runs in
# host position, so position -- not punctuation -- keeps junk words out.
_DNS_CANDIDATE_RE = re.compile(r"(?=.*[a-z])[a-z0-9_-]+(\.[a-z0-9_-]+)*\Z")


_HOST_VERDICT_CACHE: "dict[str, bool]" = {}


_HOST_VERDICT_PENDING: "set[str]" = set()


_HOST_VERDICT_LOCK = threading.Lock()


# ponytail: unbounded per-host growth is capped by evicting the oldest entry;
# an LRU adds bookkeeping the floor does not need.
_HOST_VERDICT_CACHE_CAP = 4096


# An ALLOW verdict is not reused unbounded -- DNS rebinding could
# repoint a once-public name at loopback after the first check.  A negative
# entry older than this is served stale ONCE while a single-flight worker
# revalidates it (no recurring first-contact refusal), so the rebinding
# window is bounded by the TTL.  DENY verdicts stay permanent: over-blocking
# is this floor's safe direction.
_HOST_VERDICT_ALLOW_TTL = 300.0


_HOST_VERDICT_STAMP: "dict[str, float]" = {}


def _resolve_host_verdict_into_cache(host: str) -> None:
    """Worker: resolve *host* off-loop and record whether it is local.

    Any resolved address that is loopback/unspecified, or a member of the
    own-address set, marks the host self.  A name that RESOLVES to no local
    address is not self.  A resolution FAILURE caches nothing (round-25): a
    transient DNS error latched as an allow would serve a recovered loopback
    alias for the whole allow-TTL, so the failure only clears the pending
    latch and the next decision retries.
    """
    verdict = False
    resolved_ok = True
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        own = _own_host_names()
        for info in infos:
            addr = str(info[4][0]).split("%", 1)[0].lower()
            try:
                ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr)
            except ValueError:
                continue
            mapped = getattr(ip, "ipv4_mapped", None)
            if mapped is not None:
                ip = mapped
            if ip.is_loopback or ip.is_unspecified or str(ip).lower() in own:
                verdict = True
                break
    except Exception:
        resolved_ok = False
    with _HOST_VERDICT_LOCK:
        if resolved_ok:
            if len(_HOST_VERDICT_CACHE) >= _HOST_VERDICT_CACHE_CAP:
                evicted = next(iter(_HOST_VERDICT_CACHE))
                _HOST_VERDICT_CACHE.pop(evicted)
                _HOST_VERDICT_STAMP.pop(evicted, None)
            _HOST_VERDICT_CACHE[host] = verdict
            _HOST_VERDICT_STAMP[host] = time.monotonic()
        _HOST_VERDICT_PENDING.discard(host)


def _hosts_file_paths() -> "tuple[str, ...]":
    """Platform hosts-file path(s); a seam for tests."""
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return (os.path.join(root, "System32", "drivers", "etc", "hosts"),)
    # Component-assembled per the portability gate's remedy: this branch is
    # POSIX-only by the ``os.name`` guard above, so the path never reaches a
    # Windows process.
    return (os.path.join("/etc", "hosts"),)


# Read cap for a hosts file: covers even the multi-megabyte ad-block variants;
# a file larger than this is read truncated, and a name past the cap simply
# falls through to the async DNS revalidation path, whose resolver reads the
# real (untruncated) hosts database.
_HOSTS_FILE_READ_CAP = 4 * 1024 * 1024


# Characters read per chunk of that bounded read (GIL hold per chunk).  A
# file no larger than one chunk is also small enough for the gate path to
# parse in the same call on a miss: a typical file in well under a
# millisecond, one at the cap in a few.
_HOSTS_FILE_READ_CHUNK = 64 * 1024


_HOSTS_KEY_MOVED_ATTEMPTS = 3  # passes per call; one enrichment pass moves the key at most twice


# path -> (key, {name -> maps-to-local}), key = (mtime, ctime, size, content
# digest or None, published, own set).  Background threads parse
# (``_warm_hosts_file_cache``).  On a miss the gate path on an event loop
# parses in the same call only a file no larger than one read chunk; a larger
# file answers pending and schedules one warm thread.
# A changed file, publication or own-address set is a new key, so an own
# address learned later re-marks an alias.  Two threads
# (the enrichment worker and the on-demand warm) may parse at once without a
# lock: each publishes a complete table in one dict assignment, and only for
# the key it was judged by, so the worst case is a duplicated parse (last
# write wins), never a half-built or stale table.
_HostsKey = tuple[float, float, int, "bytes | None", bool, frozenset[str]]


_HOSTS_FILE_CACHE: "dict[str, tuple[_HostsKey, dict[str, bool]]]" = {}


# Single-flight latch for the on-demand warm the gate path schedules.
_HOSTS_WARM_LOCK = threading.Lock()


_HOSTS_WARM_IN_FLIGHT = False


def _read_hosts_bytes(path: str, limit: int, *, strict: bool) -> bytes:
    """At most *limit* bytes of *path*, read one chunk at a time.

    *strict* raises ``_HostsFileTooLarge`` when the file holds more than
    *limit* (the gate's in-call cap); otherwise the read stops at *limit*,
    the truncation described at ``_HOSTS_FILE_READ_CAP``.
    """
    parts: "list[bytes]" = []
    remaining = limit
    with open(path, "rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(_HOSTS_FILE_READ_CHUNK, remaining))
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
        if strict and remaining <= 0 and fh.read(1):
            raise _HostsFileTooLarge(path)
    return b"".join(parts)


def _hosts_file_key(path: str) -> "_HostsKey":
    """Cache key for *path*: stat identity, content digest, and the own-address state.

    A same-size rewrite that restores mtime must still be a new key, and
    ``st_ctime`` cannot promise it: Windows stamps creation time, Linux a
    coarse clock that two writes inside one tick share.  So a file no
    larger than one read chunk also keys on a blake2b digest from one read
    bounded by the chunk; a read that fails or finds more raises
    ``_HostsFileUnreadable``.  A larger file keeps digest None: on POSIX a
    same-size rewrite inside its previous write's ctime tick keeps the key
    (the one window left); on Windows it is never cached or served, so
    ``_hosts_file_verdict`` answers every dotless name pending.  The last
    two fields are the publication flag and the own set.
    """
    stat = os.stat(path)
    digest: "bytes | None" = None
    if stat.st_size <= _HOSTS_FILE_READ_CHUNK:
        try:
            data = _read_hosts_bytes(path, _HOSTS_FILE_READ_CHUNK, strict=True)
        except (OSError, _HostsFileTooLarge) as exc:
            raise _HostsFileUnreadable(path) from exc
        digest = _hosts_content_digest(data)
    return (
        stat.st_mtime,
        stat.st_ctime,
        stat.st_size,
        digest,
        _NETLINK_ADDRS_PUBLISHED,
        _own_host_names(),
    )


def _parse_hosts_file(
    path: str,
    own: "frozenset[str]",
    limit: "int | None" = None,
    content: "bytes | None" = None,
) -> "dict[str, bool]":
    """``{name -> maps-to-local}`` for *path*, judged against the own set *own*.

    *limit* bounds the read itself, in bytes: the gate passes it so a file
    replaced by a larger one after its stat is never read past the in-call
    cap on the event loop, and raises ``_HostsFileTooLarge`` instead.
    *content*, when given, is parsed instead of reading *path*: the bytes
    already read and hashed for a digest key, so the table is built from
    exactly the content its key names.
    """
    if content is None and limit is not None:
        content = _read_hosts_bytes(path, limit, strict=True)
    if content is not None:
        return _parse_hosts_chunks(_decoded_chunks(content, _HOSTS_FILE_READ_CHUNK), own)
    with open(path, encoding="utf-8", errors="replace") as fh:
        # Bounded read (the repo-wide handle-iteration guard, and a real
        # cap): see _HOSTS_FILE_READ_CAP for the overflow degradation path.
        # Read in chunks; see _parse_hosts_chunks for why and how edges join.
        def _read_chunks() -> "Iterator[str]":
            remaining = _HOSTS_FILE_READ_CAP
            while remaining > 0:
                chunk = fh.read(min(_HOSTS_FILE_READ_CHUNK, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

        return _parse_hosts_chunks(_read_chunks(), own)


def _parse_and_cache_for_key(
    path: str,
    key: "_HostsKey",
    limit: "int | None" = None,
) -> "dict[str, bool] | str":
    """Parse *path* against *key*'s own set; cache and return it if *key* still holds.

    Callers must first rule the key verifiable (``_unverifiable_hosts_file``):
    a Windows key for a file over one chunk carries no digest and is never
    parsed or cached here.

    Returns the table, or a status string when nothing was cached:
    ``"changed"`` when the FILE moved mid-parse (stat identity or digest),
    ``"too-large"`` when the file holds more than *limit*, and ``"failed"``
    when it could not be read or decoded.  A failed parse is never cached,
    so the next gate call reads again; the gate answers it pending (fail
    closed), since a hosts-file alias for this machine cannot be ruled out
    while the file cannot be read.  May raise OSError from the second stat.

    When only the own-address half of the key moved mid-parse (the
    enrichment worker publishes the address table or grows the own set --
    on a cold worker that happens within the first ssh vet, because that
    vet is what starts the worker), the table is returned but NOT cached:
    it was judged against the older own set, so a remote entry could be an
    alias for the newly learned address, and the gate's own-state re-check
    judges exactly those entries again.  The file itself is the one the
    key names, so the table's NAME SET is exact: a local entry stays local
    under a grown own set, and a name absent from the file is absent under
    any own set.  Answering those two from the uncached table is what keeps
    an everyday ``ssh user@dev-dsk`` from being refused by a worker thread
    this same call started.

    When *key* carries a digest (a file no larger than one chunk), the
    bytes are read once, hashed, and parsed from that same buffer, so the
    digest names exactly the content the table came from; a digest that
    differs from *key*'s is ``"changed"``.
    """
    try:
        if key[3] is not None:
            cap = _HOSTS_FILE_READ_CAP if limit is None else limit
            data = _read_hosts_bytes(path, cap, strict=limit is not None)
            if _hosts_content_digest(data) != key[3]:
                return "changed"
            table = _parse_hosts_file(path, key[-1], limit=limit, content=data)
        else:
            table = _parse_hosts_file(path, key[-1], limit=limit)
    except _HostsFileTooLarge:
        return "too-large"
    except (OSError, ValueError):
        return "failed"
    try:
        current = _hosts_file_key(path)
    except _HostsFileUnreadable:
        return "changed"
    if current[:4] != key[:4]:
        return "changed"
    if current != key:
        # Only the publication flag or the own set moved: hand the table
        # back for the local/absent answers, cache nothing.
        return table
    _HOSTS_FILE_CACHE[path] = (key, table)
    return table


def _unverifiable_hosts_file(key: "_HostsKey") -> bool:
    """True for a Windows file over one chunk: no digest, so its content cannot be checked."""
    return key[3] is None and _ctime_is_creation_time()


def _warm_hosts_file_cache() -> None:
    """Parse the hosts file(s) for the current key, off the gate path.

    One parse per path per call (``_parse_and_cache_for_key``).  A changed
    key caches nothing; the gate keeps answering pending for a file over the
    in-call cap, and the next pass, or the next gate miss, retries.  An
    up-to-date table costs a stat and an own-set read (plus, for a file no
    larger than one chunk, a bounded read and hash).  A Windows file over
    one chunk is skipped: the gate never serves its table.  Best-effort.
    """
    for path in _hosts_file_paths():
        try:
            key = _hosts_file_key(path)
            if _unverifiable_hosts_file(key):
                continue
            cached = _HOSTS_FILE_CACHE.get(path)
            if cached is not None and cached[0] == key:
                continue
            _parse_and_cache_for_key(path, key)
        except Exception:
            continue


def _hosts_file_warm_worker() -> None:
    """Thread body for the on-demand warm; clears the single-flight latch."""
    global _HOSTS_WARM_IN_FLIGHT
    try:
        _warm_hosts_file_cache()
    finally:
        with _HOSTS_WARM_LOCK:
            _HOSTS_WARM_IN_FLIGHT = False


def _schedule_hosts_file_warm() -> None:
    """Start one background warm unless one is already running.  Never blocks."""
    global _HOSTS_WARM_IN_FLIGHT
    with _HOSTS_WARM_LOCK:
        if _HOSTS_WARM_IN_FLIGHT:
            return
        _HOSTS_WARM_IN_FLIGHT = True
    try:
        threading.Thread(
            target=_hosts_file_warm_worker, name="kirocrew-hosts-warm", daemon=True
        ).start()
    except Exception:
        # A thread that cannot start leaves the latch clear so a later
        # check retries; this check still answers pending (deny).
        with _HOSTS_WARM_LOCK:
            _HOSTS_WARM_IN_FLIGHT = False


def _hosts_file_verdict(host: str) -> "bool | None":
    """Hosts-file verdict: True local or pending, False remote, None absent/deferred.

    This is the round-18 attack vector itself — a hosts-file alias for a
    loopback/local address — answered from the table cached for the file's
    current key.  Per call the gate stats each file (and for a file no
    larger than one chunk, one bounded read and hash; a miss adds
    two more, for the parse and its key re-check) and reads the
    own-address set (twice when a remote entry is served: once for the
    key, once to confirm it at return; each read is one lock round-trip
    while enrichment is incomplete or due a refresh).  When a path has no
    table for its current key (first check after start, file edited, own
    addresses changed), a file no larger than one read chunk is parsed in
    this call, so a cold dotless target still gets a
    same-call verdict; that read is itself bounded by the chunk size, so a
    file replaced by a larger one after the stat stops at the cap and is
    pending.  A read that fails caches nothing and is pending too (fail
    closed, where main allowed): the next call reads again, inline for a
    small file and by re-scheduling the warm for a large one, so the
    refusal lasts only while the file cannot be read.  A larger file is
    not parsed here: the answer is True, a pending refusal, checked before
    any path's verdict is used, and one single-flight warm thread is
    started; the same command succeeds once the warm lands.  A thread with
    no running event loop (a cron-launched MCP server, or a gateway worker
    thread such as the cron fire-time vet) has no loop to keep free, so it
    parses a larger file in the call exactly as the warm would, read up to
    ``_HOSTS_FILE_READ_CAP`` and truncated past it.

    A same-size rewrite that restores mtime is a new key: for a file no
    larger than one chunk through a content digest taken on every call
    from one read bounded by the chunk size, on every platform, since a
    coarse ctime tick can hide a rewrite; for a larger POSIX file through
    ``st_ctime`` (see ``_hosts_file_key``).  A Windows file over one chunk
    cannot be verified without reading past that bound, so every dotless
    name is pending there (True), with no
    stale window; the refusal note tells the agent to use the full
    hostname or an IP address.  An in-call parse whose FILE changed
    while it ran is also pending; one whose own-address state moved (the
    enrichment worker publishing mid-parse) still answers a local or an
    absent name from the table it built.  A remote answer is given only if
    publication and the own set still match its table's key at return;
    otherwise every path is judged again on the current key, at most
    ``_HOSTS_KEY_MOVED_ATTEMPTS`` passes per call (so at most that many parses
    of one file), then the answer is pending.  A name on several lines is
    local if ANY of them maps local (deny-floor direction).
    """
    for _attempt in range(_HOSTS_KEY_MOVED_ATTEMPTS):
        tables: "list[tuple[_HostsKey, dict[str, bool]]]" = []
        for path in _hosts_file_paths():
            try:
                key = _hosts_file_key(path)
            except _HostsFileUnreadable:
                _schedule_hosts_file_warm()
                return True
            except OSError:
                continue
            if _unverifiable_hosts_file(key):
                return True
            cached = _HOSTS_FILE_CACHE.get(path)
            if cached is None or cached[0] != key:
                on_loop = _event_loop_running()
                if key[2] > _HOSTS_FILE_READ_CHUNK and on_loop:
                    # Each uncached call re-schedules: a failed warm start cannot hang a later call.
                    _schedule_hosts_file_warm()
                    return True
                limit = _HOSTS_FILE_READ_CHUNK if on_loop else None
                try:
                    result = _parse_and_cache_for_key(path, key, limit=limit)
                except OSError:
                    result = "changed"
                if isinstance(result, str):
                    _schedule_hosts_file_warm()
                    return True
                cached = (key, result)
            tables.append(cached)
        remote = []
        for key, table in tables:
            verdict = table.get(host)
            if verdict is True:
                return True
            if verdict is False:
                remote.append(key)
        if not remote:
            return None
        # A not-local entry judged under an own state that has since moved is judged again.
        own_state = (_NETLINK_ADDRS_PUBLISHED, _own_host_names())
        if all((key[-2], key[-1]) == own_state for key in remote):
            # Not-local counts only when judged after publication; while the
            # own-address set is incomplete it defers to the async verdict layer.
            return False if own_state[0] else None
    _schedule_hosts_file_warm()
    return True


def _resolved_host_verdict(host: str, *, fail_closed: bool = True) -> bool:
    """Cached is-self verdict for *host*; *fail_closed* answers a miss.

    A cache hit answers directly.  A miss schedules one single-flight
    resolver worker and answers *fail_closed* for this call — True (denied)
    for dotted names, False (allowed) for dotless names the hosts file does
    not know (round-21: refusing every dotless first contact broke the
    everyday ``ssh dev-dsk <cmd>`` shape) — and a later call reads the
    published verdict.  A worker that cannot start keeps this call's answer
    and clears the latch so a later call retries.
    """
    with _HOST_VERDICT_LOCK:
        if host in _HOST_VERDICT_CACHE:
            verdict = _HOST_VERDICT_CACHE[host]
            # An aged ALLOW is served stale exactly while ONE
            # revalidation worker runs -- rebinding to loopback is caught at
            # the next publish, without a recurring first-contact refusal.
            if (
                not verdict
                and host not in _HOST_VERDICT_PENDING
                and time.monotonic() - _HOST_VERDICT_STAMP.get(host, 0.0) > _HOST_VERDICT_ALLOW_TTL
            ):
                _HOST_VERDICT_PENDING.add(host)
                try:
                    threading.Thread(
                        target=_resolve_host_verdict_into_cache,
                        args=(host,),
                        name="kirocrew-host-verdict",
                        daemon=True,
                    ).start()
                except Exception:
                    _HOST_VERDICT_PENDING.discard(host)
            return verdict
        if host not in _HOST_VERDICT_PENDING:
            _HOST_VERDICT_PENDING.add(host)
            try:
                threading.Thread(
                    target=_resolve_host_verdict_into_cache,
                    args=(host,),
                    name="kirocrew-host-verdict",
                    daemon=True,
                ).start()
            except Exception:
                _HOST_VERDICT_PENDING.discard(host)
    return fail_closed


# This machine's own names/addresses, lowered.  Seeded SYNCHRONOUSLY from
# ``socket.gethostname()`` on first use — a local syscall (uname), not DNS, so
# it is safe on the event loop and closes the resolution race where the first
# ``ssh <own-hostname>`` arrived before any name was known.  The DNS-backed
# enrichment (``socket.getfqdn``/``socket.getaddrinfo``) runs in a BACKGROUND
# thread: those are synchronous network calls, and ``is_denied`` runs inline
# on the gateway's event loop (the PreToolUse gate), where a hung resolve
# would freeze every session — the AUTOSDE no-blocking-call-on-the-loop rule
# names them.  Until enrichment lands the own-name half covers only the
# machine's own reported hostname; the hard-coded loopback half above is
# fully synchronous and never depends on any of this.  A failed enrichment
# retries on a later miss after a backoff rather than caching the failure for
# the process lifetime.
_OWN_HOST_NAMES_CACHE: "frozenset[str] | None" = None


_OWN_HOST_RESOLVE_DONE = False


_OWN_HOST_RESOLVE_LOCK = threading.Lock()


_OWN_HOST_RESOLVE_NEXT_TRY: float = 0.0


_OWN_HOST_RESOLVE_BACKOFF_SECS = 60.0


# When the last COMPLETE resolve published (time.monotonic()).  A complete set
# goes stale after ``_OWN_HOST_REFRESH_SECS`` so an interface address the host
# gains later (VPN attach, DHCP renewal) becomes a self address on a
# long-lived gateway: the stale set keeps being served (never blocks, never
# shrinks) while the single-flight worker re-enumerates and merges.
_OWN_HOST_RESOLVE_STAMP: float = 0.0


_OWN_HOST_REFRESH_SECS = 300.0


# single-flight latch — True while an enrichment worker is alive; gates the
# spawn in ``_own_host_names`` and is cleared in the worker's finally.
_OWN_HOST_RESOLVE_IN_FLIGHT = False


def _own_host_seed() -> "frozenset[str]":
    """The synchronously-knowable own names: gethostname forms + interface IPs.

    Interface addresses MUST be in the seed, not only in the async DNS
    enrichment: the enrichment publishes after the first command is judged, so
    a first ``ssh <own-interface-IP>`` would otherwise be admitted before the
    worker finishes -- a deterministic first-command escape, not a race.  The
    enumeration is local and packet-less (no name resolution of any kind: this
    runs on the event-loop ``is_denied`` path, where a slow resolver would
    stall the gateway) and runs once per process, so the first ssh-family
    command absorbs its cost and every later call reads the cache.
    """
    names: set[str] = set()
    try:
        short = socket.gethostname().strip().lower()
        if short:
            names.add(short)
            names.add(short.split(".", 1)[0])
    except Exception:  # pragma: no cover - hostname lookup is best-effort
        pass
    names |= _own_interface_addresses()
    return frozenset(n for n in names if n)


def _own_interface_addresses() -> "set[str]":
    """This machine's local interface addresses (lowered), best-effort.

    DNS enrichment misses an interface IP that has no DNS record -- a DHCP
    lease, a secondary NIC -- yet ``ssh <that-IP>`` still re-enters THIS host,
    so the own-name set needs them too.  Enumerated from the stdlib in layers,
    each wrapped on its own so a platform missing a facility contributes nothing
    and raises nothing.
    """
    addrs: set[str] = set()
    # This runs inside the synchronous seed on the event-loop ``is_denied``
    # path, so it must never resolve names: every layer below is packet-less
    # local enumeration.  DNS-derived names (getfqdn/getaddrinfo forms) come
    # from the async enrichment worker, off the loop.
    # 1. UDP-connect trick per family: a datagram ``connect`` sends no packet
    #    but binds the primary outbound address for that family, even when it
    #    has no DNS record.  The peers are TEST-NET-2 / documentation addresses,
    #    so nothing is ever contacted.
    for _family, _probe in (
        (socket.AF_INET, ("198.51.100.1", 53)),
        (socket.AF_INET6, ("2001:db8::1", 53)),
    ):
        try:
            with socket.socket(_family, socket.SOCK_DGRAM) as _sock:
                _sock.connect(_probe)
                addrs.add(_sock.getsockname()[0])
        except Exception:
            pass
    # 2. Per-interface sweep for addresses the probe above misses.  fcntl/struct
    #    are module-level optional imports (Windows has no fcntl); the sweep is
    #    gated on the linux guard AND on them being present so it never runs off
    #    Linux regardless.  SIOCGIFADDR's value is Linux-specific; macOS has its
    #    own getifaddrs sweep below.
    if sys.platform.startswith("linux") and _fcntl is not None and _struct is not None:
        try:
            for _idx, _ifname in socket.if_nameindex():
                # SIOCGIFADDR (0x8915): an interface with no IPv4 address raises
                # OSError -- skip it.
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _sock:
                        _packed = _struct.pack("256s", _ifname.encode()[:15])
                        _info = _fcntl.ioctl(_sock.fileno(), 0x8915, _packed)
                        addrs.add(socket.inet_ntoa(_info[20:24]))
                except Exception:
                    pass
            # IPv6 has no ioctl equivalent; /proc/net/if_inet6 lists them, the
            # first whitespace field being 32 hex chars.
            try:
                with open("/proc/net/if_inet6") as _fh:
                    for _line in _fh:
                        try:
                            addrs.add(str(ipaddress.IPv6Address(int(_line.split()[0], 16))))
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception:
            pass
    # 3. Windows per-adapter sweep, the sibling of the Linux one:
    #    GetAdaptersAddresses reads the local adapter table (no resolver, no
    #    packet), covering the secondary/VPN addresses the route-selected
    #    probes miss.  The helper self-gates: off Windows it returns empty.
    addrs |= _windows_interface_addresses()
    # 4. macOS per-interface sweep, the getifaddrs sibling of the two above:
    #    a pure local-table read (no resolver, no packet) covering the
    #    secondary/VPN addresses the route-selected probes miss.  The helper
    #    self-gates: off macOS it returns empty.
    addrs |= _darwin_interface_addresses()
    # Drop scope ids (``fe80::1%eth0``) so every result parses as a bare
    # address, and lower/skip empties.
    out: set[str] = set()
    for _addr in addrs:
        _norm = str(_addr).split("%", 1)[0].strip().lower()
        if _norm:
            out.add(_norm)
    return out


# Whether the netlink RTM_GETADDR layer has published.  The dump
# is a blocking socket read, so it runs in the enrichment WORKER, never the
# synchronous seed -- and until its addresses land, a non-own IP literal in
# host position could be an unlisted secondary of this very machine, so
# ``_host_is_self`` denies those (fail closed).  Off Linux there is no
# netlink layer to wait for, so the window starts closed.
_NETLINK_ADDRS_PUBLISHED: bool = not (
    sys.platform.startswith("linux") and hasattr(socket, "AF_NETLINK")
)


def warm_own_host_names() -> None:
    """Start the own-address enrichment worker now instead of at the first ssh.

    Without it the first IP-literal ssh check of a process is what starts the
    worker, and that check sees the still-unpublished flag in the same instant,
    so it is always refused.  The worker reads the netlink table before any DNS
    lookup and publishes it at once.  This only runs the synchronous seed and
    schedules the worker; the gateway startup hook calls it through
    ``asyncio.to_thread`` so the seed stays off the event loop.
    """
    _own_host_names()


def _publish_netlink_addresses(addrs: "set[str]") -> None:
    """Merge the netlink table into the own-name cache, THEN open the window.

    The order is load-bearing: flipping ``_NETLINK_ADDRS_PUBLISHED`` before
    the addresses are in the cache would let a concurrent check see the
    window open while an own secondary IP is still missing from the set,
    and admit it.
    """
    global _OWN_HOST_NAMES_CACHE, _NETLINK_ADDRS_PUBLISHED
    with _OWN_HOST_RESOLVE_LOCK:
        base = _OWN_HOST_NAMES_CACHE if _OWN_HOST_NAMES_CACHE is not None else _own_host_seed()
        _OWN_HOST_NAMES_CACHE = base | frozenset(a for a in addrs if a)
        _NETLINK_ADDRS_PUBLISHED = True


# Consecutive worker passes whose netlink dump did not complete.  A host
# where every dump fails keeps IP-literal ssh refused for good, so the third
# miss in a row logs one warning an operator can find.
_NETLINK_MISSES = 0


_NETLINK_MISS_WARN_AT = 3


def _note_netlink_result(ok: bool) -> None:
    global _NETLINK_MISSES
    _NETLINK_MISSES = 0 if ok else _NETLINK_MISSES + 1
    if _NETLINK_MISSES == _NETLINK_MISS_WARN_AT:
        logger.warning(
            "own-address netlink read has not completed in %d attempts; ssh/scp/sftp/rsync "
            "to IP-literal targets stays refused until it does",
            _NETLINK_MISSES,
        )


def _resolve_own_host_names() -> "tuple[frozenset[str], bool]":
    """Resolve this machine's own hostname/FQDN/addresses (lowered).

    Blocking (DNS) — call from a worker thread, never on the event loop.
    Returns ``(resolved, complete)``: *resolved* is the set of names/addresses
    found (empty when nothing could be resolved), and *complete* is False when
    the FQDN lookup or ANY per-name address lookup raised — so a pass that
    enriched only some names is not mistaken for a full one.  The set content is
    unchanged by *complete*; a caller publishes the partial set but withholds the
    resolved-once latch when it is False.
    """
    names: set[str] = set(_own_host_seed())
    complete = True
    # The netlink RTM_GETADDR dump lists EVERY assigned address (secondary
    # IPv4s the SIOCGIFADDR sweep cannot see).  Its recv blocks, so it lives
    # here in the worker.  Unlike the sweeps below it is LOAD-BEARING: the
    # IP-literal window stays closed until it publishes, so an empty pass on
    # a netlink-capable host keeps ``complete`` False and the backoff retry
    # alive rather than caching a table-less process for its lifetime.
    #
    # It runs FIRST and publishes at once: it is a kernel-local read, while
    # the DNS lookups below can take many seconds on a host whose name is not
    # in DNS, and every IP-literal ssh is refused until this publishes.
    nl = _linux_netlink_addresses()
    if nl:
        _note_netlink_result(True)
        names |= nl
        _publish_netlink_addresses(nl)
    elif sys.platform.startswith("linux") and hasattr(socket, "AF_NETLINK"):
        complete = False
        _note_netlink_result(False)
    # Parse the hosts file now, before the DNS lookups below (which can take
    # seconds), with or without a netlink dump: until a table is cached a
    # dotless ssh target is parsed for in the gate call (small file) or
    # refused as pending (large file).  The pass re-warms after the
    # DNS merge, which re-parses only if DNS added an own address.
    _warm_hosts_file_cache()
    try:
        fqdn = socket.getfqdn().strip().lower()
        if fqdn and fqdn != "localhost":
            names.add(fqdn)
    except Exception:  # pragma: no cover - fqdn lookup is best-effort
        complete = False
    for name in sorted(names):
        try:
            for info in socket.getaddrinfo(name, None):
                # Drop an IPv6 zone id: ``getaddrinfo`` can return a
                # scoped spelling (``fe80::1%eth0``) for a link-local address,
                # which would never equal the bare ``fe80::1`` a command names,
                # so strip it before caching -- the same normalization the
                # operand side does in ``_host_is_self``.
                addr = str(info[4][0]).split("%", 1)[0].strip().lower()
                if addr:
                    names.add(addr)
        except Exception:
            complete = False
            continue
    # Interface addresses DNS does not know are merged in here.  A miss inside
    # the enumeration is NOT an incomplete resolve -- the retry latch is for DNS
    # enrichment, and a host with no IPv6 route is not a partial pass -- so this
    # never touches ``complete`` (and the helper is best-effort, never raising).
    names |= _own_interface_addresses()
    return frozenset(n for n in names if n), complete


def _resolve_own_host_names_into_cache() -> None:
    """Worker-thread body: publish resolved names, latch DONE only when complete.

    Merges into the existing cache (a later partial pass never SHRINKS it) and
    publishes whenever anything resolved, so partial results still protect.  The
    resolved-once latch (``_OWN_HOST_RESOLVE_DONE``) is set ONLY on a complete
    resolve: an incomplete one leaves it False so ``_own_host_names`` keeps
    scheduling the backoff retry for the names that failed to enrich, instead of
    caching a partial set for the process lifetime.  The worker clears the
    single-flight latch on exit (success, partial, or raise) so the backoff
    retry can spawn again.
    """
    global _OWN_HOST_NAMES_CACHE, _OWN_HOST_RESOLVE_DONE, _OWN_HOST_RESOLVE_IN_FLIGHT
    global _OWN_HOST_RESOLVE_STAMP
    try:
        resolved, complete = _resolve_own_host_names()
        if resolved:
            # Under the lock: ``_publish_netlink_addresses`` merges into the
            # same cache mid-pass, and an unlocked read-modify-write here
            # could drop its addresses after the window already opened.
            with _OWN_HOST_RESOLVE_LOCK:
                existing = _OWN_HOST_NAMES_CACHE or frozenset()
                _OWN_HOST_NAMES_CACHE = existing | resolved
        if complete:
            _OWN_HOST_RESOLVE_DONE = True
            _OWN_HOST_RESOLVE_STAMP = time.monotonic()
        # Re-warm after the DNS-derived own addresses are merged, and on every
        # refresh pass: a grown own set re-parses, an unchanged one costs a
        # stat and an own-set read.
        _warm_hosts_file_cache()
    finally:
        with _OWN_HOST_RESOLVE_LOCK:
            _OWN_HOST_RESOLVE_IN_FLIGHT = False


def _own_host_names() -> "frozenset[str]":
    """The own-name set: the synchronous seed at once, DNS enrichment later.

    Non-blocking: the first call publishes the gethostname seed inline (so an
    own-hostname target is denied from the very first command — no resolution
    race), then kicks the DNS enrichment off in a daemon thread and returns
    whatever is published.  The enrichment is single-flight: at most one worker
    at a time, retried after the backoff while it keeps failing.
    """
    global _OWN_HOST_NAMES_CACHE, _OWN_HOST_RESOLVE_NEXT_TRY, _OWN_HOST_RESOLVE_IN_FLIGHT
    if (
        _OWN_HOST_RESOLVE_DONE
        and time.monotonic() - _OWN_HOST_RESOLVE_STAMP < _OWN_HOST_REFRESH_SECS
    ):
        cached = _OWN_HOST_NAMES_CACHE
        return cached if cached is not None else frozenset()
    with _OWN_HOST_RESOLVE_LOCK:
        if _OWN_HOST_NAMES_CACHE is None:
            _OWN_HOST_NAMES_CACHE = _own_host_seed()
        now = time.monotonic()
        if now >= _OWN_HOST_RESOLVE_NEXT_TRY and not _OWN_HOST_RESOLVE_IN_FLIGHT:
            _OWN_HOST_RESOLVE_NEXT_TRY = now + _OWN_HOST_RESOLVE_BACKOFF_SECS
            _OWN_HOST_RESOLVE_IN_FLIGHT = True
            try:
                threading.Thread(
                    target=_resolve_own_host_names_into_cache,
                    name="kirocrew-own-host-resolve",
                    daemon=True,
                ).start()
            except Exception:
                # A thread that cannot start (resource exhaustion) must not
                # abort the permission decision: clear the latch so a later
                # call retries the worker, and answer from the synchronous
                # seed already cached above.
                _OWN_HOST_RESOLVE_IN_FLIGHT = False
        return _OWN_HOST_NAMES_CACHE


def _host_is_self(host: str, *, dns_fallback: bool = True) -> bool:
    """True if *host* (already isolated) names this machine.

    Checks the hard-coded loopback names, IP-literal forms — bare IPv6
    (``::1``, ``::ffff:127.0.0.1``), and the numeric IPv4 spellings
    ``inet_aton`` accepts (``2130706433``, ``0x7f000001``, ``0177.0.0.1``) —
    and finally the resolved own-name set.  The DNS-alias verdict layer at the
    end runs only with ``dns_fallback`` (round-17): callers set it for tokens
    in HOST POSITION, so a dotted local FILENAME (``backup.tar.gz`` as an scp
    source) is never refused as an unresolved first-contact hostname.
    """
    host = host.rstrip(".")
    if not host:
        return False
    # Strip an IPv6 zone id (``fe80::1%eth0`` -> ``fe80::1``) before any
    # address compare: the scope suffix never changes which address
    # this is, and Python hands back both spellings, so the token and the
    # cached own-address must reduce to the same bare form.  Scoped to
    # IPv6-ish tokens (a ``:`` is present) so an ordinary hostname carrying a
    # ``%`` is left untouched.
    if ":" in host and "%" in host:
        host = host.split("%", 1)[0]
    if _GLOB_CHARS.intersection(host):
        # A glob operand expands against the filesystem before exec, so a
        # pattern that CAN match a self name is treated as one (see
        # ``_GLOB_CHARS``); a pattern that cannot match any self name is
        # not a self target, and no later layer parses a glob.
        candidates = set(_LOOPBACK_HOST_NAMES) | {"127.0.0.1", "::1"} | _own_host_names()
        return any(fnmatch.fnmatchcase(cand, host) for cand in candidates)
    if host in _LOOPBACK_HOST_NAMES:
        return True
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
        # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) explicitly: is_loopback
        # only delegates to the mapped address from Python 3.12.4 on, and
        # requires-python admits older 3.12 micros where it reads False.
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if ip.is_loopback or ip.is_unspecified:
            return True
        # Read the window flag BEFORE the names: the publisher stores the
        # addresses and then sets the flag under one lock, so a True flag seen
        # first guarantees the names read next already hold the netlink table.
        published = _NETLINK_ADDRS_PUBLISHED
        if str(ip).lower() in _own_host_names():
            return True
        if dns_fallback and not published:
            # Same unread-table window as the ``inet_aton`` branch below --
            # this branch is the one IPv6 literals take.
            return True
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)
        ip4 = ipaddress.IPv4Address(packed)
        if ip4.is_loopback or ip4.is_unspecified:
            return True
        published = _NETLINK_ADDRS_PUBLISHED
        if str(ip4) in _own_host_names():
            return True
        if dns_fallback and not published:
            # The kernel address table is unread; this literal could be an
            # unlisted secondary of this machine.  Deny until the worker
            # publishes -- host position only.
            return True
    except OSError:
        pass
    if host in _own_host_names():
        return True
    # A hostname that survives every textual layer may still be a DNS alias
    # for a loopback/local address; the off-loop verdict layer classifies
    # it and the decision fails closed until the verdict is published.
    # Consulted only in host position (``dns_fallback``).
    if dns_fallback and _DNS_CANDIDATE_RE.fullmatch(host) is not None:
        if "." not in host:
            # A DOTLESS name is the hosts-file alias class (a
            # loopback alias in /etc/hosts) -- and the hosts table answers that
            # vector SAME-CALL (a file over one read chunk is pending, i.e.
            # refused, until a background parse for the current key lands).  Absent an entry, answer
            # OPEN while one async worker revalidates through DNS: the
            # fail-closed first contact broke the everyday ``ssh dev-dsk``
            # shape (CI allow pin), and a dotless loopback alias that lives
            # only in DNS search domains is caught at the next decision
            # (documented residual).  Dotted names keep fail-closed.
            hosts_verdict = _hosts_file_verdict(host)
            if hosts_verdict is not None:
                return hosts_verdict
            return _resolved_host_verdict(host, fail_closed=False)
        return _resolved_host_verdict(host)
    return False
