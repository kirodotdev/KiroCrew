"""Gateway-owned push verdicts: evidence the push guard ran, held where the agent cannot reach it.

WHAT THIS IS
------------
The prepare-pr skill's pre-push stale-base guard answers one question -- is this branch
being published on a base that has moved -- and a verdict that is only printed persists
nothing, so a publish whose gate was skipped is byte-identical to one whose gate passed.

The evidence lives HERE, in the gateway process, and nothing outside this process writes
it. The agent asks the gateway to run the guard; the gateway runs it, records what IT
observed, and the ``git push`` allow path in the shell-command floor reads that record.

WHY THE GATEWAY IS THE WRITER
-----------------------------
Any store the agent can write is evidence the gated party controls, and signing does not
help when the key is as readable as the file. So the writer is the one party the agent
cannot impersonate: the gateway itself. ``record`` is called from the gateway's own route
handler, which runs in this process; the MCP tool the agent calls is a separate process
that reaches that route over loopback and carries a request, never a result.

WHY THE GATE TOUCHES NO FILESYSTEM
----------------------------------
The publish floor runs inside the permission gate, where a slow mount is a stall of every
task in the process. So the floor reads only this module's in-process activation reading and
nothing else: no receipt file, no policy file, no ref read, no subprocess, no network.

That is affordable because on an activated install the agent never holds publish authority
at all: the gateway judges the commit and does the push itself, so an agent-visible ``git
push`` has no legitimate reason to run and the floor refuses every one of them outright. The
floor needs no receipt to match and no tree-binding to resolve -- one flat refusal under
activation removes the authority this gate exists to remove.

WHAT A VERDICT IS KEYED ON
--------------------------
The CALLING SESSION, never a worktree named in a request body -- the contract
``dashboard/handlers/session_ledger.py`` states for its own routes. A session cannot ask
for a verdict about one worktree and then publish from another, because it never names the
worktree: its own identity selects the record. There is no enrolled list, so there is also
no over-refusal of a repository nobody asked about.

ACTIVATION IS THE OPERATOR'S, AND IT IS TRUSTED
-----------------------------------------------
``activation_enabled`` reads a leaf on the protected keystone -- the one placement where the
agent's file tools can neither read nor write it (it is write-protected and file-tool
unreadable; the read-only sandbox mount still lets a shell read the bytes), the same floor the
computer-use primary enable sits on
and for the same reason. ``config.json`` may REQUEST activation; only the keystone
AUTHORIZES it, so an agent with file-write access cannot enable or disable this gate. It is
off until an operator turns it on out of band, so an installation that never activates it
is judged exactly as it is on main.

Once activated, the check runs in the UNCONDITIONAL git-publish path rather than behind the
opt-out tiers, because an opt-out key would hand the gated party its own bypass.

Activation is on disk and survives a restart. Verdicts are the opposite, in-process only: a
restart costs one guard re-run rather than leaving a stale pass behind.

A git ``pre-push`` hook is deliberately NOT the mechanism: the gateway pins
``core.hooksPath=/dev/null`` so a repository-planted hook cannot execute host-side.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Label the refusal reports, mirroring the other publish-floor tags.
DENY_LABEL = "prepare-pr push verdict"

#: Named in every refusal so an operator reads what to do, not merely what failed. The
#: tool name rather than a command line: the interpreter and the skill's location differ
#: per install, and a command that does not run is worse than a name.
GATE_TOOL = "the prepare-pr push guard tool"

#: How long a verdict stays usable. A bound rather than a freshness proxy: the verdict is
#: invalidated by OBSERVED mutation, so this only caps how long an untouched session may
#: sit between its guard run and its push.
MAX_AGE_SECONDS = 60 * 60


@dataclass(frozen=True)
class Verdict:
    """What the gateway observed when it ran the guard for one session.

    Every field is part of WHAT WAS JUDGED, not decoration. A verdict that recorded only its
    own existence authorized any publish from the session that earned it: a different branch,
    a different remote, a different destination ref. Each field below is something a publish
    can name, so each one is something the floor can hold the publish to.
    """

    gitdir: str
    worktree: str
    head: str
    base: str
    base_sha: str
    remote: str
    source_ref: str
    recorded_at: float


_LOCK = threading.Lock()
_VERDICTS: dict[str, Verdict] = {}


def record(
    session_key: str,
    *,
    gitdir: str,
    worktree: str,
    head: str,
    base: str,
    base_sha: str,
    remote: str,
    source_ref: str,
) -> None:
    """Record the guard's pass for *session_key*. Called ONLY by the gateway's own route.

    There is deliberately no path from an agent-supplied payload to this function: the
    route computes every field from the run it performed itself.

    Every field is REQUIRED with no default. A default here would be the quiet way this gate
    loses its binding: a caller that forgot the remote would record a verdict that matches
    every remote, and nothing would fail.
    """
    if not session_key:
        raise ValueError("a verdict must belong to a session")
    with _LOCK:
        _VERDICTS[session_key] = Verdict(
            gitdir=gitdir,
            worktree=worktree,
            head=head,
            base=base,
            base_sha=base_sha,
            remote=remote,
            source_ref=source_ref,
            recorded_at=time.time(),
        )


def verdict_for(session_key: str) -> Verdict | None:
    """The live verdict for *session_key*, or ``None`` when there is none to read.

    Expiry is applied here rather than by a sweeper so a reader can never see a verdict
    the writer would consider stale.
    """
    if not session_key:
        return None
    with _LOCK:
        found = _VERDICTS.get(session_key)
        if found is None:
            return None
        if time.time() - found.recorded_at > MAX_AGE_SECONDS:
            del _VERDICTS[session_key]
            return None
        return found


def invalidate(session_key: str) -> None:
    """Drop *session_key*'s verdict. Idempotent: absence is the desired end state."""
    if not session_key:
        return
    with _LOCK:
        _VERDICTS.pop(session_key, None)


