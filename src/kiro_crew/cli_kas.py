"""``kirocrew kas`` -- install, inspect and remove Kiro Crew's own KAS engine bundle.

Operator-facing, like ``kirocrew doctor`` and ``kirocrew sandbox``: it runs in the
operator's own terminal, outside the agent sandbox, which is the only place the
``engines`` leaf is writable (``sandbox._CREW_READONLY_LEAVES``). The work itself
is reached through :mod:`kiro_crew.agent_sdk.kas_bundle`; this module is argument parsing and
wording.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from kiro_crew.agent_sdk import kas_bundle
from kiro_crew.agent_sdk.kas_bundle import (
    KAS_PIN_MANIFEST_SHA256,
    KAS_PIN_VERSION,
    KAS_VERSION_OVERRIDE_ENV,
    pinned_version,
)

__all__ = ["HOST_UNSUPPORTED_EXIT", "add_kas_parser", "kas_cmd"]


def add_kas_parser(parser: argparse.ArgumentParser) -> None:
    """Attach the ``install`` / ``status`` / ``remove`` subcommands to *parser*."""
    sub = parser.add_subparsers(dest="kas_action")

    install = sub.add_parser(
        "install",
        help="Download this release's pinned KAS engine + Node runtime",
    )
    install.add_argument(
        "--version",
        default=None,
        help=(
            f"Engine version to install (default: this release's pin, {KAS_PIN_VERSION}). "
            "Any other version needs --unpinned"
        ),
    )
    install.add_argument(
        "--unpinned",
        action="store_true",
        help=(
            "Accept a manifest this release does not pin (another --version, or a "
            "republished one). For testing; the harness still runs the pinned version "
            f"unless {KAS_VERSION_OVERRIDE_ENV} names the other one"
        ),
    )
    install.add_argument(
        "--url",
        default=None,
        help="https:// base url to fetch from (default: agent.kas_bundle_url)",
    )
    install.add_argument(
        "--from",
        dest="from_dir",
        default=None,
        metavar="DIR",
        help=(
            "Install from a local directory holding manifest.json and the artifacts it "
            "names, instead of downloading (offline installs, mirrors, development)"
        ),
    )
    install.add_argument(
        "--force", action="store_true", help="Replace an already-installed copy of that version"
    )
    install.add_argument(
        "--if-in-use",
        action="store_true",
        help=(
            "Install only when this install runs Kiro Crew's own engine "
            "(agent.kas_bundle is on, or KAS takes that route on its own with an "
            "engine on disk), or refresh one already on disk (a failure then only "
            "prints the retry); otherwise do nothing. Every update runs this from "
            "the new release"
        ),
    )

    sub.add_parser("status", help="Show the pinned engine, what is installed, and the route")
    remove = sub.add_parser("remove", help="Delete installed engine versions")
    remove.add_argument(
        "--version", default=None, help="Delete only this version (default: every version)"
    )
    remove.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    # The sign-in verbs (login / whoami / logout / route) live beside the engine
    # verbs because they share the route: on the bundle route the vault is the
    # engine's only credential. Their wording and flow are cli_kas_login's.
    from kiro_crew.cli_kas_login import add_kas_login_parsers

    add_kas_login_parsers(sub)


def _progress_printer():
    last: dict[str, int] = {}

    def _on_progress(label: str, done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 0
        if last.get(label) == pct // 10:
            return
        last[label] = pct // 10
        print(f"  {label}: {pct:3d}%  ({done / 1_048_576:.1f} / {total / 1_048_576:.1f} MiB)")

    return _on_progress


def _already_installed(version: str, force: bool) -> kas_bundle.InstalledKasBundle | None:
    """The data home's working copy of *version*, unless ``--force`` asked to replace it.

    Only ``engines/kas`` is asked, never the desktop app's staged copy: that one is
    not where this command writes, so it does not make an install here redundant.
    A directory at the version's name that does not read back as an engine is not
    an answer either; ``install_bundle`` then refuses it and names ``--force``.
    """
    if force:
        return None
    return kas_bundle.installed_bundle(root=kas_bundle.kas_engines_root(), version=version)


#: ``install --if-in-use`` needs the engine: the bundle route runs it.
ENGINE_REQUIRED = "required"
#: ``install --if-in-use`` only refreshes an engine already on disk; a failure
#: must not stop an update (the relay route runs kiro-cli, not the engine).
ENGINE_OPTIONAL = "optional"


def _bundle_switch_on() -> bool:
    """Whether ``agent.kas_bundle`` is on, so any KAS session runs Crew's own engine.

    Deliberately not ``kas_bundle_route()``, which also asks that the DEFAULT
    backend be KAS: with the switch on, a member route (``agent.member_acp_backend``),
    a mate's own harness pin or a spawn that asks for KAS runs the engine too, so
    an update must not switch to a release whose engine is missing for them. An
    unreadable config reads as off, the answer every bundle-route reader gives.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return bool(KiroCrewConfig.load().agent.kas_bundle)
    except Exception:  # noqa: BLE001 - unreadable config is the relay route
        return False


