"""Harness adapters: the reviewed code one bundled harness needs, and no more.

A :class:`~kiro_crew.acp.harness_descriptor.HarnessDescriptor` says what a
harness IS; an adapter carries the few steps that cannot be said as data —
resolving an executable that is not simply a PATH name, contributing argv a
token template cannot express, and mutating the child environment before exec.

Only a bundled descriptor may name an adapter (the ``ADAPTER_*`` vocabulary); an
operator's descriptor is data and gets :class:`HarnessAdapter`, the base, whose
every method is the generic rule. That asymmetry is the whole point of the split:
configuration must never be able to select Python.

Four properties hold across this module, and each method is written to keep them:

- **The attested executable is the one that execs.** :func:`resolve_spawn_executable`
  resolves a candidate and then attests it; :func:`checked_spawn_argv` refuses an
  argv whose ``argv[0]`` is anything else. The second half is not redundant:
  without it a descriptor whose template never mentions ``{executable}`` would
  attest one path while ``exec`` resolved a bare name through ``PATH`` at spawn
  time, and the attestation would read as protection while providing none.
- **Attestation is uniform.** Bundled or operator, every harness executable goes
  through the same gate kiro-cli has always taken (runnable, non-zero-byte, its
  launch path pinned), so an operator harness is not a hole in it.
- **Adapters do blocking work and say so.** Resolution stats the filesystem and
  ``pre_spawn`` can write an agent spec, so every method here is synchronous and
  the spawn path calls it from a worker thread.
- **Nothing here imports the registry.** The registry serves descriptors and
  derives availability from :func:`resolve_executable`, so the dependency runs one
  way only: registry -> adapters -> descriptor.

Deliberately NOT here: KAS's per-session custom-agent projection. It is not a
spawn step — it runs per ``session/new`` — so it stays in ``AcpRuntime``, keyed
positively off the backend as it is today. :meth:`HarnessAdapter.post_initialize`
exists so that a harness whose post-handshake step DOES belong here can land
without editing the spawn path (harness-parity H13).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from kiro_crew import platform_compat
from kiro_crew.acp.harness_descriptor import (
    ADAPTER_CLAUDE,
    ADAPTER_GENERIC,
    ADAPTER_KAS,
    ADAPTER_KIRO,
    HarnessDescriptor,
)
from kiro_crew.acp.harness_descriptor import render_argv as render_argv_template
from kiro_crew.acp.protocol_profile import (
    KAS_PROFILE,
    KIRO_PROFILE,
    STANDARD_ACP_PROFILE,
    ProtocolProfile,
)
from kiro_crew.env import augmented_path

logger = logging.getLogger(__name__)


class HarnessSpawnRefused(RuntimeError):
    """A harness cannot be launched, with a reason that names the harness.

    Raised instead of returning an empty argv so a caller cannot proceed with a
    half-resolved command: every refusal here is terminal for that spawn, and
    R6.3's rule is refusal over falling back to a different harness.
    """


class HarnessExecutableTrustError(HarnessSpawnRefused):
    """Resolved harness bytes are not approved for credential-bearing ACP.

    The one lineage for the attestation refusal, and it is harness-neutral: the
    gate it reports was never kiro-specific (it asks whether a candidate is a
    runnable, non-empty file) and every harness takes it, so naming one harness
    in the exception made the others look exempt. ``AcpClient``'s own resolver
    raises this too, so a caller catching it covers both session paths.
    """


# ── Candidate inspection ──


def _is_spawnable_file(resolved: str) -> bool:
    """Can ``resolved`` be launched as a program on THIS platform?

    This is the spawn-a-binary question, deliberately NOT
    ``platform_compat.is_executable_file`` — that predicate answers the
    hooks/scripts question (on Windows it accepts only a known SCRIPT suffix
    ``.sh/.ps1/.cmd/.bat/.py/.exe``), which would reject a real installed
    ACP binary such as ``kiro-cli`` / ``agy-acp`` that carries no such suffix.

    POSIX: unchanged — the file must carry an execute bit (``os.access(X_OK)``),
    so a ``chmod -x`` still refuses it and the interrupted-install branch stays
    intact.

    Windows: there is NO POSIX execute bit (every file reports the same mode), so
    an ``X_OK`` check would reject every candidate. Windows determines
    runnability from the file extension via ``PATHEXT``, and PATH resolution
    already went through ``shutil.which`` (which honours ``PATHEXT``); for an
    explicit absolute path there is no bit left to check, so an existing regular
    file is spawnable. The zero-byte guard (checked before this, below) still
    refuses a truncated install on every platform.
    """
    if platform_compat.IS_WINDOWS:
        return os.path.isfile(resolved)
    return os.access(resolved, os.X_OK)


def _candidate_problem(resolved: str) -> str:
    """Why ``resolved`` cannot be spawned, or "" when it can.

    One place for the three questions availability and spawn both ask — the
    candidate exists, it is not empty, and it is an executable file — so every
    resolver gives the same verdict for the same file, on every platform. They
    are ordered so the reason names the actual defect: a path that is simply
    absent reads as "does not exist" rather than as a permissions problem, which
    is the difference between "install it" and "chmod it".

    Emptiness is checked BEFORE executability so a zero-byte file gets the
    "zero-byte file" reason on every platform (a truncated install keeps its
    execute bit on POSIX, and on Windows there is no bit to distinguish it from a
    non-executable at all) — the two spawn paths must agree that a truncated
    binary is refused, and with one reason.
    """
    if not os.path.exists(resolved):
        return f"{resolved} does not exist"
    try:
        if os.path.getsize(resolved) <= 0:
            return f"{resolved} is a zero-byte file"
    except OSError as exc:
        return f"{resolved} could not be inspected ({exc.strerror or exc})"
    if not _is_spawnable_file(resolved):
        return f"{resolved} is not an executable file"
    return ""


def _resolve_kiro_cli_candidate(descriptor: HarnessDescriptor) -> tuple[str, str]:
    """``(path, reason)`` for kiro-cli, shared by every harness the CLI serves.

    The same ``kiro_cli.resolve_kiro_cli`` discovery ``AcpClient`` uses, so the
    operator override, the fixed install locations, and the augmented PATH are
    honoured identically by every spawn path — a bare ``shutil.which`` regressed
    the runtime to "not found in PATH" once for a non-login gateway with a
    ``~/.local/bin`` install.

    That chain's own filter checks the execute bit everywhere but the zero-byte
    case only on Windows, so the shared check runs here too: a truncated
    ``~/.local/bin/kiro-cli`` (an interrupted install) is executable on POSIX and
    would otherwise resolve, then exec into a process that exits with no ACP
    frame.

    Two harnesses resolve through it — the kiro backend and the KAS relay, which
    IS a kiro-cli — and they must agree, because an operator who overrode the
    binary did so for both.
    """
    # Deferred: kiro_cli reaches the platform layer and the environment, and this
    # module is imported by the descriptor/registry side of the package.
    from kiro_crew.kiro_cli import resolve_kiro_cli

    resolved = resolve_kiro_cli()
    if not resolved:
        # Same searched-directories diagnostic AcpClient._spawn gives (#6497), so
        # the runtime path names WHERE it looked — the fixed install locations and
        # the KIROCREW_KIRO_BIN override, not just "PATH" — instead of a bare
        # "not found". Shared with the client through the one canonical message so
        # the two spawn paths cannot drift. Deferred import: acp.client imports
        # this module, so a module-level import would be a cycle.
        from kiro_crew.acp.client import kiro_cli_not_found_message

        return "", kiro_cli_not_found_message(environ=os.environ, home=Path.home())
    problem = _candidate_problem(resolved)
    if problem:
        return "", problem
    return resolved, ""


# ── Adapters ──


class HarnessAdapter:
    """The generic harness: everything an operator descriptor gets.

    Every method is the data-only rule, and a bundled adapter overrides only the
    ones its harness genuinely needs. Subclassing (rather than a dict of
    callables) is what makes that partial: an adapter that needs a custom argv
    does not thereby take over environment handling, so the number of behaviours
    a harness can accidentally change is bounded by what it actually overrode.
    """

    #: The ``ADAPTER_*`` name a bundled descriptor selects this with; empty for
    #: the base itself. No live descriptor resolves to the bare base —
    #: adapter-less descriptors resolve to :class:`GenericAdapter` (``name`` =
    #: ``ADAPTER_GENERIC``) — so this stays empty only for the abstract parent.
    name = ""

    def resolve_executable(self, descriptor: HarnessDescriptor) -> tuple[str, str]:
        """``(path, reason)`` for the descriptor's executable; one is always empty.

        Resolution only — nothing is executed and nothing is attested here, so a
        listing can ask this question about every registered harness without
        launching any of them or pinning a digest for one nobody selected.

        The path a bundled adapter or an operator gave is used as-is when it is
        absolute; a bare name goes through ``shutil.which``. Either way the
        candidate must be an executable, non-empty file — a truncated or
        non-executable candidate execs into an opaque "process exited" with no
        ACP frame, and naming it here turns that into a readable reason.
        """
        candidate = descriptor.executable
        if not candidate:
            return "", "no executable is declared"
        if os.path.isabs(candidate):
            resolved = candidate
        else:
            found = shutil.which(candidate)
            if not found:
                return "", f"{candidate!r} was not found on PATH"
            resolved = found
        problem = _candidate_problem(resolved)
        if problem:
            return "", problem
        return resolved, ""

    def render_argv(
        self,
        descriptor: HarnessDescriptor,
        *,
        executable: str,
        agent: str = "",
        model: str = "",
        workdir: str | os.PathLike[str] = "",
    ) -> list[str]:
        """The pre-sandbox argv for one spawn.

        The default is pure template rendering, so the harnesses whose invocation
        IS expressible as data — kiro-cli, Codex, every operator harness — share
        one implementation and cannot drift from each other.
        """
        return render_argv_template(
            descriptor,
            executable=executable,
            agent=agent,
            model=model,
            workdir=workdir,
        )

    def pre_spawn(
        self,
        descriptor: HarnessDescriptor,
        *,
        env: MutableMapping[str, str],
        workdir: str,
        agent: str,
    ) -> None:
        """Last chance to prepare the machine and the child environment.

        Runs off the event loop, immediately before the environment scrub, so an
        adapter may add a variable the scrub then vets — and cannot add one after
        it.

        The default strips kiro-cli's own API key: a foreign harness must never
        receive it, and the failure direction of getting this wrong is silent
        (the harness ignores an unknown variable while the key is exposed to it).
        """
        # Deferred: config.loader -> dashboard -> session -> acp would be a cycle
        # at module scope. Matches the in-file convention in acp/runtime.py.
        from kiro_crew.config.loader import strip_kiro_cli_api_key

        strip_kiro_cli_api_key(env)

    def post_initialize(self, descriptor: HarnessDescriptor, init_resp: Mapping[str, Any]) -> None:
        """React to the harness's ACP ``initialize`` response.

        No bundled harness needs this in the ``AcpRuntime`` path yet, and it is
        declared anyway: an adapter whose post-handshake step lands later must be
        able to do so without adding a branch to the spawn path, which is what
        H13 asks of every added harness.
        """

    @property
    def protocol_profile(self) -> ProtocolProfile:
        """The ACP wire dialect this harness speaks.

        The base — every operator descriptor and bundled Codex — speaks the
        public ACP wire (:data:`STANDARD_ACP_PROFILE`): integer protocol version,
        standard permission-option field names, ``session/set_config_option`` for
        model/effort, and ``agent_thought_chunk`` reasoning updates. A bespoke
        adapter overrides this only when its harness's wire genuinely differs.

        This is a property so a caller reads the profile the same way whichever
        adapter serves the descriptor, and so the wire decision lives on the
        adapter rather than in an ``_is_claude`` branch of the client/runtime.
        """
        return STANDARD_ACP_PROFILE


class KiroAdapter(HarnessAdapter):
    """kiro-cli: the first-class harness (harness-parity H1, H9).

    Its argv IS the descriptor template, so ``render_argv`` is inherited and the
    ``--agent`` / ``--model`` conventions live in the descriptor as data. What
    cannot be data is the two side effects below.
    """

    name = ADAPTER_KIRO

    def resolve_executable(self, descriptor: HarnessDescriptor) -> tuple[str, str]:
        """Resolve kiro-cli through its own candidate chain.

        See :func:`_resolve_kiro_cli_candidate`, shared with the KAS relay.
        """
        return _resolve_kiro_cli_candidate(descriptor)

    def pre_spawn(
        self,
        descriptor: HarnessDescriptor,
        *,
        env: MutableMapping[str, str],
        workdir: str,
        agent: str,
    ) -> None:
        """Materialize the agent spec, then hand kiro-cli its own API key.

        Self-heal: kiro-cli discovers its selectable modes at startup from
        ``~/.kiro/agents/*.json``, so the managed default agent file must exist
        BEFORE this ``--agent`` spawn or a later ``set_mode`` fails with "Mode
        '<agent>' not found". Regenerate it if missing, best-effort — a spec that
        cannot be written is not a reason to refuse the spawn, and a non-managed
        agent cannot be materialized here at all (the ``create_session`` guard
        fails those closed instead).

        The API key is re-injected from the data home's ``.env`` because a
        post-scrub Docker image has stripped it from the gateway's own
        environment; only kiro-cli gets it, which is why this overrides the
        base's strip rather than adding to it.
        """
        # Deferred for the same cycle reason as the base implementation.
        from kiro_crew.agent import ensure_agent_materialized
        from kiro_crew.config.loader import inject_kiro_cli_api_key

        try:
            ensure_agent_materialized(agent)
        except Exception:
            logger.warning("pre-spawn agent materialization failed", exc_info=True)
        inject_kiro_cli_api_key(env)

    @property
    def protocol_profile(self) -> ProtocolProfile:
        """kiro-cli's wire (:data:`KIRO_PROFILE`).

        Date-string protocol version, kiro-style permission options,
        ``session/set_model`` + ``set_mode`` (no ``session/set_config_option``),
        and no dedicated thought-chunk update type.
        """
        return KIRO_PROFILE


class KasAdapter(HarnessAdapter):
    """KAS: the v3 engine reached through kiro-cli's own ACP relay.

    The relay means KAS is not a separate process tree Crew assembles — it is a
    kiro-cli invoked with different arguments. So resolution is kiro-cli's, and
    the only thing that cannot be said as data is the argv, which
    :mod:`kiro_crew.acp.kas_transport` owns so that the engine and auth-owner
    flags live beside the reasoning for each.

    ``pre_spawn`` is deliberately NOT overridden, so KAS takes the base's strip of
    ``KIRO_API_KEY``: that variable is kiro-cli's credential for its own v2 agent
    loop, and the v3 engine authenticates from the OIDC store via
    ``--auth-method cli`` instead. Handing it over would widen credential exposure
    for a consumer that does not read it.
    """

    name = ADAPTER_KAS

    def resolve_executable(self, descriptor: HarnessDescriptor) -> tuple[str, str]:
        """Resolve kiro-cli, since the relay IS kiro-cli.

        Shared with :class:`KiroAdapter` rather than reimplemented: both harnesses
        must honour the same operator override, fixed install locations, and
        augmented PATH, and a second copy of that chain is how one of them
        regresses to "not found in PATH" alone.
        """
        return _resolve_kiro_cli_candidate(descriptor)

    def render_argv(
        self,
        descriptor: HarnessDescriptor,
        *,
        executable: str,
        agent: str = "",
        model: str = "",
        workdir: str | os.PathLike[str] = "",
    ) -> list[str]:
        """``kiro-cli acp --agent-engine v3 --auth-method cli``.

        ``executable`` is the ATTESTED kiro-cli path, so it lands as ``argv[0]``
        and the whole command is anchored on bytes that were vetted. Neither the
        agent nor the model appears: KAS takes custom agents over the wire in
        ``session/new`` (``_meta.kiro.customAgents``) and its model is chosen per
        session, so pinning either at process start would apply it to every
        session on the process.
        """
        # Deferred to keep this module's imports off the KAS wire modules, which
        # reach the ACP types; the registry side imports this one.
        from kiro_crew.acp.kas_transport import build_kas_argv

        return build_kas_argv(executable)

    @property
    def protocol_profile(self) -> ProtocolProfile:
        """The KAS relay's wire (:data:`KAS_PROFILE`).

        kiro-cli's dialect in every respect — the relay IS a kiro-cli — EXCEPT
        the ``initialize`` ``protocolVersion``, which the relay expects as the
        public-ACP integer ``1`` (``runtime.py``'s ``PROTOCOL_VERSION_KAS``), not
        kiro-cli's date string. Returning :data:`KIRO_PROFILE` here would flip
        that handshake byte the moment a client/runtime site reads the profile
        instead of forking on the backend, so KAS carries its own constant.
        """
        return KAS_PROFILE


class ClaudeAdapter(HarnessAdapter):
    """The Claude Code seam.

    Registered so the harness is nameable and its capabilities readable. After
    upstream #7301 the public build genuinely serves it (``acp/client.py`` owns the
    spawn path and the adapter is a public npm package), so nothing here overrides
    the generic rules. Its live effort push runs in ``providers.acp`` after the
    session is ready, which is not a spawn step and does not belong here.
    """

    name = ADAPTER_CLAUDE

    @property
    def protocol_profile(self) -> ProtocolProfile:
        """The public ACP wire (:data:`STANDARD_ACP_PROFILE`).

        Stated explicitly (though identical to the base) so the claude seam's
        wire is pinned as data rather than inherited by accident: integer
        protocol version, standard permission options,
        ``session/set_config_option`` for model/effort, and
        ``agent_thought_chunk`` reasoning updates.
        """
        return STANDARD_ACP_PROFILE


class GenericAdapter(HarnessAdapter):
    """Any harness that speaks standard ACP over stdio, from data alone (R2).

    This is what every adapter-less descriptor resolves to — bundled Codex and
    every operator harness — so adopting a new provider's ACP server is config,
    not a code PR. It carries no harness-specific behaviour: ``render_argv`` is
    the base's pure template rendering (``agent_args`` only when an agent is
    passed, ``model_args`` only when a model is pinned, no hardcoded flags), and
    ``pre_spawn`` is the base's strip of kiro-cli's own API key. The one thing it
    changes from the base is executable resolution, below.
    """

    name = ADAPTER_GENERIC

    @property
    def protocol_profile(self) -> ProtocolProfile:
        """The public ACP wire (:data:`STANDARD_ACP_PROFILE`).

        Stated explicitly (though identical to the base) so the wire dialect for
        every operator harness and bundled Codex is pinned as data rather than
        inherited by accident — the same discipline :class:`ClaudeAdapter` keeps.
        A generic stdio ACP harness speaks the public wire: integer protocol
        version, standard permission options, ``session/set_config_option`` for
        model/effort, and ``agent_thought_chunk`` reasoning updates.
        """
        return STANDARD_ACP_PROFILE

    def resolve_executable(self, descriptor: HarnessDescriptor) -> tuple[str, str]:
        """``(path, reason)`` for the descriptor's executable; one is always empty.

        An absolute path is honoured as given — it must still be an executable,
        non-empty file, or the reason names the defect. A BARE NAME (no path
        separator) is resolved through PATH, but through the SAME augmented PATH
        the kiro chain uses (:func:`kiro_crew.env.augmented_path`), not the
        process's inherited PATH: a gateway launched under systemd or another
        non-login shell rarely inherits ``~/.local/bin`` and its kin, so a plain
        ``shutil.which`` would report "not found" for an operator harness
        installed exactly where the kiro binary is found. Reusing the shared
        helper keeps the two paths in sync rather than copying its directory list
        here.

        A RELATIVE path (one that contains a separator but is not absolute, e.g.
        ``./bin/agy`` or ``bin/agy``) is refused with a reason that names the real
        rule. ``shutil.which`` short-circuits such a candidate against the
        process's own working directory and discards the ``path`` argument
        entirely, so the augmented PATH would never be consulted and the resolved
        file would depend on wherever the gateway happens to be running (under
        systemd, ``WorkingDirectory`` or ``/`` — nothing the operator chose).
        Refusing keeps the diagnostic honest instead of reporting a PATH miss for
        a PATH that was never searched.
        """
        candidate = descriptor.executable
        if not candidate:
            return "", "no executable is declared"
        if os.path.isabs(candidate):
            resolved = candidate
        elif os.path.dirname(candidate):
            # Relative path with a separator: shutil.which would resolve it
            # against the gateway's cwd and ignore the augmented PATH, so the
            # bytes that ran would depend on where the gateway was launched.
            return "", (
                f"{candidate!r} is a relative path; it would be resolved against "
                f"the gateway's working directory, not the session workdir or the "
                f"augmented PATH — give an absolute path or a bare name on PATH"
            )
        else:
            found = shutil.which(candidate, path=augmented_path(os.environ.get("PATH", "")))
            if not found:
                return "", f"{candidate!r} was not found on PATH"
            resolved = found
        problem = _candidate_problem(resolved)
        if problem:
            return "", problem
        return resolved, ""


_GENERIC_ADAPTER = GenericAdapter()

#: Adapter per ``ADAPTER_*`` name. A descriptor with no adapter — every operator
#: harness, and bundled Codex — resolves to the generic adapter, so "has an
#: adapter" is never a branch a caller has to write.
_ADAPTERS: Mapping[str, HarnessAdapter] = {
    ADAPTER_KIRO: KiroAdapter(),
    ADAPTER_KAS: KasAdapter(),
    ADAPTER_CLAUDE: ClaudeAdapter(),
    ADAPTER_GENERIC: _GENERIC_ADAPTER,
}


def adapter_for(descriptor: HarnessDescriptor) -> HarnessAdapter:
    """The adapter serving ``descriptor``, never None.

    A descriptor that names no adapter — every operator harness, and bundled
    Codex — resolves to :class:`GenericAdapter`, the standard-ACP-over-stdio rule
    (R1.2). A named adapter selects its bespoke instance. An unknown adapter name
    cannot reach here (``validate_descriptor`` closes the vocabulary and an
    operator descriptor cannot carry the field at all), so the generic fallback
    also covers a bundled descriptor mid-rename rather than throwing during a
    spawn.
    """
    if not descriptor.adapter:
        return _GENERIC_ADAPTER
    return _ADAPTERS.get(descriptor.adapter, _GENERIC_ADAPTER)


def resolve_executable(descriptor: HarnessDescriptor) -> tuple[str, str]:
    """``(path, reason)`` for ``descriptor``'s executable; one is always empty.

    The listing-side entry point: resolution only, nothing executed and nothing
    attested (see :meth:`HarnessAdapter.resolve_executable`).
    """
    return adapter_for(descriptor).resolve_executable(descriptor)


def attest_executable(harness_id: str, resolved: str) -> str:
    """The launch path for ``resolved``, or raise :class:`HarnessExecutableTrustError`.

    The Trust_Attestation every harness takes: the candidate must be a runnable,
    non-zero-byte file, and the path returned is the one to exec — anchored with
    ``abspath`` rather than ``realpath`` so a multiplexer launcher dispatching on
    its own ``argv[0]`` and a multi-call binary resolving a sibling subcommand
    both still work.

    Applied to bundled and operator harnesses alike (R2.2). Uniformity is the
    requirement: gating only the harness that shipped first would leave every
    later one exec'ing whatever a config value pointed at.
    """
    # Deferred: kiro_prerequisite imports the sandbox helpers, which import back
    # into this side of the package.
    from kiro_crew.kiro_prerequisite import snapshot_trusted_acp_executable

    try:
        if platform_compat.IS_WINDOWS:
            snapshot = snapshot_trusted_acp_executable(
                resolved,
                platform_name="win32",
                environ=os.environ,
            )
        else:
            snapshot = snapshot_trusted_acp_executable(resolved)
    except OSError as exc:
        raise HarnessExecutableTrustError(f"harness {harness_id!r}: {exc}") from exc
    except ValueError as exc:
        # The snapshot's own refusal text names Kiro CLI, because its other
        # caller is the kiro-only client path. Restating the verdict against the
        # candidate keeps an operator harness's refusal about THAT harness:
        # "harness 'mine': Kiro CLI is not a runnable executable" sends the
        # operator to reinstall a tool that is not the one that failed. The
        # shared candidate verdict names the actual defect where it is still
        # observable, and the original text stays on the chained cause.
        problem = _candidate_problem(resolved) or (
            f"{resolved} is not a runnable executable for ACP execution"
        )
        raise HarnessExecutableTrustError(f"harness {harness_id!r}: {problem}") from exc
    return snapshot.launch_path


def resolve_spawn_executable(descriptor: HarnessDescriptor) -> str:
    """Resolve and attest ``descriptor``'s executable, returning the path to exec.

    Blocking (``shutil.which``, ``stat``, and the attestation's own reads), so
    callers on the event loop run it in a worker thread.
    """
    resolved, reason = resolve_executable(descriptor)
    if reason:
        raise HarnessSpawnRefused(f"harness {descriptor.id!r} cannot start: {reason}")
    return attest_executable(descriptor.id, resolved)


def checked_spawn_argv(descriptor: HarnessDescriptor, argv: list[str], attested: str) -> list[str]:
    """``argv``, once it is proven to exec the attested executable.

    This is the second half of attestation, and without it the first half is
    decoration: a template that never substitutes ``{executable}`` leaves a bare
    name in ``argv[0]``, which ``exec`` then resolves through ``PATH`` at spawn
    time — so the bytes that were vetted and the bytes that run need not be the
    same file. Validation already requires the template to start with
    ``{executable}``; this catches the other producer, an adapter's own
    ``render_argv`` override, where no template is involved at all.

    Refuses rather than rewriting ``argv[0]``: a silent rewrite would make a
    descriptor or adapter bug indistinguishable from a correct spawn, and the
    mismatch is always a bug in Kiro Crew's own data rather than something an
    operator can act on.
    """
    if not argv:
        raise HarnessSpawnRefused(
            f"harness {descriptor.id!r} rendered an empty argv, so there is " f"nothing to exec"
        )
    if argv[0] != attested:
        raise HarnessSpawnRefused(
            f"harness {descriptor.id!r} would exec {argv[0]!r} but "
            f"{attested!r} is the executable that was attested; refusing to "
            f"spawn bytes that were never checked"
        )
    return argv
