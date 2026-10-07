"""The sandbox-escape floor: refuse an ssh-family connection back into this host.

The agent's shell runs inside a sandbox and sshd does not, so a connection whose
target is this same machine re-enters it outside every control the security
package enforces. ``_is_ssh_to_self`` reads the target in operand position and
resolves it against this host's own names, addresses and hosts file.

It lives apart from ``argv_floor`` so that module keeps headroom under the
anti-monolith cap. The facade (``kiro_crew.security``) exports every name here,
routed to this module.

Layer. Like ``argv_floor`` this module reads the shell reader's word layer and
the address/hosts-file plumbing below it. It reads two argv-floor helpers
(``_self_token_frames``, ``_static_substitution_output``) as attributes of that
module, so the dependency runs one way: ``argv_floor`` never imports this module.
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

# fcntl/struct drive the Linux per-interface address sweep in
# _resolve_own_host_names_into_cache.  They are imported here at module scope to
# satisfy the top-level-imports rule; both are stdlib on Linux, and fcntl is
# absent on Windows, so the import is guarded and the sweep is gated on
# sys.platform == "linux" regardless (round-8).
try:
    import fcntl as _fcntl
    import struct as _struct
except ImportError:  # pragma: no cover - non-Linux platforms lack fcntl
    _fcntl = None  # type: ignore[assignment]
    _struct = None  # type: ignore[assignment]

from . import argv_floor as _argv_floor
from . import shell_normalizer as _shell_normalizer
from .host_addresses import (  # noqa: F401  (parser re-imported as a test entry point)
    _darwin_interface_addresses,
    _linux_netlink_addresses,
    _parse_netlink_addr_dump,
    _windows_interface_addresses,
)
from .hosts_file import (  # noqa: F401  (re-bound here: the seam tests monkeypatch)
    _decoded_chunks,
    _hosts_content_digest,
    _hosts_content_digest_enabled,
    _HostsFileTooLarge,
    _HostsFileUnreadable,
    _parse_hosts_chunks,
)
from .shell_normalizer import (
    _argv_programs,
    _cut_at_operator,
    _data_consumer_exempt,
    _decode_shell_quoted_literals,
    _ends_argv,
    _program_basename,
    _resolve_param_defaults,
    _strip_redirect,
    _substitution_depth_delta,
    _xargs_here_string_rebuild,
)

logger = logging.getLogger(__name__)


# ── Sandbox-escape floor (ssh back into this same host) ──
# The agent's shell runs inside a sandbox; sshd does not.  A connection whose
# TARGET is this same machine re-enters it outside every control in this
# module, so the floor resolves the target in OPERAND POSITION — behind
# options (``ssh -p 22 localhost``), redirects, a ``user@`` prefix, URI
# schemes, bracketed/bare IPv6 and numeric IPv4 literals, and quoting the
# raw-text regex cannot see through — and refuses it when it names this host.
#
# Named residuals, fail-open BY DESIGN (failing closed on any of these would
# deny every legitimate remote ssh, which this box's workflows depend on):
# config-level routing that needs ssh's own config resolution (``-F`` files,
# ssh_config Host aliases), and a substitution that only resolves to this
# host at run time (``$(some-command)``).  The literal
# ``hostname``-substitution spellings and ``-o hostname=``/``-o proxyjump=``
# values ARE checked below, as text, and a ``-o proxycommand=`` value is
# checked twice (round-26): the floor recurses on it as a command line AND
# scans it for a LITERAL self endpoint (``nc 127.0.0.1 22`` relays the
# session to the local sshd with no ssh verb for the recursion to see) --
# a resolvable ALIAS inside such a transport stays in the residual class.
# The alias fail-open above is scoped: a DOTLESS alias is answered from the
# hosts file same-call (open) with async revalidation; a DOTTED alias that
# resolves is classified by its addresses; a DOTTED name whose lookup FAILS
# is refused per call, deliberately -- answering a failed lookup open would
# let whoever controls resolution mint an allow by suppressing it, the
# same class the verdict cache refuses to latch.
#
# The strictly larger residual is OUT OF GATE SCOPE by the module's own
# doctrine (a script body is never a gate subject): interpreter/script-file
# indirection (``bash escape.sh``, ``python -c`` + subprocess or paramiko)
# and non-ssh-family clients (``git clone ssh://localhost/…``, autossh) reach
# the unsandboxed sshd without an ssh-family command line ever crossing this
# floor.  The sandbox has no network namespace, so loopback:22 stays
# reachable from inside; this floor is the interim tier, and the fix of
# record is an OS-level network fence in the sandbox (tracked follow-up:
# see the sandbox-escape residuals note in docs/system-specs/modules/security.md).

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

# Operand spellings that resolve to this host's name at run time, matched as
# TEXT inside a single argv token.  The substitution forms are substring
# matches ("$(hostname -f)" still hits); the variable forms are exact or
# dot-suffixed ("$hostname", "$hostname.example") so an unrelated variable
# like "$hostname_backup" is NOT read as this host.
_HOSTNAME_SUBSTITUTION_HINTS: tuple[str, ...] = ("$(hostname", "`hostname")
_HOSTNAME_VARIABLE_FORMS: tuple[str, ...] = ("$hostname", "${hostname}")

# A FLAT command substitution -- no nested substitution characters in the
# body -- is the only shape ``_static_substitution_output`` can decide
# statically.  Both spellings are matched; group 1 xor group 2 carries the
# body.
_FLAT_SUBSTITUTION_RE = re.compile(r"\$\(([^()`$]*)\)|`([^`()$]*)`")

# bash pathname-expands an unquoted glob against the filesystem before exec,
# so a word carrying one of these can BECOME an ssh-family program or a
# self-host by matching a file that exists (``/usr/bin/s?h`` matches the
# installed client; ``localho?t`` matches a file the agent creates).  The
# deny floor cannot consult the filesystem, so a pattern that CAN match is
# treated as matching -- an over-approximation toward deny.
_GLOB_CHARS: frozenset[str] = frozenset("*?[")


def _glob_can_name_ssh_verb(probe: str) -> bool:
    """True when a glob word in *probe* can expand to an ssh-family program."""
    for word in probe.split():
        if not _GLOB_CHARS.intersection(word):
            continue
        base = word.rsplit("/", 1)[-1]
        for verb in _SSH_FAMILY_VERBS:
            if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base):
                return True
    return False


# A hostname the textual layers cannot classify may still resolve to a
# loopback or local address (a DNS alias pointed at 127.0.0.1).  Resolution
# is a blocking network call, so it runs in a worker off the event loop:
# the decision FAILS CLOSED (denied) until the worker publishes a verdict,
# and the verdict is cached per host.  Only dotted, lettered hostname
# shapes reach this layer: IP-literal spellings are classified above it,
# and a dotless word on an ssh command line is ordinarily the remote
# command, not a destination.
# Hostname shape eligible for the DNS-alias verdict layer.  The dotted-part
# is OPTIONAL (round-18): a dotless name in a confirmed HOST position may be
# an /etc/hosts loopback alias, and since round-17 the layer only runs in
# host position, so position -- not punctuation -- keeps junk words out.
_DNS_CANDIDATE_RE = re.compile(r"(?=.*[a-z])[a-z0-9_-]+(\.[a-z0-9_-]+)*\Z")
_HOST_VERDICT_CACHE: "dict[str, bool]" = {}
_HOST_VERDICT_PENDING: "set[str]" = set()
_HOST_VERDICT_LOCK = threading.Lock()
# ponytail: unbounded per-host growth is capped by evicting the oldest entry;
# an LRU adds bookkeeping the floor does not need.
_HOST_VERDICT_CACHE_CAP = 4096
# round-18: an ALLOW verdict is not reused unbounded -- DNS rebinding could
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


# path -> (key, {name -> maps-to-local}), key = (mtime, ctime, size, content
# digest or None, published, own set).  Background threads parse
# (``_warm_hosts_file_cache``).  On a miss the gate path parses in the
# same call only a file no larger than one read chunk; a larger file
# answers pending and schedules one warm thread.
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

    A same-size rewrite that restores mtime must still be a new key.  On
    POSIX ``st_ctime`` catches it: any write or chmod sets ctime, and no
    extra read is done (the digest field is None).  On Windows
    ``st_ctime`` is creation time and catches nothing, so for a file no
    larger than one read chunk the key also carries a blake2b digest of
    that file, from one read bounded by the chunk size; a read that fails
    or finds more than a chunk raises ``_HostsFileUnreadable``.  A Windows
    file over one chunk keeps digest None and is never cached or served:
    its content cannot be verified without a gate read past the chunk, so
    ``_hosts_file_verdict`` answers every dotless name pending.  The last
    two fields are the publication flag and the own set.
    """
    stat = os.stat(path)
    digest: "bytes | None" = None
    if _hosts_content_digest_enabled() and stat.st_size <= _HOSTS_FILE_READ_CHUNK:
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
    ``"changed"`` when the key moved mid-parse (the enrichment worker can
    publish the address table or grow the own set, and a table built across
    that change could mark an alias for the new address remote),
    ``"too-large"`` when the file holds more than *limit*, and ``"failed"``
    when it could not be read or decoded.  A failed parse is never cached,
    so the next gate call reads again; the gate answers it pending (fail
    closed), since a hosts-file alias for this machine cannot be ruled out
    while the file cannot be read.  May raise OSError from the second stat.

    When *key* carries a digest (Windows), the bytes are read once, hashed,
    and parsed from that same buffer, so the digest names exactly the
    content the table came from; a digest that differs from *key*'s is
    ``"changed"``.
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
        if _hosts_file_key(path) != key:
            return "changed"
    except _HostsFileUnreadable:
        return "changed"
    _HOSTS_FILE_CACHE[path] = (key, table)
    return table


