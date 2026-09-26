"""Environment contract for the three container processes.

Every process in the task reads its configuration from here and nowhere else.
Parsing happens once, at import of `load()`, and the result is frozen: a process
that disagrees with another about a path or a port is the failure mode this
module exists to prevent.

`SMC_` is a historical prefix. It stands for an earlier project name and says
nothing about what this deployment is: a task belongs to the owner who launched
it, one principal reaches it, and there is no sharing surface, no second caller
and no guest. `SMC_SINGLE_PRINCIPAL` below is what the container refuses to boot
without, and it is the opposite of a sharing switch -- it is the deployment
vouching that only one principal can reach the task. Renaming the prefix would
touch the image, the task definition and every test that constructs an
environment, so the names stay and this paragraph is the correction.

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

# The header a control-route request must carry. What keeps a
# customer off those routes is the VALUE, not the header: the front compares it against
# `SMC_CONTROL_SECRET` in constant time and refuses when no secret is configured, so a
# caller who sends this header without holding the secret is denied like any other.
# Nothing strips or rewrites the header on the way in.
#
# Pinned here rather than in the front process because the string will have more
# than one consumer. The front process that checks it is the only one in this tree
# today; the sender and the deploy template that supplies the secret both belong to
# the deploy track. That track is not Python, so a constant private to the front
# process would be a name another system copies by hand.
CONTROL_SECRET_HEADER = "X-SMC-Control-Secret"

#: Ceiling on an object this task will read from the bucket into memory, or warn about
#: sending to it.
#:
#: Pinned here because every process that moves an object reads it, and a second copy is
#: the drift the shared key module exists to prevent in the other direction. The front
#: holds a fetched transcript IN MEMORY for the length of a turn and the restore step holds
#: an authority file long enough to validate and write it, so without a ceiling one object
#: decides how much memory the task uses. The sidecar reads the same number to say so at
#: upload time, when an operator can still act, rather than leaving a customer's turn to
#: discover it.
#:
#: 64 MiB is far above any real transcript or slot index (both are JSON text) and far below
#: the task's memory, so it separates "a big conversation" from "an object that should not
#: be read at all" without needing to know which conversations exist.
MAX_OBJECT_BYTES: int = 64 * 1024 * 1024

#: How long one bucket request may take, and how many attempts it gets.
#:
#: Declared here because the supervisor's sidecar drain window is sized against them:
#: the final cycle has to finish inside that window, so one hung connection must not be
#: able to consume it. boto3's own defaults are minutes long with more retries, which is
#: the right posture for a long-running client and the wrong one for a process that is
#: being drained.
#:
#: The timeout is spent TWICE per attempt, because it bounds the connect and the read
#: separately. The count is a count of ATTEMPTS including the first, which is what the
#: client's ``total_max_attempts`` key means; its ``max_attempts`` key counts RETRIES and
#: botocore adds one to it, so passing this constant there would buy an attempt the budget
#: below does not reserve.
#:
#: One attempt, so that a whole PUT is one attempt and the final cycle's per-object budget
#: below can bound it. Retrying inside the drain window cannot help: four attempts of
#: connect-plus-read plus standard-mode backoff is 47s of a 45s window, which leaves the
#: authority objects unable to publish at all. The retry an interval cycle wants is the
#: next interval cycle, which re-reads the same files and republishes whatever this one
#: recorded as failed; the final cycle has no next cycle, and that is exactly the window
#: that cannot afford one.
BACKUP_REQUEST_TIMEOUT_SECS: int = 5
BACKUP_MAX_ATTEMPTS: int = 1

#: How long each child gets to drain at teardown, and the stop timeout their sum requires.
#:
#: Declared here rather than in the supervisor because the sum is ONE CONTRACT SPLIT ACROSS
#: TWO SUBSYSTEMS. The supervisor spends these windows in order; the task definition the
#: control plane registers must give the task at least their sum, or the platform SIGKILLs
#: the supervisor mid-drain. Both read this module, so neither can move without the other.
#:
#: The front goes first, to stop new turns arriving. The backend gets the longest of the
#: three because a ``kiro-cli`` worker setsid's into its own process group, so only the
#: backend's own SIGTERM handler can reap it and a shorter window orphans workers that go
#: on to finish their turn. The sidecar goes last, and its window is for ONE cycle that
#: begins AFTER it is signalled -- the objects the backend's drain just produced, not a
#: full pass, since everything earlier is already recorded uploaded. That window is sized
#: against the transport rather than guessed: ``BACKUP_PER_OBJECT_BUDGET_SECS`` below bounds
#: one PUT, and this holds several, so a cycle waiting on a slow bucket finishes instead of
#: being killed.
#:
#: A cut final cycle still costs at most the turns since the last interval, and the objects
#: it did upload stay durable, so the worst case is bounded and loud rather than total --
#: but it is a worst case, not the planned window. Fargate caps ``stopTimeout`` at 120s,
#: which the sum has to stay under.
FRONT_DRAIN_SECS: float = 5.0
BACKEND_DRAIN_SECS: float = 25.0
SIDECAR_DRAIN_SECS: float = 45.0
#: What one ATTEMPT can cost: a connect and a read, each bounded by the request timeout.
#:
#: Two timeouts, not one. ``connect_timeout`` and ``read_timeout`` are separate bounds on
#: separate phases of the same attempt, so an attempt that hangs on the connect and then
#: again on the read spends both.
BACKUP_ATTEMPT_COST_SECS: float = 2 * BACKUP_REQUEST_TIMEOUT_SECS
#: What one object can cost the final cycle: every attempt it may make, plus the waits
#: between them.
#:
#: DERIVED from the client's own configuration rather than written as a number, so the gate
#: cannot promise a bound the transport does not honour. Standard-mode backoff is
#: exponential from a one-second base, so the wait before attempt n is ``2 ** (n - 2)`` and
#: the waits across ``attempts`` attempts total ``2 ** (attempts - 1) - 1``.
#:
#: The final cycle uploads sequentially, so its total is this times the number of changed
#: objects, which nothing bounds. The cycle therefore carries a DEADLINE and stops
#: attempting objects it cannot finish inside the drain window, naming each one it did not
#: reach. That converts an overrun from a SIGKILL in the middle of a PUT -- which loses the
#: object being sent and says nothing about the rest -- into a short cycle that reports
#: exactly what is missing and exits non-zero.
BACKUP_PER_OBJECT_BUDGET_SECS: float = BACKUP_MAX_ATTEMPTS * BACKUP_ATTEMPT_COST_SECS + (
    2 ** (BACKUP_MAX_ATTEMPTS - 1) - 1
)
#: Slack between the last drain window and the platform's own stop timeout.
#:
#: Draining is not only the children's own time: each group is signalled, waited on, then
#: swept and reaped, and the orphan sweep runs afterwards. Without this margin the task
#: timeout equals the windows exactly, so the reap overhead alone puts the supervisor past
#: it and the platform kills the process that was about to report cleanly.
TEARDOWN_REAP_MARGIN_SECS: float = 10.0
TASK_STOP_TIMEOUT_SECS: int = int(
    FRONT_DRAIN_SECS + BACKEND_DRAIN_SECS + SIDECAR_DRAIN_SECS + TEARDOWN_REAP_MARGIN_SECS
)
#: The largest ``stopTimeout`` Fargate accepts on a container definition.
MAX_TASK_STOP_TIMEOUT_SECS: int = 120


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

    # Shared filesystem. Every process in the task sees the same paths.
    data_home: Path
    config_dir: Path

    # Where the sidecar writes this task's state and where the front reads a slot's
    # transcript from on demand. One bucket, one prefix, two directions: the key both
    # sides derive lives in ``keys.py`` so they cannot disagree about it.
    crew_name: str
    backup_bucket: str | None
    backup_prefix: str

    # Seconds between backup cycles. A task replacement loses at most the turns taken
    # since the last completed cycle, so this is the width of that window, and the
    # front's rule that a local transcript is never overwritten by a fetched one is
    # written against it: the local copy leads the bucket by up to one interval.
    #
    # Carries a default for the same reason ``bundle_dir`` does: several tests build
    # Settings by hand.
    backup_interval_secs: int = 60

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

    # How many seconds this task may run before the supervisor stops it, where zero
    # means unbounded.
    #
    # Absent reads as zero, so a launch path that says nothing about lifetime behaves
    # exactly as it does without this setting: the task runs until something outside
    # stops it. The launcher derives the value from the same bound its own launch-time
    # sweep enforces, which is why there is one number and not two to keep in step.
    #
    # It exists because a Fargate task is unattended. A sweep driven by a launch cannot
    # reach a cluster whose last launch has already happened, so a deadline the task
    # carries itself is the only one that still holds with no further launch, no
    # scheduler, and the owner's gateway switched off.
    #
    # Carries a default for the same reason `bundle_dir` does: several tests build
    # Settings by hand.
    task_ttl_seconds: int = 0

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


def _interval(name: str, default: int) -> int:
    """Read a cadence in seconds, refusing one that is not a cadence.

    Zero or negative is not a faster cadence, it is a busy loop: the wait between cycles
    returns immediately and the process uploads continuously. That is the only value the
    container can say is wrong, so it is the only one refused.

    There is deliberately no lower floor above it. A floor would be a claim about what a
    cycle costs on a real data home, and nothing here has measured that; an operator who
    sets two seconds on a crew with three small transcripts is not making a mistake this
    module can see. Refused rather than clamped, for the reason ``parse_route_prefix``
    refuses a bare word: a silently corrected value produces a deployment that behaves
    differently from the one the operator described.
    """
    value = _int(name, default)
    if value <= 0:
        raise ConfigError(
            f"{name} is {value}, which is not a cadence. The wait between cycles would "
            "return immediately and the task would upload continuously instead of "
            "serving turns. It is refused rather than raised to a default, because a "
            "value this low says the operator meant something the container cannot do."
        )
    return value


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


def parse_task_ttl_seconds(name: str) -> int:
    """Seconds a task may run, where zero means unbounded and a negative is REFUSED.

    Absent, empty and ``0`` all read as unbounded, so a launch path that says
    nothing about lifetime gets the behaviour it gets without this setting at all.

    A negative value is refused rather than repaired, on the same ground as
    ``_bool``: it is not a shorter life. A deadline already in the past stops the
    task in its first wait, and the supervisor treats a lifetime stop as an
    ORDERLY one, so a task that did no work at all would report a clean shutdown.
    The launcher derives this value and the request builder refuses a caller who
    names it, so a negative arriving here says the launcher is wrong rather than
    that an operator mistyped, and a refusal at startup is how that gets seen.
    """
    value = _int(name, 0)
    if value < 0:
        raise ConfigError(
            f"{name} must be zero or more seconds, got {value}. Zero means unbounded. "
            "A negative deadline is already past, so the task would stop in its first "
            "wait and report that as an orderly shutdown."
        )
    return value


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
        # What the wrong value costs is worth keeping in view. `session_map.json`
        # and `open_slots.json` are what turn a slot id back into a conversation,
        # so a `config_dir` pointing somewhere the rest of the task does not read
        # leaves the transcripts findable and the resume and the conversation list
        # not. It stays overridable only so a test can construct the wrong case on
        # purpose; the supervisor refuses to start when the two disagree.
        config_dir=_path("SMC_CONFIG_DIR", str(data_home)),
        crew_name=os.environ.get("SMC_CREW_NAME") or "",
        backup_bucket=os.environ.get("SMC_BACKUP_BUCKET") or None,
        backup_prefix=os.environ.get("SMC_BACKUP_PREFIX") or "",
        backup_interval_secs=_interval("SMC_BACKUP_INTERVAL_SECS", 60),
        # The crew bundle in the image. Defaults to the real path; a test points
        # SMC_BUNDLE_DIR at a fixture. Never defaulted to a temp dir (see field).
        bundle_dir=_path("SMC_BUNDLE_DIR", "/app/crew-bundle"),
        # Derived by the launcher from the same bound its launch-time sweep enforces,
        # never operator-supplied: the request builder refuses a caller who names it.
        # Absent or zero means unbounded, so a launch that says nothing about lifetime
        # behaves as it does without this setting.
        task_ttl_seconds=parse_task_ttl_seconds("SMC_TASK_TTL_SECONDS"),
    )