#: The keystone leaf holding activation: a FLAT name at the crew home's root, with the same
#: placement and the same protection as ``computer_use.json``. It is listed in
#: ``security.paths._CREW_SECRET_LEAVES`` for read+write protection on the tool path, and in
#: ``sandbox._CREW_READONLY_LEAVES``, ``sandbox._CREW_CHILD_WITHHELD_LEAVES`` and
#: ``sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES`` so the OS sandbox mounts it read-only for
#: the shell. THOSE ENTRIES are what make this a trusted opt-in rather than a preference: the
#: claim "the agent cannot write it" is a claim about those four tables, and without them the
#: gated party writes its own off switch.
#:
#: Flat rather than a ``security/`` subpath because every one of those tables keys on a
#: crew-home LEAF; a subpath would need a new protected directory instead of an existing,
#: tested shape.
class ActivationUnreadable(RuntimeError):
    """The activation leaf exists but could not be read or parsed.

    Distinct from absence on purpose. Absence means nobody activated gating and is answered
    with a plain false; this means an operator's activation state is UNKNOWN, and answering
    false there would let one corrupted file silently disable the gate on an installation
    that had turned it on. The floor turns this into a publish refusal.
    """


ACTIVATION_LEAF = "push-verdict-activation.json"

#: Where the gateway keeps its OWN copy of the commits it judges: one bare repository per
#: repository identity, under the crew home.
#:
#: It exists because the worktree must never be written. The gateway fetches the base from the
#: remote and the candidate out of the worktree into this repository, and runs the guard HERE,
#: so the tree being judged is only ever read. A fetch into the worktree would both modify the
#: state being judged and fail outright once that tree is read-only.
#:
#: It gets the SAME protection as ``ACTIVATION_LEAF`` and for a sharper reason: a mirror the
#: agent can write is a mirror in which the agent plants its own base commit, and then the
#: ancestry check passes against a base of the agent's choosing. That is the agent-writable
#: evidence problem again, one layer further out, so the mirror is listed read+write protected
#: on the tool path and read-only for the shell in every sandbox disposition. The gateway
#: writes it directly, not through the sandbox, exactly as the settings PUT writes the
#: computer-use enable.
#:
#: Being a DIRECTORY it takes the directory dispositions, which is not a detail: a fence seals
#: an EXISTING path only, and on a fresh activated install no judgement has run yet, so the
#: mirror root is absent, the read-only mount is skipped, and the name the agent would create
#: is exactly the one the listing exists to deny. ``sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES``
#: materialises it before any sandbox starts and ``_CREW_NOFOLLOW_READONLY_DIR_LEAVES`` keeps its
#: name unreplaceable. It is deliberately absent from the FILE precreate list, which would
#: materialise an empty file at the mirror root's own path and make the first ``mkdir`` raise.
MIRROR_DIR = "push-verdict-mirrors"