def _unverifiable_hosts_file(key: "_HostsKey") -> bool:
    """True for a Windows file over one chunk: no digest, so its content cannot be checked."""
    return key[3] is None and _hosts_content_digest_enabled()


def _warm_hosts_file_cache() -> None:
    """Parse the hosts file(s) for the current key, off the gate path.

    One parse per path per call (``_parse_and_cache_for_key``).  A changed
    key caches nothing; the gate keeps answering pending for a file over the
    in-call cap, and the next pass, or the next gate miss, retries.  An
    up-to-date table costs a stat and an own-set read (plus, on Windows, a
    bounded read and hash).  A Windows file over one chunk is skipped: the
    gate never serves its table.  Best-effort.
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
    current key.  Per call the gate stats each file (on Windows, also one
    bounded read and hash of a file no larger than one chunk; a miss adds
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
    started; the same command succeeds once the warm lands.

    A same-size rewrite that restores mtime is a new key: on POSIX through
    ``st_ctime``, with no extra read; on Windows, where ``st_ctime`` is
    creation time, through a content digest taken on every call from one
    read bounded by the chunk size, for a file no larger than one chunk.
    A Windows file over one chunk cannot be verified without reading past
    that bound, so every dotless name is pending there (True), with no
    stale window; the refusal note tells the agent to use the full
    hostname or an IP address.  An in-call parse whose key changed
    while it ran is also pending.  A remote answer is given
    only if publication and the own set still match the table's key at the
    moment of return.  A name on several lines is local if ANY of them maps
    local (deny-floor direction).
    """
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
            if key[2] > _HOSTS_FILE_READ_CHUNK:
                # Every call on an uncached key re-schedules the warm, so a
                # retry does not hang on one thread that failed to start.
                _schedule_hosts_file_warm()
                return True
            try:
                result = _parse_and_cache_for_key(path, key, limit=_HOSTS_FILE_READ_CHUNK)
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
    # The worker may have published or grown the own set since the key was
    # read; a not-local entry judged by the older state is not served.
    own_state = (_NETLINK_ADDRS_PUBLISHED, _own_host_names())
    if any((key[-2], key[-1]) != own_state for key in remote):
        _schedule_hosts_file_warm()
        return True
    # Not-local counts only when judged after publication; while the
    # own-address set is incomplete it defers to the async verdict layer.
    # (Every remote key's publication bit equals own_state[0] here.)
    return False if own_state[0] else None


def _resolved_host_verdict(host: str, *, fail_closed: bool = True) -> bool:
    """Cached is-self verdict for *host*; *fail_closed* answers a miss.

    A cache hit answers directly.  A miss schedules one single-flight
    resolver worker and answers *fail_closed* for this call — True (denied)
    for dotted names, False (allowed) for dotless names the hosts file does
    not know (round-21: refusing every dotless first contact broke the
    everyday ``ssh devbox <cmd>`` shape) — and a later call reads the
    published verdict.  A worker that cannot start keeps this call's answer
    and clears the latch so a later call retries.
    """
    with _HOST_VERDICT_LOCK:
        if host in _HOST_VERDICT_CACHE:
            verdict = _HOST_VERDICT_CACHE[host]
            # round-18: an aged ALLOW is served stale exactly while ONE
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


# ``${VAR:-word}`` / ``${VAR:=word}`` / ``${VAR:+word}`` — bash substitutes the
# embedded WORD before exec, so a self-host hiding in the default IS the
# destination when the variable is unset (``ssh "${TARGET:-localhost}"``).
# The word is statically visible, so it is checked; a bare ``$VAR`` whose
# value only exists at run time remains the documented fail-open residual.
_EXPANSION_DEFAULT_RE = re.compile(r"\$\{[^{}:]*:[-=+?]([^{}]+)\}")

_SSH_FAMILY_VERBS: frozenset[str] = frozenset({"ssh", "scp", "sftp", "rsync"})

# Verbs that place a copy (or a link) of their SOURCE operand at their
# DESTINATION operand.  A destination whose source is an ssh-family binary
# IS that binary under a new name, so the walk binds it as the verb for the
# rest of the line (round-20).
_BINARY_COPY_VERBS: frozenset[str] = frozenset({"cp", "mv", "ln", "install"})

# A leading environment-assignment word (``FOO=bar cmd``) keeps the NEXT
# token in program position.  The walk reads lowercased text.
_ENV_ASSIGN_PREFIX_RE = re.compile(r"[a-z_][a-z0-9_]*=")

# rsync execs TWO env-var command values FROM HERE: ``RSYNC_RSH`` (its remote
# shell, exactly as ``-e``/``--rsh``) and ``RSYNC_CONNECT_PROG`` (its
# daemon-mode proxy for reaching ``rsync://`` / ``host::module``
# destinations).  A LEADING assignment (``RSYNC_RSH='ssh localhost' rsync …``)
# rides BEFORE the verb, where the operand walk in ``_is_ssh_to_self`` never
# sees it.  The captured value is a command line rsync execs FROM HERE, so it
# recurses the floor.  Tokens reach the walk lowercased; IGNORECASE keeps the
# match robust regardless.  The remote-shell env vars of OTHER verbs
# (``GIT_SSH_COMMAND`` and the like) stay a documented residual, deliberately
# out of this floor's scope.
_RSYNC_RSH_ASSIGN_RE = re.compile(r"^(rsync_rsh|rsync_connect_prog)=(.+)$", re.IGNORECASE)

# Sentinel bytes standing in for an ``_ends_argv`` boundary character that was
# QUOTED or backslash-escaped in the raw source, so it is literal DATA rather
# than a command separator.  ``_self_tokens`` runs shlex, which resolves quotes
# BEFORE the operand walk in ``_is_ssh_to_self`` runs, so by token time a quoted
# ``a;b`` is indistinguishable from a glued unquoted operator ``a;b`` -- yet the
# first is one operand and the second is a command boundary.  The distinguishing
# information exists only in the SOURCE, before tokenization, so the raw text is
# scanned there and every data separator is rewritten to a sentinel that
# ``_ends_argv`` does not treat as a boundary.  A real, unquoted separator keeps
# its character and still ends the walk.  The table covers EVERY character that
# can make ``_ends_argv`` end the walk (``;`` ``|`` ``&`` newline ``#`` ``(``
# ``{``) -- covering only a subset would let a quoted spelling of the missing
# one hide a self-host operand behind a fake boundary (``scp '&' localhost:/x``
# stops the walk at ``&`` with the target unexamined).  Control bytes are used
# because a real shell command never contains them.
_QUOTED_SEP_SENTINELS = {
    ";": "\x00",
    "|": "\x01",
    "&": "\x02",
    "\n": "\x03",
    "#": "\x04",
    "(": "\x05",
    "{": "\x06",
}


