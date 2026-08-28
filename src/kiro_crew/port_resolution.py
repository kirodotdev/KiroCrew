"""Client-side gateway port resolution — a deliberately light leaf module.

Home of the resolution chain every *client* of a running gateway uses to find
the port it should talk to: the CLI client commands (``token`` / ``status`` /
``logout`` / ``stop``) and the MCP stdio server's gateway callbacks. Both kinds
of consumer import this module at module scope; :mod:`kiro_crew.cli_server`
re-exports every name so its public surface (and the many tests that patch
these symbols there) is unchanged.

Leanness is this module's contract, not an accident: the MCP stdio server is
spawned once per CLI session and must not pay for ``cli_server``'s import graph
(frontend build, service controllers, preflight, embeddings) just to learn a
port number. Every import below is either stdlib or a module the MCP server
already loads at startup. A regression test asserts that importing this module
does not pull in ``kiro_crew.cli_server``.

Patch-namespace contract
------------------------

A large body of tests patches these symbols in the ``cli_server`` namespace
(``kiro_crew.cli_server._gateway_owns_port`` and friends). Chain-internal
calls therefore resolve their callee through :func:`_patchable`, which
prefers the ``cli_server`` namespace whenever that module is loaded and falls
back to this module otherwise. In production the two namespaces hold the very
same function objects (``cli_server`` re-exports them), so the lookup is
behaviour-neutral; under test it is what lets a patch in the ``cli_server``
namespace intercept a call made from inside the chain. The flip side: patch
``cli_server.<name>``, not this module's namespace — patching a name here
does NOT intercept chain-internal calls while ``cli_server`` is loaded, and
in a test process it almost always is.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
import urllib.parse
from collections.abc import Mapping, MutableMapping
from typing import Any

from kiro_crew import platform_compat

# Referenced only through _patchable's ``globals()`` fallback (the class NAME
# is itself a patch target — see _config_url_port), which flake8 cannot see.
from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
from kiro_crew.config.loader import _DEFAULT_PORT
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.instances import run_marker

#: Module whose namespace test patches of the chain historically target.
#: Looked up lazily via ``sys.modules`` — importing it here would recreate the
#: exact heavy import edge this module exists to remove.
_PATCH_NS = "kiro_crew.cli_server"


def _patchable(name: str) -> Any:
    """Resolve chain-internal callee *name* through the patched namespace.

    Returns the attribute from :data:`_PATCH_NS` when that module is loaded
    (so ``mock.patch("kiro_crew.cli_server.<name>")`` intercepts calls made
    from inside the chain, exactly as it did when the chain lived there), and
    from this module's own globals otherwise (the MCP stdio server never loads
    ``cli_server``). The ``getattr`` fallback also covers the window where
    ``cli_server`` is mid-import and has not bound its re-exports yet.
    """
    mod = sys.modules.get(_PATCH_NS)
    if mod is not None:
        fn = getattr(mod, name, None)
        if fn is not None:
            return fn
    return globals()[name]


def _config_url_port() -> int | None:
    """Port explicitly named by ``dashboard.url``, or ``None``.

    Distinct from :func:`parse_dashboard_url`, which substitutes
    ``_DEFAULT_PORT`` for a portless URL (``http://my.host``). That substitution
    is right for the *server* (it must bind something) but wrong for a client:
    it would report "config says 5476" and short-circuit the run-marker
    fallback below, even though the user never named a port. So detect the
    explicit case and let a portless URL fall through.
    """
    try:
        # The class NAME resolves through the patched namespace, not just the
        # chain functions: tests rebind ``cli_server.KiroCrewConfig`` wholesale
        # (``patch("kiro_crew.cli_server.KiroCrewConfig")``), which a direct
        # global read here would silently miss.
        cfg = _patchable("KiroCrewConfig").load()
        url = cfg.dashboard.url or ""
    except Exception:
        # Config load failures must not break client commands.
        return None
    if not isinstance(url, str):
        # ``dashboard.url`` is user-editable JSON and core installs may lack
        # jsonschema, so the value can be any type (``"url": 123``). urlparse
        # raises TypeError on a non-str, which is NOT a ValueError — without
        # this guard a bad config type would crash every client command.
        logging.getLogger(__name__).warning(
            "Ignoring non-string dashboard.url of type %s", type(url).__name__
        )
        return None
    if not url:
        return None
    try:
        _, port = parse_dashboard_url(url)
        # parse_dashboard_url already normalises the scheme, tolerates malformed
        # URLs and applies the KIROCREW_PORT override; re-split only to learn
        # whether the port was written down or defaulted in.
        explicit = urllib.parse.urlsplit(url if "://" in url else f"http://{url}").port
    except (TypeError, ValueError):
        return None
    return port if explicit is not None else None


def _gateway_owns_port(port: int) -> bool:
    """True only when *this user's* gateway process is listening on *port*.

    Reachability is not enough to trust a discovered port. Client commands hand
    the local secret to whatever answers (``_token`` and ``_logout`` send
    ``X-Local-Secret``), and ``clear_marker`` runs only on graceful shutdown —
    so a crashed gateway leaves a marker naming a port some unrelated process
    may since have bound. A bare TCP connect would walk the secret straight into
    that process, which could then mint owner tokens against the real gateway.

    A command-line check is not enough either: argv is attacker-chosen, so a
    listener launched as ``/tmp/kirocrew gateway`` would pass it. The proof used
    here is an identity the attacker cannot forge, in three parts:

    1. **Recorded pid** — ``run_marker.read_pid(port)`` reads the sidecar the
       gateway wrote at ``0600`` inside the ``0700`` ``run/`` dir. Another local
       user cannot write it, so they cannot nominate a process of theirs.
    2. **Holds the port** — that pid must be among
       ``platform_compat.find_listening_pids(port)``. This is what makes a stale
       recorded pid harmless: it has to actually hold the port we are about to
       send the secret to.
    3. **Owned by us, and ours** — the pid's uid must equal the caller's
       (``process_owner_uid``), and its argv must look like a gateway. The uid
       check is what closes pid *recycling* into a foreign user's process; argv
       remains only as defense in depth, never as the sole proof.

    A same-user attacker is out of scope by construction: they can already read
    ``.local_secret`` (mode ``0600``, their own uid), so nothing here can be an
    escalation for them. The boundary this closes is a *different* local user.

    **Fails closed** at every step: no sidecar, no recorded pid, a pid that does
    not hold the port, an unresolvable uid, a missing lookup tool
    (``find_listening_pids`` folds that into an empty list) or a throwing one —
    all deny, and discovery is skipped in favour of the documented default.
    ``--port`` and ``KIROCREW_PORT`` remain available on such hosts.

    **Non-POSIX denies outright.** ``process_owner_uid`` cannot report an owner
    on Windows, and a home that is writable by another user (a shared or
    misconfigured ``KIROCREW_HOME``) would let them replace both the marker and
    the sidecar with a forged listener — the file-permission argument that
    carries step 1 is exactly what stops holding there. Rather than trust
    steps 1-2 alone, discovery is skipped: Windows users keep ``--port`` /
    ``KIROCREW_PORT``, which is precisely where they were before this fallback
    existed, so nothing regresses. This is the one place the feature is
    deliberately unavailable rather than approximated.
    """
    if not platform_compat.IS_POSIX:
        return False
    recorded = run_marker.read_pid(port)
    if recorded is None:
        return False
    try:
        pids = platform_compat.find_listening_pids(port)
    except Exception:
        return False
    if recorded not in pids:
        return False
    owner = platform_compat.process_owner_uid(recorded)
    if owner is None or owner != os.getuid():
        return False
    return _patchable("_is_kirocrew_process")(recorded)


def _marker_port() -> int | None:
    """Port of the sole gateway-owned run-marker, or ``None``.

    Zero-configuration discovery for the common single-gateway box: the gateway
    already advertises itself by writing ``<data-home>/run/gateway-<port>.bin``
    (see :mod:`kiro_crew.instances.run_marker`), so a client with no ``--port``,
    no ``KIROCREW_PORT`` and no port in ``dashboard.url`` can read that instead
    of assuming 5476 and connecting to a dead port.

    Two guards keep this from being a guess:

    * **Ownership.** Only ports where a verified Kiro Crew gateway process is
      listening count (:func:`_gateway_owns_port`); a stale marker, or one whose
      port has been taken over by an unrelated process, is discarded.
    * **Ambiguity.** With several gateways up there is no basis to pick one, so
      this refuses (returns ``None``, landing on the documented default) and
      tells the user on stderr which ports it saw and how to name one.
    """
    try:
        candidates = run_marker.marker_ports()
    except Exception:
        return None
    if not candidates:
        return None
    owns = _patchable("_gateway_owns_port")
    owned = [p for p in candidates if owns(p)]
    if len(owned) == 1:
        return owned[0]
    if len(owned) > 1:
        print(
            f"⚠️  Multiple gateways are running (ports {', '.join(str(p) for p in owned)}); "
            f"not guessing which one you meant — using {_DEFAULT_PORT}. "
            "Pass --port or set KIROCREW_PORT to target a specific gateway.",
            file=sys.stderr,
        )
    return None


def resolve_client_port(cli_port: int | None) -> int:
    """Return the dashboard port a *client* CLI command (token/status/logout/stop)
    should talk to.

    Resolution order:

    1. Explicit ``--port`` CLI flag if the user passed one (``cli_port`` is not ``None``).
    2. ``KIROCREW_PORT`` env var if set to a valid integer. Deliberately ABOVE
       the bound-port export: an explicitly-set ``KIROCREW_PORT`` is how a
       caller retargets a child at a DIFFERENT gateway — ``pod exec`` builds a
       client env with ``KIROCREW_PORT=<pod-port>`` precisely so the command
       reaches the pod, while the inherited ``KIROCREW_BOUND_PORT`` still
       names the spawning LIVE gateway. Ranking the bound value higher would
       walk pod ``token``/``status``/``logout`` (and the local secret they
       carry) into the live gateway — a cross-plane isolation break.
    3. ``KIROCREW_BOUND_PORT`` env var if set to a valid integer — the port the
       parent gateway ACTUALLY bound, exported once its TCP site is listening
       (``dashboard.server._export_bound_port``). Below the operator override
       (see above); above config because a bound fact from the live parent
       beats a guess re-derived from a portless ``dashboard.url``.
    4. Port explicitly named by ``dashboard.url`` in the config file
       (``<data-home>/config.json``), when it parses.
    5. The sole gateway-owned run-marker (``<data-home>/run/gateway-<port>.bin``)
       — see :func:`_marker_port`. Skipped when no marker's port is held by a
       verified gateway process, and refused (with a stderr hint) when several
       are.
    6. ``_DEFAULT_PORT`` (5476) as the final fallback.

    Steps 1, 2 and 4 match the server-side ``parse_dashboard_url()`` logic so
    that ``kirocrew token`` / ``status`` / ``logout`` / ``stop`` all hit the
    same port the gateway is actually bound to when the user has configured a
    non-default ``dashboard.url`` (for example a dev instance on 6777 or an
    alternative prod port like 7778). Steps 3 and 5 cover the case where
    nothing was configured at all but a gateway is up on a non-default port
    (e.g. started with ``kirocrew gateway --port 6776``): the parent's own
    export, or the running gateway's marker, is better evidence than the 5476
    default.
    """
    port, _ = _patchable("resolve_client_port_ex")(cli_port)
    return port


def resolve_client_port_ex(cli_port: int | None) -> tuple[int, bool]:
    """Like :func:`resolve_client_port`, also reporting HOW the port resolved.

    The second element is ``True`` when the port came from positive evidence
    (an explicit flag, an env var, a configured port, or a verified
    run-marker) and ``False`` when resolution fell through to
    ``_DEFAULT_PORT``. Callers that cache a resolution for the process
    lifetime need the distinction: a fall-through only proves nothing was
    discoverable *at that instant* — during gateway boot the run-marker may
    not be written yet — so pinning it would freeze the weakest outcome
    forever, while every positive source is stable for the process lifetime.
    """
    port, source = _patchable("resolve_client_port_src")(cli_port)
    return port, source != "default"


def resolve_client_port_src(cli_port: int | None) -> tuple[int, str]:
    """Like :func:`resolve_client_port`, also reporting WHERE the port came from.

    The second element names the chain step that produced the port: ``"cli"``,
    ``"env"`` (``KIROCREW_PORT``), ``"bound"`` (``KIROCREW_BOUND_PORT``),
    ``"config"`` (a port explicitly written in ``dashboard.url``), ``"marker"``
    (the sole gateway-owned run-marker), or ``"default"`` (the fall-through).

    The distinction :func:`resolve_client_port_ex` cannot make — and the reason
    this exists — is ``"marker"`` versus the other positive sources. A flag, an
    env var, or a configured port is a *user decision*, stable for the process
    lifetime and safe to cache. A marker-discovered port is only *verified at
    that instant*: the ownership proof says this user's gateway holds the port
    NOW, not that it always will. A gateway that exits or moves frees the port
    for any local process to rebind, so a caller about to attach a credential
    to a request must re-run this chain (re-verifying ownership) rather than
    trust a cached marker resolution.
    """
    if cli_port is not None:
        return cli_port, "cli"
    env_port = os.environ.get("KIROCREW_PORT")
    if env_port:
        try:
            return int(env_port), "env"
        except ValueError:
            # Fall through to bound/config/marker/default — main() validates
            # this early, but guard here too in case the helper is reached via
            # another path.
            pass
    bound_port = os.environ.get("KIROCREW_BOUND_PORT")
    if bound_port:
        try:
            return int(bound_port), "bound"
        except ValueError:
            pass
    cfg_port = _patchable("_config_url_port")()
    if cfg_port:
        return cfg_port, "config"
    discovered = _patchable("_marker_port")()
    if discovered:
        return discovered, "marker"
    return _DEFAULT_PORT, "default"


def resolve_serving_port() -> int:
    """The port THIS gateway process is serving, for its own in-process callers.

    Distinct from :func:`resolve_client_port_ex` in ONE way that matters:
    ``KIROCREW_BOUND_PORT`` is consulted BEFORE ``KIROCREW_PORT``. The client
    resolver reads ``KIROCREW_PORT`` first, which is correct for a CLI client --
    there the variable means "talk to that instance". This resolver is for code
    running INSIDE the gateway (the frame relay, cron dial-port minting, the cron
    trigger endpoint): there the port the process actually bound is ground truth,
    and ``KIROCREW_PORT`` is a request that may be stale or merely inherited. A
    shell that exported ``KIROCREW_PORT=5476`` and then started a second gateway
    with ``--port auto`` leaves both set; the client order would pick 5476, a
    SIBLING, and an in-gateway caller pairing a credential with that port
    authenticates against the wrong instance.

    Reordering the client resolver instead would fix these callers by breaking
    every CLI client's ability to aim at a chosen instance, so the two resolvers
    stay separate. After the bound port, the remaining precedence (``KIROCREW_PORT``
    -> configured -> marker -> default) is shared with the client resolver, so a
    gateway with no bound port exported still honours a dev instance's
    ``KIROCREW_PORT``.

    Returns the port only. Every in-gateway caller reads a per-port credential for
    the returned value, and an unresolved credential reads empty and is refused by
    the strict ingress (fail-closed), so no separate evidence flag is needed.
    """
    bound_port = os.environ.get("KIROCREW_BOUND_PORT")
    if bound_port:
        try:
            return int(bound_port)
        except ValueError:
            pass  # malformed value is no evidence; fall through to the client order
    port, _evidence_backed = resolve_client_port_ex(None)
    return port


def pin_gateway_child_port(
    env: MutableMapping[str, str],
    *,
    parent_env: Mapping[str, str] | None = None,
) -> None:
    """Pin a gateway-owned child's callback target to the port its parent serves.

    A gateway can inherit ``KIROCREW_PORT`` from the shell that launched it and
    then bind a different port through ``--port`` or ``--port auto``. Generic
    client processes must continue to prefer that explicit target, but an ACP
    child belongs to the gateway that spawned it: its authenticated MCP callbacks
    must return to that parent, never to the inherited sibling.

    Read bound truth from the parent environment rather than *env*, because ACP
    registry and adapter environment overlays are merged into the child mapping
    before this guard runs. Those overlays may configure the adapter, but they
    cannot retarget Kiro Crew's control-plane callbacks.

    When no validated bound port exists, leave *env* unchanged. This preserves
    direct CLI and pre-listen spawn behavior, where ``KIROCREW_PORT`` remains the
    only explicit target available.
    """
    source = os.environ if parent_env is None else parent_env
    raw_bound = source.get("KIROCREW_BOUND_PORT", "")
    try:
        bound = int(raw_bound)
    except (TypeError, ValueError):
        return
    if not 1 <= bound <= 65535:
        return
    value = str(bound)
    env["KIROCREW_BOUND_PORT"] = value
    env["KIROCREW_PORT"] = value


# Subcommands that launch a long-running Kiro Crew *server* process which
# ``kirocrew stop`` may need to terminate. These mirror the entry-point
# subcommands dispatched in ``cli.py`` (``gateway`` / ``dashboard``; ``start``
# is the historical alias). The task runner (``run``) is intentionally excluded:
# it is not bound to the dashboard port, so we must never SIGTERM it from
# ``kirocrew stop``.
_KIROCREW_SERVER_SUBCOMMANDS = frozenset({"gateway", "dashboard", "start"})


def _basename_stem(tok: str) -> str:
    """Basename of *tok* without a Windows ``.exe`` suffix.

    Lets the venv launchers ``python.exe`` / ``kirocrew.exe`` match the same
    checks as their POSIX ``python`` / ``kirocrew`` counterparts. ``shlex.split``
    with ``posix=False`` leaves quotes on some tokens, so strip them too.

    Split on BOTH separators explicitly rather than via ``os.path.basename``:
    that is host-dependent (``posixpath`` on Linux does NOT split backslashes),
    so a Windows cmdline classified on the Linux CI fleet would keep its full
    ``D:\\...\\kirocrew.exe`` path and never match. This is host-independent — a
    basename is whatever follows the last ``/`` or ``\\``.

    Module scope rather than nested in :func:`_args_look_like_kirocrew` so
    :func:`kiro_crew.cli_server._own_console_script` shares the one definition.
    """
    cleaned = tok.strip('"')
    base = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base


def _args_look_like_kirocrew(args: str) -> bool:
    """Return ``True`` if a process command-line *args* string is a Kiro Crew server.

    This gates ``os.kill(pid, SIGTERM)`` in ``cli_server._stop``, so it must be
    **precise** (never match an unrelated process that merely mentions
    "kirocrew") while still recognising *every* way the gateway can be spawned.

    Instead of enumerating brittle substring variants (``kiro_crew.gateway`` vs
    ``kiro_crew gateway`` vs ``kirocrew gateway`` …), we parse the command line
    *structurally* and key on the real module/binary name plus a known server
    subcommand (:data:`_KIROCREW_SERVER_SUBCOMMANDS`). This is deterministic and
    robust to interpreter path, Python version suffix, and whitespace. Two spawn
    shapes are recognised:

    * **Module invocation** — ``<python> -m kiro_crew <subcmd>`` (the form used by
      a service install and the launchd/systemd service), plus the legacy dotted
      form ``<python> -m kiro_crew.<subcmd>``. A Python interpreter must precede
      ``-m`` so we don't misread some other tool's ``-m`` flag (e.g. ``grep -m``).
    * **Console script** — ``/path/to/kirocrew <subcmd>`` (used when the
      ``kirocrew`` wrapper resolves on ``PATH``).

    Examples::

        >>> _args_look_like_kirocrew("/x/python3.10 -m kiro_crew gateway")
        True
        >>> _args_look_like_kirocrew("python3 -m kiro_crew.dashboard")
        True
        >>> _args_look_like_kirocrew("/usr/local/bin/kirocrew start")
        True
        >>> _args_look_like_kirocrew("python -m kiro_crew run /tmp/spec.md")  # task runner
        False
        >>> _args_look_like_kirocrew("vim /tmp/kirocrew-notes.txt")
        False
    """
    # ``ps -o args=`` (POSIX) / Win32_Process.CommandLine (Windows WMI) return a
    # shell-style string; tokenize it the way the host shell would. On Windows
    # use posix=False so backslash path separators survive (default posix=True
    # eats them: ``C:\Py\python.exe`` -> ``C:Pypython.exe``, breaking the
    # interpreter/basename checks below). Fall back to a naive split on a
    # malformed string (e.g. an odd quote) so this best-effort check never raises.
    try:
        tokens = shlex.split(args, posix=not platform_compat.IS_WINDOWS)
    except ValueError:
        tokens = args.split()

    for index, token in enumerate(tokens):
        # --- Module form: "<python> -m kiro_crew <subcmd>" / "-m kiro_crew.<subcmd>"
        if token == "-m" and index + 1 < len(tokens):
            # Only treat "-m" as Python's module flag when a Python interpreter
            # precedes it; otherwise an unrelated tool's "-m" option could be
            # misread (e.g. "grep -m kiro_crew gateway file"). The match is
            # case-insensitive: the macOS framework build's interpreter basename
            # is "Python" (capital P), which a case-sensitive startswith would
            # miss, leaving stop/restart unable to find the gateway.
            interpreter_seen = any(
                _basename_stem(t).lower().startswith("python") for t in tokens[:index]
            )
            if interpreter_seen:
                # "kiro_crew.gateway" -> ("kiro_crew", "gateway"); a bare
                # "kiro_crew" -> ("kiro_crew", "").
                package, _, dotted_subcmd = tokens[index + 1].partition(".")
                if package == "kiro_crew":
                    # Dotted submodule form: ``-m kiro_crew.gateway``.
                    if dotted_subcmd in _KIROCREW_SERVER_SUBCOMMANDS:
                        return True
                    # Subcommand-as-argument form: ``-m kiro_crew gateway``. The
                    # subcommand is argparse's first positional after the module,
                    # i.e. always at index+2. Check only that slot so a later
                    # positional/flag value cannot match — e.g.
                    # ``-m kiro_crew run gateway`` ("gateway" is a file argument
                    # to the task runner) must NOT be treated as a server.
                    if (
                        index + 2 < len(tokens)
                        and tokens[index + 2] in _KIROCREW_SERVER_SUBCOMMANDS
                    ):
                        return True

        # --- Console-script form: ".../kirocrew <subcmd>" (or kirocrew.exe on Win)
        if (
            _basename_stem(token) == "kirocrew"
            and index + 1 < len(tokens)
            and tokens[index + 1] in _KIROCREW_SERVER_SUBCOMMANDS
        ):
            return True

    return False


def _is_kirocrew_process(pid: int) -> bool:
    """Return ``True`` if *pid* looks like a Kiro Crew gateway process.

    Resolves the process command line cross-platform via
    :func:`platform_compat.process_command_line` (Linux ``/proc``, macOS ``ps``,
    Windows ``Win32_Process`` WMI — the venv ``kirocrew.exe`` re-execs
    ``python.exe`` so the image name alone is ambiguous there) and defers
    classification to :func:`_args_look_like_kirocrew`.

    ``process_command_line`` returns ``""`` on any failure (dead PID, missing
    ``ps``, WMI error), which classifies as "not a match" — ``cli_server._stop``'s
    separate ``listening_pid_tool_available()`` check already surfaces the
    tool-absent case, so this never needs to raise.
    """
    out = platform_compat.process_command_line(pid)
    if not out:
        return False
    return _args_look_like_kirocrew(out)