@dataclass(frozen=True)
class Activation:
    """What the keystone says about this installation's gating.

    Three facts, and they are pinned in the SAME operator-written file on purpose. The digest
    is what anchors the guard's bytes outside the repository: an installation tree can be the
    very checkout the agent edits, so no digest computed from that tree can vouch for it. Only
    a value an operator wrote where the agent cannot write can.

    ``pinned_push_url`` anchors the DESTINATION the same way the digest anchors the guard's
    bytes. The push destination is resolved from the agent-writable worktree git config, so an
    ordinary ``git remote set-url`` performed before the request points the credential-exempt
    gateway publish at an attacker-chosen same-history repository -- and the re-resolution at
    publish time only compares the freshly-resolved target to itself, catching a change AFTER
    judging rather than an agent-chosen destination that was already in place. The one value
    the agent cannot write is one an operator pinned here, so a pinned destination is the
    binding that closes "agent picks the destination": empty when the operator pinned none,
    in which case the destination is unconstrained exactly as it is on an install that never
    activated.
    """

    enabled: bool
    guard_sha256: str
    pinned_push_url: str = ""


def activation() -> Activation:
    """What an operator has activated on this installation, read from the keystone.

    Re-read per call, with no cache, which is a DELIBERATE choice pinned by
    ``test_the_activation_read_is_live_on_every_publish``: a stat-keyed cache of the parse
    cannot tell two writes of the same byte length within one clock tick apart, and the value
    it would serve is an ENABLE decision, so the failure mode is the gate reading as off after
    an operator turned it on. The cost -- one ``open`` + JSON parse of a tiny leaf -- is paid to
    keep that correctness, and the one caller on the event loop (``hooks.on_tool_call``) reaches
    it only on an installation whose operator ACTIVATED gating.

    On disk rather than in memory, so activation SURVIVES A RESTART: a gateway that comes
    back up is still gating, and an operator does not silently lose a control they turned
    on. The verdicts themselves are deliberately the opposite -- in-process only, so a
    restart costs one guard re-run and never leaves a stale pass behind.

    Read by opening the keystone path directly in the gateway, which is the keystone-reader
    pattern the other protected leaves use. ``config.json`` is not consulted here at all:
    an operator may REQUEST activation from config, but only the keystone AUTHORIZES it, and
    the same split applies to the digest -- a digest offered in config is not read, so an
    agent that writes config can neither turn this gate on nor change which guard bytes it
    will accept.

    Absence and unreadability are DIFFERENT facts and get different answers. Absence is
    the honest answer for an installation nobody activated: off, with no digest. A leaf that
    exists but cannot be read or parsed raises ``ActivationUnreadable``, because off there
    would be a fail-OPEN -- corrupting this one file would silently disable the gate
    on an installation whose operator had turned it on. Returning off is only safe for
    the never-activated case, and that case is exactly the one that raises nothing: an
    unreadable leaf can only exist where something wrote a leaf.
    """
    # Imported here rather than at module scope because this module is reachable from
    # ``kiro_crew.security``, and OUTSIDE the try because an ImportError is a defect in
    # this file, not an unactivated installation. Swallowing it would leave the gate
    # silently off forever -- the failure mode where the feature ships dead and every
    # test that mocks the reader still passes.
    from kiro_crew.config.paths import data_home

    path = data_home() / ACTIVATION_LEAF
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return Activation(enabled=False, guard_sha256="")
    except (OSError, ValueError) as exc:
        logger.warning("push verdict activation unreadable; refusing publishes", exc_info=True)
        raise ActivationUnreadable(str(exc)) from exc
    if not isinstance(payload, dict):
        # A document that PARSES but is not an object is a corrupted leaf, not an operator
        # writing a disable, so it raises for the same reason unparseable bytes do. Returning
        # off here was a fail-OPEN with a narrower entrance than the parse error above:
        # truncating this file to ``[]`` or to a bare number silently disabled the gate on an
        # installation whose operator had turned it on, and neither shape is anything a
        # writer of this leaf produces.
        logger.warning("push verdict activation is not an object; refusing publishes")
        raise ActivationUnreadable(f"activation document is {type(payload).__name__}, not object")
    enabled_value = payload.get("enabled")
    if enabled_value is not None and not isinstance(enabled_value, bool):
        # The sibling of the case above, and left unfixed it is how a closed finding returns:
        # ``{"enabled": 1}`` and ``{"enabled": "true"}`` are corrupted enables, and reading
        # either as off is the same fail-open one level in. ABSENT stays off, because that is
        # the never-activated installation; a real JSON ``false`` stays off too, because that
        # is an operator's disable.
        logger.warning("push verdict activation flag is not a boolean; refusing publishes")
        raise ActivationUnreadable(
            f"activation 'enabled' is {type(enabled_value).__name__}, not boolean"
        )
    # ``is True`` rather than ``bool(...)``: the JSON string "false" and the number 1 are
    # both truthy, and neither is an operator writing an enable. Only a real JSON ``true``
    # activates, which also makes a corrupted or half-written leaf read as OFF.
    enabled = payload.get("enabled") is True
    return Activation(
        enabled=enabled,
        guard_sha256=_pinned_digest(payload.get("guard_sha256")),
        pinned_push_url=_pinned_push_url(payload.get("pinned_push_url")),
    )


