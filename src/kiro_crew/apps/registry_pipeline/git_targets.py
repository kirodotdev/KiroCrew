"""Clone targets: how a git URL is read, checked, compared and made credential-free.

One owner for the text rules every clone and every consent decision share: the
target an entry names (``_entry_git_url``), its host and transport
(``_git_url_host``, ``_is_ssh_git_url``, ``_clone_sandbox_mode``), the forms that
cannot serve as one identity (``_git_target_is_unsupported``), userinfo stripping
and normalization (``_strip_git_target_userinfo``, ``_normalize_git_target``,
``_same_git_target``), the one-shot credential environment
(``_git_transport_env``), and the fixed classes git output from a credentialed
transport is reduced to before it is logged.
"""

from __future__ import annotations

import os
import re
from ipaddress import IPv6Address
from typing import Any


def _entry_git_url(entry: dict[str, Any]) -> str:
    """Resolve the clone URL for a registry entry.

    Prefers an explicit ``gitUrl`` field.  Falls back to the legacy ``repo``
    field (which may itself contain a full URL).  Returns an empty string if
    neither yields something that looks cloneable — including when an
    index-controlled value is not a string at all (an object-valued ``gitUrl``
    from a malformed external index must degrade to "no URL", never crash the
    caller).
    """
    raw = entry.get("gitUrl") or entry.get("repo") or ""
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _public_registry_name(reg: Any) -> str:
    """Credential-free identity stamped on rows and returned to callers."""
    return _strip_git_target_userinfo(reg.name or reg.repo)


def _redact_url_userinfo(url: str) -> str:
    """Strip any ``user[:password]@`` from *url* before it reaches a log.

    A clone URL is index-supplied and may embed credentials
    (``https://user:token@host/path``). These URLs are written to the SEL audit
    trail and to warnings, both of which persist and the former of which is
    dashboard-readable, so the credential must not travel with them.

    Userinfo is removed rather than the whole URL: a record whose purpose is
    "credentials were offered to clone THIS" is worth little if it cannot say
    which repository, and a bare host cannot distinguish two repos on one forge.
    """
    if not url:
        return url
    scheme, sep, rest = url.partition("://")
    if sep:
        head, slash, tail = rest.partition("/")
        if "@" in head:
            host = head.rsplit("@", 1)[1]
            return f"{scheme}://[redacted]@{host}{slash}{tail}"
        return url
    # scp-style ``user@host:path`` carries no password, but the user is still an
    # identity; normalise it the same way so both forms read alike in a log.
    if "@" in url and ":" in url.split("@", 1)[1]:
        return "[redacted]@" + url.split("@", 1)[1]
    return url


def _looks_like_git_url(url: str) -> bool:
    """Heuristic: does *url* look like a git-cloneable remote?

    Accepts ``https://``/``http://``/``ssh://``/``git://`` URLs and
    ``user@host:path`` scp-style remotes.  A bare token (no scheme, no
    ``@host:``) is treated as a local/name reference, not cloneable.
    """
    if not url:
        return False
    if url.startswith(("https://", "http://", "ssh://", "git://", "git+")):
        return True
    # scp-style: user@host:path
    if re.match(r"^[^/@]+@[^/:]+:.+", url):
        return True
    return False


# Well-known public git forges that legitimately serve repos over SSH. Cloning
# from one of these may need ~/.ssh exposed for key auth (private repos), so the
# sandbox is loosened from "strict" to "standard" ONLY for these hosts plus any
# host the user explicitly configured as an external registry. Everything else
# stays "strict" (~/.ssh hidden) so a typo'd/hostile remote can never be offered
# the owner's SSH keys. https remotes never need ~/.ssh and always stay strict.
_PUBLIC_GIT_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "ssh.github.com",
        "gitlab.com",
        "bitbucket.org",
        "git.sr.ht",
        "codeberg.org",
    }
)


def _git_target_has_ambiguous_scp_prefix(url: str) -> bool:
    """Whether a no-scheme target has Git's host/path colon before ``@``."""
    target = (url or "").strip()
    if "://" in target:
        return False
    at_index = target.find("@")
    colon_index = target.find(":")
    return at_index > 0 and 0 <= colon_index < at_index


