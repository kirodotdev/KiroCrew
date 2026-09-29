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

`SMC_INTERNAL_ONLY` is the second declaration of that kind and the only one that
LOOSENS anything: it is the deployment vouching that this task runs the operator's
OWN crews and that the operator bears the risk of what those crews read, which is
what makes an unsandboxed model subprocess acceptable on a host that cannot sandbox
one. It does NOT claim that no untrusted input arrives -- see the field below for the
exposure it accepts. Both default to "not claimed", because a claim is what unlocks a
posture and silence must never.

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

# The namespace every installed crew spec lives in, as a filename stem AND as the
# spec's declared ``name``. Kiro Crew derives specs of its own into the same directory
# -- ``kirocrew.json``, ``kirocrew-lite.json`` and the ``kirocrew-worker.json`` mirror
# it rebuilds from the default -- and a crew called ``kirocrew-worker`` is not
# hypothetical: it is the crew this deployment ships. Without a namespace the install
# lands ON the mirror, and the next spawn-path re-derivation replaces the shared crew's
# prompt and tools with Kiro Crew's own, with no error.
#
# The prefix is on BOTH the filename and the declared name because those are two
# different resolutions and only one of them dispatches. kiro-cli enumerates agents by
# the spec's DECLARED name, and Kiro Crew's snapshot of dispatchable agents does the
# same, so a file named ``crew-x.json`` whose spec still declares ``x`` is reachable
# under neither id: ``x`` is now declared TWICE (by the crew and by the mirror) and is
# refused as ambiguous, while ``crew-x`` is declared by nothing and falls back to the
# default agent -- the silent-default failure the bundle install exists to prevent.
# Measured against ``acp/kas_agents.load_agent_spec`` and
# ``config.loader._scan_materialized_agents``, not inferred.
#
# ``crew-`` is free by construction: every spec Kiro Crew manages is named
# ``kirocrew*`` (``kiro_crew/agent_files.py``), and no KAS built-in id begins with it.
# That tree is not importable here (the container installs no ``kiro_crew``), so the
# two definitions are pinned together by a test instead, the way the agents-dir
# resolver already is.
CREW_AGENT_ID_PREFIX = "crew-"

# ``_AGENT_NAME_RE`` in ``kiro_crew/validation.py`` caps a dispatchable agent name at 64
# characters. Mirrored rather than imported, for the same reason as the prefix above.
MAX_CREW_AGENT_ID_LEN = 64


def crew_agent_id(crew_name: str) -> str:
    """The agent id a crew is dispatched under: its name inside the crew namespace.

    Pure, and deliberately the only place the mapping is spelled: the supervisor
    INSTALLS the crew's spec under this id and the front ADDRESSES it under this id,
    and a deployment where those two disagree serves a default agent while reporting
    a healthy install. The customer-facing address is unchanged -- a caller still
    names the crew -- so this never appears in the API.

    Not validated here. Whether the id can name an agent at all is decided once, at
    install time, where a refusal stops the boot; see ``bundle.install_bundle``. A
    per-request check would answer the same question in the place where the only
    available answer is a 500.
    """
    return f"{CREW_AGENT_ID_PREFIX}{crew_name}"


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

    # Whether the DEPLOYMENT vouches that this task runs the operator's OWN crews and
    # that the operator bears the risk of what those crews read -- the internal-only
    # trust boundary.
    #
    # It exists because the container is sandboxed-only and Fargate cannot be sandboxed.
    # kiro-cli sandboxes the model subprocess in an unprivileged user namespace; Fargate's
    # default seccomp profile denies `unshare(CLONE_NEWUSER)` and offers no
    # `privileged`, no `dockerSecurityOptions` and no capability that changes it, so the
    # supervisor refused to start there. Measured on a real task, not inferred.
    #
    # What accepting this setting accepts. On a host with no user namespace the model
    # worker runs UNSANDBOXED, and that worker auto-approves every tool it calls. It is
    # a child of the backend under the same uid, and the backend must be able to decrypt
    # the crew's vault to answer the engine's token request -- so the worker can reach
    # the model credential, and taking the credential out of its environment does not
    # change that. Measured: a uid-1000 process reads and decrypts that vault directly.
    #
    # Be exact about the size of that exposure, because the setting's name invites
    # reading it as smaller. It is NOT only about who sends the prompt. A crew consumes
    # untrusted CONTENT in the ordinary course of its work -- tool output, a fetched web
    # page, a connector or API payload, text someone else wrote -- any of which can carry
    # an injection, and all of which reach the worker whoever sent the prompt. So with
    # this set, a worker injected through any of those routes can read the model
    # credential. What the operator accepts is that whole exposure on their own crews,
    # where the credential at risk and the account it belongs to are theirs. A user
    # namespace is the real containment, and a Firecracker-based runtime is the answer
    # for multi-tenant or external callers.
    #
    # A security property the container cannot observe arrives as a setting, exactly as
    # `single_principal` does. Defaults to False, which is the SAFE default: claiming the
    # boundary is what unlocks the risky posture, so silence must mean "not claimed", and
    # a local host or any other lane that says nothing keeps refusing.
    internal_only: bool = False

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
    def kiro_home(self) -> Path:
        """kiro-cli's user directory for this container: ``<data home>/kiro``.

        The ONE definition of that path, because two processes have to agree on it
        and they reach it by different routes: the supervisor installs the crew's
        agent spec under it (``bundle.install_bundle``) and the backend reads and
        REWRITES specs there (``kiro_crew.agent.rebuild_agent_config``). Both are
        pointed at it by the same exported ``KIRO_HOME`` (``backend.ENV_KIRO_HOME``,
        exported in ``supervisor.__main__.export_kiro_home``), so a change to the
        layout moves both sides at once.

        Why not the process HOME's ``~/.kiro/agents``, which is where this landed
        before: that directory is SHARED by every instance under this ``$HOME``, and
        the backend runs on a non-default data home (``KIROCREW_HOME=<data home>``).
        Kiro Crew refuses to rewrite a shared agents dir from a non-default home --
        the specs it would write pin the writer's data home into every managed MCP
        server entry, which breaks strict session identity for a default-home
        gateway (kirodotdev/KiroCrew#9690). In the container that refusal meant the
        default spec ``kirocrew.json`` was never written at all, and every turn died
        at ``DerivedSpecStale``.

        ``<data home>/kiro`` is the one layout that guard exempts by construction:
        it matches ``config.paths.isolated_agents_dir(data home)`` exactly (``<data
        home>/kiro/agents``), which is its documented private-target case -- a
        directory this instance's own teardown owns, shared with nobody. Matched
        EXACTLY there, not by ancestry, so the ``kiro`` segment is load-bearing and
        the container must not spell this ``<data home>`` or any other nesting.
        """
        return self.data_home / "kiro"

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


