"""How a governed item matches a rule pattern.

The enforcer-owned registries the governance model never reads from a policy or profile
document: ``_ORDINAL_SCALES`` (each ordinal's strictness order) and ``_MATCHERS`` (the
five match strategies, extended only through ``register_matcher``). Beside them, the
item normalization the ``path`` matcher applies (``_norm_item``) and the host the
``host`` matcher tests (``_url_host``). A new match strategy goes here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Callable, Dict, Tuple

from kiro_crew.platform.guest_paths import GuestPath, guest_norm

# ──────────────────────────────────────────────────────────────────────────
# Enforcer-owned registries (NEVER sourced from a policy/profile document)
# ──────────────────────────────────────────────────────────────────────────
# Keeping the strictness order and the match strategy in code — not in the
# governed files — is what makes the ceiling un-weakenable: a malicious profile
# cannot reorder "strict < off" to defeat a sandbox floor, nor redefine how a
# path glob matches.  Both registries are append-only by construction.

# Ordinal strictness scales, strictness-ASCENDING (loosest first → strictest
# last).  ``approval`` and ``sandbox`` are verified against the live enforcers
# (approval pipeline; ``sandbox_tiers.py`` level dirs off<standard<cc<strict).
_ORDINAL_SCALES: Dict[str, Tuple[str, ...]] = {
    "approval": ("yolo", "auto", "interactive"),
    "sandbox": ("off", "standard", "cc", "strict"),
}


def _match_identifier(item: str, pattern: str) -> bool:
    """Case-insensitive fnmatch for opaque identifiers (tools, apps, agents)."""
    return fnmatch.fnmatch(item.strip().casefold(), pattern.strip().casefold())


def _match_command(item: str, pattern: str) -> bool:
    """Case-sensitive fnmatch on a shell command body (deterministic per-OS).

    Uses ``fnmatchcase`` (not ``fnmatch``) so the decision does not depend on
    the host's filesystem case-folding — matching ``security``/``admission``.
    """
    return fnmatch.fnmatchcase(item, pattern)


def _match_path(item: str, pattern: str) -> bool:
    """Glob match for filesystem paths, expanding ``~`` / ``$HOME``.

    Case-sensitive (POSIX path semantics).  ``fnmatch``'s ``*`` already crosses
    directory separators, which is the documented (broad) behaviour for a
    deny-oriented read block; callers wanting precise segment semantics should
    prefer narrower patterns.

    Only the ITEM is normalized (``_norm_item``: expand → absolutize → lexically
    collapse ``.``/``..``); the PATTERN is only ``~``/``$VAR``-expanded and is
    matched verbatim.  Normalizing the item — never the pattern — does two jobs
    and avoids one trap:

    * a ``..`` traversal cannot satisfy an ALLOW-mode prefix:
      ``/home/u/ws/../.bashrc`` normalizes to ``/home/u/.bashrc`` and does not
      fnmatch ``/home/u/ws/**`` (without this ``*`` spans the ``..`` and the
      write is wrongly PERMITTED though the OS resolves it outside the allow-list);
    * an agent-supplied RELATIVE item is anchored to an absolute form, so it can
      still match an absolute DENY glob (``../../etc/passwd`` cannot dodge
      ``/etc/**``) instead of silently failing to match and falling open;
    * the trap avoided: ``os.path.normpath`` must NOT touch the pattern — it treats
      ``*``/``**`` as ordinary segments and collapses an adjacent ``..`` against
      them (``/a/**/../b`` → ``/a/b``, silently dropping the ``**``), which would
      widen an allow / shrink a deny.  Matching the pattern verbatim preserves the
      operator's authored globs exactly.

    Normalization is purely lexical (no filesystem ``resolve()``), so it adds no
    I/O.  The relative-item anchor is the host process CWD (the same anchor the
    resolved ``is_sensitive_path`` keystone uses); it cannot perfectly reconstruct
    a backend's CWD, so that always-on keystone remains the authoritative,
    resolved block for the trust-root / credential dirs.

    LEXICAL-ONLY CONTRACT (no ``realpath``): matching does NOT resolve symlinks.
    A symlink that lexically sits inside an allow-prefix
    (``<allow>/link -> <secret>/key``) passes ``_match_path`` even though the OS
    write lands at ``<secret>/key`` outside the allow-list.  This is intentional:
    resolving would add filesystem I/O on every gate call and would refuse writes
    through symlinks an operator placed deliberately (a common workspace layout).
    The contract is therefore: **allow-mode prefixes are a lexical scoping aid,
    not a hardened sandbox against symlinks** — the resolved ``is_sensitive_path``
    keystone (which DOES ``resolve()``) is the authoritative guard for the
    trust-root / credential dirs, and an operator must not rely on an allow-mode
    prefix to confine writes in a directory that contains untrusted symlinks.
    See ``docs/system-specs/modules/governance.md`` → "Path matcher".

    A REMOTE-WORKSPACE target (:class:`~kiro_crew.platform.guest_paths.GuestPath`,
    which only ``classify_tool_args`` mints, from arguments the permission-event
    builder tagged) names a file on the Linux guest, so it is normalized with
    ``posixpath`` instead (:func:`~kiro_crew.platform.guest_paths.guest_norm`):
    anchored at the guest workdir, ``..`` and a leading ``//`` collapsed, no
    ``~``/``$VAR`` expansion with this gateway's values. Chosen per item, so a local
    call on a Windows gateway is matched exactly as before. One with no anchor
    matches no pattern; ``classify_tool_args`` pairs it with the never-permittable
    unverifiable item, which an allow-mode set refuses.
    """
    if isinstance(item, GuestPath):
        norm = guest_norm(item)
        return norm is not None and fnmatch.fnmatchcase(norm, _expand(pattern))
    return fnmatch.fnmatchcase(_norm_item(item), _expand(pattern))


def _match_host(item: str, pattern: str) -> bool:
    """Case-insensitive fnmatch for network egress hosts / globs (DNS is CI)."""
    return fnmatch.fnmatch(item.strip().casefold(), pattern.strip().casefold())


def _match_mcp(item: str, pattern: str) -> bool:
    """Match an MCP reference where a server grant covers all its tools.

    ``item`` is the queried reference in canonical ``@server`` / ``@server/tool``
    form (convert a raw ``mcp__server__tool`` title with :func:`mcp_title_to_ref`
    first).  A ``@server`` pattern matches any ``@server/tool`` under it
    (server-level grant); ``@server/tool`` matches only itself.  A deny at either
    granularity is therefore final (handled by ``ScopedRuleset.permits`` in deny
    mode).  Comparison is case-insensitive on the server/tool identifiers.
    """
    it = item.strip().casefold()
    pat = pattern.strip().casefold()
    if it == pat:
        return True
    # A whole-server pattern (no '/') covers every tool under that server.
    if "/" not in pat:
        return it.startswith(pat + "/")
    return False


# Match-strategy registry.  A ScopedRuleset names its matcher; permits() looks
# it up here.  Adding a domain = (optionally) one new entry, not evaluator code.
_MATCHERS: Dict[str, Callable[[str, str], bool]] = {
    "identifier": _match_identifier,
    "command": _match_command,
    "path": _match_path,
    "host": _match_host,
    "mcp": _match_mcp,
}

_DEFAULT_MATCHER = "identifier"


def _expand(path_str: str) -> str:
    """Expand ``~`` and ``$VAR`` without touching the filesystem (no resolve())."""
    return os.path.expanduser(os.path.expandvars(path_str))


def _norm_item(path_str: str) -> str:
    """Expand, absolutize, then lexically collapse ``.``/``..`` in a queried item.

    Applied to the queried ITEM only (never a pattern — see ``_match_path``).
    Three steps, all purely lexical (no filesystem ``resolve()``, no I/O):

    1. ``_expand`` — ``~`` / ``$VAR`` substitution.
    2. ``os.path.abspath`` — anchor a relative path (e.g. ``../../etc/passwd`` or
       ``etc/passwd``) to the host CWD so it cannot dodge an absolute DENY glob by
       failing to match; ``abspath`` also runs ``normpath``, collapsing ``..``.
       An already-absolute path is unchanged except for the collapse.
    3. collapse a leading ``//`` to ``/`` — POSIX (4.13) leaves a path beginning
       with exactly two slashes implementation-defined, and ``normpath`` PRESERVES
       it (``normpath('//etc/passwd') == '//etc/passwd'``).  An item supplied as
       ``//etc/passwd`` would then not match a ``/etc/**`` deny even though the OS
       opens ``/etc/passwd`` — a deny bypass.  Collapse so the lexical form equals
       what the OS resolves.  A leading ``///+`` already normalizes
       to a single ``/``, so only the exact ``//`` case needs this.

    Collapsing ``..`` is what stops a traversal from satisfying an allow-prefix
    glob.  ``abspath`` cannot reconstruct an ACP backend's actual CWD, so it is a
    best-effort anchor; the resolved ``is_sensitive_path`` keystone remains the
    authoritative block for the trust-root / credential dirs.
    """
    p = os.path.abspath(_expand(path_str))
    if p.startswith("//") and not p.startswith("///"):
        p = p[1:]
    return p


# Schemes that actually open a network connection to a host. A URL on any other
# scheme (mailto:, data:, javascript:, tel:, file:, blob:, …) contacts no host,
# so the egress gate must not derive a phantom host from it.
_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss", "ftp", "ftps"})


def _looks_like_host(token: str) -> bool:
    """True if *token* looks like a network host (vs. a URI scheme word).

    A real ``host:port`` input (``example.com:8080``, ``localhost:3000``,
    ``127.0.0.1:9``) is mis-parsed by urlparse as ``scheme:path`` — the host
    becomes the "scheme".  A non-network URI (``tel:80``, ``gopher:1234``,
    ``mailto:443``) has a bare alphabetic-word scheme.  We distinguish by SHAPE
    rather than a denylist of every URI scheme: a hostname contains a ``.`` or a
    digit, or is exactly ``localhost``; a bare scheme word never does.  This is
    what stops a non-network scheme + numeric payload from yielding a phantom
    host without having to enumerate ``tel``/``gopher``/``sms``/… .
    """
    t = token.lower()
    return t == "localhost" or "." in t or any(c.isdigit() for c in t)


def _url_host(url: str) -> str:
    """Extract the host from a URL for the ``host`` matcher (no network I/O).

    Returns ``""`` for a URL that carries no network host, so a hostless URL is
    NOT mis-classified as egress to a phantom host the fetch would never contact.
    The cases, by shape of the input:

    * ``scheme://authority/…`` — a host only when ``scheme`` is a known NETWORK
      scheme (``http``/``https``/``ws``/``wss``/``ftp``/``ftps``).  ``file:///…``
      and any other scheme with an authority return ``""``.
    * no scheme (``host/path`` or ``//host/path``) — recover the authority via a
      ``//`` retry.
    * ``scheme:rest`` with NO ``://`` — EITHER a non-network URI
      (``mailto:``/``tel:``/``data:`` → no host) OR the scheme-less ``host:port``
      form that urlparse mis-reads as ``scheme:path`` (``example.com:8080`` →
      scheme ``example.com``, path ``8080``).  We retry as an authority ONLY when
      ``rest`` begins with a bare numeric port AND the parsed "scheme" looks like
      a host (``_looks_like_host``).  ``example.com:8080`` / ``localhost:3000`` →
      host; ``tel:80`` / ``mailto:443`` / ``gopher:1234`` → ``""`` (a bare-word
      URI scheme is never a hostname), so they cannot yield a phantom host.
    """
    from urllib.parse import urlparse

    try:
        s = url.strip()
        parsed = urlparse(s)
        netloc = parsed.netloc
        scheme = parsed.scheme.lower()
        if "://" in s:
            # Real scheme + authority: only a network scheme contacts a host.
            netloc = netloc if scheme in _NETWORK_SCHEMES else ""
        elif not scheme:
            # Scheme-less ``host/path`` (netloc empty → recover via ``//`` retry)
            # or protocol-relative ``//host/path`` (netloc already parsed → keep).
            netloc = netloc or urlparse("//" + s).netloc
        elif not netloc:
            # ``scheme:rest`` with no ``://``: the ``host:port`` form ONLY when
            # ``rest`` is a bare numeric port AND the parsed "scheme" looks like a
            # hostname.  ``tel:80``/``mailto:443``/``gopher:1234`` have a bare-word
            # scheme (not a host) so they stay hostless; ``example.com:8080`` /
            # ``localhost:3000`` have a host-shaped token and resolve.
            first_seg = parsed.path.split("/", 1)[0]
            host_port_form = first_seg.isdigit() and _looks_like_host(scheme)
            netloc = urlparse("//" + s).netloc if host_port_form else ""
    except Exception:
        return ""
    # Strip userinfo + port: ``user:pass@host:443`` → ``host``.
    host = netloc.rsplit("@", 1)[-1]
    if host.startswith("["):  # IPv6 literal [::1]:443
        return host[1 : host.index("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def register_matcher(name: str, fn: Callable[[str, str], bool]) -> None:
    """Register an additional match strategy (extensibility seam, append-only).

    Raises if *name* is already registered with a different function so a typo
    cannot silently shadow a built-in matcher.
    """
    existing = _MATCHERS.get(name)
    if existing is not None and existing is not fn:
        raise ValueError(f"matcher {name!r} is already registered")
    _MATCHERS[name] = fn


def url_policy_hosts(url: object) -> Tuple[str, ...]:
    """Every host a client could connect to for *url*, for a ``host``-matcher list item.

    A policy that names hosts is only as strong as the host it is asked about, and
    the HTTP clients in use read one URL differently: ``urllib`` percent-decodes the
    authority, ``requests``/``urllib3`` and ``aiohttp``/``yarl`` treat a backslash
    as the start of the path and IDNA-encode a non-ASCII name, and ``yarl`` reads
    an ideographic full stop as a dot. Rather than chase spellings, each client's
    own parser is asked (when installed), plus the standard library's plain and
    percent-decoded readings. A caller permits the request only when EVERY reading
    returned here is permitted.

    Each reading is lower-cased and loses any IPv6 zone and terminal DNS root
    dot, so entries are written without the dot. An IP literal is asked in canonical form only
    (dotted IPv4, compressed IPv6), so an IP entry names a host in that form. A
    non-ASCII reading is not asked, because no client sends it as written; the
    clients that encode it supply the ``xn--`` form they do send. Every other
    ASCII reading is asked as read, even one with characters a DNS name does not
    use, so a deny glob still sees it. A URL with no ASCII reading is asked as ``""``, which
    matches no entry: an allow list refuses it and a deny list does not. So, as
    for ``network.egress``, an allow list is the hard bound, entries name ASCII
    (``xn--``) hosts, and a deny list refuses the spellings it can name.
    """
    from urllib.parse import unquote, urlsplit

    text = str(url or "").strip()
    raw: list = []

    def _add(read: "Callable[[], object]") -> None:
        try:
            host = read()
        except Exception:
            return
        if isinstance(host, str) and host:
            raw.append(host.strip("[]"))

    _add(lambda: urlsplit(text).hostname)
    # The host part only: decoding the userinfo too would cut the authority at an
    # encoded ``@`` or ``/`` in a password and invent a host from it.
    _add(lambda: urlsplit("//" + unquote(urlsplit(text).netloc.rpartition("@")[2])).hostname)
    # What ``urllib``/``http.client`` connect to for a non-ASCII name: Python's own
    # ``idna`` codec, which maps ``ß`` to ``ss`` where ``urllib3``/``yarl`` keep it.
    for read in list(raw):  # the two standard-library readings so far
        if not read.isascii():

            def _python_idna(host: str = read) -> str:
                return host.encode("idna").decode("ascii")

            _add(_python_idna)
    _add(lambda: urlsplit(text.replace("\\", "/")).hostname)
    try:
        from urllib3.util import parse_url
    except ImportError:
        pass
    else:
        _add(lambda: parse_url(text).host)
    try:
        import yarl
    except ImportError:
        pass
    else:
        _add(lambda: yarl.URL(text).raw_host)

    hosts: list = []

    def _keep(host: str) -> None:
        if host not in hosts:
            hosts.append(host)

    for host in raw:
        spelled = host.lower()
        # An IPv6 zone (``fe80::1%eth0``) names an interface, not another host.
        if ":" in spelled and "%" in spelled:
            spelled = spelled.split("%", 1)[0]
        bare = spelled.rstrip(".")
        ip = _canonical_ip(bare)
        if ip:
            # An IP literal is asked in canonical form only, whatever spelling the
            # resolver accepts (``167772165``, ``0x0a000005``, ``::ffff:10.0.0.5``).
            _keep(ip)
            continue
        if not bare.isascii():
            # Not ASCII: no client sends it as written. The clients that
            # IDNA-encode it contribute their own reading.
            continue
        # Any other ASCII reading is asked as read, even an odd one
        # (``clip+one.blocked.example``): a resolver may still answer for it, so
        # a deny glob must see it, and an allow list refuses it.
        _keep(bare)
    if not hosts:
        # No reading is a host a client would send: ``""``, which an allow list
        # refuses.
        hosts.append("")
    return tuple(hosts)


def _canonical_ip(host: str) -> str:
    """The canonical text of *host* when it is an IP literal in any form, else ``""``."""
    import ipaddress

    from kiro_crew.security import canonicalize_ip

    try:
        return str(ipaddress.ip_address(canonicalize_ip(host)))
    except ValueError:
        return ""


#: The characters a host reading can hold: a URL host's own characters (RFC 3986
#: unreserved, sub-delims and ``%``), lower-cased, plus an IP literal's colons.
#: ``url_policy_hosts`` asks every ASCII reading, so a list entry is held to these.
_PLAIN_HOST_RE = re.compile(r"[a-z0-9._~!$&'()*+,;=%:-]+")
