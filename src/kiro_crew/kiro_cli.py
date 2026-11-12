"""Side-effect-free Kiro CLI discovery shared by setup and ACP launch paths."""

from __future__ import annotations

import os
import re
import stat
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import identity_stores, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.env import augmented_path
from kiro_crew.kiro_cli_bundle import (  # noqa: F401 - re-exported surface
    BUNDLED_KIRO_CLI_ENTRY,
    BUNDLED_KIRO_CLI_WINDOWS_ENTRY,
    BUNDLED_KIRO_DIR_ENV,
    bundled_kiro_cli_dir,
    bundled_kiro_cli_entry,
    is_bundled_kiro_cli,
)

if TYPE_CHECKING:
    from kiro_crew.kiro_prerequisite import ProcessResult

KIRO_CLI_NAME = "kiro-cli"


def chat_sibling(binary: str, platform_name: str | None = None) -> str | None:
    """The ``kiro-cli-chat`` beside a resolved ``kiro-cli`` launcher, or ``None``.

    The POSIX ``kiro-cli`` is the q_cli launcher (install, update, doctor,
    login, shell completion). It ``exec``s ``kiro-cli-chat`` for ``chat`` and
    ``acp`` -- but only after checking ITS OWN sign-in, so ``kiro-cli acp
    --agent-engine v3`` with no ``--auth-method cli`` exits ``You are not logged
    in`` on a host where kiro-cli is signed out, even though that mode leaves the
    credential to the ACP client and ``kiro-cli-chat acp --agent-engine v3`` runs
    (verified on 2.25.0: the chat binary starts KAS in ``--auth=acp-callback``
    and asks the host). A KAS spawn that Crew's vault is about to authenticate
    therefore has to enter through the chat binary, which is what the desktop
    bundle already does (:data:`BUNDLED_KIRO_CLI_ENTRY`).

    ``None`` whenever the swap does not apply, so callers fall back to *binary*
    unchanged: on Windows (one self-contained ``kiro-cli.exe``), when *binary*
    is not named ``kiro-cli`` (the operator's ``KIROCREW_KIRO_BIN`` may name
    anything; the bundled entry is already the chat binary), or when no
    non-empty executable ``kiro-cli-chat`` sits in the SAME directory. The directory is the
    resolved path's own, never its ``realpath``: the launch-in-place rule
    (acp-client spec) means a symlinked ``~/.local/bin/kiro-cli`` is joined by
    ``~/.local/bin/kiro-cli-chat`` when the install shipped both, and a wrapper
    directory without one keeps the wrapper. Pure ``stat`` work, no spawn.
    """
    resolved_platform = platform_name or sys.platform
    if resolved_platform == "win32" or not binary:
        return None
    if os.path.basename(binary) != KIRO_CLI_NAME:
        return None
    sibling = os.path.join(os.path.dirname(binary), BUNDLED_KIRO_CLI_ENTRY)
    if not platform_compat.is_executable_file(sibling, platform_name=resolved_platform):
        return None
    # An executable that is empty cannot be exec'd (ENOEXEC) and would fail the
    # spawn loudly where the launcher might still have run; the same guard the
    # win32 candidate walk applies to App-Execution-Alias stubs.
    try:
        if os.path.getsize(sibling) == 0:
            return None
    except OSError:
        return None
    return sibling


# kiro-cli's own local state database. Holds identity-describing rows next to
# credential rows, so every reader here is read-only and key-scoped. Alias of
# the single canonical filename constant so the six former copies cannot drift.
KIRO_CLI_STATE_DB = identity_stores.AUTH_SQLITE_DB

# Non-secret rows kiro-cli writes when the signed-in identity came from IAM
# Identity Center. Presence is the whole signal: the values (a start URL and a
# region) are never returned, and no token key is selected.
_IDC_STATE_KEYS = ("auth.idc.start-url", "auth.idc.region")

# Name only. Governance covers API-key sign-ins too, so its presence decides
# whether an admin registry can apply; the value is never read.
_API_KEY_ENV = "KIRO_API_KEY"

# SEL audit id for the identity probe below. Registered in
# ``hooks._AUDIT_ONLY_READ_IDS``; an unregistered id fails closed.
_IDC_PROBE_READ_ID = "kiro_cli.idc_identity_probe"

_STATE_DB_TIMEOUT_SECS = 5.0

#: Lowest kiro-cli release observed to ACCEPT a top-level ``permissions`` block in
#: an agent spec.
#:
#: kiro-cli validates agent specs with serde ``deny_unknown_fields``, so a release
#: whose schema lacks the field does not ignore it -- it refuses the WHOLE file,
#: drops the agent from its table, and every Kiro Crew MCP server is silently
#: absent from the session while ``--agent kirocrew`` resolves to the default
#: agent. Observed refusing on 2.10.0 and accepting on 2.23.0.
#:
#: The floor is the release actually PROBED, for the same reason
#: :data:`~kiro_crew.mcp_hot_reload.MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION` states:
#: lowering it once an older release is verified is a one-line change, while
#: granting it to a release that refuses the field costs the user every tool with
#: nothing red to say why.
#:
#: What authorizes lowering it: install the candidate release (a 2.x between
#: 2.10.0 and 2.23.0), put a spec carrying ``"permissions": {"rules": []}`` in
#: its agents directory, start a session with ``--agent`` naming that spec, and
#: confirm the agent resolves -- its MCP servers present, no ``unknown field
#: 'permissions'`` in the log. The floor becomes the lowest release that passes.
#: A changelog entry is not a probe; a release nobody ran stays above the floor.
SPEC_PERMISSIONS_MIN_VERSION: tuple[int, int, int] = (2, 23, 0)