#: Why an unreadable boolean is refused, when the caller names no reason of its own.
#: Every boolean here is a security property the container cannot observe, so the
#: general statement is the honest default rather than one setting's specifics.
_BOOL_REFUSAL_REASON = (
    "which is a security property this container cannot observe for itself, so a "
    "value it cannot read is refused rather than guessed at"
)


def _bool(name: str, default: bool, *, why: str = _BOOL_REFUSAL_REASON) -> bool:
    """Parse a strict boolean. An unrecognised value is REFUSED, not falsy.

    Every caller of this gates a security-class setting, so the usual
    ``value.lower() in ("1", "true")`` idiom is the wrong shape: it silently reads a
    typo such as ``ture`` or a templating artefact such as ``${Claim}`` as "no",
    which is the safe direction here but hides that the deployment did not say what
    it meant. Refusing makes the operator fix the value.

    *why* is the setting's own reason, carried into the refusal. It is a parameter
    rather than one sentence covering every caller because the settings decide
    different things, and a message naming the wrong one sends an operator looking
    at the wrong parameter. There is more than one such setting now
    (``SMC_SINGLE_PRINCIPAL`` and ``SMC_INTERNAL_ONLY``), which is exactly when a
    shared sentence starts being wrong for one of them.
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
        f"interpreted loosely because it controls {why}."
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
        single_principal=_bool(
            "SMC_SINGLE_PRINCIPAL",
            False,
            why=(
                "whether the deployment claims a single principal, which decides "
                "whether one caller's turns may share a conversation slot with another's"
            ),
        ),
        # The internal-only trust boundary (see the field). Absent means "not claimed",
        # so a deployment that says nothing keeps the sandboxed-only refusal.
        internal_only=_bool(
            "SMC_INTERNAL_ONLY",
            False,
            why=(
                "whether this task serves the operator's own crews only, which decides "
                "whether the model subprocess may run unsandboxed on a host that cannot "
                "sandbox it"
            ),
        ),
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
        # The crew bundle in the image. Defaults to the real path; a test points
        # SMC_BUNDLE_DIR at a fixture. Never defaulted to a temp dir (see field).
        bundle_dir=_path("SMC_BUNDLE_DIR", "/app/crew-bundle"),
        # Derived by the launcher from the same bound its launch-time sweep enforces,
        # never operator-supplied: the request builder refuses a caller who names it.
        # Absent or zero means unbounded, so a launch that says nothing about lifetime
        # behaves as it does without this setting.
        task_ttl_seconds=parse_task_ttl_seconds("SMC_TASK_TTL_SECONDS"),
    )
