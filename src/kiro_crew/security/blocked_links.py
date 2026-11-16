"""Which links in a reply are not rendered live: the blocked-link decision.

A link whose URL the exfiltration heuristics flag (a long or base64-looking query,
heavy percent-encoding, a payload still encoded when the decode budget runs out, or a
credential standing in the URL as the text holds it) is not rendered as a live link
and is not fetched: the dashboard shows a Blocked link card in its place until the
reader allows its host. This is a rendering decision, never a rewrite. The saved
transcript, the live stream, channel egress and exports keep every URL exactly as
written, and nothing here returns text.

:func:`blocked_links` is the decision. It judges each URL with the per-URL verdict the
blocking scan uses (``exfil._flagged_url_span``), so the platform's exact-host
exemptions, Kiro Crew's own Slack app-create deep link, Outlook's own web links and a
valid presigned S3 URL stay live here exactly as the scan passes them. A diagram link
(``plantuml.com/plantuml/svg/<diagram>``) or a shared-document link carries its id in
the path with no long query, so the verdict passes it like any other clean link. The
decision reads neither the owner's redaction setting nor an output seam: it applies
with redaction on or off, like the click-to-load remote images.

Hosts a reader allowed (``security.redaction_allow``) are passed in. An allowed host
relaxes only the query-length and base64 rules (:data:`ALLOWABLE_BLOCKED_LINK_RULES`),
the relaxation the platform's exact-host exemption gives, so a link on it that is
flagged for anything else stays blocked. The dashboard renders inside
:func:`allowed_link_hosts`, and :func:`scoped_blocked_links` writes one
``redaction_allowed_host_used`` SEL event the first time in a process that an
allowance lets a flagged link render live in a workspace.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import threading
from collections.abc import Iterator
from urllib.parse import unquote_plus

from .exfil import (
    _URL_SCAN_RE,
    _exfil_exempt_hosts,
    _flagged_url_span,
    _log_operator_list_use,
)
from .exfil_url_rules import _MAX_URL_DECODE_PASSES
from .redaction import redact_credentials
from .redaction_switch import always_scan

#: The rules an allowed host relaxes: a long query and a base64-looking one. A link
#: blocked for anything else stays blocked whatever the reader allowed.
ALLOWABLE_BLOCKED_LINK_RULES = frozenset({"exfil_query_length", "exfil_query_pattern"})
#: The longest address a card offers to open or copy. A longer one is described with
#: the reason ``length``: its link stays blocked, and the card offers no action on it.
MAX_BLOCKED_LINK_URL_CHARS = 8192
#: The rule a record names when the verdict flagged a URL without tracing one. Every
#: flagged return of ``exfil._exfil_url_warning`` traces a rule, so this only keeps
#: such a link blocked rather than rendering it live.
UNTRACED_RULE = "exfil_url"
#: The keys a record carries, and nothing besides.
BLOCKED_LINK_RECORD_KEYS = ("url", "domain", "rule", "query_chars", "withheld")
#: The rules that flag a URL for a credential it carries, as written or encoded, or for
#: a payload still encoded when the decode budget runs out. Opening such an address can
#: send that value, so its card never offers to.
_CREDENTIAL_RULES = frozenset(
    {
        "exfil_hard_credential",
        "exfil_fixed_credential",
        "exfil_encoded_credential",
        "exfil_decode_saturated",
    }
)


def _credential_clean(value: str) -> bool:
    """Whether the credential pass leaves *value* unchanged in every decoded form.

    Checked as written AND in every bounded percent-decoded form: a partially
    encoded credential (``ghp%5F...``) is unchanged as literal text while a browser
    decodes it back into the secret the moment it is requested. Saturation fails
    closed. Run under ``always_scan``: this decides whether opening the address can
    send a credential, which the owner's choice not to redact what they read does
    not change.
    """
    with always_scan():
        if redact_credentials(value)[0] != value:
            return False
        decoded = value
        for _ in range(_MAX_URL_DECODE_PASSES):
            nxt = unquote_plus(decoded)
            if nxt == decoded:
                return True
            decoded = nxt
            if redact_credentials(decoded)[0] != decoded:
                return False
    return unquote_plus(decoded) == decoded


def _withheld(url: str, rule: str) -> str | None:
    """Why the card offers no Open once or Copy for *url*, or None when it may.

    ``length``: longer than :data:`MAX_BLOCKED_LINK_URL_CHARS`. ``credential``: the
    verdict flagged it for a credential (:data:`_CREDENTIAL_RULES`), the authority
    carries userinfo, or some decoded form of the address holds a value the
    credential pass reads as a credential, so opening it would send that secret.
    """
    if len(url) > MAX_BLOCKED_LINK_URL_CHARS:
        return "length"
    rest = url.split("://", 1)[-1]
    authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if rule in _CREDENTIAL_RULES or "@" in authority or not _credential_clean(url):
        return "credential"
    return None


def _record(flagged: tuple, rules: list[str]) -> dict:
    judged, url, path_and_query, _warning = flagged
    qmark = path_and_query.find("?")
    rule = rules[0] if rules else UNTRACED_RULE
    return {
        "url": url,
        "domain": judged.group(1).lower(),
        "rule": rule,
        "query_chars": len(path_and_query) - qmark - 1 if qmark != -1 else 0,
        "withheld": _withheld(url, rule),
    }


def blocked_links(
    text: str,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
    relaxed_out: dict[str, set[str]] | None = None,
) -> list[dict]:
    """One record per distinct URL in *text* that is not rendered as a live link.

    Pure: *text* is only read. Records come in first-appearance order, one per
    distinct URL string however often it appears::

        {"url": str, "domain": str, "rule": str, "query_chars": int,
         "withheld": "credential" | "length" | None}

    ``url`` is the span of *text* the verdict judged, exactly as written, so a
    renderer can match a link's address against it; ``domain`` is its host,
    lowercased. ``withheld`` says why the card offers no Open once or Copy
    (:func:`_withheld`). There is no count cap: a URL the list leaves out would
    render live, so the list is bounded by the text it describes.

    *allowed_hosts* are the hosts a reader allowed for this text's workspace; they
    relax only :data:`ALLOWABLE_BLOCKED_LINK_RULES`. *relaxed_out*, when given,
    receives ``{host: {rule, ...}}`` for every allowed host that let a flagged link
    through, which is what the use audit names.
    """
    records: list[dict] = []
    seen: set[str] = set()
    for _start, flagged, rules in _blocked(text, allowed_hosts, relaxed_out):
        url = flagged[1]
        if url in seen:
            continue
        seen.add(url)
        records.append(_record(flagged, rules))
    return records


def _blocked(
    text: str, allowed_hosts: frozenset[str], relaxed_out: dict[str, set[str]] | None
) -> Iterator[tuple[int, tuple, list[str]]]:
    """``(start, flagged, rules)`` for each occurrence of a blocked URL in *text*."""
    if not text or "://" not in text:
        return
    platform = _exfil_exempt_hosts()
    allowed = frozenset(h.lower() for h in allowed_hosts)
    for match in _URL_SCAN_RE.finditer(text):
        rules: list[str] = []
        flagged = _flagged_url_span(match, platform, rules)
        if flagged is None:
            continue
        domain = flagged[0].group(1).lower()
        if allowed and domain in allowed:
            # Only relaxes: an exempt host skips the length and base64 rules alone,
            # so this judges the same match again with the allowance in force.
            relaxed_rules: list[str] = []
            relaxed = _flagged_url_span(match, platform | allowed, relaxed_rules)
            if relaxed is None:
                if relaxed_out is not None:
                    relaxed_out.setdefault(domain, set()).update(rules or (UNTRACED_RULE,))
                continue
            flagged, rules = relaxed, relaxed_rules
        yield text.find(flagged[1], match.start()), flagged, rules


# ── the render scope and the use audit ───────────────────────────────────────

#: Hosts a reader allowed for the workspace whose rows are being rendered, set only
#: by :func:`allowed_link_hosts` around one render.
_SCOPED_HOSTS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "kiro_crew_allowed_link_hosts", default=frozenset()
)
#: The workspace those hosts were allowed in, so a use audit names the allowance.
_SCOPED_WORKSPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kiro_crew_allowed_link_workspace", default=""
)
#: The revoke epoch read before those hosts were, so a use audit can tell a snapshot
#: taken before a revoke from one taken after it. None outside a scope.
_SCOPED_EPOCH: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "kiro_crew_allowed_link_epoch", default=None
)

# One ``redaction_allowed_host_used`` event per (workspace, host) per process: every
# history load and live frame renders the same links again. The memo holds at most
# ``_ALLOWED_HOST_AUDITED_MAX`` pairs; past it a new pair is audited on every render
# but not remembered: more SEL records, bounded memory, owner-added hosts. A revoke
# forgets the host, so a later re-allow is audited on first use.
_ALLOWED_HOST_AUDITED: set[tuple[str, str]] = set()
_ALLOWED_HOST_AUDITED_MAX = 1024
# One lock for the memo and the epoch: a revoke iterates the set while render
# workers add to it, and an unguarded add mid-iteration raises.
_ALLOWED_HOST_AUDIT_LOCK = threading.Lock()
# Bumped on every revoke. Render memos key on it, so a row rendered before the revoke
# is rendered again after a re-allow and its use is audited again.
_allowed_host_audit_epoch = 0


@contextlib.contextmanager
def allowed_link_hosts(
    hosts: frozenset[str], workspace: str | None = None, epoch: int | None = None
) -> Iterator[None]:
    """Render the enclosed rows with ``hosts`` allowed, in ``workspace``.

    ``workspace`` is the stored workspace key the hosts came from; it is only
    written into the use audit and changes no decision. ``epoch`` is
    :func:`allowed_host_audit_epoch` read before ``hosts`` was; a use audit from a
    scope whose epoch a revoke has since passed is not remembered.
    """
    token = _SCOPED_HOSTS.set(frozenset(h.lower() for h in hosts))
    ws_token = _SCOPED_WORKSPACE.set(workspace or "")
    epoch_mark = _SCOPED_EPOCH.set(_allowed_host_audit_epoch if epoch is None else epoch)
    try:
        yield
    finally:
        _SCOPED_EPOCH.reset(epoch_mark)
        _SCOPED_WORKSPACE.reset(ws_token)
        _SCOPED_HOSTS.reset(token)


def current_allowed_link_hosts() -> frozenset[str]:
    """The hosts :func:`allowed_link_hosts` has in effect here, for cache keys."""
    return _SCOPED_HOSTS.get()


def current_allowed_link_workspace() -> str:
    """The workspace :func:`allowed_link_hosts` has in effect here, for cache keys."""
    return _SCOPED_WORKSPACE.get()


def current_allowed_link_epoch() -> int:
    """The revoke epoch :func:`allowed_link_hosts` took with its hosts, for cache keys."""
    scoped = _SCOPED_EPOCH.get()
    return _allowed_host_audit_epoch if scoped is None else scoped


def blocked_link_context() -> tuple[tuple[str, ...], tuple[str, ...], str, int]:
    """Everything outside the text that :func:`scoped_blocked_links` reads, for cache keys.

    The platform's exempt hosts (they move mid-process: a companion loads after boot,
    a policy tightens), the hosts the render's scope allows, its workspace and its
    revoke epoch. A memo of rendered rows keyed without them would keep serving a link
    as blocked after its host was exempted or allowed, or as live after either ended.
    """
    return (
        tuple(sorted(_exfil_exempt_hosts())),
        tuple(sorted(_SCOPED_HOSTS.get())),
        _SCOPED_WORKSPACE.get(),
        current_allowed_link_epoch(),
    )


def allowed_host_audit_epoch() -> int:
    """How many revokes this process has seen, for render memo keys."""
    return _allowed_host_audit_epoch


def forget_allowed_host_use(host: str) -> None:
    """Drop ``host`` from the use memo in every workspace (called on revoke)."""
    global _allowed_host_audit_epoch
    with _ALLOWED_HOST_AUDIT_LOCK:
        for key in [k for k in _ALLOWED_HOST_AUDITED if k[1] == host]:
            _ALLOWED_HOST_AUDITED.discard(key)
        _allowed_host_audit_epoch += 1


def _emit_allowed_host_used_event(host: str, rules: set[str]) -> None:
    """SEL-audit that a reader-allowed host let a flagged link render live."""
    workspace = _SCOPED_WORKSPACE.get()
    key = (workspace, host)
    with _ALLOWED_HOST_AUDIT_LOCK:
        if key in _ALLOWED_HOST_AUDITED:
            return
        # A snapshot taken before a revoke still shows its links, so the use is
        # audited, but remembering it would hide the first use after a re-allow
        # that the revoke's forget is there to expose.
        scoped = _SCOPED_EPOCH.get()
        fresh = scoped is None or scoped == _allowed_host_audit_epoch
        if fresh and len(_ALLOWED_HOST_AUDITED) < _ALLOWED_HOST_AUDITED_MAX:
            _ALLOWED_HOST_AUDITED.add(key)
    meta = {
        "workspace": workspace,
        "host": host,
        "rules": sorted(rules),
        "mechanism": "REDACTION_ALLOWED_HOST",
    }
    _log_operator_list_use("redaction_allowed_host_used", "blocked_links", host, lambda: meta)


def scoped_blocked_links(text: str) -> list[dict]:
    """:func:`blocked_links` under the hosts :func:`allowed_link_hosts` has in effect.

    Audits each allowed host that let a flagged link render live (once per
    workspace and host per process). A failed audit write never changes the records.
    """
    hosts = _SCOPED_HOSTS.get()
    if not hosts:
        return blocked_links(text)
    relaxed: dict[str, set[str]] = {}
    records = blocked_links(text, allowed_hosts=hosts, relaxed_out=relaxed)
    for host, rules in relaxed.items():
        _emit_allowed_host_used_event(host, rules)
    return records


#: What app egress writes in place of a link the dashboard blocks: the placeholder
#: builds before the blocked-link chip wrote everywhere.
SUSPICIOUS_URL_TAG = "[REDACTED: suspicious URL to {domain}]"


def replace_blocked_links(text: str) -> tuple[str, int]:
    """*text* with each link the dashboard draws as a Blocked link chip replaced whole.

    For app egress (``ctx.scrub.outbound``): an app renders its own page, which has no
    chip, so a link the chip would hold back is written as :data:`SUSPICIOUS_URL_TAG`
    instead. The decision is :func:`blocked_links` itself under the hosts in effect
    here: a render scope's (:func:`allowed_link_hosts`), else the owner's allowed hosts
    for the default workspace; an allowed host relaxes exactly what it relaxes for the
    chip, and its use is audited the same way. Returns ``(text, links replaced)``.
    """
    if not text or "://" not in text:
        return text, 0
    if _SCOPED_HOSTS.get():
        return _replace_scoped(text)
    from .redaction_allow import allowed_hosts_for, normalize_workspace

    with allowed_link_hosts(allowed_hosts_for(None), normalize_workspace(None)):
        return _replace_scoped(text)


def _replace_scoped(text: str) -> tuple[str, int]:
    relaxed: dict[str, set[str]] = {}
    out: list[str] = []
    cursor = 0
    count = 0
    for start, flagged, _rules in _blocked(text, _SCOPED_HOSTS.get(), relaxed):
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(SUSPICIOUS_URL_TAG.format(domain=flagged[0].group(1).lower()))
        cursor = start + len(flagged[1])
        count += 1
    for host, rules in relaxed.items():
        _emit_allowed_host_used_event(host, rules)
    if not count:
        return text, 0
    out.append(text[cursor:])
    return "".join(out), count


#: The placeholder builds before the chip wrote in place of a suspicious URL, and
#: the records they kept beside it (``meta.blocked_links``) with its address.
_LEGACY_PLACEHOLDER_RE = re.compile(
    r"\[REDACTED: suspicious URL to ([a-z0-9.-]{1,253})\]", re.IGNORECASE
)
_LEGACY_RECORDS_MAX = 64
_LEGACY_URL_MAX = 8192


def _legacy_url(record: object) -> tuple[str, str] | None:
    """``(domain, url)`` of one record an older build kept, when it names its own host."""
    from urllib.parse import urlsplit

    if not isinstance(record, dict):
        return None
    domain, url = record.get("domain"), record.get("url")
    if not isinstance(domain, str) or not isinstance(url, str) or not url:
        return None
    if len(url) > _LEGACY_URL_MAX or any(ch.isspace() or ord(ch) < 32 for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.hostname.lower() != domain.lower():
        return None
    return domain.lower(), url


def restore_legacy_blocked_links(text: str, holder: object) -> str:
    """*text* with each placeholder an older build wrote for a blocked link put back
    as the address its records kept, for the emitted copy only.

    The current decisions then apply to the address as they do to any reply: the
    Blocked link chip, an allowed host, the credential masks. Pairs by host and
    order, as the records were written in first-appearance order: a host is put
    back only when its placeholders and its valid records are equal in number and
    either the row vouched for that order (``blocked_links_ordered``) or every one
    of its records names the same address. Anything else leaves that host's
    placeholders as written. The saved row is never rewritten.
    """
    if not isinstance(text, str) or "[REDACTED: suspicious URL to " not in text:
        return text
    if not isinstance(holder, dict) or not isinstance(holder.get("blocked_links"), list):
        return text
    by_domain: dict[str, list[str]] = {}
    for record in holder["blocked_links"][:_LEGACY_RECORDS_MAX]:
        pair = _legacy_url(record)
        if pair is not None:
            by_domain.setdefault(pair[0], []).append(pair[1])
    matches = list(_LEGACY_PLACEHOLDER_RE.finditer(text))
    counts: dict[str, int] = {}
    for m in matches:
        counts[m.group(1).lower()] = counts.get(m.group(1).lower(), 0) + 1
    ordered = holder.get("blocked_links_ordered") is True
    hosts = {
        d
        for d, urls in by_domain.items()
        if counts.get(d, 0) == len(urls) and (ordered or len(set(urls)) == 1)
    }
    if not hosts:
        return text
    out: list[str] = []
    taken: dict[str, int] = {}
    last = 0
    for m in matches:
        d = m.group(1).lower()
        if d not in hosts:
            continue
        out += [text[last : m.start()], by_domain[d][taken.get(d, 0)]]
        taken[d] = taken.get(d, 0) + 1
        last = m.end()
    return "".join(out) + text[last:]