#: Lowest kiro-cli release where a Crew-owned KAS relay spawn passes
#: ``--auth-method client``.
#:
#: From this release an omitted ``--auth-method`` means ``cli``: kiro-cli answers
#: the engine's ``_kiro/auth/getAccessToken`` from its own store, so Crew's vault
#: is never asked. ``client`` is what hands that request to Crew. Observed: the
#: no-flag default is client-owned on 2.35.1 and cli-owned on 2.35.2, 2.37.1 and
#: 3.0.0. ``client`` parses on 2.35.2; on 2.37.1 and 3.0.0, signed out, it also
#: forwards the request to the ACP client. Forwarding with kiro-cli signed in has
#: not been measured.
#:
#: The floor errs low on purpose, and an unknown version is treated as at or
#: above it. A release that lacks the value refuses the flag at argument parse,
#: before ``initialize``, so a gate that is too generous fails closed. A gate that
#: is too strict keeps the flag off a release that needs it, and turns run on
#: kiro-cli's own account instead of the vault's.
KAS_AUTH_CLIENT_MIN_KIRO_CLI: tuple[int, int, int] = (2, 35, 2)

#: Lowest verified release for both kiro and KAS where deferred Crew tools avoid
#: the thinking-signature rejection. Older or unknown engines keep Crew tools resident.
#: Empirical, not a named upstream fix: thinking turns before and after a deferred
#: kirocrew-* load ran clean on the default engine at 2.26.1 and 2.27.0 and on KAS
#: (``acp --agent-engine v3``) at 2.27.0. Lowering it needs that same matrix re-run
#: on both engines at the lower release.
MANDATORY_MCPS_DROP_MIN_VERSION: tuple[int, int, int] = (2, 27, 0)

#: First kiro-cli release with no launcher. From 3.0.0 the POSIX install is the
#: standalone archive: ``bin/kiro-cli`` IS the chat binary, with no
#: ``kiro-cli-chat`` beside it, and no newer launcher bundle ships (kiro-cli
#: ``scripts/build.py`` ``build_user_archives``; ``cutover.rs`` ``release_answer``).
STANDALONE_KIRO_CLI_MIN_VERSION: tuple[int, int, int] = (3, 0, 0)

#: ``--version`` is a local read of an already-resolved binary, so it gets the
#: same short leash the readiness probe puts on its own first execution.
_VERSION_PROBE_TIMEOUT_SECS = 5

#: One answer per binary IDENTITY, not per process: keyed by the pinned path and
#: its mtime, so ``kiro-cli update`` swapping the binary invalidates the entry
#: instead of leaving a whole gateway lifetime on a stale verdict.
_version_cache: dict[tuple[str, int], tuple[int, int, int] | None] = {}


def _sandboxed_probe(binary: str, args: Sequence[str], timeout_secs: float) -> ProcessResult | None:
    """Run one fixed-argv kiro-cli probe inside the sandbox; ``None`` if it could not run.

    The pinned install can sit in a directory the sandboxed agent writes as the same
    account (``~/.local/bin``), so a probe that ran it outside the sandbox would run
    whatever an agent left there as the gateway itself. The probe goes through
    ``kiro_prerequisite._run_process`` instead, at the configured tier, as an agent
    session runs the same binary and as ``agent_sdk.drivers.acp._sandboxed_acp_help``
    runs a ``PATH`` hit. ``first_party_fixed_argv``: the argv is fixed and the binary
    is :func:`pin_kiro_cli`'s, so a host with no sandbox backend still runs the probe
    unconfined, as before. Called on a running event loop, the probe runs in a worker
    thread and blocks the caller as the plain spawn did. Never raises.
    """
    # Imported here: this module is a leaf every setup and launch path imports at boot.
    import asyncio  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from kiro_crew.kiro_prerequisite import _run_process  # noqa: PLC0415
    from kiro_crew.sandbox import (  # noqa: PLC0415
        configured_sandbox_mode,
        scrub_agent_subprocess_env,
    )

    try:
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            # Main's plain spawn failed here; the runner's ``env`` hop would instead
            # exit 127 with a line that reads like the child's own answer.
            return None
        probe = _run_process(
            binary,
            list(args),
            # Scrubbed as for a session spawn: on Windows the runner skips the sandbox,
            # so its own launcher scrub never runs.
            env=scrub_agent_subprocess_env(dict(os.environ)),
            timeout_secs=timeout_secs,
            sandbox_mode=configured_sandbox_mode(),
            first_party_fixed_argv=True,
            reap_survivors=True,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(probe)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, probe).result()
    except Exception:  # noqa: BLE001 - a probe that could not run is unknown
        return None


def _probe_ran(result: ProcessResult) -> bool:
    """Whether *result* is the child's own whole answer.

    False for a spawn, sandbox or timeout failure, and for output that filled the
    capture bound, which may have lost the line a caller looks for.
    """
    from kiro_crew.kiro_prerequisite import _MAX_CAPTURED_OUTPUT  # noqa: PLC0415

    return (
        result.returncode is not None
        and not result.timed_out
        and result.sandbox_failure is None
        and len(result.output) < _MAX_CAPTURED_OUTPUT
    )


def mandatory_mcps_drop_supported(version: tuple[int, int, int] | None) -> bool:
    """Whether *version* meets the verified floor for deferring Crew's MCP servers."""
    return version is not None and version >= MANDATORY_MCPS_DROP_MIN_VERSION


def kas_auth_client_flag_supported(version: tuple[int, int, int] | None) -> bool:
    """Whether a Crew-owned KAS relay spawn on *version* passes ``--auth-method client``.

    True unless *version* is known and below the floor. An unknown version is
    True: a release that lacks the value stops at argument parse, which fails
    closed, while an omitted flag on a release at or above the floor runs on
    kiro-cli's own credential.
    """
    return version is None or version >= KAS_AUTH_CLIENT_MIN_KIRO_CLI


def standalone_kiro_cli(version: tuple[int, int, int] | None) -> bool:
    """Whether a ``kiro-cli`` at *version* is the standalone chat binary, not a launcher."""
    return version is not None and version >= STANDALONE_KIRO_CLI_MIN_VERSION