def _git_target_has_ambiguous_ssh_userinfo(url: str) -> bool:
    """Whether an SSH URI has colon-bearing routing userinfo.

    Git passes the complete ``user:segment`` spelling to OpenSSH as the remote
    username; the segment is not a password field.  Rewriting it to ``user``
    would therefore make the consent/host identity differ from the transport.
    """
    target = (url or "").strip()
    scheme, sep, rest = target.partition("://")
    if not sep or scheme.lower() not in {"ssh", "git+ssh"}:
        return False
    authority_end = len(rest)
    for delimiter in "/?#":
        found = rest.find(delimiter)
        if found >= 0:
            authority_end = min(authority_end, found)
    authority = rest[:authority_end]
    userinfo, at, _hostport = authority.rpartition("@")
    return bool(at and ":" in userinfo)


def _normalized_ipv6_literal(value: str) -> str:
    """Canonical bracket contents, or ``""`` for malformed/non-IPv6 text."""
    if not value or "%" in value:
        return ""
    try:
        return IPv6Address(value).compressed.lower()
    except ValueError:
        return ""


def _valid_git_port(value: str) -> bool:
    """Validate a decimal TCP port without unbounded integer conversion."""
    return 1 <= len(value) <= 5 and value.isascii() and value.isdigit() and 0 < int(value) <= 65535


def _git_url_host(url: str) -> str:
    """Extract an exact lowercase host from a Git URI/SCP target.

    Bracketed IPv6 literals are validated and returned without brackets in
    canonical compressed form. Malformed authorities, unbracketed IPv6, empty
    hosts, and colon-before-``@`` SCP identities fail closed to ``""``.
    """
    target = (url or "").strip()
    if not target or any(ch.isspace() for ch in target):
        return ""
    if (
        "?" in target
        or "#" in target
        or _git_target_has_ambiguous_scp_prefix(target)
        or _git_target_has_ambiguous_ssh_userinfo(target)
    ):
        return ""

    scheme_end = target.find("://")
    if scheme_end >= 0:
        scheme = target[:scheme_end]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.\-]*", scheme):
            return ""
        rest = target[scheme_end + 3 :]
        authority_end = len(rest)
        for delimiter in "/?#":
            found = rest.find(delimiter)
            if found >= 0:
                authority_end = min(authority_end, found)
        authority = rest[:authority_end]
        if not authority or authority.count("@") > 1:
            return ""
        _userinfo, at, hostport = authority.rpartition("@")
        if not at:
            hostport = authority

        if hostport.startswith("["):
            close = hostport.find("]", 1)
            if close <= 1:
                return ""
            suffix = hostport[close + 1 :]
            if suffix and (not suffix.startswith(":") or not _valid_git_port(suffix[1:])):
                return ""
            return _normalized_ipv6_literal(hostport[1:close])

        if any(ch in "[]/@" for ch in hostport):
            return ""
        if ":" in hostport:
            if hostport.count(":") != 1:
                return ""
            host, port = hostport.rsplit(":", 1)
            if not _valid_git_port(port):
                return ""
        else:
            host = hostport
        return host.lower() if host else ""

    if target.count("@") > 1:
        return ""
    _userinfo, at, host_and_path = target.rpartition("@")
    if not at:
        host_and_path = target
    if host_and_path.startswith("["):
        close = host_and_path.find("]", 1)
        if close <= 1 or close + 1 >= len(host_and_path):
            return ""
        if host_and_path[close + 1] != ":" or not host_and_path[close + 2 :]:
            return ""
        return _normalized_ipv6_literal(host_and_path[1:close])

    colon = host_and_path.find(":")
    if colon <= 0 or not host_and_path[colon + 1 :]:
        return ""
    host = host_and_path[:colon]
    if any(ch in "[]/@" for ch in host):
        return ""
    return host.lower()


def _is_ssh_git_url(url: str) -> bool:
    """True when *url* clones over SSH (and would need ~/.ssh for key auth)."""
    target = (url or "").strip()
    scheme_end = target.find("://")
    if scheme_end >= 0:
        return target[:scheme_end].lower() in {"ssh", "git+ssh"} and bool(_git_url_host(target))
    return "@" in target and bool(_git_url_host(target))


