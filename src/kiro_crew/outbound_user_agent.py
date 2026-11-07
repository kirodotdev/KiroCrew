"""The product token Kiro Crew's outbound git traffic identifies itself with.

A git host counts its clients by the ``User-Agent`` each request carries. Git sends
``git/<version>`` by default, so a push an agent runtime makes is indistinguishable
from one typed in a terminal. ``GIT_HTTP_USER_AGENT`` replaces that string on every
HTTP(S) fetch and push the process tree makes; set on an agent runtime's
environment it reaches every ``git`` the agent runs.

The header keeps a ``git/`` product token FIRST. Some forges and SSO proxies tell a
git client from a browser by that prefix and answer a sign-in page or the dumb
protocol to anything else, so the replacement header is ``git/2.0 KiroCrew/<ver>``,
the shape libgit2 uses for the same reason (its own name after a fixed ``git/2.0``).
A fixed prefix, not the host's real git version: that would cost a subprocess per
spawn and would put a second per-host fact on the wire. A host counts Kiro Crew
clients by matching the ``KiroCrew/`` token, wherever it sits in the header.

The Kiro Crew token is the product name and the release version only, clamped to
``major.minor.patch`` the way the beacon clamps it (``beacon.release``): a nightly
or enterprise ``__version__`` carries a per-build stamp that would name one build,
and so nearly one install. The clamped version is low-cardinality (every install
on a release shares it) and the string names no host, user, install or session:
a server learns that Kiro Crew made the request, and nothing about which Kiro
Crew. Who made it is the credential's job, not this header's. The token is
self-declared, so it serves usage counting, never authorization. SSH transports
carry no ``User-Agent``, so a push over SSH is not tagged.

It is a usage signal that leaves the machine, so it rides the one consent gate every
other outbound signal rides (``beacon.telemetry_permitted``: the opt-out variable,
the governance ceiling, ``telemetry.beacon_enabled``, CI, a non-default home, the
first-run privacy disclosure). An install that has opted out of telemetry sends
git's default ``User-Agent`` and nothing says Kiro Crew.
"""

from __future__ import annotations

import logging
import re

from kiro_crew import __version__
from kiro_crew.beacon import release, telemetry_permitted
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

logger = logging.getLogger(__name__)

#: Git's own override for the ``User-Agent`` of its HTTP transport; no other git
#: setting or config key reaches the header from the environment.
GIT_HTTP_USER_AGENT_ENV = "GIT_HTTP_USER_AGENT"

_PRODUCT = "KiroCrew"  # brand-ok: wire identifier, an RFC 9110 product token has no space

#: The git product token that leads the header (see the module docstring).
_GIT_PREFIX = "git/2.0"

#: This product's identifier at any version, so one an OLDER Kiro Crew left on the
#: gateway's own environment is recognised as ours too.
_PRODUCT_RE = re.compile(rf"{re.escape(_PRODUCT)}/[0-9A-Za-z.+-]+")

#: A whole header this product set: the fixed prefix and the identifier, nothing else.
_HEADER_RE = re.compile(rf"{re.escape(_GIT_PREFIX)} {_PRODUCT_RE.pattern}")

#: The config sections the consent verdict is read from. A degraded one holds
#: DEFAULTS, and the default ``telemetry.beacon_enabled`` is true, so a verdict read
#: off a torn document could permit the tag on an install whose real setting could
#: not be read.
_CONSENT_SECTIONS: frozenset[str] = frozenset({"telemetry", "dashboard"})


def outbound_user_agent() -> str:
    """``KiroCrew/<major.minor.patch>``: one product token, no host or install detail."""
    return f"{_PRODUCT}/{release(__version__)}"


def git_http_user_agent() -> str:
    """The whole ``User-Agent`` header git sends: ``git/2.0 KiroCrew/<version>``."""
    return f"{_GIT_PREFIX} {outbound_user_agent()}"


def git_user_agent_permitted() -> bool:
    """Whether this install may tag its git traffic: the shared telemetry verdict.

    Blocking (reads ``config.json`` and writes the governance audit record), so the
    launch tail calls it off the event loop. The verdict is per spawn, never cached:
    an opt-out flipped between two sessions must reach the next one.

    Fails closed, like the spawn-env readers beside it: a config that cannot be
    loaded, or whose ``telemetry`` or ``dashboard`` section (or whole document)
    the loader replaced with defaults, answers ``False``. The default
    ``beacon_enabled`` is true, so reading a torn section as consent would tag the
    traffic of an install whose real setting could not be read; and config must
    never break a spawn.
    """
    try:
        cfg = KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 - a spawn never fails on config
        logger.warning("git User-Agent tag withheld: config could not be loaded", exc_info=True)
        return False
    degraded = set(cfg.degraded_sections) & (_CONSENT_SECTIONS | {DEGRADED_WHOLE_CONFIG})
    if degraded:
        logger.warning(
            "git User-Agent tag withheld: the consent verdict cannot be read off a "
            "degraded config (%s)",
            "/".join(sorted(degraded)),
        )
        return False
    return telemetry_permitted(
        enabled=cfg.telemetry.beacon_enabled,
        acked=cfg.dashboard.privacy_acked,
        audit_tool="git_user_agent",
    ).ok