def mandatory_mcps_drop_allowed(spawned_binary: str | None) -> bool:
    """Allow deferral only for the pinned install and its own chat sibling.

    The executable must match the pin or its chat sibling by realpath, and its
    invoked directory must match the pin's directory. Both pinned executables
    must meet the version floor; only those paths are probed. Missing spawn
    paths, pod bundles, and non-pinned launchers keep Crew's servers resident.
    Every check runs on the executables the sandbox wrap will run: the spawned
    path and the pin both pass through the edition's agent-executable resolver
    first, so a managed launcher is judged by the install it is swapped for,
    never by the launcher itself. Any failure keeps the servers resident; never raises.

    An operator ``KIROCREW_KIRO_BIN`` override also keeps them resident: its
    directory is the operator's choice, not a known install directory, so no path
    derived from it is probed (the KAS spawn skips that sibling for the same reason).
    """
    if not spawned_binary or (os.environ.get("KIROCREW_KIRO_BIN") or "").strip():
        return False
    try:
        pinned = pin_kiro_cli()[0]
        if pinned is None:
            return False
        # The same resolver sandbox_argv applies to argv[0] before it execs: an
        # edition may swap a managed launcher (one with no kiro-cli-chat beside it)
        # for the direct install behind it. Identity on the public build.
        from kiro_crew.sandbox_argv import _resolve_agent_executable  # noqa: PLC0415

        pinned = _resolve_agent_executable(pinned)
        spawned_binary = _resolve_agent_executable(spawned_binary)
        if not pinned or not spawned_binary:
            return False
        sibling = chat_sibling(pinned)
        candidates = [pinned, sibling]
        if os.path.normcase(os.path.realpath(spawned_binary)) not in {
            os.path.normcase(os.path.realpath(binary))
            for binary in candidates
            if binary is not None
        } or os.path.normcase(
            os.path.realpath(os.path.dirname(spawned_binary))
        ) != os.path.normcase(
            os.path.realpath(os.path.dirname(pinned))
        ):
            return False
        # A POSIX launcher with no sibling in its own directory execs whatever
        # kiro-cli-chat it finds on PATH, whose version is unknown. From 3.0.0
        # no launcher ships: kiro-cli is the chat binary itself, alone.
        if (
            sibling is None
            and sys.platform != "win32"
            and os.path.basename(pinned) == KIRO_CLI_NAME
            and not standalone_kiro_cli(kiro_cli_version_at(pinned))
        ):
            return False
        return all(
            mandatory_mcps_drop_supported(kiro_cli_version_at(binary))
            for binary in candidates
            if binary is not None
        )
    except Exception:  # noqa: BLE001 - an unanswerable trust check refuses deferral
        return False


def spec_permissions_supported(version: tuple[int, int, int] | None) -> bool:
    """Pure gate: does a kiro-cli at *version* accept a spec ``permissions`` block?

    An unknown version is not "probably new enough": it is False. The two losses
    are not symmetric. Writing the field on a release that refuses it costs the
    whole spec -- no MCP servers, no tools, no Kiro Crew agent at all.
    Withholding it costs the KAS mode listing for that agent. So the unknown case
    takes the smaller loss.
    """
    if version is None:
        return False
    return version >= SPEC_PERMISSIONS_MIN_VERSION


def installed_kiro_cli_version() -> tuple[int, int, int] | None:
    """The pinned kiro-cli's version, or ``None`` when it cannot be established.

    Blocking: one bounded ``--version`` spawn on the first call per binary
    identity, cached by path and mtime thereafter. The binary comes from
    :func:`pin_kiro_cli` -- an absolute path from the known install directories
    with the inherited ``PATH`` excluded -- because a bare argv0 would be
    re-resolved inside ``exec`` against a ``PATH`` that can lead with an
    agent-writable directory. No pin means no spawn and no answer.

    Never raises. Every failure (absent binary, refused spawn, timeout,
    unparseable output) gives the same answer as an old CLI: unknown, which the
    gate reads as "do not write the field".
    """
    try:
        binary, _unpinned = pin_kiro_cli()
    except Exception:  # noqa: BLE001 - an unanswerable probe is not an error here
        return None
    return kiro_cli_version_at(binary) if binary is not None else None


def kiro_cli_version_at(binary: str) -> tuple[int, int, int] | None:
    """Probe a resolved executable, cached by path and mtime; failure returns None.

    Call only on a path derived from :func:`pin_kiro_cli`.
    """
    # The ``--version`` line and the handshake's ``agentInfo.version`` are the
    # same spelling, so the parser is shared rather than copied. Imported here
    # because ``mcp_hot_reload`` reaches ``acp_backends``, and this module is a
    # leaf every setup and launch path imports at boot.
    from kiro_crew.mcp_hot_reload import parse_kiro_cli_version  # noqa: PLC0415

    try:
        key = (binary, os.stat(binary).st_mtime_ns)
    except OSError:
        return None
    if key in _version_cache:
        return _version_cache[key]
    result = _sandboxed_probe(binary, ["--version"], _VERSION_PROBE_TIMEOUT_SECS)
    if result is not None and result.sandbox_failure is not None:
        # Not cached: a sandbox that could not be built says nothing about the binary.
        return None
    version: tuple[int, int, int] | None = None
    if result is not None and result.ok:
        # stdout and stderr arrive as one text, so each line is read on its own: a
        # line after the version would otherwise hide it from the last-token parse.
        version = next(
            (v for line in result.output.splitlines() if (v := parse_kiro_cli_version(line))),
            None,
        )
    _version_cache[key] = version
    return version


#: Ceiling on the ``acp --help`` spawn. Help output is local and immediate; the cap
#: only bounds a wedged binary.
_KAS_HELP_PROBE_TIMEOUT_SECS = 15


def kas_relay_help(binary: str) -> str | None:
    """``acp --help`` text for this kiro-cli, or ``None`` when the probe FAILED.

    Read from help output because there is no machine-readable capability surface
    for the engine selector. ``None`` means only one thing — the probe could not
    run (spawn error, timeout) — so the caller reports genuinely-unknown as
    unknown. Help text that RAN and simply lacks the engine selector is returned
    as-is, not as ``None``: a kiro-cli too old to offer ``--agent-engine`` cannot
    serve KAS at all, and reporting that as "unknown" would let a broken
    configuration pass the readiness check and fail later at spawn instead.

    Call only on :func:`pin_kiro_cli`'s install, or from ``kirocrew doctor`` on the
    binary it resolved. The probe runs inside the sandbox (:func:`_sandboxed_probe`).
    Local binary, argv list, no shell, no credential involved.
    """
    result = _sandboxed_probe(binary, ["acp", "--help"], _KAS_HELP_PROBE_TIMEOUT_SECS)
    if result is None or not _probe_ran(result):
        return None
    return result.output


