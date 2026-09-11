"""Environment contract for the three container processes.

Every process in the task reads its configuration from here and nowhere else.
Parsing happens once, at import of `load()`, and the result is frozen: a process
that disagrees with another about a path or a port is the failure mode this
module exists to prevent.

Two values are deliberately NOT configurable.

`BACKEND_HOST` is fixed at 127.0.0.1. The Kiro Crew backend must never be
reachable from the network, and a setting is a thing an operator can get wrong.

The backend's authentication secret is not here either. It is generated per boot
and is read from disk on every use; see `secret.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Not configurable. See the module docstring.
BACKEND_HOST = "127.0.0.1"

# The header the owner's control plane sends to reach a control route, and the
# one the API Gateway integration injects on every request so a client cannot
# forge it.
#
# Pinned here rather than in the front process because three places have to
# agree on the exact string: the front process that checks it, the CloudFormation
# integration that injects it, and the control plane that sends it. Two of those
# are not Python, so a constant private to the front process is a name three
# systems copy by hand.
CONTROL_SECRET_HEADER = "X-SMC-Control-Secret"


class ConfigError(ValueError):
    """Raised when the environment is wrong in a way that must not be repaired.

    Refusing beats guessing: a silently corrected value produces a deployment
    that works differently from the one the operator described.
    """


@dataclass(frozen=True)
class Settings:
    # The Kiro Crew backend, on loopback.
    backend_port: int
    backend_run_dir: Path

    # The front process, the only listener the network reaches.
    front_port: int
    route_prefix: str
    control_secret: str | None

    # Shared filesystem. All three processes see the same paths.
    data_home: Path
    config_dir: Path

    # Where the front reads a slot's transcript from on demand (Track B). These named the
    # backup destination when the sidecar wrote here too; the backup subsystem was extracted
    # from this PR, so today only the front's on-demand fetch reads this bucket, and it reads
    # an empty one until the durability feature lands.
    crew_name: str
    backup_bucket: str | None
    backup_prefix: str

    # The crew bundle baked into the image (PACKAGING-CONTRACT.md, T3). The
    # supervisor installs it into the crew's read paths before the backend
    # starts, so "it started" means "the named crew is installed". Defaults to
    # the real image path `/app/crew-bundle`, NEVER a temp dir: a temp default
    # would let a test's throwaway bundle look like the shipped one, which is
    # the class of "served a default agent while gates were green" this change
    # exists to prevent.
    #
    # Carries a default because the Settings dataclass is constructed by hand in
    # several tests, so a field with no default would break every one of them.
    bundle_dir: Path = Path("/app/crew-bundle")

    # Whether the DEPLOYMENT vouches that exactly one principal reaches this task.
    #
    # It matters because the customer turn route forwards the caller's ``id`` and that id
    # drives the on-demand transcript fetch. This process has no caller identity to bind
    # the id to: authorisation happens before the call reaches the task, and nothing
    # passes an identity through to here, so a binding written in this process would fail
    # OPEN. What can be answered here is the other half of the same question: with
    # persistent memory on, is one principal the only one who can send an id at all.
    #
    # A security property the container cannot observe arrives as a setting, and the
    # container refuses to run on the unsafe combination rather than assuming the safe
    # one. Defaults to False, which is the SAFE default here -- claiming single-principal
    # is what unlocks the risky pairing, so silence must mean "not claimed".
    #
    # This does not duplicate the deploy-time rule in the templates, which refuses the
    # stack. It closes the case that rule cannot see: an image run by any other path.
    single_principal: bool = False

    @property
    def backend_base_url(self) -> str:
        return f"http://{BACKEND_HOST}:{self.backend_port}"

    @property
    def sessions_dir(self) -> Path:
        return self.data_home / "sessions"

    @property
    def archive_dir(self) -> Path:
        return self.data_home / "sessions" / "archive"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_home / "artifacts"

    @property
    def session_map_path(self) -> Path:
        return self.config_dir / "session_map.json"

    @property
    def open_slots_path(self) -> Path:
        return self.config_dir / "open_slots.json"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


def _bool(name: str, default: bool) -> bool:
    """Parse a strict boolean. An unrecognised value is REFUSED, not falsy.

    This gates a security-class setting (whether the deployment claims a single
    principal), so the usual ``value.lower() in ("1", "true")`` idiom is the wrong
    shape: it silently reads a typo such as ``ture`` or a templating artefact such
    as ``${Claim}`` as "no", which is the safe direction here but hides that the
    deployment did not say what it meant. Refusing makes the operator fix the value.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"{name} must be a boolean (true/false), got {raw!r}. It is not "
        "interpreted loosely because it controls whether the deployment claims a "
        "single principal, which decides whether one caller's turns may share a "
        "conversation slot with another's."
    )