#: Config paths the consent verdict reads. Every agent process carries the tag it
#: was spawned with, so a change here that WITHDRAWS consent must reach the live
#: processes; the session manager's config applier watches these and asks
#: :func:`consent_withdrawn` whether this change is such a withdrawal.
TELEMETRY_CONSENT_CONFIG_PATHS: tuple[str, ...] = (
    "telemetry.beacon_enabled",
    "dashboard.privacy_acked",
)


def consent_withdrawn(old: object, new: object) -> bool:
    """Whether a config change took the tag from permitted to refused.

    ``old`` and ``new`` are the two config documents of one reload. Only this
    transition needs live processes replaced: a grant tags nothing a process was
    forbidden to send, and a change that leaves the verdict where it was (an env
    opt-out or a governance ceiling already refusing) changes nothing on the wire.
    Audit-free (``audit_tool=""``): this is a comparison, not an enforcement.
    """
    if old is None:
        return False

    def _ok(cfg: object) -> bool:
        return telemetry_permitted(
            enabled=bool(cfg.telemetry.beacon_enabled),  # type: ignore[attr-defined]
            acked=bool(cfg.dashboard.privacy_acked),  # type: ignore[attr-defined]
        ).ok

    return _ok(old) and not _ok(new)


def apply_git_user_agent(env: dict[str, str]) -> dict[str, str]:
    """Put the Kiro Crew identifier on the git transport User-Agent of ``env``, in place.

    With no inherited ``GIT_HTTP_USER_AGENT``, or one this product set itself (any
    version), the header is the whole ``git/2.0 KiroCrew/<ver>``. An inherited
    value the OPERATOR chose is kept and the identifier is appended to it:
    git-config(1) documents ``http.userAgent`` and this variable for proxies that
    admit only known User-Agent strings, so replacing the operator's value would
    break every agent fetch through such a proxy while the same command in their
    terminal works, and a host still counts by matching ``KiroCrew/`` wherever it
    sits. Any ``KiroCrew/<ver>`` already in the value is removed first, so a shell
    an older Kiro Crew already extended carries one identifier at the current
    version, never two. The agent scrub leaves this name alone (it strips only
    ``GIT_ASKPASS`` from the family) and agent configuration cannot carry it (it
    refuses ``GIT_*`` names except git's prompt switch and CA path), so the
    gateway's own shell is the only source of an inherited value. A
    ``http.userAgent`` set in git CONFIG is still overridden by this variable (git
    gives the environment precedence); an operator behind such a proxy exports the
    variable in the gateway's shell instead, and it is kept.
    """
    inherited = env.get(GIT_HTTP_USER_AGENT_ENV)
    if inherited is None or _HEADER_RE.fullmatch(inherited):
        env[GIT_HTTP_USER_AGENT_ENV] = git_http_user_agent()
        return env
    theirs = " ".join(_PRODUCT_RE.sub("", inherited).split())
    env[GIT_HTTP_USER_AGENT_ENV] = (
        f"{theirs} {outbound_user_agent()}" if theirs else git_http_user_agent()
    )
    return env


def drop_inherited_git_user_agent(env: dict[str, str]) -> dict[str, str]:
    """Remove a Kiro Crew product token this process inherited; keep anything else.

    The refused-verdict twin of :func:`apply_git_user_agent`. A gateway started from
    inside a tagged agent shell inherits that shell's ``GIT_HTTP_USER_AGENT``, so a
    child spawned under a refusing verdict would still send a Kiro Crew token the
    install never consented to. Only a value shaped like this product's own header
    (``git/2.0 KiroCrew/<version>``, any version) is dropped whole; an operator's
    own User-Agent that an earlier permitted spawn extended loses only the
    ``KiroCrew/<version>`` identifier and is otherwise left exactly as exported.
    """
    value = env.get(GIT_HTTP_USER_AGENT_ENV)
    if value is None:
        return env
    if _HEADER_RE.fullmatch(value):
        del env[GIT_HTTP_USER_AGENT_ENV]
        return env
    if not _PRODUCT_RE.search(value):
        return env  # nothing of ours in it: untouched, whitespace and all
    theirs = " ".join(_PRODUCT_RE.sub("", value).split())
    if theirs:
        env[GIT_HTTP_USER_AGENT_ENV] = theirs
    else:
        del env[GIT_HTTP_USER_AGENT_ENV]
    return env