#: Ceiling on the v3 start probe. With stdin closed a v3 engine that runs here
#: exits on its own once it reads EOF: about ten seconds measured on kiro-cli 3.1.0,
#: most of it kiro-cli's own start-up work before the engine runs, which is the
#: path its refusal comes from too. The cap bounds only a start that never exits.
_KAS_START_PROBE_TIMEOUT_SECS = 30
#: Start-probe answers, keyed by (path, mtime, argv) like the version cache.
_kas_start_cache: dict[tuple[str, int, tuple[str, ...]], str] = {}


def kas_relay_start_output(binary: str, argv_tail: Sequence[str]) -> str | None:
    """Output of one ``binary *argv_tail`` start with stdin closed, or ``None`` when it FAILED.

    The KAS engine check behind ``agent_sdk.kas_engine_start_verdict``: kiro-cli's
    ``acp --help`` lists ``--agent-engine v3`` even on a host where starting that
    engine is refused, so only a start shows the refusal. It runs through
    :func:`_sandboxed_probe`, inside the sandbox at the configured tier: the child
    reads EOF on stdin and serves nothing, its output is bounded, and it leads its
    own process group under the supervisor, so a start still running at
    :data:`_KAS_START_PROBE_TIMEOUT_SECS` is ended with the engine it launched.
    ``None`` means only that nothing is known: the spawn failed, the sandbox
    refused it, or the start timed out. Cached by path, mtime and argv, so an
    upgrade in place is re-read.

    Call only on :func:`pin_kiro_cli`'s install, or from ``kirocrew doctor`` on the
    binary its ``acp --help`` row already ran.
    Local binary, argv list, no shell, and Kiro Crew passes and reads no credential.
    Blocking: one bounded spawn on the first call per binary identity.
    """
    argv = tuple(argv_tail)
    try:
        key = (binary, os.stat(binary).st_mtime_ns, argv)
    except OSError:
        return None
    cached = _kas_start_cache.get(key)
    if cached is not None:
        return cached
    result = _sandboxed_probe(binary, argv, _KAS_START_PROBE_TIMEOUT_SECS)
    if result is None or not _probe_ran(result):
        # Not cached: a start that failed, was refused or never exited says nothing
        # about the host.
        return None
    _kas_start_cache[key] = result.output
    return result.output


#: Ceiling on the ``chat --help`` spawn, for the same reason as the ``acp --help`` one.
_CHAT_HELP_PROBE_TIMEOUT_SECS = 15
#: ``chat --help`` engine answers, keyed by (path, mtime) like the version cache.
_chat_engines_cache: dict[tuple[str, int], tuple[str, ...] | None] = {}
_POSSIBLE_VALUES_RE = re.compile(r"\[possible values:\s*([^\]]*)\]")


def chat_agent_engines(help_text: str) -> tuple[str, ...]:
    """The engines ``chat --help`` lists for ``--agent-engine``, in its own order.

    Empty when the help names no ``--agent-engine`` selector at all (a kiro-cli
    older than the selector). The values are read from the clap
    ``[possible values: ...]`` line that follows the flag, not from its prose,
    because the prose names the default engine and that changes between releases.
    """
    at = help_text.find("--agent-engine")
    if at < 0:
        return ()
    # The selector's own block ends where the next long option begins.
    end = help_text.find("\n      --", at + 1)
    block = help_text[at : end if end > 0 else len(help_text)]
    match = _POSSIBLE_VALUES_RE.search(block)
    if match is None:
        return ()
    return tuple(v.strip() for v in match.group(1).split(",") if v.strip())


def chat_agent_engines_at(binary: str) -> tuple[str, ...] | None:
    """The engines this kiro-cli's ``chat --agent-engine`` accepts, or ``None`` if unknown.

    ``()`` means the probe ran and the binary has no engine selector; ``None``
    means the probe could not run (absent binary, spawn error, timeout, nonzero
    exit). Cached by path and mtime, so an upgrade in place is re-read.

    Call only on a path derived from :func:`pin_kiro_cli`. The probe runs inside the
    sandbox (:func:`_sandboxed_probe`). Blocking: one bounded spawn on the first call
    per binary identity. Local binary, argv list, no shell, no credential involved.
    """
    try:
        key = (binary, os.stat(binary).st_mtime_ns)
    except OSError:
        return None
    if key in _chat_engines_cache:
        return _chat_engines_cache[key]
    result = _sandboxed_probe(binary, ["chat", "--help"], _CHAT_HELP_PROBE_TIMEOUT_SECS)
    if result is not None and result.sandbox_failure is not None:
        # Not cached: a sandbox that could not be built says nothing about the binary.
        return None
    engines: tuple[str, ...] | None = None
    if result is not None and _probe_ran(result) and result.ok:
        engines = chat_agent_engines(result.output)
    _chat_engines_cache[key] = engines
    return engines


#: The engine the Kiro CLI harness drives. Passed explicitly because kiro-cli's
#: own default is not a contract: 3.0.0's ``acp --help`` says "v2 when neither is
#: set", yet a bare ``acp`` there answers ``initialize`` with the v3 capability
#: block (``_meta.kiro.extensionMethods``), while 2.37.1 answers bare with v2.
KIRO_ACP_ENGINE = "v2"
#: ``acp --help`` engine answers, keyed by (path, mtime) like the chat cache.
_acp_engines_cache: dict[tuple[str, int], tuple[str, ...]] = {}


def _is_pinned_kiro_cli(binary: str) -> bool:
    """Whether *binary* is :func:`pin_kiro_cli`'s own install. Stats only; never raises."""
    try:
        pinned, _unpinned = pin_kiro_cli()
        return pinned is not None and os.path.samefile(pinned, binary)
    except (OSError, ValueError):
        return False