def _automatic_bundle_route() -> bool:
    """Whether a KAS spawn here takes the bundle route although the switch is off.

    ``kas_bundle.kas_route()`` (``acp.kas_route.resolve_kas_route``) is the rule
    the spawn itself applies: with no kiro-cli, or one that cannot run a session,
    a KAS spawn runs Crew's own engine. A failed probe reads as the relay, as the
    route's own readers do.
    """
    try:
        return bool(kas_bundle.kas_route().bundle)
    except Exception:  # noqa: BLE001 - an unanswerable probe is the relay route
        return False


def _engine_need() -> str | None:
    """How much this install needs the pinned engine, for ``install --if-in-use``.

    :data:`ENGINE_REQUIRED` when ``agent.kas_bundle`` is on (:func:`_bundle_switch_on`):
    every KAS session, the default's or a member's or a mate's, runs the engine,
    so an update without it is refused. Also required when an engine version is
    on disk and a KAS spawn here takes the bundle route on its own
    (:func:`_automatic_bundle_route`): KAS already runs on that engine, so a
    release whose pin it lacks would stop sessions that worked.
    :data:`ENGINE_OPTIONAL` when an engine is only on disk and the spawn runs
    kiro-cli's relay (the installers lay one down on every default install): the
    update tries to bring the new pin along, and any failure only prints the
    retry. ``None`` otherwise, which includes the automatic route with nothing on
    disk: KAS there never worked before this update either, and the gateway
    installs the engine at the first spawn. An engines directory that cannot be
    listed counts as no engine.
    """
    if _bundle_switch_on():
        return ENGINE_REQUIRED
    try:
        on_disk = bool(kas_bundle.installed_versions())
    except OSError:
        on_disk = False
    if not on_disk:
        return None
    return ENGINE_REQUIRED if _automatic_bundle_route() else ENGINE_OPTIONAL


def _install(args: argparse.Namespace) -> int:
    if not getattr(args, "if_in_use", False):
        return _install_now(args)
    need = _engine_need()
    if need is None:
        print("The KAS engine bundle is not in use on this install; nothing to install.")
        return 0
    if need == ENGINE_REQUIRED:
        return _install_now(args)
    return _install_optional(args)


#: ``kirocrew kas install``'s exit code when this host cannot run the engine at
#: all, so a retry cannot help. The installer scripts read it to leave out
#: their "retry when online" advice; every other failure exits 1.
HOST_UNSUPPORTED_EXIT = 3

#: The line an optional refresh ends with when it did not install the engine.
_OPTIONAL_RETRY_LINE = (
    "⚠  The KAS engine for this release was not installed; agent.kas_bundle is off, "
    "so the update goes on. Retry with: kirocrew kas install"
)

#: The line an optional refresh ends with when this host cannot run the engine.
_OPTIONAL_UNSUPPORTED_LINE = (
    "⚠  This host cannot run the KAS engine (see above); agent.kas_bundle is off, "
    "so the update goes on."
)


class _RefreshDeadlinePassed(kas_bundle.KasBundleError):
    """An optional refresh ran past its own deadline and stopped between chunks."""


def _exit_now(code: int) -> None:
    """End this process at once with *code*; the hard stop's exit, a seam for tests."""
    os._exit(code)


def _install_optional(args: argparse.Namespace) -> int:
    """Refresh an engine only on disk; every way it does not finish exits the optional code.

    The switch is off, so nothing runs the engine: a refused fetch, a full or
    unwritable disk, a slow or stalled download are all reported with the retry,
    never an update failure. The refresh bounds itself below the update's own kill
    of this child (``release_apply.ENGINE_INSTALL_TIMEOUT_SECS``), because that
    kill reads as a failed install: a download still going at
    ``ENGINE_OPTIONAL_DEADLINE_SECS`` stops at its next chunk (the install removes
    its staging directory), and a stall that never reaches a chunk is ended at
    ``ENGINE_OPTIONAL_HARD_STOP_SECS`` by a timer that exits the process. The first
    stdout line, ``ENGINE_OPTIONAL_MARKER``, lets the update read a child that
    still ended some other way (killed by a signal) as this optional case.
    """
    from kiro_crew.platform import release_apply

    print(release_apply.ENGINE_OPTIONAL_MARKER, flush=True)

    def _hard_stop() -> None:
        print(
            f"❌ the engine refresh did not finish within "
            f"{release_apply.ENGINE_OPTIONAL_HARD_STOP_SECS:.0f}s",
            file=sys.stderr,
        )
        print(_OPTIONAL_RETRY_LINE, file=sys.stderr, flush=True)
        _exit_now(release_apply.ENGINE_OPTIONAL_FAILED_EXIT)

    timer = threading.Timer(release_apply.ENGINE_OPTIONAL_HARD_STOP_SECS, _hard_stop)
    timer.daemon = True
    timer.start()
    try:
        rc = _install_now(
            args, deadline=time.monotonic() + release_apply.ENGINE_OPTIONAL_DEADLINE_SECS
        )
    except Exception as exc:  # noqa: BLE001 - an optional refresh never fails the update
        print(f"❌ {exc}", file=sys.stderr)
        rc = 1
    finally:
        timer.cancel()
    if rc == 0:
        return 0
    if rc == HOST_UNSUPPORTED_EXIT:
        print(_OPTIONAL_UNSUPPORTED_LINE, file=sys.stderr)
    else:
        print(_OPTIONAL_RETRY_LINE, file=sys.stderr)
    return release_apply.ENGINE_OPTIONAL_FAILED_EXIT


