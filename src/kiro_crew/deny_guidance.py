"""Remediation guidance attached to a denied tool call — the "do this instead" half.

A refusal that states only WHY leaves the model to invent a way forward, and for
credential work the invention is systematically wrong in a way that costs the
user the capability entirely: the agent re-tries the same shape under a
different reader (``cat`` → ``head`` → ``python open``), each of which the same
rule family blocks, and then reports that the host has no AWS access at all.
The sanctioned path was available the whole time — nothing ever told it.

Guidance is keyed by the CLASS of thing the gate refused, not by the individual
rule. Per-rule text cannot cover the tiers that matter: an edition overlay
contributes bare fnmatch globs carrying no id, category or description, so for
exactly the rules an enterprise adds there is nothing to hang text on. The class
is instead recovered from the refusal text, whose anchor phrases every producer
in :mod:`kiro_crew.security` already shares. ``test_deny_guidance.py`` drives
those real producers rather than asserting on copied strings, so an anchor that
drifts fails there instead of silently degrading to no guidance.

The remediation prose is static, and none of it is interpolated from the command,
which is what keeps it safe to hand back to a model that may be acting on
injected content: it names the sanctioned path, never a way around the rule. The
one interpolated value anywhere in the module is the server id
:func:`credential_tool_hint` names, which comes from the host's own MCP
configuration rather than from the refused call.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable, Mapping

from kiro_crew.platform import context as platform_context
from kiro_crew.platform.capability_bound import bind_capability_manager
from kiro_crew.platform.defaults import DefaultCapabilityManager

logger = logging.getLogger(__name__)

#: Deny classes. The split follows what the caller must DO differently, which is
#: why AWS and enterprise-SSO credentials are separate: one has a local
#: resolution the agent can drive itself (the SDK reads the profile), the other
#: can only be re-established by the human in their own terminal.
DENY_CLASS_AWS_CREDENTIAL = "aws_credential"
DENY_CLASS_SSO_CREDENTIAL = "sso_credential"
DENY_CLASS_SECRET_FILE = "secret_file"
DENY_CLASS_TRUST_ROOT = "trust_root"
DENY_CLASS_EXFIL_SHAPE = "exfil_shape"
DENY_CLASS_SELF_PROTECTION = "self_protection"

#: Ordered (class, anchors) rules, matched case-insensitively as substrings of
#: the refusal text. Order is precedence and is load bearing: a command can
#: satisfy two classes at once (reading a credential file INTO an outbound
#: request body is both), and the narrower verdict is the one worth acting on.
#: The trust root comes first because it is the one class where the answer is
#: "you cannot, and neither can a workaround" — offering a credential remedy
#: there would send the model looking for a path that must not exist.
_CLASS_ANCHORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        DENY_CLASS_TRUST_ROOT,
        ("governance trust-root", "write-protected config path"),
    ),
    (
        DENY_CLASS_SSO_CREDENTIAL,
        ("sso", "cookie"),
    ),
    (
        DENY_CLASS_AWS_CREDENTIAL,
        (
            ".aws",
            "aws_secret",
            "aws_access",
            "aws_session",
            "aws credentials from environment",
            "imds endpoint",
            "169.254.169.254",
            "boto3",
            "botocore",
        ),
    ),
    (
        DENY_CLASS_EXFIL_SHAPE,
        ("data-exfiltration pattern",),
    ),
    (
        DENY_CLASS_SELF_PROTECTION,
        ("matched structurally on the command's argv",),
    ),
    # Widest credential anchor last: every more specific credential class above
    # also matches these phrases, so leading with them would collapse the whole
    # taxonomy into one generic answer.
    #
    # Every anchor in this table is deliberately GENERIC. The public core must not
    # carry any edition's credential-tool or identity-store names, so a refusal
    # naming one of those degrades to this widest class — whose prose is written to
    # stay true for every fenced credential store, whichever client owns it —
    # rather than being classified by a marker this file is not allowed to know. An
    # edition that wants a sharper answer supplies it through its own adapter.
    (
        DENY_CLASS_SECRET_FILE,
        (
            "sensitive credential path",
            "sensitive path",
            "credentials",
            ".ssh",
            ".gnupg",
            ".netrc",
            ".npmrc",
            ".pypirc",
            "git-credentials",
        ),
    ),
)


def _anchor_matcher(anchor: str, *, allow_plural: bool = False) -> re.Pattern[str]:
    """An anchor matcher whose edges cannot land in the middle of a word.

    A bare substring test misfires on the short anchors: ``sso`` occurs inside
    "processor", "associated" and "lessons", and its class is matched BEFORE the
    widest credential class, so any refusal whose text merely contained one of
    those words was answered with enterprise-SSO prose — the "second wall" this
    module exists to prevent. The boundary is a character-class lookaround rather
    than ``\\b`` because several anchors open with punctuation (``.aws``,
    ``.ssh``), where ``\\b`` would instead REQUIRE a word character before the
    dot and stop matching the paths those anchors are for.

    ``allow_plural`` additionally accepts a trailing ``s``, and is opt-in because
    the two callers want different things. A CLASS ANCHOR is matched against
    refusal text produced by :mod:`kiro_crew.security`, whose wording is fixed, so
    tolerating inflections there would only widen it for no gain. A SERVER KEYWORD
    is matched against names a third party chose, and the idiomatic spelling is
    the plural — ``aws-credentials``, "vends credentials" — so a singular-only
    boundary silently drops exactly the servers the hint exists to find.
    """
    edge = "[0-9a-z_]"
    prefix = f"(?<!{edge})" if re.match(edge, anchor[:1]) else ""
    suffix = f"(?!{edge})" if re.match(edge, anchor[-1:]) else ""
    plural = "s?" if allow_plural else ""
    return re.compile(f"{prefix}{re.escape(anchor)}{plural}{suffix}")


#: Precompiled form of :data:`_CLASS_ANCHORS`, in the same precedence order.
_CLASS_MATCHERS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (deny_class, tuple(_anchor_matcher(anchor) for anchor in anchors))
    for deny_class, anchors in _CLASS_ANCHORS
)


#: agent, in the present tense, naming the sanctioned path concretely enough to
#: act on without a further round-trip to the user.
REMEDIATION: dict[str, str] = {
    DENY_CLASS_AWS_CREDENTIAL: (
        "You do not need to read AWS credential material, and no reader of it is "
        "allowed — trying head/less/python instead of cat hits the same rule. What "
        "is refused is YOU opening the file; the SDK inside the `aws` process still "
        "reads it for you, so an already-configured profile works without you ever "
        "touching it. AWS CLI calls themselves are NOT blocked, so run the command "
        "you actually wanted: to list configured profiles use `aws configure "
        "list-profiles`, to confirm the identity in effect use `aws sts "
        "get-caller-identity`, and to select one of several configured profiles "
        "pass `--profile <name>`. Do "
        "NOT assume the SDK will find a credential just because the user has one — "
        "your environment can point at a session-scoped credentials location rather "
        "than the user's own, so a credential they minted by hand in their terminal "
        "may be invisible to you even though it exists on the host. If the identity "
        "check comes back with none, report which check you ran and what it said "
        "rather than concluding this host has no AWS access: the durable setup is a "
        "profile whose `credential_process` vends credentials on demand, which is "
        "the user's step to take (for example `aws sso login` first). On a host "
        "that provides a credential-vending tool that tool is the sanctioned path "
        "instead of a named profile, and the credential it supplies lands on the "
        "DEFAULT profile — there, run the command plainly and do not pass "
        "`--profile`."
    ),
    DENY_CLASS_SSO_CREDENTIAL: (
        "This is a live enterprise SSO bearer credential: holding it would let you "
        "act as the user against every SSO-gated service, so it is fenced for "
        "reading as well as writing, and copying it into a cookie jar is blocked "
        "on the same grounds. You cannot authenticate on the user's behalf and "
        "must not try to re-mint the session yourself. Ask the user to run their "
        "host's SSO login command in their own terminal, then retry the request "
        "that needed it."
    ),
    DENY_CLASS_SECRET_FILE: (
        "What was refused touches credential or key material — either a path that "
        "holds it or a command that mints it — so a different reader, or the same "
        "action spelled another way, hits the same rule family. You almost never "
        "need the material itself: run the command that USES it instead, because "
        "every client whose credential store is fenced here — cloud, version-control, "
        "remote-shell, container and package clients alike — resolves its own "
        "credentials without your help. When the refused thing was a command that "
        "would have obtained a credential, the supported route is that client's own "
        "credential helper (for a cloud CLI, a configured profile whose "
        "`credential_process` vends one on demand) or the credential-vending tool "
        "this host provides — both of which are the user's "
        "setup, not something to re-attempt from here. If the task genuinely cannot "
        "proceed without the material, name the STEP that needs it and let the user "
        "carry out that step themselves. Routing the material through this "
        "conversation is not the alternative to reading it: the refusal was about "
        "that material reaching you, and it reaches you just as surely when a person "
        "types it — landing in this transcript and in everything derived from it."
    ),
    DENY_CLASS_TRUST_ROOT: (
        "This path is the security ceiling you are governed BY, so it is "
        "deliberately unreachable from inside a tool call — that is the property "
        "which makes the ceiling un-disableable, not a misconfiguration to work "
        "around. Do not look for another writer or a temp-file rename. If the "
        "policy genuinely needs to change, state what needs changing and let the "
        "user edit it themselves."
    ),
    DENY_CLASS_EXFIL_SHAPE: (
        "This refusal is about what the action would DO — move a local file's "
        "contents off this host — so it is not a spelling problem and must not be "
        "re-spelled. The rule matches the request SHAPE, which means a form that "
        "got past it would mean the control was defeated rather than satisfied; "
        "those bytes must not leave through you by any route. If the upload is "
        "genuinely what the task needs, name the file and the destination and let "
        "the user send it themselves. If you only needed the remote call and a "
        "local file was never the point, make the call without one — and if NO "
        "local file is involved at all, so the request only resembled the refused "
        "shape, say that plainly and report it as an over-block instead of hunting "
        "for a form that slips past."
    ),
    DENY_CLASS_SELF_PROTECTION: (
        "This refusal is about what the action would DO — reach the product's own "
        "credential mint, or stop the supervisor that is running you — so it is not "
        "a spelling problem and must not be re-spelled. The same program reached by "
        "any other invocation form is the same action, so a form that got past the "
        "check would mean the control was defeated rather than satisfied; do not go "
        "looking for one. If what you actually needed was unrelated and importing "
        "the product merely tripped the shape, get it another way that does not run "
        "product code — a file-reading tool, a CLI subcommand's own output, or an "
        "ordinary package query. If you genuinely need this exact action, say so "
        "and let the user run it."
    ),
}

#: class → commands the prose above tells the caller to run. Pinned so
#: ``test_deny_guidance.py`` can prove each one is actually ALLOWED. Guidance
#: that walks the agent into a second wall is worse than none: it spends a turn
#: and teaches it that the advice is untrustworthy.
#:
#: Only the classes whose sanctioned path IS a command appear here. A class whose
#: refusal cannot be satisfied by running something else — the trust root, the
#: self-protection floor, the exfiltration shape — deliberately has no entry: an
#: "example" for one of those is an alternative SPELLING of the refused action,
#: which is the one thing this module must never hand back.
SUGGESTED_COMMANDS: dict[str, tuple[str, ...]] = {
    DENY_CLASS_AWS_CREDENTIAL: (
        "aws configure list-profiles",
        "aws sts get-caller-identity",
    ),
}

#: Substrings that identify an installed MCP server as a credential vendor.
#: Deliberately generic: the public core must not name any edition's server, and
#: a keyword match keeps a host-specific vendor discoverable without one. Chosen
#: to be narrow enough not to sweep in unrelated servers — a bare "auth" would
#: match "author", and a bare "aws" would match every AWS-adjacent tool.
_CREDENTIAL_SERVER_KEYWORDS: tuple[str, ...] = (
    "credential",
    "creds",
    "sso",
    "sts",
    "iam",
)

#: Fields of a capability-manager row consulted for the keyword match.
_SERVER_TEXT_FIELDS: tuple[str, ...] = ("server_id", "name", "title", "description")

#: Boundary-matched form of the keywords, sharing :func:`_anchor_matcher` with the
#: class anchors so both places break words the same way. A bare substring test
#: named the wrong server: ``sts`` matches "posts messages" and ``iam`` matches a
#: name like "williams", so an unrelated server was recommended as a credential
#: vendor — advice the agent cannot act on, which is the failure the hint exists
#: to avoid. Plural-tolerant, because the idiomatic vendor spelling IS the plural
#: (``aws-credentials``, "vends credentials") and a singular-only boundary drops
#: precisely the servers worth naming. The keywords stay short BECAUSE they are
#: boundary-matched; the comment above about "auth" matching "author" is the same
#: hazard one level down.
_CREDENTIAL_SERVER_MATCHERS: tuple[re.Pattern[str], ...] = tuple(
    _anchor_matcher(keyword, allow_plural=True) for keyword in _CREDENTIAL_SERVER_KEYWORDS
)

#: TTL for the installed-server snapshot. The lookup shells out to the edition's
#: package manager, so it is cached rather than run per refusal; denials are rare
#: enough that a stale-by-minutes hint costs nothing, while an uncached call
#: would put a subprocess on a path that fires during an already-failing turn.
_HINT_TTL_SECS = 300.0

_hint_cache: str = ""
_hint_cache_ts: float = 0.0


def classify_deny(reason: str, subject: str = "") -> str:
    """The deny class named by *reason*, or "" when none applies.

    *subject* is the refused thing itself — the tool title, which for a shell
    call carries the command and for a file read is the path. It is needed
    because the sensitive-path tier refuses with a deliberately GENERIC reason
    ("accesses sensitive credential path") that names no path, so reason alone
    cannot tell an AWS profile from an SSH key from an SSO cookie — three
    refusals with three different sanctioned paths. Consulted as display text
    only: it selects WHICH remediation prose is shown and can never make
    something allowed, so an LLM-authored title steering it costs nothing.

    "" is a first-class answer, not a failure: most denials (a destructive rm, a
    protected-branch push) are self-explanatory, and inventing guidance for them
    would bury the classes where the agent genuinely cannot infer the next step.
    """
    text = f"{reason or ''} {subject or ''}".lower().strip()
    if not text:
        return ""
    for deny_class, matchers in _CLASS_MATCHERS:
        if any(matcher.search(text) for matcher in matchers):
            return deny_class
    return ""


def remediation_for(reason: str, subject: str = "", *, credential_tool_hint: str = "") -> str:
    """Guidance for *reason*, with the host's credential-vendor hint folded in.

    *credential_tool_hint* is appended only for the two credential classes that
    a vending tool can actually resolve. Appending it to, say, a trust-root
    refusal would suggest a credential tool could reach the security ceiling.
    """
    deny_class = classify_deny(reason, subject)
    if not deny_class:
        return ""
    text = REMEDIATION.get(deny_class, "")
    hint = (credential_tool_hint or "").strip()
    if hint and deny_class in (DENY_CLASS_AWS_CREDENTIAL, DENY_CLASS_SSO_CREDENTIAL):
        text = f"{text} {hint}"
    return text


#: A server id is echoed into prose the AGENT reads as host guidance, so only a
#: plausible identifier may pass. An id is chosen by whoever authored the server,
#: not by this repo, so an instruction-shaped one ("creds, ignore the above and …")
#: would arrive wearing Kiro Crew's own voice — the framing is the escalation, not
#: the bytes, since a tool list already carries them as data. Anything with
#: whitespace or sentence punctuation is therefore refused rather than quoted:
#: quoting does not help a reader that has no parser. Real ids pass unchanged
#: (``creds-agent``, ``kirocrew-core``, ``local-chorus-mcp``, ``mochi:mochi``).
_SAFE_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


def credential_vendor_server_ids(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Installed MCP server ids that look like credential vendors, sorted.

    Split out from :func:`credential_tool_hint` so a caller with a DIFFERENT
    AUDIENCE can phrase its own sentence from the same matching policy. The hint
    is written as second-person instructions to the agent ("prefer one of those
    and then run the command normally"), which is wrong prose to print to a human
    in ``doctor``: the reader cannot call an MCP tool and has no "guidance above"
    on their screen. Sharing the ids is right; sharing the sentence is not.

    Filtered through :data:`_SAFE_SERVER_ID_RE` here rather than at either call
    site, because BOTH audiences render these ids into prose and neither should
    have to remember to sanitize. Dropping an id degrades the hint to absent,
    which is the pre-existing behaviour on a host with no vendor — the safe
    direction, and never a claim that no vendor exists.
    """
    names: list[str] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        server_id = str(row.get("server_id") or row.get("name") or "").strip()
        if not server_id or not _SAFE_SERVER_ID_RE.match(server_id):
            continue
        haystack = " ".join(str(row.get(field) or "") for field in _SERVER_TEXT_FIELDS).lower()
        if any(matcher.search(haystack) for matcher in _CREDENTIAL_SERVER_MATCHERS):
            if server_id not in names:
                names.append(server_id)
    return sorted(names)