def kiro_acp_engine_args(binary: str) -> list[str]:
    """``["--agent-engine", "v2"]`` when this kiro-cli's ``acp`` lists v2, else ``[]``.

    The argv tail that pins the Kiro CLI harness to the engine it speaks, the
    same way the KAS relay pins ``v3`` (``acp.kas_transport``). Empty when the
    binary is absent or not the pinned install, the probe fails, or the help
    lists no v2: an unknown flag
    or value would fail the whole spawn, which is worse than an unpinned one.
    Cached by path and mtime, so an upgrade in place is re-read.

    Call only on a trusted, resolved kiro-cli path. The probe
    (:func:`kas_relay_help`) runs inside the sandbox. Blocking: one bounded spawn
    on the first call per binary identity.
    """
    try:
        key = (binary, os.stat(binary).st_mtime_ns)
    except OSError:
        return []
    engines = _acp_engines_cache.get(key)
    if engines is None:
        if not _is_pinned_kiro_cli(binary):
            # A kiro-cli reached only through PATH may sit in an agent-writable
            # directory; the session runs it inside the sandbox, but this probe
            # would not. shortcut: such a launch stays bare (the pre-pin argv);
            # upgrade by running the probe under the sandbox.
            return []
        # The KAS floor's own ``acp --help`` probe; the clap ``[possible values:
        # ...]`` reading is the one ``chat --help`` uses.
        help_text = kas_relay_help(binary)
        if help_text is None:
            # Not cached: a probe that could not run says nothing about the binary.
            return []
        engines = chat_agent_engines(help_text)
        _acp_engines_cache[key] = engines
    return ["--agent-engine", KIRO_ACP_ENGINE] if KIRO_ACP_ENGINE in engines else []


