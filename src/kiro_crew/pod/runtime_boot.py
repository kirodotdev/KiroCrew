"""A pod's process entry points: the service boot, ``pod exec``, and refusals.

:func:`boot` is the body the service manager runs as ``kirocrew pod _run <name>``:
it prepares the isolated home and ``exec``s the worktree's own gateway (Windows
supervises it instead). :func:`exec_in_pod` runs an allowlisted verb in the same
environment through the shared :func:`pod_context`. Every terminal refusal is
recorded host-side so a launchd exit 0 stays legible.

Pod-home preparation, the per-pod env file and the platform flags are read from
their owning modules at call time -- :mod:`kiro_crew.pod.runtime` for the core --
because that is the namespace the pod suite patches.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.pod import launchd
from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime, runtime_attestation, runtime_home, runtime_ports
from kiro_crew.pod import windows as win_backend
from kiro_crew.pod.config import (
    EXIT_PROVISIONING,
    EXIT_REFUSED_UNRECOVERABLE,
    TERMINAL_BOOT_EXIT_CODES,
    PodConfig,
)
from kiro_crew.pod.runtime import PodError


def _refuse(cfg: PodConfig, name: str, code: int, reason: str) -> int:
    """Print a FATAL for *reason*, record it, and return the terminal *code*.

    Every terminal exit goes through here so the record and the exit cannot drift.
    They did drift once: ``_run_internal`` prints "recorded at <path>" whenever it
    translates a terminal code for launchd, but only the OS-home refusal wrote the
    file, so the provisioning (3) and live-port (70) exits named a path that did
    not exist (found in review). Those two are translated to 0 on macOS exactly
    like 78 is, so they have the same legibility problem and need the same record.
    """
    print(f"FATAL: {reason}")
    _record_refusal(cfg, name, reason)
    return code


def terminal_exit_code(cfg: PodConfig, name: str, code: int) -> int:
    """The exit status to hand the SERVICE MANAGER for *code*.

    The record-conditional wrapper around :func:`kiro_crew.pod.launchd.launchd_exit_code`,
    and the ONLY translation callers should use. ``launchd_exit_code`` states the
    platform semantics (launchd restarts on non-zero, so a terminal refusal has to
    exit 0 or it loops every ``ThrottleInterval``); this adds the condition that
    makes exiting 0 honest.

    **Translating unconditionally was a real hole.** Exit 0 tells launchd the boot
    ended cleanly, and the only thing that keeps a refusal legible after that is
    the host-side note. If the note failed to land -- the write refused a planted
    link, the directory was unusable, the disk was full -- then translating anyway
    produces a pod that looks cleanly stopped with NO record anywhere of why, which
    is strictly worse than the restart loop the translation exists to prevent. So a
    terminal code is translated only when :func:`refusal_reason` confirms the record
    is actually readable; otherwise the honest non-zero survives and launchd's
    retry, noisy as it is, at least keeps the failure visible.

    Non-terminal codes and 0 pass through untouched on every platform, so ordinary
    crash recovery is unaffected.
    """
    if code not in TERMINAL_BOOT_EXIT_CODES:
        return code
    if not runtime.IS_MACOS:
        # systemd exempts these codes via RestartPreventExitStatus, so the honest
        # code is also the non-looping one there. Windows Task Scheduler has no
        # restart policy at all -- a task whose action exits non-zero is recorded
        # with that result and stays down -- so the honest code is already
        # terminal, and `windows.unit_state` reads that recorded result as the
        # crash signal. Nothing to translate on either.
        return code
    if refusal_reason(cfg, name) is None:
        print(
            f"kirocrew-pod: keeping exit {code} — the refusal could not be recorded at "
            f"{cfg.refusal_file(name)}, so exiting 0 would hide it entirely"
        )
        return code
    return launchd.launchd_exit_code(code)


def _record_refusal(cfg: PodConfig, name: str, reason: str) -> None:
    """Record a TERMINAL boot refusal for *name* on the HOST side.

    Best-effort by design: the refusal itself is already decided and printed, so a
    failure to write the note must not turn into a second failure mode. What it
    buys is legibility on macOS, where :func:`kiro_crew.pod.launchd.launchd_exit_code`
    has to exit 0 to stop launchd's restart loop and the refusal would otherwise
    be indistinguishable from a clean exit.

    **Refuses to write outside the pod plane, regardless of caller.** The name is
    re-validated here and the resulting path is proven to be a direct child of
    ``cfg.pods_dir`` before anything is published. That is defense in depth, not the
    primary control -- ``boot`` validates before entering its guarded region, so a
    path-shaped name should never arrive -- but the primary control is one caller's
    ordering and this is a property of the function. Without it, any future caller
    that records before validating turns a best-effort note into an arbitrary host
    write: ``pod _run /tmp/important`` would have atomically overwritten
    ``/tmp/important.refused``. Same reasoning as ``cleanup_home``'s independent
    name re-validation, and the same "protected on one path only is not protected"
    rule the pod runtime states elsewhere.
    """
    try:
        runtime.validate_name(name)
    except PodError:
        print(f"kirocrew-pod: refusing to record a refusal for invalid pod name {name!r}")
        return
    target = cfg.refusal_file(name)
    try:
        root = cfg.pods_dir.resolve()
        if target.resolve().parent != root:
            print(f"kirocrew-pod: refusing to write a refusal note outside {root}")
            return
    except OSError:
        # Cannot prove the location, so do not write. A missing note costs
        # legibility; an unproven one costs an arbitrary host file.
        return
    try:
        pinned_fs.write_file_pinned(
            target,
            f"{reason}\n",
            what="pod refusal note",
            mode=0o600,
            refusal=PodError,
        )
    except (OSError, PodError, ValueError):
        pass


def _clear_refusal(cfg: PodConfig, name: str) -> None:
    """Drop a stale refusal note so it only ever describes the LAST boot."""
    try:
        pinned_fs.unlink_pinned(cfg.refusal_file(name), what="pod refusal note")
    except (OSError, ValueError):
        pass


def refusal_reason(cfg: PodConfig, name: str) -> str | None:
    """The recorded terminal-refusal reason for *name*, or None if its last boot
    did not refuse. Unreadable is reported as refused-for-an-unknown-reason rather
    than as clean -- the file existing is itself the signal.

    Read through the PINNED no-follow chokepoint, the same one ``_record_refusal``
    publishes through. A by-name ``read_text`` here followed a link at the final
    component, so a planted ``<name>.refused`` symlink pointed at any host file the
    gateway could read made ``kirocrew pod ls`` print that file's contents under
    the note's own label -- a disclosure primitive on the exact path whose WRITE
    side was already pinned (found in review). A non-regular note is refused with a
    reason instead of being followed: the file existing still reports a refusal, so
    the signal survives while the contents never do.
    """
    target = cfg.refusal_file(name)
    try:
        text = pinned_fs.read_file_pinned(target, what="pod refusal note", refusal=PodError).strip()
    except FileNotFoundError:
        return None
    except PodError:
        return "boot refused (reason file is not a regular file; refusing to read it)"
    except (OSError, ValueError):
        return "boot refused (reason file unreadable)"
    return text or "boot refused (reason not recorded)"


def pod_context(cfg: PodConfig, name: str) -> tuple[Path, dict[str, str]]:
    """Resolve pod *name* to ``(its own kirocrew binary, its isolated env)``.

    The single seam every pod-scoped command goes through, so ``boot`` and
    :func:`exec_in_pod` cannot drift apart. Notably the env comes
    from :func:`build_pod_env`, which means a command run against a pod inherits
    the SAME messaging-credential scrubbing as the pod's own gateway — a
    hand-rolled env here would silently let a throwaway instance act as the live
    Slack / WeCom / Telegram identity. The pod's ``EMBEDDINGS=`` setting travels
    the same way, so a ``pod exec`` against an embedding-light pod does not
    quietly download the model the pod was booted to do without.

    Raises :class:`PodError` when the pod has no pinned checkout (never brought
    up from inside a checkout) or that checkout has no provisioned venv.
    """
    runtime.validate_name(name)
    env_data = runtime.read_env_file(cfg, name)
    checkout_str = env_data.get("CHECKOUT")
    if not checkout_str:
        raise PodError(
            f"pod {name!r} has no pinned checkout — run `kirocrew pod up {name}` "
            f"from inside a kirocrew checkout first"
        )
    checkout = Path(checkout_str).expanduser()
    bin_path = prov.venv_bin(checkout)
    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        raise PodError(f"no kirocrew venv at {bin_path} (provision {name} first)")
    env = runtime.build_pod_env(
        cfg,
        cfg.home_dir(name),
        runtime_ports.derive_port(cfg, name),
        checkout,
        skip_model_download=runtime.embeddings_disabled(env_data),
    )
    return bin_path, env


# `pod exec` forwards to a real kirocrew, so it inherits the WHOLE CLI — including
# verbs that manage the HOST rather than any one instance. This is an ALLOWLIST
# rather than a denylist because the set of host-scoped verbs is open-ended (a
# by-name denylist repeatedly missed verbs — `stop`, then `restart`, then
# `service`): every verb below acts only on `KIROCREW_HOME` state, which
# `pod exec` has already pointed at the pod.
# Anything else — present or newly added — is refused until it is deliberately
# listed, so the failure mode of drift is "temporarily unavailable" rather than
# "silently operated on the user's live machine".
#
# Deliberately EXCLUDED, with the reason each is host-scoped, not pod-scoped:
#   setup, update  — rewrite the install and the ~/.local/bin launcher
#   app            — `apps/bridges.py` edits `~/.kiro/settings/mcp.json`, the
#                    HOST registry, which is NOT covered by KIROCREW_HOME or
#                    KIRO_HOME. (The app agent JSONs it symlinks now follow
#                    `kiro_agents_dir()`, so those land under the pod's own
#                    KIRO_HOME — but the settings registry still does not, so a
#                    pod install/uninstall would still mutate host state.)
#   stop, restart  — service-aware: `cli_server._stop` short-circuits to
#                    systemctl when no explicit --port is passed, so they hit the
#                    LIVE gateway; and `restart` additionally leaves a DETACHED
#                    replacement that `pod down` cannot stop
#   service        — installs/removes the machine-wide systemd unit
#   gateway        — would race a second gateway against the pod's own unit
#   pod            — pod management from inside a pod (a nested `pod down` would
#                    tear down its own supervisor)
#   cloud          — provisions resources in the user's AWS account
#   browse         — writes browser auth state outside KIROCREW_HOME
#   manifest       — emits a Slack app manifest tied to the real identity
#   run            — `task_reporter.save_progress` writes `TASK_PROGRESS.md`
#                    "next to the spec file" (`Path(run.spec_path).parent`), so
#                    `run /host/TASK.md` writes `/host/TASK_PROGRESS.md`. An
#                    IMPLICIT write outside the pod, derived from a path the user
#                    supplied as an input rather than a destination.
#   snapshot       — its destination is CONFIGURABLE (`snapshot_dir`, or a
#                    positional dir) and `--keep N` DELETES older archives beyond
#                    N. `sanitized_seed_config` only forces tunnel/telegram/wecom
#                    off, so a pod seeded from the live config inherits the user's
#                    real backup directory — `snapshot --keep 1` from a pod would
#                    prune live backups. Destructive and cross-plane.
#   doctor         — NOT read-only: `cli_doctor._doctor_mcp_tools` does
#                    `atomic_write(agent_path, ...)` where `agent_path` is
#                    `_agents_dir() / AGENT_FILENAME` (in `cli_doctor._doctor`), i.e. under the
#                    real HOME. It auto-adds missing MCP servers, so a pod
#                    `doctor` rewrites the LIVE agent configuration.
#   tui, chat      — `cli_chat._tui` resolves its port as
#                    `getattr(args, "port", None) or cfg…get("port", 5476)` — the
#                    CONFIG dashboard port, falling back to literal 5476, never
#                    `KIROCREW_PORT`. A pod's config names no dashboard port, so it
#                    lands on the LIVE gateway. `chat` is excluded too because
#                    `chat --tui` branches straight into `_tui` (cli.py:1818), so
#                    excluding only `tui` left the same hole open. Every OTHER
#                    client verb (`status`, `logout`, and the credential verb) goes
#                    through `port_resolution.resolve_client_port`, which DOES honour
#                    `KIROCREW_PORT` — so this hazard is confined to `_tui`.
#   logs           — `cli_server._logs_cmd` runs `journalctl -u <SERVICE_NAME>`,
#                    the HOST service unit, so inside a pod it would show the LIVE
#                    gateway's journal while appearing to show the pod's. Wrong
#                    answer rather than damage, but a confidently wrong one.
#                    `kirocrew pod logs NAME` reads the pod's own unit.
#   mcp-*          — stdio server entrypoints, not user-facing commands
#
# `agent` and `workspace` ARE listed: both dispatchers were checked and operate
# only on `config_dir()` state (the config.json agents/workspaces maps), never on
# `~/.kiro/agents`. `workspace` is safe specifically because it asserts every
# source and destination `is_relative_to(config_dir())`; without that guard it
# would belong with `snapshot`. `restore` is listed because although its SOURCE
# archive path is arbitrary, that is a read — everything it writes lands in
# `config_dir()`.
#
# The inclusion test, stated once so it does not have to be rediscovered. A verb
# qualifies only if BOTH hold:
#   (a) every path it writes IMPLICITLY — anywhere the user did not name as a
#       destination — is inside `config_dir()`; and
#   (b) it deletes nothing outside `config_dir()`.
# An explicitly-named output destination is fine (`memory export -o FILE` writes
# where the user pointed it, no differently from shell redirection). What fails is
# an implicit write derived from an INPUT path (`run` → `TASK_PROGRESS.md` beside
# the spec), a host registry (`app`), a host service (`service`, `stop`,
# `restart`), a deletion driven by config (`snapshot --keep`), or a host-scoped
# read that merely LOOKS pod-scoped (`logs`).
_POD_SAFE_VERBS = frozenset(
    {
        "agent",
        "artifact",
        "config",
        "consolidate",
        "cron",
        "eval",
        "knowledge",
        "learn",
        "logout",
        "memory",
        "policy",
        "restore",
        "security",
        "spawn",
        "status",
        "token",
        "workspace",
    }
)


# The pod-native equivalent to suggest for the verbs users are most likely to try.
_POD_EQUIVALENT: dict[str, str] = {
    "stop": "kirocrew pod down {name}",
    "restart": "kirocrew pod down {name} && kirocrew pod up {name}",
    "gateway": "kirocrew pod up {name}",
    "logs": "kirocrew pod logs {name}",
}


def require_pod_safe_verb(argv: list[str], name: str) -> None:
    """Raise :class:`PodError` unless ``argv[0]`` is a pod-scoped verb.

    The verb must come FIRST. That is a deliberate constraint rather than a
    limitation to work around: allowing global flags ahead of it reintroduces the
    parsing ambiguity that made the previous denylist bypassable (``-v stop``, and
    worse ``--log-level DEBUG stop``, where the flag's value is indistinguishable
    from a verb). Flags AFTER the verb are untouched, so `-- status --json` works.
    """
    verb = argv[0] if argv else ""
    if verb in _POD_SAFE_VERBS:
        return
    if verb in _POD_EQUIVALENT:
        hint = f" Use `{_POD_EQUIVALENT[verb].format(name=name)}` instead."
    elif verb.startswith("-"):
        hint = " The verb must come first; put global flags after it."
    else:
        hint = (
            " Only verbs that act on the pod's own data are allowed: "
            + ", ".join(sorted(_POD_SAFE_VERBS))
            + "."
        )
    raise PodError(f"refusing `{verb or '(nothing)'}` inside `pod exec`:{hint}")


def exec_in_pod(cfg: PodConfig, name: str, argv: list[str]) -> int:
    """``exec`` *argv* as the pod's own kirocrew, in the pod's isolated env.

    Replaces the current process on success, so the child's exit status and its
    stdio (including a TTY, which matters for ``chat`` / ``tui``) reach the caller
    untouched. Returns an exit code only when the exec itself fails.
    """
    require_pod_safe_verb(argv, name)
    bin_path, env = pod_context(cfg, name)
    # Run INSIDE the pod's workspace, not the caller's cwd. Agent verbs resolve
    # relative paths against the working directory, so inheriting the invoking
    # shell's cwd (often the live checkout) would let `-- run ./TASK.md` or a file
    # edit land outside the pod even with KIROCREW_WORKSPACE set correctly.
    workspace = Path(env["KIROCREW_WORKSPACE"])
    try:
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chdir(workspace)
    except OSError as exc:
        print(f"FATAL: could not enter the pod workspace {workspace}: {exc}")
        return 70
    try:
        os.execve(str(bin_path), [str(bin_path), *argv], env)
    except OSError as exc:  # pragma: no cover - exec failure is environmental
        print(f"FATAL: could not exec {bin_path}: {exc}")
        return 70
    return 0  # unreachable on success


# --------------------------------------------------------------------------- #
# Boot — the ExecStart body. Re-entered as ``kirocrew pod _run <name>`` by the
# systemd unit. Reads the PINNED checkout (never shells git), then exec()s the
# worktree's own gateway with an isolated HOME; never returns on success.
# --------------------------------------------------------------------------- #
def target_supports_flag(checkout: Path, flag: str) -> bool:
    """Whether the gateway in *checkout* will accept *flag* on its argv.

    A pod's argv is built by the CONTROL PLANE (the template unit's ``ExecStart``
    resolves ONE kirocrew at ``pod install`` time and every instance re-enters it
    via ``%i``), but the gateway that argv reaches is the TARGET WORKTREE's own
    binary. The two are independent checkouts, so an updated control plane can
    hand a flag to a gateway too old to declare it -- argparse exits 2, and
    ``Restart=on-failure`` + ``RestartSec=5`` (the unit carries no
    ``RestartPreventExitStatus``) turns that into a restart loop every 5s rather
    than a visible failure. Dev Fleet's whole point is worktrees at different
    commits, so this is the ordinary case and not an exotic one.

    Read from the checkout's own ``cli.py`` rather than by running
    ``gateway --help``: the source IS what will execute (provisioning installs the
    checkout editable), and a subprocess on every pod boot costs an interpreter
    start for a question a string search answers.

    Unreadable source answers False -- the flag is DROPPED, not forced. Refusing
    to boot would be the fail-closed instinct, but here it is strictly worse: with
    no ``RestartPreventExitStatus`` a refusal is itself the 5s restart loop this
    exists to prevent, while dropping the flag leaves that pod at exactly the
    guarantee it had before this flag existed (the seeded ``tunnel.enabled=False``)
    -- no regression, just no improvement. The caller says so out loud.
    """
    try:
        src = (checkout / "src" / "kiro_crew" / "cli.py").read_text(encoding="utf-8")
    except OSError:
        return False
    # Comment lines do not declare anything, and a checkout that only MENTIONS the
    # flag in prose would otherwise pass the probe and then argparse-exit on it --
    # the exact restart loop this exists to prevent. The real declaration is a bare
    # quoted literal on its own line inside ``add_argument(...)``, so matching the
    # quoted form (rather than ``add_argument("--flag"``) is what keeps this working
    # against the repo's actual formatting; ``test_the_probe_accepts_this_very_repo``
    # is the ratchet that catches a refactor moving the declaration elsewhere.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if f'"{flag}"' in stripped or f"'{flag}'" in stripped:
            return True
    return False


#: Bound on the child-bootstrap probe. A harness that has not reached either
#: terminal state by now is treated as VIABLE, not as a failure: staying alive on
#: stdin is exactly what a healthy ``acp`` child does, and a slow host must not
#: turn a working pod into a refusal.
_CHILD_VIABILITY_TIMEOUT_SECS = 20.0


def _probe_pod_child_bootstrap(pod_env: dict[str, str]) -> None:
    """Refuse the boot when the pod's kiro-cli child cannot bootstrap.

    Raises :class:`PodError`, which ``boot``'s guard turns into a RECORDED
    terminal refusal (``_refuse`` + ``EXIT_REFUSED_UNRECOVERABLE``), so a pod
    whose child cannot start never reaches health 200 and systemd does not
    restart into the same failure every 5s.

    **Why this exists.** The pod remaps the child's ``HOME`` so its OAuth grants
    die with the pod, and that remap broke every ACP spawn in a pod for a whole
    revision while ``/health`` answered 200 the entire time: the failure surfaced
    only as ``agent_unreachable`` on each provider's Connect/Test, which reads as
    a Connections bug rather than a boot failure. A pod that cannot run an agent
    turn is not a working pod, so it must fail at ``pod up``, loudly, with the
    reason recorded where ``kirocrew pod status`` shows it.

    **The probe is the real spawn path, not an approximation.** It resolves the
    executable the same way and passes it through
    ``acp.client.apply_pod_bundle_spawn``, so the bundle-binary substitution the
    pod child depends on is what gets exercised. Anything cheaper (a bare
    ``--version``) short-circuits before the harness bootstraps and would have
    reported the broken revision as healthy -- that is precisely the mistake an
    earlier round's component test made.

    Three outcomes, and only one refuses:

    * Still running when the bound expires -- a healthy ``acp`` child waiting on
      stdin. Killed and accepted.
    * Exited naming its own login gate -- started fine, has no credential.
      Accepted with a warning, because seeding is best-effort.
    * Exited any other way -- could not bootstrap. REFUSED, with the captured
      stderr tail as the recorded reason.

    A missing kiro-cli is NOT a refusal: it is a separate prerequisite Kiro Crew
    does not bundle, it has its own message elsewhere, and failing the boot for it
    would break every pod on a host that simply has not installed it.
    """
    # Function-local: this runs on the gateway boot path, where a module-level
    # import would pull the agent stack into every pod process that never spawns a
    # child. Reached through ``agent_sdk`` -- the ONE sanctioned surface for the
    # agent backend (``scripts/check_agent_sdk_boundary.py``); application code,
    # this module included, may not import ``kiro_crew.acp`` directly. The probe's
    # spawn logic lives there for that reason, and returns a VERDICT because
    # refusing a boot is a pod concept this function owns, not the SDK's.
    from kiro_crew.agent_sdk.pod_child_probe import (
        PROBE_DEAD,
        PROBE_SIGNED_OUT,
        PROBE_UNAVAILABLE,
        probe_pod_child_bootstrap,
    )

    result = probe_pod_child_bootstrap(pod_env, timeout_secs=_CHILD_VIABILITY_TIMEOUT_SECS)
    if result.verdict == PROBE_UNAVAILABLE:
        print("kirocrew-pod: child viability probe skipped (no kiro-cli on this host)")
        return
    if result.verdict == PROBE_SIGNED_OUT:
        print(
            "kirocrew-pod: child bootstrapped but is signed out; "
            f"sign in inside the pod. Child said: {result.detail}"
        )
        return
    if result.verdict == PROBE_DEAD:
        raise PodError(
            f"the pod's kiro-cli child {result.detail}, so every agent turn in this pod "
            f"would fail while /health still answered 200. "
            f"Child: {result.child} with HOME={result.home}."
        )


def boot(cfg: PodConfig, name: str) -> int:
    """Boot the isolated gateway for pod *name*, converting EVERY refusal into a
    recorded terminal exit. Returns an exit code on failure; on POSIX it ``exec``s
    on success and does not return, while on Windows — which has no ``exec`` — it
    supervises the gateway and returns its exit code once it ends.

    **This wrapper is the class closure for "a refusal that escapes the boot path
    with a non-terminal exit".** Two narrower guards do not cover the class: routing
    the explicit ``return`` sites through :func:`_refuse` misses a ``raise``, and an
    AST guard over those returns misses it for the same reason -- a ``raise
    PodError`` is neither a bare return nor visible to a scan over returns. Such a
    raise escapes to the CLI's generic handler, which exits 1, a code NOT in
    :data:`TERMINAL_BOOT_EXIT_CODES`, so systemd retries it and launchd's KeepAlive
    restarts it every ``ThrottleInterval``: the exact restart loop the terminal-exit
    contract exists to prevent, reached by the one shape a return-shaped guard
    cannot see.

    Enumerating raises does not close it either. ``PodError`` is raised from roughly
    forty sites under ``pod/``, many of them transitively reachable from here
    (``validate_name``, ``read_env_file``, ``write_pod_config``,
    ``seed_home_from_scenario``, ``_ensure_pod_dir``, the whole ``pinned_fs``
    refusal surface), and any future one joins them silently. So the conversion is
    structural instead: the body cannot raise ``PodError`` past this frame, and a
    refusal added tomorrow is recorded and given a terminal code without anyone
    remembering to route it.

    ``execve`` replaces the process on the POSIX success path, so nothing after the
    body can run and the wrapper costs the happy path nothing. On Windows the body
    returns the supervised gateway's own exit code instead; that code is not in
    :data:`TERMINAL_BOOT_EXIT_CODES`, so it flows out untranslated and the wrapper
    still only ever converts refusals.

    **Name validation happens BEFORE the guard, deliberately.** The wrapper records
    every refusal it catches, and ``_record_refusal`` derives its path from *name* --
    so catching a NAME-validation failure turned the refusal machinery into a
    host-write primitive: ``pod _run /tmp/important`` would have written
    ``/tmp/important.refused`` outside the pod plane. There is no legitimate pod to
    record against when the name itself is rejected, so that failure exits with the
    honest error and writes nothing. The guard therefore only ever sees a validated
    name, which is also what lets ``_record_refusal`` treat a path-shaped name as
    unreachable rather than merely unlikely.
    """
    try:
        runtime.validate_name(name)
    except PodError as exc:
        # Outside the guard on purpose: see the docstring. No record is written --
        # there is no pod this could be a refusal FOR.
        print(f"FATAL: {exc}")
        return EXIT_PROVISIONING
    try:
        return _boot_unguarded(cfg, name)
    except OSError as exc:
        if not runtime.IS_WINDOWS:
            raise
        return _refuse(cfg, name, EXIT_REFUSED_UNRECOVERABLE, str(exc))
    except PodError as exc:
        # Already-recorded refusals return through _refuse and never arrive here;
        # this is the escape hatch closing, so record and give it a terminal code.
        return _refuse(cfg, name, EXIT_REFUSED_UNRECOVERABLE, str(exc))


def _boot_unguarded(cfg: PodConfig, name: str) -> int:
    """The boot body. Call :func:`boot`, never this: a ``PodError`` raised here is
    a refusal that must be recorded and given a terminal exit code, and only the
    wrapper does that."""
    runtime.validate_name(name)
    env_data = runtime.read_env_file(cfg, name)
    checkout_str = env_data.get("CHECKOUT")
    if not checkout_str:
        return _refuse(
            cfg,
            name,
            3,
            f"pod {name!r} has no pinned checkout — run "
            f"`kirocrew pod up {name}` from inside a kirocrew checkout first",
        )
    checkout = Path(checkout_str).expanduser()
    home_dir = cfg.home_dir(name)
    bin_path = prov.venv_bin(checkout)

    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        return _refuse(cfg, name, 3, f"no kirocrew venv at {bin_path} (provision {name} first)")
    if not (checkout / "src" / "kiro_crew" / "static" / "dist").is_dir():
        return _refuse(cfg, name, 3, f"no built dist for {name} (build the worktree first)")

    port = runtime_ports.derive_port(cfg, name)
    if port == cfg.live_port:
        return _refuse(cfg, name, 70, f"derived port is the live plane :{cfg.live_port} — refusing")

    seed = env_data.get("SEED", "")
    approval = env_data.get("APPROVAL", "")
    if approval and approval not in runtime.APPROVAL_MODES:
        # `pod up` constrains the flag, but this file is hand-editable, so an
        # unknown value can still reach here. Do NOT merely drop it: omitting
        # --approval does not mean "interactive". The gateway leaves
        # ``approval_mode`` unset, and slack/events.py falls through to
        # ``cfg.agent.approval_mode``, which config/loader.py defaults to
        # "auto" -- auto-approve every tool. Dropping would therefore be the
        # LEAST restrictive outcome. Pin interactive explicitly instead.
        print(
            f"kirocrew-pod: ignoring unknown APPROVAL={approval!r} "
            f"(expected one of: {', '.join(runtime.APPROVAL_MODES)}); "
            f"forcing --approval interactive"
        )
        approval = "interactive"

    crons_raw = env_data.get("CRONS", "")
    crons = crons_raw.strip().lower() in runtime.CRONS_TRUE
    if crons_raw and not crons:
        # Same reasoning as APPROVAL above: hand-editable file, so an
        # unrecognised value falls back to the safer setting (scheduler off)
        # instead of guessing, and the pod still boots.
        print(
            f"kirocrew-pod: ignoring unrecognised CRONS={crons_raw!r} "
            f"(expected one of: {', '.join(sorted(runtime.CRONS_TRUE))}); scheduler stays off"
        )

    # A named scenario owns the whole home and must land before the create-only
    # config writer. Directory seeds keep their existing config-only behavior.
    scenario = seed if runtime_home.is_scenario_ref(seed) else ""
    if scenario:
        try:
            fresh = runtime_home.seed_home_from_scenario(cfg, name, scenario)
        except PodError as exc:
            return _refuse(cfg, name, 3, str(exc))
        print(
            f"kirocrew-pod: seeded home from scenario {scenario!r}"
            if fresh
            else f"kirocrew-pod: home already populated — scenario {scenario!r} not re-applied"
        )
    if not scenario:
        # Named scenarios finish config/workspace setup through the pinned home
        # descriptor before their completion marker is published. Directory
        # seeds keep the existing config-only path.
        runtime_home.write_pod_config(home_dir, seed)
    # Independent of the scenario/directory-seed split above: every pod, seeded
    # or blank, gets its own OAuth-grant-cache home, with the runtime identity
    # store snapshotted in so the harness can resolve its access token (see
    # ``_seed_pod_os_home`` and ``build_pod_env``'s ``KIROCREW_OS_HOME``
    # docstring). The host's SSO cache is NOT copied -- ``.aws/sso/cache`` is
    # created EMPTY and holds only grants this pod itself mints. Create-only per
    # file, so re-running boot against an already-seeded home is a no-op.
    #
    # A REFUSAL here is fatal, not skippable. This directory becomes the pod
    # child's ``HOME``; if a component is a planted symlink, booting anyway hands
    # the pod's kiro-cli the real host tree and it writes machine-level grants
    # through the link. Exit ``EXIT_REFUSED_UNRECOVERABLE`` so systemd does not
    # restart into the same refusal every 5s -- see that constant.
    try:
        # ``create_and_open_dir_pinned`` pins the ANCESTOR chain and creates only
        # the final component, so the pod home must already exist. It does on both
        # branches above (``write_pod_config`` creates it; a scenario seed builds
        # it), but pinning that here keeps a refusal meaning "a component was
        # unsafe" rather than "a branch happened not to create the parent" -- and
        # the create itself is pinned, so a link planted AT the pod home cannot
        # redirect it.
        runtime._ensure_pod_dir(home_dir, what="pod home")
        runtime_home._seed_pod_os_home(home_dir / "os-home")
    except PodError as exc:
        return _refuse(
            cfg,
            name,
            EXIT_REFUSED_UNRECOVERABLE,
            f"{exc}. Refusing to boot without a verified pod OS home -- the pod's "
            "kiro-cli would write OAuth grants outside the pod. Inspect "
            f"{home_dir / 'os-home'} for a replaced component, then "
            f"`kirocrew pod down {name}` and bring it up again.",
        )
    # Past every terminal refusal: clear any marker an earlier refused boot left,
    # so the record means "the LAST boot refused" rather than "a boot once did".
    _clear_refusal(cfg, name)

    print(f"kirocrew-pod: name={name} port={port} home={home_dir} checkout={checkout}")
    embedless = runtime.embeddings_disabled(env_data)
    pod_env = runtime.build_pod_env(cfg, home_dir, port, checkout, skip_model_download=embedless)
    if pod_env.get(runtime.SKIP_MODEL_DOWNLOAD_ENV) == "1":
        # Keyed on the env the pod will RUN with, not on the env file: a switch the
        # service manager's environment already exports is inherited by build_pod_env
        # and boots the same embedding-light pod without EMBEDDINGS=0 ever being
        # written, so a file-keyed announce stayed silent about exactly that pod.
        # The journal is the only place an operator can confirm the mode after the
        # fact, and "no embed model" is not otherwise observable from a healthy pod:
        # search still answers, just through the keyword fallback. Say it once here,
        # naming the source, so a load measurement taken against this pod is
        # attributable. `== "1"` is the embedder's own test (it ignores every other
        # spelling), so this cannot announce a mode the pod will not be in.
        source = (
            "EMBEDDINGS=0 in the pod env file"
            if embedless
            else "inherited from the boot environment"
        )
        print(
            f"kirocrew-pod: embeddings off ({runtime.SKIP_MODEL_DOWNLOAD_ENV}=1, {source}) — the "
            f"model is not downloaded and memory/knowledge search uses the keyword fallback"
        )
    # Last gate before the gateway serves: a pod whose kiro-cli child cannot
    # bootstrap answers /health 200 while every agent turn fails, so it must
    # refuse HERE rather than present itself as up. Raises PodError, which
    # ``boot``'s guard records as a terminal refusal.
    _probe_pod_child_bootstrap(pod_env)
    # Everything this function printed is still sitting in Python's block-buffered
    # stdout (the journal is a pipe, not a tty), and the exec below REPLACES the
    # process image, discarding that buffer. A refusal survives because it returns
    # and exits, which flushes; the success path does not, so the probe's own
    # "signed out" and boot banner lines were being lost. Flush before the exec.
    sys.stdout.flush()
    sys.stderr.flush()
    argv = ["gateway"]
    if not crons:
        argv.append("--no-crons")
    # Unconditional for any checkout that understands it, and deliberately not an
    # env key like CRONS above: a pod is a throwaway instance and must have no
    # published surface, so there is nothing for the operator to opt into.
    # ``write_pod_config`` seeds ``tunnel.enabled=False`` too, but that is a value
    # in a file it only writes ONCE (it returns early when config.json exists) and
    # anything composing config later can turn it back on — after which the pod
    # published on every boot and nothing re-asserted the guarantee. This flag is
    # re-asserted at every exec, so the seeded value is now defense in depth rather
    # than the enforcement. Reach a pod on the 127.0.0.1 port ``pod url`` prints,
    # over ``ssh -L`` from another host.
    #
    # Probed rather than assumed because this argv is built by the control plane
    # while the gateway it reaches belongs to the target worktree — see
    # ``target_supports_flag`` for why a miss must drop the flag instead of
    # refusing the boot.
    if target_supports_flag(checkout, "--no-tunnel"):
        argv.append("--no-tunnel")
    else:
        # This gateway predates the flag, so it does not RECEIVE the new guarantee:
        # it keeps the pod's seeded ``tunnel.enabled=False`` and behaves exactly as
        # it did before the flag existed. No regression, no improvement -- and
        # deliberately nothing more.
        #
        # Config is NOT re-pinned here to hand it the guarantee anyway. That was
        # tried and the premise does not hold: ``KiroCrewConfig.load()`` deep-merges
        # ``config.local.json`` OVER ``config.json`` with the overlay winning, and
        # ``kirocrew config set`` writes that overlay by default -- so pinning
        # ``config.json`` is not pinning the setting. Nor would it be sufficient if
        # it were: the gateway's enable test is an OR
        # (``cfg.tunnel.enabled or current_context().tunnel.enabled()``) and no
        # config file reaches the provider half at all.
        #
        # Refusing the boot is likewise unavailable: the refusal exit is non-zero
        # and ``Restart=on-failure`` + ``RestartSec=5`` (the unit carries no
        # ``RestartPreventExitStatus``) turns a refusal into a 5s restart loop.
        print(
            "kirocrew-pod: --no-tunnel not found in this checkout's cli.py, so the "
            "flag was not passed -- this pod keeps the tunnel behaviour it had "
            "before the flag existed. Update the checkout to get the guarantee. "
            f"Checkout: {checkout}. If that checkout DOES declare the flag, the "
            "probe has drifted -- see target_supports_flag."
        )
    if approval:
        argv += ["--approval", approval]
    if runtime.IS_WINDOWS:
        # Windows has no exec. CPython's os.execve there SPAWNS and terminates the
        # caller, which would break this path twice: the pid would change (so
        # `main_pid` would stop naming the process that bound the port) and the
        # scheduled task's own process would exit while the gateway kept running
        # orphaned, with Task Scheduler reporting the task finished. Supervise the
        # gateway as a child instead and return its exit code, which keeps every
        # caller's contract identical and the wrapper alive as its parent.
        return win_backend.supervise_gateway(
            cfg,
            name,
            bin_path,
            argv,
            pod_env,
            gateway_pid_record=runtime_attestation._pod_pid_record_path(cfg, name, port),
        )
    os.execve(str(bin_path), [str(bin_path), *argv], pod_env)
    return 0  # unreachable on success