def parse_route_prefix(raw: str | None) -> str:
    """Normalise an external path prefix the caller's paths arrive with.

    Optional, and empty by default. Nothing in this repository sets
    ``SMC_ROUTE_PREFIX``: it exists for a deploy path that addresses a crew by a
    path segment (``/c/<crew>/...``) and cannot rewrite the path before the task
    sees it. Delete the setting and ``strip_prefix`` with it if the deployment
    that reaches this container never needs one.

    A wrong value fails CLOSED. ``strip_prefix`` removes the prefix only from a
    path that is the prefix or begins with ``prefix + "/"`` and returns anything
    else unchanged, and the front's customer surface is a two-entry allowlist
    checked AFTER stripping, so a path that does not strip to one of those two is
    control and is refused without the control secret. It cannot strip a control
    route into the customer surface, because the only way to reach the turn path
    after stripping is to have sent the turn path.

    A bare word is REFUSED rather than repaired. ``SMC_ROUTE_PREFIX=frontdesk``
    almost certainly means the operator does not know whether the value carries
    its own slash, and a guess here is invisible until a request is misrouted.
    """
    if raw is None or raw.strip() == "":
        return ""
    value = raw.strip()
    if not value.startswith("/"):
        raise ConfigError(
            f"SMC_ROUTE_PREFIX must start with '/', got {value!r}. "
            "It is refused rather than corrected because a wrong prefix "
            "misroutes requests instead of failing."
        )
    value = value.rstrip("/")
    if "//" in value:
        raise ConfigError(f"SMC_ROUTE_PREFIX contains an empty segment: {raw!r}")
    if value == "":
        # ``"/"`` and ``"//"`` survive the checks above and strip down to nothing, which
        # would silently mean "no prefix" -- so the container would serve the bare routes
        # while the deployment believed it had set a prefix. A value that means nothing
        # after normalisation is REFUSED for the same reason a bare word is: the operator
        # did not say what they meant, and the failure would first show as a misrouted
        # request rather than as an error. Leave it unset to mean no prefix.
        raise ConfigError(
            f"SMC_ROUTE_PREFIX is {raw!r}, which normalises to an empty prefix. Unset it "
            "to serve the routes unprefixed; a value that means nothing is refused rather "
            "than read as no value."
        )
    return value


def load() -> Settings:
    """Read the environment once. Call at process start, pass the result down."""
    data_home = _path("SMC_DATA_HOME", "/var/lib/kirocrew")
    return Settings(
        backend_port=_int("SMC_BACKEND_PORT", 8765),
        backend_run_dir=_path("SMC_BACKEND_RUN_DIR", str(data_home / "run")),
        front_port=_int("SMC_FRONT_PORT", 8080),
        route_prefix=parse_route_prefix(os.environ.get("SMC_ROUTE_PREFIX")),
        control_secret=os.environ.get("SMC_CONTROL_SECRET") or None,
        # Absent or empty means "not claimed", which is the posture that refuses the
        # risky pairing rather than the one that permits it. A value that cannot be read
        # is REFUSED instead: see `_bool`.
        single_principal=_bool("SMC_SINGLE_PRINCIPAL", False),
        data_home=data_home,
        # Defaults to the data home itself, NOT a `config/` subdirectory.
        # Verified against a running gateway: Kiro Crew's `config_dir()` and
        # `data_home()` resolve to the same directory, so `session_map.json` and
        # `open_slots.json` sit at the home root.
        #
        # This default was wrong once, and the way it failed is worth keeping in
        # view: with `data_home/config` the sidecar backs up every transcript and
        # NEITHER of those two files, so the backup looks healthy and the restore
        # has no resume and no conversation list. It stays overridable only so a
        # test can construct the wrong case on purpose; the supervisor refuses to
        # start when the two disagree.
        config_dir=_path("SMC_CONFIG_DIR", str(data_home)),
        crew_name=os.environ.get("SMC_CREW_NAME") or "",
        backup_bucket=os.environ.get("SMC_BACKUP_BUCKET") or None,
        backup_prefix=os.environ.get("SMC_BACKUP_PREFIX") or "",
        # The crew bundle in the image. Defaults to the real path; a test points
        # SMC_BUNDLE_DIR at a fixture. Never defaulted to a temp dir (see field).
        bundle_dir=_path("SMC_BUNDLE_DIR", "/app/crew-bundle"),
    )
