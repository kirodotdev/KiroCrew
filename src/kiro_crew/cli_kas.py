"""``kirocrew kas`` -- install, inspect and remove Kiro Crew's own KAS engine bundle.

Operator-facing, like ``kirocrew doctor`` and ``kirocrew sandbox``: it runs in the
operator's own terminal, outside the agent sandbox, which is the only place the
``engines`` leaf is writable (``sandbox._CREW_READONLY_LEAVES``). The work itself
is reached through :mod:`kiro_crew.agent_sdk.kas_bundle`; this module is argument parsing and
wording.
"""

from __future__ import annotations

import argparse
import shutil
import sys
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
            "Install as part of an update: every update runs this from the new "
            "release, and any failure fails the update so no release switches "
            "without its engine. A host the engine cannot run on exits 0"
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

    Only ``engines/kas`` and the engine a platform wheel carries inside the package
    are asked, never the desktop app's staged copy: that one is not where this
    command writes, so it does not make an install here redundant. The packaged copy
    ships with this exact code and is replaced only by the next update, so it does.
    A directory at the version's name that does not read back as an engine is not
    an answer either; ``install_bundle`` then refuses it and names ``--force``.
    An answer skips ``install_bundle``, so the stale staging sweep it would have
    run runs here.
    """
    if force:
        return None
    packaged = kas_bundle.installed_bundle(version=version)
    if packaged is not None and packaged.packaged:
        # A platform wheel carries this exact version inside the package.
        return packaged
    root = kas_bundle.kas_engines_root()
    present = kas_bundle.installed_bundle(root=root, version=version)
    if present is not None:
        kas_bundle.sweep_stale_staging(root)
    return present


def _install(args: argparse.Namespace) -> int:
    """``kirocrew kas install``; with ``--if-in-use``, the step every update runs.

    The update step installs the pinned engine whatever ``agent.kas_bundle`` says,
    and any failure fails the update (the engine guarantee): no release is
    switched to without its engine. A platform wheel carries it, so the step finds
    it and downloads nothing. A host the engine cannot run on at all is the one
    exception: no retry can install it there, so the step says so and exits 0. That
    holds whether the local probe sees it or the pinned manifest does (a platform it
    publishes no build for, such as Windows on ARM): both are ``HOST_UNSUPPORTED_EXIT``.
    """
    if not getattr(args, "if_in_use", False):
        return _install_now(args)
    unsupported = _host_unsupported_reason()
    if unsupported:
        print(f"{unsupported} Nothing was installed; the KAS engine is unavailable here.")
        return 0
    rc = _install_now(args)
    if rc == HOST_UNSUPPORTED_EXIT:
        print("Nothing was installed; the KAS engine is unavailable on this host (see above).")
        return 0
    return rc


def _host_unsupported_reason() -> str:
    """``kas_bundle.host_unsupported_reason()``, or ``''`` when the probe itself fails.

    A probe that fails is not a reason to skip: the install then runs and reports
    whatever stops it.
    """
    try:
        return kas_bundle.host_unsupported_reason()
    except Exception:  # noqa: BLE001 - an unanswerable probe leaves the install to decide
        return ""


#: ``kirocrew kas install``'s exit code when this host cannot run the engine at
#: all, so a retry cannot help. The installer scripts read it to leave out
#: their "retry when online" advice; every other failure exits 1.
HOST_UNSUPPORTED_EXIT = 3


def _install_now(args: argparse.Namespace) -> int:
    """Install the engine *args* name."""
    from kiro_crew.config.loader import KiroCrewConfig

    on_progress = _progress_printer()

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
        if args.force and installed.version == KAS_PIN_VERSION:
            runs = kas_bundle.installed_bundle(version=installed.version)
            if runs is not None and runs.packaged:
                print(
                    f"⚠  This install carries KAS {installed.version} inside the package "
                    f"({runs.root}); that copy runs, not the one just written."
                )
    except kas_bundle.KasHostUnsupported as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return HOST_UNSUPPORTED_EXIT
    except kas_bundle.KasBundleError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return _report_installed(installed, already=False)


def _report_installed(installed: kas_bundle.InstalledKasBundle, *, already: bool) -> int:
    if already and installed.packaged:
        print(f"✅ KAS {installed.version} is built into this install at {installed.root}")
    elif already:
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
    # Decide the closing advice from the route the next spawn actually takes --
    # the same predicate `kirocrew doctor` reads (kas_bundle.kas_route) -- not from
    # agent.kas_bundle alone. With the flag off but no kiro-cli (or a signed-out
    # one), the route resolves to the bundle automatically, and telling the user to
    # "turn it on" would contradict doctor's "route: bundle" row.
    route = kas_bundle.kas_route()
    if not route.bundle:
        print(
            "\nThe bundle route is off. Turn it on, then restart:\n"
            "  kirocrew config set agent.kas_bundle true\n"
            "  kirocrew restart"
        )
    elif kas_bundle.route_chosen_by_sharing_preview(route):
        print(
            "\nKiro Crew will use this engine: the ACP runtime sharing preview "
            "picks it while agent.kas_bundle is off. `kirocrew restart` picks "
            "the new engine up."
        )
    elif route.automatic:
        print(
            "\nKiro Crew will use this engine: the bundle route is on "
            "because kiro-cli's relay cannot run here. `kirocrew restart` "
            "picks the new engine up."
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
        # Engine guarantee: a missing engine is a failure on any host that
        # can run one, whatever agent.kas_bundle says.
        return 0 if _host_unsupported_reason() else 1
    if installed.packaged:
        where = "  (built into the wheel)"
    elif installed.bundled:
        where = "  (built into the app)"
    else:
        where = ""
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
        if args.version is None:
            shutil.rmtree(root)
        else:
            # One version goes whole or stays whole; removing every version stays in place.
            kas_bundle.remove_version(args.version, root=root.parent)
    except kas_bundle.KasBundleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
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