def _clone_sandbox_mode(git_url: str, trusted_hosts: frozenset[str] | None = None) -> str:
    """Pick the sandbox mode for cloning *git_url*.

    Returns ``"standard"`` (exposes ~/.ssh so git can offer the owner's SSH
    keys) ONLY for an SSH/scp remote whose host is trusted — a well-known
    public forge or a host the user explicitly configured as an external
    registry. All other cases return ``"strict"`` (~/.ssh hidden): https/git
    remotes never need SSH keys, and an untrusted SSH host fails closed rather
    than being offered the owner's private keys.
    """
    if _git_target_is_unsupported(git_url) or not _is_ssh_git_url(git_url):
        return "strict"
    host = _git_url_host(git_url)
    if not host:
        return "strict"
    allowed = _PUBLIC_GIT_HOSTS | (trusted_hosts or frozenset())
    return "standard" if host in allowed else "strict"


def _strip_git_target_userinfo(url: str) -> str:
    """Return *url* without embedded secrets, preserving SSH usernames.

    Clone credentials select *who may fetch* a repository; they are not part of
    the repository's identity. Keeping them in the consent identity both made
    credential rotation invalidate an otherwise identical grant and, worse,
    carried secrets through ``trustRepository`` into config and the dashboard.

    HTTP(S) userinfo is an authentication capability, so it is removed in full.
    In SSH and git+ssh URLs the username is transport routing (and is often
    required by the server); retain username-only userinfo. Colon-bearing SSH
    userinfo is also routing text rather than a password field, so executable
    and governance paths reject it as an ambiguous identity. This metadata-only
    sanitizer redacts the suffix rather than returning it through persistence.
    The scp form (``user@host:path``) has no password field, so its username is
    retained; a password-like prefix is reduced to that username. URI query and
    fragment components are also excluded because a free-form source can place a
    token there and no downstream API/log/persistence sink can classify every
    provider-specific secret key safely. Bare/local paths remain untouched.
    """

    target = (url or "").strip()
    scheme, sep, rest = target.partition("://")
    if sep:
        suffix_at = len(rest)
        for delimiter in "/?#":
            found = rest.find(delimiter)
            if found >= 0:
                suffix_at = min(suffix_at, found)
        authority, suffix = rest[:suffix_at], rest[suffix_at:]
        public_suffix_at = len(suffix)
        for delimiter in "?#":
            found = suffix.find(delimiter)
            if found >= 0:
                public_suffix_at = min(public_suffix_at, found)
        public_suffix = suffix[:public_suffix_at]
        userinfo, at, hostport = authority.rpartition("@")
        if not at:
            return f"{scheme}://{authority}{public_suffix}"
        if scheme.lower() in {"ssh", "git+ssh"}:
            username, password_sep, _password = userinfo.partition(":")
            safe_authority = f"{username}@{hostport}" if username else hostport
            # A username-only SSH authority is already safe and must remain
            # byte-for-byte stable; this also keeps callers from classifying it
            # as a credentialed transport merely because it contains ``@``.
            if not password_sep:
                safe_authority = authority
        else:
            safe_authority = hostport
        return f"{scheme}://{safe_authority}{public_suffix}"

    # SCP's standard ``user@host:path`` form carries a routing username, not a
    # password, and must remain byte-for-byte stable. A password-like
    # ``user:password@host:path`` is not a valid SCP credential mechanism: Git
    # treats the first colon as host/path, so executable/governance callers must
    # reject it via ``_git_target_is_unsupported``. This helper still redacts the
    # credential-like segment for metadata-only registration and API projection.
    # Bare/local paths stay untouched unless the suffix is SCP-shaped.
    at_index = target.rfind("@")
    if at_index <= 0:
        return target
    userinfo = target[:at_index]
    username, password_sep, _password = userinfo.partition(":")
    if not password_sep:
        return target

    authority_and_path = target[at_index + 1 :]
    colon = authority_and_path.find(":")
    if not authority_and_path.startswith("[") and colon <= 0:
        # No SCP-shaped ``host:path`` suffix: preserve a local filename that
        # merely contains both ':' and '@'.
        return target
    safe_username = (
        username if username and all(ch.isalnum() or ch in "._-" for ch in username) else ""
    )
    prefix = f"{safe_username}@" if safe_username else ""
    return f"{prefix}{authority_and_path}"