def kiro_cli_state_dbs(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    """Return candidate paths to kiro-cli's state database, most likely first.

    Mirrors the per-platform data directories the readiness probe stages from,
    including the ``XDG_DATA_HOME`` / ``LOCALAPPDATA`` redirections, so a host
    with a relocated data dir is not silently treated as having no store. Thin
    wrapper over :func:`identity_stores.state_db_candidates`, which owns the
    canonical per-platform table and the dedupe.
    """
    return identity_stores.state_db_candidates(platform_name, home, environ)


def api_key_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether an API-key credential is configured for kiro-cli.

    Only presence is inspected, never the value. Enterprise MCP governance
    applies to API-key sign-ins as well as IAM Identity Center, so a caller
    deciding whether governance *can* apply has to consider this too — treating
    an API-key account as ungoverned produces advice that breaks a correctly
    configured host.

    Reads the process environment, which by this point also carries anything the
    credential loader lifted out of Kiro Crew's ``.env`` -- except under the
    Docker scrub (``_KIROCREW_CREDS_SCRUBBED``), where the key stays only in
    ``.env``; :func:`_data_home_api_key_may_be_set` reads that file.
    """
    env = environ if environ is not None else os.environ
    return bool((env.get(_API_KEY_ENV) or "").strip())


def mcp_governance_may_apply(
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether an administrator's MCP registry can be in force for this identity.

    True for IAM Identity Center and for API-key sign-ins, the two the governance
    surface covers. False for Builder ID and social sign-ins, which
    organization-level MCP controls do not reach.

    Deliberately an OR of two independent signals rather than a single lookup: a
    host can be governed through either, and answering "ungoverned" for the one
    it does not check is what turns a diagnostic into bad advice.
    """
    return signed_in_via_idc(platform_name, home, environ) or api_key_configured(environ)


def _data_home_api_key_may_be_set() -> bool:
    """Whether the data home's ``.env`` holds ``KIRO_API_KEY``, or cannot be read.

    The Docker entrypoint moves the key into ``.env`` and scrubs it from the
    process environment (``_KIROCREW_CREDS_SCRUBBED``), and kiro-cli sessions
    get it back from there, so :func:`api_key_configured` alone misses the key
    those sessions sign in with. Only presence is inspected. An unreadable file
    proves nothing and answers True, the same rule
    :func:`kiro_crew.acp.kas_route.kiro_cli_signed_out` applies.
    """
    from kiro_crew.config.loader import CRED_KIRO_API_KEY, read_env_file_credential

    definitive: list[bool] = []
    try:
        if read_env_file_credential(CRED_KIRO_API_KEY, definitive=definitive).strip():
            return True
    except Exception:  # noqa: BLE001 - an unreadable answer proves nothing
        return True
    return definitive != [True]


# How long a "no admin registry can govern this sign-in" answer is reused. It is
# asked on every spec write and every kiro-cli session start while registry mode
# is declared, and the confirming ``whoami`` is a subprocess that can take
# seconds. The answer is also keyed on the sign-in's own traces (see
# :func:`_sign_in_fingerprint`), so a sign-in that touches kiro-cli's store or
# the API-key variable is seen at once rather than at the end of the window.
_GOVERNANCE_UNREACHABLE_TTL_SECS = 300.0
_governance_unreachable_cache: tuple[float, object, bool] | None = None
_governance_unreachable_lock = threading.Lock()


def _sign_in_fingerprint(
    home: Path | None = None, environ: Mapping[str, str] | None = None
) -> tuple[tuple[tuple[str, int, int] | None, ...], bool, bool]:
    """What changes when kiro-cli signs in or out: its store files and the API key.

    Only ``stat`` data and the key's presence, in the environment and in the
    data home's ``.env``, never a value. A sign-in writes
    ``data.sqlite3`` or its WAL, so a cached answer from before it does not
    match. Unreadable paths count as absent.
    """
    env = environ if environ is not None else os.environ
    stamps: list[tuple[str, int, int] | None] = []
    for db in kiro_cli_state_dbs(sys.platform, home if home is not None else Path.home(), env):
        for path in (db, db.with_name(db.name + "-wal")):
            try:
                st = os.stat(path)
            except OSError:
                stamps.append(None)
                continue
            stamps.append((str(path), st.st_mtime_ns, st.st_size))
    return (tuple(stamps), api_key_configured(env), _data_home_api_key_may_be_set())


def registry_governance_known_unreachable() -> bool:
    """True only when it is KNOWN that no admin MCP registry can govern this sign-in.

    Both signals must agree: :func:`mcp_governance_may_apply` finds no Identity
    Center rows and no API key, the data home's ``.env`` holds no API key either
    (:func:`_data_home_api_key_may_be_set`), AND ``kiro-cli whoami`` names a Builder ID or
    social account type. The store probe alone cannot decide it, because some
    kiro-cli builds sign an Identity Center user in without writing the rows it
    counts. Any doubt answers ``False``: a caller that drops the registry marker
    on this answer must never drop it on a governed account, where kiro-cli keeps
    only marked entries.

    The spec writers that ask this are reached from many synchronous paths, and
    a coroutine can sit above any of them. ``whoami`` can take seconds and runs
    under a process-wide lock, so on a thread running an event loop the probe
    never runs it and never waits for that lock: it answers from
    :func:`_governance_unreachable_on_loop`. The rule turns on the calling
    thread, not on a claim about the call graph, so a caller added later is
    covered too.
    """
    # Function-local: this module is imported by lightweight path-resolution
    # callers that must not pay for the atomic-write module's imports.
    from kiro_crew.atomic_write import on_event_loop

    if on_event_loop():
        return _governance_unreachable_on_loop()
    return _governance_unreachable_blocking()


def _governance_unreachable_blocking() -> bool:
    """The full probe, ``whoami`` included. Never called on an event loop thread."""
    global _governance_unreachable_cache
    with _governance_unreachable_lock:
        now = time.monotonic()
        fingerprint: object
        try:
            fingerprint = _sign_in_fingerprint()
        except Exception:  # noqa: BLE001 - no fingerprint means no reuse
            fingerprint = object()
        cached = _governance_unreachable_cache
        if (
            cached is not None
            and cached[1] == fingerprint
            and now - cached[0] < _GOVERNANCE_UNREACHABLE_TTL_SECS
        ):
            return cached[2]
        try:
            if mcp_governance_may_apply() or _data_home_api_key_may_be_set():
                verdict = False
            else:
                # Function-local: login_target imports this module at its top.
                from kiro_crew.cloud.login_target import whoami_names_personal_account

                verdict = whoami_names_personal_account()
        except Exception:  # noqa: BLE001 - a probe failure is "cannot tell"
            verdict = False
        _governance_unreachable_cache = (now, fingerprint, verdict)
        return verdict


# Single-flight flag for the off-loop warm-up. Its lock only guards the flag and
# is never held across I/O, so taking it on the loop thread cannot stall.
_governance_warm_running = False
_governance_warm_flag_lock = threading.Lock()


def _governance_unreachable_on_loop() -> bool:
    """The probe's answer on an event loop thread: cached, or doubt. Never blocks.

    Reads the cache without taking :data:`_governance_unreachable_lock`, which an
    off-loop probe may hold for the whole ``whoami``. A cached answer is reused
    while the sign-in fingerprint still matches it, past the reuse window too:
    switching between a personal and a governed sign-in writes kiro-cli's store
    or changes the API key, and either changes the fingerprint. Past the window
    a warm-up re-asks off the loop for the next call.

    With no answer for this sign-in the probe answers ``False``, the doubt
    answer, which keeps the registry marker, and starts a warm-up so the next
    spec write knows. A governed account therefore never loses the marker here.
    """
    try:
        fingerprint: object = _sign_in_fingerprint()
    except Exception:  # noqa: BLE001 - no fingerprint means no reuse
        fingerprint = object()
    cached = _governance_unreachable_cache
    if cached is not None and cached[1] == fingerprint:
        if time.monotonic() - cached[0] >= _GOVERNANCE_UNREACHABLE_TTL_SECS:
            _start_governance_warm()
        return cached[2]
    _start_governance_warm()
    return False


def _start_governance_warm() -> None:
    """Run the full probe once on a daemon thread, unless one is already running."""
    global _governance_warm_running
    with _governance_warm_flag_lock:
        if _governance_warm_running:
            return
        _governance_warm_running = True

    def _warm() -> None:
        global _governance_warm_running
        try:
            # Catches every probe failure itself and caches it as doubt.
            _governance_unreachable_blocking()
        finally:
            with _governance_warm_flag_lock:
                _governance_warm_running = False

    try:
        threading.Thread(target=_warm, name="kirocrew-registry-probe", daemon=True).start()
    except RuntimeError:
        # No thread could start (interpreter shutting down): leave the flag clear
        # so a later call can try again.
        with _governance_warm_flag_lock:
            _governance_warm_running = False


def signed_in_via_idc(
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether kiro-cli's local state says the identity came from IDC.

    Returns False on any failure. An unreadable or absent store means "cannot
    tell", and inferring an enterprise account from a missing file would put a
    governance warning in front of every personal install.

    The store holds live credential material, so every read of it owes an SEL
    audit event even though this function only ever selects the COUNT of two
    non-secret ``auth.idc.*`` marker rows. Auditing is FAIL-CLOSED: if the audit
    cannot be emitted the probe reports "cannot tell" rather than reading
    unaudited, which costs a diagnostic hint and never a credential.
    """
    # Imported here rather than at module scope: ``hooks`` pulls in the security
    # and governance planes, and this module is imported by lightweight
    # path-resolution callers (including the MCP server entry points) that must
    # not pay for that, nor risk an import cycle through them.
    from kiro_crew.hooks import emit_internal_read_audit

    candidates = kiro_cli_state_dbs(
        platform_name or sys.platform,
        home if home is not None else Path.home(),
        environ if environ is not None else os.environ,
    )
    placeholders = ",".join("?" * len(_IDC_STATE_KEYS))
    for db in candidates:
        connection = _open_state_db_readonly(db)
        if connection is None:
            continue
        try:
            if not emit_internal_read_audit(_IDC_PROBE_READ_ID, "success"):
                # Audit surface unavailable: do not read the store.
                return False
            with connection:
                row = connection.execute(
                    f"SELECT count(*) FROM state WHERE key IN ({placeholders})",
                    _IDC_STATE_KEYS,
                ).fetchone()
            if row and int(row[0]) > 0:
                return True
        except (sqlite3.Error, ValueError):
            continue
        finally:
            connection.close()
    return False


def _open_state_db_readonly(path: Path) -> sqlite3.Connection | None:
    """Open kiro-cli's state store read-only, or return None if it cannot be read.

    Mirrors the readiness probe's gates on the same file: reject a symlink and
    require a regular file, then hand SQLite a read-only URI.

    ``mode=ro`` WITHOUT ``immutable=1``. The immutable flag avoids touching a
    sidecar, but it also tells SQLite the file cannot change, so the WAL is
    IGNORED — and against a store in WAL mode whose newest commits are still in
    ``data.sqlite3-wal`` the identity rows read as absent. A fresh Identity
    Center sign-in would then look like a personal account and silence the very
    governance diagnosis this function exists to trigger. Plain ``mode=ro``
    applies the WAL, so the answer matches what kiro-cli itself would read; the
    cost is that SQLite may create or refresh the ``-shm`` index beside the live
    database exactly as any other reader does, and it holds no identity data.
    """
    try:
        if path.is_symlink():
            return None
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return None
    except OSError:
        return None
    uri = f"file:{urllib.request.pathname2url(str(path))}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=_STATE_DB_TIMEOUT_SECS)
    except sqlite3.Error:
        return None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _windows_program_files(environ: Mapping[str, str]) -> str:
    return environ.get("ProgramFiles") or environ.get("PROGRAMFILES") or r"C:\Program Files"


#: The macOS install locations that are FIXED system-wide, in search order, with
#: the user's own bundle inserted after the first entry by
#: :func:`known_kiro_cli_dirs`.
#:
#: Named rather than inlined because these are the ONE part of the candidate set
#: no argument can point elsewhere: every other entry is derived from ``home`` or
#: ``environ`` (the mise shim entry with the exception this function's own
#: docstring records -- ``mise_data_dir`` still honours the PROCESS-level
#: ``MISE_DATA_DIR`` / ``XDG_DATA_HOME``, so it is home-pinned only in their
#: absence), which is what lets a caller pin the set and then report it as the
#: directories that were searched. A test that fakes a host by pinning
#: ``(platform_name, home, environ)`` still gets these three, so on a developer
#: machine with a real install it is asserting about that machine's
#: ``/Applications`` rather than about its own fixture. Emptying this tuple is how
#: such a test fences them; the values themselves are pinned by
#: ``test_kiro_cli_pin.py`` so an empty default can never ship.
_MACOS_SYSTEM_DIRS: tuple[str, ...] = (
    "/Applications/Kiro CLI.app/Contents/MacOS",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def known_kiro_cli_dirs(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
    *,
    include_inherited_path: bool = True,
) -> list[str]:
    """Return fixed and inherited directories where Kiro CLI may be installed.

    Every home-derived path comes from the ``home`` argument, never from a live
    ``os.path.expanduser("~")``, so a caller that pins ``(platform_name, home,
    environ)`` gets the same account's directories from this function and from
    :func:`find_kiro_cli_candidates`, and may report them as the directories
    that were searched. (:func:`~kiro_crew.env.mise_data_dir` still honours the
    process-level ``MISE_DATA_DIR``/``XDG_DATA_HOME`` overrides, so the mise
    shim entry is home-pinned only in their absence.)

    The desktop app's bundled copy (:data:`BUNDLED_KIRO_DIR_ENV`) leads the
    list on every platform. It ranks ABOVE every system install -- the app was
    built and tested against that exact version -- but BELOW the
    ``KIROCREW_KIRO_BIN`` operator override, which
    :func:`find_kiro_cli_candidates` places ahead of every directory here, so
    an operator can still force a different binary. It sits in the FIXED part
    of the list, ahead of the ``include_inherited_path`` branch, so the pinned
    off-``PATH`` spawns (:func:`pin_kiro_cli`, the ACP launch, the readiness
    probe) resolve the bundled copy through the same ranking as the
    interactive setup path: one candidate order, whichever caller asks.
    """

    dirs: list[str] = []
    bundled = bundled_kiro_cli_dir(environ)
    if bundled:
        dirs.append(bundled)
    if platform_name == "win32":
        local_app_data = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        dirs += [
            str(local_app_data / "Kiro-Cli"),
            str(Path(_windows_program_files(environ)) / "Kiro-Cli"),
        ]
    else:
        dirs += [
            str(home / ".local" / "bin"),
            str(home / ".cargo" / "bin"),
        ]
    if platform_name == "darwin":
        # Slicing rather than unpacking: a test fences the fixed locations by
        # emptying ``_MACOS_SYSTEM_DIRS``, and slices are safe at any length while
        # ``head, *rest = ()`` raises. Order is preserved -- the system bundle, the
        # user's own bundle, then the shared bin dirs.
        fixed = list(_MACOS_SYSTEM_DIRS)
        user_app = str(home / "Applications" / "Kiro CLI.app" / "Contents" / "MacOS")
        dirs.extend(fixed[:1] + [user_app] + fixed[1:])
    if include_inherited_path and platform_name == "win32":
        dirs.extend(part for part in environ.get("PATH", "").split(";") if part)
        # A GUI-launched Windows gateway can retain an old PATH after a user
        # installs a CLI. Keep the inherited order, then add the shared set of
        # standard user tool directories and the venv Scripts fallback.
        dirs.extend(part for part in augmented_path("", home=str(home)).split(os.pathsep) if part)
    elif include_inherited_path:
        # `home=` is forwarded for the same reason the win32 branch above does it:
        # `augmented_path` falls back to a LIVE `os.path.expanduser("~")` when the
        # keyword is omitted, so the `{home}`-templated extras and the Node/mise bin
        # dirs would come from the process's account while the `.local/bin` and
        # `.cargo/bin` entries above come from the caller's `home`. That makes this
        # function's result depend on state outside its arguments, which is exactly
        # what the ACP resolver's "the directories named in a not-found message are
        # the directories that were actually searched" contract relies on it NOT
        # doing (see acp/client_spawn_env.py's `_resolve_kiro_bin_for_spawn` docstring).
        dirs.extend(
            part
            for part in augmented_path(environ.get("PATH", ""), home=str(home)).split(os.pathsep)
            if part
        )
    return _unique(dirs)


def find_kiro_cli_candidates(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
    *,
    include_inherited_path: bool = True,
) -> list[str]:
    """Enumerate executable Kiro CLI candidates without mutating the environment.

    Every directory contributes ``kiro-cli`` (``.exe`` on Windows) except the
    desktop app's bundled directory, which contributes
    :func:`bundled_kiro_cli_entry`: POSIX ships the chat binary directly while
    Windows ships the one executable extracted from its MSI.
    """

    name = f"{KIRO_CLI_NAME}.exe" if platform_name == "win32" else KIRO_CLI_NAME
    bundled = bundled_kiro_cli_dir(environ)
    candidates: list[str] = []
    override = environ.get("KIROCREW_KIRO_BIN", "")
    if override:
        candidates.append(override)
    candidates.extend(
        str(
            Path(directory)
            / (bundled_kiro_cli_entry(platform_name) if directory == bundled else name)
        )
        for directory in known_kiro_cli_dirs(
            platform_name,
            home,
            environ,
            include_inherited_path=include_inherited_path,
        )
    )
    result: list[str] = []
    for candidate in _unique(candidates):
        if platform_compat.is_executable_file(candidate, platform_name=platform_name):
            if platform_name == "win32":
                try:
                    if os.path.getsize(candidate) == 0:
                        continue
                except OSError:
                    continue
            result.append(os.path.realpath(candidate) if platform_name == "win32" else candidate)
    return result


def resolve_kiro_cli(
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    include_inherited_path: bool = True,
) -> str | None:
    """Return the first executable Kiro CLI candidate, if one exists.

    ``include_inherited_path=False`` forwards to
    :func:`find_kiro_cli_candidates` and drops the inherited ``PATH`` from the
    candidate set. What remains is the fixed known install directories plus the
    explicit ``KIROCREW_KIRO_BIN`` override and the desktop app's bundled
    directory (:data:`BUNDLED_KIRO_DIR_ENV`), which are deliberately still
    honoured: both are set by the process that starts the gateway, not named by
    a directory an agent can plant a file in. Unattended callers pass the
    keyword so a ``PATH`` leading with an agent-writable directory cannot choose
    what they execute; interactive ones keep the default, where a nonstandard install
    on ``PATH`` is a convenience rather than an exposure.
    """

    resolved_platform = platform_name or sys.platform
    resolved_home = home or Path.home()
    resolved_environ = environ if environ is not None else os.environ
    candidates = find_kiro_cli_candidates(
        resolved_platform,
        resolved_home,
        resolved_environ,
        include_inherited_path=include_inherited_path,
    )
    return candidates[0] if candidates else None


def kiro_cli_installed() -> bool:
    """Whether any kiro-cli is installed here, pinned or reachable only on ``PATH``.

    A stat of the install directories (:func:`resolve_kiro_cli`), never a spawn.
    Read by the spec ``permissions`` gate on the KAS bundle route and by
    ``kirocrew doctor``'s row for it: an installed kiro-cli reads the same
    ``~/.kiro/agents`` files, so its version gate still applies there. A lookup
    that raises counts as installed, because the gate it then runs withholds on
    an unknown version, the smaller loss.
    """
    try:
        return resolve_kiro_cli() is not None
    except Exception:  # noqa: BLE001 - an unanswerable lookup fails closed
        return True


#: Fixed wording for the one refusal worth reporting: an install that exists but
#: only through ``PATH``. Named here so ``kirocrew update`` and the diagnostics
#: bundle tell the operator the same thing, including the override that fixes it.
PATH_ONLY_INSTALL_NOTE = (
    "kiro-cli resolves only through PATH, which this spawn does not trust; "
    "point KIROCREW_KIRO_BIN at the binary's absolute path to have it used here"
)


def pin_kiro_cli() -> tuple[str | None, bool]:
    """``(pinned absolute path or None, an unpinned install exists)``.

    The sync pin for a spawn that must not exec a bare argv0. ``argv0`` is
    re-resolved off the inherited ``PATH`` inside ``exec``, and a gateway's
    ``PATH`` can lead with an agent-writable directory (a worktree venv's
    ``bin``), so the candidate set is :func:`resolve_kiro_cli` with
    ``include_inherited_path=False``: the fixed known install directories plus
    the operator's own ``KIROCREW_KIRO_BIN`` and the desktop app's bundled
    directory. ``None`` means refuse — callers skip the step rather than fall
    back to the bare name.

    The second element separates the two ways the pin comes back empty, which
    a caller reports differently: kiro-cli is not installed at all (nothing to
    say — the backend is optional), or it IS installed somewhere the pin does
    not accept, which an operator needs told about, together with
    :data:`PATH_ONLY_INSTALL_NOTE`. The ``PATH``-inclusive lookup that answers
    it only ever decides the wording; it never names what gets spawned.

    Absolute or nothing. The one candidate that can come back relative is the
    override itself (``KIROCREW_KIRO_BIN=kiro-cli``): the existence check would
    pass against the current directory while ``exec`` re-resolved the bare
    argv0 off ``PATH`` — the exact divergence this pin exists to remove. A
    relative pin is therefore refused and reported like a ``PATH``-only
    install, since the note already names the fix.

    Sync and unbounded: it stats directories under the home directory. The
    gateway's unattended paths run this in a thread under a timeout
    (``slack.gateway._pinned_kiro_cli``); a CLI command or a request handler
    already blocking on the spawn itself has nothing to gain from that.
    """

    pinned = resolve_kiro_cli(include_inherited_path=False)
    if pinned is not None and os.path.isabs(pinned):
        return pinned, False
    return None, resolve_kiro_cli() is not None
