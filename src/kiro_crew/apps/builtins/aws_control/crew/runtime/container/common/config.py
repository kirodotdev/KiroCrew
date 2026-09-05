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

    # Backup destination.
    crew_name: str
    backup_bucket: str | None
    backup_prefix: str
    backup_interval_secs: int

    # Sandbox posture, supplied by the DEPLOYMENT, never assumed by the container.
    # kiro-cli spawns the model subprocess inside an unprivileged user namespace;
    # Fargate does not permit one (the probe forks and calls os.unshare, and the
    # kernel refuses), so on Fargate the choice is unsandboxed execution or no
    # service. The container must not make that choice silently, so it arrives as
    # a setting and the supervisor writes it into the config key kiro-cli reads.
    #
    # Defaults to False so that constructing a Settings without naming this field
    # gets the SAFE posture. The default belongs here rather than being omitted:
    # a required field would make every caller state a security decision it may
    # have no opinion about, and the loudest of those callers is a test.
    allow_unsandboxed_exec: bool = False

    # The crew bundle baked into the image (PACKAGING-CONTRACT.md, T3). The
    # supervisor installs it into the crew's read paths before the backend
    # starts, so "it started" means "the named crew is installed". Defaults to
    # the real image path `/app/crew-bundle`, NEVER a temp dir: a temp default
    # would let a test's throwaway bundle look like the shipped one, which is
    # the class of "served a default agent while gates were green" this change
    # exists to prevent.
    #
    # Carries a default for the same reason `allow_unsandboxed_exec` does: the
    # Settings dataclass is constructed by hand in several tests, so a field
    # with no default would break every one of them.
    bundle_dir: Path = Path("/app/crew-bundle")

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

    def backup_unit(self) -> list[Path]:
        """The paths that must be backed up and restored together.

        This is one unit. An incomplete set degrades restore silently rather
        than failing: without `session_map.json` there is no resume at all, and
        `open_slots.json` is the authoritative record of which conversations
        existed, so a restore missing it loses the conversation list even though
        every transcript is present.
        """
        return [
            self.sessions_dir,
            self.archive_dir,
            self.session_map_path,
            self.open_slots_path,
            self.artifacts_dir,
        ]


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

    This gates whether the model subprocess may run unsandboxed, so the usual
    ``value.lower() in ("1", "true")`` idiom is the wrong shape: it silently reads
    a typo such as ``ture`` or a templating artefact such as ``${Allow}`` as
    "no", which is the safe direction here but hides that the deployment did not
    say what it meant. Refusing makes the operator fix the value.
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
        "interpreted loosely because it controls whether the model subprocess "
        "may run without a sandbox."
    )


def parse_route_prefix(raw: str | None) -> str:
    """Normalise the external path prefix the load balancer cannot strip.

    An ALB forward action cannot rewrite a path, so `/c/<crew>/...` arrives at
    the container and the front process has to strip it. Getting this wrong is
    not a 404: path classification runs on the prefixed path and control routes
    read as customer routes.

    A bare word is REFUSED rather than repaired. `SMC_ROUTE_PREFIX=frontdesk`
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
        # Defaults to False -- the SAFE posture, not the one that boots. A missing
        # or misspelled variable therefore refuses to start rather than quietly
        # running the model unsandboxed, so the deployment has to say this out loud.
        allow_unsandboxed_exec=_bool("SMC_ALLOW_UNSANDBOXED_EXEC", False),
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
        backup_interval_secs=_int("SMC_BACKUP_INTERVAL_SECS", 30),
        # The crew bundle in the image. Defaults to the real path; a test points
        # SMC_BUNDLE_DIR at a fixture. Never defaulted to a temp dir (see field).
        bundle_dir=_path("SMC_BUNDLE_DIR", "/app/crew-bundle"),
    )