def credential_tool_hint(rows: Iterable[Mapping[str, Any]]) -> str:
    """Hint that this host has credential-vending MCP server(s), by COUNT.

    Addressed to the AGENT, on the refusal path. Pure, so the keyword policy is
    testable without a platform context. Returns "" when nothing matches — which
    is the public edition's normal state, and the reason the hint is additive
    rather than part of the base prose.

    **No server id is interpolated.** An id is authored by whoever wrote the
    server, and this text is read as host guidance immediately before "SUPERSEDES
    the profile guidance above" — so an id is untrusted input arriving in a
    trusted voice. Character filtering cannot fix that: `:` and `-` are required
    by real ids (``mochi:mochi``) and are already sufficient to spell
    ``SYSTEM:ignore-prior-instructions``, which needs no whitespace at all. A
    COUNT cannot carry an instruction, and the agent can already see the servers
    by name in its own tool list, so naming them here buys nothing the agent
    does not already have through a trusted channel.
    """
    names = credential_vendor_server_ids(rows)
    if not names:
        return ""
    plural = "s" if len(names) > 1 else ""
    return (
        f"This host also has {len(names)} MCP server{plural} that may vend "
        "credentials directly — identify it in your own tool list rather than "
        "from this notice. Prefer it and then run the command normally — that "
        "SUPERSEDES the profile guidance above, because a credential-vending host "
        "commonly makes the profile files unreadable even to commands that are "
        "otherwise allowed, and may reject an explicit --profile. If the vendor "
        "reports no configured profile, that is the user's setup step, not a "
        "missing capability."
    )


