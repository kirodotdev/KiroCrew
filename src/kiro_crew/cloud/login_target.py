"""The Kiro identity a managed remote crew must sign in as.

A managed crew is "my Kiro running elsewhere", so the identity it signs in
with is a DURABLE property of the launch — inherited from the launching
machine, overridable, persisted with the launch job, forwarded on every
start AND resume of the device flow, and verified after authorization — not
a set of flags supplied to whichever ``kiro-cli login`` happens to run.

``kiro-cli login --use-device-flow`` with no identity flags is the Builder ID
flow. An IAM Identity Center (Kiro Pro) user launching a managed crew was
therefore shown the generic Builder ID portal, and because the pre-login
check was a boolean ("is *some* session present?"), approving the wrong
account read as success. The standalone recovery command grew the three flags;
the managed paths did not. This module is the shape that makes the omission
type-visible: every sign-in entry point takes a :class:`KiroLoginTarget`.

Nothing here is a credential. The start URL, license tier and Identity Center
region are configuration; device codes and tokens never enter this object.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from kiro_crew.kiro_cli import resolve_kiro_cli
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

# The account types ``kiro-cli whoami --format json`` reports.
ACCOUNT_TYPE_BUILDER_ID = "BuilderId"
ACCOUNT_TYPE_IDENTITY_CENTER = "IamIdentityCenter"
#: kiro-cli's whoami reports this when a ``KIRO_API_KEY`` is in the environment.
#: It is neither identity family and must never satisfy a login target.
ACCOUNT_TYPE_API_KEY = "ApiKey"
#: The account types a DEFAULT (Builder ID) target accepts as its own family:
#: ``BuilderId`` itself and the ``Social<Provider>`` sessions (Google, GitHub)
#: that the default flow's own social-callback fallback produces. Deliberately
#: closed otherwise: an unlisted type is not a match.
_PERSONAL_ACCOUNT_TYPES = frozenset({ACCOUNT_TYPE_BUILDER_ID})
_SOCIAL_ACCOUNT_TYPE_PREFIX = "Social"


def is_personal_account_type(account_type: str) -> bool:
    """Is *account_type* in the family a default (Builder ID) target accepts?

    ``BuilderId`` and every ``Social<Provider>`` type. ``IamIdentityCenter``,
    ``ApiKey``, an unknown type and an empty one are not.
    """
    return account_type in _PERSONAL_ACCOUNT_TYPES or account_type.startswith(
        _SOCIAL_ACCOUNT_TYPE_PREFIX
    )


_LICENSES = frozenset({"free", "pro"})
# Same shape ``auth/service.py`` accepts for an Identity Center region.
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
# A syntactically valid DNS hostname: labels of letters/digits/hyphens, at least
# two labels, a TLD that is not all digits. Deliberately NOT pinned to
# ``<org>.awsapps.com`` -- see normalize_start_url.
_HOSTNAME_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_CONTROL_OR_SHELL_RE = re.compile(r"[\x00-\x1f\x7f\s\"'`$\\;&|<>(){}\[\]*?!~#]")


class LoginTargetError(ValueError):
    """The supplied identity target is not one ``kiro-cli login`` can act on."""


def normalize_start_url(raw: str) -> str:
    """Return the canonical ``https://<host>/<path>`` form of a start URL, or raise.

    Refuses what is UNSAFE, not what is unfamiliar. HTTPS on the default port
    only; no userinfo, query or fragment; a syntactically valid DNS hostname;
    and — because the value later reaches a remote shell (shell-quoted at that
    boundary) — no control or shell metacharacters; validation and quoting
    protect against different mistakes. The host and path are NOT pinned to
    ``<org>.awsapps.com/start``: Identity Center portals also live in other
    partitions (GovCloud, China) and behind custom domains, and the auth
    module's stance is that any customer Identity Center start URL works.
    Pinning would lock those organizations out of sign-in entirely; the
    portal itself rejects a URL that is not one of its own.
    """
    value = (raw or "").strip()
    if not value:
        raise LoginTargetError("start URL is required for an Identity Center sign-in")
    if _CONTROL_OR_SHELL_RE.search(value):
        raise LoginTargetError("start URL contains characters that are not allowed")
    if "://" not in value:
        value = "https://" + value
    # urlsplit and its .port accessor raise bare ValueError on malformed input
    # (``:abc`` as a port, an unbalanced IPv6 bracket). Every malformed shape is
    # the same refusal to the caller, never a traceback or a 500.
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise LoginTargetError("start URL is not a valid URL") from exc
    if parts.scheme != "https":
        raise LoginTargetError("start URL must use https")
    if parts.username is not None or parts.password is not None:
        raise LoginTargetError("start URL must not carry credentials")
    if port is not None:
        raise LoginTargetError("start URL must use the default https port")
    if parts.query or parts.fragment:
        raise LoginTargetError("start URL must not carry a query or fragment")
    host = (parts.hostname or "").lower()
    if not _HOSTNAME_RE.fullmatch(host):
        raise LoginTargetError("start URL host must be a valid DNS hostname")
    path = parts.path.rstrip("/") or "/start"
    return f"https://{host}{path}"


def normalize_region(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if not _REGION_RE.fullmatch(value):
        raise LoginTargetError(f"invalid Identity Center region: {raw!r}")
    return value


@dataclass(frozen=True)
class KiroLoginTarget:
    """Which Kiro identity the remote ``kiro-cli`` should sign in as.

    The EMPTY target (all fields ``""``) is the Builder ID / default flow and
    is exactly what every managed path did before this type existed, so
    callers that have no opinion stay backward compatible. A Pro target
    requires the start URL and the Identity Center region.
    """

    license: str = ""
    start_url: str = ""
    region: str = ""

    @property
    def is_default(self) -> bool:
        return not (self.license or self.start_url or self.region)

    @property
    def is_identity_center(self) -> bool:
        return bool(self.start_url)

    @classmethod
    def from_fields(
        cls, *, license: str = "", start_url: str = "", region: str = ""
    ) -> "KiroLoginTarget":
        """Validate and normalize user-supplied fields into a target."""
        lic = (license or "").strip().lower()
        if lic and lic not in _LICENSES:
            raise LoginTargetError(f"license must be one of {sorted(_LICENSES)}, not {license!r}")
        url = normalize_start_url(start_url) if (start_url or "").strip() else ""
        reg = normalize_region(region)
        if lic == "pro" and not url:
            raise LoginTargetError("a pro license requires the Identity Center start URL")
        if url and not lic:
            lic = "pro"
        if url and not reg:
            raise LoginTargetError("an Identity Center sign-in requires the Identity Center region")
        if reg and not url:
            raise LoginTargetError("an Identity Center region without a start URL is meaningless")
        return cls(license=lic, start_url=url, region=reg)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "KiroLoginTarget":
        """Load a persisted target.

        Absent, not a mapping, or every field empty → the default target:
        persisted files written before this type existed have no target keys
        and must keep loading (as Builder ID) rather than fail the job. A
        NON-EMPTY target that fails validation raises :class:`LoginTargetError`
        — it named an identity, so it must not be quietly read as Builder ID;
        the caller decides what a job with an unreadable identity becomes.
        """
        if not isinstance(data, Mapping):
            return cls()
        fields = {
            "license": str(data.get("license") or ""),
            "start_url": str(data.get("start_url") or ""),
            "region": str(data.get("region") or ""),
        }
        if not any(fields.values()):
            return cls()
        return cls.from_fields(**fields)

    def to_dict(self) -> dict[str, str]:
        return {"license": self.license, "start_url": self.start_url, "region": self.region}

    def login_kwargs(self) -> dict[str, str]:
        """The keyword arguments ``cloud.login`` takes for this target."""
        return {
            "identity_provider": self.start_url,
            "license_": self.license,
            "idp_region": self.region,
        }

    def describe(self) -> str:
        if self.is_identity_center:
            return f"IAM Identity Center ({self.start_url}, {self.region})"
        return "Builder ID"

    def recovery_command(self) -> str:
        """The shell line that signs the wrong session out and the right one in.

        kiro-cli ignores a login over a live session, so switching identity is
        always logout-then-login. The login half names this target: the
        Identity Center flags for a pinned target, the bare command for the
        Builder ID default.
        """
        login_cmd = "kirocrew cloud login"
        if self.is_identity_center:
            login_cmd += (
                f" --identity-provider {self.start_url} --license pro --idp-region {self.region}"
            )
        return f"kirocrew cloud logout && {login_cmd}"


# ── whoami parsing (shared with the dashboard's credit readout) ─────────────


def parse_whoami_output(raw: str) -> dict[str, str]:
    """Extract the identity from ``kiro-cli whoami --format json`` output.

    stdout is untrusted: only the LEADING JSON object is parsed (kiro-cli
    appends a non-JSON "Profile:" block after it), values must be strings, and
    each is length-bounded. Returns any of ``email`` / ``account_type`` /
    ``start_url``, or ``{}`` when no identity can be read.
    """
    start = raw.find("{")
    if start < 0:
        return {}
    depth = 0
    end = -1
    for i in range(start, len(raw)):
        ch = raw[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for src, dst, cap in (
        ("email", "email", 254),
        ("accountType", "account_type", 60),
        ("startUrl", "start_url", 200),
    ):
        v = data.get(src)
        if isinstance(v, str) and v:
            out[dst] = v[:cap]
    return out


def target_from_whoami(identity: Mapping[str, Any], *, region: str = "") -> KiroLoginTarget | None:
    """Derive the target a ``whoami`` identity implies.

    An Identity Center identity yields a Pro target on its start URL; *region*
    is the Identity Center region, which whoami does not report and the caller
    must supply (or the result is left un-regioned for the caller to complete).
    Builder ID, a personal (social) sign-in, or no identity at all is the default
    target. An Identity Center identity with NO readable start URL (missing, or
    one that fails validation) is ``None``: the machine is signed in to some
    organization, but which one is unknown, and a caller must not resolve that
    to Builder ID -- it asks for an explicit target instead.
    """
    if str(identity.get("account_type") or "") != ACCOUNT_TYPE_IDENTITY_CENTER:
        return KiroLoginTarget()
    url = str(identity.get("start_url") or "")
    if not url:
        return None
    try:
        return KiroLoginTarget(
            license="pro", start_url=normalize_start_url(url), region=normalize_region(region)
        )
    except LoginTargetError:
        return None


def discover_local_identity(
    kiro_bin: str | None = None, *, timeout: float = 15.0
) -> dict[str, str] | None:
    """Run the LOCAL ``kiro-cli whoami --format json`` and parse it.

    Returns the parsed identity when ``whoami`` ran and answered (``{}`` when
    it exited cleanly reporting no identity), and ``None`` when nothing is
    known — the binary failed to resolve, would not start, timed out, or exited
    nonzero without printing an identity. The two are different answers:
    ``{}`` means this machine has no Identity Center sign-in to inherit, while
    ``None`` means nothing is known about it, and a caller that defaults the
    crew's identity must treat ``None`` as unknown rather than as Builder ID.
    A machine with no kiro-cli at all has no local sign-in and reports ``{}``.

    Used by the CLI launch paths to inherit the launching machine's identity.
    The dashboard has its own async, sandbox-tiered fetch; this one is the
    plain synchronous form for ``kirocrew setup`` / ``kirocrew cloud launch``.
    """
    if kiro_bin is None:
        try:
            kiro_bin = resolve_kiro_cli()
        except Exception:  # noqa: BLE001 - discovery is best-effort
            logger.debug("kiro-cli resolution failed", exc_info=True)
            return None
    if not kiro_bin:
        return {}
    try:
        proc = subprocess.run(
            [kiro_bin, "whoami", "--format", "json"],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("local whoami failed", exc_info=True)
        return None
    identity = parse_whoami_output(proc.stdout or proc.stderr or "")
    if proc.returncode != 0 and not identity:
        # whoami ran but reported an error and no identity: an expired or
        # broken local session, a CLI fault. Nothing is known about the
        # identity, so this is the unknown answer, not "no sign-in to inherit".
        logger.debug("local whoami exited %s without an identity", proc.returncode)
        return None
    return identity


# ── remote identity comparison ──────────────────────────────────────────────


def identity_matches_target(identity: Mapping[str, Any], target: KiroLoginTarget) -> bool:
    """Does a remote ``whoami`` identity satisfy *target*?

    Compares the account-type family and, for Identity Center, the normalized
    start URL. Email is deliberately not compared: the target is "this
    organization's Kiro Pro", and the launching human may legitimately hold a
    different mailbox on the crew than on the laptop. A mismatched-but-valid
    session is NOT a match — that is the whole point of comparing at all.
    """
    account_type = str(identity.get("account_type") or "")
    if target.is_identity_center:
        if account_type != ACCOUNT_TYPE_IDENTITY_CENTER:
            return False
        try:
            return normalize_start_url(str(identity.get("start_url") or "")) == target.start_url
        except LoginTargetError:
            return False
    # Default / Builder ID target: ALLOWLIST the personal family (Builder ID and
    # the Social<Provider> sessions its own callback fallback signs in), never
    # "anything that is not Identity Center". kiro-cli also reports ``ApiKey``
    # (a ``KIRO_API_KEY`` in the instance environment) and may grow new types; a
    # reused instance carrying one of those must read as a different identity,
    # not be adopted as Builder ID and run the crew under the wrong usage pool.
    # An unknown type is refused the same way -- visible and recoverable
    # (``cloud logout``), where fail-open is silent.
    return is_personal_account_type(account_type)