def _bounded_progress(
    printer: Callable[[str, int, int], None], deadline: float
) -> Callable[[str, int, int], None]:
    """*printer*, raising :class:`_RefreshDeadlinePassed` on the first chunk past *deadline*."""
    from kiro_crew.platform.release_apply import ENGINE_OPTIONAL_DEADLINE_SECS

    def _on_progress(label: str, done: int, total: int) -> None:
        if time.monotonic() >= deadline:
            raise _RefreshDeadlinePassed(
                f"the engine refresh did not finish within {ENGINE_OPTIONAL_DEADLINE_SECS:.0f}s"
            )
        printer(label, done, total)

    return _on_progress


def _install_now(args: argparse.Namespace, *, deadline: float | None = None) -> int:
    """Install the engine *args* name; with *deadline*, stop the download past it."""
    from kiro_crew.config.loader import KiroCrewConfig

    on_progress = _progress_printer()
    if deadline is not None:
        on_progress = _bounded_progress(on_progress, deadline)

    try:
        # Before any "Fetching"/"Installing" line, so a host that cannot run the
        # engine is refused first (install_bundle repeats the same check).
        kas_bundle.require_loadable_host()
        if args.from_dir:
            source_dir = Path(args.from_dir).expanduser().resolve()
            manifest_path = source_dir / kas_bundle.KAS_BUNDLE_MANIFEST_NAME
            if not manifest_path.is_file():
                print(f"❌ {manifest_path} not found", file=sys.stderr)
                return 1
            import json

            # One read: the bytes checked against the pin are the bytes parsed.
            # A second read of a directory this process does not own (a shared
            # offline mirror) could hand the pin check a different file than
            # the one whose artifact hashes the install then trusts.
            try:
                manifest_bytes = manifest_path.read_bytes()
                manifest_doc = json.loads(manifest_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                print(f"❌ {manifest_path} is not a readable JSON manifest: {exc}", file=sys.stderr)
                return 1
            manifest = kas_bundle.parse_manifest(manifest_doc)
            want = args.version or KAS_PIN_VERSION
            if manifest.version != want:
                print(
                    f"❌ {manifest_path} describes KAS {manifest.version}, not {want}",
                    file=sys.stderr,
                )
                return 1
            if not args.unpinned and (
                manifest.version != KAS_PIN_VERSION
                or kas_bundle.sha256_bytes(manifest_bytes) != KAS_PIN_MANIFEST_SHA256
            ):
                print(
                    f"❌ {manifest_path} is not the manifest this release pins "
                    f"(KAS {KAS_PIN_VERSION}); pass --unpinned to install it anyway",
                    file=sys.stderr,
                )
                return 1
            present = _already_installed(manifest.version, args.force)
            if present is not None:
                return _report_installed(present, already=True)
            source = str(source_dir)
        else:
            version = args.version or KAS_PIN_VERSION
            if version != KAS_PIN_VERSION and not args.unpinned:
                print(
                    f"❌ this release pins KAS {KAS_PIN_VERSION}; pass --unpinned to install "
                    f"{version} for testing",
                    file=sys.stderr,
                )
                return 1
            # Asked before any fetch, so re-running an installer on a machine
            # that already has the engine succeeds offline too.
            present = _already_installed(version, args.force)
            if present is not None:
                return _report_installed(present, already=True)
            base = args.url or KiroCrewConfig.load().agent.kas_bundle_url
            print(f"Fetching manifest for KAS {version} from {base} ...")
            manifest, manifest_url = kas_bundle.fetch_manifest(
                base,
                version=version,
                sha256=None if args.unpinned else KAS_PIN_MANIFEST_SHA256,
            )
            source = manifest_url

        print(
            f"Installing KAS {manifest.version} "
            f"({kas_bundle.host_platform_key()}) with Node {manifest.node_version} "
            f"({kas_bundle.host_node_key()}) ..."
        )
        installed = kas_bundle.install_bundle(
            manifest, source=source, force=args.force, on_progress=on_progress
        )
    except kas_bundle.KasHostUnsupported as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return HOST_UNSUPPORTED_EXIT
    except kas_bundle.KasBundleError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return _report_installed(installed, already=False)


def _report_installed(installed: kas_bundle.InstalledKasBundle, *, already: bool) -> int:
    from kiro_crew.config.loader import KiroCrewConfig

    if already:
        print(
            f"✅ KAS {installed.version} is already installed at {installed.root} "
            "(use --force to replace it)"
        )
    else:
        print(f"✅ KAS {installed.version} installed at {installed.root}")
    if installed.version != pinned_version():
        print(
            f"   (not the version the harness runs, {pinned_version()}; set "
            f"{KAS_VERSION_OVERRIDE_ENV}={installed.version} in the gateway's environment to use it)"
        )
    print(f"   engine: {installed.acp_server}")
    print(f"   node:   {installed.node_bin}")
    cfg = KiroCrewConfig.load().agent
    if not cfg.kas_bundle:
        print(
            "\nThe bundle route is off. Turn it on, then restart:\n"
            "  kirocrew config set agent.kas_bundle true\n"
            "  kirocrew restart"
        )
    else:
        print("\nagent.kas_bundle is on; `kirocrew restart` picks the new engine up.")
    print(
        "The engine signs in through Kiro Crew's own login (dashboard → Sign in); "
        "kiro-cli is not used on this route."
    )
    return 0


def _status(_args: argparse.Namespace) -> int:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load().agent
    root = kas_bundle.kas_engines_root()
    runs = pinned_version()
    installed = kas_bundle.installed_bundle()
    print(f"bundle route:  {'on' if cfg.kas_bundle else 'off'}  (agent.kas_bundle)")
    pin_note = "" if runs == KAS_PIN_VERSION else f"  ({KAS_VERSION_OVERRIDE_ENV} override)"
    print(f"pinned:        KAS {KAS_PIN_VERSION}; runs {runs}{pin_note}")
    print(f"manifest base: {cfg.kas_bundle_url}")
    print(f"engines root:  {root}")
    others = [v for v in kas_bundle.installed_versions(root=root) if v != runs]
    if others:
        print(f"also on disk:  {', '.join(others)}  (`kirocrew kas remove --version V`)")
    if installed is None:
        print(f"installed:     KAS {runs} is not installed  (run `kirocrew kas install`)")
        return 0 if not cfg.kas_bundle else 1
    where = "  (built into the app)" if installed.bundled else ""
    print(f"installed:     KAS {installed.version}, Node {installed.node_version or '?'}{where}")
    print(f"  engine: {installed.acp_server}")
    print(f"  node:   {installed.node_bin}")
    return 0


def _remove(args: argparse.Namespace) -> int:
    root = kas_bundle.kas_engines_root()
    # Removing every version is the flag left OUT, never a blank one. A blank
    # --version (an unset "$VAR") is refused before anything is read or deleted:
    # read as falsy it would widen a one-version delete to the whole engines root.
    if args.version is not None and not args.version.strip():
        print("❌ --version is blank; omit it to remove every version", file=sys.stderr)
        return 2
    if args.version is not None:
        if args.version not in kas_bundle.installed_versions(root=root):
            print(f"❌ KAS {args.version} is not installed", file=sys.stderr)
            return 1
        root = root / args.version
    if not root.exists():
        print("nothing installed")
        return 0
    if not args.yes:
        try:
            answer = input(f"Delete {root} and every engine under it? [y/N] ").strip().lower()
        except EOFError:
            # No terminal to answer on (piped or closed stdin) reads as "no".
            print("\naborted: no answer on stdin; pass --yes to remove without asking")
            return 1
        if answer not in ("y", "yes"):
            print("aborted")
            return 1
    try:
        shutil.rmtree(root)
    except OSError as exc:
        print(f"❌ could not remove {root}: {exc}", file=sys.stderr)
        return 1
    print(f"removed {root}")
    return 0


def kas_cmd(args: argparse.Namespace) -> int:
    action = getattr(args, "kas_action", None)
    if action == "install":
        return _install(args)
    if action == "status":
        return _status(args)
    if action == "remove":
        return _remove(args)
    from kiro_crew.cli_kas_login import kas_login_cmd

    handled = kas_login_cmd(args)
    if handled is not None:
        return handled
    print("usage: kirocrew kas {install,status,remove,login,whoami,logout,route}", file=sys.stderr)
    return 2