def _url_embedded_credential(url: str) -> bool:
    """Whether *url* carries userinfo that must NOT be pinned.

    A pinned destination should name a repository, not carry a credential: the gateway
    authenticates the publish from its OWN configured credentials, and the activation leaf is
    sandbox-readable, so a secret pinned here would be exposed with no gate.

    What counts as an unpinnable credential depends on the transport, because a username is
    identity for SSH but a credential carrier for HTTP(S):

    * SSH -- the ``ssh://user@host/path`` scheme form and the scp-like ``user@host:path`` form
      authenticate AS the username and resolve a login-relative path, so a bare ``user@host``
      (username, no ``:secret``) is legitimate destination identity and is ALLOWED; only a
      ``:PASSWORD`` half is a secret and is flagged.
    * HTTP(S) and every other scheme -- the username is a credential carrier
      (``https://x-access-token:TOKEN@host`` or a bare ``https://user@host``), never identity,
      so ANY non-empty userinfo (username alone OR ``user:secret``) is flagged: a pin needs no
      userinfo there at all.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.netloc:
        if parts.scheme == "ssh":
            # SSH: the username is identity; only a colon-delimited password/token is a secret.
            return bool(parts.password)
        # Non-SSH scheme (http/https/git/...): the username is a credential carrier, so ANY
        # userinfo (username-only or user:secret) is unpinnable.
        return bool(parts.username) or bool(parts.password)
    # No netloc -> either a bare path or the scp-like ``user@host:path`` (an SSH form). Only the
    # segment before the first ``/`` can hold an authority; a ``user:secret@host`` there is the
    # scp-like embedded-secret shape, and a bare ``user@host`` is legitimate SSH identity.
    # Guard against a ``scheme:`` being mistaken for userinfo: a real scp target has no ``//``
    # and its ``@`` precedes the ``:path`` colon.
    head = url.split("/", 1)[0]
    at = head.find("@")
    if at <= 0:
        return False
    return ":" in head[:at]


def _url_has_unparseable_port(url: str) -> bool:
    """True when *url*'s authority carries a port git/ssh cannot parse.

    ``urllib.parse.urlsplit(...).port`` raises ``ValueError`` for a non-numeric or out-of-range
    port (``host:notaport``, ``host:99999``). The resolver and the credential-free compare both
    read ``.port``, so an unparseable port from the agent-writable worktree config would crash
    the push request with an unhandled ``ValueError`` rather than refuse it. This detects that
    case so the caller can return the existing ``unsafe_remote_url`` refusal instead of crashing.
    A port-less URL, or one with a valid port, returns False.
    """
    try:
        urllib.parse.urlsplit(url).port
    except ValueError:
        return True
    return False


def _credential_free_url(url: str) -> str:
    """*url* with any embedded SECRET removed but the non-secret username KEPT, for compares.

    Two remotes that differ ONLY in an embedded password/token name the same repository, so the
    destination pin must be compared without the secret: a token in ``git remote get-url`` must
    neither defeat the match nor be required in the pin.

    The USERNAME is treated by transport, because whether it is identity or a credential
    placeholder depends on the form:

    * SSH -- the ``ssh://user@host/path`` scheme form and the scp-like ``user@host:path`` form
      both authenticate AS the username and (for the scp-like spelling) resolve a LOGIN-RELATIVE
      path, so ``deploy@host:repo`` and ``staging@host:repo`` are two repositories under two
      accounts. The username is IDENTITY and is PRESERVED; only a ``:secret`` half is stripped.
    * HTTP(S) -- the username in ``https://x-access-token:TOKEN@host/path`` is a credential
      carrier, not identity (the path is absolute and host-relative, and the account is the
      token), so the WHOLE userinfo is stripped -- otherwise a bare-host operator pin could
      never match a token-bearing resolved URL for the same repository.

    A genuinely different repository (different host, path, or -- for SSH -- username) still
    differs after the strip.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.netloc:
        host = parts.hostname or ""
        # SSH keeps the username (identity); every other scheme (http/https/git/...) drops the
        # whole userinfo, because there the username is a credential carrier, not identity.
        keep_user = parts.scheme == "ssh"
        userinfo = f"{parts.username}@" if (keep_user and parts.username) else ""
        # ``parts.hostname`` returns an IPv6 literal WITHOUT its ``[...]`` brackets, so
        # re-appending ``:port`` to the bare address would produce an ambiguous authority in
        # which the host/port boundary is lost: ``[2001:db8::1]:443`` and ``[2001:db8::1:443]``
        # (no port) would both recompose to ``2001:db8::1:443`` and compare EQUAL, collapsing
        # two distinct destinations to one identity. The destination pin is a plain string
        # equality over this output, so that collision could let an agent-chosen IPv6
        # destination match the operator pin and be published to. Re-bracket an IPv6 hostname
        # (the only hostname that can contain ``:``) so the authority stays unambiguous; a
        # non-IPv6 hostname never contains ``:`` and is unchanged, so the common case is
        # byte-identical.
        if ":" in host:
            host = f"[{host}]"
        # ``.port`` raises ValueError on an unparseable port; the resolver refuses such a URL
        # (``unsafe_remote_url``) before reaching here, but guard defensively so this pure
        # helper never raises -- fall back to the netloc verbatim minus any userinfo.
        try:
            port = parts.port
        except ValueError:
            netloc = parts.netloc.rsplit("@", 1)[-1] if keep_user is False else parts.netloc
            return urllib.parse.urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
            )
        if port is not None:
            host = f"{host}:{port}"
        return urllib.parse.urlunsplit(
            (parts.scheme, userinfo + host, parts.path, parts.query, parts.fragment)
        )
    # scp-like or bare path: strip only a ``:secret`` from a ``user[:secret]@`` prefix on the
    # pre-slash segment, KEEPING the username. For the scp-like ``user@host:path`` form the
    # path is login-relative, so the username is part of the destination identity (two accounts
    # at one host are two repositories) and must survive the strip; only the password/token is
    # a secret to drop.
    head, sep, tail = url.partition("/")
    at = head.find("@")
    if at > 0:
        userinfo, host_part = head[:at], head[at + 1 :]
        user = userinfo.split(":", 1)[0]
        head = f"{user}@{host_part}" if user else host_part
    return head + sep + tail