def _git_target_has_query_or_fragment(url: str) -> bool:
    """Whether a clone target has a suffix that cannot be a safe identity."""
    return "?" in (url or "") or "#" in (url or "")


def _git_target_is_unsupported(url: str) -> bool:
    """Whether *url* cannot safely serve as both consent and Git identity.

    A query may select different server-side content, so stripping it from the
    consent identity while still using it for transport would reintroduce a
    repository-rebinding gap. A colon-before-@ SCP target has the same problem:
    Git treats the colon as host/path, not as a password separator. In SSH URI
    userinfo, Git passes the whole colon-bearing string as the OpenSSH username;
    it is likewise not a password that can be removed without changing routing.
    Registry clone and governance paths reject these forms before trust or fetch;
    metadata-only paths may still sanitize them to prevent credential-like text
    from entering persistence and APIs.
    """
    return (
        _git_target_has_query_or_fragment(url)
        or _git_target_has_ambiguous_scp_prefix(url)
        or _git_target_has_ambiguous_ssh_userinfo(url)
    )


def _loggable_git_transport_output(text: str, *, credentialed: bool) -> str:
    """Return git output that is safe to publish or log.

    A credential-bearing URL is expanded inside git from the one-shot transport
    environment, and git is allowed to echo that expanded URL on failure. In that
    posture we never try to scrub and forward free text: doing so would keep the
    raw credential in the logging dataflow and make correctness depend on an
    exhaustive replacement grammar. Return only fixed classifications instead.

    Non-credentialed transports retain the historical output because there is no
    embedded userinfo capability for git to echo.
    """

    if not credentialed or not text:
        return text
    if _git_output_is_auth_shaped(text):
        return "git authentication failed (credentialed transport details redacted)"
    failure_class = _redacted_git_failure_class(text)
    if failure_class:
        return f"git transport failed: {failure_class} (details redacted)"
    return "git transport output redacted (credentialed remote)"