def _mask_quoted_separators(text: str) -> str:
    """Rewrite quoted/escaped ``_ends_argv`` boundary chars in *text* to sentinels.

    Single-pass quote-state scan over the RAW source:

    * inside single quotes -- boundary chars are literal data, so they become
      sentinels; a single quote takes no escapes, so a backslash inside is an
      ordinary character.
    * inside double quotes -- boundary chars are literal data, so they become
      sentinels; a backslash does not change a separator's literalness, so each
      character is handled on its own.
    * outside quotes -- a backslash escapes the next character, so an escaped
      boundary char is data and becomes a sentinel (the backslash is kept so
      shlex still de-escapes downstream, and an escaped quote never opens a
      context); the one exception is backslash-newline, a line continuation,
      handled at the escape branch.
    * every other character is copied verbatim; an unterminated quote leaves the
      rest of the string quoted, matching how bash reads it.

    Idempotent for this floor's purposes: a sentinel byte already present in
    hostile input is left in place, and ``_unmask_separators`` later turns it
    into a real separator -- which only ends the walk EARLIER, widening the deny
    (the safe direction).  The mask feeds ``_self_tokens`` (shlex) and recursive
    floor calls, both of which discard quotes anyway, so it never changes the
    resolved tokens beyond neutralizing the boundary ambiguity it exists to
    close.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is None:
            if ch == "\\" and i + 1 < n:
                # Escaped char: an escaped separator is data (mask it); keep the
                # backslash so shlex de-escapes downstream, and consume the next
                # char here so an escaped quote never opens a quote context.
                # EXCEPTION: backslash-newline is a LINE CONTINUATION -- bash
                # (and posix shlex) glue the surrounding text into one token
                # (``local\<newline>host`` resolves to ``localhost``), so it is
                # neither data nor a separator; masking it would corrupt the
                # glued token and un-match a continued self-host spelling.
                nxt = text[i + 1]
                out.append(ch)
                if nxt == "\n":
                    out.append(nxt)
                else:
                    out.append(_QUOTED_SEP_SENTINELS.get(nxt, nxt))
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
            out.append(ch)
        elif quote == '"' and ch == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
            # Inside DOUBLE quotes bash honors ``\`` only before ``"  \  $  ` ``
            # (round-8): ``\"`` is a literal quote, NOT a close.  Copy both bytes
            # verbatim and skip the next char so the escaped quote never ends the
            # quote context -- otherwise ``scp "a\";b" localhost:/x`` would exit
            # the quote at ``\"`` and read the following ``;`` as a real
            # separator, ending the operand walk before the self-host target.
            # Single quotes honor no escape, so this is scoped to ``"``.
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        elif ch == quote:
            quote = None
            out.append(ch)
        else:
            # Inside a quote: a separator is literal data; everything else
            # (backslash included) is copied verbatim.
            out.append(_QUOTED_SEP_SENTINELS.get(ch, ch))
        i += 1
    return "".join(out)


def _unmask_separators(text: str) -> str:
    """Reverse :func:`_mask_quoted_separators` -- restore the boundary chars."""
    for sep, sentinel in _QUOTED_SEP_SENTINELS.items():
        text = text.replace(sentinel, sep)
    return text


# An EMPTY command substitution expands to nothing, so ``s$()sh`` runs ``ssh``
# and ``local$()host`` resolves to ``localhost`` -- the same glue-evasion the
# self-kill floor names for ``p$()kill``, but aimed at the verb gate and at the
# operand.  Collapsing these in the SOURCE text (before the gate and before
# tokenization) makes both the substring gate and every resolved operand read
# the spliced-out spelling.  Only ``$()``/backticks are matched: an empty
# ``${}`` is a bash syntax error, not an expansion, so it never splices.
_EMPTY_EXPANSION_RE = re.compile(r"\$\(\s*\)|`\s*`")

# ssh options whose VALUE is a forward/bind spec or a login name, not a
# destination this process connects to from here (lowered text collapses
# ``-L``/``-l`` and ``-W``/``-w``): ``-L``/``-R``/``-D``/``-W`` forward specs
# name listen addresses and far-side hops, ``-b`` a local bind address, and
# ``-l`` a login name.  Every value except ``-R``'s is exempt from the
# fail-closed operand check — ``ssh -L 127.0.0.1:8080:db:5432 far-host`` is
# the recommended way to reach a remote service and must stay allowed.  The
# ``-R`` destination is dialed FROM HERE, so its spec IS checked (round-27,
# ``_remote_forward_value_targets_self``); ``-J`` deliberately stays checked
# too: the jump connection originates from THIS machine.
_SSH_FORWARD_OPT_LETTERS: frozenset[str] = frozenset("lrdwb")

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


# Whether the netlink RTM_GETADDR layer has published (round-33).  The dump
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
                # Drop an IPv6 zone id (round-8): ``getaddrinfo`` can return a
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
    # address compare (round-8): the scope suffix never changes which address
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
            # this branch is the one IPv6 literals take (round-33).
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
            # publishes (round-33) -- host position only.
            return True
    except OSError:
        pass
    if host in _own_host_names():
        return True
    # A hostname that survives every textual layer may still be a DNS alias
    # for a loopback/local address; the off-loop verdict layer classifies
    # it and the decision fails closed until the verdict is published.
    # Consulted only in host position (round-17, ``dns_fallback``).
    if dns_fallback and _DNS_CANDIDATE_RE.fullmatch(host) is not None:
        if "." not in host:
            # round-21: a DOTLESS name is the hosts-file alias class the
            # round-18 finding named -- and the hosts table answers that
            # vector SAME-CALL (a file over one read chunk is pending, i.e.
            # refused, until a background parse for the current key lands).  Absent an entry, answer
            # OPEN while one async worker revalidates through DNS: the
            # fail-closed first contact broke the everyday ``ssh devbox``
            # shape (CI allow pin), and a dotless loopback alias that lives
            # only in DNS search domains is caught at the next decision
            # (documented residual).  Dotted names keep fail-closed.
            hosts_verdict = _hosts_file_verdict(host)
            if hosts_verdict is not None:
                return hosts_verdict
            return _resolved_host_verdict(host, fail_closed=False)
        return _resolved_host_verdict(host)
    return False


# round-10 static resolutions for ``_operand_targets_self``.  A ``$((…))``
# carrying ONE integer literal is normalized to the decimal spelling bash
# prints (hex and bash-octal included); anything else inside ``$((…))`` is
# left alone — an in-floor expression evaluator is itself attack surface, and
# a variable-carrying expression is runtime state (documented residual).
_ARITH_INT_LITERAL_RE = re.compile(r"\$\(\(\s*(-?(?:0[xX][0-9a-fA-F]+|[0-9]+))\s*\)\)")

# A brace group with a comma alternation or a ``..`` range is a brace
# expansion; ``{single}`` is literal in bash and stays untouched, and the
# lookbehind keeps ``${…}`` parameter expansions out.
_BRACE_GROUP_RE = re.compile(r"(?<!\$)\{([^{}]*)\}")
_BRACE_RANGE_RE = re.compile(r"\A(-?[0-9]+|[A-Za-z])\.\.(-?[0-9]+|[A-Za-z])(?:\.\.(-?[0-9]+))?\Z")
# ponytail: fan-out ceiling.  Expansion stops at 256 generated words and the
# caller DENIES (fail-closed) — a wider product in a connection operand is
# pathological, and enumerating it here would be its own DoS.
_BRACE_EXPANSION_CAP = 256


def _arith_int_literal_repl(match: "re.Match[str]") -> str:
    """The decimal spelling bash prints for one arithmetic integer literal."""
    lit = match.group(1)
    magnitude = lit.lstrip("-")
    try:
        if magnitude.lower().startswith("0x"):
            value = int(lit, 16)
        elif magnitude.startswith("0") and len(magnitude) > 1:
            # bash reads a leading zero as octal; an invalid octal digit is a
            # bash error (no command runs), so the spelling is left alone.
            value = int(lit, 8)
        else:
            value = int(lit, 10)
        # ``str`` stays INSIDE the try (round-18): hex/octal ``int`` uses a
        # power-of-two base exempt from the interpreter's digit cap, so a
        # ~3600-digit hex literal converts -- but its decimal ``str`` IS
        # capped and would raise straight through ``is_denied``, aborting
        # the evaluation.  Keeping the original spelling instead composes
        # with the target-position rule into a fail-closed deny.
        return str(value)
    except ValueError:
        return match.group(0)


def _brace_alternatives(body: str) -> "list[str] | None":
    """The words one brace-group *body* expands to, or None when literal.

    A comma body is an alternation (empty alternatives included, as in
    ``{h,}``); a ``a..b`` / ``a..b..step`` body is a numeric or single-char
    range.  Anything else — ``{single}``, an empty body — is not a brace
    expansion in bash and expands to nothing here.
    """
    range_match = _BRACE_RANGE_RE.match(body)
    if range_match is not None:
        start_s, end_s, step_s = range_match.groups()
        if start_s.isalpha() != end_s.isalpha():
            return None
        try:
            step = abs(int(step_s)) if step_s else 1
            if step == 0:
                step = 1
            if start_s.isalpha():
                start, end = ord(start_s), ord(end_s)
                render = chr
            else:
                start, end = int(start_s), int(end_s)
                render = str  # type: ignore[assignment]
        except ValueError:
            # ``int()`` refuses digit strings past the interpreter's
            # conversion cap (~4300 digits); uncaught, that crashes the
            # gate.  An endpoint or step that large cannot name a single
            # legitimate target, so signal overflow exactly like the cap
            # check below -- the caller converts it to a fail-closed deny.
            return [""] * (_BRACE_EXPANSION_CAP + 1)
        if abs(end - start) // step + 1 > _BRACE_EXPANSION_CAP:
            # Signal overflow with an over-long list the caller's cap check
            # converts to a fail-closed deny.
            return [""] * (_BRACE_EXPANSION_CAP + 1)
        direction = 1 if start <= end else -1
        return [render(v) for v in range(start, end + direction, direction * step)]
    if "," in body:
        return body.split(",")
    return None


def _brace_expansions(token: str) -> "list[str] | None":
    """Every word bash brace expansion produces for *token*; None on overflow.

    Groups are expanded left-to-right to a fixpoint (each rewrite removes one
    brace pair, so this terminates); the returned words carry no expandable
    group.  A product beyond ``_BRACE_EXPANSION_CAP`` returns None and the
    caller fails closed.
    """
    words = [token]
    changed = True
    while changed:
        changed = False
        next_words: list[str] = []
        for word in words:
            expanded = False
            for group in _BRACE_GROUP_RE.finditer(word):
                alternatives = _brace_alternatives(group.group(1))
                if alternatives is None:
                    continue
                head, tail = word[: group.start()], word[group.end() :]
                next_words.extend(head + alt + tail for alt in alternatives)
                expanded = True
                changed = True
                break
            if not expanded:
                next_words.append(word)
            if len(next_words) > _BRACE_EXPANSION_CAP:
                return None
        words = next_words
    return words


def _operand_targets_self(operand: str, *, host_position: bool = True) -> bool:
    """True if *operand* names THIS machine as a connection target.

    Resolves the host part the way the ssh family reads an operand: an
    optional ``ssh://``/``scp://``/``sftp://``/``rsync://`` scheme (authority
    isolated before any path), an optional ``user@`` prefix stripped from the
    HOST part only (an ``@`` in a remote PATH is not userinfo), a bracketed
    IPv6 literal, a bare IPv6/numeric IP literal, and a ``host:path`` colon
    form.  A bare word equal to a self-host name also counts: for ssh/sftp it
    IS the positional target, and for scp/rsync a local file named exactly
    ``localhost`` is not worth carving an allowance for (over-blocking is the
    safer direction, and the catalog pattern must stay a SUBSET of this
    predicate — see ``test_retained_pattern_is_a_subset_of_its_predicate``).

    Every statically-derivable expansion/gluing form is resolved over the
    WHOLE operand and the resolved spelling re-checked (round-10 invariant):
    parameter defaults (``local${U:-host}`` -> ``localhost``), brace
    expansion (``local{h,}ost``), and single-integer arithmetic literals
    (``$((0x7f)).0.0.1``).  Each resolution only WIDENS the deny.  Forms
    whose produced text depends on runtime state — a bare ``$VAR`` value,
    command substitution with output (``$(get-host)``), arithmetic beyond
    one literal (``$((i+1))``) — are the documented run-time residual class:
    emulating them needs an in-floor evaluator that is itself attack
    surface, and their statically-visible parts are already checked (the
    embedded defaults here, the ``$(hostname`` hints, the empty-substitution
    collapse in ``_is_ssh_to_self``).
    """
    t = operand.strip().strip("\"'")
    if not t or t.startswith("-"):
        return False
    # A glued shell operator (``ssh localhost;true`` hands the operand
    # ``localhost;true``) is never part of a hostname, so consult the
    # operator-cut spelling too -- the same ``(token, _cut_at_operator(token))``
    # idiom the git-push floor uses.  This only WIDENS the deny: the cut token
    # has no operator left, so the recursive call cuts nothing and does not
    # recurse again, and a bare ``$VAR`` (no operator to cut) is unaffected.
    cut = _cut_at_operator(t)
    if cut != t and _operand_targets_self(cut, host_position=host_position):
        return True
    for hint in _HOSTNAME_SUBSTITUTION_HINTS:
        if hint in t:
            return True
    for var in _HOSTNAME_VARIABLE_FORMS:
        if t == var or t.startswith(var + ".") or t.startswith(var + ":"):
            return True
    # An expansion's embedded default is the destination when the variable is
    # unset — check every ``${…:-word}``-style word (recursion terminates:
    # the word is strictly shorter than the operand).  Kept alongside the
    # whole-operand resolution below because it is WIDER on mangled glue
    # (``foo${X:-localhost}bar``): the embedded word alone is self even when
    # the resolved whole is not.
    for match in _EXPANSION_DEFAULT_RE.finditer(t):
        if _operand_targets_self(match.group(1), host_position=host_position):
            return True
    # round-10: bash substitutes a parameter default INTO the surrounding
    # word (``local${KC_UNSET:-host}`` -> ``localhost``), so the whole
    # operand is resolved and re-checked — checking each default word in
    # isolation misses the reconstruction.  ``_resolve_param_defaults``
    # substitutes every ``${VAR<op>literal}`` with its literal to a fixpoint
    # (nesting and colon-less operators included); the recursive call sees a
    # string with no such form left, so it does not recurse here again.
    resolved = _resolve_param_defaults(t)
    if resolved != t and _operand_targets_self(resolved, host_position=host_position):
        return True
    # round-10: a single integer literal inside arithmetic expansion is
    # printed decimally by bash (``$((0x7f))`` -> ``127``), gluing into the
    # surrounding word.  Only the literal spellings are normalized — an
    # expression or a variable stays unresolved (run-time residual, see the
    # docstring) — and the substituted string carries no ``$((…))`` literal,
    # so the recursion is a single level.
    normalized = _ARITH_INT_LITERAL_RE.sub(_arith_int_literal_repl, t)
    if normalized != t and _operand_targets_self(normalized, host_position=host_position):
        return True
    # round-17: an arithmetic expansion that SURVIVED normalization is an
    # expression (``$((0+1))``) whose produced text only the shell knows.
    # In a connection-target position that unknown glues into the host
    # (``127.0.0.$((0+1))`` -> ``127.0.0.1``), so fail closed.  For the
    # ``host:path`` colon form only the PRE-COLON host part can name this
    # machine (round-25): arithmetic after the colon glues into a remote
    # filename (``far:/backups/part$((i)).tar``), so it stays allowed.
    if "$((" in normalized and (
        host_position or (":" in normalized and "$((" in normalized.split(":", 1)[0])
    ):
        return True
    # round-10: brace expansion splices each alternative into the
    # surrounding word BEFORE every other expansion (``local{h,}ost`` ->
    # ``localhost``), so each choice is re-checked.  ``None`` means the
    # fan-out exceeded the cap: fail closed — no legitimate single-target
    # connection operand needs a >256-way product, and over-blocking is this
    # floor's safe direction.
    choices = _brace_expansions(t)
    if choices is None:
        return True
    for choice in choices:
        if choice != t and _operand_targets_self(choice, host_position=host_position):
            return True
    scheme_seen = False
    for scheme in ("ssh://", "scp://", "sftp://", "rsync://"):
        if t.startswith(scheme):
            # Isolate the URI authority before any path, so an ``@`` or ``:``
            # in the remote path cannot masquerade as userinfo or a port.
            t = t[len(scheme) :].split("/", 1)[0]
            scheme_seen = True
            break
    # A bare loopback/IP literal is a host even when it CONTAINS colons
    # (``::1``, ``::ffff:127.0.0.1``): check the whole token before
    # colon-splitting, which would read an IPv6 literal as an empty host.
    # The DNS layer applies to the WHOLE token only when the token sits in
    # host position (or a scheme marked it as an authority) -- a plain word
    # here may be a local filename (round-17).
    if _host_is_self(t, dns_fallback=host_position or scheme_seen):
        return True
    if t.startswith("[") or "@[" in t:
        # Bracketed IPv6, optionally behind userinfo: user@[::1]:path
        host = t.split("[", 1)[1].partition("]")[0]
    else:
        # host[:path] first, THEN userinfo — in that order, so an ``@`` in
        # the path part (``localhost:/tmp/a@b``) is never taken as userinfo.
        pre_colon = t.partition(":")[0]
        if "@" in pre_colon:
            # An ``@`` BEFORE the first colon is real userinfo.  Check the
            # whole remainder after it as a bare host first: ``user@::1``
            # names ``::1``, which the plain colon split below would misread
            # as an empty host.  Userinfo marks the remainder as a host.
            if _host_is_self(t[len(pre_colon.rsplit("@", 1)[0]) + 1 :]):
                return True
        host = pre_colon
        if "@" in host:
            host = host.rsplit("@", 1)[1]
    # An isolated host part (a scheme authority, a bracketed literal, a
    # ``host:path`` prefix, a userinfo remainder) IS a host wherever the
    # operand sits, so the DNS layer applies; an operand isolation did not
    # shorten is a bare word -- a host only in host position (round-17).
    if "@" in t and (scheme_seen or "/" not in t):
        # Userinfo may CONTAIN a colon (``user:pass@host``): the colon-first
        # split above then reads the host as ``user``.  OpenSSH resolves the
        # destination AFTER the LAST ``@`` -- in a URI authority (its path is
        # already split off) and in the plain ``[user@]host`` form (which has
        # no path).  A token with a ``/`` outside a scheme keeps the round-17
        # order: its ``@`` is path data (``far:/backup/a@b``).
        tail = t.rsplit("@", 1)[1]
        if _host_is_self(tail, dns_fallback=host_position or scheme_seen):
            return True
        tail_host = tail.partition(":")[0]
        if tail_host != tail and _host_is_self(
            tail_host, dns_fallback=host_position or scheme_seen
        ):
            return True
    return _host_is_self(host, dns_fallback=host_position or scheme_seen or host != t)


def _ssh_family_verb(token: str) -> "str | None":
    """The ssh-family verb *token* invokes, or None.

    Path-qualified (``/usr/bin/ssh``) and Windows (``ssh.exe``) spellings
    resolve to the bare verb.  A glob basename that CAN expand to one
    (``s?h``, see ``_GLOB_CHARS``) resolves to the verb it can name.
    """
    base = _program_basename(_strip_redirect(token.strip("\"'")))
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    if base in _SSH_FAMILY_VERBS:
        return base
    if _GLOB_CHARS.intersection(base):
        for verb in _SSH_FAMILY_VERBS:
            if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base):
                return verb
    return None


# ssh_config keywords that SET the destination.  ``-o hostname=…`` rewrites
# the host the positional operand merely aliases, and ``-o proxyjump=…``
# opens a connection of its own — so a self value in either is a self
# connection regardless of the operand.  Generic ``opt=value`` spellings
# (rsync ``--exclude=localhost``) name data, not a destination.
_SSH_ROUTING_OPTION_KEYS: tuple[str, ...] = ("hostname", "proxyjump")

# ssh_config keywords whose VALUE is a command line ssh runs LOCALLY, not a
# host.  ``-o proxycommand="ssh localhost x"`` and ``-o localcommand=…`` both
# exec their value on THIS machine, so a value that itself opens a channel to
# this host is a self connection; ``-o knownhostscommand=…`` likewise runs its
# value locally (to print known_hosts lines), so it is scanned the same way.
# scp/sftp forward ``-o`` to ssh, so the same hole reaches them.  The value is
# checked by recursing the whole floor on it.
_SSH_COMMAND_OPTION_KEYS: tuple[str, ...] = ("proxycommand", "localcommand", "knownhostscommand")

# OpenSSH's valueless short flags, case-folded (the floor lowercases the whole
# command line once at entry).  getopt lets these BUNDLE in front of an option
# letter -- ``-voHostname=x`` is ``-v`` + ``-o Hostname=x`` -- so a glued
# ``o``/``j`` is still the option letter after this prefix.  Value-taking
# letters (``-l``, ``-p``, ...) are NOT here: they consume the rest of the
# token as their argument.  A folded twin whose uppercase takes a value
# (``-Q``, ``-M``) can only ADD a denial of a spelling ssh itself rejects,
# never an allow.
_SSH_VALUELESS_SHORT_FLAGS = "1246acfgkmnqstvxy"

# Case-folded option letters that CONSUME the next token as their value, PER
# VERB (round-19).  The walk itself stays table-free and fail-closed -- these
# sets only decide whether the token AFTER an option keeps HOST POSITION and
# whether it consumes the ssh/sftp positional slot.  The source is case-folded,
# so where upper and lower case disagree on valueness the letter is IN the
# set, i.e. treated as VALUE-TAKING: a value token mistaken for the positional
# CONSUMES the slot and the real host after it is never checked at all
# (``sftp -R 64 localhost``), while a host token mistaken for a value leaves
# the slot pending, so every later token still gets the full checks --
# over-checking is this floor's safe direction.  (This reverses the round-18
# collision rule, whose "the DNS layer still covers a host after it" bet did
# not survive the consumption path.)  scp/rsync have no positional slot in
# this walk, so they need no entry.
_VERB_VALUE_TAKING_OPT_LETTERS: "dict[str, frozenset[str]]" = {
    "ssh": frozenset("bcdefijlmopqrsw"),
    "sftp": frozenset("bcdfijloprsx"),
}

# Folded letters whose two cases DISAGREE on valueness (round-31): the letter
# stays in the value set above (so the slot is not consumed by mistake), but
# the swallowed token ALSO keeps full host-position checks — both candidate
# destinations are over-checked, this floor's safe direction.  ssh: c/C, f/F,
# m/M, q/Q, s/S.  sftp: c/C, f/F, p/P, r/R.
_VERB_FOLD_AMBIGUOUS_OPT_LETTERS: "dict[str, frozenset[str]]" = {
    "ssh": frozenset("cfmqs"),
    "sftp": frozenset("cfpr"),
}


def _proxyjump_value_targets_self(value: str) -> bool:
    """True if any hop in a ProxyJump chain names this host.

    A ProxyJump value is a COMMA-SEPARATED chain (``localhost,far``); ssh dials
    the first hop directly from HERE, so a self-host anywhere in the chain is a
    self dial.  Each hop is checked (fail-closed): over-blocking a later hop is
    the safe direction, and it keeps this simple.  The WHOLE value is checked
    too (round-10): a brace alternation's own comma (``local{h,}ost``) is torn
    by the hop split, and only the unsplit spelling resolves back to the word
    bash glues together.
    """
    if _operand_targets_self(value):
        return True
    return any(_operand_targets_self(hop) for hop in value.split(","))


def _remote_forward_value_targets_self(value: str) -> bool:
    """True if a remote-forward (``-R`` / RemoteForward) spec dials THIS host.

    ``-R`` is the one forward whose DESTINATION is dialed FROM HERE: the far
    sshd listens, and each accepted connection is handed back for THIS client
    to connect to ``host:hostport`` locally -- so ``-R 2222:localhost:22``
    gives remote users the local unsandboxed sshd.  Branch table for
    ``_SSH_FORWARD_OPT_LETTERS`` (the others stay exempt): ``-L``/``-D``
    destinations are dialed from the FAR side (its loopback is the far
    machine), ``-w`` names tun devices, ``-b`` a local source bind -- none
    dials a destination from here.  Spec shapes, colon-split OUTSIDE
    brackets: ``[bind:]port:host:hostport`` (3-4 fields) checks the host
    field as a connection target (DNS-classified like any host value);
    ``[bind:]port`` (1-2 fields) is reverse-SOCKS, where the REMOTE chooses
    every local dial destination -- loopback included -- so it fails closed.
    The config spelling ``RemoteForward listen dest`` is whitespace-joined
    onto ``:`` first, which reduces it to the same shape.
    """
    spec = ":".join(value.strip().strip("\"'").split())
    fields: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(spec):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            fields.append(spec[start:i])
            start = i + 1
    fields.append(spec[start:])
    if len(fields) <= 2:
        return True
    dest = fields[-2].strip().strip("[]")
    return bool(dest) and _operand_targets_self(dest)


def _command_value_names_self_endpoint(value: str) -> bool:
    """True if a ProxyCommand-style value names a LITERAL self endpoint.

    The value is a local command line whose network endpoint decides where
    the outer session lands (round-26): ``nc 127.0.0.1 22`` relays the whole
    session to the local sshd with no ssh-family verb for the recursion to
    see.  Each whitespace word is checked as a connection operand, and its
    bracketed groups and separator-split fragments are checked LITERALLY --
    fragments never resolve DNS (``dns_fallback=False``), so option words
    like ``-connect`` or socat's ``TCP`` address prefix cannot fail closed,
    and a resolvable alias inside a transport (``nc myalias 22``) stays the
    documented alias residual the interim tier does not close.
    """
    for word in value.split():
        stripped = word.strip("\"'")
        if not stripped or stripped.startswith("-"):
            continue
        if _operand_targets_self(stripped, host_position=False):
            return True
        for group in re.findall(r"\[([^\]]*)\]", stripped):
            if _host_is_self(group, dns_fallback=False):
                return True
        without_brackets = re.sub(r"\[[^\]]*\]", "", stripped)
        for frag in re.split(r"[:=,]", without_brackets):
            if frag and _host_is_self(frag, dns_fallback=False):
                return True
    return False


def _routing_option_key_value_targets_self(key: str, value: str) -> bool:
    """True if ssh option *key* routes *value* to this host.

    ``-o`` may be GLUED to the keyword (``-ohostname=…``), so a leading ``o`` in
    front of a longer name is the option letter, not part of the keyword.
    ProxyJump is a comma-chain; ProxyCommand/LocalCommand values are local
    command lines that recurse the floor (the value is strictly shorter than the
    token, so the recursion terminates); Hostname is a single host.
    """
    if key.startswith("o") and len(key) > 1:
        key = key[1:]
    if key == "proxyjump":
        return _proxyjump_value_targets_self(value)
    if key == "remoteforward":
        return _remote_forward_value_targets_self(value)
    if key in _SSH_ROUTING_OPTION_KEYS:  # "hostname" (proxyjump handled above)
        return _operand_targets_self(value)
    if key in _SSH_COMMAND_OPTION_KEYS:
        return _is_ssh_to_self(value) or _command_value_names_self_endpoint(value)
    return False


def _routing_option_value_targets_self(token: str, *, value_slot: bool) -> bool:
    """True if *token* carries a routing option value naming this host.

    Covers the ``key=value`` spelling, the config-style WHITESPACE spelling
    OpenSSH equally accepts (``-o "Hostname localhost"`` resolves to the same
    routing -- verified against ``ssh -G``), and the attached jump-host form
    (``-Jlocalhost``, ``-Jlocalhost,far``, which opens a connection of its own).
    The whitespace form is only read in a VALUE SLOT (an option's argument, or
    attached to the option itself): in plain operand position a two-word token
    is remote command data (``ssh far 'hostname localhost'``), not routing.
    """
    head, eq, value = token.partition("=")
    if eq:
        key = head.lstrip("-")
        if token.startswith("-"):
            # getopt bundles valueless flags in FRONT of the option letter:
            # ``-voHostname=x`` is ``-v`` + ``-o Hostname=x``.
            key = key.lstrip(_SSH_VALUELESS_SHORT_FLAGS)
        if _routing_option_key_value_targets_self(key, value):
            return True
    elif value_slot:
        parts = token.strip().split(None, 1)
        if len(parts) == 2 and _routing_option_key_value_targets_self(
            parts[0].lstrip("-"), parts[1]
        ):
            return True
    if token.startswith("-"):
        bare = token.lstrip("-").lstrip(_SSH_VALUELESS_SHORT_FLAGS)
        if len(bare) > 1 and bare[0] == "j" and _proxyjump_value_targets_self(bare[1:]):
            return True
    return False


# round-17: same-line literal shell assignments.  bash substitutes ``$a`` /
# ``${a}`` from an assignment earlier on the SAME line before exec, so
# ``a=s; ${a}sh localhost`` runs ``ssh``.  Only literal, separator-free values
# are modeled (optionally quoted); a value carrying ``$``, a backtick, spaces,
# or a command separator stays unresolved -- the documented run-time residual.
# Resolution is position-aware: a reference takes the LATEST assignment whose
# text precedes it, exactly like bash, so ``${a}sh; a=s`` does not resolve.
_LINE_ASSIGNMENT_RE = re.compile(
    r"(?:\A|[;&|`(\s])([a-z_][a-z0-9_]*)=([\"']?)([a-z0-9._:@/-]*)\2(?=\Z|[)\s;&|])"
)
_VAR_REFERENCE_RE = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}|\$([a-z_][a-z0-9_]*)")


def _resolve_line_assignments(text: str) -> str:
    """Substitute ``$var``/``${var}`` from literal same-line assignments."""
    assignments: "list[tuple[int, str, str]]" = []
    for m in _LINE_ASSIGNMENT_RE.finditer(text):
        assignments.append((m.end(), m.group(1), m.group(3)))
    if not assignments:
        return text

    def _substitute(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        value: "str | None" = None
        for end, assigned, assigned_value in assignments:
            if end <= match.start() and assigned == name:
                value = assigned_value
        return match.group(0) if value is None else value

    return _VAR_REFERENCE_RE.sub(_substitute, text)


# round-17: one-level function-call argument binding.  A same-line function
# definition whose body dials a positional parameter (``f(){ ssh "$1" id; };
# f localhost``) is an ordinary evasion: the literal call argument IS the
# destination.  Each (definition, later call) pair is bound and the bound body
# recursed through the floor.  One level only -- a function calling another
# function is the documented residual -- and both fan-outs are capped.
_FUNCTION_DEF_RE = re.compile(
    # ``name() { … }``, ``function name() { … }``, and bash's parenthesis-free
    # ``function name { … }`` keyword form (round-18) all define the same
    # function.  The body group matches BALANCED braces one level deep
    # (round-24): a non-greedy ``.*?`` stopped at the ``}`` inside ``${1}``,
    # truncating the body and losing the dial.  The two alternatives are
    # disjoint on their first character, so the group is backtracking-safe;
    # a body nesting braces two deep stays unmatched -- same one-level scope
    # as the binder itself (a function calling a function is the documented
    # residual).
    r"(?:function\s+([a-z_][a-z0-9_]*)\s*(?:\(\s*\))?|([a-z_][a-z0-9_]*)\s*\(\s*\))\s*"
    r"\{((?:[^{}]|\{[^{}]*\})*)\}",
    re.DOTALL,
)
_FUNCTION_BIND_CAP = 8

# round-24: a call still INVOKES the function behind leading assignment words
# (``x=1 f localhost``) and the invocation keywords that run their operand as
# a command (``time f``, ``! f``, ``if f; then``).  ``command``/``exec``/
# ``nohup`` are NOT here: ``command`` bypasses function lookup and the other
# two exec real binaries, so none of them reaches the function body.
_CALL_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*\Z")
_CALL_WRAPPER_WORDS: frozenset[str] = frozenset(
    {"time", "!", "if", "elif", "while", "until", "then", "else", "do", "{", "("}
)


def _function_call_binds_self(text: str) -> bool:
    """True if a bound (def, call) pair opens an ssh channel to this host."""
    # The caller hands over masked text: a quoted ``{`` rides as a sentinel
    # byte while its closing ``}`` stays literal, which breaks the DEF
    # regex's balanced-brace body capture on ``"${10}"``.  Restore ONLY the
    # parameter-expansion opener -- ``${`` is an expansion, never a command
    # boundary -- so quoted braced positionals capture and bind (round-33).
    text = text.replace("$\x06", "${")
    for def_count, m in enumerate(_FUNCTION_DEF_RE.finditer(text)):
        if def_count >= _FUNCTION_BIND_CAP:
            # Past the cap the binder cannot resolve what a definition does,
            # so it fails CLOSED: a 9th definition is deniable padding, not a
            # command shape the floor can clear (round-30).
            return True
        name, body = m.group(1) or m.group(2), m.group(3)
        # The caller hands over masked text (quoted separators AND quoted
        # braces ride as mask bytes), so a quoted ``"${10}"`` reads as
        # ``"$\x0610}"`` here.  Unmask the captured body before binding or
        # the braced replacements never match (round-33).
        body = _unmask_separators(body)
        if "$" not in body:
            continue  # nothing to bind
        call_count = 0
        for segment in re.split(r"[;&|\n]", text[m.end() :]):
            words = segment.split()
            # round-24: skip leading assignment words and invocation keywords
            # -- both still invoke the function.  Any OTHER leading word means
            # the name is an argument (``echo f localhost``), not a call.
            idx = 0
            while idx < len(words) and (
                words[idx] in _CALL_WRAPPER_WORDS
                or _CALL_ASSIGNMENT_RE.fullmatch(words[idx]) is not None
            ):
                idx += 1
            if idx >= len(words) or words[idx] != name:
                continue
            call_count += 1
            if call_count > _FUNCTION_BIND_CAP:
                # Same fail-closed rule as the definition cap: the 9th call
                # of one name is unresolvable padding, so deny (round-30).
                return True
            args = [w.strip("\"'") for w in words[idx + 1 :]]
            bound = body
            joined = " ".join(args)
            for star in ('"$@"', "$@", '"$*"', "$*"):
                bound = bound.replace(star, joined)
            # Braced ``${N}`` has no single-digit ceiling in bash, so it
            # binds for EVERY supplied argument; unbraced ``$N`` reads one
            # digit (``$10`` is ``${1}0``), so it stays capped at nine
            # (round-33).
            for i, arg in enumerate(args, start=1):
                bound = bound.replace('"${%d}"' % i, arg)
                bound = bound.replace("${%d}" % i, arg)
                if i <= 9:
                    bound = bound.replace('"$%d"' % i, arg)
                    bound = bound.replace("$%d" % i, arg)
            if bound != body and _is_ssh_to_self(bound):
                return True
    return False


def _is_ssh_to_self(text_lower: str) -> bool:
    """True if *text_lower* opens an ssh/scp/sftp/rsync channel to THIS host.

    Structural for the same reason the other floors are: the target hides
    behind options, redirects, quoting, and prefixes.  The argv walk is
    FAIL-CLOSED about option grammar: EVERY token that could be an operand —
    including one sitting where an option's value would go — is checked
    against the self-host set.  The per-verb value-taking table
    (``_VERB_VALUE_TAKING_OPT_LETTERS``) never exempts a token from that
    check; it only decides whether the token after an option keeps HOST
    POSITION (the DNS-layer gate) and whether it consumes the ssh/sftp
    positional slot, with collisions resolved toward value-taking — the
    over-checking direction (a valueless-flag table exempting the target
    itself is the shape that mis-consumed ``scp -r localhost:…``).  A
    non-forwarding option value (a port, a cipher) never names this host, so
    the check costs nothing; a self-host hiding in value position is denied.
    The one carved-out value class is the forward/bind specs
    (``_SSH_FORWARD_OPT_LETTERS``): ``ssh -L 127.0.0.1:8080:db:5432
    far-host`` names a local LISTEN address, not a destination, and must
    stay allowed.
    ssh/sftp consume their FIRST unshadowed operand as the positional host
    (later operands are the remote command, where a word like "localhost" is
    data, not a destination); scp/rsync accept a target in any operand
    position.  ``opt=value`` tokens (``-ohostname=localhost``,
    ``-o proxyjump=localhost``) have their value checked too.  Redirections
    are stepped over the way bash removes them from argv.  Same argv-boundary
    discipline as the self-kill floor: the walk is substitution-aware and
    stops at this command's own separator.

    Two evasions are neutralized before the walk.  Empty command substitutions
    (``s$()sh``, ``local$()host``) expand to nothing, so they are collapsed out
    of the source text first -- otherwise they hide the verb from the substring
    gate and the self-host from an operand.  Quote and backslash splices
    (``ss""h``, ``s\\sh``) are rejoined by shlex in the tokens but still hide the
    verb from the raw-substring gate, so the gate probes a splice-stripped copy.
    Two value classes are command lines this host runs LOCALLY rather than
    hosts, and both recurse this floor on the value: ssh's
    ``-o proxycommand=``/``-o localcommand=`` (in
    ``_routing_option_value_targets_self``) and rsync's ``-e``/``--rsh``.
    rsync also honors a leading ``RSYNC_RSH=`` environment assignment as the
    same selector -- handled in the walk below; the wider remote-shell env-var
    family (``GIT_SSH_COMMAND`` and the like) is a documented residual, as are
    the shell-builtin export spellings (``declare -x``/``typeset -x``), which
    are not leading-assignment syntax and so are not modeled by the walk.
    """
    # Collapse empty expansions in the SOURCE so the gate and tokenization both
    # read the spliced-out spelling (``s$()sh localhost`` -> ``ssh localhost``).
    text_lower = _EMPTY_EXPANSION_RE.sub("", text_lower)

    # A flat command substitution whose output is statically decidable
    # (``$(printf localhost)``, backtick ``echo localhost``) IS its output by
    # the time the kernel sees the command line, so splice that output into
    # the source before the probe and the walk -- the resolved self-host is
    # then an ordinary operand, glued spellings (``local$(printf host)``)
    # included.  ``_static_substitution_output`` resolves only echo/printf
    # literals; every other body keeps its original text (the ``$(hostname``
    # hints below read it) and stays the documented run-time residual.  A
    # single-quoted spelling resolves too -- an over-approximation toward
    # deny, accepted for a deny floor.
    def _splice_static_substitution(match: "re.Match[str]") -> str:
        body = match.group(1) if match.group(1) is not None else match.group(2)
        out = _argv_floor._static_substitution_output(body)
        return match.group(0) if out == "\x00" else out

    text_lower = _FLAT_SUBSTITUTION_RE.sub(_splice_static_substitution, text_lower)
    # round-17: substitute ``$a``/``${a}`` from literal same-line assignments
    # (``a=s; ${a}sh localhost`` -> ``ssh localhost``) so neither the verb nor
    # the operand can be spliced from statically-known text.  Values are
    # separator-free by construction, so the substitution cannot fabricate a
    # command boundary the masking below would misread.
    text_lower = _resolve_line_assignments(text_lower)
    # Mask every ``;``/``|`` that is QUOTED or backslash-escaped in the source to
    # a sentinel BEFORE tokenization, so a quoted separator surviving into a
    # shlex-dequoted token (``scp 'a;b' localhost:/x``) is not read as a
    # command boundary by ``_ends_argv`` -- which would end the walk before the
    # real target and let the connection through.  It is restored only at the
    # faithful operand/routing checks below; recursion sites receive the masked
    # text unchanged (the mask is idempotent -- their quotes are already gone).
    text_lower = _mask_quoted_separators(text_lower)
    # Quote/backslash splices survive into the tokens (shlex rejoins them) but
    # defeat this raw-substring gate, so probe a copy with them stripped out.
    # round-20 (Opus): bash decodes ANSI-C quoting ($'\x73\x73\x68' -> ssh)
    # before exec, and the operand walk's tokenizer resolves it too -- so the
    # gate guarding that walk probes the DECODED text, or a verb spelling the
    # walk would deny slips past the gate the walk never gets to correct.
    probe = _decode_shell_quoted_literals(text_lower)
    probe = probe.replace('"', "").replace("'", "").replace("\\", "")
    # bash substitutes ``${VAR:-word}`` defaults before exec, so the verb can
    # be spliced from statically-known text (``s${U:-s}h`` -> ``ssh``).
    # Resolve the same forms the operand walk resolves; a bare ``$VAR`` whose
    # value exists only at run time stays the documented fail-open residual.
    probe = _resolve_param_defaults(probe)
    if not any(verb in probe for verb in _SSH_FAMILY_VERBS) and not _glob_can_name_ssh_verb(probe):
        return False
    # round-17: bind one level of function-call arguments (``f(){ ssh "$1"
    # id; }; f localhost``) and recurse on the bound body -- the literal call
    # argument is the destination the walk below cannot see through ``$1``.
    if _function_call_binds_self(text_lower):
        return True
    # round-8: a self-targeting ``RSYNC_RSH`` set in one frame is inherited by
    # LATER frames (an exported value, or a command-scoped prefix on a command
    # that spawns a nested ``sh -c`` payload), where the per-frame pending/export
    # walk below -- which re-initialises inside each frame -- cannot see it.
    # This function-scope latch carries that fact forward; it is set at the END
    # of a frame (so the round-7 same-frame ``;``-clear semantics are unchanged
    # for the frame that owns the assignment) and never cleared.
    # Over-approximation, accepted for a deny floor: once set, the latch also
    # covers a textually-later SIBLING nested payload; a contrived same-frame
    # ``RSYNC_RSH=self cmd; rsync other:/ .`` stays allowed by the round-7 clear
    # rule because the latch is consulted only in LATER frames.
    rsync_rsh_carried_self = False
    # round-20 (GPT): destination paths a same-line cp/mv/ln/install gave to
    # an ssh-family binary, bound to the verb they now carry.  Shared across
    # frames (a copy staged in a wrapper payload reaches its siblings); a
    # copy staged in an EARLIER command line is the documented residual.
    bound_program_verbs: "dict[str, str]" = {}
    for tokens in _argv_floor._self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        # A leading ``RSYNC_RSH=<cmd>`` environment assignment selects rsync's
        # remote shell exactly like ``-e``/``--rsh``, but rides BEFORE the verb
        # so the operand walk below never sees it.  When rsync runs live, that
        # value is a local command line that recurses the floor.  Bash applies a
        # leading assignment to the WHOLE simple command that follows -- the
        # command word and every child it execs, wrappers of any shape
        # (``env``/``command``/``timeout`` …) included -- so an ordinary word
        # does NOT drop the pending value; only a command separator ends that
        # simple command and clears it (tracked substitution-aware, so a ``;``
        # inside ``$( … )`` does not).  ``export`` makes the value persist for
        # the rest of the line, so an exported value is remembered separately
        # and survives separators.
        rsync_rsh_pending: dict[str, str] = {}
        rsync_rsh_exported: dict[str, str] = {}
        # Names declared for export BEFORE any value exists (the POSIX
        # ``export VAR; VAR=value`` order, round-31): a later assignment to a
        # marked name is exported the moment it lands.  Never cleared by
        # separators.
        rsync_rsh_export_marked: set[str] = set()
        # The last plain assignment seen in this frame, PER NAME (round-30:
        # the two rsync env vars are independent — one shared slot let a later
        # far assignment overwrite an earlier self value), wherever it
        # appeared: a shell variable persists for the rest of the line even
        # after the simple command it prefixed ends, so a later bare
        # ``export RSYNC_RSH`` (the POSIX ``VAR=value; export VAR`` two-step)
        # promotes THAT name's value into the environment.  Never cleared by
        # separators.
        rsync_rsh_assigned: dict[str, str] = {}
        prev_stripped_tok: "str | None" = None
        cmd_start = True  # the next token sits in program position
        outer_depth = 0
        xargs_prefix_index: "int | None" = None  # a bare ``xargs`` in this simple command
        # Once per FRAME, not once per verb token: see ``_is_credential_mint``.
        disqualified: "bool | None" = None
        for i, token in enumerate(tokens):
            verb = _ssh_family_verb(token)
            if verb is None and bound_program_verbs:
                stripped_prog = token.strip("\"'")
                verb = bound_program_verbs.get(stripped_prog) or bound_program_verbs.get(
                    _program_basename(stripped_prog)
                )
            if verb is None:
                stripped_tok = token.strip("\"'")
                assign = _RSYNC_RSH_ASSIGN_RE.match(stripped_tok)
                if assign is not None:
                    env_name = assign.group(1)
                    value = assign.group(2).strip("\"'")
                    rsync_rsh_pending[env_name] = value
                    rsync_rsh_assigned[env_name] = value
                    # A preceding ``export`` makes the assignment persist into
                    # every later segment's environment for real, so remember it
                    # where no separator clears it.
                    if prev_stripped_tok == "export" or env_name in rsync_rsh_export_marked:
                        rsync_rsh_exported[env_name] = value
                else:
                    exported_name = stripped_tok.rstrip(";&")
                    if prev_stripped_tok == "export" and exported_name in (
                        "rsync_rsh",
                        "rsync_connect_prog",
                    ):
                        # Bare ``export RSYNC_RSH`` (or ``RSYNC_CONNECT_PROG``)
                        # after an earlier plain assignment: THAT name's stored
                        # value enters the environment.  The walk reads lowered
                        # text, and a glued separator (``export RSYNC_RSH;``)
                        # rides on the name token.  Declared BEFORE any value
                        # (export-first), the name is marked so the later
                        # assignment exports itself (round-31).
                        rsync_rsh_export_marked.add(exported_name)
                        if exported_name in rsync_rsh_assigned:
                            rsync_rsh_exported[exported_name] = rsync_rsh_assigned[exported_name]
                    outer_depth += _substitution_depth_delta(token)
                    if outer_depth <= 0 and _ends_argv(token):
                        # A command separator ends the simple command the
                        # leading assignment prefixed; pending does not cross it.
                        rsync_rsh_pending.clear()
                        xargs_prefix_index = None
                    outer_depth = max(outer_depth, 0)
                # round-35 (GPT): xargs turns its stdin into argv for the
                # program it launches -- remember a bare ``xargs`` in this
                # simple command (any position: wrappers keep it off program
                # position), so a later verb + here-string is judged as the
                # command xargs actually runs.
                if _program_basename(stripped_tok) == "xargs":
                    xargs_prefix_index = i
                # round-20 (GPT): a program-position cp/mv/ln/install whose
                # source operand is an ssh-family binary binds its DESTINATION
                # operand as that verb for the rest of the line, so
                # ``cp /usr/bin/ssh /tmp/x && /tmp/x localhost`` cannot shed
                # the floor by shedding the basename.  Statically decidable,
                # exactly like the round-17 assignment/function binders.
                if cmd_start and _program_basename(stripped_tok) in _BINARY_COPY_VERBS:
                    operands: "list[str]" = []
                    for peek in tokens[i + 1 :]:
                        peeked = peek.strip("\"'")
                        if _ends_argv(peek):
                            # A separator can ride glued on the last operand
                            # (``/tmp/y;``) -- keep the de-glued word, then
                            # stop at the boundary.
                            deglued = peeked.rstrip(";&|\n")
                            if deglued and not deglued.startswith("-"):
                                operands.append(deglued)
                            break
                        if peeked.startswith("-"):
                            continue
                        operands.append(peeked)
                    if len(operands) >= 2:
                        for source in operands[:-1]:
                            bound_verb = _ssh_family_verb(source)
                            if bound_verb is not None:
                                dest = operands[-1]
                                bound_program_verbs[dest] = bound_verb
                                bound_program_verbs[_program_basename(dest)] = bound_verb
                                break
                cmd_start = _ends_argv(token) or (
                    cmd_start and _ENV_ASSIGN_PREFIX_RE.match(stripped_tok) is not None
                )
                prev_stripped_tok = stripped_tok
                continue
            # A verb starts a fresh simple command, so the ``export`` adjacency
            # run ends here.
            prev_stripped_tok = None
            # ``echo ssh localhost`` prints two words; it connects to nothing.
            if disqualified is None:
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(i, token, programs, tokens, command_disqualified=disqualified):
                continue
            # round-35 (GPT): launched through xargs, the verb's REAL argv
            # arrives on stdin -- and a here-string puts that stdin in the
            # source text, so ``xargs ssh <<< localhost`` runs
            # ``ssh localhost``.  Rebuild that command and judge it like a
            # directly-typed one; the rebuild drops the xargs word and the
            # here-string pair, so the recursion walks strictly fewer tokens
            # and terminates.
            if xargs_prefix_index is not None and xargs_prefix_index < i:
                rebuilt = _xargs_here_string_rebuild(verb, tokens, xargs_prefix_index, i)
                if rebuilt is not None and _is_ssh_to_self(rebuilt):
                    return True
            # rsync execs a pending ``RSYNC_RSH`` / ``RSYNC_CONNECT_PROG``
            # value as its remote shell (or daemon proxy) FROM HERE, so a
            # value that opens a channel to this host is a self dial.
            # An unexported value applies only to this simple command; an
            # exported one persists, so fall back to it when nothing is pending.
            if verb == "rsync":
                for env_name in ("rsync_rsh", "rsync_connect_prog"):
                    env_val = rsync_rsh_pending.get(env_name, rsync_rsh_exported.get(env_name))
                    if env_val is not None and _is_ssh_to_self(env_val):
                        return True
                # round-8: with neither a pending nor an exported value in THIS
                # frame, fall back to a self-targeting value carried from an
                # EARLIER frame (inherited into this nested payload).
                if not rsync_rsh_pending and not rsync_rsh_exported and rsync_rsh_carried_self:
                    return True
            positional_pending = verb in ("ssh", "sftp")
            option_shadow = False  # previous token was an option that may take a value
            value_shadow = False  # previous token was an option that CONSUMES a value
            value_shadow_ambiguous = False  # ...but its folded letter is case-ambiguous
            forward_value_pending = False  # previous token was a forward/bind option
            forward_value_letter = ""  # which forward letter armed it (``r`` is checked)
            redirect_target_pending = False  # previous token was a detached redirect op
            rsh_value_pending = False  # previous token was rsync -e/--rsh (value is a local cmd)
            proxyjump_value_pending = False  # previous token was a detached -J (value = hop chain)
            opts_terminated = False  # an exact ``--`` ended option parsing (POSIX)
            depth = 0
            for arg in tokens[i + 1 :]:
                stripped = arg.strip("\"'")
                # Classify BEFORE testing whether the token ends the argv
                # (same order as the self-kill floor): a quoted remote payload
                # may contain separator characters, and for scp/rsync a
                # target can legally follow it.
                if redirect_target_pending:
                    # The filename after a detached ``>``/``2>``/``<`` — bash
                    # removes both words from argv before exec.
                    redirect_target_pending = False
                elif rsh_value_pending:
                    # The detached value of rsync ``-e``/``--rsh``: a
                    # remote-shell command line rsync execs FROM HERE, so
                    # ``-e 'ssh localhost'`` opens a local ssh into this host.
                    # Recurse the floor on the value (strictly shorter than the
                    # whole command, so this terminates).  Checked first so the
                    # value is consumed whatever it looks like.
                    rsh_value_pending = False
                    option_shadow = False
                    value_shadow = False
                    if _is_ssh_to_self(stripped):
                        return True
                elif proxyjump_value_pending:
                    # The detached value of ``-J`` (round-17): a ProxyJump hop
                    # chain whose FIRST hop is dialed from here -- comma-split
                    # it exactly like the attached ``-Jvalue`` spelling, which
                    # ``_routing_option_value_targets_self`` already covers.
                    proxyjump_value_pending = False
                    option_shadow = False
                    value_shadow = False
                    if _proxyjump_value_targets_self(_unmask_separators(stripped)):
                        return True
                elif ">" in stripped or "<" in stripped:
                    # A redirection construct.  The part before the operator is
                    # an ordinary word when non-numeric
                    # (``localhost>/dev/null``); a bare or fd-prefixed operator
                    # (``>``, ``2>``) also consumes the NEXT token as its
                    # target, unless the target is attached (``>/dev/null``,
                    # ``2>&1``).
                    remainder = _strip_redirect(stripped)
                    if remainder and not remainder.isdigit():
                        checkable = positional_pending or verb in ("scp", "rsync")
                        # Host position = the unshadowed ssh/sftp positional
                        # slot; an option value or an scp/rsync file operand
                        # is not one (its ``host:path`` colon form re-enables
                        # the DNS layer inside the check) -- round-17.
                        if checkable and _operand_targets_self(
                            _unmask_separators(remainder),
                            host_position=positional_pending and not value_shadow,
                        ):
                            return True
                        if not value_shadow:
                            # round-18: after a VALUELESS flag the token IS
                            # the positional destination -- consuming the slot
                            # keeps a later dotted remote-command argument
                            # out of host position.
                            positional_pending = False
                    elif stripped.endswith((">", "<")):
                        redirect_target_pending = True
                    option_shadow = False
                    value_shadow = False
                elif stripped == "--" and not opts_terminated:
                    # round-20 (GPT): exact ``--`` is the POSIX option
                    # TERMINATOR -- everything after it is an operand.
                    # Classified as a long option, ``value_shadow`` swallowed
                    # the NEXT token out of host position, so a DNS-classified
                    # self alias after ``--`` was never checked.
                    opts_terminated = True
                    option_shadow = False
                    value_shadow = False
                elif not opts_terminated and stripped.startswith("-") and len(stripped) > 1:
                    # An option.  Its attached ``=value`` is checked only for
                    # the ROUTING options ssh resolves a destination from
                    # (``-ohostname=localhost``, ``-oproxyjump=localhost``) —
                    # a generic ``--opt=value`` (rsync ``--exclude=localhost``)
                    # names data, not a destination.
                    if _routing_option_value_targets_self(
                        _unmask_separators(stripped), value_slot=True
                    ):
                        return True
                    # rsync runs its ``-e``/``--rsh`` value as the remote-shell
                    # command FROM HERE, so ``-e 'ssh localhost'`` execs a local
                    # ssh into this host: the value is a command line, not a
                    # host, and must recurse this floor.  It rides in-token for
                    # ``--rsh=…`` and for a single-dash bundle that reaches
                    # ``e`` with letters after it (``-e'ssh …'`` tokenizes to
                    # ``-essh …``); a bare ``--rsh`` or a bundle ENDING in ``e``
                    # (``-ave``) takes the NEXT token via ``rsh_value_pending``.
                    # Only exactly ``rsh`` among double-dash options consumes a
                    # value, so ``--exclude=localhost`` stays data.
                    if verb == "rsync":
                        bare_opt = stripped.lstrip("-")
                        if stripped == "--rsh":
                            rsh_value_pending = True
                        elif stripped.startswith("--rsh="):
                            if _is_ssh_to_self(stripped.partition("=")[2]):
                                return True
                        elif not stripped.startswith("--") and "e" in bare_opt:
                            attached = bare_opt.partition("e")[2]
                            if attached and _is_ssh_to_self(attached):
                                return True
                            if not attached:
                                rsh_value_pending = True
                    option_shadow = True
                    bare = stripped.lstrip("-")
                    # round-18/19: only an option whose (bundle-final) letter
                    # takes a value for THIS verb swallows the next token out
                    # of host position; ``ssh -v self.example`` keeps its
                    # destination DNS-checked, ``sftp -R 64 localhost`` keeps
                    # its positional slot pending past the consumed ``64``.
                    # Double-dash options keep the conservative value
                    # assumption.
                    value_shadow = stripped.startswith("--") or (
                        bool(bare)
                        and bare[-1] in _VERB_VALUE_TAKING_OPT_LETTERS.get(verb, frozenset())
                    )
                    # round-31: a folded letter whose two cases disagree on
                    # valueness keeps the swallowed token host-checkable.
                    value_shadow_ambiguous = (
                        not stripped.startswith("--")
                        and bool(bare)
                        and bare[-1] in _VERB_FOLD_AMBIGUOUS_OPT_LETTERS.get(verb, frozenset())
                    )
                    # Forward/bind exemption is ssh-ONLY: scp/rsync have no
                    # forward options, and their `-r`/`-l` are valueless
                    # flags -- mis-classifying them as value-taking would hide
                    # the target of `scp -r localhost:…`.  Bundle-final
                    # spellings arm like the ``-4J`` precedent below, and the
                    # LETTER is recorded because ``-R`` is not exempt like its
                    # siblings (see ``_remote_forward_value_targets_self``).
                    forward_value_pending = (
                        verb == "ssh"
                        and not stripped.startswith("--")
                        and bool(bare)
                        and bare[-1] in _SSH_FORWARD_OPT_LETTERS
                    )
                    forward_value_letter = bare[-1] if forward_value_pending else ""
                    # The GLUED remote-forward spelling carries its spec in the
                    # same token (``-R2222:localhost:22``, ``-R2222``); ssh has
                    # no valueless ``-r``, so within the ssh verb a leading
                    # ``r`` with a spec-shaped remainder is the forward.  A
                    # valueless bundle prefix (``-vR2222:…``) is stripped
                    # first, the round-28 ``-o``/``-J`` treatment (round-31).
                    fwd_bare = bare.lstrip(_SSH_VALUELESS_SHORT_FLAGS)
                    if (
                        verb == "ssh"
                        and not stripped.startswith("--")
                        and len(fwd_bare) > 1
                        and fwd_bare[0] == "r"
                        and (":" in fwd_bare[1:] or fwd_bare[1:].isdigit())
                        and _remote_forward_value_targets_self(fwd_bare[1:])
                    ):
                        return True
                    # round-17: a DETACHED ``-J`` -- or a flag bundle ending in
                    # the jump letter (``-4J``) -- takes the NEXT token as its
                    # ProxyJump hop chain.  The attached spelling (``-Jhost``)
                    # is handled by ``_routing_option_value_targets_self``.
                    if not stripped.startswith("--") and bare.endswith("j"):
                        proxyjump_value_pending = True
                        forward_value_pending = False
                        forward_value_letter = ""
                elif forward_value_pending:
                    # The value of a forward/bind option (see
                    # ``_SSH_FORWARD_OPT_LETTERS``) names a listen address or
                    # a far-side hop for every letter EXCEPT ``r``: a
                    # remote-forward destination is dialed FROM HERE, so its
                    # spec is checked instead of exempted (round-27; the
                    # branch table lives on ``_remote_forward_value_targets_self``).
                    if forward_value_letter == "r" and _remote_forward_value_targets_self(
                        _unmask_separators(stripped)
                    ):
                        return True
                    forward_value_pending = False
                    forward_value_letter = ""
                    option_shadow = False
                    value_shadow = False
                else:
                    # An operand, or the value of the preceding option.  Check
                    # it either way (fail-closed — see the docstring); only an
                    # UNSHADOWED operand consumes the ssh/sftp positional slot.
                    checkable = positional_pending or verb in ("scp", "rsync")
                    if checkable and _operand_targets_self(
                        _unmask_separators(stripped),
                        host_position=positional_pending
                        and (not value_shadow or value_shadow_ambiguous),
                    ):
                        return True
                    if _routing_option_value_targets_self(
                        _unmask_separators(stripped), value_slot=option_shadow
                    ):
                        return True
                    if not value_shadow:
                        # round-18: same consumption rule as the redirect
                        # branch above.
                        positional_pending = False
                    option_shadow = False
                    value_shadow = False
                    value_shadow_ambiguous = False
                depth += _substitution_depth_delta(arg)
                if depth <= 0 and _ends_argv(arg):
                    break
                depth = max(depth, 0)
        # End of frame: a leading RSYNC_RSH selector that SURVIVED to here was
        # consumed by the frame's command (which may spawn a nested payload),
        # and an exported one persists for the whole line -- either way, if it
        # is self-targeting, later frames inherit it.  Latch it now (never
        # cleared) so a nested ``sh -c 'rsync ...'`` frame denies.  Same
        # precedence as the in-walk rsync check: pending, else exported.
        for env_name in ("rsync_rsh", "rsync_connect_prog"):
            carried_now = rsync_rsh_pending.get(env_name, rsync_rsh_exported.get(env_name))
            if carried_now is not None and _is_ssh_to_self(carried_now):
                rsync_rsh_carried_self = True
    return False