def _pinned_push_url(value: object) -> str:
    """*value* as an operator-pinned push destination, or ``""`` when none is pinned.

    An ABSENT or JSON ``null`` key means the operator pinned no destination, which is the
    honest never-pinned answer -- ``""``, the destination unconstrained. A present value that
    is not a non-empty string is a CORRUPTED pin, and reading it as "" would be the same
    fail-open the boolean and digest checks refuse: damaging this field would silently drop
    the destination binding on an installation whose operator set one. So a present non-string,
    or an empty/whitespace string, RAISES rather than reads as absent -- an operator who wants
    no pin removes the key.

    A leading-dash value is refused for the same reason ``_effective_push_target`` refuses a
    leading-dash remote URL: git reads it as an option, not a repository, so it could never be
    a destination and pinning it would only ever refuse every publish silently.

    A URL carrying an embedded PASSWORD/TOKEN in its userinfo
    (``https://x-access-token:TOKEN@host/repo.git`` or the scp-like ``user:secret@host:path``)
    is REFUSED. The activation leaf is deliberately readable in-sandbox, so REQUIRING the
    operator to pin a credentialed URL to publish would force a token into a leaf any in-sandbox
    ``open()`` can read -- exposing it with no gate and no record. The gateway authenticates the
    publish from its OWN configured credentials, so the pin need only name the destination, not
    carry a secret; a bare ``user@host`` username with no password is allowed, since it is not a
    secret. The compare side (``dashboard/handlers/push_verdict.py``) strips userinfo from both
    the pin and the resolved URL, so a token in ``git remote get-url`` neither defeats the match
    nor is required here.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        logger.warning("push verdict pinned_push_url is not a string; refusing publishes")
        raise ActivationUnreadable(
            f"activation 'pinned_push_url' is {type(value).__name__}, not string"
        )
    candidate = value.strip()
    if not candidate:
        logger.warning("push verdict pinned_push_url is blank; refusing publishes")
        raise ActivationUnreadable("activation 'pinned_push_url' is present but blank")
    if candidate.startswith("-"):
        logger.warning("push verdict pinned_push_url begins with '-'; refusing publishes")
        raise ActivationUnreadable("activation 'pinned_push_url' begins with '-'")
    if _url_embedded_credential(candidate):
        logger.warning(
            "push verdict pinned_push_url carries an embedded credential; refusing publishes"
        )
        raise ActivationUnreadable(
            "activation 'pinned_push_url' carries an embedded credential (a password or token "
            "in the URL). Pin a URL that carries no embedded credentials; the gateway "
            "authenticates the publish from its own configured credentials, so the pin need "
            "only name the destination repository."
        )
    return candidate


def _pinned_digest(value: object) -> str:
    """*value* as a pinned sha256, or ``""`` when it is not one.

    Anything that is not a full lowercase hex digest is treated as ABSENT rather than as a
    digest that will never match. The two behave the same at the comparison, but only absence
    produces the refusal that tells an operator to pin one, which is the message that gets the
    installation working again.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if len(candidate) != 64:
        return ""
    return candidate if all(character in "0123456789abcdef" for character in candidate) else ""


def activation_enabled() -> bool:
    """Whether an operator has activated push-verdict gating on this installation.

    The bool projection of ``activation()``, which is all the publish floor needs: the floor
    decides whether to gate, while the route is what has to care which guard bytes are
    pinned. One reader underneath, so the two cannot disagree about the same file.
    """
    return activation().enabled


def agent_publish_denied_detail() -> str:
    return (
        "push-verdict gating is activated on this installation, so the gateway publishes "
        f"on your behalf after it runs {GATE_TOOL} and judges the commit. An agent-run "
        "`git push` is refused outright -- the gateway holds the only publish authority "
        "while gating is on. Ask the gateway to publish instead of pushing directly."
    )


def activation_unreadable_detail(error: str) -> str:
    """Why a publish is refused while the activation state cannot be read."""
    return (
        "push-verdict gating is activated on this installation but its activation record "
        f"could not be read ({error}), so whether this publish must be gated is unknown. "
        "Refused rather than allowed: an unreadable record is not the same as gating being "
        "off. Repair or remove "
        + ACTIVATION_LEAF
        + " in the crew data directory, which needs an operator because the agent cannot "
        "write that path."
    )