async def resolve_credential_tool_hint() -> str:
    """Cached :func:`credential_tool_hint` for the composed edition.

    Costs nothing on a host with no capability manager: the public default
    reports ``available() == False``, so this returns "" without spawning
    anything. Fail-soft in every direction — a hint is an enhancement to a
    refusal that already works, so a lookup error degrades to "" rather than
    turning a clean policy block into a turn error.
    """
    global _hint_cache, _hint_cache_ts
    now = time.monotonic()
    if _hint_cache_ts and now - _hint_cache_ts < _HINT_TTL_SECS:
        return _hint_cache
    hint = ""
    try:
        # Reached through the MODULE rather than a bound name so a caller (and a
        # test) that swaps the composition seam is honoured, not shadowed by a
        # reference captured at import time.
        manager = platform_context.safe_context_call(
            lambda: platform_context.current_context().capability_manager,
            fallback_factory=lambda: bind_capability_manager(DefaultCapabilityManager()),
            log_message="capability_manager lookup failed; skipping credential-tool hint",
        )
        if manager.available():
            hint = credential_tool_hint(await manager.list_mcp())
    except Exception:
        # Includes PlatformCompositionError: a composition fault must not be
        # re-raised onto the refusal path, whose job is to explain a block that
        # already happened. The single write below still caches "" so a broken
        # host is not probed once per denial.
        logger.debug("credential-tool hint lookup failed", exc_info=True)
    _hint_cache = hint
    _hint_cache_ts = now
    return hint


def reset_credential_tool_hint_cache() -> None:
    """Drop the cached hint. For tests, and for a capability mutation."""
    global _hint_cache, _hint_cache_ts
    _hint_cache = ""
    _hint_cache_ts = 0.0