def _git_transport_env(
    credential_target: str, safe_target: str, env: dict[str, str]
) -> dict[str, str]:
    """Return the one-shot environment for an already-sanitized git command.

    Embedded HTTP/SSH userinfo may be needed for the network request, but handing
    that URL directly to ``git clone`` also persists it as ``remote.origin.url``.
    Callers therefore build and sandbox argv with *safe_target* only. This helper
    receives the credential-bearing transport target separately and returns only
    an environment -- never an argv element or repository identity.

    The mapping is passed through ``GIT_CONFIG_*`` instead of ``git -c`` argv.
    Besides keeping process listings credential-free, this is a security boundary:
    sandbox diagnostics retain and log argv, so raw userinfo must never enter that
    generic API. The returned environment is a copy so callers can create it only
    after :func:`wrap_argv` and give it to the exact clone/fetch/pull subprocess;
    local setup and checkout commands keep the credential-free base environment.

    Git may execute repository- or operator-configured hooks, fsmonitor commands,
    and credential helpers inside that same subprocess. Those children inherit
    the command environment, including the one-shot URL rewrite. Append fixed
    neutralizers after every inherited entry so none of those extension points
    can receive the embedded credential while this command is in flight.
    """

    if _git_target_is_unsupported(credential_target):
        raise ValueError(
            "git clone target contains an unsupported query or fragment or an "
            "ambiguous Git transport identity"
        )
    if _strip_git_target_userinfo(credential_target) != safe_target:
        raise ValueError("git credential target does not match the safe clone target")
    if not credential_target or credential_target == safe_target:
        return env
    scheme = credential_target.partition("://")[0].lower()
    if scheme not in {"http", "https"}:
        # Only Git's built-in HTTP transport has a supported embedded-userinfo
        # credential contract here. Arbitrary schemes can dispatch a configured
        # remote helper or proxy; giving those processes the raw rewrite would
        # turn an unclassified extension point into a credential recipient.
        raise ValueError("embedded git credentials require an HTTP(S) target")

    transport_env = dict(env)
    try:
        config_count = int(transport_env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        # ``git`` would reject a malformed count too. Resetting the command-local
        # sequence is the useful fail-safe here: the caller's input mapping is not
        # mutated, and no inherited entry can displace the credential rewrite.
        config_count = 0
    if config_count < 0:
        config_count = 0
    command_config = (
        (f"url.{credential_target}.insteadOf", safe_target),
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("core.askPass", ""),
        # Keep hooksPath last: within the command-scope config it must win over
        # a duplicate inherited entry as well as repository/global config.
        ("core.hooksPath", os.devnull),
    )
    for key, value in command_config:
        transport_env[f"GIT_CONFIG_KEY_{config_count}"] = key
        transport_env[f"GIT_CONFIG_VALUE_{config_count}"] = value
        config_count += 1
    transport_env["GIT_CONFIG_COUNT"] = str(config_count)
    return transport_env


def _normalize_git_target(url: str) -> str:
    """Canonical form used whenever a security decision compares clone URLs.

    Cosmetic variance between the separately-authored seed and catalog is a trailing
    ``/``, a trailing ``.git``, and the case of the scheme and host -- those three
    are normalised.

    **The PATH keeps its case.** Repository paths are case-sensitive on plenty of
    forges, so folding them makes two DIFFERENT repositories compare equal, and this
    predicate is what decides whether a catalog row may stand in for a bundled app.
    App trust is keyed by name, so a false "same target" here is the name-rebinding
    that requiring URL equality exists to prevent.
    """

    normalized = _strip_git_target_userinfo(url).rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    scheme, sep, rest = normalized.partition("://")
    if not sep:
        # No scheme to split on (scp-style or a bare path): fold nothing, since
        # the host cannot be told from the path without guessing.
        return normalized

    # Split the authority without parsing/re-serialising the rest of the URL.
    # ``urlsplit`` exposes convenient hostname/port properties, but rebuilding
    # from decoded components changes exact spelling. Only the URI scheme and
    # HOSTNAME are case-insensitive. The port and path remain byte-for-byte;
    # Query/fragment suffixes were removed above (and registry clone paths reject
    # them before transport). HTTP credentials and colon-bearing SSH userinfo were
    # removed; executable paths reject the latter because it changes routing. A
    # username-only SSH authority remains because dropping it changes which
    # endpoint Git actually invokes.
    suffix_at = len(rest)
    for delimiter in "/?#":
        found = rest.find(delimiter)
        if found >= 0:
            suffix_at = min(suffix_at, found)
    authority, suffix = rest[:suffix_at], rest[suffix_at:]

    username = ""
    hostport = authority
    userinfo, at, candidate_hostport = authority.rpartition("@")
    if at:
        username = f"{userinfo}@"
        hostport = candidate_hostport
    if hostport.startswith("["):
        # RFC URI IPv6 literals are bracketed. Preserve brackets and the exact
        # non-default port spelling while folding only the literal hostname.
        close = hostport.find("]")
        if close >= 0:
            hostport = f"[{hostport[1:close].lower()}]{hostport[close + 1:]}"
    elif hostport.count(":") <= 1:
        # Zero/one colon is an ordinary hostname with an optional port. More
        # than one is malformed/unbracketed IPv6; do not guess where its host
        # ends because a false equivalence here rebinds an execution grant.
        hostname, colon, port = hostport.rpartition(":")
        if colon:
            hostport = f"{hostname.lower()}:{port}"
        else:
            hostport = hostport.lower()

    return f"{scheme.lower()}://{username}{hostport}{suffix}"


def _same_git_target(a: str, b: str) -> bool:
    """Whether two clone URLs name the same repository."""

    if _git_target_is_unsupported(a) or _git_target_is_unsupported(b):
        return False
    return bool(a) and _normalize_git_target(a) == _normalize_git_target(b)


# Auth/permission failure classes on the clone-failure surface. The clone
# subprocess merges stderr into stdout (``stderr=STDOUT``), so this classifier
# sees git's auth-failure text. Kept as a STRICT allowlist of known
# auth-refusal phrasings so the credential-posture remedy ("private app repos
# must live inside the registry repo") only fires when withheld credentials
# are a plausible cause — an owner who hits a typo'd branch or a DNS blip must
# NOT be told to restructure their repositories. Matched case-insensitively.
_GIT_AUTH_FAILURE_MARKERS = (
    # SSH credential refusal ONLY. The bare token "permission denied" also
    # appears in a LOCAL filesystem error — an unwritable clone destination
    # emits `fatal: could not create work tree dir '…': Permission denied` —
    # so matching it mislabels a disk-permission failure as withheld remote
    # credentials and shows the "move the repo inside the registry" hint on an
    # error that has nothing to do with credentials. SSH's real auth-refusal
    # wording always carries the method parenthetical (`git@host: Permission
    # denied (publickey).`, also `(publickey,password)` /
    # `(publickey,keyboard-interactive)`), which a local errno `Permission
    # denied` never has — so anchor on `permission denied (publickey` (open
    # paren, no close, to catch every comma-separated method list).
    "permission denied (publickey",
    "authentication failed",
    "could not read username",
    "could not read password",
    "access denied",
    "fatal: authentication",
    "terminal prompts disabled",
    "invalid username or password",
)


# Known NON-auth failure classes, mapped to a fixed derived label. This is an
# allowlist emitting a CONSTANT string per class — never a slice of raw git
# output — so no credential-bearing or path-bearing stderr can reach the
# banner (PR-1418 lesson: free-text stderr passthrough cannot be closed by
# shape enumeration). Matched case-insensitively; first match wins.
_GIT_FAILURE_CLASS_LABELS: tuple[tuple[str, str], ...] = (
    ("could not resolve", "host could not be resolved"),
    ("connection timed out", "the connection timed out"),
    ("connection refused", "the connection was refused"),
    ("network is unreachable", "the network was unreachable"),
    ("remote branch", "the requested branch does not exist"),
    # Anchored on git's own ref-error phrasing ("couldn't find remote ref …",
    # "remote ref … does not exist"), NOT the bare token "does not exist": that
    # substring also appears in unrelated failures (e.g. a path/pathspec error),
    # and matching it would mislabel them "the requested ref does not exist".
    # "remote ref" occurs only in git's missing-ref messages, so it stays a
    # precise ref-error signal.
    ("remote ref", "the requested ref does not exist"),
    # Anchored on git/curl/(open|gnu)tls TLS-error phrasing, NOT the bare token
    # "ssl": that substring also appears in a repo URL (e.g. cloning
    # github.com/openssl/openssl, whose stderr echoes the URL), and matching it
    # would mislabel an ordinary auth/not-found failure "a TLS/SSL error
    # occurred" — the exact false-positive class this table guards against.
    # These phrases occur in genuine TLS handshake/verification errors
    # ("SSL certificate problem …", "SSL routines:…", "gnutls_handshake()
    # failed", "TLS handshake failed", "Unsupported SSL backend 'schannel'")
    # and never in a normal repo URL path segment. Deliberately no bare "ssl_"
    # anchor: a repo path like ".../ssl_utils" would match it. Likewise the
    # gnutls anchor carries git's full symbol "gnutls_handshake" rather than the
    # bare library name, so cloning github.com/gnutls/gnutls (whose stderr
    # echoes the URL) is not mislabeled a TLS error.
    ("ssl certificate", "a TLS/SSL error occurred"),
    ("ssl routines", "a TLS/SSL error occurred"),
    ("ssl backend", "a TLS/SSL error occurred"),
    ("gnutls_handshake", "a TLS/SSL error occurred"),
    ("tls handshake", "a TLS/SSL error occurred"),
)


def _git_output_is_auth_shaped(text: str) -> bool:
    """Whether *text* matches a known auth/permission failure class.

    Strict allowlist — see :data:`_GIT_AUTH_FAILURE_MARKERS`. Never echoes
    *text*; returns only a boolean.
    """
    low = text.lower()
    return any(marker in low for marker in _GIT_AUTH_FAILURE_MARKERS)


def _redacted_git_failure_class(text: str) -> str:
    """A fixed, derived label for a known non-auth failure class, or ``""``.

    Returns a CONSTANT allowlisted phrase — never a slice of *text* — so no
    credential-bearing or path-bearing subprocess output reaches the banner.
    """
    low = text.lower()
    for marker, label in _GIT_FAILURE_CLASS_LABELS:
        if marker in low:
            return label
    return ""
